"""Raw CSV, CSV.GZ and ZIP evidence packs. Everything is streamed row by row from SQLite.

CSV layout: a few '#' metadata lines (generation time, range, record count, export version, integrity), then a
header row and one row per test. Values are written exactly as stored: floats in shortest round-trip form,
times as UTC ISO-8601 plus the local time and offset, so record hashes can be recomputed from the file.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import zipfile
from pathlib import Path

from .. import canonical, integrity, quality, settings_store, timeutil
from ..config import VERSION
from . import jobs

DIAG_COLS = ["cpu_load1", "cpu_load5", "cpu_temp_c", "throttled", "mem_available_pct", "swap_used_pct",
             "rx_errors_delta", "tx_errors_delta", "rx_dropped_delta", "tx_dropped_delta", "rx_bytes_delta",
             "tx_bytes_delta", "window_ms", "background_rx_mbps", "background_tx_mbps", "pretest_rx_mbps", "pretest_tx_mbps",
             "gateway_rtt_ms", "gateway_loss_pct", "external_rtt_ms", "external_loss_pct", "dns_ok", "public_ip_changed",
             "pi_uptime_s", "boot_id", "agent_version"]

# (header, key, xlsx kind, xlsx width). Kinds: dt (local date-time), text, n1/n2/n3 (decimals), int.
RAW_COLUMNS = [
    ("Local time", "_local", "dt", 19), ("UTC time", "started_at", "text", 24), ("UTC offset (min)", "utc_offset_min", "int", 9),
    ("Time zone", "tz_name", "text", 14), ("Status", "status", "text", 8), ("Failure code", "failure_code", "text", 14),
    ("Failure detail", "failure_detail", "text", 30), ("Measurement quality", "_quality", "text", 20), ("Download (Mbps)", "download_mbps", "n2", 11),
    ("Upload (Mbps)", "upload_mbps", "n2", 10), ("Ping idle (ms)", "ping_ms", "n2", 9), ("Jitter (ms)", "jitter_ms", "n2", 9),
    ("Ping low (ms)", "ping_low_ms", "n2", 9), ("Ping high (ms)", "ping_high_ms", "n2", 9),
    ("Latency under download (ms)", "dl_latency_ms", "n2", 11), ("Latency under upload (ms)", "ul_latency_ms", "n2", 11),
    ("Packet loss (%)", "packet_loss_pct", "n2", 9), ("Duration (ms)", "duration_ms", "int", 10),
    ("Download bytes", "download_bytes", "int", 13), ("Upload bytes", "upload_bytes", "int", 12),
    ("Download phase (ms)", "download_ms", "int", 10), ("Upload phase (ms)", "upload_ms", "int", 10),
    ("Test kind", "kind", "text", 12), ("Trigger", "trigger_reason", "text", 20), ("Related test", "related_uuid", "text", 12),
    ("Engine", "engine", "text", 12), ("Engine version", "engine_version", "text", 14),
    ("Server ID", "server_id", "text", 9), ("Server operator", "server_sponsor", "text", 22),
    ("Server", "server_name", "text", 26), ("Server location", "server_location", "text", 14),
    ("Server country", "server_country", "text", 12), ("Server host", "server_host", "text", 26),
    ("Distance (km)", "distance_km", "n1", 9), ("ISP (as seen by engine)", "isp", "text", 14),
    ("Result ID", "result_id", "text", 12), ("Result URL", "result_url", "text", 30),
    ("Interface", "iface", "text", 8), ("Link speed (Mbps)", "link_speed_mbps", "int", 9), ("Duplex", "link_duplex", "text", 7),
    ("Link state", "link_state", "text", 8), ("Ended (UTC)", "ended_at", "text", 24),
    ("Source", "source", "text", 11), ("Record UUID", "uuid", "text", 36), ("Agent", "agent_id", "text", 10),
    ("Agent sequence", "agent_seq", "int", 9), ("t_ms (UTC epoch ms)", "t_ms", "int", 14),
    ("Raw output SHA-256", "raw_sha256", "text", 20), ("Record hash", "record_hash", "text", 20),
    ("Ledger index", "chain_index", "int", 9), ("Chain hash", "chain_hash", "text", 20),
    ("Owner flags", "flags", "text", 14), ("Anomaly z", "anomaly_z", "n2", 8),
    ("Pi CPU load (1 min)", "cpu_load1", "n2", 8), ("Pi CPU load (5 min)", "cpu_load5", "n2", 8),
    ("Pi CPU temp (C)", "cpu_temp_c", "n1", 8), ("Pi throttled flags", "throttled", "text", 9),
    ("Pi memory available (%)", "mem_available_pct", "n1", 9), ("Pi swap used (%)", "swap_used_pct", "n1", 8),
    ("RX errors during test", "rx_errors_delta", "int", 8), ("TX errors during test", "tx_errors_delta", "int", 8),
    ("RX drops during test", "rx_dropped_delta", "int", 8), ("TX drops during test", "tx_dropped_delta", "int", 8),
    ("Pi RX bytes during test", "rx_bytes_delta", "int", 13), ("Pi TX bytes during test", "tx_bytes_delta", "int", 12),
    ("Counter window (ms)", "window_ms", "int", 9),
    ("Other Pi traffic in (Mbps)", "background_rx_mbps", "n2", 9), ("Other Pi traffic out (Mbps)", "background_tx_mbps", "n2", 9),
    ("Pi traffic before test in (Mbps)", "pretest_rx_mbps", "n2", 9), ("Pi traffic before test out (Mbps)", "pretest_tx_mbps", "n2", 9),
    ("Router ping (ms)", "gateway_rtt_ms", "n2", 8), ("Router ping loss (%)", "gateway_loss_pct", "n1", 8),
    ("External ping (ms)", "external_rtt_ms", "n2", 8), ("External ping loss (%)", "external_loss_pct", "n1", 8),
    ("DNS OK", "dns_ok", "int", 6), ("Public IP changed", "public_ip_changed", "int", 7), ("Pi uptime (s)", "pi_uptime_s", "n1", 10),
    ("Boot ID", "boot_id", "text", 12), ("Agent version", "agent_version", "text", 9),
]


def sources(params: dict) -> tuple:
    return ("agent", "synthetic", "legacy_csv", "legacy_xlsx") if params.get("include_legacy", True) else ("agent", "synthetic")


def raw_query(params: dict) -> tuple[str, str]:
    src = sources(params)
    ph = ",".join("?" * len(src))
    diag = ", ".join(f"d.{c}" for c in DIAG_COLS) + (", d.public_ip" if params.get("include_public_ip") else "")
    sql = (f"SELECT m.*, {diag}, d.uuid AS _diag_uuid, l.chain_index, l.chain_hash, dm.anomaly_z, "
           f"(SELECT group_concat(f.flag || ': ' || f.reason, ' | ') FROM measurement_flags f WHERE f.measurement_uuid = m.uuid "
           f" AND f.revoked_at IS NULL) AS flags "
           f"FROM measurements m LEFT JOIN diagnostics d ON d.measurement_uuid = m.uuid "
           f"LEFT JOIN ledger l ON l.stream = 'measurement' AND l.uuid = m.uuid "
           f"LEFT JOIN derived_measurement dm ON dm.uuid = m.uuid "
           f"WHERE m.t_ms >= ? AND m.t_ms < ? AND m.source IN ({ph}) ORDER BY m.t_ms, m.id")
    count = f"SELECT count(*) FROM measurements m WHERE m.t_ms >= ? AND m.t_ms < ? AND m.source IN ({ph})"
    return sql, count


def columns(params: dict) -> list:
    cols = list(RAW_COLUMNS)
    if params.get("include_public_ip"):
        cols.append(("Public IP", "public_ip", "text", 15))
    return cols


def csv_value(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    return v


def iter_rows(conn, s, a, b, params):
    sql, _ = raw_query(params)
    tz = s["timezone"]
    pkc = settings_store.PackageClock(s["packages"], tz)
    for r in conn.execute(sql, (a, b, *sources(params))):
        d = dict(r)
        d["_local"] = timeutil.local(d["t_ms"], tz).strftime("%Y-%m-%d %H:%M:%S")
        d["_quality"] = quality_text(d, pkc)
        yield d


def quality_text(d: dict, pkc) -> str | None:
    """'clean', or the measurement-quality labels that apply (speedmon/quality.py); empty for failures and imports."""
    if d["status"] != "ok" or d["source"] not in ("agent", "synthetic"):
        return None
    diag = (d.get("link_speed_mbps"), d.get("link_speed_mbps"), d.get("_diag_uuid"), d.get("background_rx_mbps"),
            d.get("background_tx_mbps"), d.get("pretest_rx_mbps"), d.get("pretest_tx_mbps"), d.get("cpu_load1"),
            d.get("cpu_temp_c"), d.get("throttled"), d.get("rx_errors_delta"), d.get("tx_errors_delta"),
            d.get("rx_dropped_delta"), d.get("tx_dropped_delta"))
    codes = quality.assess(True, d["source"], d["kind"], d["download_mbps"], pkc.at(d["t_ms"]), diag)
    names = [quality.FLAGS[c][0] for c in codes]
    return ("clean" + (f" ({', '.join(names)})" if names else "")) if quality.is_clean(codes) else ", ".join(names)


def count_rows(conn, a, b, params) -> int:
    _, q = raw_query(params)
    return conn.execute(q, (a, b, *sources(params))).fetchone()[0]


def preamble(conn, s, a, b, label, n, verification: dict | None) -> list[str]:
    head = integrity.head(conn)
    lines = [f"# Broadband performance raw data: {s['package_name']}",
             f"# Generated: {timeutil.fmt_local(timeutil.now_ms(), s['timezone'], True)} ({timeutil.now_iso()})",
             f"# Period: {label} ({timeutil.iso(a)} to {timeutil.iso(b)}, end exclusive)",
             f"# Records: {n}", f"# Software: speedmon {VERSION}, {jobs.EXPORT_VERSION}, hash scheme v{canonical.HASH_VERSION}",
             f"# Ledger head at export: {head['head']} ({head['entries']} entries)"]
    if verification:
        lines.append(f"# Integrity check at export: {'PASSED' if verification['ok'] else 'FAILED'} "
                     f"({verification['records']} records re-hashed, {len(verification['problems'])} problems)")
    return lines


def write_measurements(conn, s, a, b, params, fh, label="", progress=None, with_preamble=True, verification=None) -> int:
    n_total = count_rows(conn, a, b, params)
    if with_preamble:
        for line in preamble(conn, s, a, b, label, n_total, verification):
            fh.write(line + "\n")
    cols = columns(params)
    w = csv.writer(fh)
    w.writerow([c[1] if c[1] != "_local" else "local_time" for c in cols])
    n = 0
    for d in iter_rows(conn, s, a, b, params):
        w.writerow([csv_value(d.get(k)) for _, k, _, _ in cols])
        n += 1
        if progress and n % 20000 == 0:
            progress(n / max(n_total, 1))
    return n


def write_measurements_file(conn, s, a, b, params, path: Path, gz: bool = False, progress=None) -> int:
    verification = integrity.verify(conn)
    opener = (lambda: gzip.open(path, "wt", encoding="utf-8", newline="")) if gz else \
        (lambda: open(path, "w", encoding="utf-8", newline=""))
    with opener() as fh:
        return write_measurements(conn, s, a, b, params, fh, label=_label(s, a, b), progress=progress,
                                  verification=verification)


def _label(s, a, b):
    return f"{timeutil.fmt_local(a, s['timezone'])} to {timeutil.fmt_local(b, s['timezone'])}"


def _table_csv(zf, name, conn, sql, args, fmt=None) -> int:
    n = 0
    with zf.open(name, "w", force_zip64=True) as raw:
        fh = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        cur = conn.execute(sql, args)
        w = csv.writer(fh)
        w.writerow([c[0] for c in cur.description])
        for r in cur:
            w.writerow([csv_value(v) for v in r])
            n += 1
        fh.flush()
        fh.detach()
    return n


def write_zip(conn, s, settings, a, b, label, params, path: Path, progress=None) -> int:
    verification = integrity.verify(conn)
    if progress:
        progress(0.1)
    files = {}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        with zf.open("measurements.csv", "w", force_zip64=True) as raw:
            fh = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            n = write_measurements(conn, s, a, b, params, fh, label, with_preamble=False,
                                   progress=(lambda f: progress(0.1 + 0.5 * f)) if progress else None)
            fh.flush()
            fh.detach()
        files["measurements.csv"] = n
        if params.get("include_probes", True):
            files["probes.csv"] = _table_csv(zf, "probes.csv", conn, "SELECT * FROM probes WHERE t_ms >= ? AND t_ms < ? "
                                             "ORDER BY t_ms, id", (a, b))
        if progress:
            progress(0.75)
        files["events.csv"] = _table_csv(zf, "events.csv", conn, "SELECT * FROM events WHERE t_ms >= ? AND t_ms < ? ORDER BY t_ms",
                                         (a, b))
        files["annotations.csv"] = _table_csv(zf, "annotations.csv", conn,
                                              "SELECT uuid, start_ms, end_ms, title, body, category, created_at, updated_at, "
                                              "deleted_at FROM annotations ORDER BY start_ms", ())
        files["owner_flags.csv"] = _table_csv(zf, "owner_flags.csv", conn, "SELECT * FROM measurement_flags ORDER BY id", ())
        src = sources(params)
        ph = ",".join("?" * len(src))
        files["ledger.csv"] = _table_csv(
            zf, "ledger.csv", conn,
            "SELECT l.* FROM ledger l WHERE (l.stream = 'measurement' AND l.uuid IN (SELECT uuid FROM measurements WHERE t_ms >= ? "
            f"AND t_ms < ? AND source IN ({ph}))) OR (l.stream = 'diagnostic' AND l.uuid IN (SELECT d.uuid FROM diagnostics d "
            "WHERE d.t_ms >= ? AND d.t_ms < ?)) OR (l.stream = 'probe' AND ? AND l.uuid IN (SELECT uuid FROM probes WHERE t_ms >= ? "
            "AND t_ms < ?)) OR (l.stream = 'event' AND l.uuid IN (SELECT uuid FROM events WHERE t_ms >= ? AND t_ms < ?)) "
            "ORDER BY l.chain_index", (a, b, *src, a, b, int(params.get("include_probes", True)), a, b, a, b))
        files["diagnostics.csv"] = _table_csv(
            zf, "diagnostics.csv", conn,
            "SELECT " + ", ".join(c for c in canonical.FIELDS["diagnostic"] if c != "public_ip" or params.get("include_public_ip"))
            + (", NULL AS public_ip" if not params.get("include_public_ip") else "") +
            ", record_hash, received_at FROM diagnostics WHERE t_ms >= ? AND t_ms < ? ORDER BY t_ms", (a, b))
        if params.get("include_raw_json", True):
            nraw = 0
            with zf.open("raw_engine_output.jsonl", "w", force_zip64=True) as raw:
                for uid, text in conn.execute(f"SELECT uuid, raw FROM measurements WHERE t_ms >= ? AND t_ms < ? AND source IN "
                                              f"({ph}) ORDER BY t_ms, id", (a, b, *src)):
                    raw.write((json.dumps({"uuid": uid, "raw": text}, ensure_ascii=False) + "\n").encode("utf-8"))
                    nraw += 1
            files["raw_engine_output.jsonl"] = nraw
        if progress:
            progress(0.9)
        report = jobs.report_data(conn, s, a, b, label)
        zf.writestr("report.json", json.dumps(report, indent=1, default=str))
        zf.writestr("integrity.json", json.dumps(verification, indent=1))
        zf.writestr("verify_export.py", (Path(__file__).parent / "verify_export.py").read_text())
        zf.writestr("README.txt", README.format(label=label, generated=report["generated_local"], software=report["software"],
                                                head=report["ledger"]["head"], n=n,
                                                verdict="PASSED" if verification["ok"] else "FAILED"))
        manifest = {"export_version": jobs.EXPORT_VERSION, "software": report["software"], "generated_at": report["generated_at"],
                    "range": report["range"], "records": files, "hash_scheme": canonical.HASH_VERSION,
                    "hash_fields": canonical.FIELDS, "real_fields": sorted(canonical.REAL_FIELDS),
                    "ledger_head": report["ledger"], "integrity_ok": verification["ok"],
                    "public_ip_included": bool(params.get("include_public_ip")),
                    "files": {i.filename: {"bytes": i.file_size, "crc32": f"{i.CRC:08x}"} for i in zf.infolist()}}
        zf.writestr("manifest.json", json.dumps(manifest, indent=1))
    return n


README = """Broadband evidence pack
=======================

Period:        {label}
Generated:     {generated}
Software:      {software}
Measurements:  {n}
Ledger head:   {head}
Integrity check of the whole database at export time: {verdict}

Files
  measurements.csv         every speed test in the period, one row each (successful and failed), with the
                           diagnostics captured alongside it and each record's hash and ledger position
  probes.csv               the light connectivity probes between tests (latency, loss, DNS, outage state)
  events.csv               monitor events: start/stop, reboots, link changes, public-IP changes, deferred tests
  diagnostics.csv          the diagnostic records as stored (separate from the measurements by design)
  annotations.csv          the owner's timeline notes (they never alter any measurement)
  owner_flags.csv          the owner's per-test corrections, e.g. "exclude: I was downloading a game"
  raw_engine_output.jsonl  the speed-test engine's own output for each test (local IPs and MAC removed at source)
  ledger.csv               hash-chain entries for the records above
  report.json              the summary figures shown in the report
  integrity.json           the full integrity check result
  manifest.json            what is in this pack, the hash scheme and the field lists
  verify_export.py         re-computes every record hash from these CSV files and checks the chain links:
                             python3 verify_export.py .

Times are UTC in ISO-8601 (and as epoch milliseconds in t_ms); local times use the time zone and offset shown
in each row. Speeds are megabits per second (1 Mbps = 1,000,000 bits/s).
"""
