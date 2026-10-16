"""Export jobs and the report figures shared by Evidence mode and the XLSX report.

Exports run one at a time in a background thread and write to var/exports/<uuid>/ (0700). Rows are streamed
from SQLite, so memory stays flat however many years are exported. There is no row limit: XLSX raw data
continues onto "Raw Data 2", "Raw Data 3", ... at Excel's 1,048,576-row sheet limit. Each finished file's
SHA-256 and the ledger head at export time are recorded in the exports table (and shown in the UI), so a copy
handed to someone else can later be matched to this system.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import statistics
import threading
import traceback
import uuid as uuidlib
from pathlib import Path

from .. import analytics, integrity, settings_store, timeutil
from ..config import VERSION

FORMATS = ("xlsx", "csv", "csv.gz", "zip")
EXPORT_VERSION = "speedmon-export/1"


def validate_params(body: dict) -> dict:
    fmt = body.get("format", "xlsx")
    if fmt not in FORMATS:
        raise ValueError("format must be xlsx, csv, csv.gz or zip")
    return {"format": fmt, "include_legacy": bool(body.get("include_legacy", True)),
            "include_probes": bool(body.get("include_probes", True)),
            "include_public_ip": bool(body.get("include_public_ip", False)),
            "include_raw_json": bool(body.get("include_raw_json", True))}


# ---------------------------------------------------------------- report figures

def probe_latency(conn, a: int, b: int) -> dict:
    ext = sorted(r[0] for r in conn.execute("SELECT ext_rtt_ms FROM probes WHERE t_ms >= ? AND t_ms < ? AND ext_rtt_ms IS NOT NULL",
                                            (a, b)))
    gw = sorted(r[0] for r in conn.execute("SELECT gw_rtt_ms FROM probes WHERE t_ms >= ? AND t_ms < ? AND gw_rtt_ms IS NOT NULL",
                                           (a, b)))
    row = conn.execute("SELECT count(*), avg(ext_loss_pct), max(ext_loss_pct), sum(ext_loss_pct > 0), "
                       "sum(state IN ('wan_down','dns_fail')), sum(state IN ('lan_fault','link_down')), "
                       "sum(dns_ok = 0) FROM probes WHERE t_ms >= ? AND t_ms < ?", (a, b)).fetchone()
    pct = analytics.percentile
    return {"n": row[0], "ext_median": pct(ext, 50), "ext_p95": pct(ext, 95) if len(ext) >= 20 else None,
            "ext_min": ext[0] if ext else None, "ext_max": ext[-1] if ext else None,
            "gw_median": pct(gw, 50), "gw_p95": pct(gw, 95) if len(gw) >= 20 else None,
            "loss_avg": row[1], "loss_max": row[2], "probes_with_loss": row[3], "probes_outage": row[4] or 0,
            "probes_local": row[5] or 0, "dns_failures": row[6] or 0}


def report_data(conn, s: dict, a: int, b: int, label: str) -> dict:
    tests = analytics.load_tests(conn, a, b, s)
    bt = analytics.basis(tests)
    sm = analytics.summary(tests, s)
    sm["excluded"] = analytics.excluded_count(conn, a, b, s)
    days = analytics.daily(bt, s)
    hours = analytics.hourly(bt, s)
    dows = analytics.weekday(bt, s)
    ta = analytics.time_analysis(tests, s)
    degr = analytics.degradation_incidents(tests, s)
    outs = analytics.outage_incidents(conn, a, b)
    cong = analytics.congestion(tests, s)
    cov = analytics.coverage(conn, a, b, s, tests)
    legacy_n = conn.execute("SELECT count(*) FROM measurements WHERE source LIKE 'legacy%' AND t_ms >= ? AND t_ms < ?",
                            (a, b)).fetchone()[0]
    servers = [dict(r) for r in conn.execute(
        "SELECT server_id, server_sponsor, server_location, count(*) AS n, sum(status = 'ok') AS n_ok, "
        "avg(download_mbps) AS mean FROM measurements WHERE t_ms >= ? AND t_ms < ? AND source IN ('agent','synthetic') "
        "GROUP BY server_id ORDER BY n DESC LIMIT 15", (a, b))]
    engines = [dict(r) for r in conn.execute(
        "SELECT engine, engine_version, count(*) AS n FROM measurements WHERE t_ms >= ? AND t_ms < ? GROUP BY engine, "
        "engine_version ORDER BY n DESC", (a, b))]
    notes = [dict(r) for r in conn.execute(
        "SELECT title, body, category, start_ms, end_ms FROM annotations WHERE deleted_at IS NULL AND start_ms < ? AND "
        "coalesce(end_ms, start_ms) >= ? ORDER BY start_ms", (b, a))]
    worst_hours = sorted([h for h in hours if h.get("n_ok", 0) >= 5 and h.get("median") is not None],
                         key=lambda h: h["median"])[:3]
    dl = sm["download"]
    answers = {
        "highest": dl["max"], "typical": dl["median"], "consistent_p10": dl["p"]["10"], "lowest": dl["min"],
        "pct_target": sm["pct_target"], "worst_hours": worst_hours,
        "worst_days": ta["days"]["worst"][:3], "congestion": cong.get("findings", []),
        "congestion_message": cong.get("message"),
        "incidents": len(degr), "isolated_poor": _isolated_poor(tests, s),
    }
    head = integrity.head(conn)
    shown_from = a if a > 86_400_000 else (sm["first_ms"] or a)          # "All data" starts at the first record
    return {
        "generated_at": timeutil.now_iso(), "generated_local": timeutil.fmt_local(timeutil.now_ms(), s["timezone"], True),
        "software": f"speedmon {VERSION}", "export_version": EXPORT_VERSION,
        "range": {"from_ms": shown_from, "to_ms": b, "label": label,
                  "from_local": timeutil.fmt_local(shown_from, s["timezone"]), "to_local": timeutil.fmt_local(b, s["timezone"])},
        "settings": {**{k: sm["package"].get(k) for k in ("advertised_down", "advertised_up", "minimum_guaranteed_down",
                                                         "target_down", "degraded_down", "severe_down", "target_up",
                                                         "degraded_up", "tiers")},
                     "package_name": sm["package"]["name"],
                     **{k: s[k] for k in ("interval_min", "confirm_count", "burst_interval_min", "timezone", "engine",
                                          "fallback_engine", "server_mode", "servers", "include_legacy")}},
        "packages": sm["packages"],
        "summary": sm, "time": ta, "daily": days, "hourly": hours, "weekday": dows,
        "degradations": degr, "outages": outs, "congestion": cong, "coverage": cov,
        "histogram": analytics.histogram([x.dl for x in bt if x.ok], 2 * analytics.bin_width(sm["package"]["advertised_down"]),
                                         sm["package"]["advertised_down"]),
        "latency": probe_latency(conn, a, b), "guarantee": analytics.guarantee_watch(tests), "legacy_in_range": legacy_n, "servers": servers, "engines": engines,
        "annotations": notes, "answers": answers, "ledger": head,
        "methodology": methodology({**s, **{k: sm["package"].get(k) for k in ("advertised_down", "advertised_up",
                                     "target_down", "degraded_down", "severe_down")}}, sm["packages"]),
    }


def _isolated_poor(tests, s) -> dict:
    seq = [x for x in tests if not x.local_fail]
    poor = [i for i, x in enumerate(seq) if _poor(x, s)]
    isolated = sum(1 for i in poor if (i == 0 or not _poor(seq[i - 1], s)) and (i + 1 >= len(seq) or not _poor(seq[i + 1], s)))
    return {"poor": len(poor), "isolated": isolated, "in_runs": len(poor) - isolated}


def _poor(x, s):
    return (not x.ok) or x.dl < x.pk["degraded_down"]


def methodology(s: dict, packages: list | None = None) -> list[tuple[str, str]]:
    pk_text = "; ".join(f"from {p['from']}: {p['name']} (advertised {p['advertised_down']:g}/{p['advertised_up']:g} Mbps, "
                        f"on target ≥ {p['target_down']:g}, degraded < {p['degraded_down']:g}, severe < {p['severe_down']:g})"
                        for p in (packages or [])) or "one package throughout"
    return [
        ("Package in force", "Every test is judged against the broadband package in force on its date, so an upgrade "
         f"doesn't change how earlier results are assessed. In this period: {pk_text}."),
        ("What was measured", "Full throughput tests (download, upload, idle and loaded latency, jitter, packet loss) run "
         "by a Raspberry Pi 5 connected by Ethernet to the home router, which is connected by Ethernet to the Virgin Media "
         "Hub in modem mode. Between full tests, light connectivity probes (pings to the router and to two external "
         "anchors, a DNS lookup and a tiny HTTPS request) run every "
         f"{s.get('probe_interval_s', 60)} seconds."),
        ("Test engine", "Ookla Speedtest CLI (official, multi-connection) is the primary engine; speedtest-go (open "
         "source, same server network) is used automatically when the primary engine errors, so an engine or server "
         "problem can be told apart from a line problem. Each record states the engine and version used."),
        ("Schedule", f"One scheduled test per {s['interval_min']}-minute slot, at a random moment inside the slot, so "
         "every time of day is sampled evenly. After a result below the degraded threshold "
         f"({s['degraded_down']:g} Mbps) up to {s['confirm_count']} confirmation tests run; if they agree, tests run every "
         f"{s['burst_interval_min']} minutes until the line recovers. A daily cap prevents runaway testing."),
        ("Which tests the statistics use", "Percentages, averages, percentiles, daily/hourly/weekday figures and "
         "the congestion detector use scheduled tests only, because their timing is random. Confirmation, burst and "
         "manual tests deliberately sample bad moments more often and would bias them. They are in the raw data and "
         "count towards streaks and incidents."),
        ("Failures", "A failed test is recorded with its reason, never dropped. Failures where the router answered but "
         "the Internet did not are counted as outages. Failures caused by the Pi's own link or the router not "
         "answering are reported but not counted against the ISP. Speed statistics use successful tests only; a "
         "failure is never counted as 0 Mbps."),
        ("Sampling, not continuous measurement", "Speed tests are samples. 'Longest sequence' figures count consecutive "
         "tests, and the period between the first and last test is labelled as the sampled span. It is not a claim "
         "that the speed held continuously in between. Outage durations come from the probes and are given as a "
         "lower bound (first to last failing probe) and an upper bound (last good probe before to first good probe after)."),
        ("Percentiles", "Linear interpolation between closest ranks, the same as Excel's PERCENTILE.INC. P1 and P99 are "
         "only reported with at least 100 tests, P5/P95 with 20, P10/P90 with 10."),
        ("Congestion detector", analytics.congestion.__doc__.strip().replace("\n    ", " ")),
        ("Anomalies", "A test is flagged as anomalous when its download is more than 3.5 robust standard deviations "
         "(median absolute deviation x 1.4826) away from the median of earlier tests at the same local hour (+-1 h) and "
         "day type in the previous 28 days, with at least 12 reference tests."),
        ("Measurement ceiling", "The Pi's built-in Ethernet port is 1 Gbit/s, so about 940 Mbps is the most it can achieve "
         "over it (a 2.5 GbE adapter raises that; each test records the link speed it ran on). "
         f"The package is advertised at {s['advertised_down']:g} Mbps down / {s['advertised_up']:g} Mbps "
         f"up. The target threshold ({s['target_down']:g} Mbps) is set below the 1 GbE ceiling so the Pi's own port is "
         "not counted against the ISP."),
        ("Local diagnostics", "Each test records the Pi's link speed and duplex, interface errors and drops, CPU load and "
         "temperature, throttling flags, memory, other traffic on the Pi's own port during the test, router "
         "reachability and public-IP changes, so a slow result can be checked against problems on the Pi side. "
         "Traffic from other devices in the home is not visible to the Pi."),
        ("Legacy data", "Results from 26 June 2025 to 6 October 2026 come from the earlier speedtest-cli scripts. That "
         "tool is single-process Python, often ran twice at once, and sometimes picked distant servers, so those "
         "results are marked legacy and kept out of the statistics unless the include-legacy setting is on. They are "
         "preserved unchanged in the raw data with their original CSV line."),
        ("Times", f"Stored in UTC. Days and hours are in {s['timezone']} local time, including daylight-saving changes. "
         "Each raw row gives the UTC time, the local time and the UTC offset that applied."),
        ("Integrity", "Raw records cannot be edited or deleted by the application (database triggers refuse it). Each "
         "record has a SHA-256 hash of its canonical content, and every record is chained to the previous one in the "
         "order it was stored. The Integrity sheet gives the chain head at export time. Owner corrections and "
         "annotations are stored separately and never change a raw record. This is tamper-evidence that can be checked "
         "independently, not a legal certification."),
    ]


# ---------------------------------------------------------------- job runner

class Exporter:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.thread = None
        settings.export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path_for(self, uid: str, name: str) -> Path:
        return self.settings.export_dir / uid / name

    def queue(self, conn, fmt, label, a, b, params) -> str:
        uid = str(uuidlib.uuid4())
        conn.execute("INSERT INTO exports (uuid, created_at, format, range_label, from_ms, to_ms, params, status) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')", (uid, timeutil.now_iso(), fmt, label, a, b, json.dumps(params)))
        return uid

    def kick(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True, name="exporter")
                self.thread.start()

    def cleanup(self):
        cutoff = timeutil.iso(timeutil.now_ms() - self.settings.export_keep_hours * 3600_000)
        with self.db.tx() as conn:
            conn.execute("UPDATE exports SET status = 'failed', error = 'interrupted by a restart' WHERE status IN ('queued','running')")
            for r in conn.execute("SELECT uuid FROM exports WHERE status = 'done' AND finished_at < ?", (cutoff,)).fetchall():
                shutil.rmtree(self.settings.export_dir / r["uuid"], ignore_errors=True)
                conn.execute("UPDATE exports SET status = 'expired' WHERE uuid = ?", (r["uuid"],))

    def _run(self):
        while True:
            with self.db.tx() as conn:
                row = conn.execute("SELECT * FROM exports WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
                if not row:
                    return
                conn.execute("UPDATE exports SET status = 'running' WHERE id = ?", (row["id"],))
            self.run_one(dict(row))

    def progress(self, uid: str, frac: float):
        with self.db.tx() as conn:
            conn.execute("UPDATE exports SET progress = ? WHERE uuid = ?", (round(min(max(frac, 0), 1), 4), uid))

    def run_one(self, job: dict) -> None:
        from . import csvx, xlsx
        uid = job["uuid"]
        params = json.loads(job["params"])
        out_dir = self.settings.export_dir / uid
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with self.db.read() as conn:
                s = settings_store.get_all(conn)
                a, b = job["from_ms"], job["to_ms"]
                first = conn.execute("SELECT min(t_ms) FROM measurements WHERE t_ms >= ?", (a,)).fetchone()[0]
                d0 = timeutil.local(max(a, first or a), s["timezone"]).strftime("%Y%m%d")
                d1 = timeutil.local(b - 1, s["timezone"]).strftime("%Y%m%d")
                stamp = timeutil.local(timeutil.now_ms(), s["timezone"]).strftime("%Y%m%d-%H%M")
                ext = {"xlsx": "xlsx", "csv": "csv", "csv.gz": "csv.gz", "zip": "zip"}[job["format"]]
                name = f"broadband-evidence_{d0}-{d1}_generated-{stamp}.{ext}"
                path = out_dir / name
                cb = lambda f: self.progress(uid, f)
                if job["format"] == "xlsx":
                    rows = xlsx.write(conn, s, self.settings, a, b, job["range_label"], params, path, cb)
                elif job["format"] in ("csv", "csv.gz"):
                    rows = csvx.write_measurements_file(conn, s, a, b, params, path, gz=job["format"] == "csv.gz", progress=cb)
                else:
                    rows = csvx.write_zip(conn, s, self.settings, a, b, job["range_label"], params, path, cb)
                head = integrity.head(conn)["head"]
            digest = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            with self.db.tx() as conn:
                conn.execute("UPDATE exports SET status = 'done', progress = 1, finished_at = ?, rows = ?, file_name = ?, "
                             "file_bytes = ?, file_sha256 = ?, ledger_head = ?, error = NULL WHERE uuid = ?",
                             (timeutil.now_iso(), rows, name, path.stat().st_size, digest.hexdigest(), head, uid))
        except Exception as exc:
            shutil.rmtree(out_dir, ignore_errors=True)
            with self.db.tx() as conn:
                conn.execute("UPDATE exports SET status = 'failed', finished_at = ?, error = ? WHERE uuid = ?",
                             (timeutil.now_iso(), f"{type(exc).__name__}: {exc}"[:500], uid))
            traceback.print_exc()


def _median(v):
    return statistics.median(v) if v else None
