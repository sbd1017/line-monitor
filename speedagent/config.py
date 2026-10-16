"""Agent configuration from the environment (systemd unit) plus the schedule fetched from the backend.

The agent never imports the backend package. Hard limits are duplicated here on purpose: whatever the backend
sends, the agent never tests more often than these floors.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "1.0.0"

HARD = {
    "min_interval_min": 5,
    "min_burst_interval_min": 5,
    "min_confirm_delay_s": 60,
    "max_confirm_count": 5,
    "max_tests_per_day": 300,
    "min_probe_interval_s": 10,
    "min_manual_gap_s": 120,
}

DEFAULT_SCHEDULE = {
    "interval_min": 30, "confirm_count": 2, "confirm_delay_s": 120, "burst_interval_min": 10, "burst_max_min": 120,
    "recovery_count": 2, "max_tests_per_day": 96, "paused_until": None, "quiet_threshold_mbps": 50.0,
    "quiet_max_defer": 3, "probe_interval_s": 60, "engine": "ookla", "fallback_engine": "speedtest-go",
    "server_mode": "auto", "servers": [], "degraded_down": 500.0, "target_down": 900.0, "severe_down": 200.0,
    "timezone": "Europe/London",
}


def clamp_schedule(cfg: dict) -> dict:
    """Merge with defaults and enforce the hard limits (never trust the wire)."""
    c = {**DEFAULT_SCHEDULE, **{k: v for k, v in (cfg or {}).items() if k in DEFAULT_SCHEDULE}}

    def num(key, lo, hi, kind=float):
        try:
            v = kind(c[key])
        except (TypeError, ValueError):
            v = kind(DEFAULT_SCHEDULE[key])
        c[key] = min(max(v, lo), hi)

    num("interval_min", HARD["min_interval_min"], 1440, int)
    num("burst_interval_min", HARD["min_burst_interval_min"], 720, int)
    num("confirm_delay_s", HARD["min_confirm_delay_s"], 3600, int)
    num("confirm_count", 0, HARD["max_confirm_count"], int)
    num("burst_max_min", 10, 1440, int)
    num("recovery_count", 1, 10, int)
    num("max_tests_per_day", 1, HARD["max_tests_per_day"], int)
    num("probe_interval_s", HARD["min_probe_interval_s"], 600, int)
    num("quiet_threshold_mbps", 1, 10000)
    num("quiet_max_defer", 0, 10, int)
    num("degraded_down", 1, 100000)
    num("target_down", 1, 100000)
    num("severe_down", 1, 100000)
    if c["engine"] not in ("ookla", "speedtest-go", "simulated"):
        c["engine"] = "ookla"
    if c["fallback_engine"] not in ("ookla", "speedtest-go", "none", "simulated"):
        c["fallback_engine"] = "none"
    if c["server_mode"] not in ("auto", "pinned", "prefer"):
        c["server_mode"] = "auto"
    c["servers"] = [str(s) for s in (c.get("servers") or []) if str(s).isdigit()][:20]
    return c


@dataclass
class AgentSettings:
    env: str = "production"
    agent_id: str = field(default_factory=lambda: socket.gethostname().split(".")[0][:32] or "agent")
    state_dir: Path = Path("/var/lib/speedagent")
    backend: str = "http://127.0.0.1:8772/speedtest/agent/v1"
    key_file: Path | None = None
    iface: str | None = None
    ookla_bin: str = "/opt/speedmon/engines/ookla-1.2.0/speedtest"
    speedtest_go_bin: str = "/opt/speedmon/engines/speedtest-go-1.8.3/speedtest-go"
    gateway: str | None = None                                  # default: from the routing table
    anchors: tuple = ("1.1.1.1", "8.8.8.8")
    dns_suffix: str = "example.com"                             # random-label lookups (never cached)
    http_check: str = "https://cp.cloudflare.com/generate_204"
    test_timeout_s: int = 150
    simulate: str | None = None                                 # development only: simulated engine profile
    latest_file: Path | None = None                             # public summary of the latest test (latest.py)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "agent.sqlite3"

    def key(self) -> bytes:
        paths = []
        if os.environ.get("CREDENTIALS_DIRECTORY"):
            paths.append(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "agent-key")
        if self.key_file:
            paths.append(self.key_file)
        for p in paths:
            try:
                k = p.read_text().strip()
            except OSError:
                continue
            if len(k) >= 32:
                return k.encode()
        raise SystemExit("No agent key: expected $CREDENTIALS_DIRECTORY/agent-key or SPEEDAGENT_KEY_FILE (32+ characters)")


def load() -> AgentSettings:
    e = os.environ
    s = AgentSettings()
    s.env = e.get("SPEEDAGENT_ENV", "production")
    if e.get("SPEEDAGENT_ID"):
        s.agent_id = e["SPEEDAGENT_ID"][:32]
    if e.get("SPEEDAGENT_STATE_DIR"):
        s.state_dir = Path(e["SPEEDAGENT_STATE_DIR"])
    s.backend = e.get("SPEEDAGENT_BACKEND", s.backend).rstrip("/")
    if e.get("SPEEDAGENT_KEY_FILE"):
        s.key_file = Path(e["SPEEDAGENT_KEY_FILE"])
    s.iface = e.get("SPEEDAGENT_IFACE") or None
    s.ookla_bin = e.get("SPEEDAGENT_OOKLA", s.ookla_bin)
    s.speedtest_go_bin = e.get("SPEEDAGENT_SPEEDTEST_GO", s.speedtest_go_bin)
    s.gateway = e.get("SPEEDAGENT_GATEWAY") or None
    if e.get("SPEEDAGENT_ANCHORS"):
        s.anchors = tuple(a.strip() for a in e["SPEEDAGENT_ANCHORS"].split(",") if a.strip())
    s.dns_suffix = e.get("SPEEDAGENT_DNS_SUFFIX", s.dns_suffix)
    s.http_check = e.get("SPEEDAGENT_HTTP_CHECK", s.http_check)
    if e.get("SPEEDAGENT_LATEST_FILE"):
        s.latest_file = Path(e["SPEEDAGENT_LATEST_FILE"])
    if s.env != "production" and e.get("SPEEDAGENT_SIMULATE"):
        s.simulate = e["SPEEDAGENT_SIMULATE"]
    return s
