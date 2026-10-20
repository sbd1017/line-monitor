"""Build the demo database: three years of generated history ending now, with the owner's layer (package history,
notes, a couple of flags) filled in as a real owner would have.

    python -m demo.generate --data-dir DIR [--anchor YYYY-MM-DD] [--end-ms MS] [--seed N] [--verify]

Writes DIR/speedmon.sqlite3.new and DIR/demo-state.json.new, then (unless --no-swap) moves them into place. Records
go through the app's own canonical form and hash chain, so Integrity → Verify passes. Every row is
source='synthetic' and the database is marked as synthetic: a production instance refuses to start on it.
Deterministic: the same anchor, end time and seed give byte-for-byte the same records and ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from speedmon import canonical, ingest, integrity, settings_store, timeutil
from speedmon.db import Database

from .simulate import AGENT_ID, Sim
from .world import DAY, HOUR, MIN, TZ, World, rng, rng_uuid

FLUSH = 20_000


class BulkWriter:
    """Appends records the way the backend would (record hash, ledger chain), in large batches."""

    def __init__(self, conn, seed: int):
        self.conn, self.seed = conn, seed
        self.buf: list = []
        head = conn.execute("SELECT chain_index, chain_hash FROM ledger ORDER BY chain_index DESC LIMIT 1").fetchone()
        self.idx, self.prev = (head[0], head[1]) if head else (-1, canonical.GENESIS)
        self.counts = {k: 0 for k in ingest.STREAM_TABLE}

    def __call__(self, stream: str, rec: dict, raw: str | None = None) -> None:
        self.buf.append((stream, rec, raw))
        if len(self.buf) >= FLUSH:
            self.flush()

    def flush(self) -> None:
        by_stream: dict = {k: [] for k in ingest.STREAM_TABLE}
        ledger = []
        for stream, rec, raw in self.buf:
            rec = canonical.normalise(stream, rec)
            h = canonical.record_hash(stream, rec)
            received = timeutil.iso(rec["t_ms"] + 900 + (rec["agent_seq"] % 7) * 130)   # sent within a second or two
            row = [rec[k] for k in canonical.FIELDS[stream]] + [h, received]
            if stream == "measurement":
                row.append(raw)
            by_stream[stream].append(row)
            self.idx += 1
            chain = canonical.chain_next(self.prev, stream, rec["uuid"], h)
            ledger.append((self.idx, stream, rec["uuid"], h, self.prev, chain, canonical.HASH_VERSION, received))
            self.prev = chain
        for stream, rows in by_stream.items():
            if not rows:
                continue
            cols = list(canonical.FIELDS[stream]) + ["record_hash", "received_at"] + (["raw"] if stream == "measurement" else [])
            self.conn.executemany(f"INSERT INTO {ingest.STREAM_TABLE[stream]} ({', '.join(cols)}) VALUES "
                                  f"({', '.join('?' * len(cols))})", rows)
            self.counts[stream] += len(rows)
        self.conn.executemany("INSERT INTO ledger (chain_index, stream, uuid, record_hash, prev_hash, chain_hash, hash_version, "
                              "recorded_at) VALUES (?,?,?,?,?,?,?,?)", ledger)
        self.buf = []


def owner_layer(conn, w: World, end_ms: int) -> None:
    """Settings, package history, notes and flags, as the owner of this line would have left them."""
    iso = timeutil.iso
    first = w.start_ms
    settings_store.set_internal(conn, "data_origin", "synthetic")
    pk_old, pk_new = w.packages
    put = lambda key, value, at: conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at", (key, json.dumps(value), iso(at)))
    hist = lambda key, old, new, at, src="settings": conn.execute(
        "INSERT INTO settings_history (key, old_value, new_value, changed_at, source) VALUES (?, ?, ?, ?, ?)",
        (key, None if old is None else json.dumps(old), json.dumps(new), iso(at), src))
    audit = lambda at, action, detail=None: conn.execute("INSERT INTO audit_log (at, action, detail) VALUES (?, ?, ?)",
                                                         (iso(at), action, json.dumps(detail or {})))
    t_setup = first - 2 * HOUR
    put("packages", [pk_old], t_setup)
    hist("packages", None, [pk_old], t_setup)
    put("default_range", "30d", t_setup + 5 * MIN)
    hist("default_range", "7d", "30d", t_setup + 5 * MIN)
    audit(t_setup, "settings_changed", {"keys": ["packages"]})
    t_up = w.ms(w.gig1_day) - 2 * DAY + 20 * HOUR + 14 * MIN
    put("packages", [pk_old, pk_new], t_up)
    hist("packages", [pk_old], [pk_old, pk_new], t_up)
    audit(t_up, "settings_changed", {"keys": ["packages"]})

    notes = []
    note = lambda start, end, title, body, cat: notes.append((start, end, title, body, cat))
    d = lambda back, hh=12, mm=0: w.at(w.end_day - timedelta(days=back), hh, mm)
    note(first, None, "Monitor started", "Pi 5 wired straight into the Hub (modem mode), the router behind it. Speed tests "
         "every 30 minutes at a random moment, quick checks every minute.", "note")
    note(w.ms(w.gig1_day) + 9 * HOUR + 40 * MIN, None, "Upgraded to Gig1",
         "New Hub 5 installed this morning. The Pi's own port tops out around 940 Mbps, so results sit at the monitor's "
         "limit until I fit a faster adapter.", "isp")
    note(w.ms(w.adapter_day) + 11 * HOUR, None, "2.5 GbE USB adapter fitted",
         "The monitor can now measure the whole Gig1 speed, not just what a 1 GbE port can carry.", "hardware")
    for s in w.events:
        if s.kind == "slowlink":
            note(s.a, s.b, "Bad Ethernet cable", "The Pi's link came up at 100 Mb/s. Results in this window show the cable, "
                 "not the line (flagged automatically). Replaced the patch lead.", "hardware")
        elif s.kind == "offline" and s.info.get("why") == "power cut":
            note(s.a, s.b, "Power cut", "Whole street lost power. Nothing was measured while the Pi was off.", "other")
        elif s.kind == "offline" and "moved" in s.info.get("why", ""):
            note(s.a, s.b, "Router and Pi moved", "Moved everything to the front room. Monitor off for the afternoon.", "hardware")
        elif s.kind == "outage" and s.b - s.a > 3 * HOUR:
            note(s.a, s.b, "Area outage", f"The ISP's status page confirmed it: {s.info.get('why', 'no reason given')}.",
                 "outage")
        elif s.kind == "outage" and "maintenance" in s.info.get("why", ""):
            note(s.a, s.b, "Planned maintenance", "Notified by the ISP a week before.", "isp")
        elif s.kind == "lan":
            why = s.info.get("why", "router restarted")
            note(s.a, s.b, "Router restart", f"{why[0].upper()}{why[1:]}. A local fault: it doesn't count against the ISP.",
                 "reboot")
    f = w.fault
    note(f.a + 15 * HOUR, None, "Reported slow speeds",
         "Phoned the ISP: speeds well under the guarantee since last night. Fault reference 41-77302. They see noise on "
         "the upstream and are booking an engineer.", "complaint")
    note(f.b - 4 * HOUR, f.b - 45 * MIN, "Engineer visit",
         "Engineer found a corroded tap at the street cabinet and replaced it. Speeds back to normal by lunchtime.", "engineer")
    note(f.b + 2 * DAY + 10 * HOUR, None, "Guarantee claim",
         "Speeds were below the minimum guaranteed speed on more than three days in a row. Sent the export (XLSX + ledger "
         "head) with the claim; credit applied to the next bill.", "complaint")
    note(d(33, 19, 30), d(33, 22, 30), "Big match night", "Evening speeds dipped as expected: the whole street streaming.", "note")
    for start, end, title, body, cat in notes:
        r = rng(w.seed, "note", title, start)
        created = min(end_ms - HOUR, (end or start) + int(r.uniform(0.2, 6) * HOUR))
        uid = rng_uuid(r)
        cur = conn.execute("INSERT INTO annotations (uuid, start_ms, end_ms, title, body, category, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (uid, start, end, title, body, cat, iso(created), iso(created)))
        conn.execute("INSERT INTO annotation_history (annotation_id, at, action, snapshot) VALUES (?, ?, 'create', ?)",
                     (cur.lastrowid, iso(created), json.dumps({"title": title, "body": body, "category": cat,
                                                               "start_ms": start, "end_ms": end})))

    # two tests the owner set aside (with the reason) and one disputed
    busy = conn.execute("SELECT m.uuid, m.t_ms FROM measurements m JOIN diagnostics d ON d.measurement_uuid = m.uuid "
                        "WHERE d.background_rx_mbps > 60 AND m.kind = 'scheduled' ORDER BY m.t_ms DESC LIMIT 2").fetchall()
    for uid, t in busy:
        conn.execute("INSERT INTO measurement_flags (measurement_uuid, flag, reason, created_at) VALUES (?, 'exclude', ?, ?)",
                     (uid, "A game update was downloading on the Pi's switch port during this test.", iso(t + 3 * HOUR)))
        audit(t + 3 * HOUR, "flag_added", {"uuid": uid, "flag": "exclude"})
    slow = conn.execute("SELECT uuid, t_ms FROM measurements WHERE status = 'ok' AND kind = 'scheduled' AND t_ms >= ? AND "
                        "t_ms < ? ORDER BY download_mbps LIMIT 1", (f.a, f.b)).fetchone()
    if slow:
        conn.execute("INSERT INTO measurement_flags (measurement_uuid, flag, reason, created_at) VALUES (?, 'note', ?, ?)",
                     (slow[0], "Lowest result of the fault week; quoted in the complaint.", iso(slow[1] + 20 * HOUR)))
    for k in range(0, 1096, 91):
        t = w.start_ms + k * DAY + 21 * HOUR
        if t < end_ms:
            audit(t, "export_requested", {"format": "xlsx", "range": "last 90 days"})


def agent_status_row(conn, sim: Sim, end_ms: int) -> None:
    lp = sim.last_probe or {}
    st = sim.policy
    status = {"state": "idle", "phase": None, "progress": None, "live_mbps": None, "queue_depth": 0, "oldest_unsent_ms": None,
              "rejected_local": 0, "version": "1.0.0", "engine": "ookla", "mode": st.mode, "tests_today": st.tests_today,
              "cap": 96, "paused": False, "next_test_ms": None, "next_kind": "scheduled", "next_scheduled_ms": None,
              "interval_min": 30, "link": {"speed": sim.w.link_speed(end_ms), "duplex": "full", "state": "up",
                                            "iface": sim.w.iface(end_ms)},
              "last_probe": {"t": lp.get("t"), "state": lp.get("state"), "ext": lp.get("ext"), "gw": lp.get("gw"),
                             "loss": lp.get("loss")},
              "last_error": None, "uptime_s": round((end_ms - sim.boot["ms"]) / 1000) if sim.boot else None,
              "throttled": "0x0"}
    from speedagent import policy
    d = policy.decide(AGENT_ID, sim.cfg(end_ms), policy.State.from_dict(st.to_dict()), end_ms, sim.today(end_ms), False, False)
    status["next_test_ms"] = d.get("until") if d["action"] == "wait" else end_ms
    status["next_kind"] = d.get("kind")
    status["next_scheduled_ms"] = d.get("next_scheduled_ms")
    conn.execute("INSERT INTO agent_status (agent_id, last_seen_ms, status) VALUES (?, ?, ?) ON CONFLICT(agent_id) DO UPDATE "
                 "SET last_seen_ms = excluded.last_seen_ms, status = excluded.status", (AGENT_ID, end_ms, json.dumps(status)))


def build(data_dir: Path, anchor: date, end_ms: int, seed: int, log=print, days: int = 1096) -> dict:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db_new = data_dir / "speedmon.sqlite3.new"
    for p in (db_new, Path(str(db_new) + "-wal"), Path(str(db_new) + "-shm"), Path(str(db_new) + ".ok")):
        p.unlink(missing_ok=True)
    w = World(anchor, seed, days)
    if not w.start_ms < end_ms <= w.ms(w.end_day):
        raise SystemExit(f"end time must fall on the anchor day {anchor} (local)")
    db = Database(db_new)
    db.migrate()
    t = time.monotonic()
    with db.tx() as conn:
        put = BulkWriter(conn, seed)
        sim = Sim(w)
        sim.run(end_ms, put)
        put.flush()
        log(f"records {put.counts} in {time.monotonic() - t:.0f}s")
        owner_layer(conn, w, end_ms)
        derived = ingest.rebuild_derived(conn)
        agent_status_row(conn, sim, end_ms)
        log(f"derived {derived} at {time.monotonic() - t:.0f}s")
    state = sim.state()
    state_new = data_dir / "demo-state.json.new"
    state_new.write_text(json.dumps(state, indent=1) + "\n")
    conn = sqlite3.connect(db_new)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("ANALYZE")
    conn.close()
    return {"db": str(db_new), "state": str(state_new), "counts": put.counts, "end_ms": end_ms, "anchor": anchor.isoformat(),
            "seconds": round(time.monotonic() - t)}


def verify(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    res = integrity.verify(conn)
    gaps = integrity.sequence_gaps(conn)
    conn.close()
    return {"ok": res["ok"], "records": res["records"], "head": res["head"], "problems": res["problems"][:5],
            "missing_seq": sum(g["missing"] for g in gaps)}


def swap(data_dir: Path) -> None:
    """Move the freshly built database and state into place, with the web app stopped and the minute updater held
    off by its lock. Visitors' exports belong to the old database, so their files go too."""
    import fcntl
    import shutil
    if not (data_dir / "speedmon.sqlite3.new").exists() or not (data_dir / "demo-state.json.new").exists():
        raise SystemExit("nothing to swap in: build it first (demo.generate --no-swap)")
    with open(data_dir / "tick.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        shutil.rmtree(data_dir / "exports", ignore_errors=True)
        live = data_dir / "speedmon.sqlite3"
        for suffix in ("-wal", "-shm"):
            Path(str(live) + suffix).unlink(missing_ok=True)
        os.replace(data_dir / "speedmon.sqlite3.new", live)
        os.replace(data_dir / "demo-state.json.new", data_dir / "demo-state.json")
        for p in data_dir.glob("speedmon.sqlite3.new*"):
            p.unlink(missing_ok=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="demo.generate")
    p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("SPEEDMON_DATA_DIR", "var-demo")))
    p.add_argument("--anchor", help="local date the history ends on (default: today)")
    p.add_argument("--end-ms", type=int, help="exact end time, UTC ms (default: now)")
    p.add_argument("--seed", type=int, default=20231)
    p.add_argument("--verify", action="store_true", help="re-hash everything and walk the chain afterwards")
    p.add_argument("--days", type=int, default=1096, help="length of the history (default: three years)")
    p.add_argument("--no-swap", action="store_true", help="leave the .new files for the caller to move")
    p.add_argument("--swap-only", action="store_true", help="only move the .new files into place (service stopped)")
    a = p.parse_args(argv)
    if a.swap_only:
        swap(a.data_dir)
        return 0
    end_ms = a.end_ms or timeutil.now_ms()
    anchor = date.fromisoformat(a.anchor) if a.anchor else date.fromisoformat(timeutil.local(end_ms, TZ).date().isoformat())
    info = build(a.data_dir, anchor, end_ms, a.seed, days=a.days)
    print(json.dumps(info))
    if a.verify:
        v = verify(Path(info["db"]))
        print(json.dumps(v))
        if not v["ok"] or v["missing_seq"]:
            return 2
        (a.data_dir / "speedmon.sqlite3.new.ok").write_text(info["anchor"])   # the nightly reset looks for this
    if not a.no_swap:
        swap(a.data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
