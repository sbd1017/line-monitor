# Changelog

What changed in each release of Line Monitor, newest first.

## v1.10.0 - 2026-11-20

- Always dark: the unused light palette (data-theme="light") is removed; printed evidence stays black on white

## v1.9.0 - 2026-11-13

- Automatic compensation now knows the 2027 England and Wales bank holidays.
- The compensation note on Evidence now says isp and outage notes count as a report, as well as complaint ones.
- Docs: the confirmation and quiet-check thresholds follow the package, Integrity sits at the bottom of Evidence, and production sign-in uses the site-wide login.

## v1.8.0 - 2026-11-08

- Timestamp receipts: every hour two public RFC 3161 authorities (FreeTSA and DigiCert) sign a statement of the ledger head, so records can be shown to have existed at that time; stored outside the chain, append-only, re-verified to a trusted root by the weekly check, with `anchor`, `anchors` and `verify-anchors` commands and up to 200 receipts plus a checker in every ZIP pack
- Where loss starts: a traceroute past the house every 15 minutes (mtr), kept as its own append-only record stream and read as clear, at the router, past the router or unclear; shown on Live, explained in the methodology, and exported as paths.csv with addresses masked unless public IPs are included
- Evidence: expected automatic compensation for each outage under Ofcom's scheme, from the report note, two full working days that skip weekends and bank holidays, and the daily rate in force at the time; an outage with no report is listed as unclaimed
- Operations: recording the ISP's own status answer during an outage as an isp note, which also starts the compensation clock
- Demo: a traceroute every quarter hour that follows the story (loss past the router in the fault week, a silent router while it restarts, the odd busy router)
- README: where loss starts, timestamp receipts, automatic compensation, and mtr and openssl in the requirements

## v1.7.0 - 2026-11-03

- Simple: a new page that answers "am I getting what I pay for?" in plain words, with three numbers (what you pay for, what you usually get, your slowest test), when it is slow, whether it drops out, the ISP's guarantee, and the steps to take and what to say when something is wrong
- Live: Anomalies shows the latest three, with Show more for the rest
- Central login: the app now also requires its own proxy key (X-Site-Proxy-Key) from nginx, so other processes on the server cannot pose as the owner
- Dependencies: FastAPI 0.120.4 and Starlette 0.49.3
- Demo: a minute probe and a test's pre-test probe landing on the same millisecond no longer clash
- README: the Simple page with a screenshot, test quality labels, anomalies, the site-login key, and Integrity now in Evidence

## v1.6.0 - 2026-10-21

- Pages can no longer appear twice when a navigation starts while another is still loading (double click, quick Back)

## v1.5.0 - 2026-10-20

- A public demo (demo/): three years of generated history for a made-up line, kept running by a minute-by-minute simulation, with a nightly reset and read-only settings
- Central sign-in: the app can trust a site-wide login in front of it (speedmon/site_auth.py), off unless SITE_AUTH=1
- Faster pages with years of history: the dashboard no longer scans every probe on each request, Evidence reads probe latency in one pass, and the integrity check uses index counts
- Testing notes: timings at one, two and three years of data
- Long periods are much faster: per-day probe summaries (exact, rebuildable) and an in-memory store of tests, so All-time Evidence and Summary no longer re-read years of data
- The average probe loss is now computed exactly on every SQLite version
- Code cleanup, no behaviour change: one shared download-band rule (was written out four times), clearer names (Measurement, requested_period), dead helpers, constants and CSS removed, repeated inline styles moved to CSS
- Cached pages refresh when a test's diagnostics arrive, so a test no longer shows as Not verified until the next one

## v1.4.0 - 2026-10-18

- Live: a small floating section bar (Live, History, Promise, Reliability, Patterns, DNA) that follows your scroll position, jumps to each section, and gives each one a shareable link such as /speedtest/#reliability
- Measurement quality: every test is labelled clean or with what on the monitor's side could have held it down (Pi busy, Pi under load, Ethernet errors, link too slow), from diagnostics it already records; Promise kept, Evidence and the XLSX show clean-only figures beside the full ones; Explorer filters by quality and the test drawer shows the readings behind each label
- Monitor capacity: a warning on Live and in Evidence when the Pi's link can't measure the package (e.g. Gig1 on 1 Gb/s Ethernet)
- Promise kept: the guarantee watch no longer disappears when there's been no on-target run
- The 'next test in' countdown in the top bar now matches the Live page (it was counting down to the short pause after a test)
- Pi busy is now relative to the package: other traffic on the Pi above 5% of the advertised download (at least 5 Mbps), so a TV stream counts on M125 but barely on Gig1
- Before a test the agent waits for a lull below the same figure (up to 3 x 30 s), automatically from the package unless set in Settings
- XLSX: Excel no longer asks to repair the file (the Incidents sheet wrote an empty frozen pane)

## v1.3.0 - 2026-10-17

- Charts: hovering a point shows its pop-up again, and scroll or double-click zoom work after the first load (a timer had overwritten the chart's position-to-time function)
- Live: the legacy-data checkbox and legend key only appear when something was imported

## v1.2.0 - 2026-10-17

- Integrity moved to the bottom of Evidence (its own tab is gone; old links go there)
- Explorer colours upload against the package's upload thresholds, and pings above the latency threshold
- The legacy-data options hide when nothing was imported
- Compare: one choice of what to compare (week, month, before/after a date, custom) with only its own options showing, the dates in words, and plain-English verdicts that say why when there isn't enough data
- Live: the Connection DNA chart no longer fails when its card is very small

## v1.1.0 - 2026-10-17

- Live: the next-test countdown no longer counts down to the short pause after a test, a pause or the daily limit

## v1.0.0 - 2026-10-16

- First release of Line Monitor.
