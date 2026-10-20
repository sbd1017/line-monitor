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
