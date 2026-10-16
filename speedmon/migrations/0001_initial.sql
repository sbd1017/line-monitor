-- speedmon schema v1.
-- EVIDENCE tables (measurements, diagnostics, probes, events, ledger, imports) are append-only:
-- triggers refuse every UPDATE and DELETE. Corrections live in measurement_flags / annotations.
-- DERIVED tables (derived_measurement, rollup_probe_hourly) can be rebuilt from evidence at any time.
-- Times: t_ms = UTC epoch milliseconds (indexed); *_at = ISO-8601 UTC text with a Z.

CREATE TABLE measurements (
    id                INTEGER PRIMARY KEY,
    uuid              TEXT NOT NULL UNIQUE,
    agent_id          TEXT NOT NULL,
    agent_seq         INTEGER,
    source            TEXT NOT NULL CHECK (source IN ('agent', 'legacy_csv', 'legacy_xlsx', 'synthetic')),
    kind              TEXT NOT NULL CHECK (kind IN ('scheduled', 'confirmation', 'burst', 'manual', 'fallback', 'retry', 'legacy')),
    trigger_reason    TEXT,
    related_uuid      TEXT,
    engine            TEXT NOT NULL,
    engine_version    TEXT,
    t_ms              INTEGER NOT NULL,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    duration_ms       INTEGER,
    tz_name           TEXT NOT NULL,
    utc_offset_min    INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    failure_code      TEXT,
    failure_detail    TEXT,
    download_mbps     REAL,
    upload_mbps       REAL,
    download_bytes    INTEGER,
    upload_bytes      INTEGER,
    download_ms       INTEGER,
    upload_ms         INTEGER,
    ping_ms           REAL,
    jitter_ms         REAL,
    ping_low_ms       REAL,
    ping_high_ms      REAL,
    dl_latency_ms     REAL,
    ul_latency_ms     REAL,
    packet_loss_pct   REAL,
    server_id         TEXT,
    server_name       TEXT,
    server_sponsor    TEXT,
    server_location   TEXT,
    server_country    TEXT,
    server_host       TEXT,
    distance_km       REAL,
    result_id         TEXT,
    result_url        TEXT,
    isp               TEXT,
    iface             TEXT,
    link_speed_mbps   INTEGER,
    link_duplex       TEXT,
    link_state        TEXT,
    raw_sha256        TEXT,
    raw               TEXT,
    record_hash       TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    CHECK (status = 'failed' OR download_mbps IS NOT NULL),
    CHECK (status = 'ok' OR failure_code IS NOT NULL)
);
CREATE INDEX measurements_t ON measurements (t_ms);
CREATE INDEX measurements_source_t ON measurements (source, t_ms);
CREATE INDEX measurements_server ON measurements (server_id);
CREATE UNIQUE INDEX measurements_agent_seq ON measurements (agent_id, agent_seq) WHERE agent_seq IS NOT NULL;

CREATE TABLE diagnostics (
    id                 INTEGER PRIMARY KEY,
    uuid               TEXT NOT NULL UNIQUE,
    measurement_uuid   TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    agent_seq          INTEGER,
    t_ms               INTEGER NOT NULL,
    cpu_load1          REAL,
    cpu_load5          REAL,
    cpu_temp_c         REAL,
    throttled          TEXT,
    mem_available_pct  REAL,
    swap_used_pct      REAL,
    iface              TEXT,
    link_speed_mbps    INTEGER,
    link_duplex        TEXT,
    link_state         TEXT,
    rx_errors_delta    INTEGER,
    tx_errors_delta    INTEGER,
    rx_dropped_delta   INTEGER,
    tx_dropped_delta   INTEGER,
    rx_bytes_delta     INTEGER,
    tx_bytes_delta     INTEGER,
    window_ms          INTEGER,
    background_rx_mbps REAL,
    background_tx_mbps REAL,
    pretest_rx_mbps    REAL,
    pretest_tx_mbps    REAL,
    gateway_rtt_ms     REAL,
    gateway_loss_pct   REAL,
    external_rtt_ms    REAL,
    external_loss_pct  REAL,
    dns_ok             INTEGER,
    public_ip          TEXT,            -- private: never sent to the browser, never in exports unless asked
    public_ip_changed  INTEGER,
    pi_uptime_s        REAL,
    boot_id            TEXT,
    agent_version      TEXT,
    extra              TEXT,
    record_hash        TEXT NOT NULL,
    received_at        TEXT NOT NULL
);
CREATE INDEX diagnostics_measurement ON diagnostics (measurement_uuid);
CREATE UNIQUE INDEX diagnostics_agent_seq ON diagnostics (agent_id, agent_seq) WHERE agent_seq IS NOT NULL;

CREATE TABLE probes (
    id               INTEGER PRIMARY KEY,
    uuid             TEXT NOT NULL UNIQUE,
    agent_id         TEXT NOT NULL,
    agent_seq        INTEGER,
    t_ms             INTEGER NOT NULL,
    at               TEXT NOT NULL,
    interval_s       INTEGER NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('ok', 'degraded', 'wan_down', 'dns_fail', 'lan_fault', 'link_down')),
    gw_rtt_ms        REAL,
    gw_loss_pct      REAL,
    ext_rtt_ms       REAL,
    ext_loss_pct     REAL,
    ext_detail       TEXT,
    dns_ms           REAL,
    dns_ok           INTEGER,
    http_ms          REAL,
    http_ok          INTEGER,
    link_up          INTEGER,
    link_speed_mbps  INTEGER,
    record_hash      TEXT NOT NULL,
    received_at      TEXT NOT NULL
);
CREATE INDEX probes_t ON probes (t_ms);
CREATE UNIQUE INDEX probes_agent_seq ON probes (agent_id, agent_seq) WHERE agent_seq IS NOT NULL;

CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    uuid         TEXT NOT NULL UNIQUE,
    agent_id     TEXT NOT NULL,
    agent_seq    INTEGER,
    t_ms         INTEGER NOT NULL,
    at           TEXT NOT NULL,
    type         TEXT NOT NULL,
    detail       TEXT,
    record_hash  TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE INDEX events_t ON events (t_ms);
CREATE INDEX events_type_t ON events (type, t_ms);
CREATE UNIQUE INDEX events_agent_seq ON events (agent_id, agent_seq) WHERE agent_seq IS NOT NULL;

-- One hash chain over every evidence record, in insertion order (see speedmon/canonical.py).
CREATE TABLE ledger (
    chain_index  INTEGER PRIMARY KEY,
    stream       TEXT NOT NULL CHECK (stream IN ('measurement', 'diagnostic', 'probe', 'event')),
    uuid         TEXT NOT NULL,
    record_hash  TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    chain_hash   TEXT NOT NULL,
    hash_version INTEGER NOT NULL,
    recorded_at  TEXT NOT NULL,
    UNIQUE (stream, uuid)
);

-- Each legacy import run (file fingerprint and counts), so the provenance of imported rows is explicit.
CREATE TABLE imports (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    file_sha256   TEXT NOT NULL,
    file_bytes    INTEGER NOT NULL,
    format        TEXT NOT NULL,
    rows_read     INTEGER NOT NULL,
    inserted      INTEGER NOT NULL,
    duplicates    INTEGER NOT NULL,
    repaired      INTEGER NOT NULL,
    rejected      INTEGER NOT NULL,
    notes         TEXT
);

CREATE TRIGGER measurements_append_only_u BEFORE UPDATE ON measurements BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER measurements_append_only_d BEFORE DELETE ON measurements BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER diagnostics_append_only_u BEFORE UPDATE ON diagnostics BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER diagnostics_append_only_d BEFORE DELETE ON diagnostics BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER probes_append_only_u BEFORE UPDATE ON probes BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER probes_append_only_d BEFORE DELETE ON probes BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER events_append_only_u BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER events_append_only_d BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER ledger_append_only_u BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER ledger_append_only_d BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER imports_append_only_u BEFORE UPDATE ON imports BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER imports_append_only_d BEFORE DELETE ON imports BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;

-- Submissions that failed validation or whose hash didn't match. Kept (never dropped) for inspection.
CREATE TABLE quarantine (
    id           INTEGER PRIMARY KEY,
    received_at  TEXT NOT NULL,
    agent_id     TEXT,
    stream       TEXT,
    uuid         TEXT,
    payload      TEXT NOT NULL,
    error        TEXT NOT NULL
);
CREATE TRIGGER quarantine_append_only_u BEFORE UPDATE ON quarantine BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER quarantine_append_only_d BEFORE DELETE ON quarantine BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;

-- ---------------------------------------------------------------- owner's layer (never alters evidence)

-- Per-measurement corrections: exclude from statistics, notes, disputes. Revoked, never deleted.
CREATE TABLE measurement_flags (
    id               INTEGER PRIMARY KEY,
    measurement_uuid TEXT NOT NULL REFERENCES measurements (uuid),
    flag             TEXT NOT NULL CHECK (flag IN ('exclude', 'note', 'disputed')),
    reason           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    revoked_at       TEXT,
    revoked_reason   TEXT
);
CREATE INDEX measurement_flags_uuid ON measurement_flags (measurement_uuid);

CREATE TABLE annotations (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL UNIQUE,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'note',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT,
    CHECK (end_ms IS NULL OR end_ms >= start_ms)
);
CREATE INDEX annotations_t ON annotations (start_ms);

CREATE TABLE annotation_history (
    id            INTEGER PRIMARY KEY,
    annotation_id INTEGER NOT NULL,
    at            TEXT NOT NULL,
    action        TEXT NOT NULL,
    snapshot      TEXT NOT NULL
);

-- ---------------------------------------------------------------- configuration and housekeeping

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE settings_history (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    source      TEXT NOT NULL
);

CREATE TABLE sessions (
    id_hash       TEXT PRIMARY KEY,
    csrf          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

CREATE TABLE audit_log (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    action  TEXT NOT NULL,
    detail  TEXT
);

-- Commands for the agent (run a test now). Picked up when the agent next polls.
CREATE TABLE commands (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('run_test')),
    payload     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'expired')),
    sent_at     TEXT
);

-- Latest status from each agent (cache, not evidence).
CREATE TABLE agent_status (
    agent_id      TEXT PRIMARY KEY,
    last_seen_ms  INTEGER NOT NULL,
    status        TEXT NOT NULL
);

CREATE TABLE exports (
    id            INTEGER PRIMARY KEY,
    uuid          TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    finished_at   TEXT,
    format        TEXT NOT NULL,
    range_label   TEXT NOT NULL,
    from_ms       INTEGER,
    to_ms         INTEGER,
    params        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'expired')),
    progress      REAL NOT NULL DEFAULT 0,
    rows          INTEGER,
    file_name     TEXT,
    file_bytes    INTEGER,
    file_sha256   TEXT,
    ledger_head   TEXT,
    error         TEXT
);

-- ---------------------------------------------------------------- derived (rebuildable)

CREATE TABLE derived_measurement (
    uuid         TEXT PRIMARY KEY,
    anomaly_z    REAL,
    ref_median   REAL,
    ref_n        INTEGER,
    anomaly      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE rollup_probe_hourly (
    hour_ms      INTEGER PRIMARY KEY,
    n            INTEGER NOT NULL,
    n_ok         INTEGER NOT NULL,
    n_degraded   INTEGER NOT NULL,
    n_wan_down   INTEGER NOT NULL,
    n_dns_fail   INTEGER NOT NULL,
    n_lan_fault  INTEGER NOT NULL,
    n_link_down  INTEGER NOT NULL,
    ext_n        INTEGER NOT NULL,
    ext_sum      REAL NOT NULL,
    ext_min      REAL,
    ext_max      REAL,
    gw_n         INTEGER NOT NULL,
    gw_sum       REAL NOT NULL,
    loss_sum     REAL NOT NULL,
    loss_n       INTEGER NOT NULL
);
