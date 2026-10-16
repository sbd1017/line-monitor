"""SQLite access and versioned migrations (speedmon/migrations/NNNN_name.sql, applied in order).

Same approach as /fin and /bets: one file on local disk inside the service's private data directory,
no network listener, every migration in its own transaction and recorded in schema_migrations.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA synchronous = FULL")         # evidence: a committed write survives power loss
        return conn

    @contextmanager
    def tx(self):
        """One atomic unit of work: BEGIN IMMEDIATE ... COMMIT (ROLLBACK on any error)."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> list[str]:
        """Apply pending migrations; returns the names applied now."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():                       # owner-only from the first byte, whatever the umask
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                         "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))")
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            applied = []
            for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                version = int(path.name[:4])
                if version in done:
                    continue
                sql = path.read_text()
                if re.search(r"\bBEGIN\s+(IMMEDIATE|EXCLUSIVE|DEFERRED|TRANSACTION)\b|\bBEGIN\s*;|\bCOMMIT\b",
                             re.sub(r"--[^\n]*", "", sql), re.I):
                    raise RuntimeError(f"{path.name}: migrations must not manage transactions")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _statements(sql):
                        conn.execute(statement)
                    conn.execute("INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (version, path.stem))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                applied.append(path.stem)
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise RuntimeError(f"foreign key check failed: {len(fk)} rows")
            return applied
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.read() as conn:
            row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
            return row[0] or 0


def _statements(sql: str):
    """Split a migration into statements. Comments are removed first; migrations must not put
    '--' or ';' inside string literals. CREATE TRIGGER ... BEGIN ... END; is kept whole."""
    body = re.sub(r"--[^\n]*", "", sql)
    buf: list[str] = []
    depth = 0                                         # inside CREATE TRIGGER ... BEGIN ... END
    for part in body.split(";"):
        buf.append(part)
        depth += len(re.findall(r"\bBEGIN\b", part, re.I)) - len(re.findall(r"\bEND\b", part, re.I))
        if depth <= 0:
            stmt = ";".join(buf).strip()
            if stmt:
                yield stmt
            buf, depth = [], 0
    if "".join(buf).strip():
        yield ";".join(buf).strip()
