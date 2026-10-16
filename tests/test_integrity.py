import sqlite3

import pytest

from speedmon import canonical, ingest, integrity
from helpers import item, measurement, probe


def test_canonical_modules_identical():
    from pathlib import Path
    a = (Path(__file__).parent.parent / "speedmon" / "canonical.py").read_text().splitlines()[1:]
    b = (Path(__file__).parent.parent / "speedagent" / "canonical.py").read_text().splitlines()[1:]
    assert a == b


def test_hash_survives_int_float_and_empty_text():
    rec, raw = measurement(dl=900)                       # int for a REAL field
    rec["failure_detail"] = ""
    n = canonical.normalise("measurement", rec)
    assert n["download_mbps"] == 900.0 and isinstance(n["download_mbps"], float) and n["failure_detail"] is None
    assert canonical.record_hash("measurement", rec) == canonical.record_hash("measurement", n)


def test_ingest_idempotent_and_chained(db):
    rec, raw = measurement(seq=1)
    with db.tx() as c:
        r1 = ingest.ingest_batch(c, "pi", [item("measurement", rec, raw)], False)
        r2 = ingest.ingest_batch(c, "pi", [item("measurement", rec, raw)], False)
    assert r1["accepted"] == [rec["uuid"]] and r2["duplicates"] == [rec["uuid"]]
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM measurements").fetchone()[0] == 1
        v = integrity.verify(c)
    assert v["ok"] and v["ledger_entries"] == 1


def test_rejections_are_quarantined_not_dropped(db):
    rec, raw = measurement(seq=1)
    bad_hash = item("measurement", rec, raw)
    bad_hash["record_hash"] = "0" * 64
    rec2, raw2 = measurement(seq=2)
    tampered_raw = item("measurement", rec2, raw2)
    tampered_raw["raw"] = '{"x": 2}'
    rec3, raw3 = measurement(seq=3, source="synthetic")
    other_agent, raw4 = measurement(seq=4, agent_id="someone-else")
    with db.tx() as c:
        r = ingest.ingest_batch(c, "pi", [bad_hash, tampered_raw, item("measurement", rec3, raw3), item("measurement", other_agent, raw4)],
                                allow_synthetic=False)
    assert not r["accepted"] and len(r["rejected"]) == 4
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 4
        assert c.execute("SELECT count(*) FROM measurements").fetchone()[0] == 0


def test_uuid_reuse_with_different_content_rejected(db):
    rec, raw = measurement(seq=1)
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("measurement", rec, raw)], False)
        rec2 = dict(rec, download_mbps=10.0)
        r = ingest.ingest_batch(c, "pi", [item("measurement", rec2, raw)], False)
    assert r["rejected"] and "different content" in r["rejected"][0]["error"]


def test_raw_tables_refuse_update_and_delete(db):
    rec, raw = measurement(seq=1)
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("measurement", rec, raw)], False)
    for sql in ("UPDATE measurements SET download_mbps = 1", "DELETE FROM measurements", "UPDATE ledger SET chain_hash = 'x'",
                "DELETE FROM ledger"):
        with pytest.raises(sqlite3.IntegrityError):
            with db.tx() as c:
                c.execute(sql)


def test_verify_detects_tampering(db):
    items = []
    for i in range(5):
        rec, raw = measurement(seq=i + 1, t_ms=1_790_000_000_000 + i * 60_000)
        items.append(item("measurement", rec, raw))
    items.append(item("probe", probe(1_790_000_000_000, seq=10)))
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", items, False)
    with db.read() as c:
        assert integrity.verify(c)["ok"]
    with db.tx() as c:                                  # someone with raw database access edits history
        c.execute("DROP TRIGGER measurements_append_only_u")
        c.execute("UPDATE measurements SET download_mbps = 999 WHERE agent_seq = 3")
    with db.read() as c:
        v = integrity.verify(c)
    assert not v["ok"]
    assert any("does not match its stored hash" in p for p in v["problems"])
    assert any("append-only protection removed" in p for p in v["problems"])


def test_sequence_gaps_reported(db):
    with db.tx() as c:
        ingest.ingest_batch(c, "pi", [item("probe", probe(1_790_000_000_000 + i * 60_000, seq=s)) for i, s in enumerate((1, 2, 5))], False)
        gaps = integrity.sequence_gaps(c)
    assert gaps[0]["missing"] == 2 and gaps[0]["gaps"] == [[3, 4]]
