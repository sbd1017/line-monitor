"""The /speedtest web app: dashboard shell, JSON API for the owner, and the signed agent API.

Boundary (``boundary`` middleware, runs before every route):
  * Anything outside /speedtest/ -> 404.
  * /speedtest/agent/* -> HMAC-signed requests from the measurement agent only, from loopback only, with no
    Cloudflare headers (nginx refuses that prefix from the internet as well). Never a browser session.
  * Without a live session, every other path except the entrance (and its stylesheet) gets the SAME answer
    whether or not it exists: GET -> 303 to /speedtest/enter, anything else -> 401. No handler runs.
  * Every POST needs a same-site Origin/Referer (when sent) and the session's CSRF token.
  * Every response: strict CSP (scripts and styles from this origin only), no-store, noindex, no framing.
Private values never leave the server: no IP addresses (public or internal), no paths, no keys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
import uuid as uuidlib
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analytics, auth, canonical, ingest, integrity, quality, series, settings_store, statusfile, timeutil
from . import site_auth
from .config import VERSION, Settings, load_settings
from .db import Database
from .exports import jobs as export_jobs

log = logging.getLogger("speedmon")
HERE = Path(__file__).parent
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
       "font-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
SPA_ROUTES = ("/", "/evidence", "/explorer", "/export", "/settings", "/compare", "/integrity", "/annotations")
FLAG_KINDS = ("exclude", "note", "disputed")
ANNOTATION_CATEGORIES = ("note", "engineer", "outage", "hardware", "firmware", "reboot", "isp", "complaint", "other")


class TTLCache:
    def __init__(self, size: int = 64):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
        return None

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    central_auth = site_auth.enabled()
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = Database(settings.db_path)
    db.migrate()
    with db.read() as conn:
        origin = settings_store.get_all(conn).get("data_origin")
    if settings.is_production and origin == "synthetic":
        raise SystemExit("Refusing to start in production on a database that contains synthetic data")
    sessions = auth.Sessions(db, settings.session_idle_minutes, settings.session_absolute_hours)
    limiter = auth.EntranceLimiter(settings.entrance_max_failures, settings.entrance_window_seconds,
                                   settings.entrance_overall_failures)
    agent_key = settings.agent_key()
    nonces: OrderedDict = OrderedDict()
    nonce_lock = threading.Lock()
    cache = TTLCache()
    live = {"status": None, "received_ms": None}
    manual_runs: list[float] = []
    exporter = export_jobs.Exporter(db, settings)
    exporter.cleanup()

    BASE = settings.base_path
    COOKIE = ("__Secure-" if settings.secure_cookies else "") + auth.COOKIE
    ENTRANCE, ENTRANCE_ASSETS, AGENT = BASE + "/enter", BASE + "/static/entrance/", BASE + "/agent/"

    def static_version() -> str:
        h = hashlib.sha256()
        for p in sorted((HERE / "static").rglob("*")):
            if p.is_file():
                h.update(p.name.encode())
                h.update(p.read_bytes())
        return h.hexdigest()[:10]

    STATIC_V = static_version()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.settings, app.state.db, app.state.exporter = settings, db, exporter
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(u=lambda p="/": BASE + p, V=STATIC_V, VERSION=VERSION, ENV=settings.env)
    app.mount(BASE + "/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------ boundary

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        if peer == settings.trusted_proxy:
            return request.headers.get("x-real-ip", peer)
        return peer

    def origin_problem(request: Request) -> bool:
        if request.headers.get("sec-fetch-site") == "cross-site":
            return True
        origin = request.headers.get("origin")
        if origin == "null":
            return True
        origin = origin or request.headers.get("referer")
        if not origin:
            return False
        parts = urlsplit(origin)
        here = f"{parts.scheme}://{parts.netloc}"
        allowed = set(settings.public_origins)
        if not settings.is_production:
            allowed.add(f"http://{request.headers.get('host', '')}")
        return here not in allowed

    def headers(response: Response, path: str = "") -> Response:
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        if path.startswith(BASE + "/static/") and response.status_code in (200, 304):
            h["Cache-Control"] = "private, no-cache"   # revalidate (ETag -> 304): ES modules import each other unversioned
        else:
            h["Cache-Control"] = "no-store"
        return response

    def not_signed_in(request: Request) -> Response:
        if request.method in ("GET", "HEAD"):
            return RedirectResponse(ENTRANCE, status_code=303)
        return JSONResponse({"error": "Not signed in"}, status_code=401)

    def agent_auth(request: Request, body: bytes) -> str | None:
        peer = request.client.host if request.client else ""
        if peer not in ("127.0.0.1", "::1") or request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip"):
            return None
        if not agent_key:
            return None
        agent_id = request.headers.get("x-agent-id", "")
        ts = request.headers.get("x-agent-time", "")
        nonce = request.headers.get("x-agent-nonce", "")
        sig = request.headers.get("x-agent-signature", "")
        if not (agent_id and len(agent_id) <= 64 and ts.isdigit() and 16 <= len(nonce) <= 64 and len(sig) == 64):
            return None
        if abs(int(ts) - timeutil.now_ms()) > 300_000:
            return None
        msg = "\n".join([request.method, request.url.path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()
        expect = hmac.new(agent_key, msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return None
        with nonce_lock:
            now = time.monotonic()
            while nonces and next(iter(nonces.values())) < now - 600:
                nonces.popitem(last=False)
            if nonce in nonces:
                return None
            nonces[nonce] = now
        return agent_id

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        path = request.url.path
        if path == BASE:
            return headers(RedirectResponse(BASE + "/", status_code=308))
        if not path.startswith(BASE + "/"):
            return headers(PlainTextResponse("Not found", status_code=404))
        if request.method not in ("GET", "HEAD", "POST"):
            return headers(PlainTextResponse("Not allowed", status_code=405))
        if path.startswith(AGENT):
            body = await request.body()
            if len(body) > 8 * 1024 * 1024:
                return headers(PlainTextResponse("Request too large", status_code=413))
            agent_id = agent_auth(request, body)
            if not agent_id:
                return headers(JSONResponse({"error": "unauthorised"}, status_code=401))
            request.state.agent_id = agent_id
            request.state.session = None
            try:
                return headers(await call_next(request))
            except Exception as exc:
                log.error("agent request failed: %s", type(exc).__name__)
                return headers(JSONResponse({"error": "server error"}, status_code=500))
        session = site_auth.session_from_proxy(request) if central_auth else sessions.lookup(request.cookies.get(COOKIE))
        if central_auth and session is None:
            return headers(JSONResponse({"error": "Site authentication required"}, status_code=401))
        request.state.session = session
        public = path == ENTRANCE or (path.startswith(ENTRANCE_ASSETS) and ".." not in path)
        if session is None and not public:
            return headers(not_signed_in(request))
        if central_auth and path == BASE + "/enter":
            if request.method in ("GET", "HEAD"):
                return headers(RedirectResponse(BASE + "/", status_code=303))
            return headers(PlainTextResponse("Use the site login", status_code=405))
        if request.method == "POST":
            body = await request.body()
            if len(body) > 256 * 1024:
                return headers(PlainTextResponse("Request too large", status_code=413))
            if origin_problem(request):
                return headers(PlainTextResponse("Cross-site request blocked", status_code=403))
            if path != ENTRANCE:
                token = request.headers.get("x-csrf-token", "")
                if not token and request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
                    token = (parse_qs(body.decode("utf-8", "replace")).get("csrf_token") or [""])[0]
                if not (token and hmac.compare_digest(token, session["csrf"])):
                    return headers(JSONResponse({"error": "Security token missing or expired. Reload the page."},
                                                status_code=403))
        try:
            response = await call_next(request)
        except Exception as exc:
            log.exception("request failed: %s", type(exc).__name__)
            response = JSONResponse({"error": "Something went wrong on the server."}, status_code=500)
        return headers(response, path)

    # ------------------------------------------------------------ helpers

    def S(conn) -> dict:
        return settings_store.get_all(conn)

    def audit(conn, action: str, detail: dict | None = None) -> None:
        conn.execute("INSERT INTO audit_log (at, action, detail) VALUES (?, ?, ?)",
                     (timeutil.now_iso(), action, json.dumps(detail or {})[:2000]))

    def err(message: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    def data_version(conn) -> str:
        m = conn.execute("SELECT coalesce(max(id), 0) FROM measurements").fetchone()[0]
        f = conn.execute("SELECT coalesce(max(id), 0), count(revoked_at) FROM measurement_flags").fetchone()
        st = conn.execute("SELECT coalesce(max(updated_at), '') FROM settings").fetchone()[0]
        return f"{m}:{f[0]}:{f[1]}:{st}"

    def rng(conn, request: Request, prefix: str = "") -> tuple[int, int, str]:
        q = request.query_params
        s = S(conn)
        preset = q.get(prefix + "range") or s["default_range"]
        if q.get(prefix + "from") and q.get(prefix + "to") and preset in ("custom", "ms"):
            if preset == "ms":
                a, b = int(q[prefix + "from"]), int(q[prefix + "to"])
                if b <= a:
                    raise ValueError("empty range")
                return a, b, f"{timeutil.fmt_local(a, s['timezone'])} to {timeutil.fmt_local(b, s['timezone'])}"
            return timeutil.resolve_range("custom", s["timezone"], q[prefix + "from"], q[prefix + "to"])
        return timeutil.resolve_range(preset, s["timezone"], first_ms=analytics.first_ms(conn, s))

    def rkey(request: Request, prefix: str = "") -> tuple:
        """Cache key for a requested period: presets relative to "now" are reused for 10 minutes (and until new data
        arrives, via data_version); all-time and custom periods until new data arrives."""
        q = request.query_params
        preset = q.get(prefix + "range") or "default"
        bucket = 0 if preset in ("all", "custom", "ms") else timeutil.now_ms() // 600_000
        return (preset, q.get(prefix + "from"), q.get(prefix + "to"), bucket)

    def cached(key, fn):
        hit = cache.get(key)
        if hit is not None:
            return hit
        value = fn()
        cache.put(key, value)
        return value

    def agent_status(conn) -> dict | None:
        row = conn.execute("SELECT agent_id, last_seen_ms, status FROM agent_status ORDER BY last_seen_ms DESC LIMIT 1").fetchone()
        if not row:
            return None
        st = json.loads(row["status"])
        st["agent_id"], st["last_seen_ms"] = row["agent_id"], row["last_seen_ms"]
        st["age_s"] = (timeutil.now_ms() - row["last_seen_ms"]) / 1000
        st["online"] = st["age_s"] < 180
        return st

    def public_test(r) -> dict:
        d = dict(r)
        d.pop("raw", None)
        return d

    # ------------------------------------------------------------ entrance

    @app.get(ENTRANCE)
    def enter_page(request: Request):
        if request.state.session:
            return RedirectResponse(BASE + "/", status_code=303)
        return templates.TemplateResponse(request, "enter.html", {"miss": "miss" in request.query_params})

    @app.post(ENTRANCE)
    async def enter(request: Request):
        form = await request.form()
        phrase = form.get("phrase") if isinstance(form.get("phrase"), str) else ""
        ip = client_ip(request)
        blocked = limiter.blocked(ip)
        good = auth.verify_phrase(phrase, auth.load_phrase_hash(settings.phrase_file))
        if good and not blocked:
            limiter.reset(ip)
            token, _ = sessions.create()
            with db.tx() as conn:
                audit(conn, "signed_in")
            resp = RedirectResponse(BASE + "/", status_code=303)
            resp.set_cookie(COOKIE, token, httponly=True, secure=settings.secure_cookies, samesite="strict",
                            path=BASE, max_age=settings.session_absolute_hours * 3600)
            return resp
        if not good:
            limiter.fail(ip)
        return RedirectResponse(ENTRANCE + "?miss", status_code=303)

    @app.post(BASE + "/leave")
    def leave(request: Request):
        if central_auth:
            return RedirectResponse("/logout", status_code=303)
        sessions.end(request.cookies.get(COOKIE))
        resp = RedirectResponse(ENTRANCE, status_code=303)
        resp.delete_cookie(COOKIE, path=BASE)
        return resp

    # ------------------------------------------------------------ shell

    def shell(request: Request):
        with db.read() as conn:
            synthetic = S(conn).get("data_origin") == "synthetic"
        return templates.TemplateResponse(request, "app.html", {"csrf": request.state.session["csrf"],
                                                                "synthetic": synthetic})

    for route in SPA_ROUTES:
        app.add_api_route(BASE + route, shell, methods=["GET"])
    app.add_api_route(BASE + "/day/{day}", shell, methods=["GET"])
    app.add_api_route(BASE + "/test/{uid}", shell, methods=["GET"])

    # ------------------------------------------------------------ API: overview

    @app.get(BASE + "/api/bootstrap")
    def bootstrap(request: Request):
        with db.read() as conn:
            s = S(conn)
            return {"version": VERSION, "env": settings.env, "settings": {k: s[k] for k in s if k != "data_origin"},
                    "synthetic": s.get("data_origin") == "synthetic", "bounds": analytics.data_bounds(conn, s),
                    "fields": settings_store.describe(), "hard": settings_store.HARD, "now_ms": timeutil.now_ms(),
                    "annotation_categories": ANNOTATION_CATEGORIES}

    @app.get(BASE + "/api/live")
    def live_view(request: Request):
        return live_payload()

    def live_payload() -> dict:
        with db.read() as conn:
            s = S(conn)
            now = timeutil.now_ms()
            last = conn.execute("SELECT * FROM measurements WHERE source IN ('agent','synthetic') ORDER BY t_ms DESC LIMIT 1").fetchone()
            last_ok = conn.execute("SELECT * FROM measurements WHERE source IN ('agent','synthetic') AND status = 'ok' "
                                   "ORDER BY t_ms DESC LIMIT 1").fetchone()
            recent = conn.execute("SELECT t_ms, status, download_mbps, upload_mbps, ping_ms FROM measurements WHERE "
                                  "source IN ('agent','synthetic') ORDER BY t_ms DESC LIMIT 12").fetchall()
            pulse = conn.execute("SELECT t_ms, ext_rtt_ms, gw_rtt_ms, ext_loss_pct, state FROM probes WHERE t_ms >= ? "
                                 "ORDER BY t_ms", (now - 3 * timeutil.HOUR_MS,)).fetchall()
            mv = data_version(conn)

            def averages():
                tz = s["timezone"]
                out = {}
                for key in ("today", "24h", "7d", "30d"):
                    a, b, _ = timeutil.resolve_range(key, tz, now=now)
                    tests = analytics.basis(analytics.load_tests(conn, a, b, s))
                    oks = [x for x in tests if x.ok]
                    dl = [x.dl for x in oks]
                    ul = [x.ul for x in oks if x.ul is not None]
                    out[key] = {"n": len(tests), "n_ok": len(dl), "dl": (sum(dl) / len(dl)) if dl else None,
                                "ul": (sum(ul) / len(ul)) if ul else None,
                                "pct_target": analytics.share(sum(1 for x in oks if x.dl >= x.pk["target_down"]), len(oks))}
                return out

            avgs = cached(("averages", mv, now // 60000), averages)
            status = agent_status(conn)
            last_probe = pulse[-1] if pulse else None
            pkc_live = settings_store.PackageClock(s["packages"], s["timezone"])
            qlast = quality_codes(conn, [dict(x) for x in (last, last_ok) if x], pkc_live)
        rolling = [r["download_mbps"] for r in recent if r["status"] == "ok"][:5]
        health = "unknown"
        if status and not status["online"]:
            health = "monitor_offline"
        elif last_probe and now - last_probe["t_ms"] < 5 * 60_000:
            health = {"ok": "ok", "degraded": "latency", "wan_down": "outage", "dns_fail": "outage",
                      "lan_fault": "local", "link_down": "local"}.get(last_probe["state"], "ok")
            last_pk = settings_store.PackageClock(s["packages"], s["timezone"]).at(last["t_ms"]) if last else s
            if health == "ok" and last and last["status"] == "ok" and last["download_mbps"] < last_pk["degraded_down"] \
                    and now - last["t_ms"] < 2 * 3600_000:
                health = "degraded"
        elif status:
            health = "stale"
        def judged(r):
            if not r:
                return None
            d = public_test(r)
            pk = settings_store.PackageClock(s["packages"], s["timezone"]).at(r["t_ms"])
            d["package"] = {k: pk[k] for k in ("name", "advertised_down", "advertised_up", "target_down", "degraded_down",
                                               "severe_down", "target_up")}
            if r["status"] == "ok":
                v = r["download_mbps"]
                d["band"] = ("target" if v >= pk["target_down"] else "severe" if v < pk["severe_down"]
                             else "degraded" if v < pk["degraded_down"] else "below")
                d["pct_of_advertised"] = 100 * v / pk["advertised_down"]
            codes = qlast.get(r["uuid"], ())
            d["quality"] = {"codes": list(codes), "clean": quality.is_clean(codes), "labels": quality.labels(codes)}
            return d

        link_now = ((status or {}).get("link") or {}).get("speed") or (last["link_speed_mbps"] if last else None)

        return {"now_ms": now, "last": judged(last), "package": s["package"],
                "capacity": quality.capacity(link_now, s["package"]),
                "last_ok": judged(last_ok),
                "recent": [dict(r) for r in recent], "rolling5": (sum(rolling) / len(rolling)) if rolling else None,
                "averages": avgs, "agent": status, "pulse": [list(r) for r in pulse], "health": health}

    @app.get(BASE + "/api/summary")
    def summary_view(request: Request):
        try:
            with db.read() as conn:
                a, b, label = rng(conn, request)
                s = S(conn)
                key = ("summary", rkey(request), data_version(conn))

                def build():
                    tests = analytics.load_tests(conn, a, b, s)
                    bt = analytics.basis(tests)
                    sm = analytics.summary(tests, s)
                    sm["excluded"] = analytics.excluded_count(conn, a, b, s)
                    return {"range": {"from_ms": a, "to_ms": b, "label": label}, "summary": sm,
                            "time": analytics.time_analysis(tests, s),
                            "coverage": analytics.coverage(conn, a, b, s, tests),
                            "degradations": analytics.degradation_incidents(tests, s)[-50:],
                            "outages": analytics.outage_incidents(conn, a, b)[-50:],
                            "histogram": analytics.histogram([x.dl for x in bt if x.ok],
                                                             analytics.bin_width(sm["package"]["advertised_down"]),
                                                             sm["package"]["advertised_down"]),
                            "histogram_up": analytics.histogram([x.ul for x in bt if x.ok and x.ul is not None],
                                                                analytics.bin_width(sm["package"]["advertised_up"]),
                                                                sm["package"]["advertised_up"]),
                            "hourly": analytics.hourly(bt, s), "weekday": analytics.weekday(bt, s),
                            "guarantee": analytics.guarantee_watch(tests)}
                return cached(key, build)
        except ValueError as exc:
            return err(str(exc))

    @app.get(BASE + "/api/series")
    def series_view(request: Request):
        q = request.query_params
        try:
            a, b = int(q["from"]), int(q["to"])
        except (KeyError, ValueError):
            return err("from and to (ms) are required")
        with db.read() as conn:
            s = S(conn)
            legacy = q.get("legacy", "1" if s["include_legacy"] else "0") == "1"
            g = max(60_000, (b - a) // 400)                       # quantise so pans/re-requests hit the cache
            px = int(q.get("px", 1200))
            out = dict(cached(("series", a // g, b // g, px, legacy, data_version(conn)),
                              lambda: series.tests(conn, a, b, px, legacy, s)))
            out["annotations"] = [dict(r) for r in conn.execute(
                "SELECT uuid, start_ms, end_ms, title, category FROM annotations WHERE deleted_at IS NULL AND "
                "start_ms < ? AND coalesce(end_ms, start_ms) >= ? ORDER BY start_ms", (b, a))]
            return out

    @app.get(BASE + "/api/latency")
    def latency_view(request: Request):
        q = request.query_params
        try:
            a, b = int(q["from"]), int(q["to"])
        except (KeyError, ValueError):
            return err("from and to (ms) are required")
        with db.read() as conn:
            return series.probes(conn, a, b, int(q.get("px", 1200)))

    @app.get(BASE + "/api/timeline")
    def timeline_view(request: Request):
        try:
            with db.read() as conn:
                a, b, label = rng(conn, request)
                st = agent_status(conn)
                out = series.timeline(conn, a, b, st["last_seen_ms"] if st else None)
                s = S(conn)
                tests = analytics.load_tests(conn, a, b, s)
                out["tests"] = [[x.t, analytics.band(x, s)] for x in tests if analytics.band(x, s) != "target"][-5000:]
                out["range"] = {"from_ms": a, "to_ms": b, "label": label}
                out["outages"] = analytics.outage_incidents(conn, a, b)[-200:]
                out["degradations"] = analytics.degradation_incidents(tests, s)[-200:]
                return out
        except ValueError as exc:
            return err(str(exc))

    def tests_endpoint(name, fn):
        def view(request: Request):
            try:
                with db.read() as conn:
                    a, b, label = rng(conn, request)
                    s = S(conn)
                    key = (name, rkey(request), data_version(conn))
                    return cached(key, lambda: {"range": {"from_ms": a, "to_ms": b, "label": label},
                                                **fn(conn, analytics.load_tests(conn, a, b, s), s, a, b)})
            except ValueError as exc:
                return err(str(exc))
        app.add_api_route(BASE + "/api/" + name, view, methods=["GET"])

    def outage_minutes_by_day(conn, a, b, s):
        out: dict = {}
        clock = timeutil.LocalClock(s["timezone"])
        for t, iv in conn.execute("SELECT t_ms, interval_s FROM probes WHERE t_ms >= ? AND t_ms < ? AND state IN "
                                  "('wan_down','dns_fail')", (a, b)):
            d = clock.parts(t)[0]
            out[d] = out.get(d, 0) + iv / 60
        return out

    tests_endpoint("heatmap", lambda conn, t, s, a, b: analytics.heatmap(t, s))
    tests_endpoint("calendar", lambda conn, t, s, a, b: {"days": analytics.calendar(t, s, outage_minutes_by_day(conn, a, b, s))})
    tests_endpoint("dna", lambda conn, t, s, a, b: analytics.dna(t, s))
    tests_endpoint("congestion", lambda conn, t, s, a, b: analytics.congestion(t, s))
    tests_endpoint("daily", lambda conn, t, s, a, b: {"days": analytics.daily(analytics.basis(t), s)})

    @app.get(BASE + "/api/anomalies")
    def anomalies(request: Request):
        try:
            with db.read() as conn:
                a, b, _ = rng(conn, request)
                rows = conn.execute("SELECT m.uuid, m.t_ms, m.download_mbps, m.upload_mbps, d.anomaly_z, d.ref_median, d.ref_n "
                                    "FROM measurements m JOIN derived_measurement d ON d.uuid = m.uuid WHERE d.anomaly = 1 "
                                    "AND m.t_ms >= ? AND m.t_ms < ? ORDER BY m.t_ms DESC LIMIT 500", (a, b)).fetchall()
                return {"items": [dict(r) for r in rows], "z": ingest.ANOMALY_Z, "window_days": ingest.ANOMALY_WINDOW_DAYS,
                        "min_ref": ingest.ANOMALY_MIN_REF}
        except ValueError as exc:
            return err(str(exc))

    @app.get(BASE + "/api/compare")
    def compare_view(request: Request):
        try:
            with db.read() as conn:
                s = S(conn)
                a1, b1, la = rng(conn, request, "a_")
                a2, b2, lb = rng(conn, request, "b_")
                key = ("compare", rkey(request, "a_"), rkey(request, "b_"), data_version(conn))
                first = analytics.data_bounds(conn, s)["first_ms"]
                return cached(key, lambda: {"a_range": {"from_ms": a1, "to_ms": b1, "label": la}, "data_from_ms": first,
                                            "b_range": {"from_ms": a2, "to_ms": b2, "label": lb},
                                            **analytics.compare(analytics.load_tests(conn, a1, b1, s),
                                                                analytics.load_tests(conn, a2, b2, s), s)})
        except ValueError as exc:
            return err(str(exc))

    @app.get(BASE + "/api/evidence")
    def evidence(request: Request):
        try:
            with db.read() as conn:
                a, b, label = rng(conn, request)
                s = S(conn)
                key = ("evidence", rkey(request), data_version(conn))
                return cached(key, lambda: export_jobs.report_data(conn, s, a, b, label))
        except ValueError as exc:
            return err(str(exc))

    # ------------------------------------------------------------ API: explorer

    SORTS = {"time": "m.t_ms", "download": "m.download_mbps", "upload": "m.upload_mbps", "ping": "m.ping_ms",
             "jitter": "m.jitter_ms", "loss": "m.packet_loss_pct", "server": "m.server_name", "anomaly": "abs(d.anomaly_z)"}

    def explorer_where(conn, q) -> tuple[str, list]:
        s = S(conn)
        where, args = [], []
        a, b, _ = rng(conn, type("R", (), {"query_params": q})())
        where.append("m.t_ms >= ? AND m.t_ms < ?")
        args += [a, b]
        for col, lo, hi in (("m.download_mbps", "dl_min", "dl_max"), ("m.upload_mbps", "ul_min", "ul_max"),
                            ("m.ping_ms", "ping_min", "ping_max")):
            if q.get(lo):
                where.append(f"{col} >= ?")
                args.append(float(q[lo]))
            if q.get(hi):
                where.append(f"{col} <= ?")
                args.append(float(q[hi]))
        st = q.get("status")
        if st in ("ok", "failed"):
            where.append("m.status = ?")
            args.append(st)
        if q.get("server"):
            where.append("m.server_id = ?")
            args.append(q["server"])
        if q.get("kind"):
            where.append("m.kind = ?")
            args.append(q["kind"])
        src = q.get("source", "agent")
        if src == "agent":
            where.append("m.source IN ('agent','synthetic')")
        elif src == "legacy":
            where.append("m.source LIKE 'legacy%'")
        flt = q.get("filter")
        if flt == "anomaly":
            where.append("d.anomaly = 1")
        elif flt in ("below_target", "degraded", "severe"):
            expr, a2 = settings_store.sql_case(s, {"below_target": "target_down", "degraded": "degraded_down",
                                                   "severe": "severe_down"}[flt])
            where.append(f"m.status = 'ok' AND m.download_mbps < {expr}")
            args += a2
        elif flt == "flagged":
            where.append("EXISTS (SELECT 1 FROM measurement_flags f WHERE f.measurement_uuid = m.uuid AND f.revoked_at IS NULL)")
        elif flt in ("clean", "quality_flagged"):                  # measurement quality (speedmon/quality.py)
            quality.register_sql(conn, settings_store.PackageClock(s["packages"], s["timezone"]))
            where.append(f"lm_quality(m.status, m.source, m.kind, m.download_mbps, m.t_ms, "
                         f"{', '.join(quality.sql_columns('m', 'dq'))}) = ?")
            args.append("clean" if flt == "clean" else "flagged")
        return " AND ".join(where), args

    DQ_JOIN = "LEFT JOIN diagnostics dq ON dq.id = (SELECT id FROM diagnostics WHERE measurement_uuid = m.uuid LIMIT 1) "

    def quality_codes(conn, rows: list[dict], pkc) -> dict:
        """uuid -> quality codes for a page of measurement rows (one query for the page's diagnostics)."""
        ids = [r["uuid"] for r in rows]
        if not ids:
            return {}
        diag = {r[0]: tuple(r[1:]) for r in conn.execute(
            f"SELECT m.uuid, {', '.join(quality.SQL_COLUMNS)} FROM measurements m LEFT JOIN diagnostics d ON "
            f"d.measurement_uuid = m.uuid WHERE m.uuid IN ({','.join('?' * len(ids))})", ids)}
        return {r["uuid"]: quality.assess(r["status"] == "ok", r["source"], r["kind"], r["download_mbps"],
                                          pkc.at(r["t_ms"]), diag.get(r["uuid"])) for r in rows}

    @app.get(BASE + "/api/measurements")
    def measurements(request: Request):
        q = request.query_params
        try:
            with db.read() as conn:
                where, args = explorer_where(conn, q)
                sort = SORTS.get(q.get("sort", "time"), "m.t_ms")
                direction = "ASC" if q.get("dir") == "asc" else "DESC"
                size = max(10, min(int(q.get("size", 100)), 500))
                offset = max(0, int(q.get("offset", 0)))
                join = "LEFT JOIN derived_measurement d ON d.uuid = m.uuid " if ("d." in where or "d." in sort) else ""
                if "dq." in where:
                    join += DQ_JOIN
                total = conn.execute(f"SELECT count(*) FROM measurements m {join}WHERE {where}", args).fetchone()[0]
                # Page over ids first (answered from a covering index), then fetch only the rows shown.
                cover = {"download": "measurements_dl_cov", "upload": "measurements_ul_cov", "ping": "measurements_ping_cov"}
                hint = f"INDEXED BY {cover[q.get('sort')]} " if q.get("sort") in cover and not join and total > 20_000 else ""
                inner = (f"SELECT m.id FROM measurements m {hint}{join}WHERE {where} "
                         f"ORDER BY {sort} {direction}, m.id {direction} LIMIT ? OFFSET ?")
                rows = conn.execute(
                    f"SELECT m.uuid, m.t_ms, m.kind, m.source, m.engine, m.status, m.failure_code, m.download_mbps, "
                    f"m.upload_mbps, m.ping_ms, m.jitter_ms, m.packet_loss_pct, m.server_id, m.server_name, "
                    f"m.server_location, m.duration_ms, d.anomaly, d.anomaly_z, "
                    f"(SELECT group_concat(f.flag) FROM measurement_flags f WHERE f.measurement_uuid = m.uuid AND "
                    f" f.revoked_at IS NULL) AS flags "
                    f"FROM measurements m LEFT JOIN derived_measurement d ON d.uuid = m.uuid WHERE m.id IN ({inner}) "
                    f"ORDER BY {sort} {direction}, m.id {direction}", args + [size, offset]).fetchall()
                servers = [dict(r) for r in conn.execute(
                    "SELECT server_id, server_name, server_location, count(*) AS n FROM measurements WHERE server_id IS NOT "
                    "NULL GROUP BY server_id ORDER BY n DESC LIMIT 60")]
                pkc = settings_store.PackageClock(S(conn)["packages"], S(conn)["timezone"])
                lat_poor = S(conn)["latency_degraded_ms"]
                qc = quality_codes(conn, [dict(r) for r in rows], pkc)
                items = []
                for r in rows:
                    d = dict(r)
                    d["quality"] = list(qc.get(d["uuid"], ()))
                    if d["status"] == "ok" and d["download_mbps"] is not None:
                        pk = pkc.at(d["t_ms"])
                        v = d["download_mbps"]
                        d["band"] = ("target" if v >= pk["target_down"] else "severe" if v < pk["severe_down"]
                                     else "degraded" if v < pk["degraded_down"] else "below")
                        u = d["upload_mbps"]
                        if u is not None and pk.get("target_up"):
                            d["band_up"] = ("target" if u >= pk["target_up"]
                                            else "degraded" if u < (pk.get("degraded_up") or 0) else "below")
                    if d.get("ping_ms") is not None and d["ping_ms"] > lat_poor:
                        d["ping_high"] = True
                    items.append(d)
                return {"total": total, "offset": offset, "size": size, "items": items, "servers": servers,
                        "quality_labels": {k: {"label": v[0], "affects_clean": v[1], "why": v[2]} for k, v in quality.FLAGS.items()}}
        except ValueError as exc:
            return err(str(exc))

    def ip_fingerprint(ip: str | None) -> str | None:
        if not ip:
            return None
        salt = (agent_key or b"speedmon")
        return hmac.new(salt, ip.encode(), hashlib.sha256).hexdigest()[:8]

    @app.get(BASE + "/api/measurement/{uid}")
    def measurement(uid: str):
        with db.read() as conn:
            row = conn.execute("SELECT * FROM measurements WHERE uuid = ?", (uid,)).fetchone()
            if not row:
                return err("Not found", 404)
            m = dict(row)
            raw = m.pop("raw", None)
            try:
                m["raw"] = json.loads(raw) if raw else None
            except ValueError:
                m["raw"] = raw
            diag = conn.execute("SELECT * FROM diagnostics WHERE measurement_uuid = ?", (uid,)).fetchone()
            d = dict(diag) if diag else None
            if d:
                d["public_ip_fingerprint"] = ip_fingerprint(d.pop("public_ip", None))
                try:
                    d["extra"] = json.loads(d["extra"]) if d.get("extra") else None
                except ValueError:
                    pass
            ledger = conn.execute("SELECT chain_index, record_hash, chain_hash, recorded_at FROM ledger WHERE stream = "
                                  "'measurement' AND uuid = ?", (uid,)).fetchone()
            flags = [dict(r) for r in conn.execute("SELECT * FROM measurement_flags WHERE measurement_uuid = ? ORDER BY id", (uid,))]
            derived = conn.execute("SELECT * FROM derived_measurement WHERE uuid = ?", (uid,)).fetchone()
            near = conn.execute("SELECT t_ms, state, ext_rtt_ms, gw_rtt_ms, ext_loss_pct FROM probes WHERE t_ms >= ? AND t_ms <= ? "
                                "ORDER BY t_ms", (m["t_ms"] - 15 * 60_000, m["t_ms"] + 15 * 60_000)).fetchall()
            related = [dict(r) for r in conn.execute(
                "SELECT uuid, t_ms, kind, status, download_mbps FROM measurements WHERE related_uuid = ? OR uuid = ?",
                (uid, m.get("related_uuid") or ""))]
            calc = canonical.record_hash("measurement", m)
            s = S(conn)
            pk = settings_store.PackageClock(s["packages"], s["timezone"]).at(m["t_ms"])
            codes = quality_codes(conn, [m], settings_store.PackageClock(s["packages"], s["timezone"]))[m["uuid"]]
            qual = {"codes": list(codes), "clean": quality.is_clean(codes), "labels": quality.labels(codes),
                    "assessed": m["status"] == "ok" and m["source"] in ("agent", "synthetic"),
                    "capacity": quality.capacity(m.get("link_speed_mbps") or (d or {}).get("link_speed_mbps"), pk)}
            return {"measurement": m, "diagnostics": d, "quality": qual, "ledger": dict(ledger) if ledger else None,
                    "hash_ok": calc == m["record_hash"], "flags": flags, "derived": dict(derived) if derived else None,
                    "probes": [list(r) for r in near], "related": related}

    @app.post(BASE + "/api/measurement/{uid}/flag")
    async def add_flag(uid: str, request: Request):
        body = await request.json()
        flag, reason = body.get("flag"), (body.get("reason") or "").strip()
        if flag not in FLAG_KINDS or not 3 <= len(reason) <= 1000:
            return err("Choose a flag and give a reason (3-1000 characters)")
        with db.tx() as conn:
            if not conn.execute("SELECT 1 FROM measurements WHERE uuid = ?", (uid,)).fetchone():
                return err("Not found", 404)
            conn.execute("INSERT INTO measurement_flags (measurement_uuid, flag, reason, created_at) VALUES (?, ?, ?, ?)",
                         (uid, flag, reason, timeutil.now_iso()))
            audit(conn, "flag_added", {"uuid": uid, "flag": flag})
        return {"ok": True}

    @app.post(BASE + "/api/flag/{fid}/revoke")
    async def revoke_flag(fid: int, request: Request):
        body = await request.json()
        reason = (body.get("reason") or "").strip()
        if not 3 <= len(reason) <= 1000:
            return err("Give a reason (3-1000 characters)")
        with db.tx() as conn:
            n = conn.execute("UPDATE measurement_flags SET revoked_at = ?, revoked_reason = ? WHERE id = ? AND revoked_at IS NULL",
                             (timeutil.now_iso(), reason, fid)).rowcount
            audit(conn, "flag_revoked", {"id": fid})
        return {"ok": bool(n)}

    # ------------------------------------------------------------ API: annotations

    @app.get(BASE + "/api/annotations")
    def annotations(request: Request):
        q = request.query_params
        with db.read() as conn:
            if q.get("from") and q.get("to"):
                rows = conn.execute("SELECT * FROM annotations WHERE deleted_at IS NULL AND start_ms < ? AND "
                                    "coalesce(end_ms, start_ms) >= ? ORDER BY start_ms", (int(q["to"]), int(q["from"])))
            else:
                rows = conn.execute("SELECT * FROM annotations WHERE deleted_at IS NULL ORDER BY start_ms DESC")
            return {"items": [dict(r) for r in rows]}

    def annotation_from(body: dict, tz: str) -> dict:
        title = (body.get("title") or "").strip()
        if not 1 <= len(title) <= 120:
            raise ValueError("Title: 1-120 characters")
        text = (body.get("body") or "").strip()
        if len(text) > 4000:
            raise ValueError("Notes: at most 4000 characters")
        cat = body.get("category") or "note"
        if cat not in ANNOTATION_CATEGORIES:
            raise ValueError("Unknown category")
        start = timeutil.local_to_ms(str(body.get("start")), tz)
        end = timeutil.local_to_ms(str(body["end"]), tz) if body.get("end") else None
        if end is not None and end < start:
            raise ValueError("The end must be after the start")
        return {"title": title, "body": text, "category": cat, "start_ms": start, "end_ms": end}

    @app.post(BASE + "/api/annotations")
    async def create_annotation(request: Request):
        body = await request.json()
        try:
            with db.tx() as conn:
                a = annotation_from(body, S(conn)["timezone"])
                now = timeutil.now_iso()
                uid = str(uuidlib.uuid4())
                cur = conn.execute("INSERT INTO annotations (uuid, start_ms, end_ms, title, body, category, created_at, updated_at) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   (uid, a["start_ms"], a["end_ms"], a["title"], a["body"], a["category"], now, now))
                conn.execute("INSERT INTO annotation_history (annotation_id, at, action, snapshot) VALUES (?, ?, 'create', ?)",
                             (cur.lastrowid, now, json.dumps(a)))
            return {"ok": True, "uuid": uid}
        except ValueError as exc:
            return err(str(exc))

    @app.post(BASE + "/api/annotations/{uid}")
    async def update_annotation(uid: str, request: Request):
        body = await request.json()
        try:
            with db.tx() as conn:
                row = conn.execute("SELECT id FROM annotations WHERE uuid = ? AND deleted_at IS NULL", (uid,)).fetchone()
                if not row:
                    return err("Not found", 404)
                now = timeutil.now_iso()
                if body.get("delete"):
                    conn.execute("UPDATE annotations SET deleted_at = ? WHERE id = ?", (now, row["id"]))
                    conn.execute("INSERT INTO annotation_history (annotation_id, at, action, snapshot) VALUES (?, ?, 'delete', '{}')",
                                 (row["id"], now))
                    return {"ok": True}
                a = annotation_from(body, S(conn)["timezone"])
                conn.execute("UPDATE annotations SET start_ms = ?, end_ms = ?, title = ?, body = ?, category = ?, updated_at = ? "
                             "WHERE id = ?", (a["start_ms"], a["end_ms"], a["title"], a["body"], a["category"], now, row["id"]))
                conn.execute("INSERT INTO annotation_history (annotation_id, at, action, snapshot) VALUES (?, ?, 'update', ?)",
                             (row["id"], now, json.dumps(a)))
            return {"ok": True}
        except ValueError as exc:
            return err(str(exc))

    # ------------------------------------------------------------ API: settings and control

    @app.get(BASE + "/api/settings")
    def get_settings(request: Request):
        with db.read() as conn:
            s = S(conn)
            hist = [dict(r) for r in conn.execute("SELECT key, old_value, new_value, changed_at, source FROM settings_history "
                                                  "ORDER BY id DESC LIMIT 50")]
            sizes = conn.execute("SELECT avg(coalesce(download_bytes, 0) + coalesce(upload_bytes, 0)) FROM (SELECT * FROM "
                                 "measurements WHERE status = 'ok' AND source = 'agent' ORDER BY t_ms DESC LIMIT 50)").fetchone()[0]
            return {"settings": {k: s[k] for k in s if k != "data_origin"}, "fields": settings_store.describe(),
                    "hard": settings_store.HARD, "history": hist, "avg_test_bytes": sizes,
                    "agent": agent_status(conn)}

    @app.post(BASE + "/api/settings")
    async def post_settings(request: Request):
        body = await request.json()
        changes = body.get("changes") or {}
        try:
            with db.tx() as conn:
                clean = settings_store.validate(changes, S(conn))
                notes = settings_store.danger_notes(clean)
                if notes and not body.get("confirm"):
                    return JSONResponse({"needs_confirmation": notes}, status_code=409)
                settings_store.save(conn, clean)
                audit(conn, "settings_changed", {"keys": sorted(clean)})
            return {"ok": True, "saved": clean}
        except ValueError as exc:
            return err(str(exc))

    @app.post(BASE + "/api/run-test")
    def run_test(request: Request):
        now = time.time()
        recent = [t for t in manual_runs if now - t < 86400]
        manual_runs[:] = recent
        if recent and now - recent[-1] < 180:
            return err("A manual test was requested less than 3 minutes ago.", 429)
        if len(recent) >= 24:
            return err("At most 24 manual tests a day.", 429)
        with db.tx() as conn:
            conn.execute("UPDATE commands SET status = 'expired' WHERE status = 'pending'")
            conn.execute("INSERT INTO commands (created_at, kind, payload) VALUES (?, 'run_test', '{}')", (timeutil.now_iso(),))
            audit(conn, "manual_test_requested")
        manual_runs.append(now)
        return {"ok": True}

    @app.post(BASE + "/api/pause")
    async def pause(request: Request):
        body = await request.json()
        hours = body.get("hours")
        with db.tx() as conn:
            if hours in (None, 0, "0"):
                settings_store.save(conn, {"paused_until": None}, "pause")
            else:
                h = float(hours)
                if not 0 < h <= 24 * 30:
                    return err("Pause for 0-720 hours")
                settings_store.save(conn, {"paused_until": timeutil.iso(timeutil.now_ms() + int(h * 3600_000))}, "pause")
            audit(conn, "pause", {"hours": hours})
        return {"ok": True}

    # ------------------------------------------------------------ API: integrity and exports

    verify_state = {"running": False, "result": None}

    @app.get(BASE + "/api/integrity")
    def integrity_view(request: Request):
        with db.read() as conn:
            st = agent_status(conn)
            return {"head": integrity.head(conn), "last": verify_state["result"], "running": verify_state["running"],
                    "epoch": S(conn).get("epoch"),
                    "sequence": integrity.sequence_gaps(conn),
                    "agent_queue": {"depth": st.get("queue_depth"), "oldest_ms": st.get("oldest_unsent_ms"),
                                    "rejected": st.get("rejected_local")} if st else None,
                    "quarantine": [dict(r) for r in conn.execute(
                        "SELECT id, received_at, stream, uuid, error FROM quarantine ORDER BY id DESC LIMIT 50")],
                    "imports": [dict(r) for r in conn.execute("SELECT * FROM imports ORDER BY id")],
                    "exports": [dict(r) for r in conn.execute(
                        "SELECT uuid, created_at, format, range_label, rows, file_sha256, ledger_head FROM exports "
                        "WHERE status = 'done' ORDER BY id DESC LIMIT 20")]}

    @app.post(BASE + "/api/integrity/verify")
    def run_verify(request: Request):
        if verify_state["running"]:
            return {"ok": True, "running": True}
        verify_state["running"] = True

        def work():
            try:
                with db.read() as conn:
                    verify_state["result"] = integrity.verify(conn)
                with db.tx() as conn:
                    audit(conn, "integrity_verified", {"ok": verify_state["result"]["ok"],
                                                       "records": verify_state["result"]["records"]})
            finally:
                verify_state["running"] = False
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "running": True}

    @app.get(BASE + "/api/exports")
    def list_exports(request: Request):
        with db.read() as conn:
            return {"items": [dict(r) for r in conn.execute(
                "SELECT uuid, created_at, finished_at, format, range_label, status, progress, rows, file_name, file_bytes, "
                "file_sha256, ledger_head, error FROM exports ORDER BY id DESC LIMIT 30")]}

    @app.post(BASE + "/api/exports")
    async def create_export(request: Request):
        body = await request.json()
        try:
            with db.tx() as conn:
                s = S(conn)
                preset = body.get("range", "all")
                first = analytics.data_bounds(conn, {**s, "include_legacy": True})["first_ms"]
                a, b, label = timeutil.resolve_range(preset, s["timezone"], body.get("from"), body.get("to"), first_ms=first)
                params = export_jobs.validate_params(body)
                uid = exporter.queue(conn, params["format"], label, a, b, params)
                audit(conn, "export_requested", {"uuid": uid, "format": params["format"], "range": label})
            exporter.kick()
            return {"ok": True, "uuid": uid}
        except ValueError as exc:
            return err(str(exc))

    @app.get(BASE + "/api/exports/{uid}")
    def export_status(uid: str):
        with db.read() as conn:
            row = conn.execute("SELECT uuid, created_at, finished_at, format, range_label, status, progress, rows, file_name, "
                               "file_bytes, file_sha256, ledger_head, error FROM exports WHERE uuid = ?", (uid,)).fetchone()
            return dict(row) if row else err("Not found", 404)

    @app.get(BASE + "/exports/{uid}/download")
    def export_download(uid: str):
        with db.read() as conn:
            row = conn.execute("SELECT * FROM exports WHERE uuid = ? AND status = 'done'", (uid,)).fetchone()
        if not row:
            return err("Not found", 404)
        path = exporter.path_for(row["uuid"], row["file_name"])
        if not path.exists():
            return err("This export has expired. Create it again.", 410)
        media = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv",
                 "csv.gz": "application/gzip", "zip": "application/zip"}[row["format"]]
        return FileResponse(path, media_type=media, filename=row["file_name"])

    @app.get(BASE + "/api/audit")
    def audit_view(request: Request):
        with db.read() as conn:
            return {"items": [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")]}

    # ------------------------------------------------------------ agent API (HMAC, loopback)

    @app.post(AGENT + "v1/records")
    async def agent_records(request: Request):
        body = json.loads(await request.body() or b"{}")
        items = body.get("items")
        if not isinstance(items, list) or len(items) > 1000:
            return err("items: list of up to 1000")
        with db.tx() as conn:
            res = ingest.ingest_batch(conn, request.state.agent_id, items, allow_synthetic=not settings.is_production)
            if any(isinstance(i, dict) and isinstance(i.get("record"), dict) and i["record"].get("source") == "synthetic"
                   for i in items) and res["accepted"]:
                settings_store.set_internal(conn, "data_origin", "synthetic")
        return res

    @app.post(AGENT + "v1/status")
    async def agent_status_post(request: Request):
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            return err("object expected")
        text = json.dumps(body)[:20000]
        now = timeutil.now_ms()
        with db.tx() as conn:
            conn.execute("INSERT INTO agent_status (agent_id, last_seen_ms, status) VALUES (?, ?, ?) ON CONFLICT(agent_id) "
                         "DO UPDATE SET last_seen_ms = excluded.last_seen_ms, status = excluded.status",
                         (request.state.agent_id, now, text))
        live["status"], live["received_ms"] = body, now
        return {"ok": True}

    @app.get(AGENT + "v1/config")
    def agent_config(request: Request):
        with db.tx() as conn:
            s = S(conn)
            cmds = [dict(r) for r in conn.execute("SELECT id, kind, payload, created_at FROM commands WHERE status = 'pending' "
                                                  "ORDER BY id")]
            stale = timeutil.iso(timeutil.now_ms() - 15 * 60_000)
            conn.execute("UPDATE commands SET status = 'expired' WHERE status = 'pending' AND created_at < ?", (stale,))
            fresh = [c for c in cmds if c["created_at"] >= stale]
            for c in cmds:
                conn.execute("UPDATE commands SET status = 'sent', sent_at = ? WHERE id = ? AND status = 'pending'",
                             (timeutil.now_iso(), c["id"]))
        return {"config": settings_store.agent_config(s), "commands": fresh, "server_ms": timeutil.now_ms(),
                "version": VERSION}

    def status_document() -> dict:
        now = timeutil.now_ms()
        with db.read() as conn:
            s = S(conn)

            def guarantee():
                return analytics.guarantee_watch(analytics.load_tests(conn, now - 35 * timeutil.DAY_MS, now, s,
                                                                      include_legacy=False))

            g = cached(("status-guarantee", data_version(conn), now // 600_000), guarantee)
        return statusfile.build(live_payload(), g, now, settings.public_origins[0] + BASE + "/")

    def status_writer():
        while True:
            try:
                statusfile.write(settings.status_file, status_document())
            except Exception as exc:                     # never let the side file disturb the service
                log.warning("status file not written: %s", exc)
            if stop_status.wait(statusfile.STATUS_EVERY_S):
                return

    stop_status = threading.Event()
    app.state.status_document = status_document
    if settings.status_file:
        app.router.on_startup.append(
            lambda: threading.Thread(target=status_writer, name="status-file", daemon=True).start())
        app.router.on_shutdown.append(stop_status.set)

    return app
