-- Traceroute samples to the first router beyond the house (see speedagent/paths.py for how they read).
-- Evidence, appended and protected like every other raw stream: the boundary call must not be re-datable.
-- 'boundary': lan = loss at the gateway (the house), isp = loss starting past it and carrying to the end,
--             none = clean, unknown = too few cycles or a middle hop that clears again (probe deprioritisation).
-- 'hops' is compact JSON: [{hop, host, loss_pct, sent, avg_ms, best_ms, worst_ms}] up to 8 hops. IPv4 addresses
-- as reported; exports mask them unless public IPs are being shared.

CREATE TABLE paths (
    id             INTEGER PRIMARY KEY,
    uuid           TEXT NOT NULL UNIQUE,
    agent_id       TEXT NOT NULL,
    agent_seq      INTEGER,
    t_ms           INTEGER NOT NULL,
    at             TEXT NOT NULL,
    target         TEXT,
    target_source  TEXT,
    hop_count      INTEGER,
    hops           TEXT,
    boundary       TEXT,
    first_bad_hop  INTEGER,
    sustained      INTEGER,
    gw_loss_pct    REAL,
    gw_rtt_ms      REAL,
    cycles         INTEGER,
    duration_ms    INTEGER,
    engine         TEXT,
    engine_version TEXT,
    iface          TEXT,
    link_speed_mbps INTEGER,
    error          TEXT,
    record_hash    TEXT NOT NULL,
    received_at    TEXT NOT NULL
);

CREATE INDEX paths_by_time ON paths (t_ms);
CREATE INDEX paths_by_boundary ON paths (boundary, t_ms);

CREATE TRIGGER paths_append_only_u BEFORE UPDATE ON paths BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER paths_append_only_d BEFORE DELETE ON paths BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
