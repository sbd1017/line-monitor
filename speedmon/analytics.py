"""Every statistic the dashboard, evidence view and exports show. One implementation, used by all three.

Statistical conventions (also stated in the exported Methodology sheet):
  * Percentiles use linear interpolation between closest ranks (Excel PERCENTILE.INC). A percentile is only
    reported when the sample is big enough for it to mean something: P1/P99 need >= 100 values,
    P5/P95 >= 20, P10/P90 >= 10.
  * Speed statistics use successful tests only. Failures are counted and reported separately, never as zero.
  * Speed tests are samples. Streaks and incidents count consecutive *tests*, and their durations are labelled
    as the sampled span (first to last test). Where a true duration is bounded, the bounds come from the
    neighbouring tests or probes. Nothing claims an unobserved continuous throughput.
  * Records the owner has flagged "exclude" are left out of statistics and listed separately. Legacy
    speedtest-cli imports are left out unless the include_legacy setting is on.
  * "Local" failures (the Pi's own link down, or the router not answering) are not counted against the ISP.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from . import quality, settings_store, timeutil

WAN_FAILURES = {"wan_down", "dns_fail"}                       # the Internet itself was unreachable
LOCAL_FAILURES = {"lan_fault", "link_down", "interrupted", "agent_error"}   # the Pi/LAN/monitor side, not the ISP
PCT_MIN_N = {1: 100, 99: 100, 5: 20, 95: 20, 10: 10, 90: 10, 25: 4, 75: 4, 50: 1}
PCTS = (1, 5, 10, 25, 50, 75, 90, 95, 99)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(slots=True)
class T:
    t: int
    uuid: str
    ok: bool
    dl: float | None
    ul: float | None
    ping: float | None
    jitter: float | None
    loss: float | None
    kind: str
    source: str
    failure: str | None
    server: str | None
    excluded: bool = False
    related: str | None = None
    pk: dict | None = None                                       # package in force on the test's date
    day: str = ""
    hour: int = 0
    dow: int = 0
    minute: int = 0
    q: tuple = ()                                                # quality codes (speedmon/quality.py)

    @property
    def clean(self) -> bool:
        return quality.is_clean(self.q)

    @property
    def wan_fail(self) -> bool:
        return not self.ok and self.failure in WAN_FAILURES

    @property
    def local_fail(self) -> bool:
        return not self.ok and self.failure in LOCAL_FAILURES

    @property
    def isp_fail(self) -> bool:                                  # any failure that isn't the Pi/LAN's fault
        return not self.ok and self.failure not in LOCAL_FAILURES


# ---------------------------------------------------------------- loading

def load_tests(conn, from_ms: int, to_ms: int, settings: dict, include_excluded: bool = False,
               include_legacy: bool | None = None) -> list[T]:
    legacy = settings.get("include_legacy") if include_legacy is None else include_legacy
    sources = ("agent", "synthetic", "legacy_csv", "legacy_xlsx") if legacy else ("agent", "synthetic")
    excluded = {r[0] for r in conn.execute(
        "SELECT measurement_uuid FROM measurement_flags WHERE flag = 'exclude' AND revoked_at IS NULL")}
    clock = timeutil.LocalClock(settings["timezone"])
    pkc = settings_store.PackageClock(settings.get("packages"), settings["timezone"])
    out = []
    q = (f"SELECT m.t_ms, m.uuid, m.status, m.download_mbps, m.upload_mbps, m.ping_ms, m.jitter_ms, m.packet_loss_pct, "
         f"m.kind, m.source, m.failure_code, m.server_id, m.related_uuid, {', '.join(quality.SQL_COLUMNS)} FROM measurements m "
         f"LEFT JOIN diagnostics d ON d.measurement_uuid = m.uuid WHERE m.t_ms >= ? AND m.t_ms < ? AND m.source IN "
         f"({','.join('?' * len(sources))}) ORDER BY m.t_ms, m.id")
    seen = set()
    for r in conn.execute(q, (from_ms, to_ms, *sources)):
        if r[1] in seen:                                         # a second diagnostics row for the same test
            continue
        seen.add(r[1])
        x = T(r[0], r[1], r[2] == "ok", r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[1] in excluded, r[12])
        if x.excluded and not include_excluded:
            continue
        x.day, x.hour, x.dow, x.minute = clock.parts(x.t)
        x.pk = pkc.at(x.t)
        x.q = quality.assess(x.ok, x.source, x.kind, x.dl, x.pk, r[13:])
        out.append(x)
    return out


def excluded_count(conn, from_ms: int, to_ms: int, settings: dict) -> int:
    sources = ("agent", "synthetic", "legacy_csv", "legacy_xlsx") if settings.get("include_legacy") else ("agent", "synthetic")
    return conn.execute(f"SELECT count(DISTINCT f.measurement_uuid) FROM measurement_flags f JOIN measurements m ON m.uuid = "
                        f"f.measurement_uuid WHERE f.flag = 'exclude' AND f.revoked_at IS NULL AND m.t_ms >= ? AND m.t_ms < ? "
                        f"AND m.source IN ({','.join('?' * len(sources))})", (from_ms, to_ms, *sources)).fetchone()[0]


def data_bounds(conn, settings: dict) -> dict:
    legacy = settings.get("include_legacy")
    r = conn.execute("SELECT min(t_ms), max(t_ms), count(*) FROM measurements WHERE source IN ('agent','synthetic')").fetchone()
    lg = conn.execute("SELECT min(t_ms), max(t_ms), count(*) FROM measurements WHERE source LIKE 'legacy%'").fetchone()
    p = conn.execute("SELECT min(t_ms), max(t_ms) FROM probes").fetchone()
    first = r[0]
    if legacy and lg[0] is not None:
        first = lg[0] if first is None else min(first, lg[0])
    return {"first_ms": first, "last_ms": r[1], "tests": r[2], "legacy_first_ms": lg[0], "legacy_last_ms": lg[1],
            "legacy_tests": lg[2], "probe_first_ms": p[0], "probe_last_ms": p[1]}


# ---------------------------------------------------------------- basic statistics

def percentile(sorted_vals: list, p: float) -> float | None:
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * p / 100
    lo = math.floor(k)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def describe(values: list) -> dict:
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    out = {"n": n, "mean": None, "median": None, "min": None, "max": None, "sd": None}
    if n:
        out.update(mean=sum(vals) / n, median=percentile(vals, 50), min=vals[0], max=vals[-1],
                   sd=statistics.stdev(vals) if n > 1 else 0.0)
    out["p"] = {str(p): (percentile(vals, p) if n >= PCT_MIN_N[p] else None) for p in PCTS}
    return out


def share(n: int, d: int) -> float | None:
    return (100.0 * n / d) if d else None


def thresholds(settings: dict) -> dict:
    return {"target": settings["target_down"], "degraded": settings["degraded_down"], "severe": settings["severe_down"],
            "target_up": settings["target_up"], "degraded_up": settings["degraded_up"]}


def band(x: T, s: dict) -> str:
    if not x.ok:
        return "local" if x.local_fail else ("outage" if x.wan_fail else "failed")
    p = x.pk or s
    if x.dl >= p["target_down"]:
        return "target"
    if x.dl < p["severe_down"]:
        return "severe"
    if x.dl < p["degraded_down"]:
        return "degraded"
    return "below"                                                   # between degraded and target


BASIS_KINDS = ("scheduled", "legacy")


def basis(tests: list[T]) -> list[T]:
    """The unbiased sample for statistics: scheduled tests only (their timing is random within each slot).
    Confirmation, burst and manual tests deliberately over-sample bad moments, so they are left out of
    percentages and averages (they still count for streaks and incidents). When a scheduled test failed because
    of the engine and the fallback engine then succeeded, the fallback result stands in for that slot."""
    by_parent = {x.related: x for x in tests if x.kind == "fallback" and x.related}
    out = []
    for x in tests:
        if x.kind in BASIS_KINDS:
            out.append(by_parent.get(x.uuid, x) if not x.ok else x)
    return out


def P(x) -> dict:
    """The package (thresholds, advertised speeds) a test is judged against."""
    return x.pk


def packages_in(tests: list[T], s: dict) -> list[dict]:
    """Packages in force during these tests, with headline figures for each (scheduled tests only)."""
    groups: dict = {}
    for x in basis(tests):
        groups.setdefault(x.pk["from"], (x.pk, []))[1].append(x)
    out = []
    for frm, (pk, xs) in sorted(groups.items()):
        dl = sorted(x.dl for x in xs if x.ok)
        out.append({"from": frm, "name": pk["name"], "advertised_down": pk["advertised_down"], "advertised_up": pk["advertised_up"],
                    "target_down": pk["target_down"], "degraded_down": pk["degraded_down"], "severe_down": pk["severe_down"],
                    "n": len(xs), "n_ok": len(dl), "median": percentile(dl, 50),
                    "pct_target": share(sum(1 for v in dl if v >= pk["target_down"]), len(dl)),
                    "pct_degraded": share(sum(1 for v in dl if v < pk["degraded_down"]), len(dl)),
                    "pct_of_advertised_median": (100 * percentile(dl, 50) / pk["advertised_down"]) if dl else None,
                    "first_ms": xs[0].t, "last_ms": xs[-1].t})
    return out


def summary(tests: list[T], settings: dict) -> dict:
    """Statistics over the basis sample (see basis()); streaks over every test in time order."""
    s = settings
    all_tests = tests
    tests = basis(all_tests)
    ok = [x for x in tests if x.ok]
    counted = [x for x in tests if not x.local_fail]                 # denominator for ISP-facing shares
    dls = [x.dl for x in ok]
    n_ok = len(ok)
    fails = [x for x in tests if not x.ok]
    fail_codes: dict = defaultdict(int)
    for x in fails:
        fail_codes[x.failure or "unknown"] += 1
    up_ok = [x.ul for x in ok if x.ul is not None]
    ul_ok = [x for x in ok if x.ul is not None]
    range_pk = (all_tests[-1].pk if all_tests and all_tests[-1].pk else s.get("package") or s)
    tier_ok = [x for x in ok if P(x) is range_pk or P(x).get("from") == range_pk.get("from")]
    out = {
        "n": len(tests), "n_ok": n_ok, "n_failed": len(fails),
        "n_failed_isp": sum(1 for x in fails if x.isp_fail), "n_outage": sum(1 for x in fails if x.wan_fail),
        "n_local": sum(1 for x in fails if x.local_fail), "failure_codes": dict(fail_codes),
        "first_ms": all_tests[0].t if all_tests else None, "last_ms": all_tests[-1].t if all_tests else None,
        "download": describe(dls), "upload": describe(up_ok),
        "ping": describe([x.ping for x in ok]), "jitter": describe([x.jitter for x in ok]),
        "loss": describe([x.loss for x in ok]),
        "tests_with_loss_pct": share(sum(1 for x in ok if (x.loss or 0) > 0), sum(1 for x in ok if x.loss is not None)),
        "pct_target": share(sum(1 for x in ok if x.dl >= P(x)["target_down"]), n_ok),
        "pct_below_target": share(sum(1 for x in ok if x.dl < P(x)["target_down"]), n_ok),
        "pct_degraded": share(sum(1 for x in ok if x.dl < P(x)["degraded_down"]), n_ok),
        "pct_severe": share(sum(1 for x in ok if x.dl < P(x)["severe_down"]), n_ok),
        "pct_failed": share(sum(1 for x in counted if x.isp_fail), len(counted)),
        "pct_outage": share(sum(1 for x in counted if x.wan_fail), len(counted)),
        "up_pct_target": share(sum(1 for x in ul_ok if x.ul >= P(x)["target_up"]), len(ul_ok)),
        "up_pct_degraded": share(sum(1 for x in ul_ok if x.ul < P(x)["degraded_up"]), len(ul_ok)),
        "tiers": [{"mbps": t, "pct": share(sum(1 for x in tier_ok if x.dl >= t), len(tier_ok))} for t in range_pk["tiers"]],
        "tiers_package": range_pk["name"],
        # each test as a share of the package it was on, so periods spanning an upgrade stay meaningful
        "pct_of_advertised_median": percentile(sorted(100 * x.dl / P(x)["advertised_down"] for x in ok), 50) if ok else None,
        "pct_of_advertised_mean": (sum(100 * x.dl / P(x)["advertised_down"] for x in ok) / n_ok) if ok else None,
        "up_pct_of_advertised_median": percentile(sorted(100 * x.ul / P(x)["advertised_up"] for x in ul_ok), 50) if ul_ok else None,
        "excluded": 0, "n_all": len(all_tests),
        "kinds": {k: sum(1 for x in all_tests if x.kind == k) for k in sorted({x.kind for x in all_tests})},
    }
    guaranteed = [x for x in ok if P(x).get("minimum_guaranteed_down")]
    if guaranteed:
        out["pct_below_guarantee"] = share(sum(1 for x in guaranteed if x.dl < P(x)["minimum_guaranteed_down"]), len(guaranteed))
    out["quality"] = quality_summary(tests)
    out["packages"] = packages_in(all_tests, s)
    out["package"] = range_pk
    out["streaks"] = streaks(all_tests, s)
    return out


# ---------------------------------------------------------------- streaks and incidents

def quality_summary(tests: list[T]) -> dict:
    """The basis sample split by measurement quality: how many scheduled tests were clean, why the others weren't,
    and the headline figures recomputed over the clean ones only (so a slow result the monitor might have caused
    can be set aside, openly)."""
    ok = [x for x in tests if x.ok]
    clean = [x for x in ok if x.clean]
    flags: dict = defaultdict(int)
    for x in ok:
        for c in x.q:
            flags[c] += 1
    dls = [x.dl for x in clean]
    g = [x for x in clean if P(x).get("minimum_guaranteed_down")]
    return {"n_ok": len(ok), "n_clean": len(clean), "n_flagged": len(ok) - len(clean), "flags": dict(flags),
            "labels": {c: quality.FLAGS[c][0] for c in flags},
            "clean": {"download": describe(dls),
                      "pct_target": share(sum(1 for x in clean if x.dl >= P(x)["target_down"]), len(clean)),
                      "pct_degraded": share(sum(1 for x in clean if x.dl < P(x)["degraded_down"]), len(clean)),
                      "pct_severe": share(sum(1 for x in clean if x.dl < P(x)["severe_down"]), len(clean)),
                      "pct_below_guarantee": share(sum(1 for x in g if x.dl < P(x)["minimum_guaranteed_down"]), len(g)) if g else None}}


def _runs(tests: list[T], pred) -> list[tuple[int, int]]:
    """Index ranges [i, j] of maximal consecutive runs where pred holds."""
    runs, start = [], None
    for i, x in enumerate(tests):
        if pred(x):
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(tests) - 1))
    return runs


def _run_info(tests, run, label):
    i, j = run
    return {"kind": label, "count": j - i + 1, "first_ms": tests[i].t, "last_ms": tests[j].t,
            "sampled_span_ms": tests[j].t - tests[i].t,
            "before_ms": tests[i - 1].t if i > 0 else None, "after_ms": tests[j + 1].t if j + 1 < len(tests) else None}


def streaks(tests: list[T], s: dict) -> dict:
    seq = [x for x in tests if not x.local_fail]
    good = lambda x: x.ok and x.dl >= x.pk["target_down"]
    poor = lambda x: (not x.ok) or x.dl < x.pk["degraded_down"]
    failed = lambda x: not x.ok
    out = {}
    for name, pred in (("good", good), ("poor", poor), ("failed", failed)):
        runs = _runs(seq, pred)
        best = max(runs, key=lambda r: (r[1] - r[0], seq[r[1]].t), default=None)
        out[f"longest_{name}"] = _run_info(seq, best, name) if best else None
    current = None
    if seq:
        last = seq[-1]
        name = "good" if good(last) else ("failed" if failed(last) else ("poor" if poor(last) else "between"))
        pred = {"good": good, "failed": failed, "poor": poor,
                "between": lambda x: x.ok and x.pk["degraded_down"] <= x.dl < x.pk["target_down"]}[name]
        i = len(seq) - 1
        while i > 0 and pred(seq[i - 1]):
            i -= 1
        current = _run_info(seq, (i, len(seq) - 1), name)
    out["current"] = current
    # longest failure-free stretch (by sampled time between failures)
    fail_idx = [i for i, x in enumerate(seq) if not x.ok]
    best_ff, prev = None, -1
    for i in fail_idx + [len(seq)]:
        if i - prev - 1 > 0:
            a, b = prev + 1, i - 1
            span = seq[b].t - seq[a].t
            if best_ff is None or span > best_ff["sampled_span_ms"]:
                best_ff = _run_info(seq, (a, b), "failure_free")
        prev = i
    out["longest_failure_free"] = best_ff
    return out


def degradation_incidents(tests: list[T], s: dict, min_tests: int = 2) -> list[dict]:
    """Runs of >= min_tests consecutive tests that were degraded or failed (ISP-side). Confirmed, not one-offs."""
    seq = [x for x in tests if not x.local_fail]
    pred = lambda x: (not x.ok) or x.dl < x.pk["degraded_down"]
    out = []
    for run in _runs(seq, pred):
        if run[1] - run[0] + 1 < min_tests:
            continue
        info = _run_info(seq, run, "degradation")
        sub = seq[run[0]:run[1] + 1]
        okd = [x.dl for x in sub if x.ok]
        info.update(failed=sum(1 for x in sub if not x.ok), min_dl=min(okd) if okd else None,
                    mean_dl=(sum(okd) / len(okd)) if okd else None,
                    severe=sum(1 for x in sub if (not x.ok) or x.dl < x.pk["severe_down"]))
        out.append(info)
    return out


def outage_incidents(conn, from_ms: int, to_ms: int, min_probes: int = 2) -> list[dict]:
    """Confirmed Internet outages from the probes: >= min_probes consecutive probes where the router answered but
    the Internet didn't (wan_down / dns_fail). Duration bounds: at least first->last failing probe, at most
    last good probe before -> first good probe after. Pi/LAN faults neither extend nor end an outage.
    Only failing probes are read; the gaps between them are checked with indexed lookups."""
    good = "state IN ('ok','degraded')"
    rows = conn.execute("SELECT t_ms, state FROM probes WHERE t_ms >= ? AND t_ms < ? AND state IN ('wan_down','dns_fail') "
                        "ORDER BY t_ms", (from_ms, to_ms)).fetchall()
    runs, cur = [], []
    for t, st in rows:
        if cur and conn.execute(f"SELECT 1 FROM probes WHERE t_ms > ? AND t_ms < ? AND {good} LIMIT 1", (cur[-1][0], t)).fetchone():
            runs.append(cur)
            cur = []
        cur.append((t, st))
    if cur:
        runs.append(cur)
    out = []
    for run in runs:
        if len(run) < min_probes:
            continue
        a, b = run[0][0], run[-1][0]
        before = conn.execute(f"SELECT max(t_ms) FROM probes WHERE t_ms < ? AND {good}", (a,)).fetchone()[0]
        after = conn.execute(f"SELECT min(t_ms) FROM probes WHERE t_ms > ? AND {good}", (b,)).fetchone()[0]
        out.append({"kind": "outage", "first_ms": a, "last_ms": b, "probes": len(run), "states": sorted({r[1] for r in run}),
                    "min_duration_ms": b - a, "max_duration_ms": (after - before) if (after and before) else None,
                    "before_ms": before, "after_ms": after})
    return out


def guarantee_watch(tests: list[T]) -> dict:
    """The contract's minimum guaranteed download speed (Virgin: if the speed falls below it on 3 or more consecutive
    days, continuously or intermittently, and isn't fixed within 30 days of reporting, the customer may leave
    without an early-disconnection fee). Per local day: successful tests below the guarantee. Runs of consecutive
    calendar days with at least one such test; a day without data ends a run (nothing can be claimed for it).
    Failed tests are left out: the guarantee doesn't apply to outages."""
    from datetime import date as _d, timedelta as _td
    days: dict = {}
    mgds = None
    for x in tests:
        g = (x.pk or {}).get("minimum_guaranteed_down")
        if not g or not x.ok:
            continue
        mgds = g
        d = days.setdefault(x.day, {"day": x.day, "n": 0, "below": 0, "lowest": None, "mgds": g})
        d["n"] += 1
        if x.dl < g:
            d["below"] += 1
            d["lowest"] = x.dl if d["lowest"] is None else min(d["lowest"], x.dl)
    if mgds is None:
        return {"applies": False}
    ordered = sorted(days.values(), key=lambda d: d["day"])
    runs, cur = [], []
    prev = None
    for d in ordered:
        day = _d.fromisoformat(d["day"])
        if d["below"] and cur and prev == day - _td(days=1):
            cur.append(d)
        elif d["below"]:
            if cur:
                runs.append(cur)
            cur = [d]
        else:
            if cur:
                runs.append(cur)
            cur = []
        prev = day
    if cur:
        runs.append(cur)
    info = lambda r: {"days": len(r), "first": r[0]["day"], "last": r[-1]["day"], "tests_below": sum(x["below"] for x in r),
                      "lowest": min(x["lowest"] for x in r)}
    last_day = ordered[-1]["day"] if ordered else None
    current = info(runs[-1]) if runs and runs[-1][-1]["day"] == last_day else None
    return {"applies": True, "mgds": mgds, "days_with_data": len(ordered),
            "days_below": sum(1 for d in ordered if d["below"]), "tests_below": sum(d["below"] for d in ordered),
            "current": current, "longest": info(max(runs, key=len)) if runs else None,
            "qualifying": [info(r) for r in runs if len(r) >= 3], "recent": ordered[-30:]}


# ---------------------------------------------------------------- time analysis

def group_stats(tests: list[T], key, s: dict) -> dict:
    groups: dict = defaultdict(list)
    for x in tests:
        groups[key(x)].append(x)
    out = {}
    for k, xs in groups.items():
        oks = [x for x in xs if x.ok]
        dl = sorted(x.dl for x in oks)
        ul = [x.ul for x in xs if x.ok and x.ul is not None]
        pings = sorted(x.ping for x in xs if x.ok and x.ping is not None)
        n_ok = len(dl)
        pk = xs[-1].pk
        counted = [x for x in xs if not x.local_fail]
        out[k] = {"n": len(xs), "n_ok": n_ok, "failed": sum(1 for x in xs if x.isp_fail),
                  "mean": (sum(dl) / n_ok) if n_ok else None, "median": percentile(dl, 50),
                  "min": dl[0] if dl else None, "max": dl[-1] if dl else None,
                  "p10": percentile(dl, 10) if n_ok >= 10 else None, "p90": percentile(dl, 90) if n_ok >= 10 else None,
                  "ul_mean": (sum(ul) / len(ul)) if ul else None, "ul_median": percentile(sorted(ul), 50),
                  "ping_median": percentile(pings, 50),
                  "pct_target": share(sum(1 for x in oks if x.dl >= x.pk["target_down"]), n_ok),
                  "pct_degraded": share(sum(1 for x in oks if x.dl < x.pk["degraded_down"]), n_ok),
                  "target_down": pk["target_down"], "degraded_down": pk["degraded_down"], "severe_down": pk["severe_down"],
                  "advertised_down": pk["advertised_down"],
                  "pct_failed": share(sum(1 for x in counted if x.isp_fail), len(counted))}
    return out


def daily(tests: list[T], s: dict) -> list[dict]:
    g = group_stats(tests, lambda x: x.day, s)
    return [{"day": k, **v} for k, v in sorted(g.items())]


def hourly(tests: list[T], s: dict) -> list[dict]:
    g = group_stats(tests, lambda x: x.hour, s)
    return [{"hour": h, **g.get(h, {"n": 0, "n_ok": 0})} for h in range(24)]


def weekday(tests: list[T], s: dict) -> list[dict]:
    g = group_stats(tests, lambda x: x.dow, s)
    return [{"dow": d, "name": WEEKDAYS[d], **g.get(d, {"n": 0, "n_ok": 0})} for d in range(7)]


def min_day_samples(s: dict) -> int:
    return max(4, int(0.25 * 1440 / max(s["interval_min"], 5)))


def rank_days(days: list[dict], s: dict, n: int = 10) -> dict:
    floor = min_day_samples(s)
    eligible = [d for d in days if d["n_ok"] >= floor and d["median"] is not None]
    by = sorted(eligible, key=lambda d: (d["median"], d["mean"]))
    return {"min_samples": floor, "eligible": len(eligible), "best": list(reversed(by[-n:])), "worst": by[:n]}


def rank_groups(rows: list[dict], min_n: int = 5) -> dict:
    eligible = [r for r in rows if r.get("n_ok", 0) >= min_n and r.get("median") is not None]
    if not eligible:
        return {"best": None, "worst": None}
    return {"best": max(eligible, key=lambda r: r["median"]), "worst": min(eligible, key=lambda r: r["median"])}


def rolling_extremes(tests: list[T], window_ms: int, min_n: int) -> dict:
    """Best and worst mean download over any window [t, t + window) that starts at a test and holds >= min_n
    successful tests. Labelled as rolling averages of sampled tests."""
    ok = [x for x in tests if x.ok]
    best = worst = None
    j, total = 0, 0.0
    for i, x in enumerate(ok):
        while j < len(ok) and ok[j].t < x.t + window_ms:
            total += ok[j].dl
            j += 1
        n = j - i
        if n >= min_n:
            mean = total / n
            item = {"start_ms": x.t, "end_ms": x.t + window_ms, "first_ms": x.t, "last_ms": ok[j - 1].t, "n": n, "mean": mean}
            if best is None or mean > best["mean"]:
                best = item
            if worst is None or mean < worst["mean"]:
                worst = item
        total -= x.dl
    return {"best": best, "worst": worst, "min_n": min_n}


def time_analysis(tests: list[T], s: dict) -> dict:
    tests = basis(tests)
    days = daily(tests, s)
    hours = hourly(tests, s)
    dows = weekday(tests, s)
    per_day = 1440 / max(s["interval_min"], 5)
    return {
        "days": rank_days(days, s),
        "hours": rank_groups(hours),
        "weekdays": rank_groups(dows),
        "rolling_60m": rolling_extremes(tests, timeutil.HOUR_MS, 2),
        "rolling_24h": rolling_extremes(tests, timeutil.DAY_MS, max(4, int(per_day * 0.5))),
        "rolling_7d": rolling_extremes(tests, 7 * timeutil.DAY_MS, max(10, int(per_day * 7 * 0.5))),
    }


# ---------------------------------------------------------------- statistics tests

def _ranks(values: list) -> list:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def mann_whitney(a: list, b: list, alternative: str = "two-sided") -> dict:
    """Mann-Whitney U with tie correction and the normal approximation (fine for n >= ~10 per group).
    alternative 'less' tests whether a tends to be smaller than b."""
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return {"u": None, "p": None, "z": None}
    allv = list(a) + list(b)
    r = _ranks(allv)
    r1 = sum(r[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    counts: dict = defaultdict(int)
    for v in allv:
        counts[v] += 1
    n = n1 + n2
    tie = sum(c ** 3 - c for c in counts.values())
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - tie / (n * (n - 1)))) if n > 1 else 0
    if sigma == 0:
        return {"u": u1, "p": 1.0, "z": 0.0}
    z = (u1 - mu) / sigma
    if alternative == "less":
        p = 0.5 * math.erfc(-z / math.sqrt(2))
    elif alternative == "greater":
        p = 0.5 * math.erfc(z / math.sqrt(2))
    else:
        p = math.erfc(abs(z) / math.sqrt(2))
    return {"u": u1, "p": min(1.0, p), "z": z}


class RankPool:
    """Mann-Whitney tests of many subsets against the rest of one pool, ranking the pool once.
    Gives exactly the same U, z and p as mann_whitney(subset, rest) with O(n) work per subset."""

    def __init__(self, values: list):
        import bisect
        self._bisect = bisect
        self.values = values
        self.ranks = _ranks(values)
        self.sorted = sorted(values)
        counts: dict = defaultdict(int)
        for v in values:
            counts[v] += 1
        self.tie = sum(c ** 3 - c for c in counts.values())
        self.n = len(values)

    def test(self, idx: list, alternative: str = "less") -> dict:
        n1 = len(idx)
        n2 = self.n - n1
        if n1 < 3 or n2 < 3:
            return {"u": None, "p": None, "z": None}
        r1 = sum(self.ranks[i] for i in idx)
        u1 = r1 - n1 * (n1 + 1) / 2
        n = self.n
        sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - self.tie / (n * (n - 1))))
        if sigma == 0:
            return {"u": u1, "p": 1.0, "z": 0.0}
        z = (u1 - n1 * n2 / 2) / sigma
        p = 0.5 * math.erfc(-z / math.sqrt(2)) if alternative == "less" else math.erfc(abs(z) / math.sqrt(2))
        return {"u": u1, "p": min(1.0, p), "z": z}

    def median_excluding(self, inside_sorted: list) -> float | None:
        """Median of the pool without the given values (a sub-multiset of the pool), without building it."""
        S, I, bis = self.sorted, inside_sorted, self._bisect
        m = len(S) - len(I)
        if m <= 0:
            return None

        def kth(k):                                    # k-th smallest (0-based) of S minus I
            lo, hi = 0, len(S) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if bis.bisect_right(S, S[mid]) - bis.bisect_right(I, S[mid]) >= k + 1:
                    hi = mid
                else:
                    lo = mid + 1
            return S[lo]
        return kth(m // 2) if m % 2 else (kth(m // 2 - 1) + kth(m // 2)) / 2


def hodges_lehmann(a: list, b: list, cap: int = 1500) -> float | None:
    """Median of all pairwise differences a_i - b_j (deterministically thinned for large samples)."""
    if not a or not b:
        return None
    sa, sb = sorted(a), sorted(b)
    if len(sa) > cap:
        sa = [sa[int(i * len(sa) / cap)] for i in range(cap)]
    if len(sb) > cap:
        sb = [sb[int(i * len(sb) / cap)] for i in range(cap)]
    diffs = sorted(x - y for x in sa for y in sb)
    return percentile(diffs, 50)


def _bh(pvals: list[float], q: float) -> list[bool]:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    passed = [False] * m
    k_max = -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            k_max = rank
    for rank, i in enumerate(order, 1):
        if rank <= k_max:
            passed[i] = True
    return passed


# ---------------------------------------------------------------- congestion detector

BIN_MIN = 30
MIN_DAYS = 14
MIN_BIN_N = 10
MIN_DROP = 0.10
MIN_DROP_MBPS = 25.0


def _hhmm(minute: int) -> str:
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def congestion(tests: list[T], s: dict) -> dict:
    """Recurring time-of-day slowdowns, separately for weekdays and weekends.

    For each 30-minute local time-of-day bin, the downloads in that bin are compared with the rest of the day
    (one-sided Mann-Whitney), with Benjamini-Hochberg control of false discoveries (q = 0.01) across all bins.
    A bin only counts if its median is also >= 10% and >= 25 Mbps below the rest of the day. Adjacent bins merge
    into windows; each window must then hold on its own (p < 0.001) and show the drop on at least half of the
    days that sampled it. With less than 14 days of data nothing is claimed."""
    ok = [x for x in basis(tests) if x.ok]
    days_seen = {x.day for x in ok}
    result = {"bin_minutes": BIN_MIN, "days": len(days_seen), "tests": len(ok), "findings": [], "status": "ok",
              "method": congestion.__doc__.strip()}
    if len(days_seen) < MIN_DAYS or len(ok) < 200:
        result["status"] = "insufficient"
        result["message"] = (f"Not enough data yet: {len(days_seen)} days and {len(ok)} successful tests. The detector needs at "
                             f"least {MIN_DAYS} days and 200 tests before it will say anything.")
        return result
    for daytype, sel in (("weekday", lambda x: x.dow < 5), ("weekend", lambda x: x.dow >= 5)):
        xs = [x for x in ok if sel(x)]
        if len({x.day for x in xs}) < (6 if daytype == "weekend" else 10):
            continue
        pool = RankPool([x.dl for x in xs])
        bin_idx: dict = defaultdict(list)
        for i, x in enumerate(xs):
            bin_idx[x.minute // BIN_MIN].append(i)
        cand, pvals = [], []
        for b in range(1440 // BIN_MIN):
            idx = bin_idx.get(b, [])
            if len(idx) < MIN_BIN_N:
                continue
            inside = sorted(xs[i].dl for i in idx)
            mi, mo = statistics.median(inside), pool.median_excluding(inside)
            test = pool.test(idx, "less")
            cand.append((b, mi, mo))
            pvals.append(test["p"] if test["p"] is not None else 1.0)
        if not cand:
            continue
        passed = _bh(pvals, 0.01)
        flagged = {b for (b, mi, mo), ok_ in zip(cand, passed)
                   if ok_ and mi <= mo * (1 - MIN_DROP) and (mo - mi) >= MIN_DROP_MBPS}
        if not flagged:
            continue
        # merge into circular windows of consecutive bins
        nb = 1440 // BIN_MIN
        windows, seen = [], set()
        for b in sorted(flagged):
            if b in seen:
                continue
            start = b
            while (start - 1) % nb in flagged and (start - 1) % nb != b:
                start = (start - 1) % nb
            run, cur = [], start
            while cur in flagged and cur not in run:
                run.append(cur)
                cur = (cur + 1) % nb
            seen.update(run)
            windows.append(run)
        for run in windows:
            in_set = set(run)
            idx = [i for b in run for i in bin_idx.get(b, [])]
            inside = [xs[i] for i in idx]
            outside = [x for x in xs if x.minute // BIN_MIN not in in_set]
            test = pool.test(idx, "less")
            mi = statistics.median([x.dl for x in inside])
            mo = pool.median_excluding(sorted(x.dl for x in inside))
            per_day_in, per_day_out = defaultdict(list), defaultdict(list)
            for x in inside:
                per_day_in[x.day].append(x.dl)
            for x in outside:
                per_day_out[x.day].append(x.dl)
            both = [d for d in per_day_in if d in per_day_out]
            hit = sum(1 for d in both if statistics.median(per_day_in[d]) < statistics.median(per_day_out[d]) * (1 - MIN_DROP))
            if test["p"] is None or test["p"] >= 0.001 or not both or hit / len(both) < 0.5:
                continue
            a, b = run[0] * BIN_MIN, (run[-1] + 1) * BIN_MIN
            drop = 100 * (mo - mi) / mo if mo else None
            result["findings"].append({
                "daytype": daytype, "start": _hhmm(a), "end": _hhmm(b), "start_min": a, "end_min": b % 1440,
                "median_in": mi, "median_out": mo, "drop_pct": drop, "p": test["p"], "tests_in": len(inside),
                "days_hit": hit, "days_with_data": len(both),
                "text": (f"Recurring {daytype} slowdown between {_hhmm(a)} and {_hhmm(b)}: median {mi:.0f} Mbps against "
                         f"{mo:.0f} Mbps for the rest of the day ({drop:.0f}% lower), on {hit} of {len(both)} {'weekdays' if daytype == 'weekday' else 'weekend days'} "
                         f"that were sampled in that window. Mann-Whitney p {_fmt_p(test['p'])}, {len(inside)} tests "
                         f"in the window. Window edges have {BIN_MIN}-minute resolution."),
            })
    if not result["findings"]:
        result["message"] = ("No recurring time-of-day slowdown detected: no half-hour window was both statistically and "
                             "materially slower than the rest of the day.")
    return result


def _fmt_p(p: float) -> str:
    if p < 1e-6:
        return "< 0.000001"
    return f"= {p:.2g}" if p >= 0.001 else f"= {p:.1e}"


# ---------------------------------------------------------------- distributions, heatmaps, calendar, DNA

def bin_width(top: float) -> float:
    """Histogram band width giving ~25-45 bands up to the advertised speed (25 Mbps for gigabit, 5 for ~130)."""
    for w in (1, 2, 5, 10, 25, 50, 100):
        if top / w <= 50:
            return w
    return 100


def histogram(values: list, width: float, top: float) -> list[dict]:
    if not values:
        return []
    top = max(top, max(values))
    nbins = max(1, int(math.ceil(top / width)))
    counts = [0] * nbins
    for v in values:
        counts[min(nbins - 1, int(v // width))] += 1
    total = len(values)
    return [{"lo": i * width, "hi": (i + 1) * width, "n": c, "pct": 100 * c / total} for i, c in enumerate(counts)]


def heatmap(tests: list[T], s: dict) -> dict:
    tests = basis(tests)
    cells: dict = defaultdict(list)
    fails: dict = defaultdict(int)
    day_pk: dict = {}
    for x in tests:
        day_pk.setdefault(x.day, x.pk)
        if x.ok:
            cells[(x.day, x.hour)].append(x.dl)
        elif x.isp_fail:
            fails[(x.day, x.hour)] += 1
    days = sorted({k[0] for k in cells} | {k[0] for k in fails})
    rows = []
    for d in days:
        row = []
        for h in range(24):
            v = cells.get((d, h), [])
            row.append([round(sum(v) / len(v), 1) if v else None, len(v), fails.get((d, h), 0)])
        pk = day_pk.get(d) or s
        rows.append({"day": d, "cells": row, "target_down": pk["target_down"], "degraded_down": pk["degraded_down"],
                     "severe_down": pk["severe_down"]})
    return {"rows": rows}


def calendar(tests: list[T], s: dict, outage_minutes: dict | None = None) -> list[dict]:
    out = []
    for d in daily(basis(tests), s):
        cls = "none"
        if d["n_ok"]:
            m = d["median"]
            cls = ("target" if m >= d["target_down"] else "below" if m >= d["degraded_down"]
                   else "degraded" if m >= d["severe_down"] else "severe")
        elif d["failed"]:
            cls = "failed"
        out.append({**d, "class": cls, "outage_min": (outage_minutes or {}).get(d["day"], 0)})
    return out


def dna(tests: list[T], s: dict) -> dict:
    """Features for the Connection DNA glyph: one spoke per local hour."""
    all_tests, tests = tests, basis(tests)
    spokes = []
    for h in range(24):
        xs = [x for x in tests if x.hour == h]
        dl = sorted(x.dl for x in xs if x.ok)
        jit = sorted(x.jitter for x in xs if x.ok and x.jitter is not None)
        ping = sorted(x.ping for x in xs if x.ok and x.ping is not None)
        spokes.append({"hour": h, "n": len(xs), "median": percentile(dl, 50), "p10": percentile(dl, 10) if dl else None,
                       "p90": percentile(dl, 90) if dl else None, "ping": percentile(ping, 50),
                       "jitter": percentile(jit, 50), "failed": sum(1 for x in xs if x.isp_fail)})
    sm = summary(all_tests, s)
    dl = sm["download"]
    stability = None
    if dl["n"] >= 10 and dl["median"]:
        stability = max(0.0, 1 - (dl["p"]["90"] - dl["p"]["10"]) / dl["median"]) if dl["p"]["10"] is not None else None
    return {"spokes": spokes, "summary": {"n": sm["n"], "n_ok": sm["n_ok"], "median": dl["median"], "mean": dl["mean"],
                                          "p10": dl["p"]["10"], "pct_target": sm["pct_target"],
                                          "pct_failed": sm["pct_failed"], "ping": sm["ping"]["median"],
                                          "jitter": sm["jitter"]["median"], "stability": stability}}


def compare(a_all: list[T], b_all: list[T], s: dict) -> dict:
    a, b = basis(a_all), basis(b_all)
    da = [x.dl for x in a if x.ok]
    db = [x.dl for x in b if x.ok]
    ua = [x.ul for x in a if x.ok and x.ul is not None]
    ub = [x.ul for x in b if x.ok and x.ul is not None]
    test = mann_whitney(da, db)
    return {"a": summary(a_all, s), "b": summary(b_all, s), "download_test": test, "upload_test": mann_whitney(ua, ub),
            "shift_mbps": hodges_lehmann(db, da),
            "hourly_a": hourly(a, s), "hourly_b": hourly(b, s),
            "hist_a": histogram(da, bin_width(s["advertised_down"]), s["advertised_down"]),
            "hist_b": histogram(db, bin_width(s["advertised_down"]), s["advertised_down"]),
            "verdict": _verdict(test, da, db), "enough": len(da) >= 10 and len(db) >= 10}


def _verdict(test, da, db) -> str:
    if len(da) < 10 or len(db) < 10:
        s = lambda k: f"{k} successful test{'s' if k != 1 else ''}"
        return f"Not enough tests to compare yet: period A has {s(len(da))}, period B has {s(len(db))}."
    ma, mb = statistics.median(da), statistics.median(db)
    if test["p"] is None or test["p"] >= 0.05:
        return f"No real difference in download speed: typically {ma:.0f} Mbps in A and {mb:.0f} Mbps in B."
    direction = "faster" if mb > ma else "slower"
    return f"Period B is {direction}: typically {mb:.0f} Mbps, against {ma:.0f} Mbps in period A."


# ---------------------------------------------------------------- coverage

def coverage(conn, from_ms: int, to_ms: int, s: dict, tests: list[T]) -> dict:
    """Monitoring coverage: how much of the period the monitor was actually watching (from probes), and how many
    of the scheduled tests happened."""
    now = timeutil.now_ms()
    first_probe = conn.execute("SELECT min(t_ms) FROM probes").fetchone()[0]
    a = max(from_ms, first_probe) if first_probe else None
    b = min(to_ms, now)
    out = {"probe_minutes": 0, "expected_minutes": 0, "pct": None, "tests_expected": None, "tests_scheduled": None}
    if a is not None and b > a:
        rows = conn.execute("SELECT count(*), coalesce(sum(min(interval_s, 600)), 0) FROM probes WHERE t_ms >= ? AND t_ms < ?",
                            (a, b)).fetchone()
        out["probe_minutes"] = rows[1] / 60
        out["expected_minutes"] = (b - a) / 60000
        out["pct"] = min(100.0, 100 * out["probe_minutes"] / out["expected_minutes"]) if out["expected_minutes"] else None
        out["monitoring_from_ms"] = a
    agent_tests = [x for x in tests if x.source in ("agent", "synthetic")]
    if agent_tests:
        span = (min(to_ms, now) - max(from_ms, agent_tests[0].t)) / 60000
        out["tests_expected"] = max(1, int(span / max(s["interval_min"], 5)))
        out["tests_scheduled"] = sum(1 for x in agent_tests if x.kind == "scheduled")
        out["tests_pct"] = min(100.0, 100 * out["tests_scheduled"] / out["tests_expected"])
    return out
