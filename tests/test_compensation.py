"""Ofcom automatic compensation: working-day clock, historical rates, and the unclaimed/inside-grace paths."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from speedmon import compensation, timeutil

TZ = ZoneInfo("Europe/London")


def at(local: str) -> int:
    return int(datetime.fromisoformat(local).replace(tzinfo=TZ).timestamp() * 1000)


def incident(first: int, last: int, after=None, probes=3):
    return {"first_ms": first, "last_ms": last, "min_duration_ms": last - first,
            "max_duration_ms": (after - first) if after else None, "after_ms": after, "probes": probes}


def note(conn, when_ms, category="complaint"):
    now = timeutil.now_iso()
    conn.execute("INSERT INTO annotations (uuid, start_ms, end_ms, title, body, category, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (f"n{when_ms}", when_ms, None, "reported", "", category, now, now))


# 2026-03-02 is a Monday. Two full working days (Tue, Wed) mean the clock starts Thursday 5 March.

def local_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, TZ).date()


def test_the_clock_skips_weekends():
    # reported Friday 6 March: the two full working days are Mon and Tue, so the clock starts Wednesday 11 March
    assert local_date(compensation.trigger_at(at("2026-03-06T09:00"), TZ)) == date(2026, 3, 11)


def test_the_clock_skips_bank_holidays():
    # reported Thursday 2 April, Friday 3 April is Good Friday, Monday 6 April is Easter Monday
    assert local_date(compensation.trigger_at(at("2026-04-02T09:00"), TZ)) == date(2026, 4, 9)


def test_rates_follow_the_date():
    assert compensation.rate_for(datetime(2025, 6, 1).date()) == 9.98
    assert compensation.rate_for(datetime(2026, 6, 1).date()) == 10.34
    assert compensation.rate_for(datetime(2024, 1, 1).date()) == 9.76          # predates the table: oldest wins


def test_an_unreported_outage_is_listed_but_unclaimed(db):
    inc = incident(at("2026-03-02T10:00"), at("2026-03-02T12:00"), at("2026-03-02T12:30"))
    got = compensation.assess(db, 0, timeutil.now_ms(), "Europe/London", incidents=[inc])
    assert got["n"] == 1 and got["n_claimable"] == 0 and got["amount"] == 0
    assert got["incidents"][0]["status"] == "unreported" and "no report noted" in got["incidents"][0]["reason"]


def test_a_fix_inside_two_working_days_pays_nothing(db):
    with db.tx() as c:
        note(c, at("2026-03-02T10:05"))
    inc = incident(at("2026-03-02T10:00"), at("2026-03-02T12:00"), at("2026-03-04T12:00"))   # Wednesday
    got = compensation.assess(db, 0, timeutil.now_ms(), "Europe/London", incidents=[inc])
    assert got["n_claimable"] == 0 and got["incidents"][0]["status"] == "inside_grace"


def test_a_late_fix_is_counted_by_calendar_day_from_the_trigger(db):
    with db.tx() as c:
        note(c, at("2026-03-02T10:05"))
    inc = incident(at("2026-03-02T10:00"), at("2026-03-02T12:00"), at("2026-03-07T09:00"))   # Saturday
    got = compensation.assess(db, 0, timeutil.now_ms(), "Europe/London", incidents=[inc])
    item = got["incidents"][0]
    assert item["status"] == "claimed" and item["days"] == 3 and item["rate"] == 9.98   # March 2026 rate
    assert item["amount"] == round(3 * 9.98, 2) and got["amount"] == item["amount"]


def test_an_ongoing_outage_counts_to_now(db):
    with db.tx() as c:
        note(c, at("2026-03-02T10:05"))
    inc = incident(at("2026-03-02T10:00"), at("2026-03-02T12:00"), None)
    got = compensation.assess(db, 0, timeutil.now_ms(), "Europe/London", now_ms=at("2026-03-05T09:00"), incidents=[inc])
    assert got["incidents"][0]["status"] == "ongoing" and got["incidents"][0]["days"] == 1


def test_a_late_note_still_finds_its_outage(db):
    with db.tx() as c:
        note(c, at("2026-03-09T20:00"))            # a week later, within the window
    inc = incident(at("2026-03-02T10:00"), at("2026-03-02T12:00"), at("2026-03-02T12:30"))
    got = compensation.assess(db, 0, timeutil.now_ms(), "Europe/London", incidents=[inc])
    assert got["incidents"][0]["reported_ms"] == at("2026-03-09T20:00")


def test_other_note_categories_do_not_start_the_clock(db):
    with db.tx() as c:
        note(c, at("2026-03-02T10:05"), category="engineer")
    inc = incident(at("2026-03-02T10:00"), at("2026-03-02T12:00"), at("2026-03-09T09:00"))
    got = compensation.assess(db, 0, timeutil.now_ms(), "Europe/London", incidents=[inc])
    assert got["incidents"][0]["status"] == "unreported"


def test_deleted_notes_are_ignored(db):
    with db.tx() as c:
        note(c, at("2026-03-02T10:05"))
        c.execute("UPDATE annotations SET deleted_at = ?", (timeutil.now_iso(),))
    inc = incident(at("2026-03-02T10:00"), at("2026-03-02T12:00"), at("2026-03-09T09:00"))
    got = compensation.assess(db, 0, timeutil.now_ms(), "Europe/London", incidents=[inc])
    assert got["incidents"][0]["status"] == "unreported"
