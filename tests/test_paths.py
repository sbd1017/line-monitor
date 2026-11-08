"""Path traces: parsing, boundary classification, agent plumbing and export masking."""
from __future__ import annotations

import json
import time
import uuid

import pytest

from speedagent import paths
from speedmon import canonical, ingest, timeutil
from tests import helpers


def report(*hops):
    """mtr JSON shape: (hop, host, loss%) triples."""
    return {"report": {"hubs": [{"count": h, "host": host, "Loss%": loss, "Snt": 20, "Avg": 1.0, "Best": 0.9,
                                "Wrst": 2.0} for h, host, loss in hops]}}


def test_hops_flatten_and_cap():
    hops = paths.hops_of(report(*[(n, "???", 100.0) if n > 8 else (n, f"10.0.0.{n}", 0.0) for n in range(1, 13)]))
    assert len(hops) == paths.MAX_HOPS
    assert hops[0]["host"] == "10.0.0.1"
    assert hops[7]["host"] == "10.0.0.8"
    assert paths.hops_of(report(*[(n, "???", 100.0) if n > 2 else (n, f"10.0.0.{n}", 0.0) for n in range(1, 5)]))[2]["host"] is None


def test_loss_past_a_clean_gateway_that_carries_is_the_isp():
    hops = paths.hops_of(report((1, "192.0.2.1", 0.0), (2, "198.51.100.1", 40.0), (3, "203.0.113.9", 40.0)))
    assert paths.classify(hops, 20) == ("isp", 2, 1)


def test_a_middle_hop_that_clears_is_not_blamed_on_anyone():
    hops = paths.hops_of(report((1, "192.0.2.1", 0.0), (2, "198.51.100.1", 40.0), (3, "203.0.113.9", 0.0)))
    assert paths.classify(hops, 20) == ("unknown", 2, 0)


def test_loss_at_the_gateway_is_the_house():
    hops = paths.hops_of(report((1, "192.0.2.1", 60.0), (2, "198.51.100.1", 60.0)))
    assert paths.classify(hops, 20) == ("lan", None, 0)


def test_a_clean_path_is_none():
    hops = paths.hops_of(report((1, "192.0.2.1", 0.0), (2, "198.51.100.1", 0.0)))
    assert paths.classify(hops, 20) == ("none", None, 0)


def test_too_few_cycles_is_unknown():
    hops = paths.hops_of(report((1, "192.0.2.1", 0.0), (2, "198.51.100.1", 90.0)))
    assert paths.classify(hops, 3) == ("unknown", None, 0)


def test_silent_hops_are_skipped_when_judging():
    hops = paths.hops_of(report((1, "192.0.2.1", 0.0), (2, "???", 100.0), (3, "203.0.113.9", 50.0)))
    assert paths.classify(hops, 20) == ("isp", 3, 1)


def test_measure_records_where_the_loss_starts(monkeypatch):
    monkeypatch.setattr(paths, "_run", lambda *a, **k: report((1, "192.0.2.1", 0.0), (2, "198.51.100.1", 30.0),
                                                               (3, "203.0.113.9", 30.0)))
    got = paths.measure(cycles=20)
    assert got["boundary"] == "isp" and got["first_bad_hop"] == 2 and got["sustained"] == 1
    assert got["gw_rtt_ms"] == 1.0 and got["hop_count"] == 3 and len(json.loads(got["hops"])) == 3
    assert got["target"] == paths.ANCHOR and got["error"] is None


def test_measure_raises_when_mtr_is_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("mtr: not found")
    monkeypatch.setattr(paths, "_run", boom)
    with pytest.raises(OSError):
        paths.measure()


# ---------------------------------------------------------------- backend

def path_rec(t_ms=None, boundary="isp", hops=None, **kw):
    t = t_ms or timeutil.now_ms() - 30_000
    rec = {"uuid": str(uuid.uuid4()), "agent_id": "pi", "agent_seq": None, "t_ms": t, "at": timeutil.iso(t),
           "target": "198.51.100.1", "target_source": "anchor", "hop_count": 2, "boundary": boundary,
           "first_bad_hop": 2 if boundary == "isp" else None, "sustained": int(boundary == "isp"), "gw_loss_pct": 0.0,
           "gw_rtt_ms": 0.6, "cycles": 20, "duration_ms": 21_000, "engine": "mtr", "engine_version": None,
           "iface": "eth0", "link_speed_mbps": 2500,
           "hops": json.dumps(hops if hops is not None else [{"hop": 1, "host": "192.0.2.1", "loss_pct": 0.0,
                                                              "sent": 20, "avg_ms": 0.6, "best_ms": 0.5,
                                                              "worst_ms": 1.0},
                                                             {"hop": 2, "host": "198.51.100.1", "loss_pct": 40.0,
                                                              "sent": 20, "avg_ms": 9.0, "best_ms": 8.0,
                                                              "worst_ms": 30.0}], separators=(",", ":"))}
    rec.update(kw)
    return rec


def test_a_path_record_is_accepted_and_chained(db):
    rec = path_rec()
    with db.tx() as conn:
        res = ingest.ingest_batch(conn, "pi", [helpers.item("path", rec)], False)
    assert res["accepted"] == [rec["uuid"]]
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM ledger WHERE uuid = ?", (rec["uuid"],)).fetchone()[0] == 1


def test_an_unknown_boundary_is_rejected_into_quarantine(db):
    rec = path_rec(boundary="maybe")
    with db.tx() as conn:
        res = ingest.ingest_batch(conn, "pi", [helpers.item("path", rec)], False)
    assert res["rejected"] and "boundary must be one of" in res["rejected"][0]["error"]


def test_at_must_agree_with_t_ms(db):
    rec = path_rec(at=timeutil.iso(timeutil.now_ms() - 600_000))
    with db.tx() as conn:
        res = ingest.ingest_batch(conn, "pi", [helpers.item("path", rec)], False)
    assert res["rejected"] and "at and t_ms disagree" in res["rejected"][0]["error"]


def test_paths_are_append_only(db):
    rec = path_rec()
    with db.tx() as conn:
        ingest.ingest_batch(conn, "pi", [helpers.item("path", rec)], False)
    with pytest.raises(Exception):
        with db.tx() as conn:
            conn.execute("UPDATE paths SET boundary = 'none'")
