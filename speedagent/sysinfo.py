"""Local diagnostics from /proc and /sys (no root needed). Everything degrades to None when unavailable, so
the agent also runs (with fewer diagnostics) on a development machine."""
from __future__ import annotations

import os
import socket
import struct
import subprocess
import time
from pathlib import Path


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def default_route() -> tuple[str | None, str | None]:
    """(interface, gateway IPv4) from /proc/net/route."""
    text = _read("/proc/net/route")
    if not text:
        return None, None
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 3 and parts[1] == "00000000" and int(parts[3], 16) & 2:
            gw = socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
            return parts[0], gw
    return None, None


def link(iface: str | None) -> dict:
    if not iface:
        return {"iface": None, "link_speed_mbps": None, "link_duplex": None, "link_state": None, "carrier": None}
    base = f"/sys/class/net/{iface}"
    speed = _read(base + "/speed")
    try:
        sp = int(speed) if speed is not None else None
        sp = sp if sp and sp > 0 else None
    except ValueError:
        sp = None
    carrier = _read(base + "/carrier")
    return {"iface": iface, "link_speed_mbps": sp, "link_duplex": _read(base + "/duplex"),
            "link_state": _read(base + "/operstate"), "carrier": carrier == "1" if carrier is not None else None}


COUNTERS = ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped")


def counters(iface: str | None) -> dict:
    out = {"t": time.monotonic()}
    for c in COUNTERS:
        v = _read(f"/sys/class/net/{iface}/statistics/{c}") if iface else None
        out[c] = int(v) if v and v.isdigit() else None
    return out


def delta(a: dict, b: dict, key: str):
    if a.get(key) is None or b.get(key) is None:
        return None
    return max(0, b[key] - a[key])


def rate_mbps(a: dict, b: dict, key: str) -> float | None:
    d = delta(a, b, key)
    dt = b["t"] - a["t"]
    return None if d is None or dt <= 0 else d * 8 / dt / 1e6


def load() -> tuple[float | None, float | None]:
    try:
        l1, l5, _ = os.getloadavg()
        return l1, l5
    except OSError:
        return None, None


def cpu_temp() -> float | None:
    v = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(v) / 1000 if v else None
    except ValueError:
        return None


def throttled() -> str | None:
    """Raspberry Pi firmware flags (under-voltage, throttling, ...). 0x0 means none ever since boot."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        return out.strip().split("=", 1)[1] if "=" in out else None
    except (OSError, subprocess.SubprocessError):
        return None


def memory() -> tuple[float | None, float | None]:
    text = _read("/proc/meminfo")
    if not text:
        return None, None
    info = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.split()[0]) if v.split() else 0
    avail = 100 * info.get("MemAvailable", 0) / info["MemTotal"] if info.get("MemTotal") else None
    swap = (100 * (info["SwapTotal"] - info.get("SwapFree", 0)) / info["SwapTotal"]) if info.get("SwapTotal") else 0.0
    return avail, swap


def uptime_s() -> float | None:
    v = _read("/proc/uptime")
    return float(v.split()[0]) if v else None


def boot_id() -> str | None:
    return _read("/proc/sys/kernel/random/boot_id")


def boot_ms() -> int | None:
    text = _read("/proc/stat")
    for line in (text or "").splitlines():
        if line.startswith("btime "):
            return int(line.split()[1]) * 1000
    return None


def clock_synced() -> bool | None:
    if Path("/run/systemd/timesync/synchronized").exists():
        return True
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True,
                             timeout=3).stdout.strip()
        return out == "yes" if out else None
    except (OSError, subprocess.SubprocessError):
        return None
