"""Engine interface. An engine runs one full throughput test and returns an EngineResult with the measurement
fields it could fill, the raw engine output (with private fields removed) and, on failure, a failure code.

Failure codes: engine_missing, engine_error, server_error, network_error, timeout, parse_error, killed.
"""
from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class EngineResult:
    ok: bool
    fields: dict = field(default_factory=dict)
    raw: str | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    public_ip: str | None = None
    engine_version: str | None = None


class Engine:
    name = "base"

    def version(self) -> str | None:
        return None

    def run(self, server_id: str | None, progress=None, timeout: int = 150) -> EngineResult:   # pragma: no cover
        raise NotImplementedError


def redact(obj, keys=("internalIp", "macAddr", "externalIp", "IP", "Lat", "Lon")):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in keys else redact(v, keys)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x, keys) for x in obj]
    return obj


def classify(message: str) -> str:
    m = (message or "").lower()
    if any(s in m for s in ("resolve", "network is unreachable", "no route", "connection refused", "cannot open socket",
                            "couldn't connect", "timeout was reached", "name or service not known", "temporary failure")):
        return "network_error"
    if any(s in m for s in ("server", "configuration", "no servers", "selection")):
        return "server_error"
    if "timed out" in m or "timeout" in m:
        return "timeout"
    return "engine_error"


@contextmanager
def shielded():
    """Start engine processes with SIGUSR1 ignored (ignored signals survive exec), so a "run a test now" signal
    sent to the whole service can never kill a running test. Only possible from the main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGUSR1, old)
