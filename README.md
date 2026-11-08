# Line Monitor

I built this because my broadband kept being slower than what I pay for, and a screenshot of one speed test
proves nothing. It runs on a Raspberry Pi 5 wired to my router, tests the line around the clock, and keeps
every result in a form I can hand to my ISP or an ombudsman: the raw records, how they were measured, and a
way to check none of them were changed afterwards.

It's written around a UK Virgin Media Gig1 line (1,130 Mbps advertised, 565 Mbps minimum guaranteed), but the
package figures are settings. Any connection works, and you can keep a history of packages, so an upgrade
doesn't change how older results are judged.

There are two ways to read it. The Simple page answers one question in plain words, "am I getting what I pay
for?", with three numbers and what to do next, so anyone in the house can use it. Everything else is the
detailed view, for when you want to see exactly when, how often and by how much the line falls short.

## Live demo

**[mubar.uk/speedtest](https://mubar.uk/speedtest/)**: open it and look around, no sign-in needed.

- It shows three years of generated results for a made-up Virgin Media line, upgraded from M350 to Gig1
  partway through, with outages, a fault week and evening slowdowns built in.
- It keeps running. New results and traceroutes arrive as if the monitor were live, and "Run a test now" adds
  one. No real speed tests are run, and there are no timestamp receipts because nothing real is being signed.
- Settings, notes and pausing are read-only.
- Exports work (a full three-year XLSX takes about two minutes) and are deleted after two hours.
- The data goes back to its starting point every night at 04:00 UK time.

![The Simple page: a plain answer, what you pay for, what you usually get and your slowest test](docs/screenshots/simple.png)

<sub><b>Simple</b>: one answer, three numbers, when it's slow, whether it drops out, and Virgin's promise. When
something is wrong it lists the steps to take and what to say to your ISP.</sub>

![The Live page: latest test, the gauge and the line's status](docs/screenshots/live.png)

<table>
<tr><td width="50%"><img src="docs/screenshots/history.png" alt="Speed history chart with download and upload"><br><b>Speed history</b>: every test, zoomable from a year down to single results, with failures and anomalies marked</td>
<td width="50%"><img src="docs/screenshots/promise.png" alt="Promise kept: how often the line hit its targets"><br><b>Promise kept</b>: how often the line actually delivers, plus the guarantee watch</td></tr>
<tr><td><img src="docs/screenshots/slowdown.png" alt="Day by hour heatmap, hour of day and day of week"><br><b>When does it slow down?</b> Hour by hour, every day, with a congestion check</td>
<td><img src="docs/screenshots/calendar.png" alt="A year of days, one square each"><br><b>Every day</b> for a year, coloured by its median download</td></tr>
<tr><td><img src="docs/screenshots/evidence.png" alt="Evidence report"><br><b>Evidence</b>: a report for any period that prints or saves as a PDF</td>
<td><img src="docs/screenshots/compare.png" alt="Comparing two periods"><br><b>Compare</b> two periods, with a significance test rather than a guess</td></tr>
<tr><td><img src="docs/screenshots/explorer.png" alt="Explorer table of raw records"><br><b>Explorer</b>: every record, filterable, each one opening its full detail</td>
<td><img src="docs/screenshots/export.png" alt="Export page"><br><b>Export</b> to XLSX, CSV or a ZIP evidence pack, with no row limit</td></tr>
<tr><td><img src="docs/screenshots/dna.png" alt="Connection DNA rings"><br><b>Connection DNA</b>: a day, a week and a month at a glance</td>
<td><img src="docs/screenshots/phone.png" alt="The Live page on a phone" width="60%"><br><b>On a phone</b></td></tr>
</table>

<sub>The screenshots use generated demo data, not a real line.</sub>

## What it does

- **Full speed tests every 30 minutes** with the Ookla Speedtest CLI, at a random moment in each half hour so
  every minute of the hour gets sampled. If Ookla fails, speedtest-go runs instead.
- **A quick check every minute** (ping to the router and two outside hosts, a DNS lookup, an HTTPS request), so
  outages and latency spikes between tests are caught too.
- **More tests when it matters.** A poor result gets two confirmation tests; if they agree, it tests every 10
  minutes for a while. If the line has been bad for hours, it stops piling on extra tests. Hard limits in the
  agent's code mean no setting can make it test every few seconds.
- **Failures are results.** A test that couldn't run is recorded with the reason, never dropped and never
  counted as 0 Mbps. A Pi that was switched off shows as "monitor offline", not as an outage.
- **Fair to the ISP too.** Before a test the agent waits for other traffic on the Pi to die down. Every test is
  then labelled clean, or with what on the monitor's side could have held it down (Pi busy, Pi under load,
  Ethernet errors, a link too slow for the package). Clean-only figures sit beside the full ones, and the Live
  page warns you if the Pi's own link can't measure the package you pay for.
- **Statistics that don't overstate.** Percentages and averages use the scheduled tests only, because the extra
  tests deliberately sample the bad moments. Outage lengths are given as ranges, and the congestion check only
  says the evenings are slower when the numbers support it.
- **Where the loss starts.** Every 15 minutes a traceroute runs past the house. Hop 1 is your own router, so loss
  that starts there is inside the house, and loss that starts after it and carries on to the end is on the ISP's
  side. A router in the middle that drops a few probes but has clean hops after it is only busy, so that case is
  called unclear rather than blamed on anyone.
- **Anomalies.** Tests far outside what's normal for that hour and type of day (weekday or weekend) are picked
  out against the previous four weeks, so a one-off drop stands out from the usual evening dip.
- **Guarantee watch.** Under Virgin Media's terms, three days in a row below the minimum guaranteed speed lets
  you report it, and if it isn't fixed in 30 days you can leave without a fee. The dashboard and the XLSX track
  those runs of days for you.
- **Automatic compensation.** For each outage, the Evidence report works out what Ofcom's automatic compensation
  scheme should pay: the date you reported it (from a note), two full working days skipping weekends and bank
  holidays, then the daily rate in force at the time. With no report noted it says so instead of guessing.
- **Exports with no row limit.** A designed XLSX (summary with charts, daily and hourly tables, incidents,
  methodology, then every raw record, continuing onto a second sheet past Excel's million-row limit), CSV,
  CSV.GZ, or a ZIP evidence pack with the traceroutes, the timestamp receipts and small scripts to re-check every hash
  and every receipt.
- **Notes on the timeline**, such as "router replaced" or "engineer visit", so the before and after show up in
  Compare.
- **A fresh start** when a new package goes live: the old database is archived and verified, and a new one
  begins.

## Why the records can be trusted

Every record is hashed when it's made and chained to the one before it, so changing or removing anything
afterwards breaks the chain, and the integrity check at the bottom of the Evidence report shows it. The database refuses edits to raw records
altogether. Corrections and notes go in a separate layer that's also kept with its history. The agent numbers
every record it sends, so a missing one shows up as a gap. Every hour, two public timestamp authorities (FreeTSA and DigiCert, RFC 3161, no account needed) also sign a
statement of the current chain head. The receipts prove the records existed at that time, so even someone able to
rewrite the whole database, including me, couldn't backdate them without it showing. The weekly check re-verifies
every receipt. This is tamper evidence, not a certificate.

## How it fits together

Two small services on the Pi. The agent runs the tests and keeps everything in its own queue first, then sends
it to the backend over a signed connection on the Pi itself. If the backend is down or being updated, nothing
is lost: the queue catches up when it's back. The backend stores the records and serves a private dashboard
behind a pass phrase. It can also sit behind a site-wide login on nginx instead (off unless `SITE_AUTH=1`); then
nginx has to send a secret key of its own, so another program on the same machine can't pretend to be you.
Nothing on the home network needs an open port: I reach mine through a Cloudflare tunnel and nginx.

## Built with

Python 3.11, FastAPI and SQLite on the Pi; plain JavaScript modules and canvas charts in the browser, with no
framework, no build step, nothing loaded from other sites, and no analytics. XlsxWriter for the workbooks. The
Ookla Speedtest CLI and speedtest-go do the actual testing.

## Running it

You'll need a Raspberry Pi (or any Linux box) wired to the router by Ethernet. Wi-Fi can't measure a fast line
properly, and a Pi 5's own gigabit port tops out around 940 Mbps. To measure above that, add a 2.5 GbE USB
adapter with a Realtek RTL8156B chip (UGREEN make one, for example) and cable it to a 2.5G port on the router.
It has to be a USB-A adapter in one of the Pi's blue USB 3 ports, because the Pi 5's USB-C port is for power
only. Also:

- Python 3.11 or newer.
- The Ookla Speedtest CLI (its licence has to be accepted once) and, optionally, speedtest-go.
- mtr for the traceroutes (`mtr-tiny` on Raspberry Pi OS) and openssl for the timestamp receipts.
- systemd and nginx if you want it set up the way mine is.

To try it on your own computer with demo data first:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
scripts/dev.sh
```

Then open http://127.0.0.1:8772/speedtest/ and sign in with `correct horse battery staple` (it only works for
the demo database).

The systemd units and nginx snippets I use are in `deploy/`. Setting the pass phrase, backups, restores, updates
and troubleshooting are in [docs/OPERATIONS.md](docs/OPERATIONS.md), and how it all works, including the
database and the hash chain, is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Licence

MIT. See [LICENSE](LICENSE).
