# Line Monitor: broadband evidence platform (`https://example.com/speedtest/`)

Long-term, evidence-grade measurement of a Virgin Media Gig1 connection, from a Raspberry Pi 5 wired to the router.

* **speedagent** (on the Pi): Ookla Speedtest CLI every 30 minutes at random moments, with a speedtest-go fallback;
  confirmation and burst tests when results are poor; connectivity probes every minute; Pi-side diagnostics. Records
  are stored in a local outbox first and pushed (HMAC-signed) to the backend, so nothing is lost while the
  backend is unavailable.
* **speedmon** (on the Pi, behind nginx and Cloudflare): append-only SQLite with a SHA-256 hash chain; a private
  dashboard (Live, Simple, Evidence, Compare, Explorer, Export, Notes, Settings); XLSX/CSV/ZIP exports with no
  row limits.

| Document | For |
|---|---|
| `docs/ARCHITECTURE.md` | components, measurement strategy, database schema, integrity scheme |
| `docs/OPERATIONS.md` | services, settings, backup and restore, updating, manual tests, pausing, exporting, troubleshooting |
| `docs/TESTING.md` | what was tested and how |

Quick start (development, synthetic data only):
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
scripts/dev.sh        # http://127.0.0.1:8772/speedtest/  phrase: correct horse battery staple
```
