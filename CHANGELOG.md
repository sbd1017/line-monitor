# Changelog

What changed in each release of Line Monitor, newest first.

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
