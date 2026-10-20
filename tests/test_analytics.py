import random

from speedmon import analytics, ingest, settings_store, synthetic, timeutil
from helpers import item, measurement, probe

S = settings_store.apply_current({f.key: f.default for f in settings_store.FIELDS})


def T(t, dl, kind="scheduled", ok=True, failure=None, uuid=None, related=None):
    x = analytics.Measurement(t, uuid or str(t), ok, dl if ok else None, 100.0 if ok else None, 10.0, 1.0, 0.0, kind, "agent",
                    failure, "1", False, related)
    x.day, x.hour, x.dow, x.minute = timeutil.LocalClock("Europe/London").parts(t)
    x.pk = S["package"]
    return x


def test_percentile_matches_excel_percentile_inc():
    v = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert analytics.percentile(v, 50) == 5.5
    assert abs(analytics.percentile(v, 10) - 1.9) < 1e-9
    assert abs(analytics.percentile(v, 90) - 9.1) < 1e-9
    d = analytics.describe(list(range(1, 51)))
    assert d["p"]["1"] is None and d["p"]["5"] is not None            # P1 needs 100 values


def test_basis_excludes_burst_and_uses_fallback_for_engine_failures():
    tests = [T(1, 930), T(2, 300, kind="burst"), T(3, None, ok=False, failure="server_error", uuid="p"),
             T(4, 920, kind="fallback", related="p"), T(5, 900, kind="manual")]
    b = analytics.basis(tests)
    assert [x.t for x in b] == [1, 4]
    s = analytics.summary(tests, S)
    assert s["n"] == 2 and s["n_ok"] == 2 and s["n_all"] == 5


def test_failures_never_count_as_zero_and_local_faults_not_against_isp():
    tests = [T(i, 930) for i in range(10)] + [T(20, None, ok=False, failure="lan_fault"), T(21, None, ok=False, failure="wan_down")]
    s = analytics.summary(tests, S)
    assert s["download"]["min"] == 930 and s["n_local"] == 1 and s["n_outage"] == 1
    assert abs(s["pct_failed"] - 100 / 11) < 1e-9                       # local fault left out of the denominator


def test_streaks_are_sampled_spans():
    base = 1_790_000_000_000
    tests = [T(base + i * 1800_000, 930 if i < 5 else 300) for i in range(8)]
    st = analytics.streaks(tests, S)
    g = st["longest_good"]
    assert g["count"] == 5 and g["sampled_span_ms"] == 4 * 1800e3 and g["after_ms"] == tests[5].t
    assert st["current"]["kind"] == "poor" and st["current"]["count"] == 3
    inc = analytics.degradation_incidents(tests, S)
    assert len(inc) == 1 and inc[0]["count"] == 3


def test_congestion_detector_finds_real_pattern_and_stays_quiet_on_noise(db):
    end = timeutil.now_ms()
    with db.tx() as c:
        synthetic.seed(c, days=45, interval_min=30, probes=False, end_ms=end)
    s = dict(S)
    with db.read() as c:
        tests = analytics.load_tests(c, end - 45 * 86400e3, end + 1, s)
    res = analytics.congestion(tests, s)
    wd = [f for f in res["findings"] if f["daytype"] == "weekday"]
    assert wd and wd[0]["start"] in ("19:00", "19:30") and wd[0]["end"] in ("22:30", "23:00")
    rng = random.Random(1)
    flat = [T(end - i * 1800_000, rng.gauss(930, 10)) for i in range(45 * 48)]
    assert analytics.congestion(flat, s)["findings"] == []
    few = flat[:200]
    assert analytics.congestion(few, s)["status"] == "insufficient"


def test_outage_bounds_from_probes(db):
    t0 = 1_790_000_000_000
    states = ["ok", "ok", "wan_down", "wan_down", "wan_down", "ok", "lan_fault", "ok"]
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("probe", probe(t0 + i * 60_000, st, seq=i + 1)) for i, st in enumerate(states)], False)
        out = analytics.outage_incidents(c, t0, t0 + 3600e3)
    assert len(out) == 1
    o = out[0]
    assert o["min_duration_ms"] == 120_000 and o["max_duration_ms"] == 240_000


def test_timeline_distinguishes_monitor_offline_from_outage(db):
    from speedmon import series
    t0 = 1_790_000_000_000
    probes = [probe(t0 + i * 60_000, "ok", seq=i + 1) for i in range(10)]
    probes += [probe(t0 + (40 + i) * 60_000, "wan_down", seq=20 + i) for i in range(3)]       # 30-min silence, then outage
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("probe", p) for p in probes], False)
        tl = series.timeline(c, t0 - 600_000, t0 + 43 * 60_000)
    classes = [s[2] for s in tl["segments"]]
    assert classes[0] == "unknown" and "offline" in classes and classes[-1] == "outage"
    assert classes.index("offline") < classes.index("outage")


def test_anomaly_is_causal_and_robust(db):
    t0 = 1_790_000_000_000 - 30 * 86400_000
    items = []
    rng = random.Random(3)
    seq = 0
    for day in range(29):
        for off in (-3600_000, 0, 3600_000):                       # same local hour ±1, every day
            seq += 1
            rec, raw = measurement(t_ms=t0 + day * 86400_000 + off, dl=rng.gauss(930, 5), seq=seq)
            items.append(item("measurement", rec, raw))
    rec, raw = measurement(t_ms=t0 + 29 * 86400_000, dl=200.0, seq=1000)
    items.append(item("measurement", rec, raw))
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", items, False)
        d = c.execute("SELECT anomaly, anomaly_z FROM derived_measurement WHERE uuid = ?", (rec["uuid"],)).fetchone()
    assert d["anomaly"] == 1 and d["anomaly_z"] < -10


def test_rankpool_matches_direct_mann_whitney():
    rng = random.Random(5)
    vals = [round(rng.gauss(900, 40)) for _ in range(500)]          # rounded: plenty of ties
    pool = analytics.RankPool(vals)
    idx = list(range(0, 500, 7))
    inside = [vals[i] for i in idx]
    outside = [v for i, v in enumerate(vals) if i % 7]
    a = pool.test(idx, "less")
    b = analytics.mann_whitney(inside, outside, "less")
    assert abs(a["z"] - b["z"]) < 1e-9 and abs(a["p"] - b["p"]) < 1e-12
    import statistics
    assert pool.median_excluding(sorted(inside)) == statistics.median(outside)
    odd = [v for i, v in enumerate(vals) if i % 7 or i == 0]
    assert pool.median_excluding(sorted(inside[1:])) == statistics.median(odd)


def test_each_test_is_judged_against_the_package_in_force(db):
    """M125 (132 Mbps) until an upgrade date, Gig1 from then: 135 Mbps is on target before, severe after."""
    from helpers import item, measurement
    m125 = {"from": "2000-01-01", "name": "M125", "advertised_down": 132, "advertised_up": 20, "target_down": 120,
            "degraded_down": 66, "severe_down": 26, "target_up": 18, "degraded_up": 10, "tiers": [130, 120, 100]}
    gig1 = dict(settings_store.GIG1, **{"from": "2026-10-01"})
    with db.tx() as c:
        settings_store.save(c, settings_store.validate({"packages": [m125, gig1]}, settings_store.get_all(c)))
        before = 1_790_640_000_000                     # 29 Sep 2026
        after = 1_790_900_000_000                      # 2 Oct 2026
        items = [item("measurement", *measurement(t_ms=before + i * 1_800_000, dl=135.0, seq=i + 1)) for i in range(10)]
        items += [item("measurement", *measurement(t_ms=after + i * 1_800_000, dl=135.0, seq=100 + i)) for i in range(10)]
        ingest.ingest_batch(c, "pi", items, False)
        s = settings_store.get_all(c)
        tests = analytics.load_tests(c, before - 1, after + 86_400_000, s)
    assert {x.pk["name"] for x in tests} == {"M125", "Virgin Media Gig1 Fibre Broadband"}
    sm = analytics.summary(tests, s)
    assert sm["pct_target"] == 50.0 and sm["pct_severe"] == 50.0
    assert [p["name"] for p in sm["packages"]] == ["M125", "Virgin Media Gig1 Fibre Broadband"]
    assert sm["packages"][0]["pct_target"] == 100.0 and sm["packages"][1]["pct_target"] == 0.0
    assert analytics.streaks(tests, s)["longest_good"]["count"] == 10
    from speedmon import series
    with db.read() as c:
        raw = series.tests(c, before - 1, after + 86_400_000, 1200, False, s)
    bands = {p[0] < 1_790_809_200_000: p[-1] for p in raw["points"]}   # 1 Oct 2026 00:00 BST
    assert bands == {True: "target", False: "severe"} and len(raw["packages"]) == 2


def test_guarantee_watch_counts_consecutive_days_below_the_minimum():
    day = 86_400_000
    t0 = 1_790_000_000_000
    gig = dict(settings_store.GIG1)
    def mk(d, dl, ok=True):
        x = T(t0 + d * day, dl, ok=ok)
        x.pk = gig
        return x
    tests = [mk(0, 900), mk(1, 400), mk(1, 900), mk(2, 500), mk(3, 300), mk(4, 950),       # days 1-3 below: qualifies
             mk(6, 400), mk(7, None, ok=False), mk(8, 400)]                                  # day 5 missing, day 7 only a failure
    g = analytics.guarantee_watch(tests)
    assert g["applies"] and g["mgds"] == 565
    assert g["qualifying"] == [{"days": 3, "first": tests[1].day, "last": tests[4].day, "tests_below": 3, "lowest": 300}]
    assert g["days_below"] == 5 and g["current"]["days"] == 1        # a missing day and a failure-only day break runs
