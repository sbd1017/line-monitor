"""speedtest-go (open source, MIT; same Ookla server network). Used as the fallback engine.
Its JSON gives dl_speed/ul_speed in bytes per second and latencies/durations in nanoseconds."""
from __future__ import annotations

import json
import os
import subprocess

from .base import Engine, EngineResult, classify, redact, shielded


class SpeedtestGo(Engine):
    name = "speedtest-go"

    def __init__(self, binary: str, home: str):
        self.binary, self.home = binary, home
        self._version = None

    def _env(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        return {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}

    def version(self):
        if self._version is None:
            try:
                out = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=10, env=self._env())
                parts = (out.stdout or out.stderr).split()          # it prints its version on stderr
                self._version = parts[1].lstrip("v") if len(parts) > 1 else None
            except (OSError, subprocess.SubprocessError):
                self._version = None
        return self._version

    def run(self, server_id=None, progress=None, timeout=150) -> EngineResult:
        if not os.path.exists(self.binary):
            return EngineResult(False, failure_code="engine_missing", failure_detail=self.binary)
        cmd = [self.binary, "--json"] + (["--server", str(server_id)] if server_id else [])
        if progress:
            progress("download", None, None, None)
        try:
            with shielded():
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self._env(),
                                        cwd=self.home)
            try:
                so, se = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            out = subprocess.CompletedProcess(cmd, proc.returncode, so, se)
        except subprocess.TimeoutExpired:
            return EngineResult(False, failure_code="timeout", failure_detail=f"no result after {timeout}s",
                                engine_version=self.version())
        except OSError as exc:
            return EngineResult(False, failure_code="engine_missing", failure_detail=str(exc)[:300])
        version = self.version()
        try:
            d = json.loads(out.stdout)
            s = d["servers"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            msg = (out.stderr or out.stdout or "").strip().splitlines()[-1:] or [f"exit {out.returncode}"]
            return EngineResult(False, failure_code=classify(msg[0]), failure_detail=msg[0][:500],
                                raw=json.dumps({"stderr": (out.stderr or "")[-1000:]}), engine_version=version)
        if not s.get("dl_speed"):
            return EngineResult(False, failure_code="engine_error", failure_detail="no download result",
                                raw=json.dumps(redact(d)), engine_version=version)
        ns = lambda v: (v / 1e6) if isinstance(v, (int, float)) and v else None
        pl = s.get("packet_loss") or {}
        loss = None
        if pl.get("sent") and pl.get("max") is not None:      # speedtest-go: loss = 1 - (sent - dup) / (max + 1)
            loss = max(0.0, 100.0 * (1 - (pl["sent"] - pl.get("dup", 0)) / (pl["max"] + 1)))
        dur = s.get("test_duration") or {}
        fields = {
            "download_mbps": s["dl_speed"] * 8 / 1e6, "upload_mbps": (s.get("ul_speed") or 0) * 8 / 1e6 or None,
            "download_ms": int(dur["download"] / 1e6) if dur.get("download") else None,
            "upload_ms": int(dur["upload"] / 1e6) if dur.get("upload") else None,
            "ping_ms": ns(s.get("latency")), "jitter_ms": ns(s.get("jitter")), "ping_low_ms": ns(s.get("min_latency")),
            "ping_high_ms": ns(s.get("max_latency")), "packet_loss_pct": loss,
            "server_id": str(s.get("id")) if s.get("id") else None, "server_sponsor": s.get("sponsor"),
            "server_location": s.get("name"),
            "server_name": " – ".join(x for x in (s.get("sponsor"), s.get("name")) if x) or None,
            "server_country": s.get("country") or None, "server_host": s.get("host") or None,
            "distance_km": float(s["distance"]) if s.get("distance") else None,
        }
        return EngineResult(True, fields=fields, raw=json.dumps(redact(d), separators=(",", ":")),
                            public_ip=(d.get("user_info") or {}).get("IP"), engine_version=version)
