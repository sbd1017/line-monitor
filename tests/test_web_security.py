import time

from helpers import item, measurement, signed

PROTECTED = ["/speedtest/", "/speedtest/settings", "/speedtest/api/summary", "/speedtest/api/measurement/x", "/speedtest/nonexistent",
             "/speedtest/exports/abc/download", "/speedtest/static/app/main.js"]


def test_unauthenticated_requests_all_look_the_same(client):
    for p in PROTECTED:
        r = client.get(p, follow_redirects=False)
        assert (r.status_code, r.headers.get("location")) == (303, "/speedtest/enter"), p
        assert "no-store" in r.headers["cache-control"]
    r = client.post("/speedtest/api/settings", json={}, follow_redirects=False)
    assert r.status_code == 401
    assert client.get("/speedtest/enter").status_code == 200
    assert client.get("/speedtest/static/entrance/enter.css").status_code == 200
    assert client.get("/other").status_code == 404


def test_wrong_phrase_and_lockout(client):
    for _ in range(5):
        r = client.post("/speedtest/enter", data={"phrase": "wrong wrong wrong"}, follow_redirects=False)
        assert r.headers["location"] == "/speedtest/enter?miss"
    r = client.post("/speedtest/enter", data={"phrase": "correct horse battery staple"}, follow_redirects=False)
    assert r.headers["location"] == "/speedtest/enter?miss"            # locked out: indistinguishable


def test_csrf_and_origin_required(signed_in):
    c = signed_in
    token = c.headers.pop("x-csrf-token")
    assert c.post("/speedtest/api/run-test", json={}).status_code == 403
    c.headers["x-csrf-token"] = token
    assert c.post("/speedtest/api/run-test", json={}, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/speedtest/api/run-test", json={}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.post("/speedtest/api/run-test", json={}).status_code == 200
    assert c.post("/speedtest/api/run-test", json={}).status_code == 429      # a second manual run within 3 minutes


def test_security_headers(signed_in):
    r = signed_in.get("/speedtest/")
    assert "script-src 'self'" in r.headers["content-security-policy"] and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and "noindex" in r.headers["x-robots-tag"]


def test_agent_api_requires_valid_signature(client):
    rec, raw = measurement(seq=1)
    body = {"items": [item("measurement", rec, raw)]}
    assert client.post("/speedtest/agent/v1/records", json=body).status_code == 401              # unsigned
    assert signed(client, "POST", "/speedtest/agent/v1/records", body, key=b"x" * 48).status_code == 401   # wrong key
    old = int(time.time() * 1000) - 10 * 60_000
    assert signed(client, "POST", "/speedtest/agent/v1/records", body, ts=old).status_code == 401          # stale
    assert signed(client, "POST", "/speedtest/agent/v1/records", body, headers={"CF-Ray": "abc"}).status_code == 401  # via tunnel
    r = signed(client, "POST", "/speedtest/agent/v1/records", body, nonce="a" * 32)
    assert r.status_code == 200 and r.json()["accepted"] == [rec["uuid"]]
    assert signed(client, "POST", "/speedtest/agent/v1/records", body, nonce="a" * 32).status_code == 401  # replay


def test_agent_session_cannot_reach_owner_api_and_vice_versa(signed_in):
    c = signed_in
    assert c.post("/speedtest/agent/v1/status", json={"state": "idle"}).status_code == 401   # a browser session is not an agent
    r = signed(c, "GET", "/speedtest/agent/v1/config")
    assert r.status_code == 200 and r.json()["config"]["interval_min"] == 30


def test_private_values_never_sent_to_browser(signed_in):
    c = signed_in
    rec, raw = measurement(seq=1)
    import uuid
    diag = {"uuid": str(uuid.uuid4()), "measurement_uuid": rec["uuid"], "agent_id": "pi", "agent_seq": 2, "t_ms": rec["t_ms"],
            "public_ip": "203.0.113.77", "public_ip_changed": 0}
    assert signed(c, "POST", "/speedtest/agent/v1/records", {"items": [item("measurement", rec, raw), item("diagnostic", diag)]}).status_code == 200
    for p in [f"/speedtest/api/measurement/{rec['uuid']}", "/speedtest/api/live", "/speedtest/api/measurements?range=all", "/speedtest/api/evidence?range=all"]:
        assert "203.0.113.77" not in c.get(p).text, p
    d = c.get(f"/speedtest/api/measurement/{rec['uuid']}").json()
    assert d["diagnostics"]["public_ip_fingerprint"] and d["hash_ok"]


def test_settings_validation_and_danger_confirmation(signed_in):
    c = signed_in
    assert c.post("/speedtest/api/settings", json={"changes": {"interval_min": 1}}).status_code == 400          # below hard floor
    assert c.post("/speedtest/api/settings", json={"changes": {"degraded_down": 950}}).status_code == 400       # above target
    r = c.post("/speedtest/api/settings", json={"changes": {"interval_min": 10}})
    assert r.status_code == 409 and r.json()["needs_confirmation"]
    assert c.post("/speedtest/api/settings", json={"changes": {"interval_min": 10}, "confirm": True}).status_code == 200
    cfg = signed(c, "GET", "/speedtest/agent/v1/config").json()
    assert cfg["config"]["interval_min"] == 10
    hist = c.get("/speedtest/api/settings").json()["history"]
    assert hist[0]["key"] == "interval_min"


def test_annotations_never_touch_measurements(signed_in):
    c = signed_in
    rec, raw = measurement(seq=1)
    signed(c, "POST", "/speedtest/agent/v1/records", {"items": [item("measurement", rec, raw)]})
    r = c.post("/speedtest/api/annotations", json={"title": "Virgin engineer visit", "start": "2026-10-10T09:00", "category": "engineer"})
    assert r.status_code == 200
    c.post(f"/speedtest/api/measurement/{rec['uuid']}/flag", json={"flag": "exclude", "reason": "downloading a game"})
    d = c.get(f"/speedtest/api/measurement/{rec['uuid']}").json()
    assert d["hash_ok"] and d["flags"][0]["flag"] == "exclude"
    assert c.get("/speedtest/api/summary?range=all").json()["summary"]["excluded"] == 1


def test_explorer_paging_and_sorting(signed_in, db):
    from speedmon import synthetic
    with db.tx() as c:
        synthetic.seed(c, days=20, interval_min=60, probes=False)
    c = signed_in
    seen = []
    for off in (0, 100, 200, 300, 400, 500):
        r = c.get(f"/speedtest/api/measurements?range=all&sort=download&dir=asc&size=100&offset={off}").json()
        seen += [(x["download_mbps"], x["uuid"]) for x in r["items"]]
    assert len(seen) == r["total"] and len({u for _, u in seen}) == len(seen)
    dls = [d for d, _ in seen if d is not None]
    assert dls == sorted(dls)
    for q in ("sort=anomaly", "filter=anomaly&sort=time", "sort=ping&dir=desc", "filter=degraded&sort=upload", "status=failed"):
        assert c.get(f"/speedtest/api/measurements?range=all&{q}").status_code == 200, q


def test_status_file_is_whitelisted_and_world_readable(signed_in, app, settings, tmp_path):
    import json, stat
    from speedmon import statusfile
    c = signed_in
    rec, raw = measurement(seq=1, dl=128.0, server_host="speed.example.net:8080", server_name="Example")
    import uuid
    diag = {"uuid": str(uuid.uuid4()), "measurement_uuid": rec["uuid"], "agent_id": "pi", "agent_seq": 2, "t_ms": rec["t_ms"],
            "public_ip": "203.0.113.77", "public_ip_changed": 0}
    assert signed(c, "POST", "/speedtest/agent/v1/records", {"items": [item("measurement", rec, raw), item("diagnostic", diag)]}).status_code == 200
    st = {"state": "idle", "iface": "eth0", "gateway": "192.168.8.1", "lan_ip": "192.168.8.20", "next_test_ms": rec["t_ms"] + 1800_000,
          "interval_min": 30, "mode": "normal"}
    assert signed(c, "POST", "/speedtest/agent/v1/status", st).status_code == 200
    doc = app.state.status_document()
    text = json.dumps(doc)
    for secret in ("203.0.113.77", "192.168.8.1", "192.168.8.20", "speed.example.net", "eth0", "\"pi\"", str(settings.data_dir)):
        assert secret not in text, secret
    assert doc["last"]["download_mbps"] == 128.0 and doc["agent"]["online"] and doc["agent"]["next_test_ms"] == st["next_test_ms"]
    assert doc["url"] == "https://example.com/speedtest/" and doc["health_label"]
    assert set(doc) == {"schema", "generated_at_ms", "url", "health", "health_label", "package", "last", "last_ok", "agent",
                        "averages", "guarantee"}
    out = tmp_path / "run" / "status.json"
    out.parent.mkdir()
    statusfile.write(out, doc)
    assert json.loads(out.read_text()) == json.loads(json.dumps(doc))
    assert stat.S_IMODE(out.stat().st_mode) == 0o644 and not list(out.parent.glob(".*.tmp"))


def test_status_writer_runs_with_the_app(settings, tmp_path):
    import json, time as _t
    from fastapi.testclient import TestClient
    from speedmon.web import create_app
    settings.status_file = tmp_path / "status.json"
    with TestClient(create_app(settings)):
        for _ in range(50):
            if settings.status_file.exists():
                break
            _t.sleep(0.05)
        doc = json.loads(settings.status_file.read_text())
    assert doc["schema"] == 1 and doc["health"] == "unknown" and doc["last"] is None


def test_status_next_test_ignores_waits_that_are_not_tests():
    from speedmon import timeutil
    from speedmon import statusfile
    now = timeutil.now_ms()
    live = lambda **a: {"health": "ok", "agent": {"state": "idle", "next_scheduled_ms": now + 240_000, **a}}
    nxt = lambda **a: statusfile.build(live(**a), None, now, "https://example.com/speedtest/")["agent"]["next_test_ms"]
    assert nxt(next_kind="spacing", next_test_ms=now + 60_000) == now + 240_000   # the 90 s gap after a test isn't a test
    assert nxt(next_kind="confirmation", next_test_ms=now + 60_000) == now + 60_000
    assert nxt(next_kind="cap", next_test_ms=now + 60_000) is None


def test_explorer_colours_upload_and_high_ping(signed_in):
    c = signed_in
    good, raw1 = measurement(seq=1, dl=950.0)
    poor, raw2 = measurement(seq=2, dl=950.0, upload_mbps=40.0, ping_ms=80.0, t_ms=good["t_ms"] - 60_000)
    assert signed(c, "POST", "/speedtest/agent/v1/records", {"items": [item("measurement", good, raw1), item("measurement", poor, raw2)]}).status_code == 200
    items = {m["uuid"]: m for m in c.get("/speedtest/api/measurements?range=all").json()["items"]}
    assert items[good["uuid"]]["band_up"] == "target" and not items[good["uuid"]].get("ping_high")
    assert items[poor["uuid"]]["band_up"] == "degraded" and items[poor["uuid"]]["ping_high"]
