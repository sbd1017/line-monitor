"""Measurement quality: could something on the monitor's side have held a result down?

Every test already records the Pi's side of the measurement (speedagent/main.py): the negotiated Ethernet speed,
other traffic on the Pi's port during and just before the test, CPU load and temperature, firmware throttling
flags and Ethernet error counters. This module turns those into a few plain, deterministic labels, so a slow
result can be told apart from a slow result the monitor might have caused. Nothing here edits raw data: labels
are recomputed from stored records whenever they're needed, and the rules are listed in the methodology.

A test is CLEAN when no label that could have lowered the result applies. "At the monitor's limit" and
"fallback engine" are informational: the first can only understate the line, the second is still a valid
measurement by a different method.

What it cannot see: traffic from other devices in the house (that needs the router's counters).
"""
from __future__ import annotations

LINK_EFFICIENCY = 0.94        # best TCP throughput on an Ethernet link, as a share of its negotiated speed
BUSY_SHARE = 0.05             # other traffic on the Pi above 5% of the package's advertised download ...
BUSY_MIN = 5.0                # ... (and at least 5 Mbps) could have taken bandwidth from the test
LOAD_LIMIT = 3.0              # 1-minute load average; the Pi 5 has 4 cores
TEMP_LIMIT = 80.0             # °C; the Pi 5 starts throttling at 85
ERROR_LIMIT = 50              # Ethernet errors or drops during the test
AT_LIMIT_SHARE = 0.95         # within 5% of the usable link ceiling = pinned at the monitor's limit

# code: (label, affects clean, explanation)
FLAGS = {
    "link_slow": ("Monitor link too slow", True,
                  "The Pi's Ethernet link was slower than this package's target, so the result may show the cable, "
                  "not the line."),
    "pi_busy": ("Pi busy", True, "Other traffic on the Pi's network port could have taken bandwidth from the test."),
    "pi_strained": ("Pi under load", True, "The Pi was heavily loaded, hot or throttled during the test."),
    "link_errors": ("Ethernet errors", True, "The Pi's Ethernet port counted errors or dropped packets during the test."),
    "no_diagnostics": ("Not verified", True, "The monitor's own diagnostics for this test are missing, so it can't be "
                       "shown to be clean."),
    "at_link_limit": ("At the monitor's limit", False, "The result is at the most the Pi's Ethernet link can carry. The "
                      "line may be faster: treat the figure as a lower bound."),
    "fallback": ("Fallback engine", False, "The primary engine failed, so the fallback engine measured this slot."),
}
ORDER = ("link_slow", "link_errors", "pi_strained", "pi_busy", "no_diagnostics", "at_link_limit", "fallback")

# columns the checks need (measurement m, diagnostics d), for SQL callers
_DIAG = ("link_speed_mbps", "uuid", "background_rx_mbps", "background_tx_mbps", "pretest_rx_mbps", "pretest_tx_mbps",
         "cpu_load1", "cpu_temp_c", "throttled", "rx_errors_delta", "tx_errors_delta", "rx_dropped_delta", "tx_dropped_delta")


def sql_columns(m: str = "m", d: str = "d") -> tuple:
    return (f"{m}.link_speed_mbps",) + tuple(f"{d}.{c}" for c in _DIAG)


SQL_COLUMNS = sql_columns()


def busy_limit(pk: dict | None) -> float:
    """Other traffic on the Pi (Mbps) above which a test is 'Pi busy'. Relative to the package, because the same stream
    is a big share of a 132 Mbps line and a sliver of a gigabit one. The agent waits for a lull below the same figure."""
    return max(BUSY_MIN, BUSY_SHARE * pk["advertised_down"]) if pk and pk.get("advertised_down") else 20.0


def ceiling(link_mbps: float | None) -> float | None:
    return link_mbps * LINK_EFFICIENCY if link_mbps else None


def _throttled_now(flags: str | None) -> bool:
    """Firmware flags (vcgencmd get_throttled): bits 0-3 are conditions right now; bits 16+ are history since boot."""
    try:
        return bool(int(flags, 16) & 0xF) if flags else False
    except ValueError:
        return False


def assess(ok: bool, source: str, kind: str, dl: float | None, pk: dict | None, diag: tuple | None) -> tuple:
    """Quality codes for one test (ORDER order). `diag` is the SQL_COLUMNS values (None: no row joined)."""
    if not ok or source not in ("agent", "synthetic"):
        return ()                      # failures have their own classification; imported rows have no diagnostics
    out = set()
    if kind == "fallback":
        out.add("fallback")
    if diag is None or diag[2] is None:
        out.add("no_diagnostics")
        link = diag[0] if diag else None
    else:
        m_link, d_link, _, bg_rx, bg_tx, pre_rx, pre_tx, load1, temp, thr, rxe, txe, rxd, txd = diag
        link = m_link or d_link
        other = max(v or 0 for v in (bg_rx, bg_tx, pre_rx, pre_tx))
        if other > busy_limit(pk):
            out.add("pi_busy")
        if (load1 or 0) > LOAD_LIMIT or (temp or 0) >= TEMP_LIMIT or _throttled_now(thr):
            out.add("pi_strained")
        if max(v or 0 for v in (rxe, txe)) > 0 or max(v or 0 for v in (rxd, txd)) > ERROR_LIMIT:
            out.add("link_errors")
    cap = ceiling(link)
    if cap and pk:
        if cap < pk["target_down"]:
            out.add("link_slow")
        elif dl is not None and dl >= AT_LIMIT_SHARE * cap and cap < pk["advertised_down"]:
            out.add("at_link_limit")
    return tuple(c for c in ORDER if c in out)


def is_clean(codes) -> bool:
    return not any(FLAGS[c][1] for c in codes)


def labels(codes) -> list[dict]:
    return [{"code": c, "label": FLAGS[c][0], "affects_clean": FLAGS[c][1], "why": FLAGS[c][2]} for c in codes]


def capacity(link_mbps: float | None, pk: dict) -> dict:
    """Can this monitor's Ethernet link measure this package? level: ok | advertised | target | guarantee | unknown."""
    cap = ceiling(link_mbps)
    if not cap:
        return {"level": "unknown", "link_mbps": link_mbps, "ceiling_mbps": None, "text": "The Pi's Ethernet link speed isn't known yet."}
    adv, tgt, mg = pk["advertised_down"], pk["target_down"], pk.get("minimum_guaranteed_down")
    link_txt = f"{link_mbps / 1000:g} Gb/s" if link_mbps >= 1000 else f"{link_mbps:g} Mb/s"
    if cap >= adv:
        return {"level": "ok", "link_mbps": link_mbps, "ceiling_mbps": cap,
                "text": f"The Pi's {link_txt} link (about {cap:,.0f} Mbps usable) can measure this package's full advertised speed."}
    if mg and cap < mg:
        level, text = "guarantee", (f"MONITOR CAPACITY LIMITED: the Pi's {link_txt} link carries about {cap:,.0f} Mbps, below "
                                    f"the {mg:,.0f} Mbps minimum guaranteed speed. Results can't be used against the ISP until "
                                    "the link is faster.")
    elif cap < tgt:
        level, text = "target", (f"MONITOR CAPACITY LIMITED: the Pi's {link_txt} link carries about {cap:,.0f} Mbps, below the "
                                 f"{tgt:,.0f} Mbps target, so on-target results are impossible. Fix the cable or use a faster adapter.")
    else:
        level, text = "advertised", (f"Monitor capacity limited: the Pi's {link_txt} link carries about {cap:,.0f} Mbps, so it can "
                                     f"check the {tgt:,.0f} Mbps target{f' and the {mg:,.0f} Mbps guarantee' if mg else ''}, but "
                                     f"not the full {adv:,.0f} Mbps advertised. A 2.5 Gb/s USB adapter removes the limit.")
    return {"level": level, "link_mbps": link_mbps, "ceiling_mbps": cap, "text": text}


def register_sql(conn, package_clock) -> None:
    """lm_quality(status, source, kind, download_mbps, t_ms, <sql_columns...>) -> 'clean' | 'flagged' | NULL, so a
    query can filter on exactly these rules (the Explorer's quality filter)."""
    def fn(status, source, kind, dl, t_ms, *diag):
        ok = status == "ok"
        if not ok or source not in ("agent", "synthetic"):
            return None
        return "clean" if is_clean(assess(ok, source, kind, dl, package_clock.at(t_ms), diag)) else "flagged"
    conn.create_function("lm_quality", 5 + len(SQL_COLUMNS), fn, deterministic=True)
