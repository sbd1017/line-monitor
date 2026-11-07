"""Hub reachability probe: page hunting, login detection and the not-reachable path, against a local fake Hub."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from threading import Thread

import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer

_spec = importlib.util.spec_from_file_location("hub_probe", Path(__file__).resolve().parent.parent / "tools" / "hub_probe.py")
hub_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hub_probe)

PAGES = {
    "/": "<title>Hub 5</title><input type=password name=p>",
    "/modemstatus.html": "<title>Modem Status</title><table>downstream</table>",
    "/swinfo.html": "<title>Software</title>9.1.2",
}


@pytest.fixture
def hub():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            self.send_response(200)
            self.send_header("Set-Cookie", "session=1")
            self.end_headers()
            self.wfile.write(b"<title>Status</title>ok")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run(monkeypatch, tmp_path, target, *extra):
    monkeypatch.setattr(hub_probe, "ping", lambda ip: {"sent": True, "loss_pct": 0.0, "rtt_ms": 0.4})
    out = tmp_path / "report.md"
    code = hub_probe.main(["--hub", target, "--out", str(out), "--timeout", "1", *extra])
    return code, out.read_text()


def test_finds_management_pages(monkeypatch, tmp_path, hub):
    code, report = run(monkeypatch, tmp_path, hub)
    assert "| 200 | `/modemstatus.html` |" in report
    assert "## Verdict" in report
    assert code in (0, 3)                       # 3 while the login is unsolved, 0 once it is


def test_wants_login_without_credentials(monkeypatch, tmp_path, hub):
    code, report = run(monkeypatch, tmp_path, hub)
    assert code == 3
    assert "wants a login" in report


def test_accepted_login_clears_the_block(monkeypatch, tmp_path, hub):
    code, report = run(monkeypatch, tmp_path, hub, "--login", "admin:secret")
    assert "accepted" in report
    assert "reachable and readable" in report
    assert code == 0


def test_unreachable_prints_router_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hub_probe, "ping", lambda ip: {"sent": True, "loss_pct": 100.0, "rtt_ms": None})
    monkeypatch.setattr(hub_probe, "tcp", lambda host, port, timeout: False)
    out = tmp_path / "report.md"
    code = hub_probe.main(["--hub", "192.168.100.1", "--out", str(out), "--timeout", "0.2"])
    report = out.read_text()
    assert code == 1
    assert "ip addr add 192.168.100.2/24" in report
    assert "ip route add 192.168.100.1 dev" in report   # not 192.168.100.1.1


def test_no_web_interface_but_ping(monkeypatch, tmp_path):
    monkeypatch.setattr(hub_probe, "ping", lambda ip: {"sent": True, "loss_pct": 0.0, "rtt_ms": 1.0})
    monkeypatch.setattr(hub_probe, "tcp", lambda host, port, timeout: False)
    out = tmp_path / "report.md"
    code = hub_probe.main(["--hub", "192.168.100.1", "--out", str(out), "--timeout", "0.2"])
    assert code == 2
    assert "no web interface open" in out.read_text()
