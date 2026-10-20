"""Runs the measurement agent's own scheduling policy (speedagent/policy.py) over simulated time, against the made-up
line in world.py, and emits the records the real agent would have sent: full tests with their diagnostics, a probe
every minute, and events. Nothing here touches the network: every "result" is computed from the world model.

The simulator's state (policy state, sequence counter, last probe, boot) is a plain dict, so the generator can hand
it over to the live updater, which carries on from exactly where the history ends.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

from speedagent import policy
from speedmon import canonical, timeutil

from .world import DAY, GO_SERVERS, MIN, SERVERS, TZ, World, h01, rng, rng_uuid

AGENT_ID = "monitor-pi"
AGENT_VERSION = "1.0.0"
OOKLA_VERSION = "1.2.0.84"
GO_VERSION = "1.8.3"
ANCHORS = ("1.1.1.1", "8.8.8.8")
ENGINE_FAILURES = {"engine_missing", "engine_error", "server_error", "timeout", "parse_error"}
SCHEDULE = {"interval_min": 30, "confirm_count": 2, "confirm_delay_s": 120, "burst_interval_min": 10,
            "burst_max_min": 120, "recovery_count": 2, "max_tests_per_day": 96, "probe_interval_s": 60}
ENGINE_ERRORS = [
    ("server_error", "Configuration - Couldn't resolve host name (HostNotFoundException)", 30),
    ("server_error", "Cannot read from socket: Connection reset by peer", 22),
    ("timeout", "no result after 150s", 18),
    ("engine_error", "Upload test failed: Broken pipe", 14),
    ("network_error", "Configuration - Couldn't connect to server (NetworkError)", 16),
]


class Sim:
    def __init__(self, world: World, state: dict | None = None):
        self.w = world
        st = state or {}
        self.policy = policy.State.from_dict(st.get("policy"))
        self.seq = st.get("seq", 1)
        self.t = st.get("t", world.start_ms)
        self.next_probe = st.get("next_probe", (self.t + MIN - 1) // MIN * MIN)
        self.manual_served = st.get("manual_served", 0)             # 'run a test now' requests up to here are done
        self.requests = st.get("requests", [])                       # visitors' requests not served yet
        self.last_probe = st.get("last_probe")                       # {"state", "link"}
        self.boot = st.get("boot")                                   # {"id", "ms"}
        self.ip = st.get("ip")
        self.started = st.get("started", False)

    def state(self) -> dict:
        return {"policy": self.policy.to_dict(), "seq": self.seq, "t": self.t, "next_probe": self.next_probe,
                "manual_served": self.manual_served, "requests": self.requests, "last_probe": self.last_probe, "boot": self.boot, "ip": self.ip,
                "started": self.started, "anchor": self.w.anchor.isoformat(), "seed": self.w.seed, "days": self.w.days}

    # ------------------------------------------------------------ helpers

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq - 1

    def cfg(self, t: int) -> dict:
        pk = self.w.package(t)
        return {**SCHEDULE, "degraded_down": pk["degraded_down"], "target_down": pk["target_down"],
                "severe_down": pk["severe_down"]}

    def today(self, t: int) -> str:
        return self.w.clock.parts(t)[0]

    def event(self, emit, t: int, type_: str, detail: dict) -> None:
        r = rng(self.w.seed, "event", type_, t)
        emit("event", {"uuid": rng_uuid(r), "agent_id": AGENT_ID, "agent_seq": self._next_seq(), "t_ms": t,
                       "at": timeutil.iso(t), "type": type_, "detail": json.dumps(detail, separators=(",", ":"))})

    def fp(self, ip: str) -> str:
        return hashlib.blake2b(f"{self.w.seed}:{ip}".encode(), digest_size=4).hexdigest()

    def public_ip(self, t: int) -> str:
        """The house's public address: a documentation-range stand-in that changes every few months."""
        k = (t - self.w.start_ms) // (137 * DAY)
        for s in self.w.events:                    # a long area outage hands out a new address afterwards
            if s.kind == "outage" and s.b - s.a > 3 * 3_600_000 and t >= s.b:
                k += 1
        return f"203.0.113.{17 + int(h01(self.w.seed, 'ip', k) * 200)}"

    def boot_up(self, emit, t: int, rebooted: bool, clean: bool | None, last_alive: int | None) -> None:
        r = rng(self.w.seed, "boot", t)
        boot_ms = t - (r.randint(38, 70) * 1000 if rebooted or not self.boot else 0)
        if rebooted or not self.boot:
            self.boot = {"id": rng_uuid(r), "ms": boot_ms}
        self.event(emit, t, "agent_start", {
            "version": AGENT_VERSION, "rebooted": bool(rebooted and self.started), "previous_clean_stop": clean,
            "boot_ms": self.boot["ms"], "last_alive_ms": last_alive, "clock_synced": True, "clock_wait_s": 0,
            "engines": {"ookla": OOKLA_VERSION, "speedtest-go": GO_VERSION}, "iface": self.w.iface(t)})
        self.started = True

    # ------------------------------------------------------------ the main loop

    def run(self, t_end: int, emit, requests: list[int] = ()) -> None:
        """Simulate from self.t up to t_end. `requests` are extra 'run a test now' times (live visitors)."""
        w = self.w
        self.requests = sorted(set(self.requests) | set(requests))
        manual = sorted([m for m in w.manual if m >= w.start_ms] + self.requests)
        if not self.started:
            self.boot_up(emit, self.t, rebooted=False, clean=None, last_alive=None)
        now = self.t
        while now < t_end:
            off = w.span(now, "offline")
            if off:
                now = self._offline(emit, off, arrived=now == off.a)
                continue
            pending = [m for m in manual if self.manual_served < m <= now]
            d = policy.decide(AGENT_ID, self.cfg(now), self.policy, now, self.today(now), False, bool(pending))
            if d["action"] == "run":
                t_act = now
            else:
                t_act = d["until"]
                nxt = next((m for m in manual if m > max(now, self.manual_served)), None)
                if nxt is not None and nxt < t_act:
                    t_act = nxt
                if pending:
                    t_act = min(t_act, now + 30_000)
            off_next = next((s for s in w.offline_spans() if s.a > now), None)
            horizon = min(t_act, off_next.a if off_next else t_end, t_end)
            self._probes(emit, horizon)
            if off_next and off_next.a <= min(t_act, t_end - 1):
                now = off_next.a
                continue
            if t_act >= t_end:
                now = t_end
                break
            now = t_act
            if d["action"] != "run":
                continue
            t_done = self._test(emit, now, d, t_end)
            if t_done is None:                      # the test would still be running at t_end: do it next time
                break
            if d["kind"] == "manual":
                self.manual_served = now
                self.requests = [m for m in self.requests if m > now]
            now = t_done
        self.t = now

    def resume(self, emit, now: int, max_gap: int = 45 * MIN) -> None:
        """If nothing ran for longer than max_gap (the updater or the whole Pi was stopped), don't invent the missing
        time: the monitor was off, so the timeline shows a gap and the agent starts again now."""
        if now - self.t <= max_gap:
            return
        last = self.t
        self.t = now
        self.next_probe = (now + MIN - 1) // MIN * MIN
        self.boot_up(emit, now, rebooted=True, clean=False, last_alive=last)

    def _offline(self, emit, off, arrived: bool) -> int:
        """The monitor was off for `off`: a clean stop first if it was one, then a start when it's back."""
        if off.info.get("clean") and arrived:
            self.event(emit, off.a, "agent_stop", {"reason": "signal"})
        back = off.b + int(h01(self.w.seed, "boot-delay", off.a) * 40_000) + 25_000
        self.next_probe = (back + MIN - 1) // MIN * MIN
        self.boot_up(emit, back, rebooted=off.info.get("rebooted", True), clean=bool(off.info.get("clean")),
                     last_alive=off.a - int(h01(self.w.seed, "alive", off.a) * 50_000))
        return back

    def _probes(self, emit, until: int) -> None:
        while self.next_probe < until:
            for t, type_, detail in self.w.agent_events:
                if self.next_probe - MIN <= t < self.next_probe and t >= self.w.start_ms:
                    self.event(emit, t, type_, detail)
            if not self.w.span(self.next_probe, "offline"):
                self._probe(emit, self.next_probe + 3 + int(h01(self.w.seed, "pj", self.next_probe) * 40))
            self.next_probe += MIN

    def _probe(self, emit, t: int) -> dict:
        w = self.w
        r = rng(w.seed, "probe", t)
        kind, _ = w.state(t)
        link = w.link_speed(t)
        rec = {"uuid": rng_uuid(r), "agent_id": AGENT_ID, "agent_seq": None, "t_ms": t, "at": timeutil.iso(t),
               "interval_s": 60, "link_up": 1, "link_speed_mbps": link}
        gw = round(abs(r.gauss(0.43, 0.05)) + 0.2, 3)
        if kind == "lan":
            rec.update(state="lan_fault", gw_rtt_ms=None, gw_loss_pct=100.0, ext_rtt_ms=None, ext_loss_pct=100.0,
                       ext_detail=";".join(f"{a}=-ms/100%" for a in ANCHORS), dns_ok=0, dns_ms=None, http_ok=0, http_ms=None)
        elif kind == "outage":
            rec.update(state="wan_down", gw_rtt_ms=gw, gw_loss_pct=0.0, ext_rtt_ms=None, ext_loss_pct=100.0,
                       ext_detail=";".join(f"{a}=-ms/100%" for a in ANCHORS), dns_ok=0, dns_ms=None, http_ok=0, http_ms=None)
        else:
            base = w.latency(t)
            rtts = [base * r.gauss(1, 0.05) + (0.6 if a == "8.8.8.8" else 0) for a in ANCHORS]
            if r.random() < 0.0025:
                rtts = [x + r.uniform(25, 140) for x in rtts]                     # a momentary spike
            loss = [0.0, 0.0]
            fault = w.fault.has(t)
            for i in range(2):
                if r.random() < (0.07 if fault else 0.004):
                    loss[i] = r.choice([33.3, 33.3, 66.7]) if fault else 33.3   # 1 of 3 pings lost
            ext = sorted(rtts)[0] + (sorted(rtts)[1] - sorted(rtts)[0]) / 2
            dns_ok = 0 if kind == "dns" else 1
            rec.update(gw_rtt_ms=gw, gw_loss_pct=0.0, ext_rtt_ms=round(ext, 3), ext_loss_pct=round(sum(loss) / 2, 2),
                       ext_detail=";".join(f"{a}={round(x, 2)}ms/{l:.0f}%" for a, x, l in zip(ANCHORS, rtts, loss)),
                       dns_ok=dns_ok, dns_ms=round(abs(r.gauss(8.5, 2.5)) + 2, 2) if dns_ok else None,
                       http_ok=dns_ok, http_ms=round(r.gauss(38, 5) + base * 1.5, 2) if dns_ok else None)
            rec["state"] = ("dns_fail" if not dns_ok else
                            "degraded" if ext > 40 or rec["ext_loss_pct"] >= 20 else "ok")
        rec["agent_seq"] = self._next_seq()
        emit("probe", rec)
        prev = self.last_probe
        down = rec["state"] in ("wan_down", "dns_fail")
        if prev and prev.get("link") != link:
            self.event(emit, t + 5, "link_change", {"from_mbps": prev.get("link"), "to_mbps": link})
        if prev and (prev["state"] in ("wan_down", "dns_fail")) != down:
            self.event(emit, t + 6, "internet_" + ("down" if down else "up"), {"state": rec["state"]})
        self.last_probe = {"state": rec["state"], "link": link, "t": t, "ext": rec.get("ext_rtt_ms"),
                           "gw": rec.get("gw_rtt_ms"), "loss": rec.get("ext_loss_pct")}
        return rec

    # ------------------------------------------------------------ full tests

    def _test(self, emit, now: int, d: dict, t_end: int) -> int | None:
        """One run_test() of the agent, plus its fallback if the engine failed. Returns when it finished, or None
        if it would still be running at t_end (then the next run does it: everything is keyed on time)."""
        w = self.w
        kind = d["kind"]
        pre_kind, _ = w.state(now)
        t0 = now + (3_300 if pre_kind == "ok" else 250) + int(h01(w.seed, "t0", now) * 600)
        t1 = t0 + self._duration(t0, "ookla", pre_kind)
        fallback = pre_kind == "ok" and self._engine_failure(t0, "ookla") in ENGINE_FAILURES
        tf0 = t1 + 1_200
        t_last = tf0 + self._duration(tf0, "speedtest-go", pre_kind) if fallback else t1
        if t_last > t_end:
            return None
        pre = self._probe(emit, now)                       # the pre-test probe
        self.next_probe = max(self.next_probe, (now // MIN + 1) * MIN)
        uid, res = self._measure(emit, kind, d.get("reason"), d.get("related"), t0, "ookla", pre, pre_kind)
        out = {"uuid": uid, "status": res["status"], "download_mbps": res.get("download_mbps"),
               "failure_code": res.get("failure_code")}
        if fallback:
            _, fres = self._measure(emit, "fallback", f"ookla failed: {res['failure_code']}", uid, tf0, "speedtest-go",
                                    pre, pre_kind)
            if fres["status"] == "ok":                     # the line worked: the slot is judged by the fallback
                out = {"uuid": uid, "status": "ok", "download_mbps": fres["download_mbps"], "failure_code": None}
        self.next_probe = max(self.next_probe, (t_last + MIN - 1) // MIN * MIN)   # no probes while testing
        change = policy.after(self.cfg(now), self.policy, kind, out, t_last, d.get("slot"), uid)
        if change:
            self.event(emit, t_last + 40, "mode_change", {"change": change, "after": kind})
        return t_last + 50

    def _duration(self, t: int, engine: str, pre_kind: str) -> int:
        if pre_kind in ("outage", "lan", "dns"):
            return 40 + int(h01(self.w.seed, "dshort", t) * 60)
        if self._engine_failure(t, engine) == "timeout":
            return 150_000 + int(h01(self.w.seed, "dto", t) * 400)
        base = 21_500 if engine == "ookla" else 17_800
        return base + int(h01(self.w.seed, "dur", engine, t) * 7_000)

    def _engine_failure(self, t: int, engine: str):
        """The failure code if this engine fails at t (independent of the line being up), else None."""
        p = 0.0045 if engine == "ookla" else 0.05
        if self.w.fault.has(t):
            p *= 6
        u = h01(self.w.seed, "efail", engine, t // 1000)
        if u >= p:
            return None
        pick = u / p * sum(x[2] for x in ENGINE_ERRORS)
        for code, _, weight in ENGINE_ERRORS:
            pick -= weight
            if pick < 0:
                return code
        return ENGINE_ERRORS[-1][0]

    def _measure(self, emit, kind, reason, related, t0, engine, pre, pre_kind):
        w = self.w
        r = rng(w.seed, "test", engine, t0)
        uid = rng_uuid(r)
        link = w.link_speed(t0)
        iface = w.iface(t0)
        pk = w.package(t0)
        ip = self.public_ip(t0)
        version = OOKLA_VERSION if engine == "ookla" else GO_VERSION
        rec = {"uuid": uid, "agent_id": AGENT_ID, "agent_seq": None, "source": "synthetic", "kind": kind,
               "trigger_reason": reason, "related_uuid": related, "engine": engine, "engine_version": version,
               "t_ms": t0, "started_at": timeutil.iso(t0), "tz_name": TZ, "utc_offset_min": timeutil.offset_minutes(t0, TZ),
               "iface": iface, "link_speed_mbps": link, "link_duplex": "full", "link_state": "up"}
        fail = None
        if pre_kind in ("outage", "lan", "dns"):
            code = {"outage": "wan_down", "lan": "lan_fault", "dns": "dns_fail"}[pre_kind]
            fail = (code, {"wan_down": "router reachable but the Internet is not (pre-test probe)",
                           "lan_fault": "the router did not answer", "dns_fail": "name resolution failed (pre-test probe)"}[code])
            raw = json.dumps({"precheck": {k: pre.get(k) for k in ("state", "gw_rtt_ms", "ext_rtt_ms", "ext_loss_pct", "dns_ok",
                                                                    "http_ok", "link_speed_mbps")}})
            dur = self._duration(t0, engine, pre_kind)
        else:
            code = self._engine_failure(t0, engine)
            dur = self._duration(t0, engine, pre_kind)
            if code:
                detail = next(m for c, m, _ in ENGINE_ERRORS if c == code)
                fail = (code, detail)
                raw = json.dumps({"errors": [detail], "exit": 2} if engine == "ookla" else {"stderr": detail})
        if fail:
            rec.update(status="failed", failure_code=fail[0], failure_detail=fail[1], ended_at=timeutil.iso(t0 + dur),
                       duration_ms=dur, server_id=None)
            busy = 0.0
        else:
            fields, raw, busy = self._result(r, t0, engine, link, iface, pk, dur)
            rec.update(status="ok", ended_at=timeutil.iso(t0 + dur), duration_ms=dur, **fields)
        rec["raw_sha256"] = canonical.sha256_text(raw)
        rec["agent_seq"] = self._next_seq()
        emit("measurement", rec, raw)
        changed = 0
        if rec["status"] == "ok":
            if self.ip and self.ip != ip:
                changed = 1
                self.event(emit, t0 + dur + 20, "public_ip_change", {"from": self.fp(self.ip), "to": self.fp(ip)})
            self.ip = ip
        self._diagnostic(emit, r, rec, pre, busy, dur, changed)
        return uid, rec

    def _result(self, r, t0, engine, link, iface, pk, dur):
        w = self.w
        servers = SERVERS if engine == "ookla" else [(a, b, c, 1, 0.99) for a, b, c in GO_SERVERS]
        pick = r.random() * sum(s[3] for s in servers)
        for sponsor, loc, sid, weight, factor in servers:
            pick -= weight
            if pick < 0:
                break
        cong = w.congestion(t0)
        cap = w.line_capacity(t0) * (1 - cong) * w.fault_factor(t0) * factor
        dl = cap * r.gauss(1, 0.022)
        if r.random() < 0.008:
            dl *= r.uniform(0.6, 0.9)                                      # a bad moment on the server's side
        busy = abs(r.gauss(0.35, 0.25))
        if r.random() < 0.016:
            busy = r.uniform(25, 160)                                      # something else on the Pi was downloading
            dl -= busy * 0.8
        ceiling = link * 0.9415
        dl = max(3.0, min(dl, ceiling * r.uniform(0.9955, 0.9995)))
        ul_cap = w.upload_capacity(t0) * (1 - 0.1 * cong) * (r.uniform(0.55, 0.95) if w.fault.has(t0) else 1)
        ul = max(1.0, min(ul_cap * r.gauss(1, 0.01), ceiling * 0.99))
        base = w.latency(t0) - 1.2
        ping = max(4.0, base * r.gauss(1, 0.04))
        jitter = abs(r.gauss(0.55, 0.3)) + cong * 2.5 + (2.5 if w.fault.has(t0) else 0)
        loss = 0.0
        if r.random() < (0.3 if w.fault.has(t0) else 0.035):
            loss = round(r.uniform(0.1, 6.0 if w.fault.has(t0) else 1.2), 1)
        dl_ms = 10_000 + int(r.random() * 4_000) if engine == "ookla" else 9_000 + int(r.random() * 2_000)
        ul_ms = 9_000 + int(r.random() * 3_000) if engine == "ookla" else 8_000 + int(r.random() * 2_000)
        dl_bw, ul_bw = int(dl * 1e6 / 8), int(ul * 1e6 / 8)               # Ookla reports bytes per second
        dl_lat = ping + r.uniform(3, 14) + cong * 70
        ul_lat = ping + r.uniform(9, 30)
        host = f"speedtest-{sid}.{loc.lower()}.example.net:8080"
        dist = round({"London": 18.4, "Manchester": 262.7}[loc] + r.uniform(-0.05, 0.05), 4)
        if engine == "ookla":
            fields = {"download_mbps": dl_bw * 8 / 1e6, "upload_mbps": ul_bw * 8 / 1e6,
                      "download_bytes": int(dl_bw * dl_ms / 1000 * r.uniform(0.97, 1.0)),
                      "upload_bytes": int(ul_bw * ul_ms / 1000 * r.uniform(0.97, 1.0)), "download_ms": dl_ms,
                      "upload_ms": ul_ms, "ping_ms": round(ping, 3), "jitter_ms": round(jitter, 3),
                      "ping_low_ms": round(ping - r.uniform(0.2, 1.4), 3),
                      "ping_high_ms": round(ping + jitter * r.uniform(1.2, 3.5), 3),
                      "dl_latency_ms": round(dl_lat, 3), "ul_latency_ms": round(ul_lat, 3), "packet_loss_pct": loss,
                      "server_id": sid, "server_sponsor": sponsor, "server_location": loc,
                      "server_name": f"{sponsor} – {loc}", "server_country": "United Kingdom", "server_host": host,
                      "distance_km": None, "result_id": None, "result_url": None, "isp": "Virgin Media"}
            raw = json.dumps({
                "type": "result", "timestamp": timeutil.iso(t0 + dur)[:19] + "Z",
                "ping": {"jitter": fields["jitter_ms"], "latency": fields["ping_ms"], "low": fields["ping_low_ms"],
                         "high": fields["ping_high_ms"]},
                "download": {"bandwidth": dl_bw, "bytes": fields["download_bytes"], "elapsed": dl_ms,
                             "latency": {"iqm": fields["dl_latency_ms"], "low": round(ping, 3),
                                         "high": round(dl_lat * 2.2, 3), "jitter": round(jitter * 2, 3)}},
                "upload": {"bandwidth": ul_bw, "bytes": fields["upload_bytes"], "elapsed": ul_ms,
                           "latency": {"iqm": fields["ul_latency_ms"], "low": round(ping, 3),
                                       "high": round(ul_lat * 2.5, 3), "jitter": round(jitter * 3, 3)}},
                "packetLoss": loss, "isp": "Virgin Media",
                "interface": {"internalIp": "<redacted>", "name": iface, "macAddr": "<redacted>", "isVpn": False,
                              "externalIp": "<redacted>"},
                "server": {"id": int(sid), "host": host.split(":")[0], "port": 8080, "name": sponsor, "location": loc,
                           "country": "United Kingdom", "ip": "<redacted>"},
                "demo": "generated, not a real measurement"}, separators=(",", ":"))
        else:
            fields = {"download_mbps": dl_bw * 8 / 1e6, "upload_mbps": ul_bw * 8 / 1e6, "download_ms": dl_ms,
                      "upload_ms": ul_ms, "ping_ms": round(ping, 3), "jitter_ms": round(jitter, 3),
                      "ping_low_ms": round(ping - r.uniform(0.2, 1.0), 3), "ping_high_ms": round(ping + jitter * 2, 3),
                      "packet_loss_pct": loss, "server_id": sid, "server_sponsor": sponsor, "server_location": loc,
                      "server_name": f"{sponsor} – {loc}", "server_country": "United Kingdom", "server_host": host,
                      "distance_km": dist, "isp": None}
            raw = json.dumps({"servers": [{"id": sid, "name": loc, "sponsor": sponsor, "country": "United Kingdom",
                                           "host": host, "distance": dist, "latency": int(ping * 1e6),
                                           "jitter": int(jitter * 1e6), "dl_speed": dl_bw, "ul_speed": ul_bw,
                                           "test_duration": {"download": dl_ms * 1_000_000, "upload": ul_ms * 1_000_000}}],
                              "user_info": {"IP": "<redacted>", "Lat": "<redacted>", "Lon": "<redacted>",
                                            "Isp": "Virgin Media"},
                              "demo": "generated, not a real measurement"}, separators=(",", ":"))
        return fields, raw, busy

    def _diagnostic(self, emit, r, m: dict, pre: dict, busy: float, dur: int, ip_changed: int) -> None:
        w = self.w
        t0 = m["t_ms"]
        month = int(w.clock.parts(t0)[0][5:7])
        summer = {6: 4, 7: 6, 8: 5, 5: 2, 9: 2}.get(month, 0)
        ok = m["status"] == "ok"
        dlb = m.get("download_bytes") or (int(m["download_mbps"] * 1e6 / 8 * (m.get("download_ms") or 10_000) / 1000)
                                          if ok else 0)
        ulb = m.get("upload_bytes") or (int(m["upload_mbps"] * 1e6 / 8 * (m.get("upload_ms") or 9_000) / 1000)
                                        if ok and m.get("upload_mbps") else 0)
        window = dur + int(r.uniform(150, 400))
        bg_rx = busy if ok else None
        bg_tx = abs(r.gauss(0.2, 0.15)) if ok else None
        rec = {"uuid": rng_uuid(r), "measurement_uuid": m["uuid"], "agent_id": AGENT_ID, "agent_seq": None, "t_ms": t0,
               "cpu_load1": round(abs(r.gauss(0.32, 0.15)) + (0.4 if ok else 0), 2),
               "cpu_load5": round(abs(r.gauss(0.28, 0.1)), 2), "cpu_temp_c": round(r.gauss(49 + summer, 2.2) + (4 if ok else 0), 1),
               "throttled": "0x0", "mem_available_pct": round(r.gauss(71, 3), 1), "swap_used_pct": round(abs(r.gauss(0.4, 0.4)), 1),
               "iface": m["iface"], "link_speed_mbps": m["link_speed_mbps"], "link_duplex": "full", "link_state": "up",
               "rx_errors_delta": 0, "tx_errors_delta": 0, "rx_dropped_delta": int(r.random() < 0.05),
               "tx_dropped_delta": 0,
               "rx_bytes_delta": int(dlb * 1.05 + ulb * 0.03 + (busy * 1e6 / 8 * window / 1000)) if ok else int(r.uniform(2e3, 9e4)),
               "tx_bytes_delta": int(ulb * 1.05 + dlb * 0.03) if ok else int(r.uniform(1e3, 3e4)),
               "window_ms": window, "background_rx_mbps": round(bg_rx, 3) if bg_rx is not None else None,
               "background_tx_mbps": round(bg_tx, 3) if bg_tx is not None else None,
               "pretest_rx_mbps": round(busy * r.uniform(0.6, 1.0), 3) if busy > 20 else round(abs(r.gauss(0.3, 0.2)), 3),
               "pretest_tx_mbps": round(abs(r.gauss(0.15, 0.1)), 3),
               "gateway_rtt_ms": pre.get("gw_rtt_ms"), "gateway_loss_pct": pre.get("gw_loss_pct"),
               "external_rtt_ms": pre.get("ext_rtt_ms"), "external_loss_pct": pre.get("ext_loss_pct"),
               "dns_ok": pre.get("dns_ok"), "public_ip": self.public_ip(t0) if ok else None, "public_ip_changed": ip_changed,
               "pi_uptime_s": round((t0 - self.boot["ms"]) / 1000, 2) if self.boot else None,
               "boot_id": self.boot["id"] if self.boot else None, "agent_version": AGENT_VERSION,
               "extra": json.dumps({"deferred": 0, "precheck_state": pre.get("state"), "server_mode": "auto"},
                                   separators=(",", ":"))}
        rec["agent_seq"] = self._next_seq()
        emit("diagnostic", rec)


def world_for(state: dict) -> World:
    """The world a saved simulator state belongs to."""
    return World(date.fromisoformat(state["anchor"]), state["seed"], state.get("days", 1096))
