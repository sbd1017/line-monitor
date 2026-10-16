import csv
import html
import gzip
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from speedmon import importer, ingest, integrity, synthetic, timeutil
from speedmon.exports import jobs


def run_export(db, settings, fmt, a=0, b=None, **params):
    ex = jobs.Exporter(db, settings)
    with db.tx() as c:
        uid = ex.queue(c, fmt, "All data", a, b or timeutil.now_ms() + 1, jobs.validate_params({"format": fmt, **params}))
        c.execute("UPDATE exports SET status = 'running' WHERE uuid = ?", (uid,))
    with db.read() as c:
        job = dict(c.execute("SELECT * FROM exports WHERE uuid = ?", (uid,)).fetchone())
    ex.run_one(job)
    with db.read() as c:
        row = dict(c.execute("SELECT * FROM exports WHERE uuid = ?", (uid,)).fetchone())
    assert row["status"] == "done", row["error"]
    return row, ex.path_for(uid, row["file_name"])


def sheets(path):
    with zipfile.ZipFile(path) as z:
        wb = z.read("xl/workbook.xml").decode()
        names = [html.unescape(x) for x in re.findall(r'<sheet name="([^"]+)"', wb)]
        rows = {}
        for i, n in enumerate(names, 1):
            rows[n] = z.read(f"xl/worksheets/sheet{i}.xml").decode().count("<row ")
        charts = [f for f in z.namelist() if f.startswith("xl/charts/chart")]
    return names, rows, charts


def test_xlsx_overflows_onto_extra_raw_sheets_without_losing_rows(db, settings):
    settings.raw_rows_per_sheet = 500
    with db.tx() as c:
        res = synthetic.seed(c, days=30, interval_min=30, probes=False)
    row, path = run_export(db, settings, "xlsx")
    names, rows, charts = sheets(path)
    n = res["measurements"]
    raw = [s for s in names if s.startswith("Raw Data")]
    assert names[:7] == ["Summary", "Daily", "Hourly & Weekday", "Incidents", "Annotations", "Methodology", "Integrity"]
    assert len(raw) == -(-n // 500) and raw[1] == "Raw Data 2"
    assert sum(rows[s] - 1 for s in raw) == n == row["rows"]           # minus each header row
    assert len(charts) >= 5
    with zipfile.ZipFile(path) as z:
        strings = html.unescape(z.read("xl/worksheets/sheet1.xml").decode())   # constant_memory writes inline strings
    assert f"{n:,} rows on {len(raw)} sheets" in strings


def test_csv_and_gz_are_complete_and_self_describing(db, settings):
    with db.tx() as c:
        res = synthetic.seed(c, days=10, interval_min=60, probes=False)
    for fmt in ("csv", "csv.gz"):
        row, path = run_export(db, settings, fmt)
        text = (gzip.open(path, "rt") if fmt == "csv.gz" else open(path)).read()
        lines = text.splitlines()
        meta = [ln for ln in lines if ln.startswith("#")]
        assert any("Records:" in m for m in meta) and any("Integrity check at export: PASSED" in m for m in meta)
        body = list(csv.DictReader(ln for ln in lines if not ln.startswith("#")))
        assert len(body) == res["measurements"] == row["rows"]
        assert "public_ip" not in body[0]


def test_zip_pack_verifies_independently(db, settings, tmp_path):
    with db.tx() as c:
        synthetic.seed(c, days=5, interval_min=60, probe_interval_s=300)
    row, path = run_export(db, settings, "zip")
    out = tmp_path / "pack"
    with zipfile.ZipFile(path) as z:
        z.extractall(out)
    r = subprocess.run([sys.executable, "-I", str(out / "verify_export.py"), str(out)], capture_output=True, text=True)
    assert "RESULT: OK" in r.stdout and "complete chain from genesis" in r.stdout, r.stdout + r.stderr
    m = json.loads((out / "manifest.json").read_text())
    assert m["integrity_ok"] and not m["public_ip_included"]
    # tamper with one value: the verifier must notice
    p = out / "measurements.csv"
    lines = p.read_text().splitlines()
    hdr = lines[0].split(",")
    i = hdr.index("download_mbps")
    cells = lines[1].split(",")
    cells[i] = "999.0"
    lines[1] = ",".join(cells)
    p.write_text("\n".join(lines) + "\n")
    r = subprocess.run([sys.executable, "-I", str(out / "verify_export.py"), str(out)], capture_output=True, text=True)
    assert "MISMATCH" in r.stdout and "PROBLEM" in r.stdout


LEGACY = (
    "62951,razorblue,Manchester,2025-06-26T07:44:26.523390Z,16.0,23.6,88210269.6,20080767.1,,203.0.113.28\n"
    "\x00\x00\x0057802,PQ.Hosting,Coventry,2025-06-26T12:20:01.631425Z,143.79,28.965,88210269.6593104,20080767.14235736,,203.0.113.28\n"
    "61301,\"Misaka Network, Inc.\",Marseille,2026-10-06T19:25:02.277488Z,1277.5,46.9,0.0,9117019.3,,203.0.113.28\n"
    "62951,razorblue,Manchester,2025-06-26T07:44:26.523390Z,16.0,23.6,88210269.6,20080767.1,,203.0.113.28\n"
    "garbage line\n"
)


def test_legacy_import_repairs_flags_and_is_idempotent(db, tmp_path):
    f = tmp_path / "speed_log.csv"
    f.write_bytes(LEGACY.encode())
    before = f.read_bytes()
    with db.tx() as c:
        s1 = importer.import_csv(c, f)
        s2 = importer.import_csv(c, f)
    assert f.read_bytes() == before                                      # source never modified
    assert s1["inserted"] == 4 and s1["repaired"] == 1 and s1["rejected"] == 1
    assert s2["inserted"] == 0 and s2["duplicates"] == 4
    with db.read() as c:
        rows = c.execute("SELECT * FROM measurements ORDER BY t_ms").fetchall()
        assert {r["source"] for r in rows} == {"legacy_csv"}
        zero = [r for r in rows if r["status"] == "failed"]
        assert len(zero) == 1 and zero[0]["failure_code"] == "legacy_zero_throughput"
        assert all("203.0.113.28" not in (r["raw"] or "") for r in rows)  # IP moved to private diagnostics
        assert c.execute("SELECT count(*) FROM diagnostics WHERE public_ip = '203.0.113.28'").fetchone()[0] == 4
        assert c.execute("SELECT count(*) FROM imports").fetchone()[0] == 2
        assert integrity.verify(c)["ok"]
        misaka = [r for r in rows if r["server_sponsor"] == "Misaka Network, Inc."]
        assert misaka and misaka[0]["distance_km"] == 1277.5
