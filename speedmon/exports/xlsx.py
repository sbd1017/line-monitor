"""The XLSX broadband performance report (XlsxWriter, constant_memory: rows are streamed to disk).

Sheets: Summary (with native charts) · Daily · Hourly & Weekday · Incidents · Annotations · Methodology ·
Integrity · Raw Data [· Raw Data 2 · ...]. Raw rows continue onto a new sheet at Excel's 1,048,576-row limit;
nothing is ever left out, and the Summary says how many raw sheets there are.
"""
from __future__ import annotations

from datetime import datetime

import xlsxwriter

from .. import canonical, integrity, timeutil
from . import csvx, jobs

NAVY, INK, MUTED, LINE, TEAL, PAPER = "#0B1B2E", "#13263B", "#5B6B7D", "#D5DCE4", "#0E8A96", "#F4F7FA"
GOOD, WARN, BAD, SEVERE = "#1F8F5F", "#C98A12", "#C2452D", "#8E1B1B"


class Formats:
    def __init__(self, wb):
        add = wb.add_format
        base = {"font_name": "Calibri", "font_size": 10, "valign": "vcenter"}
        self.title = add({**base, "font_size": 20, "bold": True, "font_color": "#FFFFFF", "bg_color": NAVY})
        self.band = add({**base, "bg_color": NAVY})
        self.subtitle = add({**base, "font_size": 11, "font_color": "#A9BCD0", "bg_color": NAVY})
        self.kicker = add({**base, "font_size": 9, "bold": True, "font_color": "#56D3DF", "bg_color": NAVY})
        self.section = add({**base, "font_size": 12, "bold": True, "font_color": INK, "bottom": 2, "bottom_color": TEAL})
        self.section_fill = add({**base, "bottom": 2, "bottom_color": TEAL})
        self.label = add({**base, "font_color": MUTED})
        self.text = add({**base, "font_color": INK})
        self.wrap = add({**base, "font_color": INK, "text_wrap": True, "valign": "top"})
        self.wrap_muted = add({**base, "font_color": MUTED, "text_wrap": True, "valign": "top", "italic": True})
        self.bold = add({**base, "bold": True, "font_color": INK})
        self.th = add({**base, "bold": True, "font_color": "#FFFFFF", "bg_color": INK, "text_wrap": True, "align": "center"})
        self.th_left = add({**base, "bold": True, "font_color": "#FFFFFF", "bg_color": INK, "text_wrap": True})
        self.mbps = add({**base, "bold": True, "font_color": INK, "num_format": '#,##0.0" Mbps"'})
        self.mbps_plain = add({**base, "font_color": INK, "num_format": "#,##0.0"})
        self.ms = add({**base, "bold": True, "font_color": INK, "num_format": '0.0" ms"'})
        self.ms_plain = add({**base, "font_color": INK, "num_format": "0.0"})
        self.pct = add({**base, "bold": True, "font_color": INK, "num_format": "0.0%"})
        self.pct_plain = add({**base, "font_color": INK, "num_format": "0.0%"})
        self.wrap_bold = add({**base, "bold": True, "font_color": INK, "text_wrap": True, "valign": "top"})
        self.int = add({**base, "bold": True, "font_color": INK, "num_format": "#,##0"})
        self.int_plain = add({**base, "font_color": INK, "num_format": "#,##0"})
        self.dt = add({**base, "bold": True, "font_color": INK, "num_format": "ddd d mmm yyyy hh:mm", "align": "left"})
        self.dt_plain = add({**base, "font_color": INK, "num_format": "yyyy-mm-dd hh:mm", "align": "left"})
        self.date_plain = add({**base, "font_color": INK, "num_format": "ddd yyyy-mm-dd", "align": "left"})
        self.raw_dt = add({**base, "num_format": "yyyy-mm-dd hh:mm:ss"})
        self.raw_text = add(base)
        self.raw_n1 = add({**base, "num_format": "0.0"})
        self.raw_n2 = add({**base, "num_format": "0.00"})
        self.raw_int = add({**base, "num_format": "0"})
        self.ok = add({**base, "bold": True, "font_color": GOOD})
        self.bad = add({**base, "bold": True, "font_color": BAD})
        self.mono = add({**base, "font_name": "Consolas", "font_size": 9, "font_color": INK})
        self.cf_good = add({"bg_color": "#DDF2E6", "font_color": "#145C3D"})
        self.cf_warn = add({"bg_color": "#FBEFD5", "font_color": "#7A5208"})
        self.cf_bad = add({"bg_color": "#F8DCD5", "font_color": "#8A2A17"})
        self.cf_severe = add({"bg_color": "#E9B9B2", "font_color": "#5E0E0E", "bold": True})
        self.cf_fail = add({"font_color": BAD, "bold": True})


def _pct(v) -> str:
    return "—" if v is None else f"{v:.1f}%"


def P(v):
    """Analytics give percentages as 0-100; Excel wants fractions with a % format."""
    return None if v is None else v / 100


def _local(ms, tz):
    return None if ms is None else timeutil.local(ms, tz).replace(tzinfo=None)


def _span(ms):
    if ms is None:
        return ""
    m = ms / 60000
    if m < 120:
        return f"{m:.0f} min"
    h = m / 60
    return f"{h:.1f} h" if h < 72 else f"{h / 24:.1f} days"


class Sheet:
    """Row cursor for constant_memory worksheets (rows must be written top to bottom)."""

    def __init__(self, ws, f):
        self.ws, self.f, self.r = ws, f, 0

    def row(self, cells, height=None):
        for c, cell in enumerate(cells):
            if cell is None:
                continue
            if isinstance(cell, tuple):
                value, fmt = cell
            else:
                value, fmt = cell, None
            if value is None:
                if fmt is not None:
                    self.ws.write_blank(self.r, c, None, fmt)
                continue
            if isinstance(value, datetime):
                self.ws.write_datetime(self.r, c, value, fmt)
            else:
                self.ws.write(self.r, c, value, fmt)
        if height:
            self.ws.set_row(self.r, height)
        self.r += 1

    def gap(self, n=1):
        self.r += n


def write(conn, s, settings, a, b, label, params, path, progress=None) -> int:
    tz = s["timezone"]
    rd = jobs.report_data(conn, s, a, b, label)
    s = {**s, **rd["settings"]}                    # labels and formats follow the package in force for this period
    if progress:
        progress(0.05)
    verification = integrity.verify(conn)
    if progress:
        progress(0.12)
    n_raw = csvx.count_rows(conn, a, b, params)
    per_sheet = settings.raw_rows_per_sheet
    n_sheets = max(1, -(-n_raw // per_sheet))
    raw_names = ["Raw Data"] + [f"Raw Data {i}" for i in range(2, n_sheets + 1)]

    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
                                         "strings_to_numbers": False, "default_date_format": "yyyy-mm-dd hh:mm:ss",
                                         "nan_inf_to_errors": True})
    wb.set_properties({"title": f"Broadband performance report: {label}", "subject": s["package_name"],
                       "author": "speedmon", "comments": f"{rd['software']}; {jobs.EXPORT_VERSION}",
                       "keywords": "broadband, speed test, evidence"})
    if n_raw > 400_000:
        # A raw sheet's XML passes 4 GB uncompressed somewhere above ~1 million rows. ZIP64 is required then; for
        # small files it's left off because Excel offers to "repair" a ZIP64 file that didn't need it.
        wb.use_zip64()
    f = Formats(wb)
    ws_sum = wb.add_worksheet("Summary")
    ws_day = wb.add_worksheet("Daily")
    ws_hour = wb.add_worksheet("Hourly & Weekday")
    ws_inc = wb.add_worksheet("Incidents")
    ws_ann = wb.add_worksheet("Annotations")
    ws_meth = wb.add_worksheet("Methodology")
    ws_int = wb.add_worksheet("Integrity")
    for ws in (ws_sum, ws_day, ws_hour, ws_inc, ws_ann, ws_meth, ws_int):
        ws.hide_gridlines(2)
    ws_sum.set_tab_color(TEAL)

    daily_rows = _write_daily(ws_day, f, rd, s)
    hour_tables = _write_hourly(ws_hour, f, rd, s)
    _write_summary(wb, ws_sum, f, rd, s, raw_names, n_raw, verification, daily_rows, hour_tables)
    _write_incidents(ws_inc, f, rd, tz)
    _write_annotations(ws_ann, f, conn, rd, tz, a, b)
    _write_methodology(ws_meth, f, rd)
    _write_integrity(ws_int, f, conn, rd, verification, n_raw, raw_names, params)
    if progress:
        progress(0.2)

    cols = csvx.columns(params)
    raw_fmt = {"dt": f.raw_dt, "text": f.raw_text, "n1": f.raw_n1, "n2": f.raw_n2, "int": f.raw_int}
    status_col = next(i for i, c in enumerate(cols) if c[1] == "status")
    dl_col = next(i for i, c in enumerate(cols) if c[1] == "download_mbps")
    rows = csvx.iter_rows(conn, s, a, b, params)
    written = 0
    for idx, name in enumerate(raw_names):
        ws = wb.add_worksheet(name)
        ws.set_tab_color("#8496A9")
        for c, (_, _, kind, width) in enumerate(cols):
            ws.set_column(c, c, width, raw_fmt[kind])
        ws.set_row(0, 30)
        for c, (title, _, _, _) in enumerate(cols):
            ws.write(0, c, title, f.th)
        ws.freeze_panes(1, 1)
        take = min(per_sheet, n_raw - written) if n_raw else 0
        r = 0
        for d in rows:
            r += 1
            vals = []
            for _, key, kind, _ in cols:
                v = d.get(key)
                if key == "_local":
                    v = _local(d["t_ms"], tz)
                vals.append(v)
            ws.write_row(r, 0, vals)
            written += 1
            if progress and written % 20000 == 0:
                progress(0.2 + 0.79 * written / max(n_raw, 1))
            if r >= take:
                break
        last = max(r, 1)
        ws.autofilter(0, 0, last, len(cols) - 1)
        ws.conditional_format(1, dl_col, last, dl_col, {"type": "cell", "criteria": ">=", "value": s["target_down"],
                                                       "format": f.cf_good})
        ws.conditional_format(1, dl_col, last, dl_col, {"type": "cell", "criteria": "<", "value": s["severe_down"],
                                                       "format": f.cf_severe})
        ws.conditional_format(1, dl_col, last, dl_col, {"type": "cell", "criteria": "<", "value": s["degraded_down"],
                                                       "format": f.cf_bad})
        ws.conditional_format(1, status_col, last, status_col, {"type": "cell", "criteria": "==", "value": '"failed"',
                                                               "format": f.cf_fail})
        if idx == 0 and n_raw == 0:
            ws.write(1, 0, "No measurements in this period.", f.label)
    if written != n_raw:
        raise RuntimeError(f"raw rows written ({written}) differ from rows counted ({n_raw}); export aborted")
    wb.close()
    return written


# ---------------------------------------------------------------- Daily

DAILY_HEAD = ["Date", "Tests", "Successful", "Failed", "Mean (Mbps)", "Median (Mbps)", "Min (Mbps)", "Max (Mbps)",
              "P10 (Mbps)", "On target (%)", "Degraded (%)", "Upload mean (Mbps)", "Ping median (ms)", "Target (Mbps)",
              "Degraded below (Mbps)"]


def _write_daily(ws, f, rd, s):
    sh = Sheet(ws, f)
    widths = [16, 7, 10, 7, 11, 12, 10, 10, 10, 10, 10, 12, 11, 10, 12]
    for c, w in enumerate(widths):
        ws.set_column(c, c, w)
    ws.set_row(0, 30)
    sh.row([(h, f.th) for h in DAILY_HEAD])
    ws.freeze_panes(1, 1)
    days = rd["daily"]
    for d in days:
        sh.row([(datetime.fromisoformat(d["day"]), f.date_plain), (d["n"], f.int_plain), (d["n_ok"], f.int_plain),
                (d["failed"], f.int_plain), (d.get("mean"), f.mbps_plain), (d.get("median"), f.mbps_plain),
                (d.get("min"), f.mbps_plain), (d.get("max"), f.mbps_plain), (d.get("p10"), f.mbps_plain),
                (P(d.get("pct_target")), f.pct_plain), (P(d.get("pct_degraded")), f.pct_plain), (d.get("ul_mean"), f.mbps_plain),
                (d.get("ping_median"), f.ms_plain), (d.get("target_down", s["target_down"]), f.mbps_plain),
                (d.get("degraded_down", s["degraded_down"]), f.mbps_plain)])
    n = len(days)
    if n:
        ws.autofilter(0, 0, n, len(DAILY_HEAD) - 1)
        ws.conditional_format(1, 5, n, 5, {"type": "3_color_scale", "min_type": "num", "min_value": s["severe_down"],
                                           "mid_type": "num", "mid_value": s["degraded_down"], "max_type": "num",
                                           "max_value": s["target_down"], "min_color": "#E9B9B2", "mid_color": "#FBEFD5",
                                           "max_color": "#DDF2E6"})
        ws.conditional_format(1, 9, n, 9, {"type": "data_bar", "bar_color": "#7FC9D1", "bar_solid": True,
                                           "min_type": "num", "min_value": 0, "max_type": "num", "max_value": 1})
    else:
        sh.row([("No tests in this period.", f.label)])
    return n


# ---------------------------------------------------------------- Hourly & Weekday (+ chart tables)

def _write_hourly(ws, f, rd, s):
    sm = rd["summary"]
    hours = rd["hourly"]
    week = rd["weekday"]
    hist = rd["histogram"]
    target_rows = [("On target", None), ("Below target", None), ("Degraded", None), ("Severe", None), ("Failed", None)]
    ok = sm["n_ok"]
    if ok or sm["n"]:
        dl = sm
        n_ok = sm["n_ok"]
        on = round((dl["pct_target"] or 0) * n_ok / 100)
        deg_all = round((dl["pct_degraded"] or 0) * n_ok / 100)
        sev = round((dl["pct_severe"] or 0) * n_ok / 100)
        below = n_ok - on - deg_all
        target_rows = [("On target", on), ("Below target, not degraded", below), ("Degraded", deg_all - sev),
                       ("Severely degraded", sev), ("Failed (ISP side)", sm["n_failed_isp"] if "n_failed_isp" in sm else 0)]
    widths = {0: 8, 1: 7, 2: 11, 3: 12, 4: 10, 5: 10, 6: 11, 7: 11, 8: 12, 9: 11, 10: 3, 11: 12, 12: 7, 13: 11, 14: 12,
              15: 10, 16: 11, 17: 3, 18: 14, 19: 8, 20: 10, 21: 3, 22: 26, 23: 9}
    for c, w in widths.items():
        ws.set_column(c, c, w)
    sh = Sheet(ws, f)
    sh.row([("Hourly profile (local time)", f.section)] + [("", f.section_fill)] * 9 + [None, ("By day of week", f.section)]
           + [("", f.section_fill)] * 5 + [None, ("Download distribution", f.section), ("", f.section_fill), ("", f.section_fill),
                                           None, ("Target achievement", f.section), ("", f.section_fill)])
    head_h = ["Hour", "Tests", "Mean (Mbps)", "Median (Mbps)", "P10 (Mbps)", "P90 (Mbps)", "On target (%)", "Degraded (%)",
              "Upload median", "Ping median (ms)"]
    head_w = ["Day", "Tests", "Mean (Mbps)", "Median (Mbps)", "P10 (Mbps)", "On target (%)"]
    head_d = ["Band (Mbps)", "Tests", "Share (%)"]
    head_t = ["Result", "Tests"]
    ws.set_row(1, 30)
    sh.row([(h, f.th) for h in head_h] + [None] + [(h, f.th) for h in head_w] + [None] + [(h, f.th) for h in head_d] + [None]
           + [(h, f.th) for h in head_t])
    nrows = max(24, len(week), len(hist), len(target_rows))
    for i in range(nrows):
        cells = [None] * 24
        if i < 24:
            h = hours[i]
            cells[:10] = [(f"{i:02d}:00", f.text), (h.get("n", 0), f.int_plain), (h.get("mean"), f.mbps_plain),
                          (h.get("median"), f.mbps_plain), (h.get("p10"), f.mbps_plain), (h.get("p90"), f.mbps_plain),
                          (P(h.get("pct_target")), f.pct_plain), (P(h.get("pct_degraded")), f.pct_plain),
                          (h.get("ul_median"), f.mbps_plain), (h.get("ping_median"), f.ms_plain)]
        if i < len(week):
            w = week[i]
            cells[11:17] = [(w["name"], f.text), (w.get("n", 0), f.int_plain), (w.get("mean"), f.mbps_plain),
                            (w.get("median"), f.mbps_plain), (w.get("p10"), f.mbps_plain), (P(w.get("pct_target")), f.pct_plain)]
        if i < len(hist):
            hb = hist[i]
            cells[18:21] = [(f"{hb['lo']:.0f}–{hb['hi']:.0f}", f.text), (hb["n"], f.int_plain), (P(hb["pct"]), f.pct_plain)]
        if i < len(target_rows):
            cells[22:24] = [(target_rows[i][0], f.text), (target_rows[i][1], f.int_plain)]
        sh.row(cells)
    ws.conditional_format(2, 3, 25, 3, {"type": "3_color_scale", "min_type": "num", "min_value": s["severe_down"],
                                        "mid_type": "num", "mid_value": s["degraded_down"], "max_type": "num",
                                        "max_value": s["target_down"], "min_color": "#E9B9B2", "mid_color": "#FBEFD5",
                                        "max_color": "#DDF2E6"})
    ws.freeze_panes(2, 0)
    return {"hours_first": 2, "week_first": 2, "week_n": len(week), "hist_first": 2, "hist_n": len(hist),
            "target_first": 2, "target_n": len(target_rows)}


# ---------------------------------------------------------------- Summary

def _write_summary(wb, ws, f, rd, s, raw_names, n_raw, verification, n_days, ht):
    tz = s["timezone"]
    sm, ta, cov, ans = rd["summary"], rd["time"], rd["coverage"], rd["answers"]
    dl, ul = sm["download"], sm["upload"]
    for c, w in {0: 2, 1: 44, 2: 24, 3: 17, 4: 15, 5: 15, 6: 15, 7: 3}.items():
        ws.set_column(c, c, w)
    ws.set_column(8, 20, 9.5)
    sh = Sheet(ws, f)
    band = [("", f.band)] * 21
    sh.row([("", f.band), ("BROADBAND PERFORMANCE REPORT", f.kicker)] + band[2:], height=22)
    sh.row([("", f.band), (s["package_name"], f.title)] + band[2:], height=34)
    sh.row([("", f.band), (f"{rd['range']['label']}  ·  {rd['range']['from_local']} to {rd['range']['to_local']}  ·  "
                           f"generated {rd['generated_local']}", f.subtitle)] + band[2:], height=22)
    sh.row([("", f.band)] * 21, height=8)
    sh.gap()

    def section(title):
        sh.row([None, (title, f.section)] + [("", f.section_fill)] * 5, height=22)

    def kv(label, value, fmt=None, note=None):
        """Label in B; value in C; a note (or a long text value) spans D:G / C:G, wrapped."""
        r = sh.r
        long_text = isinstance(value, str) and len(value) > 24 and not note
        ws.write(r, 1, label, f.label)
        if long_text:
            ws.merge_range(r, 2, r, 6, value, fmt if fmt in (f.wrap, f.wrap_bold) else f.wrap_bold)
            ws.set_row(r, 15 * max(1, -(-len(value) // 95)))
        else:
            if isinstance(value, datetime):
                ws.write_datetime(r, 2, value, fmt or f.bold)
            elif value is None:
                ws.write_blank(r, 2, None, fmt or f.bold)
            else:
                ws.write(r, 2, value, fmt or f.bold)
            if note:
                ws.merge_range(r, 3, r, 6, note, f.wrap_muted)
                if len(note) > 70:
                    ws.set_row(r, 15 * -(-len(note) // 70))
        sh.r += 1

    section("Report details")
    kv("Selected period", rd["range"]["label"])
    kv("From", _local(rd["range"]["from_ms"], tz), f.dt)
    kv("To (exclusive)", _local(rd["range"]["to_ms"], tz), f.dt)
    kv("First measurement in period", _local(sm["first_ms"], tz), f.dt)
    kv("Last measurement in period", _local(sm["last_ms"], tz), f.dt)
    kv("Total measurements", sm["n_all"] + sm.get("excluded", 0), f.int,
       "All kinds, including confirmation/burst/manual tests and excluded tests")
    kv("Scheduled tests (basis for statistics)", sm["n"], f.int,
       "Randomly timed scheduled tests. Statistics below use these (see Methodology)")
    kv("Successful", sm["n_ok"], f.int)
    kv("Failed: Internet/ISP side", sm["n_failed_isp"], f.int, f"of which confirmed outages: {sm['n_outage']}")
    kv("Failed: Pi/LAN side (not counted against ISP)", sm["n_local"], f.int)
    kv("Excluded by the owner (with reasons)", sm.get("excluded", 0), f.int)
    kv("Monitoring coverage", P(cov.get("pct")), f.pct, "Share of the period the monitor was running (from 1-minute probes)")
    kv("Scheduled tests achieved", P(cov.get("tests_pct")), f.pct,
       f"{cov.get('tests_scheduled') or 0:,} of about {cov.get('tests_expected') or 0:,} expected at the current interval")
    kv("Package", s["package_name"])
    kv("Advertised download / upload", f"{s['advertised_down']:g} / {s['advertised_up']:g} Mbps")
    if len(rd["packages"]) > 1:
        kv("Package changes in this period", "; ".join(
            f"from {p['from']}: {p['name']} ({p['advertised_down']:g}/{p['advertised_up']:g} Mbps, {p['n']:,} tests, "
            f"median {p['median'] or 0:.0f} Mbps, {p['pct_target'] or 0:.1f}% on target)" for p in rd["packages"])
           + ". Each test is judged against its own package.", f.wrap)
    if s.get("minimum_guaranteed_down"):
        kv("Minimum guaranteed download", s["minimum_guaranteed_down"], f.mbps)
    kv("Thresholds (download)", f"on target ≥ {s['target_down']:g} · degraded < {s['degraded_down']:g} · severe < {s['severe_down']:g} Mbps")
    kv("Thresholds (upload)", f"on target ≥ {s['target_up']:g} · degraded < {s['degraded_up']:g} Mbps")
    eng = ", ".join(f"{e['engine']} {e['engine_version'] or ''} ({e['n']:,})" for e in rd["engines"]) or "—"
    kv("Test method", f"Wired Raspberry Pi 5 → router → Virgin Hub (modem mode). Engines: {eng}. Details: Methodology sheet.")
    kv("Generated", rd["generated_local"])
    kv("Software / export version", f"{rd['software']} · {jobs.EXPORT_VERSION}")
    kv("Raw data", f"{n_raw:,} rows on {len(raw_names)} sheet{'s' if len(raw_names) > 1 else ''}: {', '.join(raw_names)}")
    if rd["legacy_in_range"]:
        kv("Legacy speedtest-cli rows", rd["legacy_in_range"], f.int,
           "In the raw data (source = legacy_csv/legacy_xlsx); " + ("included in" if s["include_legacy"] else "excluded from")
           + " the statistics")
    kv("Integrity check at export", "PASSED" if verification["ok"] else "FAILED", f.ok if verification["ok"] else f.bad,
       f"{verification['records']:,} records re-hashed; ledger head {rd['ledger']['head'][:16]}… (see Integrity sheet)")
    sh.gap()

    section("The answers")
    kv("Highest speed measured", dl["max"], f.mbps)
    kv("Typical speed (median of scheduled tests)", dl["median"], f.mbps,
       f"{_pct_txt(sm['pct_of_advertised_median'])} of the advertised {s['advertised_down']:g} Mbps")
    kv("Consistently observed (90% of tests reached at least)", dl["p"]["10"], f.mbps, "The 10th percentile (P10)")
    kv("Lowest successful speed", dl["min"], f.mbps)
    kv("Tests close to what is paid for (≥ target)", P(sm["pct_target"]), f.pct, f"Target = {s['target_down']:g} Mbps")
    wh = ", ".join(f"{h['hour']:02d}:00 ({h['median']:.0f})" for h in ans["worst_hours"]) or "not enough data"
    kv("Worst hours of the day (median Mbps)", wh)
    wd = ", ".join(f"{d['day']} ({d['median']:.0f})" for d in ans["worst_days"]) or "not enough data"
    kv("Worst days (median Mbps)", wd)
    if ans["congestion"]:
        for i, c in enumerate(ans["congestion"]):
            kv("Recurring slowdown detected" if i == 0 else "", c["text"], f.wrap)
    else:
        kv("Evening / peak-time congestion", ans.get("congestion_message") or "", f.wrap)
    g = rd.get("guarantee") or {}
    if g.get("applies"):
        kv(f"Below the minimum guaranteed {g['mgds']:g} Mbps", f"{g['days_below']:,} of {g['days_with_data']:,} days had at least one "
           f"result below; longest run {g['longest']['days'] if g['longest'] else 0} consecutive day(s); runs of 3+ days: "
           + ("; ".join(f"{q['first']} to {q['last']} ({q['days']} days)" for q in g["qualifying"]) or "none"), f.wrap)
    qs = sm.get("quality") or {}
    if qs.get("n_ok"):
        why = "; ".join(f"{qs['labels'][c]} {n:,}" for c, n in sorted(qs["flags"].items(), key=lambda kv_: -kv_[1]))
        kv("Measurement quality", f"{qs['n_clean']:,} of {qs['n_ok']:,} successful scheduled tests were clean (nothing on the "
           f"monitor's side could have held them down)" + (f". Labels: {why}" if why else ""), f.wrap)
        cq = qs["clean"]
        kv("Clean tests only", f"on target {_pct(cq['pct_target'])}, degraded {_pct(cq['pct_degraded'])}, severe "
           f"{_pct(cq['pct_severe'])}" + (f", below the guarantee {_pct(cq['pct_below_guarantee'])}" if cq.get("pct_below_guarantee") is not None else "")
           + f"; median {cq['download']['median']:,.1f} Mbps" if cq["download"]["median"] is not None else "no clean tests", f.wrap)
    cap = rd.get("capacity") or {}
    if cap.get("level") not in (None, "ok", "unknown"):
        kv("Monitor capacity", cap["text"], f.wrap)
    ip = ans["isolated_poor"]
    kv("Isolated or recurring?", f"{ip['poor']:,} poor or failed tests: {ip['isolated']:,} isolated, {ip['in_runs']:,} in runs of "
       f"two or more; {ans['incidents']:,} confirmed degradation incidents", f.wrap)
    sh.gap()

    section("Throughput (scheduled tests, successful only)")
    sh.row([None, ("Statistic", f.th_left), ("Download", f.th), ("Upload", f.th), ("Download % of advertised", f.th)], height=28)
    adv = s["advertised_down"]
    for lab, key in (("Mean", "mean"), ("Median", "median"), ("Minimum", "min"), ("Maximum", "max")):
        sh.row([None, (lab, f.label), (dl[key], f.mbps), (ul[key], f.mbps),
                ((dl[key] / adv) if dl[key] is not None else None, f.pct_plain)])
    for p in ("1", "5", "10", "25", "75", "90", "95", "99"):
        sh.row([None, (f"P{p}", f.label), (dl["p"][p], f.mbps), (ul["p"][p], f.mbps),
                ((dl["p"][p] / adv) if dl["p"][p] is not None else None, f.pct_plain)])
    sh.row([None, ("Standard deviation", f.label), (dl["sd"], f.mbps), (ul["sd"], f.mbps)])
    sh.row([None, ("Successful tests", f.label), (dl["n"], f.int), (ul["n"], f.int)])
    sh.row([None, (f"On target (download ≥ {s['target_down']:g} / upload ≥ {s['target_up']:g})", f.label),
            (P(sm["pct_target"]), f.pct), (P(sm["up_pct_target"]), f.pct)])
    sh.row([None, ("Below target", f.label), (P(sm["pct_below_target"]), f.pct)])
    sh.row([None, (f"Degraded (download < {s['degraded_down']:g} / upload < {s['degraded_up']:g})", f.label),
            (P(sm["pct_degraded"]), f.pct), (P(sm["up_pct_degraded"]), f.pct)])
    sh.row([None, (f"Severely degraded (< {s['severe_down']:g})", f.label), (P(sm["pct_severe"]), f.pct)])
    for t in sm["tiers"]:
        sh.row([None, (f"Tests ≥ {t['mbps']:g} Mbps", f.label), (P(t["pct"]), f.pct)])
    if "pct_below_guarantee" in sm:
        sh.row([None, (f"Below minimum guarantee ({s['minimum_guaranteed_down']:g})", f.label), (P(sm["pct_below_guarantee"]), f.pct)])
    sh.row([None, ("Failed tests (ISP side, % of tests)", f.label), (P(sm["pct_failed"]), f.pct)])
    sh.row([None, ("Confirmed outages (% of tests)", f.label), (P(sm["pct_outage"]), f.pct)])
    sh.gap()

    section("Latency")
    sh.row([None, ("Measure", f.th_left), ("Mean", f.th), ("Median", f.th), ("Min", f.th), ("Max", f.th), ("P95", f.th)], height=22)
    for lab, d in (("Idle ping (speed tests)", sm["ping"]), ("Jitter (speed tests)", sm["jitter"])):
        sh.row([None, (lab, f.label), (d["mean"], f.ms), (d["median"], f.ms), (d["min"], f.ms), (d["max"], f.ms), (d["p"]["95"], f.ms)])
    lat = rd["latency"]
    sh.row([None, ("External ping (probes)", f.label), (None, f.ms), (lat["ext_median"], f.ms), (lat["ext_min"], f.ms),
            (lat["ext_max"], f.ms), (lat["ext_p95"], f.ms)])
    sh.row([None, ("Router ping (probes)", f.label), (None, f.ms), (lat["gw_median"], f.ms), None, None, (lat["gw_p95"], f.ms)])
    loss = sm["loss"]
    sh.row([None, ("Packet loss in speed tests", f.label), (P(loss["mean"]), f.pct), (P(loss["median"]), f.pct), (P(loss["min"]), f.pct),
            (P(loss["max"]), f.pct)])
    kv("Speed tests with any packet loss", P(sm["tests_with_loss_pct"]), f.pct)
    kv("Probes (1-minute) in period", lat["n"], f.int, f"{lat['probes_outage']:,} saw the Internet down while the router answered")
    sh.gap()

    section("When is it good or bad?")
    sh.row([None, ("Measure", f.th_left), ("When", f.th), ("Download", f.th), ("Tests", f.th)], height=22)
    days = ta["days"]

    def day_row(lab, d):
        if d:
            sh.row([None, (lab, f.label), (datetime.fromisoformat(d["day"]), f.date_plain), (d["median"], f.mbps), (d["n_ok"], f.int)])
        else:
            sh.row([None, (lab, f.label), (f"needs ≥ {days['min_samples']} tests in a day", f.wrap_muted)])

    day_row("Best day (median)", days["best"][0] if days["best"] else None)
    day_row("Worst day (median)", days["worst"][0] if days["worst"] else None)
    for lab, g, fmt_when in (("Best day of week (median)", ta["weekdays"]["best"], lambda x: x["name"]),
                             ("Worst day of week (median)", ta["weekdays"]["worst"], lambda x: x["name"]),
                             ("Best hour of day (median)", ta["hours"]["best"], lambda x: f"{x['hour']:02d}:00–{x['hour']:02d}:59"),
                             ("Worst hour of day (median)", ta["hours"]["worst"], lambda x: f"{x['hour']:02d}:00–{x['hour']:02d}:59")):
        sh.row([None, (lab, f.label), (fmt_when(g) if g else "not enough data", f.text), (g["median"] if g else None, f.mbps),
                (g["n_ok"] if g else None, f.int)])
    for lab, key in (("Best 24-hour period (rolling mean)", ("rolling_24h", "best")), ("Worst 24-hour period (rolling mean)", ("rolling_24h", "worst")),
                     ("Best 7-day period (rolling mean)", ("rolling_7d", "best")), ("Worst 7-day period (rolling mean)", ("rolling_7d", "worst")),
                     ("Highest 60-minute rolling average", ("rolling_60m", "best"))):
        w = ta[key[0]][key[1]]
        if w:
            when = f"{timeutil.local(w['start_ms'], tz):%d %b %H:%M} → {timeutil.local(w['end_ms'], tz):%d %b %y %H:%M}"
            sh.row([None, (lab, f.label), (when, f.text), (w["mean"], f.mbps), (w["n"], f.int)])
        else:
            sh.row([None, (lab, f.label), (f"needs ≥ {ta[key[0]]['min_n']} tests in the window", f.wrap_muted)])
    sh.gap()

    section("Sequences and incidents (consecutive tests, sampled spans)")
    st = sm["streaks"]
    sh.row([None, ("Measure", f.th_left), ("Tests", f.th), ("First test", f.th), ("Last test", f.th), ("Sampled span", f.th)], height=22)

    def run_row(lab, r):
        if r:
            sh.row([None, (lab, f.label), (r["count"], f.int), (_local(r["first_ms"], tz), f.dt_plain),
                    (_local(r["last_ms"], tz), f.dt_plain), (_span(r["sampled_span_ms"]), f.text)])
        else:
            sh.row([None, (lab, f.label), ("none", f.text)])

    run_row("Longest consecutive sampled run meeting target", st["longest_good"])
    run_row("Longest consecutive run of degraded/failed tests", st["longest_poor"])
    run_row("Longest consecutive run of failed tests", st["longest_failed"])
    run_row("Longest failure-free sampled stretch", st["longest_failure_free"])
    degr, outs = rd["degradations"], rd["outages"]
    kv("Degradation incidents (≥ 2 consecutive poor tests)", len(degr), f.int)
    if degr:
        lg = max(degr, key=lambda x: x["sampled_span_ms"])
        kv("Longest degradation incident", f"{lg['count']} tests, {timeutil.fmt_local(lg['first_ms'], tz)} → "
           f"{timeutil.fmt_local(lg['last_ms'], tz)} (sampled span {_span(lg['sampled_span_ms'])})", f.wrap)
    kv("Confirmed Internet outages (≥ 2 probes)", len(outs), f.int, "Router reachable, Internet not")
    if outs:
        lo = max(outs, key=lambda x: x["min_duration_ms"])
        kv("Longest outage", f"{timeutil.fmt_local(lo['first_ms'], tz)}: at least {_span(lo['min_duration_ms'])}, at most "
           f"{_span(lo['max_duration_ms'])}", f.wrap)
    sh.gap()

    section("Top 10 and bottom 10 days (median download, scheduled tests)")
    sh.row([None, ("Best days", f.th_left), ("Median", f.th), ("Tests", f.th), ("Worst days", f.th_left), ("Median", f.th),
            ("Tests", f.th)], height=22)
    best, worst = days["best"], days["worst"]
    for i in range(max(len(best), len(worst), 1)):
        b_, w_ = (best[i] if i < len(best) else None), (worst[i] if i < len(worst) else None)
        sh.row([None, (datetime.fromisoformat(b_["day"]) if b_ else None, f.date_plain), (b_["median"] if b_ else None, f.mbps),
                (b_["n_ok"] if b_ else None, f.int_plain), (datetime.fromisoformat(w_["day"]) if w_ else None, f.date_plain),
                (w_["median"] if w_ else None, f.mbps), (w_["n_ok"] if w_ else None, f.int_plain)])
    if not best:
        sh.row([None, (f"Days need at least {days['min_samples']} successful scheduled tests to be ranked.", f.wrap_muted)])
    sh.gap()
    sh.row([None, ("Statistics use scheduled tests, which run at a random moment in each slot. Speed tests are samples, "
                   "so spans between tests are not claims of continuous speed. See Methodology.", f.wrap_muted)], height=30)

    _charts(wb, ws, f, s, n_days, ht)


def _pct_txt(v):
    return "—" if v is None else f"{v:.0f}%"


def _charts(wb, ws, f, s, n_days, ht):
    def style(ch, title, y_title=None):
        ch.set_title({"name": title, "name_font": {"size": 11, "bold": True, "color": INK}})
        ch.set_chartarea({"border": {"none": True}, "fill": {"color": "#FFFFFF"}})
        ch.set_plotarea({"fill": {"color": "#FBFCFD"}})
        ch.set_legend({"position": "bottom", "font": {"size": 8, "color": MUTED}})
        if y_title:
            ch.set_y_axis({"name": y_title, "name_font": {"size": 8, "color": MUTED}, "num_font": {"size": 8, "color": MUTED},
                           "major_gridlines": {"visible": True, "line": {"color": "#E6EBF0"}}, "line": {"none": True}})
        return ch

    size = {"x_scale": 1.55, "y_scale": 1.05}
    row = 5
    if n_days:
        last = n_days
        c = wb.add_chart({"type": "line"})
        c.add_series({"name": "Mean download", "categories": ["Daily", 1, 0, last, 0], "values": ["Daily", 1, 4, last, 4],
                      "line": {"color": TEAL, "width": 2}})
        c.add_series({"name": "P10 download", "categories": ["Daily", 1, 0, last, 0], "values": ["Daily", 1, 8, last, 8],
                      "line": {"color": "#7FC9D1", "width": 1.25, "dash_type": "dash"}})
        c.add_series({"name": "Mean upload", "categories": ["Daily", 1, 0, last, 0], "values": ["Daily", 1, 11, last, 11],
                      "line": {"color": "#6C5BD4", "width": 1.5}})
        c.add_series({"name": "Target", "categories": ["Daily", 1, 0, last, 0], "values": ["Daily", 1, 13, last, 13],
                      "line": {"color": GOOD, "width": 1, "dash_type": "round_dot"}})
        style(c, "Daily download & upload (Mbps)", "Mbps")
        c.set_x_axis({"date_axis": True, "num_format": "d mmm", "num_font": {"size": 8, "color": MUTED, "rotation": -45},
                      "line": {"color": LINE}})
        ws.insert_chart(row, 8, c, size)
        row += 18
    hc = wb.add_chart({"type": "column"})
    hc.add_series({"name": "Median download", "categories": ["Hourly & Weekday", 2, 0, 25, 0],
                   "values": ["Hourly & Weekday", 2, 3, 25, 3], "fill": {"color": TEAL}, "gap": 40})
    line = wb.add_chart({"type": "line"})
    line.add_series({"name": "P10", "categories": ["Hourly & Weekday", 2, 0, 25, 0], "values": ["Hourly & Weekday", 2, 4, 25, 4],
                     "line": {"color": BAD, "width": 1.5}, "marker": {"type": "circle", "size": 4,
                                                                       "fill": {"color": BAD}, "border": {"none": True}}})
    hc.combine(line)
    style(hc, "Hour-of-day profile (local time)", "Mbps")
    hc.set_x_axis({"num_font": {"size": 8, "color": MUTED}, "line": {"color": LINE}})
    ws.insert_chart(row, 8, hc, size)
    row += 18
    if ht["hist_n"]:
        dc = wb.add_chart({"type": "column"})
        dc.add_series({"name": "Share of tests", "categories": ["Hourly & Weekday", 2, 18, 1 + ht["hist_n"], 18],
                       "values": ["Hourly & Weekday", 2, 20, 1 + ht["hist_n"], 20], "fill": {"color": "#3A6EA5"}, "gap": 15})
        style(dc, "Download speed distribution (% of tests per 50 Mbps band)", "% of tests")
        dc.set_y_axis({"num_format": "0%", "num_font": {"size": 8, "color": MUTED}, "major_gridlines": {"visible": True, "line": {"color": "#E6EBF0"}}, "line": {"none": True}})
        dc.set_legend({"none": True})
        dc.set_x_axis({"num_font": {"size": 7, "color": MUTED, "rotation": -45}, "line": {"color": LINE}})
        ws.insert_chart(row, 8, dc, size)
        row += 18
    pc = wb.add_chart({"type": "doughnut"})
    pc.add_series({"name": "Target achievement", "categories": ["Hourly & Weekday", 2, 22, 1 + ht["target_n"], 22],
                   "values": ["Hourly & Weekday", 2, 23, 1 + ht["target_n"], 23],
                   "points": [{"fill": {"color": GOOD}}, {"fill": {"color": "#8CC6A8"}}, {"fill": {"color": WARN}},
                              {"fill": {"color": BAD}}, {"fill": {"color": "#5B6B7D"}}],
                   "data_labels": {"percentage": True, "font": {"size": 8}}})
    pc.set_hole_size(55)
    style(pc, "Target achievement (scheduled tests)")
    ws.insert_chart(row, 8, pc, {"x_scale": 1.0, "y_scale": 1.05})
    if n_days:
        lc = wb.add_chart({"type": "line"})
        lc.add_series({"name": "Median ping (ms)", "categories": ["Daily", 1, 0, n_days, 0], "values": ["Daily", 1, 12, n_days, 12],
                       "line": {"color": "#6C5BD4", "width": 1.75}})
        style(lc, "Daily median latency (ms)", "ms")
        lc.set_legend({"none": True})
        lc.set_x_axis({"date_axis": True, "num_format": "d mmm", "num_font": {"size": 8, "color": MUTED, "rotation": -45},
                       "line": {"color": LINE}})
        ws.insert_chart(row, 14, lc, {"x_scale": 0.95, "y_scale": 1.05})
        row += 18
        tc = wb.add_chart({"type": "column", "subtype": "stacked"})
        tc.add_series({"name": "Degraded share of tests (%)", "categories": ["Daily", 1, 0, n_days, 0],
                       "values": ["Daily", 1, 10, n_days, 10], "fill": {"color": WARN}, "gap": 30})
        style(tc, "Degradation timeline (% of each day's tests below the degraded threshold)", "% of tests")
        tc.set_y_axis({"num_format": "0%", "num_font": {"size": 8, "color": MUTED}, "major_gridlines": {"visible": True, "line": {"color": "#E6EBF0"}}, "line": {"none": True}})
        tc.set_legend({"none": True})
        tc.set_x_axis({"date_axis": True, "num_format": "d mmm", "num_font": {"size": 8, "color": MUTED, "rotation": -45},
                       "line": {"color": LINE}})
        ws.insert_chart(row, 8, tc, size)


# ---------------------------------------------------------------- Incidents, Annotations, Methodology, Integrity

def _write_incidents(ws, f, rd, tz):
    sh = Sheet(ws, f)
    for c, w in {0: 20, 1: 20, 2: 8, 3: 8, 4: 12, 5: 12, 6: 14, 7: 20, 8: 20}.items():
        ws.set_column(c, c, w)
    sh.row([("Degradation incidents: two or more consecutive tests below the degraded threshold or failed", f.section)]
           + [("", f.section_fill)] * 8, height=22)
    sh.row([(h, f.th) for h in ("First poor test", "Last poor test", "Tests", "Failed", "Lowest (Mbps)", "Mean (Mbps)",
                                "Sampled span", "Last good test before", "First good test after")], height=30)
    for d in rd["degradations"]:
        sh.row([(_local(d["first_ms"], tz), f.dt_plain), (_local(d["last_ms"], tz), f.dt_plain), (d["count"], f.int_plain),
                (d["failed"], f.int_plain), (d["min_dl"], f.mbps_plain), (d["mean_dl"], f.mbps_plain),
                (_span(d["sampled_span_ms"]), f.text), (_local(d["before_ms"], tz), f.dt_plain), (_local(d["after_ms"], tz), f.dt_plain)])
    if not rd["degradations"]:
        sh.row([("None in this period.", f.label)])
    sh.gap(2)
    sh.row([("Confirmed Internet outages from the 1-minute probes (router answered, Internet did not)", f.section)]
           + [("", f.section_fill)] * 8, height=22)
    sh.row([(h, f.th) for h in ("First failing probe", "Last failing probe", "Probes", "Kind", "At least", "At most",
                                "Last good probe before", "First good probe after")], height=30)
    for o in rd["outages"]:
        sh.row([(_local(o["first_ms"], tz), f.dt_plain), (_local(o["last_ms"], tz), f.dt_plain), (o["probes"], f.int_plain),
                (", ".join(o["states"]), f.text), (_span(o["min_duration_ms"]), f.text), (_span(o["max_duration_ms"]), f.text),
                (_local(o["before_ms"], tz), f.dt_plain), (_local(o["after_ms"], tz), f.dt_plain)])
    if not rd["outages"]:
        sh.row([("None in this period.", f.label)])
    ws.freeze_panes(0, 0)


def _write_annotations(ws, f, conn, rd, tz, a, b):
    sh = Sheet(ws, f)
    for c, w in {0: 18, 1: 18, 2: 12, 3: 34, 4: 70}.items():
        ws.set_column(c, c, w)
    sh.row([("Owner's timeline notes (they never change any measurement)", f.section)] + [("", f.section_fill)] * 4, height=22)
    sh.row([(h, f.th) for h in ("Start", "End", "Category", "Title", "Notes")], height=22)
    for n in rd["annotations"]:
        sh.row([(_local(n["start_ms"], tz), f.dt_plain), (_local(n["end_ms"], tz), f.dt_plain), (n["category"], f.text),
                (n["title"], f.bold), (n["body"], f.wrap)])
    if not rd["annotations"]:
        sh.row([("No annotations in this period.", f.label)])
    sh.gap(2)
    sh.row([("Owner's corrections to individual tests (stored separately; the raw record is unchanged)", f.section)]
           + [("", f.section_fill)] * 4, height=22)
    sh.row([(h, f.th) for h in ("Test time", "Flag added", "Flag", "Status", "Reason")], height=22)
    rows = conn.execute("SELECT f.*, m.t_ms FROM measurement_flags f JOIN measurements m ON m.uuid = f.measurement_uuid "
                        "WHERE m.t_ms >= ? AND m.t_ms < ? ORDER BY m.t_ms", (a, b)).fetchall()
    for r in rows:
        status = "active" if not r["revoked_at"] else f"revoked {r['revoked_at'][:10]}: {r['revoked_reason']}"
        sh.row([(_local(r["t_ms"], tz), f.dt_plain), (r["created_at"][:16].replace("T", " "), f.text), (r["flag"], f.text),
                (status, f.text), (r["reason"], f.wrap)])
    if not rows:
        sh.row([("No corrections in this period.", f.label)])


def _write_methodology(ws, f, rd):
    sh = Sheet(ws, f)
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 110)
    sh.row([("Methodology", f.section), ("", f.section_fill)], height=24)
    for title, body in rd["methodology"]:
        sh.row([(title, f.bold), (body, f.wrap)], height=max(30, 15 * (len(body) // 120 + 1)))
    sh.gap()
    sh.row([("Settings in force at export", f.section), ("", f.section_fill)], height=22)
    for k, v in rd["settings"].items():
        sh.row([(k, f.label), (str(v), f.text)])
    sh.gap()
    sh.row([("Servers used in the period", f.section), ("", f.section_fill)], height=22)
    for srv in rd["servers"]:
        sh.row([(f"{srv['server_id'] or '—'}", f.label),
                (f"{srv['server_sponsor'] or ''} {('· ' + srv['server_location']) if srv['server_location'] else ''}: "
                 f"{srv['n']:,} tests, {srv['n_ok']:,} successful, mean {srv['mean'] or 0:.1f} Mbps", f.text)])


def _write_integrity(ws, f, conn, rd, verification, n_raw, raw_names, params):
    sh = Sheet(ws, f)
    ws.set_column(0, 0, 34)
    ws.set_column(1, 1, 100)
    sh.row([("Integrity and provenance", f.section), ("", f.section_fill)], height=24)
    sh.row([("Check performed at export", f.label), ("PASSED" if verification["ok"] else "FAILED", f.ok if verification["ok"] else f.bad)])
    sh.row([("Records re-hashed", f.label), (verification["records"], f.int)])
    sh.row([("By stream", f.label), (", ".join(f"{k}: {v:,}" for k, v in verification["by_stream"].items()), f.text)])
    sh.row([("Ledger entries", f.label), (verification["ledger_entries"], f.int)])
    sh.row([("Ledger head (chain hash)", f.label), (rd["ledger"]["head"], f.mono)])
    sh.row([("Hash scheme", f.label), (f"v{canonical.HASH_VERSION}: SHA-256 over canonical JSON (sorted keys, no spaces, UTF-8); "
                                      "chain_hash = SHA-256(previous chain hash : stream : uuid : record hash)", f.wrap)], height=30)
    sh.row([("Raw rows in this workbook", f.label), (f"{n_raw:,} on {', '.join(raw_names)}", f.text)])
    sh.row([("Public IP addresses", f.label), ("included" if params.get("include_public_ip") else "not included (private)", f.text)])
    for p in verification["problems"][:50]:
        sh.row([("Problem", f.bad), (p, f.wrap)])
    sh.gap()
    sh.row([("How to check this file independently", f.section), ("", f.section_fill)], height=22)
    for line in ("Each Raw Data row carries its Record hash and its Chain hash. Recompute the record hash by building the "
                 "canonical JSON of the record's fields (the list is in the ZIP export's manifest.json) and hashing it with "
                 "SHA-256; the ZIP export includes verify_export.py, which does this for every row.",
                 "The SHA-256 of this workbook was recorded in the system's export log when it was created, together with the "
                 "ledger head above, so this copy can later be matched to the system it came from.",
                 "Write the ledger head down (or keep this file): any later change to any earlier record would change it."):
        sh.row([None, (line, f.wrap)], height=32)
    sh.gap()
    sh.row([("Sequence and quarantine", f.section), ("", f.section_fill)], height=22)
    for g in integrity.sequence_gaps(conn):
        sh.row([(f"Agent {g['agent_id']}", f.label),
                (f"sequence {g['first']:,}–{g['last']:,}: {g['received']:,} received, {g['missing']:,} never received, "
                 f"{g['quarantined']:,} quarantined", f.text)])
    nq = conn.execute("SELECT count(*) FROM quarantine").fetchone()[0]
    sh.row([("Quarantined submissions", f.label), (nq, f.int)])
    sh.gap()
    sh.row([("Imports of earlier data", f.section), ("", f.section_fill)], height=22)
    for imp in conn.execute("SELECT * FROM imports ORDER BY id"):
        sh.row([(imp["file_name"], f.label), (f"{imp['format']}, SHA-256 {imp['file_sha256']}, {imp['rows_read']:,} rows read, "
                                              f"{imp['inserted']:,} imported, {imp['duplicates']:,} duplicates, "
                                              f"{imp['repaired']:,} repaired, {imp['rejected']:,} rejected", f.wrap)], height=30)
