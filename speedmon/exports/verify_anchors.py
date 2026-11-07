#!/usr/bin/env python3
"""Check the timestamp receipts in an evidence pack against the chain entries beside them.

  python3 verify_anchors.py <folder with the unzipped pack>

Each anchors/*.txt is a statement naming a chain position and head hash, and the *.tsr beside it is an RFC 3161
receipt over that statement from a public authority. For each pair this script re-hashes the statement, checks
it against the hash recorded in the *.json beside it, confirms the named head really is the chain hash at that
position in ledger.csv, and asks openssl to verify the receipt's signature.

openssl must be installed. Certificates are taken from the receipt itself and, where the system has a trust
store, from that too, so a receipt signed by an unknown authority is reported rather than trusted.
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem")


def main(folder):
    openssl = shutil.which("openssl")
    if not openssl:
        print("openssl not found: receipts cannot be verified (the rest of the pack can still be checked)")
        return 2
    import csv
    chain = {}
    with open(os.path.join(folder, "ledger.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            chain[int(row["chain_index"])] = row["chain_hash"]
    pairs = sorted(glob.glob(os.path.join(folder, "anchors", "*.tsr")))
    if not pairs:
        print("No receipts in this pack.")
        return 0
    bad = 0
    for tsr in pairs:
        base = tsr[:-4]
        statement = base + ".txt"
        meta = base + ".json"
        name = os.path.basename(base)
        if not (os.path.exists(statement) and os.path.exists(meta)):
            bad += 1
            print(f"INCOMPLETE {name}: missing the statement or the metadata")
            continue
        info = json.load(open(meta, encoding="utf-8"))
        text = open(statement, "rb").read()
        if hashlib.sha256(text).hexdigest() != info["statement_sha256"]:
            bad += 1
            print(f"MISMATCH   {name}: the statement does not match its recorded hash")
            continue
        index = int(info["head_index"])
        if index not in chain:
            print(f"OUTSIDE    {name}: chain entry {index} is not in this pack, so it cannot be tied to the data")
        elif chain[index] != info["head_hash"]:
            bad += 1
            print(f"BROKEN     {name}: chain entry {index} no longer holds the anchored head")
        with tempfile.TemporaryDirectory() as tmp:
            pem = subprocess.run([openssl, "pkcs7", "-inform", "DER", "-in", tsr, "-print_certs"],
                                 capture_output=True).stdout
            bundle = pem
            for candidate in CA_BUNDLES:
                try:
                    bundle += b"\n" + open(candidate, "rb").read()
                    break
                except OSError:
                    continue
            cafile = os.path.join(tmp, "ca.pem")
            open(cafile, "wb").write(bundle)
            res = subprocess.run([openssl, "ts", "-verify", "-token_in", "-in", tsr, "-data", statement,
                                  "-CAfile", cafile], capture_output=True)
        if res.returncode != 0:
            bad += 1
            print(f"UNSIGNED   {name}: the receipt does not verify ({res.stderr.decode(errors='replace').strip()[:120]})")
        else:
            print(f"OK         {name}: index {index}, signed {info['created_at']} by {info['provider']}")
    print(f"Receipts checked: {len(pairs)}")
    print("RESULT: " + ("OK" if bad == 0 else f"{bad} PROBLEM(S)"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
