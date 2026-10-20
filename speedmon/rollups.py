"""Per-day probe roll-ups, so long periods cost about the same however many years of probes are stored.

Derived and rebuildable (ingest.rebuild_derived recreates them from the probes table). Every figure built from
them is exactly the figure the probes give directly; nothing is approximated:
  * rollup_probe_daily: per UTC day, the counts, sums of whole numbers, minima and maxima the Evidence latency
    block and the coverage figure need.
  * rollup_probe_loss: per UTC day, how many probes had each non-zero external loss value, so the average loss is
    the exact sum of the values over the count.
  * rollup_probe_rtt: per UTC day, how many round-trip times fell in each value band (~1% wide). A percentile is
    found by counting through the bands, then reading only the probes in the one band that holds it (from the
    probes_ext_rtt / probes_gw_rtt indexes) and taking the exact value, so it matches sorting every probe.
Whole days inside a period come from the roll-ups; the partial days at either end are read from the probes.
The four statements below are also in migrations/0004_probe_daily_rollups.sql (a test keeps them identical).
"""
from __future__ import annotations

import logging
from fractions import Fraction

from . import timeutil

log = logging.getLogger("speedmon.rollups")
DAY = timeutil.DAY_MS
METRICS = {"ext": (0, "ext_rtt_ms", "probes_ext_rtt"), "gw": (1, "gw_rtt_ms", "probes_gw_rtt")}


def bucket_sql(col: str) -> str:
    """The value band of a round-trip time: 64 equal bands per power of two (so each is 0.8-1.6% wide) from 1/64 ms
    to 16 s, finer below and wider above. Multiplying by a power of two is exact, so the band of a value never depends
    on rounding, and bands only ever go up with the value (a percentile search relies on that)."""
    parts = [f"WHEN {col} < {2.0 ** -6!r} THEN CAST({col} * {2.0 ** 13!r} AS INTEGER)"]
    for e in range(-6, 14):
        parts.append(f"WHEN {col} < {2.0 ** (e + 1)!r} THEN {64 * (e + 7)} + CAST({col} * {2.0 ** (6 - e)!r} AS INTEGER)")
    return f"(CASE {' '.join(parts)} ELSE {64 * 21} + CAST({col} * {2.0 ** -8!r} AS INTEGER) END)"


def bucket_range(b: int) -> tuple[float, float]:
    """Values [lo, hi) that include every value in band b (a little wider; the query also checks the band itself)."""
    if b < 128:
        return (b - 1) / 2.0 ** 13, (b + 2) / 2.0 ** 13
    if b >= 64 * 22:
        e, j = 14, b - 64 * 21
    else:
        e = b // 64 - 8
        j = b - 64 * (e + 7)
    w = 2.0 ** (e - 6)
    return (j - 1) * w, (j + 2) * w


_DAY_EXPR = f"t_ms / {DAY} * {DAY}"


def _keep(col: str, fn: str) -> str:
    return (f"{col} = CASE WHEN excluded.{col} IS NULL THEN {col} WHEN {col} IS NULL THEN excluded.{col} "
            f"ELSE {fn}({col}, excluded.{col}) END")


def statements(cond: str) -> list[str]:
    """INSERT ... SELECT statements adding the probes matching `cond` to the roll-ups."""
    out = [
        "INSERT INTO rollup_probe_daily (day_ms, n, watch_s, loss_n, loss_pos, loss_max, n_outage, n_local, n_dns_fail, "
        "ext_n, ext_min, ext_max, gw_n) "
        f"SELECT {_DAY_EXPR}, count(*), sum(min(interval_s, 600)), count(ext_loss_pct), coalesce(sum(ext_loss_pct > 0), 0), "
        "max(ext_loss_pct), sum(state IN ('wan_down','dns_fail')), sum(state IN ('lan_fault','link_down')), "
        "coalesce(sum(dns_ok = 0), 0), count(ext_rtt_ms), min(ext_rtt_ms), max(ext_rtt_ms), count(gw_rtt_ms) "
        f"FROM probes WHERE {cond} GROUP BY 1 "
        "ON CONFLICT (day_ms) DO UPDATE SET n = n + excluded.n, watch_s = watch_s + excluded.watch_s, "
        "loss_n = loss_n + excluded.loss_n, loss_pos = loss_pos + excluded.loss_pos, "
        f"{_keep('loss_max', 'max')}, n_outage = n_outage + excluded.n_outage, n_local = n_local + excluded.n_local, "
        "n_dns_fail = n_dns_fail + excluded.n_dns_fail, ext_n = ext_n + excluded.ext_n, "
        f"{_keep('ext_min', 'min')}, {_keep('ext_max', 'max')}, gw_n = gw_n + excluded.gw_n",
        f"INSERT INTO rollup_probe_loss (day_ms, v, n) SELECT {_DAY_EXPR}, ext_loss_pct, count(*) FROM probes "
        f"WHERE {cond} AND ext_loss_pct IS NOT NULL AND ext_loss_pct <> 0 GROUP BY 1, 2 "
        "ON CONFLICT (day_ms, v) DO UPDATE SET n = n + excluded.n",
    ]
    for metric, col, _ in METRICS.values():
        out.append(f"INSERT INTO rollup_probe_rtt (metric, day_ms, bucket, n) SELECT {metric}, {_DAY_EXPR}, {bucket_sql(col)}, "
                   f"count(*) FROM probes WHERE {cond} AND {col} IS NOT NULL GROUP BY 2, 3 "
                   "ON CONFLICT (metric, day_ms, bucket) DO UPDATE SET n = n + excluded.n")
    return out


_ADD = statements("id = ?")
TABLES = ("rollup_probe_daily", "rollup_probe_loss", "rollup_probe_rtt")


def add_probe(conn, probe_id: int) -> None:
    """Add one stored probe (by row id) to the roll-ups; runs in the same transaction as the insert."""
    for sql in _ADD:
        conn.execute(sql, (probe_id,))


def rebuild(conn) -> None:
    for t in TABLES:
        conn.execute(f"DELETE FROM {t}")
    for sql in statements("1"):
        conn.execute(sql)


# ---------------------------------------------------------------- reading

def _inner(a: int, b: int) -> tuple[int, int] | None:
    """The whole UTC days inside [a, b), or None when there aren't any."""
    lo, hi = -(-a // DAY) * DAY, b // DAY * DAY
    return (lo, hi) if hi > lo else None


def watched_seconds(conn, a: int, b: int) -> int:
    """sum(min(interval_s, 600)) over the probes in [a, b): how long the monitor was watching."""
    raw = "SELECT coalesce(sum(min(interval_s, 600)), 0) FROM probes WHERE t_ms >= ? AND t_ms < ?"
    inner = _inner(a, b)
    if not inner:
        return conn.execute(raw, (a, b)).fetchone()[0]
    lo, hi = inner
    mid = conn.execute("SELECT coalesce(sum(watch_s), 0) FROM rollup_probe_daily WHERE day_ms >= ? AND day_ms < ?",
                       (lo, hi)).fetchone()[0]
    return mid + conn.execute(raw, (a, lo)).fetchone()[0] + conn.execute(raw, (hi, b)).fetchone()[0]


class _Stale(Exception):
    """The roll-ups disagree with the probes (they are rebuilt with `cli rebuild-derived`)."""


def latency(conn, a: int, b: int, pct) -> dict:
    """The Evidence latency block for [a, b) (see exports.jobs.probe_latency, which defines every figure).
    `pct(n, p, kth)` is the percentile rule given the sample size and the k-th smallest value."""
    inner = _inner(a, b)
    if inner:
        try:
            return _latency_rollup(conn, a, b, inner, pct)
        except _Stale as exc:
            log.warning("probe roll-ups out of step (%s); reading the probes directly", exc)
    return latency_raw(conn, a, b, pct)


def latency_raw(conn, a: int, b: int, pct) -> dict:
    # One pass over the period's probes for both round-trip columns (half the time of a query per column).
    rows = conn.execute("SELECT ext_rtt_ms, gw_rtt_ms FROM probes WHERE t_ms >= ? AND t_ms < ?", (a, b)).fetchall()
    ext = sorted(e for e, _ in rows if e is not None)
    gw = sorted(g for _, g in rows if g is not None)
    del rows
    row = conn.execute("SELECT count(*), count(ext_loss_pct), max(ext_loss_pct), sum(ext_loss_pct > 0), "
                       "sum(state IN ('wan_down','dns_fail')), sum(state IN ('lan_fault','link_down')), "
                       "sum(dns_ok = 0) FROM probes WHERE t_ms >= ? AND t_ms < ?", (a, b)).fetchone()
    loss_avg = float(_loss_sum(conn, a, b)) / row[1] if row[1] else None
    return _block(row[0], len(ext), lambda i: ext[i], ext[0] if ext else None, ext[-1] if ext else None,
                  len(gw), lambda i: gw[i], loss_avg, row[2], row[3], row[4] or 0, row[5] or 0, row[6] or 0, pct)


def _loss_sum(conn, a: int, b: int) -> Fraction:
    """Exact sum of the external loss values in [a, b). SQLite's avg() before 3.43 (the Pi has 3.40) adds floats
    naively, so the average is taken from this exact sum instead, the same way on every path."""
    return sum((Fraction(v) * k for v, k in conn.execute(
        "SELECT ext_loss_pct, count(*) FROM probes WHERE t_ms >= ? AND t_ms < ? AND ext_loss_pct IS NOT NULL AND "
        "ext_loss_pct <> 0 GROUP BY 1", (a, b))), Fraction(0))


def _block(n, ext_n, ext_kth, ext_min, ext_max, gw_n, gw_kth, loss_avg, loss_max, with_loss, outage, local, dns, pct) -> dict:
    return {"n": n, "ext_median": pct(ext_n, 50, ext_kth), "ext_p95": pct(ext_n, 95, ext_kth) if ext_n >= 20 else None,
            "ext_min": ext_min, "ext_max": ext_max,
            "gw_median": pct(gw_n, 50, gw_kth), "gw_p95": pct(gw_n, 95, gw_kth) if gw_n >= 20 else None,
            "loss_avg": loss_avg, "loss_max": loss_max, "probes_with_loss": with_loss, "probes_outage": outage,
            "probes_local": local, "dns_failures": dns}


_EDGE_STATS = ("SELECT count(*), coalesce(sum(min(interval_s, 600)), 0), count(ext_loss_pct), coalesce(sum(ext_loss_pct > 0), 0), "
               "max(ext_loss_pct), coalesce(sum(state IN ('wan_down','dns_fail')), 0), "
               "coalesce(sum(state IN ('lan_fault','link_down')), 0), coalesce(sum(dns_ok = 0), 0), count(ext_rtt_ms), "
               "min(ext_rtt_ms), max(ext_rtt_ms), count(gw_rtt_ms) FROM probes WHERE t_ms >= ? AND t_ms < ?")
_DAY_STATS = ("SELECT coalesce(sum(n), 0), coalesce(sum(watch_s), 0), coalesce(sum(loss_n), 0), coalesce(sum(loss_pos), 0), "
              "max(loss_max), coalesce(sum(n_outage), 0), coalesce(sum(n_local), 0), coalesce(sum(n_dns_fail), 0), "
              "coalesce(sum(ext_n), 0), min(ext_min), max(ext_max), coalesce(sum(gw_n), 0) FROM rollup_probe_daily "
              "WHERE day_ms >= ? AND day_ms < ?")


def _opt(fn, vals):
    vals = [v for v in vals if v is not None]
    return fn(vals) if vals else None


def _latency_rollup(conn, a, b, inner, pct) -> dict:
    lo, hi = inner
    parts = [conn.execute(_DAY_STATS, (lo, hi)).fetchone(), conn.execute(_EDGE_STATS, (a, lo)).fetchone(),
             conn.execute(_EDGE_STATS, (hi, b)).fetchone()]
    tot = [sum(p[i] for p in parts) for i in (0, 1, 2, 3, 5, 6, 7, 8, 11)]
    n, _, loss_n, loss_pos, outage, local, dns, ext_n, gw_n = tot
    loss_max = _opt(max, [p[4] for p in parts])
    ext_min, ext_max = _opt(min, [p[9] for p in parts]), _opt(max, [p[10] for p in parts])
    total = _loss_sum(conn, a, lo) + _loss_sum(conn, hi, b) + sum(
        (Fraction(v) * k for v, k in conn.execute("SELECT v, sum(n) FROM rollup_probe_loss WHERE day_ms >= ? AND day_ms < ? "
                                                    "GROUP BY v", (lo, hi))), Fraction(0))
    loss_avg = float(total) / loss_n if loss_n else None
    ext_kth = _Selector(conn, "ext", a, b, inner, ext_n)
    gw_kth = _Selector(conn, "gw", a, b, inner, gw_n)
    return _block(n, ext_n, ext_kth, ext_min, ext_max, gw_n, gw_kth, loss_avg, loss_max,
                  loss_pos if loss_n else None, outage, local, dns, pct)


class _Selector:
    """k-th smallest round-trip time in [a, b) (0-based), from the value-band counts plus one band of probes."""

    def __init__(self, conn, metric: str, a: int, b: int, inner: tuple[int, int], n: int):
        self.conn, self.a, self.b = conn, a, b
        self.mid, self.col, self.index = METRICS[metric]
        lo, hi = inner
        counts: dict = {}
        for bk, k in conn.execute("SELECT bucket, sum(n) FROM rollup_probe_rtt WHERE metric = ? AND day_ms >= ? AND day_ms < ? "
                                  "GROUP BY bucket", (self.mid, lo, hi)):
            counts[bk] = counts.get(bk, 0) + k
        for ea, eb in ((a, lo), (hi, b)):
            for bk, k in conn.execute(f"SELECT {bucket_sql(self.col)}, count(*) FROM probes WHERE t_ms >= ? AND t_ms < ? AND "
                                      f"{self.col} IS NOT NULL GROUP BY 1", (ea, eb)):
                counts[bk] = counts.get(bk, 0) + k
        self.buckets = sorted(counts.items())
        if sum(counts.values()) != n:
            raise _Stale(f"{metric} band counts {sum(counts.values())} != {n}")
        self.bands: dict = {}

    def __call__(self, i: int) -> float:
        below = 0
        for bk, k in self.buckets:
            if i < below + k:
                return self._band(bk, k)[i - below]
            below += k
        raise IndexError(i)

    def _band(self, bk: int, k: int) -> list:
        if bk not in self.bands:
            v0, v1 = bucket_range(bk)
            vals = sorted(r[0] for r in self.conn.execute(
                f"SELECT {self.col} FROM probes INDEXED BY {self.index} WHERE {self.col} >= ? AND {self.col} < ? "
                f"AND t_ms >= ? AND t_ms < ? AND {bucket_sql(self.col)} = ?", (v0, v1, self.a, self.b, bk)))
            if len(vals) != k:
                raise _Stale(f"band {bk} of {self.col}: {len(vals)} probes, roll-up says {k}")
            self.bands[bk] = vals
        return self.bands[bk]
