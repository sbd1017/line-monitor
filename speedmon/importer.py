"""Import the old speedtest-cli history (~/speed_log.csv and ~/speed_log_2025.xlsx) as legacy evidence.

The source files are only read, never changed. Each import run is recorded in ``imports`` with the file's
SHA-256, so every imported row can be traced back to an exact file. Imported rows are idempotent (UUIDv5 from
the line content and its occurrence number), so running the import again (e.g. after the CSV grew) adds only
new lines.

speedtest-cli --csv columns: server id, sponsor, server name (city), timestamp (UTC), distance (km), ping (ms),
download (bit/s), upload (bit/s), share URL, public IP.
Known defects handled:
  * NUL bytes in front of some lines (concurrent appends / power loss): removed and the row marked repaired.
  * A download of exactly 0 bit/s: kept as reported, but recorded as a failed test (legacy_zero_throughput).
  * The public IP is moved into the private diagnostics record; the stored raw line has it replaced by a marker.
  * speed_log_2025.xlsx stamped rows with the time the converter ran, and its "Ping" column holds the distance
    and "Jitter" the ping. The converter copied the CSV's last line each run, so when a test wrote nothing it
    re-logged an older result: rows whose download and upload match a CSV test from the previous 65 minutes are
    duplicates and skipped; the rest are imported with that caveat.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid as uuidlib
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from . import canonical, ingest, settings_store, timeutil

NS = uuidlib.UUID("6f1c2e1a-9d7b-4f0e-8b55-2a7d5c0e9f11")
AGENT = "legacy-speedtest-cli"
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$|^[0-9a-fA-F:]+:[0-9a-fA-F:]*$")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _insert(conn, rec: dict, raw: str, public_ip: str | None) -> bool:
    rec = canonical.normalise("measurement", rec)
    exists = conn.execute("SELECT 1 FROM measurements WHERE uuid = ?", (rec["uuid"],)).fetchone()
    if exists:
        return False
    rhash = canonical.record_hash("measurement", rec)
    ingest.insert_record(conn, "measurement", rec, rhash, raw)
    if public_ip:
        d = canonical.normalise("diagnostic", {
            "uuid": str(uuidlib.uuid5(NS, rec["uuid"] + ":diag")), "measurement_uuid": rec["uuid"], "agent_id": AGENT,
            "t_ms": rec["t_ms"], "public_ip": public_ip, "extra": json.dumps({"note": "from legacy CSV ip column"})})
        ingest.insert_record(conn, "diagnostic", d, canonical.record_hash("diagnostic", d))
    return True


def _base(t_ms: int, tz: str, source: str) -> dict:
    return {"agent_id": AGENT, "agent_seq": None, "source": source, "kind": "legacy", "engine": "speedtest-cli",
            "engine_version": "2.1.3", "t_ms": t_ms, "started_at": timeutil.iso(t_ms), "tz_name": tz,
            "utc_offset_min": timeutil.offset_minutes(t_ms, tz), "trigger_reason": "legacy cron/watchdog"}


def import_csv(conn, path: Path, tz: str = "Europe/London") -> dict:
    path = Path(path)
    digest, size = _sha256_file(path), path.stat().st_size
    stats = Counter()
    seen = Counter()
    with open(path, "rb") as fh:
        for line_no, line_b in enumerate(fh, 1):
            stats["rows_read"] += 1
            repaired = b"\x00" in line_b
            text = line_b.replace(b"\x00", b"").decode("utf-8", "replace").rstrip("\r\n")
            if not text.strip():
                stats["rejected"] += 1
                continue
            try:
                f = next(csv.reader([text]))
                if len(f) != 10 or not f[0].strip().isdigit():
                    raise ValueError("not a speedtest-cli row")
                t_ms = timeutil.parse_iso_ms(f[3])
                dl_bps, ul_bps = float(f[6]), float(f[7])
            except (ValueError, StopIteration):
                stats["rejected"] += 1
                continue
            seen[text] += 1
            uid = str(uuidlib.uuid5(NS, f"csv:{text}:{seen[text]}"))
            ip = f[9].strip() if IP_RE.match(f[9].strip() or "-") else None
            redacted = f[:9] + (["<public-ip: private diagnostics>"] if ip else [f[9]])
            out = io.StringIO()
            csv.writer(out).writerow(redacted)
            raw = json.dumps({"format": "speedtest-cli --csv", "file": path.name, "file_sha256": digest, "line_no": line_no,
                              "repaired_nul_bytes": repaired, "line": out.getvalue().rstrip("\r\n")}, ensure_ascii=False)
            zero = dl_bps <= 0
            rec = {**_base(t_ms, tz, "legacy_csv"), "uuid": uid, "status": "failed" if zero else "ok",
                   "failure_code": "legacy_zero_throughput" if zero else None,
                   "failure_detail": "speedtest-cli reported 0 bit/s download" if zero else None,
                   "download_mbps": dl_bps / 1e6, "upload_mbps": ul_bps / 1e6, "ping_ms": float(f[5]),
                   "distance_km": float(f[4]) if f[4] else None, "server_id": f[0].strip(), "server_sponsor": f[1],
                   "server_location": f[2], "server_name": f"{f[1]} – {f[2]}", "result_url": f[8] or None,
                   "raw_sha256": canonical.sha256_text(raw)}
            if _insert(conn, rec, raw, ip):
                stats["inserted"] += 1
                stats["repaired"] += int(repaired)
            else:
                stats["duplicates"] += 1
    _record_import(conn, path, digest, size, "speedtest-cli csv", stats)
    return _summary(stats)


# ---------------------------------------------------------------- minimal XLSX reader (stdlib only)

def _xlsx_rows(path: Path):
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        sheet = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))[0]
        root = ET.fromstring(z.read(sheet))
        for row in root.iter("{%s}row" % ns["m"]):
            vals = {}
            for c in row.findall("m:c", ns):
                ref = c.get("r")
                col = 0
                for ch in re.match(r"[A-Z]+", ref).group(0):
                    col = col * 26 + ord(ch) - 64
                t = c.get("t")
                v = c.find("m:v", ns)
                if t == "s" and v is not None:
                    vals[col - 1] = shared[int(v.text)]
                elif t == "inlineStr":
                    vals[col - 1] = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                elif v is not None:
                    try:
                        vals[col - 1] = float(v.text)
                    except ValueError:
                        vals[col - 1] = v.text
            if vals:
                yield [vals.get(i) for i in range(max(vals) + 1)]


def _xlsx_date(v) -> str | None:
    if isinstance(v, float):
        return (datetime(1899, 12, 30) + timedelta(days=v)).strftime("%Y-%m-%d")
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    return None


def import_xlsx(conn, path: Path, tz: str = "Europe/London") -> dict:
    """Rows from the converter's workbook that are not already present from the CSV."""
    path = Path(path)
    digest, size = _sha256_file(path), path.stat().st_size
    stats = Counter()
    csv_rows = conn.execute("SELECT t_ms, download_mbps, upload_mbps FROM measurements WHERE source = 'legacy_csv'").fetchall()
    by_minute: dict = {}
    for t, dl, ul in csv_rows:
        by_minute.setdefault(t // 60000, []).append((round(dl, 2), round(ul, 2)))
    seen = Counter()
    for i, row in enumerate(_xlsx_rows(path)):
        if i == 0:
            continue
        stats["rows_read"] += 1
        try:
            day = _xlsx_date(row[0])
            tm = str(row[1])
            if not day or not re.match(r"\d{2}:\d{2}:\d{2}", tm):
                raise ValueError
            logged = datetime.fromisoformat(f"{day}T{tm[:15]}")
            t_ms = timeutil.local_to_ms(logged.isoformat(), tz)
            dl, ul = float(row[6]), float(row[7])
        except (ValueError, TypeError, IndexError):
            stats["rejected"] += 1
            continue
        # Duplicate of a CSV row: same download and upload (2 dp) from a test that started up to 65 minutes earlier.
        # The converter logged "the last CSV line" each run, so when a test produced no line it re-logged an older one.
        m = t_ms // 60000
        if any((round(dl, 2), round(ul, 2)) in by_minute.get(k, []) for k in range(m - 65, m + 2)):
            stats["duplicates"] += 1
            continue
        key = json.dumps(row, default=str)
        seen[key] += 1
        uid = str(uuidlib.uuid5(NS, f"xlsx:{key}:{seen[key]}"))
        ip = str(row[8]).strip() if len(row) > 8 and row[8] and IP_RE.match(str(row[8]).strip()) else None
        safe = list(row[:8]) + (["<public-ip: private diagnostics>"] if ip else list(row[8:9]))
        raw = json.dumps({"format": "speed_log_2025.xlsx (convert_and_upload.py)", "file": path.name, "file_sha256": digest,
                          "row": i + 1, "values": safe,
                          "caveat": "timestamp is when the converter ran, not when the test started; the 'Ping' column "
                                    "holds the server distance and 'Jitter' holds the ping"}, ensure_ascii=False, default=str)
        rec = {**_base(t_ms, tz, "legacy_xlsx"), "uuid": uid, "status": "ok" if dl > 0 else "failed",
               "failure_code": None if dl > 0 else "legacy_zero_throughput", "download_mbps": dl, "upload_mbps": ul,
               "ping_ms": float(row[5]) if isinstance(row[5], float) else None,
               "distance_km": float(row[4]) if isinstance(row[4], float) else None,
               "server_sponsor": row[2], "server_location": row[3], "server_name": f"{row[2]} – {row[3]}",
               "raw_sha256": canonical.sha256_text(raw)}
        if _insert(conn, rec, raw, ip):
            stats["inserted"] += 1
        else:
            stats["duplicates"] += 1
    _record_import(conn, path, digest, size, "converter xlsx", stats)
    return _summary(stats)


def _summary(stats) -> dict:
    return {k: stats[k] for k in ("rows_read", "inserted", "duplicates", "repaired", "rejected")}


def _record_import(conn, path, digest, size, fmt, stats) -> None:
    conn.execute("INSERT INTO imports (started_at, file_name, file_sha256, file_bytes, format, rows_read, inserted, duplicates, "
                 "repaired, rejected, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (timeutil.now_iso(), path.name, digest, size, fmt, stats["rows_read"], stats["inserted"], stats["duplicates"],
                  stats["repaired"], stats["rejected"], None))


def tz_of(conn) -> str:
    return settings_store.get(conn, "timezone")
