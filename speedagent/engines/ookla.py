"""Ookla Speedtest CLI (official). Runs with -f jsonl -p yes so progress can be shown live.
Bandwidth in Ookla's output is bytes per second; Mbps = bandwidth * 8 / 1e6. Latencies are milliseconds.
The licence was accepted by the owner; acceptance is stored under the agent's private HOME."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from .base import Engine, EngineResult, classify, redact, shielded


class Ookla(Engine):
    name = "ookla"

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
                first = out.stdout.splitlines()[0] if out.stdout else ""
                self._version = first.split("Ookla")[1].split()[0] if "Ookla" in first else first[:40] or None
            except (OSError, subprocess.SubprocessError, IndexError):
                self._version = None
        return self._version

    def run(self, server_id=None, progress=None, timeout=150) -> EngineResult:
        if not os.path.exists(self.binary):
            return EngineResult(False, failure_code="engine_missing", failure_detail=self.binary)
        cmd = [self.binary, "--accept-license", "--accept-gdpr", "-f", "jsonl", "-p", "yes", "--selection-details"]
        if server_id:
            cmd += ["-s", str(server_id)]
        try:
            with shielded():
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self._env())
        except OSError as exc:
            return EngineResult(False, failure_code="engine_missing", failure_detail=str(exc)[:300])
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        result, errors, started = None, [], time.monotonic()
        try:
            for line in proc.stdout:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("type")
                if t == "result":
                    result = d
                elif t == "log" and d.get("level") in ("error", "warning"):
                    errors.append(d.get("message", ""))
                elif progress and t in ("ping", "download", "upload"):
                    p = d.get(t, {})
                    mbps = p.get("bandwidth", 0) * 8 / 1e6 if t != "ping" else None
                    progress(t, p.get("progress"), mbps, p.get("latency") if t == "ping" else None)
            stderr = proc.stderr.read()
            code = proc.wait()
        finally:
            killer.cancel()
        version = self.version()
        if result is None:
            lines = [ln.strip() for ln in (stderr or "").splitlines()
                     if ln.strip() and set(ln.strip()) != {"="} and "licen" not in ln.lower() and "http" not in ln]
            msg = "; ".join(errors) or (lines[-1] if lines else "")
            if time.monotonic() - started >= timeout - 1:
                return EngineResult(False, failure_code="timeout", failure_detail=f"no result after {timeout}s",
                                    raw=json.dumps({"errors": errors[-5:]}), engine_version=version)
            return EngineResult(False, failure_code=classify(msg), failure_detail=(msg or f"exit {code}")[:500],
                                raw=json.dumps({"errors": errors[-5:], "exit": code}), engine_version=version)
        try:
            ping, dl, ul, srv = result.get("ping", {}), result["download"], result["upload"], result.get("server", {})
            iface = result.get("interface", {})
            fields = {
                "download_mbps": dl["bandwidth"] * 8 / 1e6, "upload_mbps": ul["bandwidth"] * 8 / 1e6,
                "download_bytes": dl.get("bytes"), "upload_bytes": ul.get("bytes"),
                "download_ms": dl.get("elapsed"), "upload_ms": ul.get("elapsed"),
                "ping_ms": ping.get("latency"), "jitter_ms": ping.get("jitter"), "ping_low_ms": ping.get("low"),
                "ping_high_ms": ping.get("high"), "dl_latency_ms": (dl.get("latency") or {}).get("iqm"),
                "ul_latency_ms": (ul.get("latency") or {}).get("iqm"),
                "packet_loss_pct": float(result["packetLoss"]) if isinstance(result.get("packetLoss"), (int, float)) else None,
                "server_id": str(srv.get("id")) if srv.get("id") is not None else None,
                "server_sponsor": srv.get("name"), "server_location": srv.get("location"),
                "server_name": " – ".join(x for x in (srv.get("name"), srv.get("location")) if x) or None,
                "server_country": srv.get("country"), "server_host": srv.get("host"),
                "result_id": (result.get("result") or {}).get("id"), "result_url": (result.get("result") or {}).get("url"),
                "isp": result.get("isp"),
            }
        except (KeyError, TypeError) as exc:
            return EngineResult(False, failure_code="parse_error", failure_detail=str(exc)[:300],
                                raw=json.dumps(redact(result)), engine_version=version)
        return EngineResult(True, fields=fields, raw=json.dumps(redact(result), separators=(",", ":")),
                            public_ip=iface.get("externalIp"), engine_version=version)
