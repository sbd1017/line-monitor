from speedmon import analytics, ingest, quality, settings_store, timeutil
from helpers import item, measurement

M125 = {"advertised_down": 132.0, "target_down": 120.0, "minimum_guaranteed_down": None}
GIG1 = {"advertised_down": 1130.0, "target_down": 900.0, "minimum_guaranteed_down": 565.0}


def diag(link=1000, bg=0.5, pre=0.2, load=0.3, temp=45.0, thr="0x0", err=0, drop=0):
    # order of quality.SQL_COLUMNS: m.link, d.link, d.uuid, bg rx/tx, pre rx/tx, load1, temp, throttled, errors rx/tx, drops rx/tx
    return (link, link, "d-uuid", bg, bg, pre, pre, load, temp, thr, err, 0, drop, 0)


def test_clean_and_each_flag():
    a = lambda dl=135.0, pk=M125, d=None, kind="scheduled", ok=True, source="agent": quality.assess(ok, source, kind, dl, pk, d if d is not None else diag())
    assert a() == () and quality.is_clean(a())
    assert a(d=diag(pre=141.0)) == ("pi_busy",)                         # the real case: a download running on the Pi
    assert a(d=diag(bg=5.0)) == () and a(d=diag(bg=8.0)) == ("pi_busy",)   # M125: 5% of 132 = 6.6 Mbps
    assert a(d=diag(pre=18.1)) == ("pi_busy",)                           # 17 Oct 16:05: a football stream before the test
    assert a(dl=850.0, pk=GIG1, d=diag(bg=40.0)) == ()                   # Gig1: 5% of 1,130 = 56.5, a stream barely matters
    assert quality.busy_limit({"advertised_down": 50.0}) == 5.0          # never below 5 Mbps
    assert a(d=diag(load=3.5)) == ("pi_strained",) and a(d=diag(temp=81)) == ("pi_strained",)
    assert a(d=diag(thr="0x50000")) == () and a(d=diag(thr="0x4")) == ("pi_strained",)   # history bits only vs throttled now
    assert a(d=diag(err=2)) == ("link_errors",) and a(d=diag(drop=3)) == ()
    assert a(d=diag(link=100), dl=94.0) == ("link_slow",)                # 100 Mb/s cable under a 120 Mbps target
    assert a(dl=935.0, pk=GIG1) == ("at_link_limit",) and quality.is_clean(("at_link_limit",))
    assert a(dl=700.0, pk=GIG1) == ()
    assert a(kind="fallback") == ("fallback",) and quality.is_clean(("fallback",))
    assert a(d=(1000,) + (None,) * 13) == ("no_diagnostics",) and not quality.is_clean(("no_diagnostics",))
    assert a(ok=False) == () and a(source="legacy_csv") == ()


def test_capacity_levels():
    assert quality.capacity(1000, M125)["level"] == "ok"
    c = quality.capacity(1000, GIG1)
    assert c["level"] == "advertised" and "900" in c["text"] and "565" in c["text"] and "1,130" in c["text"]
    assert quality.capacity(2500, GIG1)["level"] == "ok"
    assert quality.capacity(100, M125)["level"] == "target"
    assert quality.capacity(100, GIG1)["level"] == "guarantee" and "CAPACITY LIMITED" in quality.capacity(100, GIG1)["text"]
    assert quality.capacity(None, GIG1)["level"] == "unknown"


def test_summary_reports_clean_only_figures(db):
    import uuid
    t0 = timeutil.now_ms() - 6 * 3600_000
    items = []
    for i, (dl, pre) in enumerate([(130.0, 0.1), (131.0, 0.2), (60.0, 150.0)]):     # the slow one ran while the Pi was busy
        rec, raw = measurement(t_ms=t0 + i * 1800_000, seq=2 * i + 1, dl=dl, link_speed_mbps=1000)
        d = {"uuid": str(uuid.uuid4()), "measurement_uuid": rec["uuid"], "agent_id": "pi", "agent_seq": 2 * i + 2,
             "t_ms": rec["t_ms"], "link_speed_mbps": 1000, "pretest_rx_mbps": pre, "cpu_load1": 0.2, "throttled": "0x0"}
        items += [item("measurement", rec, raw), item("diagnostic", d)]
    with db.tx() as c:
        settings_store.save(c, {"packages": [{**settings_store.GIG1, "from": "2000-01-01", "name": "Virgin Media M125",
                                              "advertised_down": 132.0, "advertised_up": 20.0, "minimum_guaranteed_down": None,
                                              "target_down": 120.0, "degraded_down": 66.0, "severe_down": 26.0,
                                              "target_up": 18.0, "degraded_up": 10.0, "tiers": [130, 120, 100]}]}, "test")
        assert len(ingest.ingest_batch(c, "pi", items, False)["accepted"]) == 6
        s = settings_store.get_all(c)
        sm = analytics.summary(analytics.load_tests(c, t0 - 1, timeutil.now_ms(), s), s)
    q = sm["quality"]
    assert q["n_ok"] == 3 and q["n_clean"] == 2 and q["flags"] == {"pi_busy": 1}
    assert sm["pct_degraded"] > 30 and q["clean"]["pct_degraded"] == 0 and q["clean"]["pct_target"] == 100


def test_explorer_filter_and_detail_show_quality(signed_in):
    import uuid
    from helpers import signed
    c = signed_in
    items = []
    for i, pre in enumerate([0.1, 160.0]):
        rec, raw = measurement(seq=2 * i + 1, dl=130.0, link_speed_mbps=1000, t_ms=timeutil.now_ms() - (i + 1) * 600_000)
        d = {"uuid": str(uuid.uuid4()), "measurement_uuid": rec["uuid"], "agent_id": "pi", "agent_seq": 2 * i + 2,
             "t_ms": rec["t_ms"], "link_speed_mbps": 1000, "pretest_rx_mbps": pre, "cpu_load1": 0.1, "throttled": "0x0"}
        items += [item("measurement", rec, raw), item("diagnostic", d)]
        if pre > 100:
            busy = rec["uuid"]
    assert signed(c, "POST", "/speedtest/agent/v1/records", {"items": items}).status_code == 200
    all_ = c.get("/speedtest/api/measurements?range=all").json()
    assert {m["uuid"]: m["quality"] for m in all_["items"]}[busy] == ["pi_busy"] and all_["quality_labels"]["pi_busy"]["label"] == "Pi busy"
    flagged = c.get("/speedtest/api/measurements?range=all&filter=quality_flagged").json()
    clean = c.get("/speedtest/api/measurements?range=all&filter=clean").json()
    assert [m["uuid"] for m in flagged["items"]] == [busy] and flagged["total"] == 1
    assert clean["total"] == 1 and busy not in [m["uuid"] for m in clean["items"]]
    det = c.get(f"/speedtest/api/measurement/{busy}").json()["quality"]
    assert det["assessed"] and not det["clean"] and det["labels"][0]["code"] == "pi_busy" and det["capacity"]["level"] == "advertised"   # Gig1 (the default) on 1 Gb/s
    live = c.get("/speedtest/api/live").json()
    assert live["capacity"]["level"] in ("ok", "advertised") and "quality" in live["last"]


def test_agent_gets_an_automatic_lull_threshold():
    from speedmon import settings_store
    s = {k: 0 for k in settings_store.AGENT_KEYS}
    s.update(quiet_threshold_mbps=None, package={"advertised_down": 132.0})
    assert abs(settings_store.agent_config(s)["quiet_threshold_mbps"] - 6.6) < 1e-9
    s.update(package={"advertised_down": 1130.0})
    assert settings_store.agent_config(s)["quiet_threshold_mbps"] == 56.5
    s.update(quiet_threshold_mbps=25.0)                                  # a fixed value in Settings wins
    assert settings_store.agent_config(s)["quiet_threshold_mbps"] == 25.0
