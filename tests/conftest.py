import re
import subprocess
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


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def tsa(tmp_path, monkeypatch):
    """A self-signed RFC 3161 authority answering from the local filesystem, so anchor tests stay offline."""
    from speedmon import anchors

    work = tmp_path / "tsa"
    (work / "certs").mkdir(parents=True)
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-keyout", "ca.key", "-out", "ca.pem", "-nodes", "-days", "2",
             "-subj", "/CN=Test TSA", "-addext", "extendedKeyUsage=critical,timeStamping",
             "-addext", "basicConstraints=critical,CA:true", cwd=work)
    (work / "serial").write_text("01")
    (work / "tsa.cnf").write_text(
        "[ tsa ]\ndefault_tsa = one\n[ one ]\ndir = .\nserial = ./serial\nsigner_cert = ./ca.pem\ncerts = ./certs\n"
        "signer_key = ./ca.key\ndefault_policy = 1.3.6.1.4.1.99999.1\ndigests = sha256\naccuracy = secs:1\n"
        "ordering = yes\ntsa_name = yes\ness_cert_id_chain = no\ness_cert_id_alg = sha256\nsigner_digest = sha256\ncrypto_device = builtin\n")

    def post(url, body, timeout):
        if url == "http://broken":
            raise anchors.AnchorError("connection refused")
        (work / "q.tsq").write_bytes(body)
        _openssl("ts", "-reply", "-queryfile", "q.tsq", "-config", "tsa.cnf", "-out", "r.tsr", cwd=work)
        return (work / "r.tsr").read_bytes()

    monkeypatch.setattr(anchors, "_post", post)
    return work
