"""The planned "start from scratch" reset (Settings, Fresh start), for example when a faster package goes live.

Nothing is deleted. The current database is copied with SQLite's backup API into var/archive/<stamp>-before-reset/,
the copy is verified (PRAGMA integrity_check and a full hash-chain verify), and only then is the live file moved
into the same folder. A fresh database starts with its own ledger from the genesis value. Settings carry over;
measurements, probes, events, annotations, flags, imports and exports do not. The new database records where its
predecessor went (file name, SHA-256, ledger head) in `settings.epoch` and the audit log, so the chain of custody
stays documented. Must run with the backend and agent stopped (scripts/epoch-reset.sh does that).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from . import canonical, integrity, settings_store, timeutil
from .db import Database


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive(settings, reason: str, label: str) -> tuple[Path, dict, dict]:
    """Copy the live database into var/archive/<stamp>-<label>/, verify the copy, then move the live files beside
    it. Returns (folder, info, stored settings). Raises SystemExit (nothing changed) if the copy doesn't verify."""
    live = settings.db_path
    stamp = timeutil.local(timeutil.now_ms(), "UTC").strftime("%Y%m%dT%H%M%SZ")
    folder = settings.data_dir / "archive" / f"{stamp}-{label}"
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    copy = folder / "speedmon.sqlite3"
    src = sqlite3.connect(live)
    dst = sqlite3.connect(copy)
    with dst:
        src.backup(dst)
    stored = {r[0]: r[1] for r in src.execute("SELECT key, value FROM settings")}
    src.close()
    dst.close()
    copy.chmod(0o600)
    check = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    check.row_factory = sqlite3.Row
    if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("archive copy failed PRAGMA integrity_check; nothing was changed")
    v = integrity.verify(check)
    counts = {t: check.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("measurements", "probes", "events", "annotations", "imports")}
    first_last = check.execute("SELECT min(t_ms), max(t_ms) FROM measurements").fetchone()
    check.close()
    if not v["ok"]:
        raise SystemExit("archive copy failed hash-chain verification; nothing was changed: " + "; ".join(v["problems"][:5]))
    digest = _sha256(copy)
    original = folder / "live-original"
    original.mkdir(mode=0o700)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(live) + suffix)
        if p.exists():
            shutil.move(str(p), original / p.name)
    info = {"at": timeutil.now_iso(), "reason": reason, "archive": f"archive/{folder.name}/speedmon.sqlite3",
            "archive_sha256": digest, "ledger_head": v["head"], "ledger_entries": v["ledger_entries"],
            "records": v["records"], "counts": counts, "first_ms": first_last[0], "last_ms": first_last[1]}
    (folder / "ARCHIVE.json").write_text(json.dumps(info, indent=1) + "\n")
    (folder / "README.txt").write_text(
        f"Archived evidence database ({reason}).\n"
        f"Archived {info['at']}. The copy was verified before the live file was moved here.\n"
        f"SHA-256 of speedmon.sqlite3: {digest}\nLedger head: {v['head']} ({v['ledger_entries']:,} entries)\n"
        "Check it any time:  python -m speedmon.cli restore-check <this folder>/speedmon.sqlite3\n")
    return folder, info, stored


def archive_and_reset(settings, reason: str = "planned reset") -> dict:
    live = settings.db_path
    folder, info, stored = _archive(settings, reason, "before-reset")
    db = Database(live)
    db.migrate()
    with db.tx() as conn:
        for key, value in stored.items():
            if key in ("data_origin", "fresh_start_at", "epoch"):     # one-shot: the new database doesn't re-trigger
                continue
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (key, value, timeutil.now_iso()))
        settings_store.set_internal(conn, "epoch", {**info, "epoch_ms": timeutil.now_ms()})
        conn.execute("INSERT INTO settings_history (key, old_value, new_value, changed_at, source) VALUES "
                     "('epoch', NULL, ?, ?, 'reset')", (json.dumps(info), timeutil.now_iso()))
        conn.execute("INSERT INTO audit_log (at, action, detail) VALUES (?, 'reset', ?)", (timeutil.now_iso(), json.dumps(info)))
    return info


# Tables carried over unchanged by rebuild_without(). Evidence tables are copied separately (filtered), and the
# derived tables are recomputed. `imports` describes the import runs, so it goes when their rows go.
CARRY = ("settings", "settings_history", "annotations", "annotation_history", "sessions", "audit_log", "commands",
         "agent_status", "exports", "quarantine")


def rebuild_without(settings, sources: tuple[str, ...], reason: str) -> dict:
    """Take records of the given `source`s (e.g. the legacy imports) out of the live database without breaking
    the evidence rules: the whole database is archived and verified first (as for a fresh start), then a new one
    is built from the archive with every other record copied exactly as stored (same content, record hashes and
    receive times) in the original ledger order, and the hash chain recomputed over them. The archive keeps the
    old chain; the audit log and `settings.rebuilds` say what was removed and where the original is.
    Must run with the backend and agent stopped."""
    from . import ingest
    if not sources or any(s in ("agent", "synthetic") for s in sources):
        raise SystemExit("only imported sources can be removed")
    live = settings.db_path
    folder, info, stored = _archive(settings, reason, "before-rebuild")
    old = folder / "speedmon.sqlite3"
    db = Database(live)
    db.migrate()
    q = ", ".join("?" * len(sources))
    conn = db.connect()
    try:
        conn.execute("ATTACH DATABASE ? AS old", (f"file:{old}?mode=ro",))
        conn.execute("BEGIN IMMEDIATE")
        removed = {"measurements": conn.execute(f"SELECT count(*) FROM old.measurements WHERE source IN ({q})", sources).fetchone()[0]}
        conn.execute("CREATE TEMP TABLE gone (uuid TEXT PRIMARY KEY)")
        conn.execute(f"INSERT INTO gone SELECT uuid FROM old.measurements WHERE source IN ({q})", sources)
        conn.execute("INSERT INTO gone SELECT uuid FROM old.diagnostics WHERE measurement_uuid IN (SELECT uuid FROM gone)")
        removed["diagnostics"] = conn.execute("SELECT count(*) FROM gone").fetchone()[0] - removed["measurements"]
        removed["imports"] = conn.execute("SELECT count(*) FROM old.imports").fetchone()[0]

        def columns(table):
            return ", ".join(r[1] for r in conn.execute(f"PRAGMA old.table_info({table})"))

        for table in ("measurements", "diagnostics", "probes", "events"):
            c = columns(table)
            conn.execute(f"INSERT INTO main.{table} ({c}) SELECT {c} FROM old.{table} "
                         f"WHERE uuid NOT IN (SELECT uuid FROM gone) ORDER BY id")
        for table in CARRY:
            c = columns(table)
            conn.execute(f"DELETE FROM main.{table}")
            conn.execute(f"INSERT INTO main.{table} ({c}) SELECT {c} FROM old.{table}")
        c = columns("measurement_flags")
        conn.execute(f"INSERT INTO main.measurement_flags ({c}) SELECT {c} FROM old.measurement_flags "
                     "WHERE measurement_uuid NOT IN (SELECT uuid FROM gone)")
        prev, idx = canonical.GENESIS, 0
        for row in conn.execute("SELECT stream, uuid, record_hash, hash_version, recorded_at FROM old.ledger "
                                "WHERE uuid NOT IN (SELECT uuid FROM gone) ORDER BY chain_index").fetchall():
            chain = canonical.chain_next(prev, row[0], row[1], row[2])
            conn.execute("INSERT INTO ledger (chain_index, stream, uuid, record_hash, prev_hash, chain_hash, hash_version, "
                         "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (idx, row[0], row[1], row[2], prev, chain, row[3], row[4]))
            prev, idx = chain, idx + 1
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE old")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    rec = {**info, "sources_removed": list(sources), "removed": removed, "new_ledger_entries": idx, "new_head": prev}
    with db.tx() as conn:
        ingest.rebuild_derived(conn)
        hist = json.loads(stored.get("rebuilds") or "[]")
        settings_store.set_internal(conn, "rebuilds", hist + [rec])
        settings_store.set_internal(conn, "include_legacy", False)
        conn.execute("INSERT INTO audit_log (at, action, detail) VALUES (?, 'rebuild_without', ?)", (timeutil.now_iso(), json.dumps(rec)))
    with db.read() as conn:
        v = integrity.verify(conn)
    if not v["ok"]:
        raise SystemExit("the rebuilt database does not verify (the archive is intact): " + "; ".join(v["problems"][:5]))
    return {**rec, "verified": True, "records": v["records"]}
