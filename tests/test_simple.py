import random

from speedmon import analytics, settings_store, simple, timeutil

M125 = {"from": "2026-01-01", "name": "Virgin Media M125", "advertised_down": 132.0, "advertised_up": 20.0,
        "minimum_guaranteed_down": None, "target_down": 120.0, "degraded_down": 66.0, "severe_down": 26.0,
        "target_up": 18.0, "degraded_up": 10.0, "tiers": [130, 120, 100]}
GIG1 = {**settings_store.GIG1, "from": "2026-11-16"}
S = settings_store.apply_current({**{f.key: f.default for f in settings_store.FIELDS}, "packages": [M125]})
NOW = timeutil.local_to_ms("2026-10-27 12:00", "Europe/London")
clk = timeutil.LocalClock("Europe/London")


def T(t, dl, pk=M125, ok=True, q=(), kind="scheduled"):
    x = analytics.Measurement(t, f"u{t}", ok, dl if ok else None, 20.0 if ok else None, 10.0, 1.0, 0.0, kind, "agent",
                              None if ok else "wan_down", "1", False, None)
    x.day, x.hour, x.dow, x.minute = clk.parts(t)
    x.pk, x.q = pk, q
    return x


def every_half_hour(days, speed, pk=M125, end=NOW):
    start = int(end - days * timeutil.DAY_MS)
    return [T(t, speed(t), pk) for t in range(start, end, 1800_000)]


def test_good_line_says_yes_and_nothing_to_do():
    r = simple.build(every_half_hour(7, lambda t: 135.0), [], S, "7d", NOW)
    assert r["level"] == "good" and r["verdict"] == "Yes. You're getting what you pay for"
    assert "all of it" in r["detail"] and not r["steps"]["action"] and r["dropouts"]["count"] == 0
    assert "two weeks" in r["slow_times"]["text"]                       # a week isn't enough to claim a slow time
    assert "doesn't come with a guaranteed minimum" in r["promise"]["text"]


def test_slow_line_says_no_and_gives_the_steps():
    r = simple.build(every_half_hour(7, lambda t: 40.0), [], S, "7d", NOW)
    assert r["level"] == "bad" and "well below" in r["verdict"] and r["typical_down"] == 40
    assert r["steps"]["action"] and "132 Mbps" in r["steps"]["say"] and "40 Mbps" in r["steps"]["say"]
    assert any("CISAS" in s for s in r["steps"]["list"])
    assert "—" not in repr(r)                                      # no em dashes anywhere


def test_monitor_side_slowdowns_are_set_aside():
    tests = every_half_hour(7, lambda t: 135.0)
    for x in tests[::3]:
        x.dl, x.q = 20.0, ("pi_busy",)                                  # the Pi was busy: not the line's fault
    r = simple.build(tests, [], S, "7d", NOW)
    assert r["level"] == "good" and r["slowest"]["mbps"] == 135 and r["set_aside"] == len(tests[::3])


def test_evening_slowdown_found_and_only_a_problem_below_target():
    rnd = random.Random(1)
    evening = lambda t: 19 <= clk.parts(t)[1] < 22 and clk.parts(t)[2] < 5
    r = simple.build(every_half_hour(30, lambda t: (60.0 if evening(t) else 134.0) + rnd.random()), [], S, "30d", NOW)
    assert r["level"] == "warn" and r["verdict"] == "Mostly, but it slows down in the evenings"
    assert "weekday evenings, 7pm to 10pm" in r["slow_times"]["text"] and "between 7pm and 10pm on weekdays" in r["steps"]["say"]
    fast = settings_store.apply_current({**S, "packages": [{**M125, "advertised_down": 50.0, "target_down": 45.0, "degraded_down": 25.0}]})
    pk = fast["package"]
    r = simple.build([T(x.t, x.dl, pk) for x in every_half_hour(30, lambda t: (60.0 if evening(t) else 134.0) + rnd.random())],
                     [], fast, "30d", NOW)
    assert r["level"] == "good" and "still more than what you pay for" in r["slow_times"]["text"]


def test_three_days_below_the_guarantee_can_be_claimed():
    s = settings_store.apply_current({**S, "packages": [M125, GIG1]}, NOW + 30 * timeutil.DAY_MS)
    end = timeutil.local_to_ms("2026-11-30 00:00", "Europe/London")
    bad = lambda t: clk.parts(t)[0] in ("2026-11-25", "2026-11-26", "2026-11-27") and clk.parts(t)[1] == 20
    r = simple.build(every_half_hour(7, lambda t: 400.0 if bad(t) else 950.0, GIG1, end), [], s, "7d", end)
    p = r["promise"]
    assert p["applies"] and p["days_below"] == 3 and p["can_claim"] and "Wed 25 Nov to Fri 27 Nov" in p["claim_text"]
    assert r["level"] == "bad" and "guaranteed minimum" in r["verdict"] and "under the guarantee" in r["steps"]["say"]


def test_the_new_package_is_judged_on_its_own():
    s = settings_store.apply_current({**S, "packages": [M125, GIG1]}, NOW + 30 * timeutil.DAY_MS)
    split = timeutil.local_to_ms("2026-11-16 00:00", "Europe/London")
    end = split + 5 * timeutil.DAY_MS
    tests = [T(x.t, x.dl, M125 if x.t < split else GIG1) for x in every_half_hour(7, lambda t: 135.0 if t < split else 950.0, M125, end)]
    r = simple.build(tests, [], s, "7d", end)
    assert r["since"] == "Mon 16 Nov" and r["package"]["advertised_down"] == 1130 and r["slowest"]["mbps"] == 950
    m125 = simple.build(every_half_hour(3, lambda t: 135.0), [], s, "7d", NOW)
    assert "From Mon 16 Nov, your new package guarantees at least 565 Mbps" in m125["promise"]["text"]


def test_dropouts_in_words():
    o = lambda a, lo, hi: {"first_ms": a, "min_duration_ms": lo * 60_000, "max_duration_ms": hi * 60_000 if hi else None}
    d = simple.dropouts([o(NOW, 8, 12), o(NOW + 9_000_000, 3, None)], "Europe/London", "this week")
    assert d["count"] == 2 and d["minutes"] == 13 and d["text"].startswith("Your internet dropped out twice this week, for about 13 minutes")
    assert "The longest was about 10 minutes, on Tue 27 Oct, noon." in d["text"]
    assert simple.dropouts([o(NOW, 70, 80)], "Europe/London", "this month")["text"] == \
        "Your internet dropped out once this month, for about 1 hour 15 minutes, on Tue 27 Oct, noon."
    many = [o(NOW + i * 10_000_000, 30, 30) for i in range(3)]
    r = simple.build(every_half_hour(7, lambda t: 135.0), many, S, "7d", NOW)
    assert r["level"] == "warn" and "dropping out" in r["verdict"]
    r = simple.build(every_half_hour(7, lambda t: 135.0), [o(NOW, 66, 66), o(NOW + 10_000_000, 7, 7)], S, "7d", NOW)
    assert r["verdict"] == "Your speed is fine, but your internet was off for over 1 hour"


def test_not_enough_tests():
    r = simple.build(every_half_hour(0.1, lambda t: 135.0), [], S, "7d", NOW)
    assert r["level"] == "unknown" and "only 5 usable tests" in r["detail"] and not r["steps"]["action"]
    assert simple.build([], [], S, "30d", NOW)["detail"].endswith("There are no usable tests for the past month.")


def test_endpoint(signed_in):
    c = signed_in
    r = c.get("/speedtest/api/simple?range=30d")
    assert r.status_code == 200 and r.json()["level"] == "unknown" and r.json()["range"]["preset"] == "30d"
    assert c.get("/speedtest/api/simple?range=all").status_code == 400
    assert c.get("/speedtest/simple").status_code == 200
