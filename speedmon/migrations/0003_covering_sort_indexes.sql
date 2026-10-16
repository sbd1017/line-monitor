-- Covering indexes so the Data Explorer can page through millions of records quickly (the id subquery is
-- answered from the index alone). Replaces the narrower 0002 sort indexes.
DROP INDEX IF EXISTS measurements_dl;
DROP INDEX IF EXISTS measurements_ul;
DROP INDEX IF EXISTS measurements_ping;
CREATE INDEX IF NOT EXISTS measurements_dl_cov ON measurements (download_mbps, id, source, t_ms);
CREATE INDEX IF NOT EXISTS measurements_ul_cov ON measurements (upload_mbps, id, source, t_ms);
CREATE INDEX IF NOT EXISTS measurements_ping_cov ON measurements (ping_ms, id, source, t_ms);
