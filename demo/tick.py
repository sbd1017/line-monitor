"""Keep the demo live: carry the simulated agent on from where the history (or the last tick) stopped, up to now.

    python -m demo.tick --data-dir DIR        (a systemd timer runs it every minute)

Each run adds a probe for every minute that passed and any full tests that fell due (a scheduled test every 30
minutes at a random moment, plus confirmations, bursts and 'run a test now' requests from the dashboard). Records
go through the backend's own ingest (validation, hash check, ledger, roll-ups), exactly as the real agent's do.
Nothing here measures anything or touches the network.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

from speedmon import canonical, ingest, timeutil
from speedmon.db import Database

from .generate import agent_status_row
from .simulate import AGENT_ID, Sim, world_for


def load_state(path: Path) -> dict:
    return json.loads(path.read_text())


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    os.replace(tmp, path)


def pending_requests(conn, now: int) -> list[int]:
    """'Run a test now' presses from the dashboard, handed to the simulated agent (as the real agent's config poll
    would, a few seconds later) and marked sent."""
    rows = conn.execute("SELECT id, created_at FROM commands WHERE status = 'pending' AND kind = 'run_test' AND created_at <= ?",
                        (timeutil.iso(now),)).fetchall()
    for r in rows:
        conn.execute("UPDATE commands SET status = 'sent', sent_at = ? WHERE id = ?", (timeutil.iso(now), r["id"]))
    return [min(timeutil.parse_iso_ms(r["created_at"]) + 4_000, now) for r in rows]


def tick(data_dir: Path, now_ms: int | None = None) -> dict:
    state_path = data_dir / "demo-state.json"
    state = load_state(state_path)
    world = world_for(state)
    sim = Sim(world, state)
    now = now_ms or timeutil.now_ms()
    db = Database(data_dir / "speedmon.sqlite3")
    items: list = []

    def emit(stream: str, rec: dict, raw: str | None = None) -> None:
        rec = canonical.normalise(stream, rec)
        item = {"stream": stream, "record": rec, "record_hash": canonical.record_hash(stream, rec)}
        if stream == "measurement":
            item["raw"] = raw
        items.append(item)

    with db.tx() as conn:
        requests = pending_requests(conn, now)
        sim.resume(emit, now)
        sim.run(now, emit, requests)
        res = {"accepted": [], "duplicates": [], "rejected": []}
        for i in range(0, len(items), 500):
            r = ingest.ingest_batch(conn, AGENT_ID, items[i:i + 500], allow_synthetic=True)
            for k in res:
                res[k] += r[k]
        agent_status_row(conn, sim, now)
    save_state(state_path, sim.state())
    return {"records": len(items), "accepted": len(res["accepted"]), "duplicates": len(res["duplicates"]),
            "rejected": res["rejected"][:3], "requests": len(requests), "t": timeutil.iso(sim.t)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="demo.tick")
    p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("SPEEDMON_DATA_DIR", "var-demo")))
    a = p.parse_args(argv)
    if not (a.data_dir / "demo-state.json").exists():
        print("no demo-state.json yet (run demo.generate first)")
        return 0
    with open(a.data_dir / "tick.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another tick is running")
            return 0
        out = tick(a.data_dir)
    print(json.dumps(out))
    return 1 if out["rejected"] else 0


if __name__ == "__main__":
    sys.exit(main())
