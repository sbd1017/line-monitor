# What was tested (16 Oct 2026)

## Automated suite: `.venv/bin/python -m pytest` (47 tests, passing on the Mac with Python 3.13 and the Pi with 3.11)

| Area | Tests |
|---|---|
| Integrity | the canonical modules are identical; hashes survive int/float/empty-text round trips; ingest is idempotent and chained; bad hash, changed raw output, synthetic records in production, another agent's id and UUID reuse with different content are all quarantined (never dropped); UPDATE/DELETE refused on raw tables and the ledger; `verify` detects a raw edit and a removed trigger; sequence gaps reported |
| Security | every unauthenticated path gives the identical 303 (or 401 for POST); phrase lockout is indistinguishable from a wrong phrase; CSRF, Origin and cross-site checks; security headers; agent API rejects unsigned, wrong-key, stale, Cloudflare-forwarded and replayed requests; a browser session can't reach the agent API; public IP never reaches the browser; settings validation and the danger confirmation; annotations and flags never change a record |
| Time | DST days of 23 and 25 hours; both 01:xx hours on the October fall-back day land in local hour 1; no 01:xx bucket on the March spring-forward day; named ranges (yesterday, last month, all) |
| Scheduling policy | hard clamps; exactly one test per 30-minute slot at varied minutes; confirmation then burst then back-off; an isolated blip gets one confirmation and no burst; daily cap; pause; a restart never repeats a slot; persistent degradation suspends extra tests and resumes after recovery |
| Analytics | percentiles match Excel PERCENTILE.INC; P1 needs ≥ 100 values; the basis sample excludes bursts and uses the fallback result for engine failures; failures never count as 0 Mbps and local faults aren't held against the ISP; streaks report sampled spans; the congestion detector finds a planted weekday 19:10–22:35 slowdown, stays quiet on noise and refuses with too little data; outage bounds from probes; timeline separates monitor offline from outage and unknown; causal anomaly detection; the rank-pool Mann-Whitney matches the direct computation exactly; Explorer paging and sorting stay consistent |
| Exports and import | XLSX overflows onto `Raw Data 2…` with every row present and ≥ 5 native charts; CSV and CSV.GZ are complete with metadata lines and no public IP; the ZIP verifies with its own `verify_export.py` and detects a changed value; legacy CSV import repairs NUL bytes, flags 0 bit/s results, moves IPs to private diagnostics, is idempotent and never modifies the source |
| Agent | a simulated test is accepted by the backend; an engine failure triggers the fallback, and both rows are stored; a pre-test outage records a failed test without running the engine; the outbox keeps order and sequence across restarts; probe state classification (ok, degraded, wan_down, dns_fail, lan_fault, link_down); real ICMP to loopback |

## On the real Pi and line

| Scenario | How | Result |
|---|---|---|
| Successful measurement | `systemctl kill --kill-whom=main -s USR1 speedagent` | 136.8/20.8 Mbps recorded with diagnostics, ledger entry, nearby server |
| Confirmation and burst | automatic after a < 500 Mbps result | 2 confirmations, then burst mode, as designed |
| Backend outage | `systemctl stop speedmon` for 3 min while the agent ran | a confirmation test ran during the outage; records queued, then delivered within seconds of restart; no sequence gaps; integrity OK |
| Service restart | `systemctl restart speedagent` (several times) | clean stop recorded, `previous_clean_stop: true` on the next start, queue flushed before exit |
| Internet unavailable | throwaway backend and agent in a systemd sandbox allowing only loopback + LAN (no change to production data or firewall) | probes `wan_down` with the router still answering; scheduled and manual tests recorded as failed `wan_down` |
| Speed-test server unavailable | same sandbox, pinned a non-existent Ookla server | Ookla `server_error` recorded, speedtest-go fallback measured 134.8 Mbps automatically |
| Backups | `systemctl start speedmon-backup` | consistent copy, `integrity_check ok`, ledger head written beside it |
| Weekly verification | `systemctl start speedmon-verify` | `RESULT: OK` (69,012 records re-hashed) |
| Website unaffected | curl through Cloudflare after the nginx change | the site's other pages unchanged; `/speedtest/*` all give the identical 303; `/speedtest/agent/*` 404 |
| Historical import | `cli import-legacy` | every CSV row (13 repaired) plus the XLSX rows not already in the CSV; source files unchanged (SHA-256 checked before/after) |
| Real-data XLSX | Export page on a read-only copy of production | 34,490 rows, 10.6 MB, < 30 s on the Pi; layout checked |
| Mobile | 375 × 812 viewport | no horizontal scroll; hero, ticker, charts and tables reflow |
| Pi reboot | `systemctl reboot` (a real reboot) | website back ~25 s after the command; speedmon, speedagent, nginx and cloudflared active; timers scheduled; no failed units. The agent's clean `agent_stop` was delivered before shutdown; on boot it waited 30 s for NTP sync, then logged `agent_start` with `rebooted: true`, `previous_clean_stop: true`; probes resumed; integrity OK, no sequence gaps |


### Multi-year scale run (Mac, synthetic, 16 Oct 2026)
10 years at **5-minute** intervals (7× denser than the default schedule): **1,286,716 tests, 2,657,839 probes**,
5.2 M ledger entries, 4.4 GB database.

| Request | Time (first / cached) |
|---|---|
| Live view | 0.09 s |
| Speed chart, all 10 years (30-day buckets) / last 24 h (raw) | 3.2 s / 0.08 s |
| Latency chart, all years / 7 days | 0.46 s / 0.03 s |
| Summary, all data | 36 s / 1.4 s (cached until new data) |
| Summary, 30 days | 0.5 s |
| Reliability timeline, all / 7 days | 10.6 s / 1.5 s |
| Calendar, all | 9.7 s |
| Congestion detector, all (was 164 s before the rank-pool rewrite) | 14 s |
| Explorer page at offset 1,200,000, sorted by download (was 115 s before id-subquery paging) | 0.12 s |
| Evidence, all data (was 214 s) | 63 s |
| **XLSX, all data** | 27 min, 613 MB; `Raw Data` 1,048,575 rows + `Raw Data 2` 238,141 rows = all 1,286,716 |
| **CSV.GZ, all data** | 9 min, 340 MB, 1,286,716 rows |
| **ZIP evidence pack, all data incl. probes** | 30 min, 1.45 GB; `verify_export.py` re-hashed 3,944,555 records and checked 5,231,270 chain links: OK |

The scale run also found two defects, both fixed and covered: XLSX files above 4 GB of XML need ZIP64 (now
switched on above 400k raw rows), and deep Explorer pages were slow (now id-subquery paging with covering indexes).
At the default 30-minute schedule, ten years is ~175k tests, about 7× less than this run. The Pi is roughly 2–3×
slower than the Mac. The real-data XLSX (34,490 rows) took under 30 s on the Pi.

### Added 16 Oct 2026 (evening)
| Scenario | Result |
|---|---|
| Package history | a 135 Mbps test is "on target" under M125 and "severe" under Gig1 on the day after the switch; summaries, streaks, chart bands and per-package figures follow each test's own package (test) |
| Agent restarted mid-download | the restart waited ~16 s for the running Ookla test to finish (127.0/20.4 Mbps recorded normally) before stopping (`KillMode=mixed`) |
| "Run a test now" signal during a running test | engines now start with SIGUSR1 ignored, so the signal can't kill a test (test); the documented command uses `--kill-whom=main` |
| Fresh start, full rehearsal on the Pi | throwaway backend + agent as runtime units; the script, run from `/` as systemd does, detected "due", stopped and drained the agent, stopped the backend, archived and verified (7 records), started a fresh database with a new chain, recorded the archive SHA-256 and previous head, cleared the setting (now "not due"), restarted both; probes flowed into the fresh database. The rehearsal first exposed, and led to fixing, a bug where the due check failed silently outside the app folder |
| Pre-reset straggler | a record from before the fresh start is quarantined ("belongs to the archived period"), not lost or mixed in (test) |
| Guarantee watch | consecutive-day runs below 565 Mbps; a missing day or a failure-only day ends a run (test) |

Two tests were cut short by my own deployment work before these fixes (21:19 and 21:23 UTC on 16 Oct: `exit -15`
and `exit -10`). They stay in the raw data as recorded.

### History length (20 Oct 2026)
Synthetic 1, 2 and 3 years (30-minute tests, minute probes: 3 years is 55k tests and 1.6M probes, 1.24 GB), every
API call cold, on the Mac. Before the fixes the default Live page (7 days) took 0.56 / 0.74 / 1.56 s, because every
request scanned the whole probe index for the data bounds. After (bounds as single index lookups, one pass for probe
latency, sequence gaps from index counts) it's 0.48 / 0.47 / 0.46 s, the same whatever the history. Results were
compared with the old code on the 3-year data and are identical. Still growing with history: Summary and Evidence on
"All" (3 years: 2.8 s and 5.0 s on the Mac). The public demo on the Pi (3 years): Live shows its first chart in 0.7 s
and has finished loading in 1.7 to 2.4 s; Evidence for 30 days 1.8 s.

### Long ranges (20 Oct 2026, not deployed yet)
Daily probe roll-ups (migration 0004, `speedmon/rollups.py`) and the in-memory test store (`analytics._TestStore`).
Same synthetic databases, checkpointed, plus a 3-year demo database (`demo/generate.py`: two packages, owner flags,
fractional loss values). Mac, fresh result cache, best of 3, process already running:

| Request | 1 year before / after | 2 years | 3 years | 3-year demo |
|---|---|---|---|---|
| Summary, All | 0.54 / 0.23 s | 2.06 / 0.46 s | 2.89 / 0.71 s | 3.25 / 0.67 s |
| Evidence, All | 1.81 / 0.42 s | 4.09 / 0.68 s | 8.25 / 1.09 s | 8.73 / 1.11 s |
| Summary, 1 year | 0.49 / 0.23 s | 0.48 / 0.21 s | 0.49 / 0.21 s | 0.46 / 0.20 s |
| Evidence, 1 year | 1.75 / 0.37 s | 1.33 / 0.34 s | 1.40 / 0.34 s | 1.26 / 0.40 s |
| Timeline / heatmap / congestion, All | 0.25 / 0.26 / 0.21 → 0.05 / 0.04 / 0.03 s | 0.53 / 0.42 / 0.45 → 0.08 / 0.08 / 0.06 s | 0.68 / 0.73 / 0.70 → 0.12 / 0.12 / 0.09 s | 0.59 / 0.77 / 0.60 → 0.09 / 0.12 / 0.09 s |

Every figure of summary, evidence, timeline, heatmap, congestion, anomalies, DNA and daily for All, 1 year and 30 days,
and the calendar, was compared with the old code on all four databases: identical. `tests/test_rollups.py` covers the
cases synthetic data doesn't (band edges, fractional loss, missing values, late diagnostics, flags, package and time
zone changes, a test delivered out of order, a fresh database at the same path, stale roll-ups). What still grows with
history is the Python statistics over the tests themselves (about 13 µs a test). After a restart the store loads every
test once in the background (0.8 s for 3 years); it holds about 23 MB for 3 years. Migration 0004 took 12 s on
3 years of probes and adds 6% to the database (two indexes on the round-trip times).
