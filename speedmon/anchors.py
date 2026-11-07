"""Externally timestamped copies of the ledger head (RFC 3161), and their verification.

The chain in canonical.py makes internal edits visible, but a chain still means nothing to a stranger unless
the head was published somewhere the person could not reach. This module does that: hourly, it takes the
current head, writes a short statement naming the chain position and the head hash, and has it signed by one
or more public timestamp authorities. The signed receipt is kept here and travels in the evidence pack, so a
later claim that the data was assembled after the fact has to explain a signature from before it.

Deliberately not chained into the ledger (an anchor points at a chain position, so chaining it back would be
circular) and deliberately unable to block anything: a TSA being down records an error row and the monitoring
carries on.

Two providers by default, so one authority being dead or disagreeable does not remove the evidence. Verify with
``verify_all``, which trusts nothing stored: it re-hashes the statement, re-checks that the chain position still
holds that head, and re-verifies the signature against the certificate inside the token.

Setup cost is one command and no account. Limits worth knowing: a public TSA proves the time, not the identity
of the requester. Receipts are checked to a trusted root through the system CA store, and against the authority's
own certificate when no store is present, so an unverifiable reply is stored as an error rather than as evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import timeutil

OPENSSL = os.environ.get("SPEEDMON_OPENSSL") or shutil.which("openssl") or "openssl"
# Trust store root certificates, so a receipt's chain can be checked to a trusted root and not just to the
# certificate it carries. First one present wins; without any, verification still happens against the token only.
SYSTEM_CA = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem")
MAX_RESPONSE = 200_000
DEFAULT_TIMEOUT = 20.0
STATEMENT_VERSION = 1

# name -> URL. Public, free, no account. Override with SPEEDMON_TSA="name=url,name=url" (empty disables).
PROVIDERS: tuple[tuple[str, str], ...] = (
    ("freetsa", "http://freetsa.org/tsr"),
    ("digicert", "http://timestamp.digicert.com"),
)


class AnchorError(RuntimeError):
    pass


def providers_from_env() -> tuple[tuple[str, str], ...]:
    raw = os.environ.get("SPEEDMON_TSA")
    if raw is None:
        return PROVIDERS
    out = []
    for item in raw.split(","):
        name, _, url = item.strip().partition("=")
        if name and url.startswith(("http://", "https://")):
            out.append((name[:32], url))
    return tuple(out)


def statement(index: int, head: str) -> str:
    """The exact text the authority signs. Fixed shape, so it never needs reconstructing at verify time."""
    return f"line-monitor ledger anchor v{STATEMENT_VERSION}\nindex={index}\nhead={head}\n"


def _run(args: list[str], timeout: float) -> bytes:
    try:
        proc = subprocess.run([OPENSSL, *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AnchorError(f"openssl: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AnchorError((msg[-1] if msg else f"openssl {args[0]} failed")[:300])
    return proc.stdout


def _post(url: str, body: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/timestamp-query",
                                          "Accept": "application/timestamp-reply"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MAX_RESPONSE)
    except urllib.error.HTTPError as exc:
        raise AnchorError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AnchorError(type(exc).__name__) from exc
    if not data:
        raise AnchorError("empty reply")
    return data


def _verify_token(token: Path, data: Path, work: Path, timeout: float) -> tuple[bool, str | None]:
    """Check the receipt against the statement, to a trusted root where one is available."""
    try:
        pem = _run(["pkcs7", "-inform", "DER", "-in", str(token), "-print_certs"], timeout)
    except AnchorError as exc:
        return False, f"cannot read the authority certificate: {exc}"
    if b"BEGIN CERTIFICATE" not in pem:
        return False, "receipt carries no certificate"
    cafile = work / "authority.pem"
    bundle = pem
    for candidate in SYSTEM_CA:
        try:
            bundle += b"\n" + Path(candidate).read_bytes()
            break
        except OSError:
            continue
    cafile.write_bytes(bundle)
    try:
        out = _run(["ts", "-verify", "-token_in", "-in", str(token), "-data", str(data), "-CAfile", str(cafile)], timeout)
    except AnchorError as exc:
        return False, str(exc)
    return (b"Verification: OK" in out), (None if b"Verification: OK" in out else out.decode("utf-8", "replace")[:200])


def head(db) -> tuple[int, str, int]:
    """(index, hash, entries) with entries 0 for an empty ledger."""
    with db.read() as conn:
        row = conn.execute("SELECT chain_index, chain_hash FROM ledger ORDER BY chain_index DESC LIMIT 1").fetchone()
        n = conn.execute("SELECT count(*) FROM ledger").fetchone()[0]
    return (row["chain_index"], row["chain_hash"], n) if row else (-1, "0" * 64, 0)


def already_anchored(conn, provider: str, head_hash: str) -> bool:
    row = conn.execute("SELECT 1 FROM anchors WHERE provider = ? AND head_hash = ? AND status = 'ok' LIMIT 1",
                       (provider, head_hash)).fetchone()
    return row is not None


def anchor_once(db, providers=None, timeout: float | None = None) -> dict:
    """Anchor the current head with each provider that has not already signed it. Never raises for a bad TSA."""
    providers = providers_from_env() if providers is None else providers
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    index, head_hash, entries = head(db)
    if not entries:
        return {"skipped": "the ledger is empty", "results": []}
    text = statement(index, head_hash)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    results = []
    with tempfile.TemporaryDirectory(prefix="anchor-") as tmp:
        work = Path(tmp)
        data = work / "statement.txt"
        data.write_text(text, encoding="utf-8")
        try:
            query = work / "query.tsq"
            _run(["ts", "-query", "-data", str(data), "-sha256", "-cert", "-no_nonce", "-out", str(query)], timeout)
            body = query.read_bytes()
        except (AnchorError, OSError) as exc:
            return {"skipped": f"could not build a timestamp request: {exc}", "results": []}
        for name, url in providers:
            with db.read() as conn:
                if already_anchored(conn, name, head_hash):
                    results.append({"provider": name, "status": "skip"})
                    continue
            status, token, error = "ok", None, None
            try:
                reply = _post(url, body, timeout)
                raw = work / f"{name}.tsr"
                raw.write_bytes(reply)
                token_path = work / f"{name}.der"
                _run(["ts", "-reply", "-in", str(raw), "-token_out", "-out", str(token_path)], timeout)
                token_bytes = token_path.read_bytes()
                token_path.write_bytes(token_bytes)
                ok, problem = _verify_token(token_path, data, work, timeout)
                if not ok:
                    raise AnchorError(problem or "the receipt did not verify")
                token = token_bytes
            except (AnchorError, OSError) as exc:
                status, error = "error", str(exc)[:300]
            with db.tx() as conn:
                conn.execute("INSERT INTO anchors (head_index, head_hash, provider, statement, statement_sha256, token, "
                             "status, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             (index, head_hash, name, text, digest, token, status, error, timeutil.now_iso()))
            results.append({"provider": name, "status": status, "error": error})
    return {"index": index, "head": head_hash, "results": results}


def verify_all(db, limit: int = 100, timeout: float | None = None) -> dict:
    """Re-verify the newest receipts and record the outcome. Reads nothing as trusted."""
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    with db.read() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, head_index, head_hash, statement, statement_sha256, token FROM anchors "
            "WHERE status = 'ok' ORDER BY id DESC LIMIT ?", (limit,))]
    checked = problems = 0
    with tempfile.TemporaryDirectory(prefix="anchor-check-") as tmp:
        work = Path(tmp)
        for row in rows:
            problem = None
            if hashlib.sha256(row["statement"].encode("utf-8")).hexdigest() != row["statement_sha256"]:
                problem = "the stored statement does not match its stored hash"
            else:
                with db.read() as conn:
                    chain = conn.execute("SELECT chain_hash FROM ledger WHERE chain_index = ?",
                                         (row["head_index"],)).fetchone()
                if chain is None:
                    problem = f"the chain has no entry at index {row['head_index']}"
                elif chain["chain_hash"] != row["head_hash"]:
                    problem = "the chain entry at that index no longer has the anchored head"
                elif not row["token"]:
                    problem = "no receipt stored"
                else:
                    data = work / f"{row['id']}.txt"
                    token = work / f"{row['id']}.der"
                    data.write_text(row["statement"], encoding="utf-8")
                    token.write_bytes(row["token"])
                    ok, why = _verify_token(token, data, work, timeout)
                    if not ok:
                        problem = f"receipt no longer verifies: {why}"
            checked += 1
            problems += bool(problem)
            with db.tx() as conn:
                conn.execute("INSERT INTO anchor_checks (anchor_id, checked_at, ok, problem) VALUES (?,?,?,?)",
                             (row["id"], timeutil.now_iso(), int(problem is None), problem))
    return {"checked": checked, "problems": problems}


def summary(db) -> dict:
    with db.read() as conn:
        row = conn.execute("SELECT count(*), coalesce(sum(status = 'ok'), 0), coalesce(sum(status = 'error'), 0), "
                           "max(created_at) FROM anchors").fetchone()
        last = conn.execute("SELECT checked_at, ok, problem FROM anchor_checks ORDER BY id DESC LIMIT 1").fetchone()
    return {"total": row[0], "ok": row[1], "errors": row[2], "last_anchor_at": row[3],
            "last_check": dict(last) if last else None}


def dump(db, directory: Path) -> list[Path]:
    """Write every receipt next to its statement, for the evidence pack. Returns the files written."""
    out = []
    directory.mkdir(parents=True, exist_ok=True)
    with db.read() as conn:
        rows = conn.execute("SELECT id, provider, created_at, statement, statement_sha256, token, status, error "
                            "FROM anchors ORDER BY id").fetchall()
    for row in rows:
        base = directory / f"{row['id']:06d}-{row['provider']}"
        (base.with_suffix(".txt")).write_text(row["statement"], encoding="utf-8")
        if row["token"]:
            (base.with_suffix(".tsr")).write_bytes(row["token"])
        (base.with_suffix(".json")).write_text(json.dumps(dict(row) | {"token": None}) + "\n", encoding="utf-8")
        out.append(base)
    return out
