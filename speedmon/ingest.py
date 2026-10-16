"""Accepting evidence records: validate, verify the agent's hash, store, chain, update derived tables.

Idempotent by UUID: the same record sent twice is acknowledged as a duplicate (identical hash) and stored once.
A record that fails validation, or reuses a UUID with different content, is kept in ``quarantine`` and reported
back; it is never silently dropped.
"""
from __future__ import annotations

import json
import re
import statistics

from . import canonical, timeutil

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
STREAM_TABLE = {"measurement": "measurements", "diagnostic": "diagnostics", "probe": "probes", "event": "events"}
MAX_TEXT = 4000
MAX_RAW = 200_000

INT_FIELDS = canonical.INT_FIELDS
ENUMS = {
    "kind": {"scheduled", "confirmation", "burst", "manual", "fallback", "retry"},
    "status": {"ok", "failed"},
    "state": {"ok", "degraded", "wan_down", "dns_fail", "lan_fault", "link_down"},
}
REQUIRED = {
    "measurement": ("uuid", "agent_id", "source", "kind", "engine", "t_ms", "started_at", "tz_name", "utc_offset_min",
                    "status"),
    "diagnostic": ("uuid", "measurement_uuid", "agent_id", "t_ms"),
    "probe": ("uuid", "agent_id", "t_ms", "at", "interval_s", "state"),
    "event": ("uuid", "agent_id", "t_ms", "at", "type"),
}


class Rejected(ValueError):
    pass


def _check_types(stream: str, rec: dict, allow_synthetic: bool) -> None:
    for key in REQUIRED[stream]:
        if rec.get(key) is None:
            raise Rejected(f"{key} is required")
    if not UUID_RE.match(str(rec["uuid"])):
        raise Rejected("uuid must be a lowercase UUID")
    for key, value in rec.items():
        if value is None:
            continue
        if key in INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise Rejected(f"{key} must be an integer")
        elif key in canonical.REAL_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise Rejected(f"{key} must be a number")
            if value != value or abs(value) > 1e12:
                raise Rejected(f"{key} out of range")
        elif not isinstance(value, str) or len(value) > MAX_TEXT:
            raise Rejected(f"{key} must be text up to {MAX_TEXT} characters")
    for key, allowed in ENUMS.items():
        if key in rec and rec[key] is not None and key in canonical.FIELDS[stream] and rec[key] not in allowed:
            raise Rejected(f"{key} must be one of {', '.join(sorted(allowed))}")
    now = timeutil.now_ms()
    if not 1_500_000_000_000 < rec["t_ms"] < now + 10 * 60_000:
        raise Rejected("t_ms is not a plausible time (agent clock wrong?)")
    if stream == "measurement":
        if rec["source"] not in ("agent", "synthetic"):
            raise Rejected("source must be 'agent'")
        if rec["source"] == "synthetic" and not allow_synthetic:
            raise Rejected("synthetic records are refused in production")
        if abs(timeutil.parse_iso_ms(rec["started_at"]) - rec["t_ms"]) > 1000:
            raise Rejected("started_at and t_ms disagree")
        if rec["status"] == "ok" and rec.get("download_mbps") is None:
            raise Rejected("a successful test needs download_mbps")
        if rec["status"] == "failed" and not rec.get("failure_code"):
            raise Rejected("a failed test needs failure_code")
        for key in ("download_mbps", "upload_mbps"):
            if rec.get(key) is not None and not 0 <= rec[key] <= 100_000:
                raise Rejected(f"{key} out of range")
    if stream in ("probe", "event") and abs(timeutil.parse_iso_ms(rec["at"]) - rec["t_ms"]) > 1000:
        raise Rejected("at and t_ms disagree")


def _ledger_head(conn) -> tuple[int, str]:
    row = conn.execute("SELECT chain_index, chain_hash FROM ledger ORDER BY chain_index DESC LIMIT 1").fetchone()
    return (row["chain_index"], row["chain_hash"]) if row else (-1, canonical.GENESIS)


def append_ledger(conn, stream: str, uuid: str, rhash: str) -> str:
    idx, prev = _ledger_head(conn)
    chain = canonical.chain_next(prev, stream, uuid, rhash)
    conn.execute("INSERT INTO ledger (chain_index, stream, uuid, record_hash, prev_hash, chain_hash, hash_version, "
                 "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (idx + 1, stream, uuid, rhash, prev, chain, canonical.HASH_VERSION, timeutil.now_iso()))
    return chain


def insert_record(conn, stream: str, rec: dict, rhash: str, raw: str | None = None) -> None:
    table = STREAM_TABLE[stream]
    cols = list(canonical.FIELDS[stream]) + ["record_hash", "received_at"]
    vals = [rec.get(k) for k in canonical.FIELDS[stream]] + [rhash, timeutil.now_iso()]
    if stream == "measurement":
        cols.append("raw")
        vals.append(raw)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    append_ledger(conn, stream, rec["uuid"], rhash)
    if stream == "probe":
        rollup_probe(conn, rec)
    elif stream == "measurement" and rec["status"] == "ok":
        update_anomaly(conn, rec)


def quarantine(conn, agent_id, stream, uuid, payload, error: str) -> None:
    conn.execute("INSERT INTO quarantine (received_at, agent_id, stream, uuid, payload, error) VALUES (?, ?, ?, ?, ?, ?)",
                 (timeutil.now_iso(), str(agent_id)[:80] if agent_id else None, str(stream)[:20] if stream else None,
                  str(uuid)[:64] if uuid else None, json.dumps(payload)[:MAX_RAW], error[:500]))


def ingest_batch(conn, agent_id: str, items: list, allow_synthetic: bool) -> dict:
    """items: [{"stream", "record", "record_hash", "raw"?}]. Runs inside the caller's transaction."""
    accepted, duplicates, rejected = [], [], []
    row = conn.execute("SELECT value FROM settings WHERE key = 'epoch'").fetchone()
    epoch_ms = json.loads(row["value"]).get("epoch_ms") if row else None
    for item in items:
        uuid = None
        try:
            if not isinstance(item, dict):
                raise Rejected("item must be an object")
            stream = item.get("stream")
            rec = item.get("record")
            if stream not in STREAM_TABLE or not isinstance(rec, dict):
                raise Rejected("unknown stream or missing record")
            uuid = rec.get("uuid")
            unknown = set(rec) - set(canonical.FIELDS[stream])
            if unknown:
                raise Rejected(f"unknown fields: {', '.join(sorted(unknown))[:200]}")
            if rec.get("agent_id") != agent_id:
                raise Rejected("agent_id does not match the signing agent")
            _check_types(stream, rec, allow_synthetic)
            if epoch_ms and rec["t_ms"] < epoch_ms:
                raise Rejected("recorded before the reset; it belongs to the archived period (kept here, not lost)")
            rec = canonical.normalise(stream, rec)
            rhash = canonical.record_hash(stream, rec)
            if item.get("record_hash") != rhash:
                raise Rejected("record_hash does not match the record (changed in transit or in the outbox?)")
            raw = item.get("raw")
            if stream == "measurement":
                if raw is not None and (not isinstance(raw, str) or len(raw) > MAX_RAW):
                    raise Rejected("raw must be text")
                if canonical.sha256_text(raw) != rec.get("raw_sha256"):
                    raise Rejected("raw_sha256 does not match raw")
            existing = conn.execute(f"SELECT record_hash FROM {STREAM_TABLE[stream]} WHERE uuid = ?", (uuid,)).fetchone()
            if existing:
                if existing["record_hash"] == rhash:
                    duplicates.append(uuid)
                    continue
                raise Rejected("uuid already stored with different content")
            if rec.get("agent_seq") is not None and conn.execute(
                    f"SELECT 1 FROM {STREAM_TABLE[stream]} WHERE agent_id = ? AND agent_seq = ?",
                    (agent_id, rec["agent_seq"])).fetchone():
                raise Rejected("agent_seq already used by a different record")
            insert_record(conn, stream, rec, rhash, raw)
            accepted.append(uuid)
        except Rejected as exc:
            quarantine(conn, agent_id, item.get("stream") if isinstance(item, dict) else None, uuid, item, str(exc))
            rejected.append({"uuid": uuid, "error": str(exc)})
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


# ---------------------------------------------------------------- derived

def rollup_probe(conn, rec: dict) -> None:
    hour = rec["t_ms"] // timeutil.HOUR_MS * timeutil.HOUR_MS
    st = rec["state"]
    ext = rec.get("ext_rtt_ms")
    gw = rec.get("gw_rtt_ms")
    loss = rec.get("ext_loss_pct")
    conn.execute(
        "INSERT INTO rollup_probe_hourly (hour_ms, n, n_ok, n_degraded, n_wan_down, n_dns_fail, n_lan_fault, n_link_down,"
        " ext_n, ext_sum, ext_min, ext_max, gw_n, gw_sum, loss_sum, loss_n) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(hour_ms) DO UPDATE SET n = n + 1, n_ok = n_ok + excluded.n_ok, "
        "n_degraded = n_degraded + excluded.n_degraded, n_wan_down = n_wan_down + excluded.n_wan_down, "
        "n_dns_fail = n_dns_fail + excluded.n_dns_fail, n_lan_fault = n_lan_fault + excluded.n_lan_fault, "
        "n_link_down = n_link_down + excluded.n_link_down, ext_n = ext_n + excluded.ext_n, "
        "ext_sum = ext_sum + excluded.ext_sum, "
        "ext_min = CASE WHEN excluded.ext_min IS NULL THEN ext_min WHEN ext_min IS NULL THEN excluded.ext_min "
        "  ELSE min(ext_min, excluded.ext_min) END, "
        "ext_max = CASE WHEN excluded.ext_max IS NULL THEN ext_max WHEN ext_max IS NULL THEN excluded.ext_max "
        "  ELSE max(ext_max, excluded.ext_max) END, "
        "gw_n = gw_n + excluded.gw_n, gw_sum = gw_sum + excluded.gw_sum, loss_sum = loss_sum + excluded.loss_sum, "
        "loss_n = loss_n + excluded.loss_n",
        (hour, int(st == "ok"), int(st == "degraded"), int(st == "wan_down"), int(st == "dns_fail"),
         int(st == "lan_fault"), int(st == "link_down"), int(ext is not None), ext or 0.0, ext, ext,
         int(gw is not None), gw or 0.0, loss or 0.0, int(loss is not None)))


ANOMALY_Z = 3.5
ANOMALY_MIN_REF = 12
ANOMALY_WINDOW_DAYS = 28


def update_anomaly(conn, rec: dict, tz: str | None = None) -> None:
    """Robust z-score of this download against earlier tests at the same local hour (±1 h) and day type
    (weekday/weekend) in the previous 28 days. Causal: only data before the test is used."""
    if tz is None:
        row = conn.execute("SELECT value FROM settings WHERE key = 'timezone'").fetchone()
        tz = json.loads(row["value"]) if row else "Europe/London"
    t = rec["t_ms"]
    _, hour, dow = timeutil.local_parts(t, tz)
    weekend = dow >= 5
    rows = conn.execute("SELECT t_ms, download_mbps FROM measurements WHERE status = 'ok' AND source IN ('agent','synthetic') "
                        "AND t_ms >= ? AND t_ms < ?", (t - ANOMALY_WINDOW_DAYS * timeutil.DAY_MS, t)).fetchall()
    ref = []
    for r in rows:
        _, h, d = timeutil.local_parts(r["t_ms"], tz)
        if (d >= 5) == weekend and min((h - hour) % 24, (hour - h) % 24) <= 1:
            ref.append(r["download_mbps"])
    z, med, anomaly = robust_z(ref, rec["download_mbps"])
    conn.execute("INSERT INTO derived_measurement (uuid, anomaly_z, ref_median, ref_n, anomaly) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(uuid) DO UPDATE SET anomaly_z = excluded.anomaly_z, ref_median = excluded.ref_median, "
                 "ref_n = excluded.ref_n, anomaly = excluded.anomaly", (rec["uuid"], z, med, len(ref), anomaly))


def robust_z(ref: list, x: float):
    if len(ref) < ANOMALY_MIN_REF:
        return None, None, 0
    med = statistics.median(ref)
    mad = statistics.median([abs(v - med) for v in ref])
    scale = 1.4826 * mad if mad > 0 else max(statistics.pstdev(ref), med * 0.02, 1.0)
    z = (x - med) / scale
    return z, med, int(abs(z) >= ANOMALY_Z)


def rebuild_derived(conn) -> dict:
    conn.execute("DELETE FROM rollup_probe_hourly")
    conn.execute("DELETE FROM derived_measurement")
    n_p = 0
    for r in conn.execute("SELECT * FROM probes ORDER BY t_ms"):
        rollup_probe(conn, dict(r))
        n_p += 1
    row = conn.execute("SELECT value FROM settings WHERE key = 'timezone'").fetchone()
    tz = json.loads(row["value"]) if row else "Europe/London"
    from collections import deque
    rows = conn.execute("SELECT uuid, t_ms, download_mbps FROM measurements WHERE status = 'ok' "
                        "AND source IN ('agent','synthetic') ORDER BY t_ms").fetchall()
    buckets: dict = {}                     # (weekend, hour) -> deque of (t_ms, download) in time order
    window = ANOMALY_WINDOW_DAYS * timeutil.DAY_MS
    out = []
    for r in rows:
        t, x = r["t_ms"], r["download_mbps"]
        _, hour, dow = timeutil.local_parts(t, tz)
        weekend = dow >= 5
        ref = []
        for h in ((hour - 1) % 24, hour, (hour + 1) % 24):
            dq = buckets.get((weekend, h))
            if dq:
                while dq and dq[0][0] < t - window:
                    dq.popleft()
                ref.extend(v for _, v in dq)
        z, med, anomaly = robust_z(ref, x)
        out.append((r["uuid"], z, med, len(ref), anomaly))
        buckets.setdefault((weekend, hour), deque()).append((t, x))
    conn.executemany("INSERT INTO derived_measurement (uuid, anomaly_z, ref_median, ref_n, anomaly) VALUES (?,?,?,?,?)", out)
    n_m = len(out)
    return {"probes": n_p, "measurements": n_m}
