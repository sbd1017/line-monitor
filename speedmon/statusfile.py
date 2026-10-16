"""A small public-safe status file for other apps on this Pi (a home dashboard tile, say).

Written by the backend every STATUS_EVERY_S seconds to $SPEEDMON_STATUS_FILE (production:
/run/speedmon/status.json, world-readable). Fields are whitelisted here, never copied wholesale: no IP
addresses, server hosts, MAC addresses, agent ids, paths or keys. Readers must treat a file whose
`generated_at_ms` is old as "monitor not writing" rather than trusting its contents.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SCHEMA = 1
STATUS_EVERY_S = 30

HEALTH_LABELS = {
    "ok": "Internet reachable, latency normal",
    "latency": "Reachable but latency is high",
    "degraded": "Latest test below the degraded threshold",
    "outage": "Router answers but the Internet does not",
    "local": "The Pi or router side has a problem",
    "monitor_offline": "No word from the monitor",
    "stale": "No probe in the last 5 minutes",
    "unknown": "Waiting for the first probe",
}


def short_name(name: str | None) -> str | None:
    if not name:
        return name
    out = re.sub(r"\s+Fibre Broadband$", "", re.sub(r"^Virgin Media\s+", "", name, flags=re.I), flags=re.I)
    return out or name


def _num(v, nd=1):
    return None if v is None else round(float(v), nd)


def _test(d: dict | None) -> dict | None:
    if not d:
        return None
    return {"t_ms": d.get("t_ms"), "status": d.get("status"), "failure_code": d.get("failure_code"),
            "kind": d.get("kind"), "download_mbps": _num(d.get("download_mbps")),
            "upload_mbps": _num(d.get("upload_mbps")), "ping_ms": _num(d.get("ping_ms")),
            "band": d.get("band"), "pct_of_advertised": _num(d.get("pct_of_advertised"))}


def build(live: dict, guarantee: dict | None, now_ms: int, url: str) -> dict:
    """Whitelist the live view (web.live_payload) plus the guarantee watch into the status document."""
    pk = live.get("package") or {}
    a = live.get("agent") or {}
    # The agent's "until" is when its current wait ends. For the gap after a test, a pause or the daily cap that
    # isn't a test, so only the scheduled time counts then (and nothing at all once the day's cap is reached).
    kind, until, sched = a.get("next_kind"), a.get("next_test_ms"), a.get("next_scheduled_ms")
    if kind == "cap":
        nexts = []
    elif kind in ("spacing", "paused"):
        nexts = [max(v for v in (until, sched) if isinstance(v, (int, float)))] if isinstance(sched, (int, float)) else []
    else:
        nexts = [v for v in (until, sched) if isinstance(v, (int, float))]
    avg = live.get("averages") or {}
    g = guarantee or {}
    cur = g.get("current") or {}
    health = live.get("health") or "unknown"
    return {
        "schema": SCHEMA,
        "generated_at_ms": now_ms,
        "url": url,
        "health": health,
        "health_label": HEALTH_LABELS.get(health, health),
        "package": {"name": short_name(pk.get("name")), "advertised_down": pk.get("advertised_down"),
                    "advertised_up": pk.get("advertised_up"), "target_down": pk.get("target_down"),
                    "degraded_down": pk.get("degraded_down"),
                    "minimum_guaranteed_down": pk.get("minimum_guaranteed_down")},
        "last": _test(live.get("last")),
        "last_ok": _test(live.get("last_ok")),
        "agent": {
            "seen": bool(a),
            "online": bool(a.get("online")),
            "last_seen_ms": a.get("last_seen_ms"),
            "state": a.get("state"),
            "phase": a.get("phase") if a.get("state") == "testing" else None,
            "live_mbps": _num(a.get("live_mbps")) if a.get("state") == "testing" else None,
            "paused": bool(a.get("paused")),
            "next_test_ms": min(nexts) if nexts else None,
            "interval_min": a.get("interval_min"),
            "mode": a.get("mode"),
        },
        "averages": {k: {"n": v.get("n"), "dl": _num(v.get("dl")), "ul": _num(v.get("ul")),
                         "pct_target": _num(v.get("pct_target"))}
                     for k, v in avg.items() if k in ("24h", "7d")},
        "guarantee": ({"applies": True, "mgds": g.get("mgds"), "days_below": g.get("days_below"),
                       "current_run_days": cur.get("days", 0), "current_run_from": cur.get("first"),
                       "longest_run_days": (g.get("longest") or {}).get("days", 0)}
                      if g.get("applies") else {"applies": False}),
    }


def write(path: Path, doc: dict) -> None:
    """Atomic replace; 0644 so other service users can read it (the unit's UMask is 0077)."""
    tmp = path.with_name(f".{path.name}.tmp")
    data = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    os.replace(tmp, path)
