"""Integrity checks over the evidence tables and the ledger (see canonical.py for the scheme).

verify() recomputes, without trusting anything stored:
  1. every record's hash from its own columns (and each measurement's raw output against raw_sha256);
  2. that every record has exactly one ledger entry carrying the same hash, and vice versa;
  3. the whole chain from the genesis value to the head.
It streams rows, so memory stays flat however large the database grows.
"""
from __future__ import annotations

from . import canonical, timeutil
from .ingest import STREAM_TABLE


def head(conn) -> dict:
    row = conn.execute("SELECT chain_index, chain_hash, recorded_at FROM ledger ORDER BY chain_index DESC LIMIT 1").fetchone()
    if not row:
        return {"entries": 0, "head": canonical.GENESIS, "at": None}
    return {"entries": row["chain_index"] + 1, "head": row["chain_hash"], "at": row["recorded_at"]}


def verify(conn, progress=None) -> dict:
    problems: list[str] = []
    counts = {}
    checked = 0
    for stream, table in STREAM_TABLE.items():
        fields = canonical.FIELDS[stream]
        n = 0
        extra = ", t.raw" if stream == "measurement" else ""
        cur = conn.execute(f"SELECT t.{', t.'.join(fields)}, t.record_hash AS stored_hash, l.record_hash AS ledger_hash{extra} "
                           f"FROM {table} t LEFT JOIN ledger l ON l.stream = ? AND l.uuid = t.uuid", (stream,))
        for row in cur:
            n += 1
            rec = {k: row[k] for k in fields}
            calc = canonical.record_hash(stream, rec)
            if calc != row["stored_hash"]:
                problems.append(f"{stream} {rec['uuid']}: content does not match its stored hash")
            if row["ledger_hash"] is None:
                problems.append(f"{stream} {rec['uuid']}: missing from the ledger")
            elif row["ledger_hash"] != calc:
                problems.append(f"{stream} {rec['uuid']}: ledger hash differs from the record")
            if stream == "measurement" and canonical.sha256_text(row["raw"]) != rec["raw_sha256"]:
                problems.append(f"measurement {rec['uuid']}: raw engine output does not match raw_sha256")
            if len(problems) > 200:
                break
        counts[stream] = n
        checked += n
        if progress:
            progress(stream, n)
        orphan = conn.execute(f"SELECT count(*) FROM ledger l LEFT JOIN {table} t ON t.uuid = l.uuid "
                              f"WHERE l.stream = ? AND t.uuid IS NULL", (stream,)).fetchone()[0]
        if orphan:
            problems.append(f"{orphan} ledger entries for {stream} records that are no longer in the database")
    prev = canonical.GENESIS
    expect = 0
    for row in conn.execute("SELECT chain_index, stream, uuid, record_hash, prev_hash, chain_hash FROM ledger ORDER BY chain_index"):
        if row["chain_index"] != expect:
            problems.append(f"ledger index jumps from {expect - 1} to {row['chain_index']}")
            expect = row["chain_index"]
        if row["prev_hash"] != prev:
            problems.append(f"ledger entry {row['chain_index']}: previous-hash link broken")
        calc = canonical.chain_next(prev, row["stream"], row["uuid"], row["record_hash"])
        if calc != row["chain_hash"]:
            problems.append(f"ledger entry {row['chain_index']}: chain hash does not recompute")
        prev = row["chain_hash"]
        expect += 1
        if len(problems) > 200:
            break
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    missing = [f"{t}_append_only_{op}" for t in list(STREAM_TABLE.values()) + ["ledger", "imports", "quarantine"]
               for op in "ud" if f"{t}_append_only_{op}" not in triggers]
    if missing:
        problems.append("append-only protection removed: " + ", ".join(missing))
    return {"ok": not problems, "checked_at": timeutil.now_iso(), "records": checked, "by_stream": counts,
            "ledger_entries": expect, "head": prev, "problems": problems[:200],
            "truncated": len(problems) > 200, "hash_version": canonical.HASH_VERSION}


def sequence_gaps(conn, agent_outbox_floor: dict | None = None) -> list[dict]:
    """Per agent: sequence numbers never received. ``agent_outbox_floor`` (from agent status) tells which are
    still waiting in the agent's outbox, so they are reported as pending rather than missing."""
    out = []
    union = " UNION ".join(f"SELECT DISTINCT agent_id FROM {t} WHERE agent_seq IS NOT NULL" for t in STREAM_TABLE.values())
    agents = [r[0] for r in conn.execute(union)]
    for agent in sorted(set(agents)):
        seqs = []
        for table in STREAM_TABLE.values():
            seqs += [r[0] for r in conn.execute(f"SELECT agent_seq FROM {table} WHERE agent_id = ? AND agent_seq IS NOT NULL",
                                                 (agent,))]
        if not seqs:
            continue
        seqs.sort()
        gaps, total = [], 0
        for a, b in zip(seqs, seqs[1:]):
            if b - a > 1:
                total += b - a - 1
                if len(gaps) < 20:
                    gaps.append([a + 1, b - 1])
        quarantined = conn.execute("SELECT count(*) FROM quarantine WHERE agent_id = ?", (agent,)).fetchone()[0]
        out.append({"agent_id": agent, "first": seqs[0], "last": seqs[-1], "received": len(seqs), "missing": total,
                    "gaps": gaps, "quarantined": quarantined})
    return out
