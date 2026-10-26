"""Tests for the public demo (run from the app folder: .venv/bin/python -m pytest demo/tests)."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

import pytest

from demo import generate, tick
from demo.simulate import Sim
from demo.world import DAY, MIN, World
from speedmon import timeutil

ANCHOR = date(2026, 3, 30)                  # spans the clocks going forward on 29 March


def end_of(anchor: date) -> int:
    return timeutil.local_to_ms(f"{anchor.isoformat()}T15:20", "Europe/London")


def collect(w: World, until: int, state=None):
    out = []
    sim = Sim(w, state)
    sim.run(until, lambda stream, rec, raw=None: out.append((stream, dict(rec), raw)))
    return sim, out


def test_same_anchor_same_history():
    w = World(ANCHOR, days=6)
    _, a = collect(w, end_of(ANCHOR))
    _, b = collect(World(ANCHOR, days=6), end_of(ANCHOR))
    assert a == b
    kinds = {r["kind"] for s, r, _ in a if s == "measurement"}
    assert "scheduled" in kinds
    tests = [r for s, r, _ in a if s == "measurement" and r["kind"] == "scheduled"]
    slots = (end_of(ANCHOR) - w.start_ms) / (30 * MIN)
    assert slots - 3 <= len(tests) <= slots + 1          # one per half hour, never two in a slot


def test_carrying_on_matches_one_run():
    """The live updater continuing from a saved state produces exactly what one long run would have."""
    w = World(ANCHOR, days=4)
    mid = end_of(ANCHOR) - 9 * 3_600_000
    sim, first = collect(w, mid)
    state = json.loads(json.dumps(sim.state()))
    _, rest = collect(World(ANCHOR, days=4), end_of(ANCHOR), state)
    _, whole = collect(World(ANCHOR, days=4), end_of(ANCHOR))
    assert first + rest == whole


def test_probes_every_minute_and_no_probe_during_a_test():
    w = World(ANCHOR, days=2)
    _, recs = collect(w, end_of(ANCHOR))
    probes = sorted(r["t_ms"] for s, r, _ in recs if s == "probe")
    tests = [(r["t_ms"], r["t_ms"] + r["duration_ms"]) for s, r, _ in recs if s == "measurement"]
    gaps = [b - a for a, b in zip(probes, probes[1:])]
    assert max(gaps) < 3 * MIN
    for a, b in tests:
        assert not any(a < p < b for p in probes)
    seqs = sorted(r["agent_seq"] for _, r, _ in recs)
    assert seqs == list(range(1, len(seqs) + 1))


def test_minute_probe_on_a_tests_first_ms_keeps_its_own_uuid():
    """25 May 2026 18:56:00.013 UTC (anchor 22 Oct 2026): the 18:56 minute probe's jitter put it on the very ms a test
    started, and the pre-test probe there drew the same uuid, so the nightly build failed on probes.uuid."""
    w = World(date(2026, 10, 22), days=153)
    clash = 1779735360013
    _, recs = collect(w, clash + MIN)
    probes = [r for s, r, _ in recs if s == "probe"]
    assert len({r["uuid"] for r in probes}) == len(probes)
    assert [r["t_ms"] for r in probes if clash <= r["t_ms"] <= clash + 1] == [clash, clash + 1]
    assert len({r["uuid"] for _, r, _ in recs}) == len(recs)

def test_evening_slower_than_morning_on_weekdays():
    w = World(date(2026, 1, 28), days=21)
    _, recs = collect(w, end_of(date(2026, 1, 28)))
    clock = timeutil.LocalClock("Europe/London")
    ev, mo = [], []
    for s, r, _ in recs:
        if s == "measurement" and r["status"] == "ok" and r["kind"] == "scheduled":
            _, hour, dow, _ = clock.parts(r["t_ms"])
            if dow < 5 and 20 <= hour <= 22:
                ev.append(r["download_mbps"])
            elif dow < 5 and 4 <= hour <= 6:
                mo.append(r["download_mbps"])
    assert sorted(ev)[len(ev) // 2] < 0.9 * sorted(mo)[len(mo) // 2]


def test_never_beyond_the_link():
    w = World(ANCHOR, days=1096)
    t0 = w.ms(w.gig1_day) + 3 * DAY
    sim = Sim(w, {"t": t0, "next_probe": t0, "started": True, "boot": {"id": "x", "ms": t0 - DAY}})
    out = []
    sim.run(t0 + 5 * DAY, lambda s, r, raw=None: out.append((s, r)))
    dl = [r["download_mbps"] for s, r in out if s == "measurement" and r["status"] == "ok"]
    assert dl and max(dl) <= 1000 * 0.9415           # the Pi's 1 GbE port before the adapter


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    info = generate.build(d, ANCHOR, end_of(ANCHOR) - 2 * 3_600_000, 20231, log=lambda *a: None, days=21)
    generate.swap(d)
    return d, info


def test_generated_database_verifies(built):
    d, _ = built
    v = generate.verify(d / "speedmon.sqlite3")
    assert v["ok"], v["problems"]
    assert v["missing_seq"] == 0
    conn = sqlite3.connect(d / "speedmon.sqlite3")
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key = 'data_origin'").fetchone()[0]) == "synthetic"
    assert conn.execute("SELECT count(*) FROM measurements WHERE source != 'synthetic'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM annotations").fetchone()[0] >= 3


def test_tick_carries_on_through_ingest(built, monkeypatch):
    d, info = built
    later = info["end_ms"] + 90 * MIN
    monkeypatch.setattr(timeutil, "now_ms", lambda: later)
    conn = sqlite3.connect(d / "speedmon.sqlite3")
    conn.execute("INSERT INTO commands (created_at, kind, payload) VALUES (?, 'run_test', '{}')",
                 (timeutil.iso(info["end_ms"] + 30 * MIN),))
    conn.commit()
    added, requests = 0, 0
    for step in range(1, 10):                           # the timer runs every minute; ten-minute steps will do
        out = tick.tick(d, info["end_ms"] + step * 10 * MIN)
        assert out["rejected"] == []
        added, requests = added + out["accepted"], requests + out["requests"]
    assert added > 80 and requests == 1
    assert conn.execute("SELECT count(*) FROM measurements WHERE kind = 'manual' AND t_ms > ?",
                        (info["end_ms"],)).fetchone()[0] == 1
    again = tick.tick(d, later)                       # nothing new: nothing added, nothing rejected
    assert again["records"] == 0
    v = generate.verify(d / "speedmon.sqlite3")
    assert v["ok"] and v["missing_seq"] == 0


def test_request_just_before_a_tick_is_not_lost():
    """A 'run a test now' that can't finish before the tick's 'now' runs on the next tick."""
    w = World(ANCHOR, days=2)
    end = end_of(ANCHOR)
    sim, _ = collect(w, end)
    state = json.loads(json.dumps(sim.state()))
    out = []
    s2 = Sim(World(ANCHOR, days=2), state)
    s2.run(end + 10_000, lambda st, r, raw=None: out.append((st, r)), [end + 5_000])
    s3 = Sim(World(ANCHOR, days=2), json.loads(json.dumps(s2.state())))
    s3.run(end + 120_000, lambda st, r, raw=None: out.append((st, r)))
    assert [r["kind"] for st, r in out if st == "measurement"].count("manual") == 1


def test_long_stop_shows_as_monitor_offline(built, monkeypatch):
    d, _ = built
    state = json.loads((d / "demo-state.json").read_text())
    later = state["t"] + 3 * 3_600_000
    monkeypatch.setattr(timeutil, "now_ms", lambda: later)
    tick.tick(d, later)
    conn = sqlite3.connect(d / "speedmon.sqlite3")
    assert conn.execute("SELECT count(*) FROM probes WHERE t_ms > ? AND t_ms < ?",
                        (state["t"] + 10 * MIN, later - 2 * MIN)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE type = 'agent_start' AND t_ms >= ?", (later - MIN,)).fetchone()[0] == 1


def test_web_demo_is_open_and_fenced(built, monkeypatch):
    from fastapi.testclient import TestClient
    from demo import serve
    from speedmon.config import load_settings
    d, _ = built
    monkeypatch.setenv("SPEEDMON_ENV", "development")
    monkeypatch.setenv("SPEEDMON_DATA_DIR", str(d))
    s = load_settings()
    s.secure_cookies = False
    c = TestClient(serve.Demo(s), base_url="http://127.0.0.1:8921")
    r = c.get("/speedtest/")
    assert r.status_code == 200 and "PUBLIC DEMO" in r.text and "/leave" not in r.text
    assert serve.COOKIE in c.cookies
    csrf = re.search(r'name="csrf" content="([^"]+)"', r.text).group(1)
    assert c.get("/speedtest/api/live").json()["last"]
    assert c.post("/speedtest/api/settings", json={"changes": {}}, headers={"x-csrf-token": csrf}).status_code == 403
    assert c.post("/speedtest/api/annotations", json={}, headers={"x-csrf-token": csrf}).status_code == 403
    assert c.post("/speedtest/api/run-test", headers={"x-csrf-token": "wrong"}).status_code == 403
    assert c.post("/speedtest/api/run-test", headers={"x-csrf-token": csrf}).json()["ok"]
    big = c.post("/speedtest/api/exports", json={"format": "zip", "range": "all", "include_probes": True},
                 headers={"x-csrf-token": csrf})
    assert big.status_code == 429 and "92 days" in big.json()["error"]
    assert c.post("/speedtest/api/exports", json={"format": "csv", "range": "30d"}, headers={"x-csrf-token": csrf}).json()["ok"]
    assert c.get("/speedtest/enter", follow_redirects=False).status_code == 303
    assert c.post("/speedtest/agent/v1/records", json={}).status_code == 401
