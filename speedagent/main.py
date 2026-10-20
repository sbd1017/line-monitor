"""The measurement agent service (systemd: speedagent.service).

Threads: probes (every probe_interval_s), sender (outbox -> backend), control (config/commands/status), and the
main scheduler loop that runs full tests. Signals: SIGTERM/SIGINT stop cleanly; SIGUSR1 requests a manual test.
A file named PAUSE in the state directory pauses full tests even when the backend is unreachable.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import logging
import signal
import sys
import threading
import time
import uuid as uuidlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config as C
from . import latest, policy, probes, sysinfo
from .engines.ookla import Ookla
from .engines.simulated import Simulated
from .engines.speedtestgo import SpeedtestGo
from .store import Store
from .uplink import Uplink, UplinkError

log = logging.getLogger("speedagent")
ENGINE_FAILURES = {"engine_missing", "engine_error", "server_error", "timeout", "parse_error"}


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


class Agent:
    def __init__(self, settings: C.AgentSettings):
        self.s = settings
        self.store = Store(settings.db_path)
        self.key = settings.key()
        self.up = Uplink(settings.backend, settings.agent_id, self.key)
        self.cfg = C.clamp_schedule(self.store.get("config") or {})
        self.state = policy.State.from_dict(self.store.get("policy"))
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.manual = threading.Event()
        self.send_now = threading.Event()
        self.live = {"state": "starting", "phase": None, "progress": None, "live_mbps": None}
        self.live_lock = threading.Lock()
        self.status_dirty = threading.Event()
        self.last_probe = None
        self.last_error = None
        self.sender_down = False
        self.decision = {}
        home = str(settings.state_dir / "engine-home")
        if settings.simulate:
            self.engines = {"simulated": Simulated(settings.simulate)}
            self.cfg["engine"], self.cfg["fallback_engine"] = "simulated", "none"
        else:
            self.engines = {"ookla": Ookla(settings.ookla_bin, home), "speedtest-go": SpeedtestGo(settings.speedtest_go_bin, home)}
        self.iface, self.gateway = settings.iface, settings.gateway
        self._route()

    # ------------------------------------------------------------ helpers

    def _route(self):
        iface, gw = sysinfo.default_route()
        self.iface = self.s.iface or iface
        self.gateway = self.s.gateway or gw

    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.cfg["timezone"])
        except Exception:
            return ZoneInfo("Europe/London")

    def today(self) -> str:
        return datetime.now(self.tz()).strftime("%Y-%m-%d")

    def fp(self, ip: str | None) -> str | None:
        return hmac.new(self.key, ip.encode(), hashlib.sha256).hexdigest()[:8] if ip else None

    def event(self, type_: str, detail: dict | None = None):
        t = now_ms()
        self.store.add("event", {"uuid": str(uuidlib.uuid4()), "agent_id": self.s.agent_id, "t_ms": t, "at": iso(t),
                                 "type": type_, "detail": json.dumps(detail or {}, separators=(",", ":"))})
        self.send_now.set()
        log.info("event %s %s", type_, detail or "")

    def save_state(self):
        self.store.put("policy", self.state.to_dict())

    def paused(self) -> bool:
        if (self.s.state_dir / "PAUSE").exists():
            return True
        until = self.cfg.get("paused_until")
        if until:
            try:
                return datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp() * 1000 > now_ms()
            except ValueError:
                return False
        return False

    def publish_latest(self, rec: dict):
        if self.s.latest_file:
            latest.write(self.s.latest_file, rec, iso(now_ms()))

    def set_live(self, **kw):
        with self.live_lock:
            self.live.update(kw)
        self.status_dirty.set()

    # ------------------------------------------------------------ threads

    def probe_loop(self):
        while not self.stop.is_set():
            iv = self.cfg["probe_interval_s"]
            wait = iv - (time.time() % iv)
            if self.stop.wait(wait):
                return
            if self.live.get("state") == "testing":
                continue                                # a probe now would measure the test's own load
            self.probe_once(iv)

    def probe_once(self, iv: int) -> dict:
        self._route()
        t = now_ms()
        try:
            r = probes.run(self.s, self.iface, self.gateway)
        except Exception as exc:                        # never let a probe bug kill the loop
            log.warning("probe failed: %s", exc)
            return {}
        rec = {"uuid": str(uuidlib.uuid4()), "agent_id": self.s.agent_id, "t_ms": t, "at": iso(t), "interval_s": iv, **r}
        self.store.add("probe", rec)
        prev = self.last_probe
        self.last_probe = rec
        self.store.put("last_alive_ms", t)
        if prev and prev.get("link_speed_mbps") != rec.get("link_speed_mbps"):
            self.event("link_change", {"from_mbps": prev.get("link_speed_mbps"), "to_mbps": rec.get("link_speed_mbps")})
        if prev and (prev["state"] in ("wan_down", "dns_fail")) != (rec["state"] in ("wan_down", "dns_fail")):
            self.event("internet_" + ("down" if rec["state"] in ("wan_down", "dns_fail") else "up"), {"state": rec["state"]})
        self.status_dirty.set()
        return rec

    def sender_loop(self):
        backoff = 2
        while not self.stop.is_set():
            batch = self.store.pending(200)
            if not batch:
                self.send_now.wait(5)
                self.send_now.clear()
                continue
            items = [{"stream": b["stream"], "record": json.loads(b["record"]), "record_hash": b["record_hash"],
                      **({"raw": b["raw"]} if b["stream"] == "measurement" else {})} for b in batch]
            try:
                res = self.up.send(items)
                ok = list(res.get("accepted", [])) + list(res.get("duplicates", []))
                rej = {r["uuid"]: r.get("error", "") for r in res.get("rejected", []) if r.get("uuid")}
                self.store.mark(ok, rej)
                if rej:
                    log.error("backend rejected %d record(s): %s", len(rej), list(rej.values())[:3])
                backoff = 2
            except UplinkError as exc:
                self.store.failed_attempt([b["seq"] for b in batch], str(exc))
                if not self.sender_down:
                    log.warning("backend unreachable (%s); records stay queued until it's back", exc)
                self.sender_down = True
                self.send_now.clear()
                self.send_now.wait(backoff)             # the control loop wakes us as soon as the backend answers
                backoff = min(backoff * 2, 60)
                continue
            if self.sender_down:
                log.info("backend reachable again; queue now %d", self.store.queue_stats()["queue_depth"])
            self.sender_down = False

    def control_loop(self):
        last_cfg = 0.0
        while not self.stop.is_set():
            testing = self.live.get("state") == "testing"
            if time.monotonic() - last_cfg > (30 if testing else 15):
                last_cfg = time.monotonic()
                try:
                    res = self.up.config()
                    new = C.clamp_schedule(res.get("config") or {})
                    if self.s.simulate:
                        new["engine"], new["fallback_engine"] = "simulated", "none"
                    changed = sorted(k for k in new if new[k] != self.cfg.get(k))
                    if changed:
                        self.cfg = new
                        self.store.put("config", new)
                        self.event("config_applied", {"keys": changed})
                        self.wake.set()
                    for cmd in res.get("commands", []):
                        if cmd.get("kind") == "run_test":
                            self.manual.set()
                            self.wake.set()
                    if self.sender_down:
                        self.send_now.set()
                except UplinkError:
                    pass                                # unreachable backend: the sender logs it and retries
            try:
                self.up.status(self.status())
            except UplinkError:
                pass
            self.status_dirty.wait(0.8 if testing else 15)
            self.status_dirty.clear()

    def status(self) -> dict:
        q = self.store.queue_stats()
        with self.live_lock:
            live = dict(self.live)
        lp = self.last_probe or {}
        ln = sysinfo.link(self.iface)
        return {**live, **q, "version": C.VERSION, "engine": self.cfg["engine"], "mode": self.state.mode,
                "tests_today": self.state.tests_today, "cap": self.cfg["max_tests_per_day"], "paused": self.paused(),
                "next_test_ms": self.decision.get("until") if self.decision.get("action") == "wait" else None,
                "next_kind": self.decision.get("kind"), "next_scheduled_ms": self.decision.get("next_scheduled_ms"),
                "interval_min": self.cfg["interval_min"],
                "link": {"speed": ln["link_speed_mbps"], "duplex": ln["link_duplex"], "state": ln["link_state"],
                         "iface": self.iface},
                "last_probe": {"t": lp.get("t_ms"), "state": lp.get("state"), "ext": lp.get("ext_rtt_ms"),
                               "gw": lp.get("gw_rtt_ms"), "loss": lp.get("ext_loss_pct")},
                "last_error": self.last_error, "uptime_s": sysinfo.uptime_s(), "throttled": sysinfo.throttled()}

    # ------------------------------------------------------------ full tests

    def quiet_check(self) -> tuple[float | None, float | None, int]:
        deferred = 0
        while True:
            a = sysinfo.counters(self.iface)
            time.sleep(3)
            b = sysinfo.counters(self.iface)
            rx, tx = sysinfo.rate_mbps(a, b, "rx_bytes"), sysinfo.rate_mbps(a, b, "tx_bytes")
            busy = max(rx or 0, tx or 0) > self.cfg["quiet_threshold_mbps"]
            if not busy or deferred >= self.cfg["quiet_max_defer"] or self.stop.is_set():
                if busy:
                    self.event("test_not_quiet", {"rx_mbps": round(rx or 0, 1), "tx_mbps": round(tx or 0, 1), "deferred": deferred})
                return rx, tx, deferred
            deferred += 1
            self.event("test_deferred", {"reason": "Pi busy", "rx_mbps": round(rx or 0, 1), "tx_mbps": round(tx or 0, 1)})
            self.set_live(state="waiting", phase="quiet")
            if self.stop.wait(30):
                return rx, tx, deferred

    def pick_server(self) -> str | None:
        mode, servers = self.cfg["server_mode"], self.cfg["servers"]
        if mode == "auto" or not servers:
            return None
        n = self.store.get("server_rr", 0)
        self.store.put("server_rr", n + 1)
        return servers[n % len(servers)] if mode == "pinned" else servers[0]

    def run_test(self, kind: str, reason: str, related: str | None = None, engine_name: str | None = None,
                 precheck: bool = True) -> dict:
        tz = self.tz()
        uid = str(uuidlib.uuid4())
        engine_name = engine_name or self.cfg["engine"]
        engine = self.engines.get(engine_name)
        self.set_live(state="testing", phase="precheck", progress=0, live_mbps=None, kind=kind, test_uuid=uid,
                      started_ms=now_ms(), engine_name=engine_name)
        pre = self.probe_once(self.cfg["probe_interval_s"]) if precheck else (self.last_probe or {})
        rx0, tx0, deferred = (None, None, 0)
        if pre.get("state") not in ("link_down", "lan_fault", "wan_down", "dns_fail"):
            rx0, tx0, deferred = self.quiet_check()
        self._route()
        link = sysinfo.link(self.iface)
        l1, l5 = sysinfo.load()
        temp, thr = sysinfo.cpu_temp(), sysinfo.throttled()
        mem, swap = sysinfo.memory()
        t0 = now_ms()
        c0 = sysinfo.counters(self.iface)
        self.set_live(state="testing", phase="ping", progress=0, started_ms=t0)
        last_push = [0.0]

        def progress(phase, frac, mbps, latency):
            if time.monotonic() - last_push[0] > 0.4 or frac == 1:
                last_push[0] = time.monotonic()
                self.set_live(phase=phase, progress=frac, live_mbps=mbps, latency=latency)

        if pre.get("state") in ("link_down", "lan_fault", "wan_down", "dns_fail"):
            from .engines.base import EngineResult
            detail = {"link_down": "the Pi's Ethernet link is down", "lan_fault": "the router did not answer",
                      "wan_down": "router reachable but the Internet is not (pre-test probe)",
                      "dns_fail": "name resolution failed (pre-test probe)"}[pre["state"]]
            res = EngineResult(False, failure_code=pre["state"], failure_detail=detail, raw=json.dumps({"precheck": pre}),
                               engine_version=engine.version() if engine else None)
        elif engine is None:
            from .engines.base import EngineResult
            res = EngineResult(False, failure_code="engine_missing", failure_detail=f"unknown engine {engine_name}")
        else:
            server = None if kind == "fallback" else self.pick_server()   # a fallback tests the line, not one server
            res = engine.run(server, progress, self.s.test_timeout_s)
            if not res.ok and server and self.cfg["server_mode"] == "prefer":
                res2 = engine.run(None, progress, self.s.test_timeout_s)
                if res2.ok:
                    res = res2
        if not res.ok and self.stop.is_set():
            # systemd stops only the agent (KillMode=mixed), but if the engine still died during a shutdown,
            # say so plainly: this is not a line failure.
            res.failure_code, res.failure_detail = "interrupted", f"stopped by a service shutdown or restart ({res.failure_detail})"
        t1 = now_ms()
        c1 = sysinfo.counters(self.iface)
        f = res.fields
        rec = {"uuid": uid, "agent_id": self.s.agent_id, "source": "synthetic" if self.s.simulate else "agent", "kind": kind,
               "trigger_reason": reason, "related_uuid": related, "engine": engine_name,
               "engine_version": res.engine_version, "t_ms": t0, "started_at": iso(t0), "ended_at": iso(t1),
               "duration_ms": t1 - t0, "tz_name": str(tz), "utc_offset_min": int(datetime.fromtimestamp(t0 / 1000, tz)
                                                                                    .utcoffset().total_seconds() // 60),
               "status": "ok" if res.ok else "failed", "failure_code": res.failure_code,
               "failure_detail": res.failure_detail, **{k: v for k, v in f.items()},
               "iface": link["iface"], "link_speed_mbps": link["link_speed_mbps"], "link_duplex": link["link_duplex"],
               "link_state": link["link_state"]}
        stored = self.store.add("measurement", rec, res.raw)
        self.publish_latest(rec)
        window = (c1["t"] - c0["t"]) * 1000
        rxd, txd = sysinfo.delta(c0, c1, "rx_bytes"), sysinfo.delta(c0, c1, "tx_bytes")
        bg_rx = bg_tx = None
        if res.ok and rxd is not None and f.get("download_bytes") and window > 0:
            # Estimate of OTHER traffic on the Pi's port during the test: interface bytes minus the test's payload,
            # allowing ~5% for TCP/IP framing and ~3% for the opposite direction's ACKs.
            dlb, ulb, secs = f["download_bytes"], f.get("upload_bytes") or 0, window / 1000
            bg_rx = max(0.0, (rxd - dlb * 1.05 - ulb * 0.03) * 8 / secs / 1e6)
            bg_tx = max(0.0, (txd - ulb * 1.05 - dlb * 0.03) * 8 / secs / 1e6)
        last_ip = self.store.get("public_ip_fp")
        ip_fp = self.fp(res.public_ip)
        changed = int(bool(ip_fp and last_ip and ip_fp != last_ip))
        if ip_fp:
            self.store.put("public_ip_fp", ip_fp)
        if changed:
            self.event("public_ip_change", {"from": last_ip, "to": ip_fp})
        diag = {"uuid": str(uuidlib.uuid4()), "measurement_uuid": uid, "agent_id": self.s.agent_id, "t_ms": t0,
                "cpu_load1": l1, "cpu_load5": l5, "cpu_temp_c": temp, "throttled": thr, "mem_available_pct": mem,
                "swap_used_pct": swap, "iface": link["iface"], "link_speed_mbps": link["link_speed_mbps"],
                "link_duplex": link["link_duplex"], "link_state": link["link_state"],
                "rx_errors_delta": sysinfo.delta(c0, c1, "rx_errors"), "tx_errors_delta": sysinfo.delta(c0, c1, "tx_errors"),
                "rx_dropped_delta": sysinfo.delta(c0, c1, "rx_dropped"), "tx_dropped_delta": sysinfo.delta(c0, c1, "tx_dropped"),
                "rx_bytes_delta": rxd, "tx_bytes_delta": txd, "window_ms": int(window), "background_rx_mbps": bg_rx,
                "background_tx_mbps": bg_tx, "pretest_rx_mbps": rx0, "pretest_tx_mbps": tx0,
                "gateway_rtt_ms": pre.get("gw_rtt_ms"), "gateway_loss_pct": pre.get("gw_loss_pct"),
                "external_rtt_ms": pre.get("ext_rtt_ms"), "external_loss_pct": pre.get("ext_loss_pct"),
                "dns_ok": pre.get("dns_ok"), "public_ip": res.public_ip, "public_ip_changed": changed,
                "pi_uptime_s": sysinfo.uptime_s(), "boot_id": sysinfo.boot_id(), "agent_version": C.VERSION,
                "extra": json.dumps({"deferred": deferred, "precheck_state": pre.get("state"), "server_mode": self.cfg["server_mode"]},
                                    separators=(",", ":"))}
        self.store.add("diagnostic", diag)
        self.send_now.set()
        log.info("test %s %s: %s", kind, engine_name, f"{f.get('download_mbps', 0):.1f}/{f.get('upload_mbps') or 0:.1f} Mbps"
                 if res.ok else f"FAILED {res.failure_code}: {res.failure_detail}")
        self.last_error = None if res.ok else f"{res.failure_code}: {res.failure_detail}"
        self.set_live(state="idle", phase=None, progress=None, live_mbps=None, last_test_uuid=uid)
        out = {"uuid": uid, "status": stored["status"], "download_mbps": stored.get("download_mbps"),
               "failure_code": stored.get("failure_code")}
        fb = self.cfg["fallback_engine"]
        if (not res.ok and res.failure_code in ENGINE_FAILURES and fb != "none" and fb != engine_name and fb in self.engines
                and kind != "fallback" and not self.stop.is_set()):
            fb_res = self.run_test("fallback", f"{engine_name} failed: {res.failure_code}", related=uid, engine_name=fb,
                                   precheck=False)
            out["fallback"] = fb_res
            if fb_res["status"] == "ok":                # the line worked: judge the slot by the fallback
                out = {**fb_res, "uuid": uid, "fallback": fb_res}
        return out

    # ------------------------------------------------------------ lifecycle

    def start_events(self):
        bid = sysinfo.boot_id()
        prev_bid = self.store.get("boot_id")
        clean = self.store.get("clean_stop")
        last_alive = self.store.get("last_alive_ms")
        self.store.put("boot_id", bid)
        self.store.put("clean_stop", False)
        synced, waited = sysinfo.clock_synced(), 0
        while synced is False and waited < 180 and not self.stop.is_set():
            self.set_live(state="waiting", phase="clock")
            time.sleep(5)
            waited += 5
            synced = sysinfo.clock_synced()
        self.event("agent_start", {"version": C.VERSION, "rebooted": bool(prev_bid and bid != prev_bid),
                                   "previous_clean_stop": clean, "boot_ms": sysinfo.boot_ms(),
                                   "last_alive_ms": last_alive, "clock_synced": synced, "clock_wait_s": waited,
                                   "engines": {k: e.version() for k, e in self.engines.items()},
                                   "iface": self.iface})

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: (self.stop.set(), self.wake.set()))
        signal.signal(signal.SIGINT, lambda *_: (self.stop.set(), self.wake.set()))
        signal.signal(signal.SIGUSR1, lambda *_: (self.manual.set(), self.wake.set()))
        threads = [threading.Thread(target=fn, daemon=True, name=fn.__name__)
                   for fn in (self.sender_loop, self.control_loop)]
        for t in threads:
            t.start()
        self.start_events()
        self.set_live(state="idle")
        threading.Thread(target=self.probe_loop, daemon=True, name="probe_loop").start()
        self.store.prune()
        last_prune = time.monotonic()
        while not self.stop.is_set():
            now = now_ms()
            d = policy.decide(self.s.agent_id, self.cfg, self.state, now, self.today(), self.paused(), self.manual.is_set())
            self.decision = d
            if d["action"] == "run":
                if d["kind"] == "manual":
                    self.manual.clear()
                try:
                    out = self.run_test(d["kind"], d["reason"], d.get("related"))
                except Exception as exc:          # a bug must not stop monitoring; record it as a failed test
                    log.exception("test crashed")
                    out = self._record_crash(d, exc)
                change = policy.after(self.cfg, self.state, d["kind"], out, now_ms(), d.get("slot"), out.get("uuid"))
                if change:
                    self.event("mode_change", {"change": change, "after": d["kind"]})
                self.save_state()
                continue
            self.save_state()
            self.status_dirty.set()
            wait = max(0.5, min(30.0, (d["until"] - now) / 1000))
            self.wake.wait(wait)
            self.wake.clear()
            if time.monotonic() - last_prune > 86400:
                self.store.prune()
                last_prune = time.monotonic()
        self.store.put("clean_stop", True)
        self.event("agent_stop", {"reason": "signal"})
        deadline = time.monotonic() + 8                 # best effort: deliver what's queued before exiting
        while self.store.queue_stats()["queue_depth"] and time.monotonic() < deadline:
            batch = self.store.pending(500)
            items = [{"stream": b["stream"], "record": json.loads(b["record"]), "record_hash": b["record_hash"],
                      **({"raw": b["raw"]} if b["stream"] == "measurement" else {})} for b in batch]
            try:
                res = self.up.send(items)
                self.store.mark(list(res.get("accepted", [])) + list(res.get("duplicates", [])),
                                {r["uuid"]: r.get("error", "") for r in res.get("rejected", []) if r.get("uuid")})
            except UplinkError:
                break                                   # backend down: the outbox keeps them for next start
        log.info("stopped; %d record(s) left queued", self.store.queue_stats()["queue_depth"])

    def _record_crash(self, d, exc) -> dict:
        t = now_ms()
        uid = str(uuidlib.uuid4())
        tz = self.tz()
        rec = {"uuid": uid, "agent_id": self.s.agent_id, "source": "synthetic" if self.s.simulate else "agent",
               "kind": d["kind"], "trigger_reason": d.get("reason"), "engine": self.cfg["engine"], "t_ms": t,
               "started_at": iso(t), "tz_name": str(tz),
               "utc_offset_min": int(datetime.fromtimestamp(t / 1000, tz).utcoffset().total_seconds() // 60),
               "status": "failed", "failure_code": "agent_error", "failure_detail": f"{type(exc).__name__}: {exc}"[:500]}
        self.store.add("measurement", rec, None)
        self.publish_latest(rec)
        self.set_live(state="idle", phase=None)
        return {"uuid": uid, "status": "failed", "failure_code": "agent_error"}


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    settings = C.load()
    if settings.env == "production" and settings.simulate:
        raise SystemExit("simulated engine refused in production")
    Agent(settings).run()


if __name__ == "__main__":
    main()
