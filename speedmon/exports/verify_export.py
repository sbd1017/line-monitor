#!/usr/bin/env python3
"""Independent check of a speedmon evidence pack (no other software needed, Python 3.8+).

  python3 verify_export.py <folder with the unzipped pack>

For every row in measurements.csv and probes.csv it rebuilds the record's canonical JSON, re-computes its
SHA-256 and compares it with the stored record hash and with the ledger entry. For every ledger entry it
re-computes the chain hash from the previous hash. Where the pack covers the whole history (an "All data"
export), the chain is checked end to end from the genesis value; otherwise each entry is checked on its own
and runs of consecutive entries are checked as linked.
"""
import csv
import hashlib
import json
import os
import sys

GENESIS = "0" * 64


def main(folder):
    manifest = json.load(open(os.path.join(folder, "manifest.json")))
    fields = manifest["hash_fields"]
    reals = set(manifest["real_fields"])
    ints = {"agent_seq", "t_ms", "duration_ms", "utc_offset_min", "download_bytes", "upload_bytes", "download_ms",
            "upload_ms", "link_speed_mbps", "rx_errors_delta", "tx_errors_delta", "rx_dropped_delta", "tx_dropped_delta",
            "rx_bytes_delta", "tx_bytes_delta", "window_ms", "interval_s", "dns_ok", "http_ok", "link_up",
            "public_ip_changed"}

    def conv(k, v):
        if v == "":
            return None
        if k in reals:
            return float(v)
        if k in ints:
            return int(v)
        return v

    def rhash(stream, row):
        obj = {k: conv(k, row.get(k, "")) for k in fields[stream]}
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    ledger = {}
    rows = list(csv.DictReader(open(os.path.join(folder, "ledger.csv"), encoding="utf-8")))
    for r in rows:
        ledger[(r["stream"], r["uuid"])] = r
    bad = 0
    checked = 0
    for stream, name in (("measurement", "measurements.csv"), ("probe", "probes.csv")):
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            reader = csv.DictReader(line for line in fh if not line.startswith("#"))
            for row in reader:
                checked += 1
                h = rhash(stream, row)
                if h != row["record_hash"]:
                    bad += 1
                    print(f"MISMATCH {stream} {row['uuid']}: content does not match its record hash")
                entry = ledger.get((stream, row["uuid"]))
                if entry is None:
                    bad += 1
                    print(f"MISSING  {stream} {row['uuid']}: no ledger entry in this pack")
                elif entry["record_hash"] != h:
                    bad += 1
                    print(f"MISMATCH {stream} {row['uuid']}: ledger hash differs")
    prev_index, prev_hash, linked = None, None, 0
    for r in sorted(rows, key=lambda r: int(r["chain_index"])):
        idx = int(r["chain_index"])
        calc = hashlib.sha256(f"{r['prev_hash']}:{r['stream']}:{r['uuid']}:{r['record_hash']}".encode()).hexdigest()
        if calc != r["chain_hash"]:
            bad += 1
            print(f"BROKEN   ledger entry {idx}: chain hash does not recompute")
        if idx == 0 and r["prev_hash"] != GENESIS:
            bad += 1
            print("BROKEN   ledger entry 0 does not start from the genesis value")
        if prev_index is not None and idx == prev_index + 1:
            linked += 1
            if r["prev_hash"] != prev_hash:
                bad += 1
                print(f"BROKEN   ledger entries {prev_index}->{idx}: link broken")
        prev_index, prev_hash = idx, r["chain_hash"]
    head = manifest["ledger_head"]["head"]
    complete = rows and int(rows[0]["chain_index"]) == 0 and linked == len(rows) - 1
    print(f"Records re-hashed: {checked}; ledger entries: {len(rows)}; consecutive links checked: {linked}")
    if complete:
        print("The pack holds the complete chain from genesis." + (" Its last entry matches the head recorded at export."
              if rows[-1]["chain_hash"] == head else " (The database head moved on after the data was read.)"))
    print("RESULT: " + ("OK" if bad == 0 else f"{bad} PROBLEM(S)"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
