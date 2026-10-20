-- Per-day probe roll-ups (DERIVED, rebuildable: ingest.rebuild_derived recreates them; see speedmon/rollups.py).
-- They make coverage and the Evidence latency block over long periods cost about the same at any history length,
-- with every figure still exact. The two indexes let a percentile read only the probes in one ~1% value band.
-- Indexes don't change any record, so the append-only evidence rules are untouched.

CREATE TABLE rollup_probe_daily (
    day_ms      INTEGER PRIMARY KEY,           -- UTC day start (t_ms / 86400000 * 86400000)
    n           INTEGER NOT NULL,              -- probes
    watch_s     INTEGER NOT NULL,              -- sum(min(interval_s, 600)): seconds watched (coverage)
    loss_n      INTEGER NOT NULL,              -- probes with an external loss figure
    loss_pos    INTEGER NOT NULL,              -- ... above 0
    loss_max    REAL,
    n_outage    INTEGER NOT NULL,              -- state wan_down or dns_fail
    n_local     INTEGER NOT NULL,              -- state lan_fault or link_down
    n_dns_fail  INTEGER NOT NULL,              -- dns_ok = 0
    ext_n       INTEGER NOT NULL,
    ext_min     REAL,
    ext_max     REAL,
    gw_n        INTEGER NOT NULL
);

-- How many probes had each non-zero external loss value, per day (an exact average needs the values, not a rounded sum).
CREATE TABLE rollup_probe_loss (
    day_ms  INTEGER NOT NULL,
    v       REAL NOT NULL,
    n       INTEGER NOT NULL,
    PRIMARY KEY (day_ms, v)
) WITHOUT ROWID;

-- How many round-trip times fell in each value band, per day. metric 0 = ext_rtt_ms, 1 = gw_rtt_ms.
CREATE TABLE rollup_probe_rtt (
    metric  INTEGER NOT NULL,
    day_ms  INTEGER NOT NULL,
    bucket  INTEGER NOT NULL,
    n       INTEGER NOT NULL,
    PRIMARY KEY (metric, day_ms, bucket)
) WITHOUT ROWID;

CREATE INDEX probes_ext_rtt ON probes (ext_rtt_ms, t_ms);
CREATE INDEX probes_gw_rtt ON probes (gw_rtt_ms, t_ms);

-- Fill them from the probes already stored (the same statements rollups.rebuild runs).
INSERT INTO rollup_probe_daily (day_ms, n, watch_s, loss_n, loss_pos, loss_max, n_outage, n_local, n_dns_fail, ext_n, ext_min, ext_max, gw_n) SELECT t_ms / 86400000 * 86400000, count(*), sum(min(interval_s, 600)), count(ext_loss_pct), coalesce(sum(ext_loss_pct > 0), 0), max(ext_loss_pct), sum(state IN ('wan_down','dns_fail')), sum(state IN ('lan_fault','link_down')), coalesce(sum(dns_ok = 0), 0), count(ext_rtt_ms), min(ext_rtt_ms), max(ext_rtt_ms), count(gw_rtt_ms) FROM probes WHERE 1 GROUP BY 1 ON CONFLICT (day_ms) DO UPDATE SET n = n + excluded.n, watch_s = watch_s + excluded.watch_s, loss_n = loss_n + excluded.loss_n, loss_pos = loss_pos + excluded.loss_pos, loss_max = CASE WHEN excluded.loss_max IS NULL THEN loss_max WHEN loss_max IS NULL THEN excluded.loss_max ELSE max(loss_max, excluded.loss_max) END, n_outage = n_outage + excluded.n_outage, n_local = n_local + excluded.n_local, n_dns_fail = n_dns_fail + excluded.n_dns_fail, ext_n = ext_n + excluded.ext_n, ext_min = CASE WHEN excluded.ext_min IS NULL THEN ext_min WHEN ext_min IS NULL THEN excluded.ext_min ELSE min(ext_min, excluded.ext_min) END, ext_max = CASE WHEN excluded.ext_max IS NULL THEN ext_max WHEN ext_max IS NULL THEN excluded.ext_max ELSE max(ext_max, excluded.ext_max) END, gw_n = gw_n + excluded.gw_n;

INSERT INTO rollup_probe_loss (day_ms, v, n) SELECT t_ms / 86400000 * 86400000, ext_loss_pct, count(*) FROM probes WHERE 1 AND ext_loss_pct IS NOT NULL AND ext_loss_pct <> 0 GROUP BY 1, 2 ON CONFLICT (day_ms, v) DO UPDATE SET n = n + excluded.n;

INSERT INTO rollup_probe_rtt (metric, day_ms, bucket, n) SELECT 0, t_ms / 86400000 * 86400000, (CASE WHEN ext_rtt_ms < 0.015625 THEN CAST(ext_rtt_ms * 8192.0 AS INTEGER) WHEN ext_rtt_ms < 0.03125 THEN 64 + CAST(ext_rtt_ms * 4096.0 AS INTEGER) WHEN ext_rtt_ms < 0.0625 THEN 128 + CAST(ext_rtt_ms * 2048.0 AS INTEGER) WHEN ext_rtt_ms < 0.125 THEN 192 + CAST(ext_rtt_ms * 1024.0 AS INTEGER) WHEN ext_rtt_ms < 0.25 THEN 256 + CAST(ext_rtt_ms * 512.0 AS INTEGER) WHEN ext_rtt_ms < 0.5 THEN 320 + CAST(ext_rtt_ms * 256.0 AS INTEGER) WHEN ext_rtt_ms < 1.0 THEN 384 + CAST(ext_rtt_ms * 128.0 AS INTEGER) WHEN ext_rtt_ms < 2.0 THEN 448 + CAST(ext_rtt_ms * 64.0 AS INTEGER) WHEN ext_rtt_ms < 4.0 THEN 512 + CAST(ext_rtt_ms * 32.0 AS INTEGER) WHEN ext_rtt_ms < 8.0 THEN 576 + CAST(ext_rtt_ms * 16.0 AS INTEGER) WHEN ext_rtt_ms < 16.0 THEN 640 + CAST(ext_rtt_ms * 8.0 AS INTEGER) WHEN ext_rtt_ms < 32.0 THEN 704 + CAST(ext_rtt_ms * 4.0 AS INTEGER) WHEN ext_rtt_ms < 64.0 THEN 768 + CAST(ext_rtt_ms * 2.0 AS INTEGER) WHEN ext_rtt_ms < 128.0 THEN 832 + CAST(ext_rtt_ms * 1.0 AS INTEGER) WHEN ext_rtt_ms < 256.0 THEN 896 + CAST(ext_rtt_ms * 0.5 AS INTEGER) WHEN ext_rtt_ms < 512.0 THEN 960 + CAST(ext_rtt_ms * 0.25 AS INTEGER) WHEN ext_rtt_ms < 1024.0 THEN 1024 + CAST(ext_rtt_ms * 0.125 AS INTEGER) WHEN ext_rtt_ms < 2048.0 THEN 1088 + CAST(ext_rtt_ms * 0.0625 AS INTEGER) WHEN ext_rtt_ms < 4096.0 THEN 1152 + CAST(ext_rtt_ms * 0.03125 AS INTEGER) WHEN ext_rtt_ms < 8192.0 THEN 1216 + CAST(ext_rtt_ms * 0.015625 AS INTEGER) WHEN ext_rtt_ms < 16384.0 THEN 1280 + CAST(ext_rtt_ms * 0.0078125 AS INTEGER) ELSE 1344 + CAST(ext_rtt_ms * 0.00390625 AS INTEGER) END), count(*) FROM probes WHERE 1 AND ext_rtt_ms IS NOT NULL GROUP BY 2, 3 ON CONFLICT (metric, day_ms, bucket) DO UPDATE SET n = n + excluded.n;

INSERT INTO rollup_probe_rtt (metric, day_ms, bucket, n) SELECT 1, t_ms / 86400000 * 86400000, (CASE WHEN gw_rtt_ms < 0.015625 THEN CAST(gw_rtt_ms * 8192.0 AS INTEGER) WHEN gw_rtt_ms < 0.03125 THEN 64 + CAST(gw_rtt_ms * 4096.0 AS INTEGER) WHEN gw_rtt_ms < 0.0625 THEN 128 + CAST(gw_rtt_ms * 2048.0 AS INTEGER) WHEN gw_rtt_ms < 0.125 THEN 192 + CAST(gw_rtt_ms * 1024.0 AS INTEGER) WHEN gw_rtt_ms < 0.25 THEN 256 + CAST(gw_rtt_ms * 512.0 AS INTEGER) WHEN gw_rtt_ms < 0.5 THEN 320 + CAST(gw_rtt_ms * 256.0 AS INTEGER) WHEN gw_rtt_ms < 1.0 THEN 384 + CAST(gw_rtt_ms * 128.0 AS INTEGER) WHEN gw_rtt_ms < 2.0 THEN 448 + CAST(gw_rtt_ms * 64.0 AS INTEGER) WHEN gw_rtt_ms < 4.0 THEN 512 + CAST(gw_rtt_ms * 32.0 AS INTEGER) WHEN gw_rtt_ms < 8.0 THEN 576 + CAST(gw_rtt_ms * 16.0 AS INTEGER) WHEN gw_rtt_ms < 16.0 THEN 640 + CAST(gw_rtt_ms * 8.0 AS INTEGER) WHEN gw_rtt_ms < 32.0 THEN 704 + CAST(gw_rtt_ms * 4.0 AS INTEGER) WHEN gw_rtt_ms < 64.0 THEN 768 + CAST(gw_rtt_ms * 2.0 AS INTEGER) WHEN gw_rtt_ms < 128.0 THEN 832 + CAST(gw_rtt_ms * 1.0 AS INTEGER) WHEN gw_rtt_ms < 256.0 THEN 896 + CAST(gw_rtt_ms * 0.5 AS INTEGER) WHEN gw_rtt_ms < 512.0 THEN 960 + CAST(gw_rtt_ms * 0.25 AS INTEGER) WHEN gw_rtt_ms < 1024.0 THEN 1024 + CAST(gw_rtt_ms * 0.125 AS INTEGER) WHEN gw_rtt_ms < 2048.0 THEN 1088 + CAST(gw_rtt_ms * 0.0625 AS INTEGER) WHEN gw_rtt_ms < 4096.0 THEN 1152 + CAST(gw_rtt_ms * 0.03125 AS INTEGER) WHEN gw_rtt_ms < 8192.0 THEN 1216 + CAST(gw_rtt_ms * 0.015625 AS INTEGER) WHEN gw_rtt_ms < 16384.0 THEN 1280 + CAST(gw_rtt_ms * 0.0078125 AS INTEGER) ELSE 1344 + CAST(gw_rtt_ms * 0.00390625 AS INTEGER) END), count(*) FROM probes WHERE 1 AND gw_rtt_ms IS NOT NULL GROUP BY 2, 3 ON CONFLICT (metric, day_ms, bucket) DO UPDATE SET n = n + excluded.n;
