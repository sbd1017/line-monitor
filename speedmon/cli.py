"""Admin commands: python -m speedmon.cli <command>  (run as the speedmon user in production).

  set-phrase            set the entrance phrase (typed at a hidden prompt; ends all sessions)
  import-legacy         import the old speedtest-cli CSV/XLSX history (files are only read)
  verify                full integrity check (re-hash every record, walk the chain)
  head                  print the ledger head (write it down / keep it somewhere else)
  backup [--keep N]     consistent online copy of the database into var/backups/
  restore-check FILE    open a backup read-only and verify it
  rebuild-derived       recompute rollups and anomaly scores (derived data only)
  stats                 counts per table
  pause HOURS / resume  pause full tests (probes keep running)
  seed-synthetic        development only: fill the database with years of synthetic data
  fresh-start-due       exit 0 if the planned fresh start (Settings) is due, 3 if not
  reset --yes           archive the whole database (verified copy) and start a fresh one: only with the backend and
                        agent stopped; normally run by speedmon-epoch-reset.service
  remove-legacy --yes   archive the database (verified copy), then rebuild it without the imported speedtest-cli rows;
                        only with the backend and agent stopped
"""
from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
import sys
from pathlib import Path

from . import auth, importer, ingest, integrity, settings_store, timeutil
from .config import load_settings
from .db import Database


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="speedmon")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-phrase")
    imp = sub.add_parser("import-legacy")
    imp.add_argument("--csv", type=Path)
    imp.add_argument("--xlsx", type=Path)
    sub.add_parser("verify")
    sub.add_parser("head")
    b = sub.add_parser("backup")
    b.add_argument("--keep", type=int, default=60)
    rc = sub.add_parser("restore-check")
    rc.add_argument("file", type=Path)
    sub.add_parser("rebuild-derived")
    sub.add_parser("stats")
    pa = sub.add_parser("pause")
    pa.add_argument("hours", type=float)
    sub.add_parser("resume")
    sub.add_parser("fresh-start-due")
    rs = sub.add_parser("reset")
    rs.add_argument("--yes", action="store_true")
    rs.add_argument("--reason", default="planned reset: Gig1 goes live")
    rl = sub.add_parser("remove-legacy")
    rl.add_argument("--yes", action="store_true")
    sd = sub.add_parser("seed-synthetic")
    sd.add_argument("--days", type=int, default=400)
    sd.add_argument("--interval", type=int, default=30)
    sd.add_argument("--probe-interval", type=int, default=60)
    sd.add_argument("--no-probes", action="store_true")
    args = p.parse_args(argv)

    s = load_settings()
    s.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = Database(s.db_path)
    db.migrate()

    if args.cmd == "set-phrase":
        a = getpass.getpass("New entrance phrase (12+ characters): ")
        if a != getpass.getpass("Again: "):
            print("They don't match. Nothing changed.")
            return 1
        auth.write_phrase_hash(s.phrase_file, a)
        n = auth.Sessions(db, 1, 1).end_all()
        print(f"Phrase set. {n} session(s) ended.")
        return 0
    if args.cmd == "import-legacy":
        with db.tx() as conn:
            tz = importer.tz_of(conn)
            if args.csv:
                print("CSV:", importer.import_csv(conn, args.csv, tz))
            if args.xlsx:
                print("XLSX:", importer.import_xlsx(conn, args.xlsx, tz))
        return 0
    if args.cmd == "verify":
        with db.read() as conn:
            res = integrity.verify(conn, progress=lambda st, n: print(f"  {st}: {n:,} records re-hashed", file=sys.stderr))
        print(json.dumps({k: v for k, v in res.items() if k != "problems"}, indent=1))
        for prob in res["problems"]:
            print("PROBLEM:", prob)
        print("RESULT:", "OK" if res["ok"] else "FAILED")
        return 0 if res["ok"] else 2
    if args.cmd == "head":
        with db.read() as conn:
            h = integrity.head(conn)
        print(f"{h['head']}  ({h['entries']:,} ledger entries, last at {h['at']})")
        return 0
    if args.cmd == "backup":
        return backup(s, args.keep)
    if args.cmd == "restore-check":
        conn = sqlite3.connect(f"file:{args.file}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        res = integrity.verify(conn)
        print(json.dumps({k: v for k, v in res.items() if k != "problems"}, indent=1))
        print("RESULT:", "OK" if res["ok"] else "FAILED")
        return 0 if res["ok"] else 2
    if args.cmd == "rebuild-derived":
        with db.tx() as conn:
            print(ingest.rebuild_derived(conn))
        return 0
    if args.cmd == "stats":
        with db.read() as conn:
            for t in ("measurements", "diagnostics", "probes", "events", "ledger", "quarantine", "annotations",
                      "measurement_flags", "imports", "exports"):
                print(f"{t:20s} {conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:>12,}")
            print("data_origin", settings_store.get_all(conn).get("data_origin", "real"))
        return 0
    if args.cmd in ("pause", "resume"):
        with db.tx() as conn:
            until = None if args.cmd == "resume" else timeutil.iso(timeutil.now_ms() + int(args.hours * 3600_000))
            settings_store.save(conn, {"paused_until": until}, "cli")
        print("Resumed." if until is None else f"Full tests paused until {until} (UTC). Probes keep running.")
        return 0
    if args.cmd == "fresh-start-due":
        with db.read() as conn:
            at = settings_store.get_all(conn).get("fresh_start_at")
        due = bool(at) and timeutil.parse_iso_ms(at) <= timeutil.now_ms()
        print(f"due ({at})" if due else f"not due ({at or 'none planned'})")
        return 0 if due else 3
    if args.cmd == "reset":
        if not args.yes:
            print("This archives everything and starts a fresh database. Re-run with --yes.")
            return 1
        from . import reset
        info = reset.archive_and_reset(s, args.reason)
        print(json.dumps(info, indent=1))
        return 0
    if args.cmd == "remove-legacy":
        if not args.yes:
            print("This archives the database and rebuilds it without the imported rows. Re-run with --yes.")
            return 1
        from . import reset
        info = reset.rebuild_without(s, ("legacy_csv", "legacy_xlsx"), "imported speedtest-cli history removed at the owner's request")
        print(json.dumps({k: info[k] for k in ("archive", "archive_sha256", "removed", "new_ledger_entries", "new_head", "verified")}, indent=1))
        return 0
    if args.cmd == "seed-synthetic":
        if s.is_production:
            print("Refusing: synthetic data is never written in production.")
            return 1
        from . import synthetic
        with db.tx() as conn:
            print(synthetic.seed(conn, days=args.days, interval_min=args.interval, probe_interval_s=args.probe_interval,
                                 probes=not args.no_probes))
        return 0
    return 1


def backup(s, keep: int) -> int:
    """Consistent online backup (SQLite backup API), then a verification of the copy. Keeps the newest `keep`."""
    s.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = timeutil.local(timeutil.now_ms(), "UTC").strftime("%Y%m%dT%H%M%SZ")
    target = s.backup_dir / f"speedmon-{stamp}.sqlite3"
    src = sqlite3.connect(s.db_path)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    target.chmod(0o600)
    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    check.row_factory = sqlite3.Row
    ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    head = integrity.head(check)
    check.close()
    (s.backup_dir / f"speedmon-{stamp}.head").write_text(json.dumps(head) + "\n")
    backups = sorted(s.backup_dir.glob("speedmon-*.sqlite3"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        old.with_suffix(".head").unlink(missing_ok=True)
    print(f"backup {target.name}: sqlite integrity {'ok' if ok else 'FAILED'}, ledger head {head['head'][:16]}… "
          f"({head['entries']:,} entries); keeping {min(len(backups), keep)}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
