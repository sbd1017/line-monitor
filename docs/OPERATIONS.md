# Operating /speedtest

Paths below are on the Pi (`ssh pi`). `APP=/home/pi/line-monitor`.
Commands that act as the backend use this prefix, written `SM` below:

```bash
cd /home/pi/line-monitor && sudo -u speedmon env SPEEDMON_ENV=production SPEEDMON_DATA_DIR=$PWD/var .venv/bin/python -m speedmon.cli
```

## First-time: set the entrance phrase (you, at a hidden prompt)
Nobody can sign in until this is done. It's at least 12 characters and never leaves the Pi.
```bash
ssh -t pi 'cd /home/pi/line-monitor && sudo -u speedmon env SPEEDMON_ENV=production SPEEDMON_DATA_DIR=$PWD/var .venv/bin/python -m speedmon.cli set-phrase'
```
Then open https://example.com/speedtest/. Changing the phrase later uses the same command and signs everyone out.

## Services

| Unit | What | Starts at boot | Restarts on crash |
|---|---|---|---|
| `speedmon.service` | backend, 127.0.0.1:8772 | yes | always, after 3 s |
| `speedagent.service` | probes + speed tests | yes (after network-online, time-sync) | always, after 5 s |
| `speedmon-backup.timer` | daily 03:37 consistent backup, keeps 60, verifies the copy | yes | (oneshot) |
| `speedmon-verify.timer` | weekly full integrity check (Sun 04:23), result in the journal | yes | (oneshot) |

```bash
systemctl status speedmon speedagent --no-pager
journalctl -u speedagent -n 50 --no-pager -o cat          # tests, events, backend reachability
journalctl -u speedmon-verify -n 5 --no-pager -o cat      # last weekly integrity result
```

The agent waits up to 3 minutes for the clock to sync after a boot, so tests are never stamped with a wrong time.
Each start records an `agent_start` event saying whether the Pi rebooted and whether the previous stop was clean.

## Environment variables (set in the unit files, `deploy/systemd/`)

| Variable | Default | Meaning |
|---|---|---|
| `SPEEDMON_ENV` | production | `production`, `development` or `test`. Production refuses synthetic data and requires Secure cookies |
| `SPEEDMON_DATA_DIR` | `$APP/var` | database, entrance hash, backups, exports |
| `SPEEDMON_HOST` / `SPEEDMON_PORT` | 127.0.0.1 / 8772 | listen address (loopback only) |
| `SPEEDMON_AGENT_KEY_FILE` | (systemd credential `agent-key`) | HMAC key file, for development only |
| `SPEEDMON_STATUS_FILE` | (unset: not written) | public-safe status for other apps; production `/run/speedmon/status.json` |
| `SPEEDAGENT_ID` | pi | agent name in every record |
| `SPEEDAGENT_STATE_DIR` | /var/lib/speedagent | outbox, policy state, engine HOME |
| `SPEEDAGENT_BACKEND` | http://127.0.0.1:8772/speedtest/agent/v1 | where to push |
| `SPEEDAGENT_OOKLA`, `SPEEDAGENT_SPEEDTEST_GO` | /opt/speedmon/engines/… | engine binaries |
| `SPEEDAGENT_IFACE`, `SPEEDAGENT_GATEWAY` | from the routing table | override the interface or router address |
| `SPEEDAGENT_ANCHORS` | 1.1.1.1,8.8.8.8 | external ping targets |
| `SPEEDAGENT_DNS_SUFFIX` | example.com | random-label DNS lookups (never cached) |

Secrets: `/etc/speedmon/agent.key` (root 0600) and `$APP/var/entrance.json` (scrypt hash only). Neither is in
the project folder on the Mac or in any export.

## Run a test now
* Dashboard: **Live → Test now** or **Settings → Run a test now** (at most one every 3 minutes, 24 a day).
* Shell: `sudo systemctl kill --kill-whom=main -s USR1 speedagent` (`--kill-whom=main` matters: without it the signal also reaches a running speed test)

## Pause / resume full tests (probes keep running)
* Dashboard: **Settings → Pause full tests** (1 h / 4 h / 24 h / 7 days / Resume).
* Shell, backend: `SM pause 4` and `SM resume`
* Shell, works even if the backend is down: `sudo -u speedagent touch /var/lib/speedagent/PAUSE` (delete the file to resume)
* Stop everything: `sudo systemctl stop speedagent`. The gap shows as **monitor offline**, never as an outage.

## Packages (what the line is judged against)
**Settings → Packages.** Each package applies from its start date (local midnight) until the next one, and every test
is judged against the package in force that day, so an upgrade doesn't rewrite how older results are assessed. The
agent's confirmation and burst thresholds switch automatically on the date. For example, Virgin Media M125 (132/20)
until an upgrade, then Gig1 from its first day with the contract's figures (advertised 1,130/104, minimum guaranteed
download 565, upload minimum 52). Gig1 is "on target" at ≥ 900 because the Pi's own 1 GbE port tops out near 940 Mbps.
To measure past that, use a 2.5 GbE USB adapter (Realtek RTL8156B, for example UGREEN's) cabled to a 2.5G port on the
router. It has to be USB-A, in one of the Pi's blue USB 3 ports: the Pi 5's USB-C port is power only. If the adapter
isn't the default route, set `SPEEDAGENT_IFACE` to it. **Pi to router** on the Live page then shows 2.5 Gb/s, and the
package's on-target figure can go up to match.

## Fresh start (archive everything, begin again)
**Settings → Fresh start.** Pick a date and time (say, a few hours after a new router or package goes live). Change it there, cancel it, or press **Start fresh now** (type START FRESH). Every 5 minutes a root timer
(`speedmon-epoch-reset.timer`) checks whether it's due. When it is, `scripts/epoch-reset.sh` stops the agent (a
running test completes), drains its queue, stops the backend, copies the database to `var/archive/<time>-before-reset/`,
**verifies the copy**, moves the original beside it (nothing is deleted), starts a fresh database (settings carried over,
new hash chain), clears the agent's scheduling memory and restarts both. The Integrity page then shows the archive's
SHA-256 and the previous ledger head. Watch it: `journalctl -u speedmon-epoch-reset -n 30 --no-pager -o cat`.
If it fails before archiving, nothing changes and it retries an hour later. The legacy speedtest-cli history goes into
the archive with everything else; re-import it any time with `SM import-legacy …` (see below) if you want it alongside
the new data. It's kept out of the statistics anyway.

## Guarantee watch
Under Virgin Media's Gig1 terms, if download speed falls below the **minimum guaranteed 565 Mbps** on **3 or more consecutive
days** (continuously or intermittently) and Virgin can't fix it within 30 days of you reporting it, you may leave
without an early-disconnection fee. The Live view (Promise kept), Evidence and the XLSX summary track this: per local
day, successful tests below 565; runs of consecutive days; runs of 3+ days are flagged. Failed tests (outages) are
left out, as the contract excludes outages, and a day without results ends a run.

## Change the test frequency
**Settings → Test schedule.** Values below 15 minutes (or a burst below 10) ask for confirmation, and the page
shows the expected data use. The agent picks up changes within 15 s and enforces its own floors (5 min, ≤ 300
tests/day) whatever is saved.

## Backups

* **Automatic:** daily into `$APP/var/backups/speedmon-<UTC time>.sqlite3` (newest 60 kept), made with SQLite's
  online backup API, `PRAGMA integrity_check`ed, with a `.head` file recording the ledger head at that moment.
* **Manual:** `SM backup --keep 60`
* **Off the Pi (recommended, weekly):** the backups sit on the same NVMe as the live database. Copy the newest one to the
  Mac (it lands in the current folder; keep it somewhere private):
  ```bash
  ssh pi 'sudo sh -c "cat \$(ls -t /home/pi/line-monitor/var/backups/speedmon-*.sqlite3 | head -1)"' > speedmon-backup-$(date +%Y%m%d).sqlite3
  ```
  Or export **ALL DATA as ZIP** from the Export page each month: it verifies itself and needs no software to read.
* The agent's outbox (`/var/lib/speedagent/agent.sqlite3`) independently holds every measurement it ever made.

## Restore
1. `sudo systemctl stop speedagent speedmon`
2. Check the backup: `SM restore-check /path/to/speedmon-YYYYMMDDTHHMMSSZ.sqlite3` (must print `RESULT: OK`)
3. Keep the current file: `sudo -u speedmon mv $APP/var/speedmon.sqlite3 $APP/var/speedmon.sqlite3.before-restore` (and any `-wal`/`-shm`)
4. `sudo install -m 0600 -o speedmon -g speedmon /path/to/backup.sqlite3 $APP/var/speedmon.sqlite3`
5. `sudo systemctl start speedmon speedagent`. The agent re-sends anything newer still in its outbox (duplicates are
   recognised by UUID). Sequence gaps on the Integrity page show anything that can't be recovered.

## Updating the code
From your computer, after `.venv/bin/python -m pytest` passes:
```bash
scripts/deploy.sh
```
It copies the code to the Pi (never `var/` or the venv), runs the tests there, restarts both services and then runs
any executable in `/usr/local/etc/speedmon/post-deploy.d/` on the Pi with the app's folder as its argument. Set
`PI` and `APP` in the environment if yours differ from the defaults at the top of the script.
Schema changes go in a new `speedmon/migrations/000N_*.sql` (applied on start). Never edit `0001_initial.sql`,
and never remove the append-only triggers. Back up first (`SM backup`). The agent finishes a running test before
it stops (TimeoutStopSec 180), and queued records are delivered after the restart.

## Exporting
**Export** page: choose a period (Today … ALL DATA or custom start/end with times) and a format:
* **XLSX report**: Summary with charts, Daily, Hourly & Weekday, Incidents, Annotations, Methodology, Integrity,
  then Raw Data (continuing to `Raw Data 2…` past 1,048,575 rows). Timings are in `docs/TESTING.md`.
* **CSV / CSV.GZ**: every test, all fields, with `#` metadata lines at the top.
* **ZIP evidence pack**: everything, plus `verify_export.py` (`python3 verify_export.py .` re-checks every hash).

Public IP addresses are left out unless you tick the box. Files are kept 24 h and their SHA-256 is listed on the
Export and Integrity pages.

## Importing old records
A speedtest-cli CSV log and/or an XLSX of the same columns. The files are only read, and re-running is safe: it only
adds lines it hasn't seen.
```bash
SM import-legacy --csv /path/to/speed_log.csv --xlsx /path/to/speed_log.xlsx
```
Imported rows are kept out of the statistics unless **Settings → Include legacy data** is on, because they came from
a different, less careful method.

To take the imported rows out again, stop both services first (as for a fresh start), then:
```bash
sudo systemctl stop speedagent speedmon
SM remove-legacy --yes
sudo systemctl start speedmon speedagent
```
Raw records are never edited, so this works like a fresh start: the whole database is copied to
`var/archive/<time>-before-rebuild/` and verified, then a new one is built with every other record copied exactly as
stored, in the same order, and the hash chain recomputed over them. The archive keeps the old chain. The Integrity
page and the audit log say what was removed and where the original is, and the source files are only ever read.

## Troubleshooting

| Symptom | Check |
|---|---|
| Dashboard says **Monitor offline** | `systemctl status speedagent`; `journalctl -u speedagent -n 50`. Was the Pi off? (Events show `rebooted: true`.) |
| **Queue** growing on the Live page | the backend isn't accepting: `systemctl status speedmon`, `journalctl -u speedmon -n 50`. Records are safe in the outbox. |
| Quarantined records (Integrity page) | the reason is shown. Fix the cause, then `sudo -u speedagent env SPEEDAGENT_STATE_DIR=/var/lib/speedagent $APP/.venv/bin/python -m speedagent.cli requeue-rejected` |
| Tests fail with `engine_missing` | the binary moved: check `/opt/speedmon/engines/` and the unit's `SPEEDAGENT_OOKLA` |
| Ookla `server_error` | the fallback engine runs automatically; persistent failures mean the server list is unreachable (try Settings → server mode auto) |
| All tests `wan_down` | the Internet really was unreachable while the router answered: see the outage list |
| `lan_fault` / `link_down` | the Pi or router side, not counted against the ISP: check the cable and the router |
| **Pi to router** isn't "cable OK" (below 1 Gb/s or half duplex) | renegotiate or replace the cable. The Pi can't measure a gigabit line on a 100 Mb link |
| Locked out after wrong phrases | wait 15 min or `sudo systemctl restart speedmon` |
| Verify fails | don't restore over it: keep the file, run `SM verify` and read the problems. Compare with the newest backup's `.head` |

## Is it the Pi, the LAN or the ISP?
If the results sit well below the package with the Pi idle and a clean 1000/Full link, separate the layers:
1. **Pi:** each test's diagnostics show link speed, errors, CPU, throttling and other traffic on the Pi.
2. **Router:** check for QoS/SQM bandwidth limits, a VPN client, DNS filtering, or a WAN port that negotiated less
   than expected. Run the router's own speed test if it has one.
3. **ISP:** with the ISP's hub in modem mode, plug a laptop straight into it and run speedtest.net. If that's just as
   slow, the problem is the ISP's side, and the evidence from this system applies.
4. Annotate whatever you change (Notes page), so before/after shows up in **Compare**.

## Status files for other apps
Two small files let other apps on the Pi show the line's state without access to the database or the private state
directories. Both are 0644, replaced atomically, and hold whitelisted fields only: no IP addresses, hosts, interface
names, agent id, result URLs or paths. Tests check that.

* **`/var/cache/speedagent/latest.json`**, written by the agent after every test (`speedagent/latest.py`): time,
  status, kind, engine, failure code, down/up Mbps, ping, jitter and server name, plus the last successful result.
  It sits in the unit's `CacheDirectory=` (0755) so the 0700 state directory stays private.
* **`/run/speedmon/status.json`**, written by the backend every 30 s (`speedmon/statusfile.py`, `RuntimeDirectory=`):
  health, package name and advertised speeds, the latest and latest successful test (down/up, ping, band, % of
  advertised), agent state (online, testing phase and live Mbps, next test time, paused), 24 h and 7 d averages,
  the guarantee watch and the dashboard URL. Treat a file whose `generated_at_ms` is more than a few minutes old as
  "monitor not reporting".
