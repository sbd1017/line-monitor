"""A tiny summary of the latest test for other apps on the Pi (a home display, say).

Written after every test to SPEEDAGENT_LATEST_FILE (unset = not written), atomically (temp file + rename) and
world-readable (0644), so readers never see a half-written file and never need access to the agent's private
state directory. Only the whitelisted fields below go in: no IP addresses, server hosts, result URLs, ISP or failure
text. It is a convenience copy, not evidence; the backend database is the record.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from pathlib import Path

log = logging.getLogger("speedagent")
SCHEMA = 1


def _num(v, nd: int = 2):
    return round(float(v), nd) if isinstance(v, (int, float)) and math.isfinite(v) else None


def summary(rec: dict) -> dict:
    """The public part of one measurement record."""
    return {"at": rec.get("started_at"), "status": rec.get("status"), "kind": rec.get("kind"),
            "source": rec.get("source"), "engine": rec.get("engine"), "failure_code": rec.get("failure_code"),
            "download_mbps": _num(rec.get("download_mbps")), "upload_mbps": _num(rec.get("upload_mbps")),
            "ping_ms": _num(rec.get("ping_ms")), "jitter_ms": _num(rec.get("jitter_ms")),
            "server": (rec.get("server_name") or None) and str(rec["server_name"])[:80]}


def _previous_ok(path: Path) -> dict | None:
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
        return prev.get("last_ok") if isinstance(prev.get("last_ok"), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def write(path: Path, rec: dict, written_at: str) -> bool:
    """Replace `path` with the summary of `rec`. Never raises: a failure here must not affect measuring."""
    try:
        latest = summary(rec)
        doc = {"schema": SCHEMA, "written_at": written_at, "latest": latest,
               "last_ok": latest if latest["status"] == "ok" else _previous_ok(path)}
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".latest.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o644)                        # the unit's UMask is 0077; this file is meant to be read
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:
        log.warning("could not write %s: %s", path, exc)
        return False
