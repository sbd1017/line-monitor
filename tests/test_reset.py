import sqlite3

from speedmon import canonical, ingest, integrity, reset, settings_store, timeutil
from helpers import item, measurement, probe


def test_reset_archives_verified_copy_and_starts_fresh(db, settings):
    t = timeutil.now_ms() - 3_600_000
    with db.tx() as c:
        settings_store.save(c, settings_store.validate({"interval_min": 20}, settings_store.get_all(c)))
        ingest.ingest_batch(c, "pi", [item("measurement", *measurement(t_ms=t, seq=1)), item("probe", probe(t, seq=2))], False)
        old_head = integrity.head(c)["head"]
    info = reset.archive_and_reset(settings, "test")
    archive = settings.data_dir / info["archive"]
    assert archive.exists() and info["ledger_head"] == old_head and info["counts"]["measurements"] == 1
    a = sqlite3.connect(f"file:{archive}?mode=ro", uri=True)
    a.row_factory = sqlite3.Row
    assert integrity.verify(a)["ok"] and a.execute("SELECT count(*) FROM measurements").fetchone()[0] == 1
    assert list((archive.parent / "live-original").glob("speedmon.sqlite3*"))       # original moved, not deleted
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM measurements").fetchone()[0] == 0
        assert integrity.head(c)["head"] == canonical.GENESIS
        s = settings_store.get_all(c)
        assert s["interval_min"] == 20 and s["epoch"]["archive_sha256"] == info["archive_sha256"]
    with db.tx() as c:                                  # a straggler from before the reset is kept aside, a new one accepted
        old = ingest.ingest_batch(c, "pi", [item("measurement", *measurement(t_ms=t, seq=3))], False)
        new = ingest.ingest_batch(c, "pi", [item("measurement", *measurement(t_ms=timeutil.now_ms() + 1000, seq=4))], False)
        assert old["rejected"] and "archived period" in old["rejected"][0]["error"]
        assert new["accepted"] and c.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 1


def test_rebuild_without_legacy_keeps_everything_else_and_verifies(db, settings):
    import uuid
    t = timeutil.now_ms() - 3_600_000
    keep, raw = measurement(t_ms=t, seq=1)
    with db.tx() as c:
        settings_store.save(c, settings_store.validate({"interval_min": 20}, settings_store.get_all(c)))
        for n in range(3):                                  # imported rows (with diagnostics) before the agent's
            rec, r = measurement(t_ms=t - (n + 1) * 86_400_000, source="legacy_csv", agent_id="legacy", agent_seq=None)
            ingest.insert_record(c, "measurement", canonical.normalise("measurement", rec), canonical.record_hash("measurement", canonical.normalise("measurement", rec)), r)
            d = canonical.normalise("diagnostic", {"uuid": str(uuid.uuid4()), "measurement_uuid": rec["uuid"], "agent_id": "legacy", "t_ms": rec["t_ms"]})
            ingest.insert_record(c, "diagnostic", d, canonical.record_hash("diagnostic", d))
        ingest.ingest_batch(c, "pi", [item("measurement", keep, raw), item("probe", probe(t, seq=2))], False)
        c.execute("INSERT INTO measurement_flags (measurement_uuid, flag, reason, created_at) VALUES (?, 'note', 'kept', ?)",
                  (keep["uuid"], timeutil.now_iso()))
        before = integrity.head(c)["head"]
    info = reset.rebuild_without(settings, ("legacy_csv", "legacy_xlsx"), "test")
    assert info["verified"] and info["removed"]["measurements"] == 3 and info["removed"]["diagnostics"] == 3
    assert info["ledger_head"] == before and info["new_head"] != before
    with db.read() as c:
        assert [r[0] for r in c.execute("SELECT source FROM measurements")] == ["agent"]
        assert c.execute("SELECT count(*) FROM diagnostics").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM probes").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM measurement_flags").fetchone()[0] == 1
        assert c.execute("SELECT raw FROM measurements").fetchone()[0] == raw
        s = settings_store.get_all(c)
        rebuilds = __import__("json").loads(c.execute("SELECT value FROM settings WHERE key = 'rebuilds'").fetchone()[0])
        assert s["interval_min"] == 20 and rebuilds[0]["archive_sha256"] == info["archive_sha256"] and not s["include_legacy"]
        assert c.execute("SELECT count(*) FROM audit_log WHERE action = 'rebuild_without'").fetchone()[0] == 1
        a = sqlite3.connect(f"file:{settings.data_dir / info['archive']}?mode=ro", uri=True)
        a.row_factory = sqlite3.Row
        assert integrity.verify(a)["ok"] and a.execute("SELECT count(*) FROM measurements").fetchone()[0] == 4
    with db.tx() as c:                                      # the chain carries on from the new head
        assert ingest.ingest_batch(c, "pi", [item("measurement", *measurement(seq=3))], False)["accepted"]
        assert integrity.verify(c)["ok"]
