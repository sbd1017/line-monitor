"""Ledger anchoring: statements, orchestration, duplicate guard, failure isolation and tamper detection.

A real local RFC 3161 authority (openssl) signs the receipts, so the signature checks are genuine and the
tests stay offline.
"""
from __future__ import annotations

from speedmon import anchors, ingest


def add_entry(db, index_hash="a" * 64):
    with db.tx() as conn:
        ingest.append_ledger(conn, "probe", f"uuid-{index_hash[:4]}", index_hash)


def test_statement_shape_is_fixed():
    assert anchors.statement(7, "b" * 64) == "line-monitor ledger anchor v1\nindex=7\nhead=" + "b" * 64 + "\n"


def test_anchors_the_head_with_every_provider(db, tsa, monkeypatch):
    monkeypatch.setattr(anchors, "PROVIDERS", (("one", "http://one"), ("two", "http://two")))
    add_entry(db)
    res = anchors.anchor_once(db, timeout=30)
    assert [r["status"] for r in res["results"]] == ["ok", "ok"]
    with db.read() as conn:
        rows = conn.execute("SELECT provider, status, head_index, head_hash, token FROM anchors ORDER BY id").fetchall()
    assert [r["provider"] for r in rows] == ["one", "two"]
    assert all(r["status"] == "ok" and r["token"] for r in rows)
    assert rows[0]["head_index"] == 0
    assert anchors.verify_all(db, timeout=30) == {"checked": 2, "problems": 0}


def test_empty_ledger_is_skipped(db):
    assert anchors.anchor_once(db, providers=(("one", "http://one"),))["skipped"].startswith("the ledger is empty")


def test_same_head_is_not_anchored_twice(db, tsa, monkeypatch):
    monkeypatch.setattr(anchors, "PROVIDERS", (("one", "http://one"),))
    add_entry(db)
    anchors.anchor_once(db, timeout=30)
    second = anchors.anchor_once(db, timeout=30)
    assert second["results"] == [{"provider": "one", "status": "skip"}]


def test_a_dead_authority_does_not_stop_the_other(db, tsa, monkeypatch):
    monkeypatch.setattr(anchors, "PROVIDERS", (("broken", "http://broken"), ("one", "http://one")))
    add_entry(db)
    res = anchors.anchor_once(db, timeout=30)
    assert [r["status"] for r in res["results"]] == ["error", "ok"]
    with db.read() as conn:
        rows = {r["provider"]: r for r in conn.execute("SELECT provider, status, token, error FROM anchors")}
    assert rows["broken"]["status"] == "error" and rows["broken"]["token"] is None
    assert "connection refused" in rows["broken"]["error"]
    assert rows["one"]["status"] == "ok"


def test_verify_catches_a_rewritten_chain(db, tsa, monkeypatch):
    monkeypatch.setattr(anchors, "PROVIDERS", (("one", "http://one"),))
    add_entry(db)
    anchors.anchor_once(db, timeout=30)
    with db.read() as conn:                      # the append-only trigger is the thing under test elsewhere
        conn.execute("DROP TRIGGER ledger_append_only_u")
        conn.execute("UPDATE ledger SET chain_hash = ? WHERE chain_index = 0", ("f" * 64,))
    assert anchors.verify_all(db, timeout=30)["problems"] == 1
    with db.read() as conn:
        check = conn.execute("SELECT ok, problem FROM anchor_checks").fetchone()
    assert check["ok"] == 0 and "no longer has the anchored head" in check["problem"]


def test_verify_catches_a_missing_chain_entry(db, tsa, monkeypatch):
    monkeypatch.setattr(anchors, "PROVIDERS", (("one", "http://one"),))
    add_entry(db)
    anchors.anchor_once(db, timeout=30)
    with db.read() as conn:
        conn.execute("DROP TRIGGER ledger_append_only_d")
        conn.execute("DELETE FROM ledger WHERE chain_index = 0")
    assert anchors.verify_all(db, timeout=30)["problems"] == 1
    with db.read() as conn:
        assert "no entry at index 0" in conn.execute("SELECT problem FROM anchor_checks").fetchone()["problem"]


def test_dump_writes_receipts(db, tsa, monkeypatch, tmp_path):
    monkeypatch.setattr(anchors, "PROVIDERS", (("one", "http://one"),))
    add_entry(db)
    anchors.anchor_once(db, timeout=30)
    files = anchors.dump(db, tmp_path / "out")
    assert (tmp_path / "out" / "000001-one.tsr").exists()
    assert (tmp_path / "out" / "000001-one.txt").read_text().startswith("line-monitor ledger anchor v1")


def test_a_junk_reply_is_recorded_not_raised(db, monkeypatch):
    monkeypatch.setattr(anchors, "PROVIDERS", (("junk", "http://junk"),))
    monkeypatch.setattr(anchors, "_post", lambda url, body, timeout: b"not a timestamp")
    add_entry(db)
    res = anchors.anchor_once(db, timeout=30)
    assert res["results"][0]["status"] == "error"
    with db.read() as conn:
        row = conn.execute("SELECT status, token, error FROM anchors").fetchone()
    assert row["status"] == "error" and row["token"] is None and row["error"]


def test_providers_can_be_overridden_by_environment(monkeypatch):
    monkeypatch.setenv("SPEEDMON_TSA", "one=https://a.example/tsr, bad, two=http://b.example")
    assert anchors.providers_from_env() == (("one", "https://a.example/tsr"), ("two", "http://b.example"))
    monkeypatch.setenv("SPEEDMON_TSA", "")
    assert anchors.providers_from_env() == ()


def test_nothing_new_is_not_a_failure(db, tsa, monkeypatch):
    from speedmon import cli
    monkeypatch.setattr(anchors, "PROVIDERS", (("one", "http://one"),))
    add_entry(db)
    assert cli.main(["anchor"]) == 0
    assert cli.main(["anchor"]) == 0            # same head: both providers skip, still a good run


def test_every_authority_failing_is_a_failure(db, monkeypatch):
    from speedmon import cli
    monkeypatch.setattr(anchors, "PROVIDERS", (("broken", "http://broken"),))
    add_entry(db)
    assert cli.main(["anchor"]) == 1
