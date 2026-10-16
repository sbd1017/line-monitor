-- Indexes for sorting/filtering the Data Explorer on large datasets.
CREATE INDEX IF NOT EXISTS measurements_dl ON measurements (download_mbps, id);
CREATE INDEX IF NOT EXISTS measurements_ul ON measurements (upload_mbps, id);
CREATE INDEX IF NOT EXISTS measurements_ping ON measurements (ping_ms, id);
CREATE INDEX IF NOT EXISTS probes_state_t ON probes (state, t_ms);
