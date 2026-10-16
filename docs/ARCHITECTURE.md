# Architecture

```
                         ┌────────────────────────── Raspberry Pi 5 ─────────────────────────────────┐
 ISP hub (modem) ───── router ─── eth0 ──┐                                                           │
                                         │  speedagent.service  (user speedagent)                    │
                                         │   ├ probes every 60 s: ICMP router + 1.1.1.1 + 8.8.8.8,    │
                                         │   │   random-name DNS lookup, HTTPS 204                    │
                                         │   ├ full tests: Ookla CLI (fallback speedtest-go)         │
                                         │   ├ diagnostics: link, NIC counters, CPU, temp, throttling │
                                         │   └ outbox  /var/lib/speedagent/agent.sqlite3 (FULL sync)  │
                                         │          │  HMAC-signed HTTP push (loopback)              │
                                         │          ▼                                                │
                                         │  speedmon.service  (user speedmon)  127.0.0.1:8772        │
                                         │   ├ ingest: validate → verify hash → store → ledger chain │
                                         │   ├ analytics, chart LOD, exports (background thread)     │
                                         │   └ var/speedmon.sqlite3  (0700, WAL, synchronous=FULL)   │
                                         │          ▲                                                │
 Internet ── Cloudflare ── cloudflared ── nginx 127.0.0.1:8081 ── location ^~ /speedtest/ ───────────┘
```

* **Push, not pull.** The agent pushes to the backend; nothing on the home network accepts unsolicited
  inbound connections. The agent API (`/speedtest/agent/*`) answers loopback peers only, refuses anything
  carrying Cloudflare headers, and nginx returns 404 for it from the internet.
* **The outbox is the safety net.** Every record goes to the agent's local SQLite outbox (with its sequence
  number and hash) before any network activity, and is only marked sent once the backend acknowledges it.
  When the backend is down, restarting or being upgraded, records wait there and flush within seconds of it
  returning. Measurements stay in the outbox permanently as a second copy; sent probes and events are pruned
  after 60 days.
* **Replaceable engine.** `speedagent/engines/` defines `Engine.run(server, progress, timeout) -> EngineResult`.
  Ookla CLI is primary and speedtest-go the fallback. A simulated engine exists for development and is refused
  in production. A new provider is a new class plus one line in `Agent.__init__`.
* **Same host, separate roles.** The agent and backend share nothing but the HMAC key (delivered by systemd
  `LoadCredential` from `/etc/speedmon/agent.key`, root 0600). The backend has no outbound network
  (`IPAddressDeny=any`). The agent has no access to the backend's `var/`. To move the agent to another device,
  point `SPEEDAGENT_BACKEND` at the backend and expose `/speedtest/agent/` in nginx (HMAC + timestamp + nonce
  already protect it).

## Measurement strategy (defaults; all editable in Settings)

| What | Default | Why |
|---|---|---|
| Scheduled full test | every **30 min**, at a random moment inside each slot | ~48 tests/day sample every minute of the hour evenly. A gigabit Ookla test moves ~1.3 GB and occupies the line ~25 s: about 1.4% of the time and ~2 TB/month at full speed (at today's ~135 Mbps it's ~140 MB per test). |
| Probes | every **60 s** | ~2 KB each. They catch outages and latency between tests. |
| Confirmation | 2 tests, 2 min apart, after a result < 500 Mbps | separates a blip from a real problem |
| Burst | every 10 min while confirmed degraded, max 2 h, ends after 2 good results | denser sampling exactly when it matters |
| Persistent degradation | if ≥ 80% of the last 12 scheduled tests were poor, confirmations and bursts are suspended | when the line is known to be bad, extra tests add load, not information |
| Daily cap | 96 full tests per local day | safety net |
| Hard floors (in the agent's code) | scheduled ≥ 5 min apart, burst ≥ 5 min, confirm ≥ 60 s, ≤ 300/day, probes ≥ 10 s | no setting can make it test every five seconds |
| Quiet check | postpone up to 3 × 30 s if the Pi itself is moving > 50 Mbps | the Pi may be doing other work (a website, a media server) |
| Pre-test probe | if the Internet or router is down, record a failed test (`wan_down`, `lan_fault`, ...) without running the engine | failures are data |

Statistics use **scheduled tests only**. Confirmation, burst and manual tests deliberately over-sample bad
moments; they appear in raw data, streaks and incidents but never in percentages or averages.

## Database (SQLite, `var/speedmon.sqlite3`)

Evidence tables are append-only (BEFORE UPDATE/DELETE triggers raise). `migrations/0001_initial.sql` is the
schema source of truth.

| Table | Kind | Contents |
|---|---|---|
| `measurements` | evidence | one row per full test, successful or failed; engine output in `raw`, its SHA-256 in `raw_sha256` |
| `diagnostics` | evidence | Pi-side context captured with each test (separate from measurements by design); `public_ip` is private |
| `probes` | evidence | 60-second connectivity probes and their state |
| `events` | evidence | agent start/stop (with reboot detection), mode changes, link/IP changes, deferred tests, config applied |
| `ledger` | evidence | hash chain over every evidence record in insertion order |
| `imports` | evidence | each legacy import: file name, SHA-256, counts |
| `quarantine` | evidence | submissions that failed validation or hash checks (kept, never dropped) |
| `measurement_flags` | owner layer | exclude / note / disputed, with reason; revoked, never deleted |
| `annotations`, `annotation_history` | owner layer | timeline notes, soft-deleted, full history |
| `settings`, `settings_history` | config | every change recorded |
| `sessions`, `audit_log`, `commands`, `agent_status`, `exports` | housekeeping | |
| `derived_measurement`, `rollup_probe_hourly` | derived | anomaly scores and hourly probe roll-ups; `python -m speedmon.cli rebuild-derived` recreates them |

Times are UTC (`t_ms` epoch ms, indexed, plus ISO text). Grouping by day/hour uses `Europe/London` with
correct DST handling (`timeutil.LocalClock`).

### Integrity scheme
`record_hash = SHA-256(canonical JSON of the stream's fields)`, with keys sorted, no spaces, UTF-8, REAL fields as
floats, and empty text as null. `chain_hash[n] = SHA-256(chain_hash[n-1] ":" stream ":" uuid ":" record_hash[n])`.
The agent computes the record hash at creation and the backend recomputes and refuses mismatches. The agent
numbers every record (`agent_seq`), so a lost record shows as a gap (Integrity page). `canonical.py` exists
twice (agent and backend) and a test asserts they're identical. This is tamper-evidence, not certification:
keep exported ledger heads somewhere else.

## Web app (`speedmon/web.py` + `speedmon/static/app/`)

* Phrase entrance (scrypt), server-side sessions (`__Secure-speedtest_session`, Path=/speedtest), CSRF token and
  Origin check on every POST. Unauthenticated requests get the identical 303/401 for every path.
* Vanilla ES modules, no build step and no external assets. CSP `script-src 'self'; style-src 'self'`.
* Charts: `timechart.js` (canvas, zoom/pan/pinch, crosshair, server-side level of detail through `/api/series`
  and `/api/latency`), `viz.js` (gauge, pulse, heatmap, calendar, DNA, bars, histograms, percentiles,
  timeline). Raw tests are sent when ≤ 2 points per pixel; otherwise SQL buckets (1 min to 30 days), using the
  hourly probe roll-up for long ranges.
* Views: Live, Evidence (paper style, prints to A4), Compare, Explorer (+ detail drawer), Export, Notes,
  Integrity, Settings, Day.

## Exports (`speedmon/exports/`)

Background job → file in `var/exports/<uuid>/` → download; kept 24 h. XlsxWriter `constant_memory` streams rows.
Raw data continues on `Raw Data 2…` at 1,048,575 rows per sheet. CSV/CSV.GZ have `#` metadata lines. The ZIP
holds everything plus `verify_export.py`. Each file's SHA-256 and the ledger head are recorded in `exports`.
