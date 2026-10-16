import json

from speedagent import config as C
from speedagent.main import Agent
from speedagent.store import Store
from speedmon import ingest


def make_agent(tmp_path, monkeypatch, profile="good"):
    key = tmp_path / "k"
    key.write_text("k" * 48)
    monkeypatch.setenv("SPEEDAGENT_ENV", "development")
    monkeypatch.setenv("SPEEDAGENT_STATE_DIR", str(tmp_path / "agent"))
    monkeypatch.setenv("SPEEDAGENT_KEY_FILE", str(key))
    monkeypatch.setenv("SPEEDAGENT_SIMULATE", profile)
    s = C.load()
    a = Agent(s)
    a.engines["simulated"].seconds = 0.05
    a.quiet_check = lambda: (0.1, 0.1, 0)
    a.probe_once = lambda iv: {"state": "ok", "gw_rtt_ms": 0.5}
    return a


def deliver(agent, db):
    items = [{"stream": b["stream"], "record": json.loads(b["record"]), "record_hash": b["record_hash"],
              **({"raw": b["raw"]} if b["stream"] == "measurement" else {})} for b in agent.store.pending()]
    with db.tx() as c:
        return ingest.ingest_batch(c, agent.s.agent_id, items, allow_synthetic=True)


def test_agent_test_records_are_accepted_by_backend(tmp_path, monkeypatch, db):
    a = make_agent(tmp_path, monkeypatch)
    out = a.run_test("manual", "test")
    assert out["status"] == "ok"
    res = deliver(a, db)
    assert len(res["accepted"]) == 2 and not res["rejected"]             # measurement + its diagnostics


def test_failed_engine_records_failure_and_runs_fallback(tmp_path, monkeypatch, db):
    a = make_agent(tmp_path, monkeypatch, "fail")
    from speedagent.engines.simulated import Simulated
    a.engines["backup"] = Simulated("good", 0.05)
    a.cfg["fallback_engine"] = "backup"
    out = a.run_test("scheduled", "slot")
    assert out["status"] == "ok" and out["fallback"]["status"] == "ok"
    res = deliver(a, db)
    with db.read() as c:
        rows = c.execute("SELECT kind, status, failure_code, related_uuid FROM measurements ORDER BY t_ms").fetchall()
    assert [r["status"] for r in rows] == ["failed", "ok"] and rows[1]["kind"] == "fallback" and rows[1]["related_uuid"]


def test_precheck_outage_records_failed_test_without_running_engine(tmp_path, monkeypatch, db):
    a = make_agent(tmp_path, monkeypatch)
    a.probe_once = lambda iv: {"state": "wan_down"}
    out = a.run_test("scheduled", "slot")
    assert out["status"] == "failed" and out["failure_code"] == "wan_down"


def test_outbox_survives_and_keeps_order(tmp_path):
    st = Store(tmp_path / "a.sqlite3")
    recs = [st.add("event", {"uuid": f"00000000-0000-4000-8000-00000000000{i}", "agent_id": "x", "t_ms": 1_790_000_000_000 + i,
                             "at": "2026-09-21T00:00:00.000Z", "type": "t"}) for i in range(3)]
    assert [r["agent_seq"] for r in recs] == [1, 2, 3]
    st2 = Store(tmp_path / "a.sqlite3")                                   # "restart"
    assert [p["seq"] for p in st2.pending()] == [1, 2, 3]
    st2.mark([recs[0]["uuid"]], {recs[1]["uuid"]: "bad"})
    q = st2.queue_stats()
    assert q["queue_depth"] == 1 and q["rejected_local"] == 1
    assert st2.add("event", {"uuid": "00000000-0000-4000-8000-000000000009", "agent_id": "x", "t_ms": 1_790_000_000_009,
                             "at": "2026-09-21T00:00:00.000Z", "type": "t"})["agent_seq"] == 4


def test_latest_file_is_atomic_public_and_has_no_private_fields(tmp_path, monkeypatch):
    import os
    import stat
    from speedagent import latest
    path = tmp_path / "pub" / "latest.json"
    path.parent.mkdir()
    monkeypatch.setenv("SPEEDAGENT_LATEST_FILE", str(path))
    a = make_agent(tmp_path, monkeypatch)
    a.run_test("scheduled", "slot")
    doc = json.loads(path.read_text())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert doc["schema"] == 1 and doc["latest"]["status"] == "ok" and doc["latest"]["download_mbps"] > 800
    assert doc["last_ok"] == doc["latest"] and doc["latest"]["server"] == "Simulated – Nowhere"
    assert set(doc["latest"]) == {"at", "status", "kind", "source", "engine", "failure_code", "download_mbps",
                                  "upload_mbps", "ping_ms", "jitter_ms", "server"}
    assert "192.0.2.10" not in path.read_text()                          # the engine's public IP never goes in
    assert [p.name for p in path.parent.iterdir()] == ["latest.json"]   # no temp files left behind
    # a failed test replaces "latest" but keeps the last good result
    good = doc["latest"]
    a.engines["simulated"].profile = "fail"
    a.cfg["fallback_engine"] = "none"
    a.run_test("scheduled", "slot")
    doc = json.loads(path.read_text())
    assert doc["latest"]["status"] == "failed" and doc["latest"]["failure_code"] == "server_error"
    assert doc["latest"]["download_mbps"] is None and doc["last_ok"] == good
    # an unwritable target is logged, never raised
    assert latest.write(tmp_path / "missing" / "latest.json", {"status": "ok"}, "now") is False


def test_latest_file_not_written_unless_configured(tmp_path, monkeypatch):
    a = make_agent(tmp_path, monkeypatch)
    assert a.s.latest_file is None
    a.run_test("manual", "test")
    assert not list(tmp_path.rglob("latest.json"))


def test_engine_processes_ignore_the_run_now_signal():
    import subprocess
    import sys
    from speedagent.engines.base import shielded
    code = "import signal; print(int(signal.getsignal(signal.SIGUSR1)))"
    with shielded():
        shielded_out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    normal_out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    import signal
    assert shielded_out == str(int(signal.SIG_IGN)) and normal_out == str(int(signal.SIG_DFL))
