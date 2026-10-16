"""The agent's local store: an outbox of evidence records waiting to be delivered, plus small state values.

Every record is written here first (synchronous=FULL), with its sequence number and hash, before any network
activity. Delivery only marks a record 'sent' after the backend acknowledges it (accepted or duplicate), so a
crash, reboot or unreachable backend never loses a record. Measurements stay here permanently as a second
copy; sent probes and events are pruned after 60 days (the backend and its backups hold them).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    seq          INTEGER PRIMARY KEY,
    stream       TEXT NOT NULL,
    uuid         TEXT NOT NULL UNIQUE,
    record       TEXT NOT NULL,
    record_hash  TEXT NOT NULL,
    raw          TEXT,
    created_ms   INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    sent_ms      INTEGER
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (status, seq);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.executescript(SCHEMA)

    # ---------------------------------------------------------- state

    def get(self, key: str, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                              (key, json.dumps(value)))

    # ---------------------------------------------------------- outbox

    def add(self, stream: str, record: dict, raw: str | None = None) -> dict:
        """Assign the next sequence number, hash and store the record. Returns the stored record."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                seq = (self.get("next_seq", 1))
                rec = dict(record)
                rec["agent_seq"] = seq
                if stream == "measurement":
                    rec["raw_sha256"] = canonical.sha256_text(raw)
                rec = canonical.normalise(stream, rec)
                h = canonical.record_hash(stream, rec)
                self.conn.execute("INSERT INTO outbox (seq, stream, uuid, record, record_hash, raw, created_ms) "
                                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (seq, stream, rec["uuid"], json.dumps(rec), h, raw, int(time.time() * 1000)))
                self.conn.execute("INSERT INTO kv (key, value) VALUES ('next_seq', ?) ON CONFLICT(key) DO UPDATE SET "
                                  "value = excluded.value", (json.dumps(seq + 1),))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return rec

    def pending(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT seq, stream, uuid, record, record_hash, raw FROM outbox WHERE status = 'pending' "
                                     "ORDER BY seq LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark(self, uuids_ok: list[str], rejected: dict[str, str]) -> None:
        now = int(time.time() * 1000)
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.executemany("UPDATE outbox SET status = 'sent', sent_ms = ?, attempts = attempts + 1 WHERE uuid = ?",
                                  [(now, u) for u in uuids_ok])
            self.conn.executemany("UPDATE outbox SET status = 'rejected', last_error = ?, attempts = attempts + 1 WHERE uuid = ?",
                                  [(e[:500], u) for u, e in rejected.items()])
            self.conn.execute("COMMIT")

    def failed_attempt(self, seqs: list[int], error: str) -> None:
        with self.lock:
            self.conn.executemany("UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE seq = ?",
                                  [(error[:500], s) for s in seqs])

    def queue_stats(self) -> dict:
        with self.lock:
            r = self.conn.execute("SELECT count(*), min(created_ms) FROM outbox WHERE status = 'pending'").fetchone()
            rej = self.conn.execute("SELECT count(*) FROM outbox WHERE status = 'rejected'").fetchone()[0]
            last = self.conn.execute("SELECT max(seq) FROM outbox WHERE status = 'sent'").fetchone()[0]
        return {"queue_depth": r[0], "oldest_unsent_ms": r[1], "rejected_local": rej, "last_sent_seq": last}

    def prune(self, keep_days: int = 60) -> int:
        cutoff = int(time.time() * 1000) - keep_days * 86_400_000
        with self.lock:
            return self.conn.execute("DELETE FROM outbox WHERE status = 'sent' AND stream IN ('probe', 'event') AND sent_ms < ?",
                                     (cutoff,)).rowcount
