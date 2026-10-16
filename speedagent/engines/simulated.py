"""Development-only engine (refused in production). Profiles: good, slow, flaky, fail."""
from __future__ import annotations

import json
import random
import time

from .base import Engine, EngineResult


class Simulated(Engine):
    name = "simulated"

    def __init__(self, profile: str = "good", seconds: float = 3.0):
        self.profile, self.seconds = profile, seconds

    def version(self):
        return "sim-1"

    def run(self, server_id=None, progress=None, timeout=150) -> EngineResult:
        prof = self.profile
        steps = 12
        for phase in ("ping", "download", "upload"):
            for i in range(steps):
                if progress:
                    progress(phase, (i + 1) / steps, None if phase == "ping" else random.uniform(100, 900), 9.5)
                time.sleep(self.seconds / (3 * steps))
        if prof == "fail" or (prof == "flaky" and random.random() < 0.4):
            return EngineResult(False, failure_code="server_error", failure_detail="simulated failure",
                                raw=json.dumps({"simulated": True}), engine_version="sim-1")
        dl = random.gauss(930, 8) if prof == "good" else random.gauss(300, 40)
        fields = {"download_mbps": dl, "upload_mbps": random.gauss(103, 1), "ping_ms": random.gauss(9.5, 0.5),
                  "jitter_ms": abs(random.gauss(1, 0.3)), "packet_loss_pct": 0.0, "server_id": "76431",
                  "server_sponsor": "Simulated", "server_location": "Nowhere", "server_name": "Simulated – Nowhere",
                  "download_bytes": int(dl * 1.25e6 * 10), "upload_bytes": 13_000_000, "download_ms": 10000, "upload_ms": 8000}
        return EngineResult(True, fields=fields, raw=json.dumps({"simulated": True, **fields}), public_ip="192.0.2.10",
                            engine_version="sim-1")
