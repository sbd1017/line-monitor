"""What Ofcom's automatic compensation scheme says each outage is worth, given what was actually recorded.

The scheme pays for a *complete* loss of service that is reported and not fixed within two full working days, and
it pays per day from then until the fix. It is automatic (no asking) and it does not cover slowness, only total
loss, so only probe-confirmed outages count here and degradation incidents never appear.

Two things are deliberately not guessed at:
  * without a recorded report time there is no clock, so the incident is listed as unclaimed rather than assumed
    reported on day one;
  * the trigger is two *full working days* after the report, skipping weekends and England and Wales bank
    holidays, so the count can only be short rather than optimistic.

Rate in force on the trigger date is applied to the whole incident. Amounts are an expectation to check against a
bill, not an entitlement this program can grant.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import analytics, timeutil

DAY_MS = 86_400_000
GRACE_WORKING_DAYS = 2
MAX_DAYS = 365
REPORT_CATEGORIES = ("complaint", "isp", "outage")
REPORT_WINDOW_MS = 14 * DAY_MS          # a report may be a while after the outage, but not months

# Daily rate for a delayed repair, from the date it took effect. Ofcom uprates every April; add the new one each
# year and never alter the old ones, so an old incident keeps the rate that applied to it.
RATES = (
    {"from": "2024-04-01", "per_day": 9.76},
    {"from": "2025-04-01", "per_day": 9.98},
    {"from": "2026-04-01", "per_day": 10.34},
)

# England and Wales bank holidays, so the two working days are counted honestly. Weekdays only beyond this list.
BANK_HOLIDAYS = frozenset((
    "2025-01-01", "2025-04-18", "2025-04-21", "2025-05-05", "2025-05-26", "2025-08-25", "2025-12-25", "2025-12-26",
    "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04", "2026-05-25", "2026-08-31", "2026-12-25", "2026-12-28",
))


def rate_for(day: date) -> float:
    """The rate in force on that date; the oldest if the date predates the table."""
    rate = RATES[0]["per_day"]
    for entry in RATES:
        if day.isoformat() >= entry["from"]:
            rate = entry["per_day"]
    return rate


def _local(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz)


def _midnight(ms: int, tz: ZoneInfo) -> int:
    d = _local(ms, tz).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)


def trigger_at(reported_ms: int, tz: ZoneInfo) -> int:
    """Two full working days after the report, as the start of the next local day."""
    d = _local(reported_ms, tz).date()
    counted = 0
    while counted < GRACE_WORKING_DAYS:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in BANK_HOLIDAYS:
            counted += 1
    d += timedelta(days=1)                 # the two days must be *full*: the clock starts the day after
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)


def _fetch(conn, sql: str, args: tuple):
    """Works with a connection or with a Database (the web app passes a connection, tests pass the Database)."""
    if hasattr(conn, "read"):
        with conn.read() as c:
            return c.execute(sql, args).fetchall()
    return conn.execute(sql, args).fetchall()


def reported_for(conn, incident: dict) -> int | None:
    """Earliest complaint note inside or shortly after the outage: the moment the owner told the ISP."""
    marks = ",".join("?" * len(REPORT_CATEGORIES))
    rows = _fetch(conn, f"SELECT min(start_ms) FROM annotations WHERE deleted_at IS NULL AND category IN ({marks}) "
                        f"AND start_ms >= ? AND start_ms <= ?",
                  (*REPORT_CATEGORIES, incident["first_ms"] - timeutil.HOUR_MS,
                   incident["last_ms"] + REPORT_WINDOW_MS))
    return rows[0][0] if rows else None


def assess(conn, from_ms: int, to_ms: int, tz: str, now_ms: int | None = None,
           incidents: list[dict] | None = None) -> dict:
    """Per incident: what the scheme would pay, and why not when it would not. Nothing here is persisted."""
    zone = ZoneInfo(tz)
    now_ms = timeutil.now_ms() if now_ms is None else now_ms
    out = []
    if incidents is None:
        if hasattr(conn, "read"):
            with conn.read() as c:
                incidents = analytics.outage_incidents(c, from_ms, to_ms)
        else:
            incidents = analytics.outage_incidents(conn, from_ms, to_ms)
    for inc in incidents:
        reported = reported_for(conn, inc)
        item = {"first_ms": inc["first_ms"], "last_ms": inc["last_ms"], "min_duration_ms": inc["min_duration_ms"],
                "max_duration_ms": inc["max_duration_ms"], "probes": inc["probes"], "reported_ms": reported,
                "trigger_ms": None, "fixed_ms": inc.get("after_ms"), "ongoing": inc.get("after_ms") is None,
                "days": 0, "rate": None, "amount": 0.0, "status": "unreported"}
        if reported is None:
            item["reason"] = "no report noted, so there is no clock to run"
            out.append(item)
            continue
        trigger = trigger_at(reported, zone)
        fixed = item["fixed_ms"] or now_ms
        item["trigger_ms"] = trigger
        if fixed <= trigger:
            item["status"] = "inside_grace"
            item["reason"] = "fixed within two working days"
            out.append(item)
            continue
        days = min(MAX_DAYS, max(1, (_local(fixed, zone).date() - _local(trigger, zone).date()).days + 1))
        rate = rate_for(_local(trigger, zone).date())
        item.update(status="ongoing" if item["ongoing"] else "claimed", days=days, rate=rate, amount=round(days * rate, 2))
        item["reason"] = "still down, counted to now" if item["ongoing"] else "complete loss, reported and not fixed in time"
        out.append(item)
    claimed = [x for x in out if x["status"] in ("claimed", "ongoing")]
    return {"incidents": out, "n": len(out), "n_claimable": len(claimed), "n_unreported": sum(1 for x in out if x["status"] == "unreported"),
            "amount": round(sum(x["amount"] for x in claimed), 2), "grace_working_days": GRACE_WORKING_DAYS,
            "rate_now": RATES[-1]["per_day"], "rates": [dict(r) for r in RATES],
            "note": "Complete loss of service only, from probe-confirmed outages. A claim needs the fault reported and a "
                    "note added here with category 'complaint'; the clock then starts two full working days later. "
                    "Amounts use the Ofcom rate in force, are a figure to check against a bill, and are not a ruling."}
