from types import SimpleNamespace

from speedagent import probes


def run_with(monkeypatch, gw_ok=True, ext_ok=True, dns_ok=True, http_ok=True, ext_ms=11.0, carrier=True):
    def ping(host, **kw):
        ok = gw_ok if host == "192.168.8.1" else ext_ok
        return {"rtt_ms": (0.5 if host == "192.168.8.1" else ext_ms) if ok else None, "loss_pct": 0.0 if ok else 100.0,
                "sent": 4, "received": 4 if ok else 0}
    monkeypatch.setattr(probes, "ping", ping)
    monkeypatch.setattr(probes, "dns_check", lambda suffix: (dns_ok, 5.0 if dns_ok else None))
    monkeypatch.setattr(probes, "http_check", lambda url: (http_ok, 30.0 if http_ok else None))
    monkeypatch.setattr(probes.sysinfo, "link", lambda iface: {"carrier": carrier, "link_speed_mbps": 1000 if carrier else None})
    s = SimpleNamespace(anchors=("1.1.1.1", "8.8.8.8"), dns_suffix="example.com", http_check="https://x")
    return probes.run(s, "eth0", "192.168.8.1")


def test_probe_states(monkeypatch):
    assert run_with(monkeypatch)["state"] == "ok"
    assert run_with(monkeypatch, ext_ms=80)["state"] == "degraded"
    assert run_with(monkeypatch, ext_ok=False, http_ok=False, dns_ok=False)["state"] == "wan_down"
    assert run_with(monkeypatch, dns_ok=False)["state"] == "dns_fail"
    assert run_with(monkeypatch, gw_ok=False, ext_ok=False, http_ok=False)["state"] == "lan_fault"   # never blamed on the ISP
    assert run_with(monkeypatch, carrier=False)["state"] == "link_down"


def test_real_icmp_to_loopback():
    r = probes.ping("127.0.0.1", count=2, interval=0.05, timeout=0.5)
    assert r["received"] == 2 and r["loss_pct"] == 0.0
