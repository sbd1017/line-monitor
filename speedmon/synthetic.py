"""Synthetic history for development and scale tests only. Every row is source='synthetic' and the database is
marked data_origin=synthetic, which production refuses to start on. Never run against real data.

The pattern is deliberately realistic enough to exercise the analytics: weekday evening congestion, a
few outages, failed tests, confirmation/burst tests, slow drift, and DST transitions (when the range spans them).
"""
from __future__ import annotations

import json
import math
import random
import uuid as uuidlib

from . import canonical, ingest, settings_store, timeutil

AGENT = "synthetic-agent"


def _bulk_insert(conn, stream: str, recs: list[dict], state: dict) -> None:
    table = ingest.STREAM_TABLE[stream]
    fields = list(canonical.FIELDS[stream])
    cols = fields + ["record_hash", "received_at"] + (["raw"] if stream == "measurement" else [])
    now = timeutil.now_iso()
    rows, ledger = [], []
    for r in recs:
        raw = r.pop("_raw", None)
        r = canonical.normalise(stream, r)
        h = canonical.record_hash(stream, r)
        rows.append([r[k] for k in fields] + [h, now] + ([raw] if stream == "measurement" else []))
        state["idx"] += 1
        chain = canonical.chain_next(state["prev"], stream, r["uuid"], h)
        ledger.append((state["idx"], stream, r["uuid"], h, state["prev"], chain, canonical.HASH_VERSION, now))
        state["prev"] = chain
    conn.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
    conn.executemany("INSERT INTO ledger (chain_index, stream, uuid, record_hash, prev_hash, chain_hash, hash_version, "
                     "recorded_at) VALUES (?,?,?,?,?,?,?,?)", ledger)


def _speed(t_ms: int, clock, rng: random.Random, day_factor: float) -> float:
    d, hour, dow, minute = clock.parts(t_ms)
    base = 935.0 * day_factor
    if dow < 5 and 19 * 60 + 10 <= minute <= 22 * 60 + 35:          # weekday evening congestion
        base *= 0.62 + 0.1 * math.sin(minute / 37)
    elif dow >= 5 and 20 * 60 <= minute <= 22 * 60:
        base *= 0.82
    v = rng.gauss(base, base * 0.05)
    if rng.random() < 0.015:
        v *= rng.uniform(0.1, 0.45)                                   # occasional deep dip
    return max(5.0, min(v, 941.0))


def seed(conn, days: int = 400, interval_min: int = 30, probe_interval_s: int = 60, probes: bool = True,
         end_ms: int | None = None, seed_value: int = 7, tz: str = "Europe/London", max_measurements: int | None = None) -> dict:
    rng = random.Random(seed_value)
    clock = timeutil.LocalClock(tz)
    end = end_ms or timeutil.now_ms()
    start = end - days * timeutil.DAY_MS
    head = conn.execute("SELECT chain_index, chain_hash FROM ledger ORDER BY chain_index DESC LIMIT 1").fetchone()
    state = {"idx": head[0] if head else -1, "prev": head[1] if head else canonical.GENESIS, "seq": 10_000_000}
    settings_store.set_internal(conn, "data_origin", "synthetic")
    outages = []
    t = start
    while t < end:
        t += int(rng.expovariate(1 / (9 * timeutil.DAY_MS)))
        outages.append((t, t + rng.randint(2, 50) * 60_000))
    in_outage = lambda x: any(a <= x < b for a, b in outages)
    meas, diags, n = [], [], 0
    slot = interval_min * 60_000
    s0 = start // slot * slot
    day_factor = {}
    burst_until = 0
    t = s0
    while t < end and (max_measurements is None or n < max_measurements):
        when = t + rng.randint(int(slot * 0.02), int(slot * 0.98))
        t += slot
        day = when // timeutil.DAY_MS
        if day not in day_factor:
            day_factor[day] = min(1.0, rng.gauss(0.98, 0.04))
        kinds = [("scheduled", when)]
        if when < burst_until:
            kinds.append(("burst", when + slot // 3))
        for kind, at in kinds:
            if at >= end:
                continue
            n += 1
            state["seq"] += 1
            uid = str(uuidlib.UUID(int=rng.getrandbits(128), version=4))
            fail = in_outage(at) or rng.random() < 0.004
            dl = None if fail else _speed(at, clock, rng, day_factor[day])
            ul = None if fail else max(1.0, min(rng.gauss(103.5, 1.5), 110.0))
            ping = None if fail else max(4.0, rng.gauss(10.5 if dl > 700 else 16, 1.5))
            raw = json.dumps({"synthetic": True})
            rec = {"uuid": uid, "agent_id": AGENT, "agent_seq": state["seq"], "source": "synthetic", "kind": kind,
                   "engine": "ookla", "engine_version": "1.2.0.84", "t_ms": at, "started_at": timeutil.iso(at),
                   "ended_at": timeutil.iso(at + 22_000), "duration_ms": 22_000, "tz_name": tz,
                   "utc_offset_min": timeutil.offset_minutes(at, tz), "status": "failed" if fail else "ok",
                   "failure_code": ("wan_down" if in_outage(at) else "server_error") if fail else None,
                   "failure_detail": "synthetic failure" if fail else None, "download_mbps": dl, "upload_mbps": ul,
                   "download_bytes": int(dl * 1.25e6 * 10) if dl else None, "upload_bytes": int(ul * 1.25e6 * 8) if ul else None,
                   "download_ms": 10000 if dl else None, "upload_ms": 8000 if ul else None, "ping_ms": ping,
                   "jitter_ms": None if fail else abs(rng.gauss(1.2, 0.6)), "packet_loss_pct": None if fail else
                   (0.0 if rng.random() > 0.05 else rng.uniform(0.1, 2)), "server_id": rng.choice(["76431", "40628", "48475"]),
                   "server_sponsor": "Virgin Media O2", "server_location": "London", "server_name": "Virgin Media O2 – London",
                   "server_country": "United Kingdom", "isp": "Virgin Media", "iface": "eth0", "link_speed_mbps": 1000,
                   "link_duplex": "full", "link_state": "up", "raw_sha256": canonical.sha256_text(raw), "_raw": raw}
            meas.append(rec)
            diags.append({"uuid": str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, uid)), "measurement_uuid": uid, "agent_id": AGENT,
                          "t_ms": at, "cpu_load1": abs(rng.gauss(0.3, 0.2)), "cpu_temp_c": rng.gauss(47, 3),
                          "throttled": "0x0", "mem_available_pct": rng.gauss(60, 5), "iface": "eth0", "link_speed_mbps": 1000,
                          "link_duplex": "full", "link_state": "up", "rx_errors_delta": 0, "tx_errors_delta": 0,
                          "background_rx_mbps": rng.uniform(30, 200) if rng.random() < 0.02 else abs(rng.gauss(0.5, 0.5)),   # ~2% Pi busy
                          "background_tx_mbps": abs(rng.gauss(0.3, 0.3)),
                          "gateway_rtt_ms": abs(rng.gauss(0.5, 0.05)), "dns_ok": 0 if fail else 1, "agent_version": "synthetic"})
            if kind == "scheduled" and dl is not None and dl < 500:
                burst_until = at + 60 * 60_000
            if len(meas) >= 5000:
                _bulk_insert(conn, "measurement", meas, state)
                _bulk_insert(conn, "diagnostic", diags, state)
                meas, diags = [], []
    if meas:
        _bulk_insert(conn, "measurement", meas, state)
        _bulk_insert(conn, "diagnostic", diags, state)
    n_p = 0
    if probes:
        batch = []
        step = probe_interval_s * 1000
        t = start // step * step
        offline = []
        tt = start
        while tt < end:
            tt += int(rng.expovariate(1 / (40 * timeutil.DAY_MS)))
            offline.append((tt, tt + rng.randint(10, 300) * 60_000))
        while t < end:
            t += step
            if any(a <= t < b for a, b in offline):
                continue
            down = in_outage(t)
            ext = None if down else max(5.0, rng.gauss(11, 1.5) + (6 if clock.parts(t)[1] in (20, 21) else 0))
            state["seq"] += 1
            batch.append({"uuid": str(uuidlib.UUID(int=rng.getrandbits(128), version=4)), "agent_id": AGENT,
                          "agent_seq": state["seq"], "t_ms": t, "at": timeutil.iso(t), "interval_s": probe_interval_s,
                          "state": "wan_down" if down else ("degraded" if ext and ext > 40 else "ok"),
                          "gw_rtt_ms": abs(rng.gauss(0.5, 0.05)), "gw_loss_pct": 0.0, "ext_rtt_ms": ext,
                          "ext_loss_pct": 100.0 if down else 0.0, "dns_ms": None if down else abs(rng.gauss(8, 3)),
                          "dns_ok": 0 if down else 1, "http_ok": 0 if down else 1, "http_ms": None if down else rng.gauss(40, 5),
                          "link_up": 1, "link_speed_mbps": 1000})
            n_p += 1
            if len(batch) >= 20000:
                _bulk_insert(conn, "probe", batch, state)
                batch = []
        if batch:
            _bulk_insert(conn, "probe", batch, state)
    derived = ingest.rebuild_derived(conn)
    return {"measurements": n, "probes": n_p, "outages": len(outages), "derived": derived}
