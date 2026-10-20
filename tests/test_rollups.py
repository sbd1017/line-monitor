"""Probe roll-ups and the test store: long periods are cheap, and every figure is exactly what direct reading gives."""
import random
import uuid as uuidlib
from fractions import Fraction

from speedmon import analytics, ingest, rollups, settings_store, timeutil
from speedmon.db import MIGRATIONS
from speedmon.exports import jobs
from helpers import item, measurement, probe

DAY = timeutil.DAY_MS
T0 = 1_790_000_000_000 - 9 * DAY


def reference_latency(c, a, b):
    """The Evidence latency block computed the original way: every probe read and sorted. The loss average is the exact
    one (SQLite's avg() before 3.43, as on the Pi, can differ in the last binary digit)."""
    rows = c.execute("SELECT ext_rtt_ms, gw_rtt_ms FROM probes WHERE t_ms >= ? AND t_ms < ?", (a, b)).fetchall()
    ext = sorted(r[0] for r in rows if r[0] is not None)
    gw = sorted(r[1] for r in rows if r[1] is not None)
    row = c.execute("SELECT count(*), avg(ext_loss_pct), max(ext_loss_pct), sum(ext_loss_pct > 0), "
                    "sum(state IN ('wan_down','dns_fail')), sum(state IN ('lan_fault','link_down')), "
                    "sum(dns_ok = 0) FROM probes WHERE t_ms >= ? AND t_ms < ?", (a, b)).fetchone()
    vals = [r[0] for r in c.execute("SELECT ext_loss_pct FROM probes WHERE t_ms >= ? AND t_ms < ? AND ext_loss_pct IS NOT NULL",
                                    (a, b))]
    loss_avg = float(sum(map(Fraction, vals), Fraction(0))) / len(vals) if vals else None
    pct = analytics.percentile
    return {"n": row[0], "ext_median": pct(ext, 50), "ext_p95": pct(ext, 95) if len(ext) >= 20 else None,
            "ext_min": ext[0] if ext else None, "ext_max": ext[-1] if ext else None,
            "gw_median": pct(gw, 50), "gw_p95": pct(gw, 95) if len(gw) >= 20 else None,
            "loss_avg": loss_avg, "loss_max": row[2], "probes_with_loss": row[3], "probes_outage": row[4] or 0,
            "probes_local": row[5] or 0, "dns_failures": row[6] or 0}


def messy_probes(n_days=9, seed=5):
    """Real-world shapes: full-precision RTTs (median of pings), values on band edges, fractional loss from 3 pings to
    2 anchors, missing values, every state, long intervals after a gap."""
    rng = random.Random(seed)
    edges = [2.0 ** e for e in range(-8, 15)] + [2.0 ** e * 1.5 for e in range(-8, 15)] + [0.0, 1e-6, 30000.0]
    out, seq, t = [], 0, T0 + 1234
    while t < T0 + n_days * DAY:
        seq += 1
        st = rng.choices(["ok", "degraded", "wan_down", "dns_fail", "lan_fault", "link_down"], [80, 8, 4, 2, 3, 3])[0]
        p = probe(t, "ok", seq=seq, ext=rng.choice(edges) if rng.random() < 0.05 else abs(rng.gauss(11, 3)) + rng.random() * 1e-9,
                  interval=rng.choice([60, 60, 60, 61, 900]))
        p["state"] = st
        p["gw_rtt_ms"] = None if st in ("lan_fault", "link_down") else (rng.choice(edges) if rng.random() < 0.05 else abs(rng.gauss(0.5, 0.08)))
        p["ext_loss_pct"] = None if rng.random() < 0.03 else rng.choice([0.0] * 20 + [100 * k / 6 for k in range(1, 7)])
        if st == "wan_down":
            p["ext_rtt_ms"] = None
        p["dns_ok"] = rng.choice([None, 0, 1, 1, 1])
        out.append(p)
        t += rng.choice([60_000, 60_000, 59_999, 61_000, 3_600_000 if rng.random() < 0.002 else 60_000])
    return out


def ranges(rng, n=60):
    yield T0, T0 + 9 * DAY
    yield 0, T0 + 100 * DAY                                         # "All": starts long before the first probe
    yield T0 // DAY * DAY + DAY, T0 // DAY * DAY + 4 * DAY          # exact day boundaries
    yield T0 + 3 * DAY, T0 + 3 * DAY + 3600_000                     # under a day: no whole day inside
    yield T0 + 50 * DAY, T0 + 60 * DAY                              # no probes at all
    for _ in range(n):
        a = T0 + rng.randrange(-DAY, 9 * DAY)
        yield a, a + rng.randrange(1, 10 * DAY)


def test_rollups_give_exactly_the_direct_figures(db):
    ps = messy_probes()
    with db.tx() as c:
        for i in range(0, len(ps), 500):                             # incremental, as the agent delivers them
            ingest.ingest_batch(c, "pi", [item("probe", p) for p in ps[i:i + 500]], False)
    rng = random.Random(9)
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM rollup_probe_loss").fetchone()[0] > 0
        for a, b in ranges(rng):
            got, want = jobs.probe_latency(c, a, b), reference_latency(c, a, b)
            assert repr(got.pop("loss_avg")) == repr(want.pop("loss_avg")), (a, b)
            assert got == want, (a, b)
            raw = c.execute("SELECT coalesce(sum(min(interval_s, 600)), 0) FROM probes WHERE t_ms >= ? AND t_ms < ?", (a, b)).fetchone()[0]
            assert rollups.watched_seconds(c, a, b) == raw


def test_rebuild_incremental_and_migration_agree(db):
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("probe", p) for p in messy_probes(4, seed=2)], False)
    dump = lambda c: {t: c.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall() for t in rollups.TABLES}
    with db.tx() as c:
        incremental = dump(c)
        ingest.rebuild_derived(c)
        assert dump(c) == incremental
        for t in rollups.TABLES:
            c.execute(f"DELETE FROM {t}")
        sql = (MIGRATIONS / "0004_probe_daily_rollups.sql").read_text()
        for stmt in rollups.statements("1"):                         # the migration fills existing databases the same way
            assert stmt + ";" in sql
            c.execute(stmt)
        assert dump(c) == incremental


def test_value_bands_only_go_up_and_cover_their_values(db):
    vals = sorted({0.0, 1e-9, 2.0 ** -7, 2.0 ** -6 - 1e-12, 2.0 ** -6, 0.4999999, 0.5, 0.75, 1.0, 9.99, 10.0, 11.0001,
                   16383.9, 16384.0, 40000.0, 1e7} | {random.Random(1).uniform(0, 100) for _ in range(300)})
    with db.read() as c:
        bands = [c.execute(f"SELECT {rollups.bucket_sql('?1')}", (v,)).fetchone()[0] for v in vals]
    assert bands == sorted(bands)
    for v, bk in zip(vals, bands):
        lo, hi = rollups.bucket_range(bk)
        assert lo <= v < hi


def test_stale_rollups_fall_back_to_the_probes(db, caplog):
    ps = messy_probes(5, seed=3)
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("probe", p) for p in ps], False)
        c.execute("DELETE FROM rollup_probe_rtt WHERE (metric, day_ms, bucket) IN (SELECT metric, day_ms, bucket FROM "
                  "rollup_probe_rtt WHERE metric = 0 ORDER BY n DESC LIMIT 1)")          # derived, so this is allowed
    with db.read() as c:
        assert jobs.probe_latency(c, T0, T0 + 5 * DAY) == reference_latency(c, T0, T0 + 5 * DAY)
    assert "out of step" in caplog.text


# ---------------------------------------------------------------- the test store

def reference_tests(c, a, b, s):
    """load_tests as it was: one query per call, every row re-assessed."""
    from speedmon import quality
    sources = ("agent", "synthetic", "legacy_csv", "legacy_xlsx") if s.get("include_legacy") else ("agent", "synthetic")
    excluded = {r[0] for r in c.execute("SELECT measurement_uuid FROM measurement_flags WHERE flag = 'exclude' AND revoked_at IS NULL")}
    clock, pkc = timeutil.LocalClock(s["timezone"]), settings_store.PackageClock(s.get("packages"), s["timezone"])
    out, seen = [], set()
    q = (f"SELECT m.t_ms, m.uuid, m.status, m.download_mbps, m.upload_mbps, m.ping_ms, m.jitter_ms, m.packet_loss_pct, "
         f"m.kind, m.source, m.failure_code, m.server_id, m.related_uuid, {', '.join(quality.SQL_COLUMNS)} FROM measurements m "
         f"LEFT JOIN diagnostics d ON d.measurement_uuid = m.uuid WHERE m.t_ms >= ? AND m.t_ms < ? AND m.source IN "
         f"({','.join('?' * len(sources))}) ORDER BY m.t_ms, m.id")
    for r in c.execute(q, (a, b, *sources)):
        if r[1] in seen:
            continue
        seen.add(r[1])
        if r[1] in excluded:
            continue
        day, hour, dow, minute = clock.parts(r[0])
        pk = pkc.at(r[0])
        out.append((r[0], r[1], r[3], day, hour, dow, minute, pk["name"], quality.assess(r[2] == "ok", r[9], r[8], r[3], pk, r[13:])))
    return out


def flat(tests):
    return [(x.t, x.uuid, x.dl, x.day, x.hour, x.dow, x.minute, x.pk["name"], x.q) for x in tests]


def diag(rec, seq, **kw):
    return item("diagnostic", {"uuid": str(uuidlib.uuid4()), "measurement_uuid": rec["uuid"], "agent_id": "pi", "agent_seq": seq,
                               "t_ms": rec["t_ms"], "link_speed_mbps": 1000, "cpu_load1": 0.2, "throttled": "0x0", **kw})


def test_test_store_follows_every_change(db, monkeypatch):
    loads = []
    real = analytics._load_rows
    monkeypatch.setattr(analytics, "_load_rows", lambda c, where, *a: loads.append(where.split(" ")[1] if where else "all") or real(c, where, *a))
    t0 = timeutil.now_ms() - 20 * DAY
    seq = iter(range(1, 10_000))
    recs = [measurement(t_ms=t0 + i * 1800_000, seq=next(seq), dl=900.0 - i, link_speed_mbps=1000) for i in range(200)]

    def check():
        with db.read() as c:
            s = settings_store.get_all(c)
            for a, b in ((0, t0 + 100 * DAY), (t0 + DAY, t0 + 2 * DAY), (t0 + 5 * DAY + 7, t0 + 30 * DAY)):
                assert flat(analytics.load_tests(c, a, b, s)) == reference_tests(c, a, b, s)

    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("measurement", r, raw) for r, raw in recs[:100]] +
                            [diag(r, next(seq)) for r, _ in recs[:90]], False)
    check()
    # new tests, then their diagnostics in a later batch (and a busy-Pi diagnostics row for an older test)
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("measurement", r, raw) for r, raw in recs[100:150]], False)
    check()
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [diag(r, next(seq)) for r, _ in recs[100:150]] + [diag(recs[95][0], next(seq), pretest_rx_mbps=900.0)], False)
    check()
    assert loads == ["all", "m.id", "m.id", "m.uuid"]              # new rows only, after the first full load
    # a second diagnostics row for a test that already has one: the first stays in use
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [diag(recs[10][0], next(seq), cpu_load1=9.0)], False)
    check()
    # an older test delivered late (out of time order)
    late, raw = measurement(t_ms=t0 + 3 * 1800_000 + 5, seq=next(seq), dl=10.0)
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("measurement", late, raw)], False)
    check()
    # owner flags, a revoked flag, a package change, a time zone change
    with db.tx() as c:
        for r, _ in recs[20:23]:
            c.execute("INSERT INTO measurement_flags (measurement_uuid, flag, reason, created_at) VALUES (?, 'exclude', 'x', 'now')", (r["uuid"],))
    check()
    with db.tx() as c:
        c.execute("UPDATE measurement_flags SET revoked_at = 'now' WHERE id = (SELECT min(id) FROM measurement_flags)")
    check()
    with db.tx() as c:
        settings_store.save(c, {"packages": [{**settings_store.GIG1, "from": "2000-01-01", "name": "Old"},
                                             {**settings_store.GIG1, "from": timeutil.local(t0 + 4 * DAY, "Europe/London").date().isoformat(),
                                              "name": "New"}]}, "test")
    check()
    with db.tx() as c:
        settings_store.save(c, {"timezone": "America/New_York"}, "test")
    check()
    # inside a write transaction the store is bypassed (uncommitted rows must not be shared)
    extra, raw = measurement(t_ms=t0 + 9 * DAY + 1, seq=next(seq))
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("measurement", extra, raw)], False)
        s = settings_store.get_all(c)
        assert extra["uuid"] in {x.uuid for x in analytics.load_tests(c, 0, t0 + 100 * DAY, s)}
        c.execute("ROLLBACK")
        c.execute("BEGIN")
    check()


def test_test_store_notices_a_new_database_at_the_same_path(settings):
    from speedmon.db import Database
    t0 = timeutil.now_ms() - DAY
    for dl in (900.0, 100.0):
        if settings.db_path.exists():
            for p in settings.db_path.parent.glob(settings.db_path.name + "*"):
                p.unlink()
        db = Database(settings.db_path)
        db.migrate()
        rec, raw = measurement(t_ms=t0, seq=1, dl=dl)
        with db.tx() as c:
            ingest.ingest_batch(c, "pi", [item("measurement", rec, raw)], False)
        with db.read() as c:
            s = settings_store.get_all(c)
            assert [x.dl for x in analytics.load_tests(c, 0, t0 + DAY, s)] == [dl]


def test_percentile_of_is_the_percentile_rule():
    import math

    def excel(v, p):                                                 # PERCENTILE.INC as it was written before
        n = len(v)
        if n == 0:
            return None
        if n == 1:
            return v[0]
        k = (n - 1) * p / 100
        lo = math.floor(k)
        hi = min(lo + 1, n - 1)
        return v[lo] + (v[hi] - v[lo]) * (k - lo)
    rng = random.Random(4)
    for n in (0, 1, 2, 3, 19, 20, 101, 1000):
        v = sorted(rng.uniform(0, 50) for _ in range(n))
        for p in analytics.PCTS:
            assert analytics.percentile_of(n, p, v.__getitem__) == analytics.percentile(v, p) == excel(v, p)
