from datetime import datetime, timezone

from speedmon import timeutil
from speedagent import policy as P
from speedagent.config import clamp_schedule

TZ = "Europe/London"


def ms(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


def test_dst_days_have_23_and_25_hours():
    spring = timeutil.resolve_range("custom", TZ, "2026-03-29", "2026-03-29")
    autumn = timeutil.resolve_range("custom", TZ, "2026-10-25", "2026-10-25")
    assert (spring[1] - spring[0]) / 3600e3 == 23
    assert (autumn[1] - autumn[0]) / 3600e3 == 25


def test_local_hour_buckets_across_fall_back():
    c = timeutil.LocalClock(TZ)
    # 00:30 UTC and 01:30 UTC on 25 Oct 2026 are 01:30 BST and 01:30 GMT: both local hour 1
    assert c.parts(ms(2026, 10, 25, 0, 30))[:2] == ("2026-10-25", 1)
    assert c.parts(ms(2026, 10, 25, 1, 30))[:2] == ("2026-10-25", 1)
    assert c.parts(ms(2026, 10, 25, 2, 30))[:2] == ("2026-10-25", 2)
    # spring forward: there is no local 01:xx on 29 Mar 2026
    hours = {c.parts(ms(2026, 3, 29, 0, 0) + i * 900_000)[1] for i in range(12)}
    assert 1 not in hours
    assert timeutil.offset_minutes(ms(2026, 7, 1), TZ) == 60 and timeutil.offset_minutes(ms(2026, 12, 1), TZ) == 0


def test_named_ranges():
    now = ms(2026, 10, 16, 12)
    a, b, _ = timeutil.resolve_range("yesterday", TZ, now=now)
    assert timeutil.iso(a) == "2026-10-14T23:00:00.000Z" and (b - a) == 86400e3
    a, b, label = timeutil.resolve_range("last_month", TZ, now=now)
    assert label == "September 2026" and timeutil.iso(a) == "2026-08-31T23:00:00.000Z"
    a, b, _ = timeutil.resolve_range("all", TZ, now=now, first_ms=123)
    assert a == 123


def test_clamp_never_allows_aggressive_values():
    c = clamp_schedule({"interval_min": 1, "burst_interval_min": 0, "confirm_delay_s": 1, "max_tests_per_day": 10_000,
                        "probe_interval_s": 1, "confirm_count": 50, "engine": "rm -rf"})
    assert c["interval_min"] == 5 and c["burst_interval_min"] == 5 and c["confirm_delay_s"] == 60
    assert c["max_tests_per_day"] == 300 and c["probe_interval_s"] == 10 and c["confirm_count"] == 5 and c["engine"] == "ookla"


def simulate(cfg, hours, poor=lambda h: False, start=ms(2026, 10, 16), fail_at=None):
    st = P.State()
    t, runs = start, []
    while t < start + hours * 3600e3:
        d = P.decide("pi", cfg, st, t, timeutil.local_parts(t, TZ)[0], False, False)
        if d["action"] == "run":
            res = {"status": "ok", "download_mbps": 300.0 if poor((t - start) / 3600e3) else 930.0}
            P.after(cfg, st, d["kind"], res, t, d.get("slot"), "u")
            runs.append((t, d["kind"]))
            t += 25_000
        else:
            t = max(t + 1000, d["until"])
    return runs, st


def test_one_scheduled_test_per_slot_at_random_minutes():
    cfg = clamp_schedule({"interval_min": 30})
    runs, _ = simulate(cfg, 48)
    sched = [t for t, k in runs if k == "scheduled"]
    assert len(sched) == 96
    slots = [t // 1800e3 for t in sched]
    assert len(set(slots)) == len(slots)
    minutes = {int(t / 60000) % 30 for t in sched}
    assert len(minutes) > 15                                   # spread across the slot, not always on the hour


def test_confirmation_then_burst_then_back_off():
    cfg = clamp_schedule({})
    runs, st = simulate(cfg, 6, poor=lambda h: 2 <= h < 3)
    kinds = [k for _, k in runs]
    assert kinds.count("confirmation") == 2 and kinds.count("burst") >= 3
    assert st.mode == "normal"
    burst_times = [t for t, k in runs if k == "burst"]
    assert all(b - a >= 5 * 60e3 for a, b in zip(burst_times, burst_times[1:]))


def test_isolated_blip_needs_no_burst():
    cfg = clamp_schedule({})
    start = ms(2026, 10, 16)
    first = [t for t, k in simulate(cfg, 1)[0] if k == "scheduled"][0]
    runs, _ = simulate(cfg, 3, poor=lambda h: abs(h - (first - start) / 3600e3) < 0.01)
    kinds = [k for _, k in runs]
    assert kinds.count("confirmation") == 1 and "burst" not in kinds


def test_daily_cap_and_pause():
    cfg = clamp_schedule({"interval_min": 5, "max_tests_per_day": 10})
    runs, st = simulate(cfg, 3)
    assert len(runs) == 10
    st2 = P.State()
    d = P.decide("pi", cfg, st2, ms(2026, 10, 16, 12), "2026-10-16", True, True)
    assert d["action"] == "wait" and d["kind"] == "paused"


def test_restart_does_not_repeat_a_slot():
    cfg = clamp_schedule({})
    st = P.State()
    t = ms(2026, 10, 16, 10)
    d = P.decide("pi", cfg, st, t + 1800e3 - 1000, "d", False, False)    # end of slot: due time has passed
    assert d["action"] == "run" and d["kind"] == "scheduled"
    P.after(cfg, st, "scheduled", {"status": "ok", "download_mbps": 930.0}, t + 1800e3 - 1000, d["slot"])
    restored = P.State.from_dict(st.to_dict())
    d2 = P.decide("pi", cfg, restored, t + 1800e3 - 500 + 120_000, "d", False, False)
    assert d2["action"] == "wait" or d2.get("slot") != d["slot"]


def test_persistent_degradation_suspends_extra_tests_then_resumes():
    cfg = clamp_schedule({})
    runs, st = simulate(cfg, 24, poor=lambda h: h < 12)
    first12 = [k for t, k in runs if (t - runs[0][0]) < 12 * 3600e3]
    later = [k for t, k in runs if (t - runs[0][0]) > 8 * 3600e3 and (t - runs[0][0]) < 12 * 3600e3]
    assert "confirmation" in first12                  # early on it confirms...
    assert set(later) == {"scheduled"}                # ...but once persistent, only scheduled tests run
    assert not st.persistent and st.mode == "normal"  # and it recovered after the line came back
    total = len(runs)
    assert total < 48 + 20
