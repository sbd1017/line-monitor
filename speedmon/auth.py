"""Phrase entrance and sessions for /speedtest (same scheme as /fin).

Phrase
  * Never in source, templates, git or logs. Only a salted scrypt hash is stored, in
    ``var/entrance.json`` (0600, service user), written by ``python -m speedmon.cli set-phrase``.
  * Compared exactly as typed, after Unicode NFC (so the same phrase typed on a Mac and an
    iPhone matches). No trimming or case folding: every character counts.
  * When no phrase has been set, nothing can get in (a dummy hash is still computed so the
    response takes the same time).

Sessions
  * Server-side: the cookie holds 256 random bits; the database stores only its SHA-256, with
    idle and absolute expiry. Signing out deletes the row, so a copied cookie stops working too.
  * Cookie: HttpOnly, Secure (production), SameSite=Strict, Path=/speedtest.
  * Each session carries its own CSRF token for every non-GET request.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import timeutil

N, R, P = 2 ** 15, 8, 1                          # ~32 MB, ~0.1 s per check on a Pi 5
MIN_PHRASE, MAX_PHRASE = 12, 256
COOKIE = "speedtest_session"
_MAXMEM = 128 * 1024 * 1024


def normalise(phrase: str) -> str:
    return unicodedata.normalize("NFC", phrase)


def _kdf(phrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(normalise(phrase).encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=_MAXMEM)


def hash_phrase(phrase: str) -> str:
    if len(phrase) < MIN_PHRASE:
        raise ValueError(f"Use at least {MIN_PHRASE} characters")
    if len(phrase) > MAX_PHRASE:
        raise ValueError(f"Use at most {MAX_PHRASE} characters")
    salt = secrets.token_bytes(16)
    digest = _kdf(phrase, salt, N, R, P)
    return "scrypt${}${}${}${}${}".format(N, R, P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


_DUMMY = None


def verify_phrase(phrase: str, stored: str | None) -> bool:
    """Constant work whether or not a hash is configured or the input is malformed."""
    global _DUMMY
    if not isinstance(phrase, str) or len(phrase) > MAX_PHRASE:
        phrase = ""
    try:
        scheme, n, r, p, salt, digest = (stored or "").split("$")
        if scheme != "scrypt":
            raise ValueError
        calc = _kdf(phrase, base64.b64decode(salt), int(n), int(r), int(p))
        return bool(phrase) and hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        if _DUMMY is None:
            _DUMMY = secrets.token_bytes(16)
        _kdf(phrase or "x", _DUMMY, N, R, P)
        return False


def load_phrase_hash(path: Path) -> str | None:
    try:
        data = json.loads(Path(path).read_text())
        value = data.get("phrase_hash")
        return value if isinstance(value, str) and value.startswith("scrypt$") else None
    except (OSError, ValueError, AttributeError):
        return None


def write_phrase_hash(path: Path, phrase: str) -> None:
    data = {"phrase_hash": hash_phrase(phrase), "changed_at": timeutil.now_iso()}
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# ---------------------------------------------------------------- sessions

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Sessions:
    def __init__(self, db, idle_minutes: int, absolute_hours: int):
        self.db, self.idle, self.absolute = db, timedelta(minutes=idle_minutes), timedelta(hours=absolute_hours)

    def create(self) -> tuple[str, dict]:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        row = {"id_hash": _digest(token), "csrf": secrets.token_urlsafe(24), "created_at": _iso(now),
               "last_seen_at": _iso(now), "expires_at": _iso(now + self.absolute)}
        with self.db.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
            conn.execute("INSERT INTO sessions (id_hash, csrf, created_at, last_seen_at, expires_at) "
                         "VALUES (:id_hash, :csrf, :created_at, :last_seen_at, :expires_at)", row)
        return token, row

    def lookup(self, token: str | None) -> dict | None:
        """The live session for this cookie value, refreshing its idle timer; None if absent/expired."""
        if not token or len(token) > 128:
            return None
        h = _digest(token)
        now = datetime.now(timezone.utc)
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id_hash = ?", (h,)).fetchone()
            if not row:
                return None
            idle_cutoff = _iso(now - self.idle)
            if row["expires_at"] <= _iso(now) or row["last_seen_at"] < idle_cutoff:
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (h,))
                return None
            # Write at most once a minute so reads stay cheap.
            if row["last_seen_at"] < _iso(now - timedelta(seconds=60)):
                conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id_hash = ?", (_iso(now), h))
            return dict(row)

    def end(self, token: str | None) -> None:
        if token:
            with self.db.tx() as conn:
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (_digest(token),))

    def end_all(self) -> int:
        with self.db.tx() as conn:
            return conn.execute("DELETE FROM sessions").rowcount

    def count(self) -> int:
        with self.db.read() as conn:
            return conn.execute("SELECT count(*) FROM sessions WHERE expires_at > ?",
                                (_iso(datetime.now(timezone.utc)),)).fetchone()[0]


class EntranceLimiter:
    """Refuse phrase attempts after too many failures from one IP, or overall (spread guessing).
    Memory only; a restart clears it (nginx adds its own per-IP rate limit in front)."""

    def __init__(self, per_ip: int = 5, window: int = 900, overall: int = 30):
        self.per_ip, self.window, self.overall = per_ip, window, overall
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.monotonic()
        recent = [t for t in self.failures.get(key, []) if now - t < self.window]
        if recent:
            self.failures[key] = recent
        else:
            self.failures.pop(key, None)
        return recent

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.per_ip or len(self._recent("*")) >= self.overall

    def fail(self, ip: str) -> None:
        for key in (ip, "*"):
            self.failures.setdefault(key, []).append(time.monotonic())

    def reset(self, ip: str) -> None:
        self.failures.pop(ip, None)
