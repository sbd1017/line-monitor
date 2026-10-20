"""Chart data with server-side level of detail. The browser asks for a time range and its pixel width. Narrow
ranges get every raw test; wider ones get fixed-size buckets (count, mean, min, max, failures). The raw records
are never altered; buckets are computed per request (tests) or from the hourly rollup (probes, long ranges).
"""
from __future__ import annotations

import json

from . import settings_store, timeutil

LADDER_MS = [m * 60_000 for m in (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080, 43200)]
MAX_RAW_POINTS = 4000


def _bucket_for(span_ms: int, max_buckets: int, floor_ms: int = 60_000) -> int:
    for b in LADDER_MS:
        if b >= floor_ms and span_ms / b <= max_buckets:
            return b
    return LADDER_MS[-1]


def _sources(legacy: bool) -> tuple:
    return ("agent", "synthetic", "legacy_csv", "legacy_xlsx") if legacy else ("agent", "synthetic")


def tests(conn, from_ms: int, to_ms: int, px: int, legacy: bool, s: dict) -> dict:
    px = max(200, min(int(px or 1200), 4000))
    src = _sources(legacy)
    ph = ",".join("?" * len(src))
    excluded = {r[0] for r in conn.execute(
        "SELECT measurement_uuid FROM measurement_flags WHERE flag = 'exclude' AND revoked_at IS NULL")}
    n = conn.execute(f"SELECT count(*) FROM measurements WHERE t_ms >= ? AND t_ms < ? AND source IN ({ph})",
                     (from_ms, to_ms, *src)).fetchone()[0]
    if n <= min(MAX_RAW_POINTS, px * 2):
        pkc = settings_store.PackageClock(s.get("packages"), s["timezone"])
        pts = []
        for r in conn.execute(
                f"SELECT m.t_ms, m.download_mbps, m.upload_mbps, m.ping_ms, m.status, m.uuid, m.kind, m.source, "
                f"m.failure_code, d.anomaly FROM measurements m LEFT JOIN derived_measurement d ON d.uuid = m.uuid "
                f"WHERE m.t_ms >= ? AND m.t_ms < ? AND m.source IN ({ph}) ORDER BY m.t_ms", (from_ms, to_ms, *src)):
            band = None
            if r[4] == "ok":
                pk = pkc.at(r[0])
                band = settings_store.download_band(r[1], pk)
            pts.append([r[0], r[1], r[2], r[3], 1 if r[4] == "ok" else 0, r[5], r[6],
                        1 if r[7].startswith("legacy") else 0, r[8], int(r[9] or 0), 1 if r[5] in excluded else 0, band])
        return {"mode": "raw", "n": n, "bucket_ms": None,
                "fields": ["t", "dl", "ul", "ping", "ok", "uuid", "kind", "legacy", "failure", "anomaly", "excluded", "band"],
                "points": pts, "packages": package_periods(s, from_ms, to_ms)}
    span = max(to_ms - from_ms, 1)
    b = _bucket_for(span, px // 2, 5 * 60_000)
    deg, deg_args = settings_store.sql_case(s, "degraded_down", "t_ms")
    tgt, tgt_args = settings_store.sql_case(s, "target_down", "t_ms")
    rows = conn.execute(
        f"SELECT (t_ms / ?) * ? AS k, count(*), sum(status = 'ok'), avg(download_mbps), min(download_mbps), "
        f"max(download_mbps), avg(upload_mbps), min(upload_mbps), max(upload_mbps), avg(ping_ms), "
        f"sum(status = 'ok' AND download_mbps < {deg}), sum(status = 'ok' AND download_mbps >= {tgt}), sum(source LIKE 'legacy%') "
        f"FROM measurements WHERE t_ms >= ? AND t_ms < ? AND source IN ({ph}) GROUP BY k ORDER BY k",
        (b, b, *deg_args, *tgt_args, from_ms, to_ms, *src)).fetchall()
    return {"mode": "bucket", "n": n, "bucket_ms": b,
            "fields": ["t", "n", "n_ok", "dl", "dl_min", "dl_max", "ul", "ul_min", "ul_max", "ping", "n_degraded", "n_target", "n_legacy"],
            "points": [list(r) for r in rows], "packages": package_periods(s, from_ms, to_ms)}


def package_periods(s: dict, a: int, b: int) -> list:
    """Packages in force across [a, b), for drawing each period's thresholds on the charts."""
    clk = settings_store.PackageClock(s.get("packages"), s["timezone"])
    return [{"start_ms": p0, "end_ms": p1, "name": pk["name"], "target_down": pk["target_down"],
             "degraded_down": pk["degraded_down"], "severe_down": pk["severe_down"], "target_up": pk["target_up"],
             "degraded_up": pk["degraded_up"], "advertised_down": pk["advertised_down"], "advertised_up": pk["advertised_up"]}
            for p0, p1, pk in clk.periods(a, b)]


def probes(conn, from_ms: int, to_ms: int, px: int) -> dict:
    px = max(200, min(int(px or 1200), 4000))
    span = max(to_ms - from_ms, 1)
    n = conn.execute("SELECT count(*) FROM probes WHERE t_ms >= ? AND t_ms < ?", (from_ms, to_ms)).fetchone()[0]
    if n <= min(MAX_RAW_POINTS, px * 2):
        rows = conn.execute("SELECT t_ms, ext_rtt_ms, gw_rtt_ms, ext_loss_pct, state, dns_ms FROM probes "
                            "WHERE t_ms >= ? AND t_ms < ? ORDER BY t_ms", (from_ms, to_ms)).fetchall()
        return {"mode": "raw", "n": n, "fields": ["t", "ext", "gw", "loss", "state", "dns"], "points": [list(r) for r in rows]}
    b = _bucket_for(span, px // 2)
    if b >= timeutil.HOUR_MS:
        rows = conn.execute(
            "SELECT (hour_ms / ?) * ? AS k, sum(n), sum(ext_sum) / nullif(sum(ext_n), 0), min(ext_min), max(ext_max), "
            "sum(gw_sum) / nullif(sum(gw_n), 0), sum(loss_sum) / nullif(sum(loss_n), 0), sum(n_wan_down + n_dns_fail), "
            "sum(n_lan_fault + n_link_down) FROM rollup_probe_hourly WHERE hour_ms >= ? AND hour_ms < ? GROUP BY k ORDER BY k",
            (b, b, from_ms // timeutil.HOUR_MS * timeutil.HOUR_MS, to_ms)).fetchall()
    else:
        rows = conn.execute(
            "SELECT (t_ms / ?) * ? AS k, count(*), avg(ext_rtt_ms), min(ext_rtt_ms), max(ext_rtt_ms), avg(gw_rtt_ms), "
            "avg(ext_loss_pct), sum(state IN ('wan_down','dns_fail')), sum(state IN ('lan_fault','link_down')) "
            "FROM probes WHERE t_ms >= ? AND t_ms < ? GROUP BY k ORDER BY k", (b, b, from_ms, to_ms)).fetchall()
    return {"mode": "bucket", "n": n, "bucket_ms": b,
            "fields": ["t", "n", "ext", "ext_min", "ext_max", "gw", "loss", "n_outage", "n_local"],
            "points": [list(r) for r in rows]}


# ---------------------------------------------------------------- reliability timeline

CLASS = {"ok": "ok", "degraded": "latency", "wan_down": "outage", "dns_fail": "outage", "lan_fault": "local",
         "link_down": "local"}


def _merge(segs: list) -> list:
    out = []
    for a, b, c, *rest in segs:
        if out and out[-1][2] == c and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b, c, *rest])
    return out


def timeline(conn, from_ms: int, to_ms: int, agent_last_seen_ms: int | None = None) -> dict:
    """Segments [start_ms, end_ms, class]: ok | latency | outage (Internet down, router fine) | local (Pi/LAN
    problem) | offline (no data from the monitor) | unknown (before monitoring began, or not yet synced)."""
    now = timeutil.now_ms()
    to_ms = min(to_ms, now)
    first = conn.execute("SELECT min(t_ms) FROM probes").fetchone()[0]
    segs: list = []
    if first is None or first >= to_ms:
        return {"segments": [[from_ms, to_ms, "unknown"]], "resolution": "none", "reasons": []}
    if from_ms < first:
        segs.append([from_ms, first, "unknown"])
    a = max(from_ms, first)
    if to_ms - a <= 14 * timeutil.DAY_MS:
        res = "probe"
        prev_end = a
        prev = conn.execute("SELECT t_ms, interval_s FROM probes WHERE t_ms < ? ORDER BY t_ms DESC LIMIT 1", (a,)).fetchone()
        if prev:
            prev_end = max(a, prev[0] + prev[1] * 1000)
        for t, interval, state in conn.execute("SELECT t_ms, interval_s, state FROM probes WHERE t_ms >= ? AND t_ms < ? "
                                               "ORDER BY t_ms", (a, to_ms)):
            iv = interval * 1000
            if t - prev_end > iv * 1.5:
                segs.append([prev_end, t, "offline"])
            segs.append([t, min(t + iv, to_ms), CLASS.get(state, "ok")])
            prev_end = t + iv
        tail_from = prev_end
        last_iv = 60_000
    else:
        res = "hour"
        rows = {r[0]: r for r in conn.execute(
            "SELECT hour_ms, n, n_wan_down + n_dns_fail, n_lan_fault + n_link_down, n_degraded FROM rollup_probe_hourly "
            "WHERE hour_ms >= ? AND hour_ms < ?", (a // timeutil.HOUR_MS * timeutil.HOUR_MS, to_ms))}
        iv_row = conn.execute("SELECT interval_s FROM probes ORDER BY t_ms DESC LIMIT 1").fetchone()
        expected = 3600 / (iv_row[0] if iv_row else 60)
        h = a // timeutil.HOUR_MS * timeutil.HOUR_MS
        while h < to_ms:
            r = rows.get(h)
            end = min(h + timeutil.HOUR_MS, to_ms)
            if not r or r[1] == 0:
                cls = "offline"
            elif r[2] >= 2:
                cls = "outage"
            elif r[3] >= 2:
                cls = "local"
            elif r[1] < expected * 0.5:
                cls = "partial"
            elif r[4] > r[1] * 0.25:
                cls = "latency"
            else:
                cls = "ok"
            segs.append([max(h, a), end, cls])
            h += timeutil.HOUR_MS
        last = conn.execute("SELECT max(t_ms) FROM probes").fetchone()[0]
        tail_from = (last + 60_000) if last and last < to_ms else to_ms
        last_iv = 60_000
    if to_ms - tail_from > last_iv * 1.5:
        recent = agent_last_seen_ms and now - agent_last_seen_ms < 5 * 60_000
        segs.append([tail_from, to_ms, "unknown" if recent else "offline"])
    reasons = []
    for t, detail in conn.execute("SELECT t_ms, detail FROM events WHERE type = 'agent_start' AND t_ms >= ? AND t_ms < ?",
                                  (from_ms, to_ms)):
        try:
            d = json.loads(detail or "{}")
        except ValueError:
            d = {}
        reasons.append({"t": t, "rebooted": bool(d.get("rebooted")), "clean": d.get("previous_clean_stop"),
                        "boot_ms": d.get("boot_ms")})
    return {"segments": _merge(segs), "resolution": res, "reasons": reasons}
