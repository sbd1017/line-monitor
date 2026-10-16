"""Signed HTTP to the backend (stdlib only). Each request carries:
  X-Agent-Id, X-Agent-Time (ms), X-Agent-Nonce, X-Agent-Signature =
  hex HMAC-SHA256(key, METHOD \\n PATH \\n time \\n nonce \\n sha256(body))
The backend refuses stale times (±5 min), reused nonces, and anything not from loopback.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class UplinkError(Exception):
    pass


class Uplink:
    def __init__(self, base: str, agent_id: str, key: bytes, timeout: float = 15):
        self.base, self.agent_id, self.key, self.timeout = base.rstrip("/"), agent_id, key, timeout

    def _request(self, method: str, path: str, payload=None) -> dict:
        url = self.base + path
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        ts = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        msg = "\n".join([method, urlsplit(url).path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()
        sig = hmac.new(self.key, msg, hashlib.sha256).hexdigest()
        req = urllib.request.Request(url, data=body if method == "POST" else None, method=method, headers={
            "Content-Type": "application/json", "X-Agent-Id": self.agent_id, "X-Agent-Time": ts, "X-Agent-Nonce": nonce,
            "X-Agent-Signature": sig, "User-Agent": "speedagent"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise UplinkError(f"http-{exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise UplinkError(f"network: {type(exc).__name__}") from None

    def send(self, items: list[dict]) -> dict:
        return self._request("POST", "/records", {"items": items})

    def status(self, status: dict) -> dict:
        return self._request("POST", "/status", status)

    def config(self) -> dict:
        return self._request("GET", "/config")
