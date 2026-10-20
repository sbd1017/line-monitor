# Public demo

A copy of Line Monitor that anyone can open, filled with three years of generated results for a made-up Virgin Media
line, with new results arriving live. It runs the unchanged app (`speedmon/`) and never measures anything.

| File | Role |
|---|---|
| `world.py` | the made-up line, as a deterministic function of time (seed + the day the history ends) |
| `simulate.py` | runs the agent's own scheduling policy (`speedagent/policy.py`) against that line and emits the records the agent would send: tests with diagnostics, a probe a minute, events |
| `generate.py` | builds the database: the records through the app's canonical form and hash chain, plus the owner's layer (package history, notes, flags) |
| `tick.py` | every minute: carries the simulated agent on to now through the backend's own ingest, and answers "Run a test now" |
| `serve.py` | the web server: no entrance phrase (a per-visitor cookie and CSRF token instead), read-only settings and notes, limits on exports and full verifications, a demo banner |
| `deploy/` | systemd units and `install.sh` |

## The story in the data

Counted back from today: M350 (362/36) for about 16 months, then Gig1 (1,130/104). On Gig1 the Pi's own 1 GbE port
caps results near 940 Mbps (flagged "at the monitor's limit") until a 2.5 GbE adapter goes in. Evening congestion,
deeper on weekdays and in winter, and a lighter daytime dip at weekends. About a dozen Internet outages from 4 minutes
to 6 hours, a DNS fault, two router restarts (local, not counted against the ISP), two days of a bad cable at
100 Mb/s, the monitor itself off three times (a power cut, a move, updates), the odd failed test covered by the
fallback engine, and a week-long ISP fault five months ago that keeps speeds under the guarantee for more than three
days in a row, with confirmations and burst tests, a complaint and an engineer visit in the notes.

## Run it locally

```bash
python -m demo.generate --data-dir var-demo --days 120          # a shorter history builds in seconds
SPEEDMON_ENV=development SPEEDMON_DATA_DIR=var-demo SPEEDMON_SECURE_COOKIES=0 SPEEDMON_PORT=8921 python -m demo.serve
python -m demo.tick --data-dir var-demo                         # now and then, to add what fell due
```

Open http://127.0.0.1:8921/speedtest/. Tests: `python -m pytest demo/tests`.

## On a server

`sudo demo/deploy/install.sh --origin https://your.demo.host` copies the code to `/opt/demos/speedtest` (root-owned,
with its own venv), creates the `demo` user if needed, builds the data in `/srv/demos/speedtest` and starts:

* `demo-speedtest.service`: the web app on 127.0.0.1:8921, under the path `/speedtest/` (the reverse proxy passes
  the path through unchanged; no websockets or server-sent events, the dashboard polls). It may only talk to localhost.
* `demo-speedtest-tick.timer`: the minute updater (no network at all).
* `demo-speedtest-prebuild.timer` at 03:40 UK time builds a fresh three years ending that day next to the live data
  and verifies every hash; `demo-speedtest-reset.timer` at 04:00 stops the app, swaps the new data in (seconds),
  clears visitors' exports, catches up to the current minute and starts it again. If the pre-build isn't ready, the
  reset builds it itself (about seven minutes). `deploy/reset` is the script it runs.

The reverse proxy should refuse `/speedtest/agent/` (the demo has no agent key, so the app refuses it anyway).
Re-run `install.sh` after updating the app to refresh the demo's copy of the code.

Every record is `source='synthetic'` and the database is marked synthetic, so a production instance refuses to start
on it, and the demo refuses to start in production mode.
