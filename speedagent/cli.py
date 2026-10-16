"""Local agent commands (run as the speedagent user, e.g. sudo -u speedagent ...):

  python -m speedagent.cli status            queue depth, mode, last tests (from the local store)
  python -m speedagent.cli recent [N]        the last N measurements in the local outbox
  python -m speedagent.cli requeue-rejected  send records the backend rejected again (after fixing the cause)
  python -m speedagent.cli drain             deliver everything still queued (exit 0 only when the queue is empty)
  python -m speedagent.cli reset-policy      forget scheduling memory (mode, daily count, recent results): used by the reset

To run a test now:   sudo systemctl kill --kill-whom=main -s USR1 speedagent
To pause tests:      sudo -u speedagent touch /var/lib/speedagent/PAUSE   (delete the file to resume)
"""
from __future__ import annotations

import json
import sys

from . import config as C
from .store import Store


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    s = C.load()
    st = Store(s.db_path)
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(json.dumps({"queue": st.queue_stats(), "policy": st.get("policy"), "config": st.get("config"),
                          "next_seq": st.get("next_seq"), "boot_id": st.get("boot_id"), "clean_stop": st.get("clean_stop"),
                          "paused_by_file": (s.state_dir / "PAUSE").exists()}, indent=1))
        return 0
    if cmd == "recent":
        n = int(argv[1]) if len(argv) > 1 else 10
        for r in st.conn.execute("SELECT seq, status, record FROM outbox WHERE stream = 'measurement' ORDER BY seq DESC LIMIT ?", (n,)):
            m = json.loads(r["record"])
            print(f"{m['started_at']} {m['kind']:<12} {m['engine']:<12} {m['status']:<6} "
                  f"{(m.get('download_mbps') or 0):8.1f} {(m.get('upload_mbps') or 0):7.1f} {m.get('failure_code') or ''} [{r['status']}]")
        return 0
    if cmd == "drain":
        from .uplink import Uplink, UplinkError
        up = Uplink(s.backend, s.agent_id, s.key())
        for _ in range(50):
            batch = st.pending(500)
            if not batch:
                print("queue empty")
                return 0
            items = [{"stream": b["stream"], "record": json.loads(b["record"]), "record_hash": b["record_hash"],
                      **({"raw": b["raw"]} if b["stream"] == "measurement" else {})} for b in batch]
            try:
                res = up.send(items)
            except UplinkError as exc:
                print(f"backend unreachable: {exc}")
                return 2
            st.mark(list(res.get("accepted", [])) + list(res.get("duplicates", [])),
                    {r["uuid"]: r.get("error", "") for r in res.get("rejected", []) if r.get("uuid")})
        print("queue not empty after 50 batches")
        return 3
    if cmd == "reset-policy":
        st.put("policy", {})
        print("policy state cleared")
        return 0
    if cmd == "requeue-rejected":
        n = st.conn.execute("UPDATE outbox SET status = 'pending' WHERE status = 'rejected'").rowcount
        print(f"{n} record(s) queued again")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
