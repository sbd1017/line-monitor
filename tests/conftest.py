import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PHRASE = "correct horse battery staple"
KEY = "k" * 48


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEEDMON_ENV", "test")
    monkeypatch.setenv("SPEEDMON_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("SPEEDMON_SECURE_COOKIES", "0")
    keyfile = tmp_path / "agent.key"
    keyfile.write_text(KEY)
    monkeypatch.setenv("SPEEDMON_AGENT_KEY_FILE", str(keyfile))
    from speedmon.config import load_settings
    s = load_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def db(settings):
    from speedmon.db import Database
    d = Database(settings.db_path)
    d.migrate()
    return d


@pytest.fixture
def app(settings):
    from speedmon import auth
    from speedmon.web import create_app
    auth.write_phrase_hash(settings.phrase_file, PHRASE)
    return create_app(settings)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    async def loopback(scope, receive, send):        # the agent API only answers loopback peers
        if scope["type"] == "http":
            scope = {**scope, "client": (scope.get("x_peer") or "127.0.0.1", 50000)}
        await app(scope, receive, send)
    return TestClient(loopback, base_url="http://testserver")


@pytest.fixture
def signed_in(client):
    r = client.post("/speedtest/enter", data={"phrase": PHRASE}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/speedtest/"
    csrf = re.search(r'name="csrf" content="([^"]+)"', client.get("/speedtest/").text).group(1)
    client.headers.update({"x-csrf-token": csrf})
    return client
