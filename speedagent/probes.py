"""Light connectivity probes between full tests (about 2 KB per probe).

* ICMP echo through an unprivileged datagram socket (net.ipv4.ping_group_range), no root and no subprocess:
  4 pings to the router, 4 to each external anchor.
* DNS: a lookup of a random, never-cached name under our own domain. An answer *or* NXDOMAIN proves the resolver
  chain works; a timeout or SERVFAIL does not.
* HTTPS: one tiny request to a 204 endpoint.

State, in order of precedence:
  link_down  the Pi's own Ethernet link is down
  lan_fault  the router doesn't answer (a Pi/LAN problem, not the ISP)
  wan_down   router fine, but no anchor answers and HTTPS fails: the Internet is unreachable
  dns_fail   Internet reachable, but name resolution fails
  degraded   reachable, but external latency or loss is poor
  ok
"""
from __future__ import annotations

import random
import secrets
import socket
import statistics
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import sysinfo

_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="probe")


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def ping(host: str, count: int = 4, interval: float = 0.15, timeout: float = 1.0) -> dict:
    """{'rtt_ms': median or None, 'min', 'max', 'loss_pct', 'sent', 'received'}"""
    rtts = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except OSError as exc:
        return {"rtt_ms": None, "loss_pct": None, "sent": 0, "received": 0, "error": f"socket: {exc.errno}"}
    try:
        ident = random.randint(0, 0xFFFF)
        for seq in range(count):
            payload = secrets.token_bytes(16)
            head = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
            pkt = struct.pack("!BBHHH", 8, 0, _checksum(head + payload), ident, seq) + payload
            t0 = time.monotonic()
            try:
                sock.sendto(pkt, (host, 0))
            except OSError:
                time.sleep(interval)
                continue
            deadline = t0 + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, _ = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    break
                if data and data[0] >> 4 == 4:                # macOS includes the IP header
                    data = data[(data[0] & 0x0F) * 4:]
                if len(data) >= 8 and data[0] == 0 and struct.unpack("!H", data[6:8])[0] == seq and data[8:] == payload:
                    rtts.append((time.monotonic() - t0) * 1000)
                    break
            spent = time.monotonic() - t0
            if seq < count - 1 and spent < interval:
                time.sleep(interval - spent)
    finally:
        sock.close()
    return {"rtt_ms": statistics.median(rtts) if rtts else None, "min": min(rtts) if rtts else None,
            "max": max(rtts) if rtts else None, "loss_pct": 100.0 * (count - len(rtts)) / count, "sent": count,
            "received": len(rtts)}


def dns_check(suffix: str, timeout: float = 3.0) -> tuple[bool, float | None]:
    name = f"lm-{secrets.token_hex(6)}.{suffix}"
    t0 = time.monotonic()

    def lookup():
        try:
            socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
            return True
        except socket.gaierror as exc:
            return exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5))   # NXDOMAIN: resolver works
    fut = _pool.submit(lookup)
    try:
        ok = fut.result(timeout=timeout)
    except Exception:
        ok = False
    return ok, ((time.monotonic() - t0) * 1000 if ok else None)


def http_check(url: str, timeout: float = 4.0) -> tuple[bool, float | None]:
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "speedagent-probe"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(64)
            ok = r.status in (200, 204)
    except (urllib.error.URLError, OSError, ValueError):
        ok = False
    return ok, ((time.monotonic() - t0) * 1000 if ok else None)


def run(settings, iface: str | None, gateway: str | None, latency_poor_ms: float = 40.0) -> dict:
    """One probe cycle. Returns the probe fields (without uuid/agent/time, added by the caller)."""
    ln = sysinfo.link(iface)
    out = {"link_up": int(bool(ln["carrier"])) if ln["carrier"] is not None else None,
           "link_speed_mbps": ln["link_speed_mbps"]}
    if ln["carrier"] is False:
        out.update(state="link_down")
        return out
    jobs = {"gw": _pool.submit(ping, gateway) if gateway else None}
    for a in settings.anchors:
        jobs[a] = _pool.submit(ping, a)
    jobs["dns"] = _pool.submit(dns_check, settings.dns_suffix)
    jobs["http"] = _pool.submit(http_check, settings.http_check)
    res = {k: (f.result() if f else None) for k, f in jobs.items()}
    gw = res["gw"]
    ext = [res[a] for a in settings.anchors]
    ext_rtts = [e["rtt_ms"] for e in ext if e["rtt_ms"] is not None]
    ext_loss = [e["loss_pct"] for e in ext if e["loss_pct"] is not None]
    dns_ok, dns_ms = res["dns"]
    http_ok, http_ms = res["http"]
    out.update(gw_rtt_ms=gw["rtt_ms"] if gw else None, gw_loss_pct=gw["loss_pct"] if gw else None,
               ext_rtt_ms=statistics.median(ext_rtts) if ext_rtts else None,
               ext_loss_pct=(sum(ext_loss) / len(ext_loss)) if ext_loss else None,
               ext_detail=";".join(f"{a}={'-' if e['rtt_ms'] is None else round(e['rtt_ms'], 2)}ms/{e['loss_pct']:.0f}%"
                                   for a, e in zip(settings.anchors, ext) if e.get("loss_pct") is not None) or None,
               dns_ok=int(dns_ok), dns_ms=dns_ms, http_ok=int(http_ok), http_ms=http_ms)
    if gw is not None and gw["received"] == 0:
        state = "lan_fault"
    elif not ext_rtts and not http_ok:
        state = "wan_down"
    elif not dns_ok:
        state = "dns_fail"
    elif (out["ext_rtt_ms"] or 0) > latency_poor_ms or (out["ext_loss_pct"] or 0) >= 20:
        state = "degraded"
    else:
        state = "ok"
    out["state"] = state
    return out

