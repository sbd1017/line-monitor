import time
import pytest
from fastapi.testclient import TestClient
from speedmon.web import create_app

BASE = "/speedtest"
CSRF = "t" * 43
HEADERS = {"x-site-user": "owner", "x-site-csrf": CSRF, "x-site-expires": str(int(time.time()) + 3600)}


def client_for(settings, monkeypatch, peer="127.0.0.1"):
    monkeypatch.setenv("SITE_AUTH", "1")
    app = create_app(settings)
    async def transport(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (peer, 50000)}
        await app(scope, receive, send)
    return TestClient(transport, follow_redirects=False)


def test_central_login_and_logout(settings, monkeypatch):
    with client_for(settings, monkeypatch) as c:
        assert c.get(BASE + "/", headers=HEADERS).status_code == 200
        assert not c.cookies
        r = c.get(BASE + "/enter", headers=HEADERS)
        assert r.status_code == 303 and r.headers["location"] == BASE + "/"
        assert c.post(BASE + "/enter", headers=HEADERS).status_code == 405
        assert c.post(BASE + "/leave", headers=HEADERS).status_code == 403
        assert c.post(BASE + "/leave", headers={**HEADERS, "origin": "https://evil.example"}, data={"csrf_token": CSRF}).status_code == 403
        r = c.post(BASE + "/leave", headers=HEADERS, data={"csrf_token": CSRF})
        assert r.status_code == 303 and r.headers["location"] == "/logout"
        assert "set-cookie" not in r.headers


@pytest.mark.parametrize("change", [{"x-site-user": "other"}, {"x-site-csrf": "short"}, {"x-site-expires": "1"}, {"x-site-expires": "NaN"}])
def test_invalid_headers_fail_closed(settings, monkeypatch, change):
    with client_for(settings, monkeypatch) as c:
        assert c.get(BASE + "/", headers={**HEADERS, **change}).status_code == 401
        assert c.get(BASE + "/enter").status_code == 401
        assert c.get(BASE + "/", headers={"cookie": "__Secure-fin_session=old; __Secure-speedmon_session=old; bets_session=old"}).status_code == 401


def test_forwarded_headers_cannot_fake_peer(settings, monkeypatch):
    with client_for(settings, monkeypatch, "198.51.100.23") as c:
        assert c.get(BASE + "/", headers={**HEADERS, "x-forwarded-for": "127.0.0.1", "x-real-ip": "127.0.0.1"}).status_code == 401


def test_duplicate_proxy_headers_rejected(settings, monkeypatch):
    with client_for(settings, monkeypatch) as c:
        assert c.get(BASE + "/", headers=list(HEADERS.items()) + [("x-site-user", "owner")]).status_code == 401


def test_proxy_key_is_required_once_configured(settings, monkeypatch, tmp_path):
    key = "k" * 64
    (tmp_path / "proxy-key").write_text(key + "\n")
    monkeypatch.setenv("SITE_PROXY_KEY_FILE", str(tmp_path / "proxy-key"))
    with client_for(settings, monkeypatch) as c:
        assert c.get(BASE + "/", headers={**HEADERS, "x-site-proxy-key": key}).status_code == 200
        assert c.get(BASE + "/", headers=HEADERS).status_code == 401              # a local process without the key
        assert c.get(BASE + "/", headers={**HEADERS, "x-site-proxy-key": "k" * 63 + "x"}).status_code == 401
        two = list(HEADERS.items()) + [("x-site-proxy-key", key), ("x-site-proxy-key", key)]
        assert c.get(BASE + "/", headers=two).status_code == 401
    with client_for(settings, monkeypatch, "198.51.100.23") as c:                # the key alone is not enough
        assert c.get(BASE + "/", headers={**HEADERS, "x-site-proxy-key": key}).status_code == 401


def test_production_refuses_without_proxy_key(settings, monkeypatch):
    monkeypatch.setenv("SITE_AUTH", "1")
    monkeypatch.delenv("SITE_PROXY_KEY_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    settings.env = "production"
    with pytest.raises(SystemExit, match="proxy key"):
        create_app(settings)


def test_agent_does_not_accept_browser_identity(settings, monkeypatch):
    with client_for(settings, monkeypatch) as c:
        assert c.post(BASE + "/agent/ingest", headers=HEADERS).status_code == 401


def test_hmac_still_works_without_site_session(settings, monkeypatch):
    from helpers import signed
    with client_for(settings, monkeypatch) as c:
        r = signed(c, "GET", BASE + "/agent/v1/config")
        assert r.status_code == 200
        assert "set-cookie" not in r.headers
        assert signed(c, "GET", BASE + "/api/summary").status_code == 401
