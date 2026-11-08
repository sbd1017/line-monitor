"""Where does the loss start? A short traceroute to Virgin's first router, every quarter hour.

A speed test says the line is slow. Support's first move is "is it us or you", and a single figure cannot answer
that. This does: hop 1 is the Flint, i.e. the edge of the house. Everything after it is Virgin's. Loss that appears
only after hop 1, while hop 1 is clean and the loss carries to the end of the path, is on their network and not on
anything in the house.

The target is always the same public anchor, not the access router: Virgin's first router answers TTL-exceeded
probes but drops echo requests aimed at itself, so tracing *to* it shows nothing at hop 2. Tracing through it does.

Reading rules, because routers are allowed to be busy:
  * a lossy hop in the middle that clears afterwards is usually that router deprioritising the probe, so the
    boundary is only called 'isp' when the loss also reaches the last responding hop (``sustained``);
  * fewer than 8 cycles is 'unknown';
  * loss at hop 1 is the house ('lan');

IPv4 addresses are stored as mtr reports them. The export masks them unless public IPs are being shared.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

MTR = shutil.which("mtr") or "/usr/bin/mtr"
MAX_HOPS = 8
BAD_PCT = 20.0
MIN_CYCLES = 8
ANCHOR = "1.1.1.1"
NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _run(target: str, cycles: int, timeout: float) -> dict:
    proc = subprocess.run([MTR, "-j", "-n", "-c", str(cycles), target], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise OSError(f"mtr exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
    return json.loads(proc.stdout)


def hops_of(report: dict) -> list[dict]:
    """Flatten mtr's hubs into the small shape we keep, hop 1 first."""
    out = []
    for hub in report.get("report", {}).get("hubs", []):
        host = hub.get("host")
        out.append({"hop": int(hub.get("count") or len(out) + 1),
                    "host": None if host in (None, "???") else str(host)[:64],
                    "loss_pct": float(hub.get("Loss%") or 0.0),
                    "sent": int(hub.get("Snt") or 0),
                    "avg_ms": float(hub.get("Avg") or 0.0),
                    "best_ms": float(hub.get("Best") or 0.0),
                    "worst_ms": float(hub.get("Wrst") or 0.0)})
    return out[:MAX_HOPS]


def classify(hops: list[dict], cycles: int) -> tuple[str, int | None, int]:
    """(boundary, first bad hop, sustained). Deliberately conservative; see the module docstring."""
    if cycles < MIN_CYCLES or not hops:
        return "unknown", None, 0
    gateway = hops[0]
    if gateway["host"] is None or gateway["loss_pct"] >= BAD_PCT:
        return "lan", None, 0
    responding = [h for h in hops[1:] if h["host"] is not None]
    if not responding:
        return "unknown", None, 0
    bad = [h for h in responding if h["loss_pct"] >= BAD_PCT]
    if not bad:
        return "none", None, 0
    sustained = int(responding[-1]["loss_pct"] >= BAD_PCT)
    return ("isp" if sustained else "unknown"), bad[0]["hop"], sustained


def measure(cycles: int = 20, timeout: float = 60) -> dict:
    """One path measurement. Raises OSError when mtr is unusable, so the caller records a failure."""
    hops = hops_of(_run(ANCHOR, cycles, timeout))
    error = None if hops else f"no hops reported for {ANCHOR}"
    boundary, first_bad, sustained = classify(hops, cycles)
    gateway_hop = hops[0] if hops else None
    return {"target": ANCHOR, "target_source": "anchor", "hops": json.dumps(hops, separators=(",", ":")),
            "hop_count": len(hops), "boundary": boundary, "first_bad_hop": first_bad, "sustained": sustained,
            "gw_loss_pct": gateway_hop["loss_pct"] if gateway_hop else None,
            "gw_rtt_ms": gateway_hop["avg_ms"] if gateway_hop else None,
            "cycles": cycles, "error": error}
