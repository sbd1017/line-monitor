"""The public demo's web server: the unchanged speedmon app, opened to visitors and fenced for a public site.

    SPEEDMON_ENV=development SPEEDMON_DATA_DIR=DIR SPEEDMON_PORT=PORT python -m demo.serve

What differs from the owner's instance (all of it here, nothing in speedmon/):
  * No entrance phrase. Every visitor gets an anonymous cookie, and speedmon's "signed in by the site" path is used
    with a session derived from it, so CSRF protection still works per visitor.
  * Read-only where one visitor's change would show to everyone: settings, notes, flags, pause, sign-out.
  * Exports: one built at a time (two queued at most) in a separate lower-priority process, a few per visitor per
    hour, the minute-by-minute probes only for periods up to 92 days, finished files deleted after two hours, and a
    cap on the space they take.
  * Integrity → Verify: at most once every 10 minutes (it re-hashes a few million records).
  * A banner saying it's a demo with generated data.
Nothing is sent anywhere: the app has no outbound features, and the status file for other apps is not enabled.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import multiprocessing
import os
import re
import secrets
import shutil
import threading
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie

import uvicorn

from speedmon import site_auth, timeutil
from speedmon import web as speedmon_web
from speedmon.config import load_settings

log = logging.getLogger("demo")
COOKIE = "lm_demo"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")
READ_ONLY = re.compile(r"^/(api/settings|api/annotations(/.*)?|api/measurement/[^/]+/flag|api/flag/[^/]+/revoke|api/pause)$")
READ_ONLY_TEXT = "This is a public demo, so settings, notes and flags are read-only (they'd change what everyone sees)."
BANNER = ("PUBLIC DEMO: generated data for a made-up line, updated live. Resets nightly at 04:00 UK time; "
          "settings and notes are read-only.")
EXPORT_PROBE_DAYS = 92
EXPORTS_PER_VISITOR_HOUR = 6
EXPORTS_PER_HOUR = 40
EXPORT_KEEP_S = 2 * 3600
EXPORT_DISK_BYTES = 2 * 1024 ** 3
VERIFY_EVERY_S = 600
EXPORT_MAX_S = 20 * 60


def _secret(data_dir) -> bytes:
    path = data_dir / "demo-secret"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key


class Demo:
    """ASGI wrapper around speedmon's app."""

    def __init__(self, settings):
        self.settings = settings
        self.base = settings.base_path
        self.key = _secret(settings.data_dir)
        site_auth.enabled = lambda: True                       # speedmon's "signed in by the site" path ...
        site_auth.session_from_proxy = self.session            # ... answered by the visitor's demo cookie
        self.app = speedmon_web.create_app(settings)
        self.db, self.exporter = self.app.state.db, self.app.state.exporter
        self.exporter.run_one = self.export_in_child(self.exporter.run_one)
        self.lock = threading.Lock()
        self.exports: dict = defaultdict(deque)                # visitor -> times of recent export requests
        self.export_times: deque = deque()
        self.last_verify = 0.0
        threading.Thread(target=self.housekeeping, name="demo-housekeeping", daemon=True).start()

    # ------------------------------------------------------------ visitors

    def csrf(self, token: str) -> str:
        return hmac.new(self.key, b"csrf:" + token.encode(), hashlib.sha256).hexdigest()

    def session(self, request):
        token = request.cookies.get(COOKIE, "")
        if not TOKEN_RE.fullmatch(token):
            return None
        return {"u": "demo", "csrf": self.csrf(token), "expires": int(time.time()) + 86400}

    @staticmethod
    def visitor(scope) -> str | None:
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                c = SimpleCookie()
                try:
                    c.load(v.decode("latin-1"))
                except Exception:
                    return None
                if COOKIE in c and TOKEN_RE.fullmatch(c[COOKIE].value):
                    return c[COOKIE].value
        return None

    # ------------------------------------------------------------ ASGI

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        token = self.visitor(scope)
        new_cookie = None
        if token is None and path.startswith(self.base + "/") and not path.startswith(self.base + "/agent/"):
            token = secrets.token_urlsafe(24)
            new_cookie = token
            scope = dict(scope)
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"cookie"] + [
                (b"cookie", f"{COOKIE}={token}".encode())]
        rel = path[len(self.base):] if path.startswith(self.base + "/") else None
        method = scope["method"]

        if rel is not None and method == "POST":
            if rel == "/leave":
                return await self.respond(send, 303, b"", [(b"location", (self.base + "/").encode())], new_cookie)
            if READ_ONLY.match(rel):
                return await self.json(send, 403, {"error": READ_ONLY_TEXT}, new_cookie)
            if rel == "/api/integrity/verify":
                with self.lock:
                    wait = VERIFY_EVERY_S - (time.monotonic() - self.last_verify)
                    if wait > 0 and self.last_verify:
                        return await self.json(send, 429, {"error": f"A full check of the ledger started less than 10 "
                                                                    f"minutes ago (the demo allows one every 10 minutes; "
                                                                    f"next in {int(wait // 60) + 1} min)."}, new_cookie)
                    self.last_verify = time.monotonic()
            if rel == "/api/exports":
                body, receive = await self.read_body(receive)
                problem = self.export_problem(token, body)
                if problem:
                    return await self.json(send, 429, {"error": problem}, new_cookie)

        html = {"on": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                ctype = dict(headers).get(b"content-type", b"")
                if ctype.startswith(b"text/html"):
                    html["on"], html["start"], html["body"] = True, message, b""
                    return
                if new_cookie:
                    headers.append(self.cookie_header(new_cookie))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and html["on"]:
                html["body"] += message.get("body", b"")
                if message.get("more_body"):
                    return
                body = self.rewrite(html["body"])
                start = html["start"]
                headers = [(k, v) for k, v in start.get("headers", []) if k != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                if new_cookie:
                    headers.append(self.cookie_header(new_cookie))
                await send({**start, "headers": headers})
                message = {"type": "http.response.body", "body": body}
            await send(message)

        return await self.app(scope, receive, send_wrapper)

    def cookie_header(self, token: str):
        secure = "; Secure" if self.settings.secure_cookies else ""
        return (b"set-cookie", f"{COOKIE}={token}; Path={self.base}; Max-Age=2592000; HttpOnly; SameSite=Strict{secure}".encode())

    async def respond(self, send, status: int, body: bytes, headers: list, new_cookie: str | None):
        headers = headers + [(b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]
        if new_cookie:
            headers.append(self.cookie_header(new_cookie))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def json(self, send, status: int, obj: dict, new_cookie: str | None):
        await self.respond(send, status, json.dumps(obj).encode(), [(b"content-type", b"application/json")], new_cookie)

    @staticmethod
    async def read_body(receive):
        chunks, more = [], True
        while more:
            m = await receive()
            chunks.append(m.get("body", b""))
            more = m.get("more_body", False)
        body = b"".join(chunks)
        sent = {"done": False}

        async def replay():
            if not sent["done"]:
                sent["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        return body, replay

    def rewrite(self, body: bytes) -> bytes:
        text = body.decode("utf-8")
        text = re.sub(r'<div class="synthetic">.*?</div>', f'<div class="synthetic">{BANNER}</div>', text, flags=re.S)
        if 'class="synthetic"' not in text:
            text = text.replace('<main id="view"', f'<div class="synthetic">{BANNER}</div>\n  <main id="view"', 1)
        text = re.sub(r'<form method="post" action="[^"]*/leave" class="leave">.*?</form>', "", text, flags=re.S)
        return text.encode("utf-8")

    # ------------------------------------------------------------ limits

    def export_problem(self, visitor: str, body: bytes) -> str | None:
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return None                                     # speedmon answers malformed requests itself
        if not isinstance(req, dict):
            return None
        now = time.monotonic()
        with self.db.read() as conn:
            busy = conn.execute("SELECT count(*) FROM exports WHERE status IN ('queued','running')").fetchone()[0]
            s = speedmon_web.settings_store.get_all(conn)
            first = speedmon_web.analytics.data_bounds(conn, {**s, "include_legacy": True})["first_ms"]
        if busy >= 2:
            return "The demo builds one export at a time and two are already waiting. Try again in a minute."
        if req.get("include_probes", True) and req.get("format") == "zip":
            try:
                a, b, _ = timeutil.resolve_range(req.get("range", "all"), s["timezone"], req.get("from"), req.get("to"),
                                                 first_ms=first)
            except (ValueError, TypeError):
                return None
            if b - a > EXPORT_PROBE_DAYS * timeutil.DAY_MS:
                return (f"In the demo, the 1-minute probes can go in a ZIP for up to {EXPORT_PROBE_DAYS} days at a time "
                        "(three years is about 1.6 million rows). Pick a shorter period, or untick the probes.")
        if self.export_bytes() > EXPORT_DISK_BYTES:
            return "The demo's export space is full for now. Finished exports are cleared after two hours."
        with self.lock:
            mine = self.exports[visitor]
            for q in (mine, self.export_times):
                while q and now - q[0] > 3600:
                    q.popleft()
            if len(mine) >= EXPORTS_PER_VISITOR_HOUR:
                return f"The demo allows {EXPORTS_PER_VISITOR_HOUR} exports an hour per visitor. Download the ones you have."
            if len(self.export_times) >= EXPORTS_PER_HOUR:
                return "The demo has built a lot of exports in the last hour. Try again a little later."
            mine.append(now)
            self.export_times.append(now)
        return None

    def export_in_child(self, run_one):
        """speedmon builds exports in a thread of the web process; a three-year workbook keeps Python busy for
        minutes and would slow every visitor's pages. Here each export runs in a forked child at a lower priority
        (the parent's thread only waits), and is stopped after EXPORT_MAX_S."""
        ctx = multiprocessing.get_context("fork")

        def child(job):
            os.nice(10)
            run_one(job)

        def run(job):
            p = ctx.Process(target=child, args=(job,), daemon=True)
            p.start()
            p.join(EXPORT_MAX_S)
            if p.is_alive():
                p.kill()
                p.join()
            with self.db.tx() as conn:
                conn.execute("UPDATE exports SET status = 'failed', finished_at = ?, error = ? WHERE uuid = ? AND status = "
                             "'running'", (timeutil.now_iso(), "stopped: took too long for the demo", job["uuid"]))
        return run

    def export_bytes(self) -> int:
        total = 0
        for root, _, files in os.walk(self.settings.export_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def housekeeping(self) -> None:
        """Delete finished exports after two hours (speedmon itself only tidies at start-up)."""
        while True:
            time.sleep(300)
            try:
                cutoff = timeutil.iso(timeutil.now_ms() - EXPORT_KEEP_S * 1000)
                with self.db.tx() as conn:
                    for r in conn.execute("SELECT uuid FROM exports WHERE status IN ('done','failed') AND finished_at < ?",
                                          (cutoff,)).fetchall():
                        shutil.rmtree(self.settings.export_dir / r["uuid"], ignore_errors=True)
                        conn.execute("UPDATE exports SET status = 'expired' WHERE uuid = ? AND status = 'done'", (r["uuid"],))
                with self.lock:
                    for v in [v for v, q in self.exports.items() if not q or time.monotonic() - q[-1] > 3600]:
                        del self.exports[v]
            except Exception as exc:
                log.warning("housekeeping: %s", exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()
    if settings.is_production:
        raise SystemExit("The demo runs with SPEEDMON_ENV=development: its data is generated.")
    settings.status_file = None                                # nothing for other apps on this host
    settings.export_keep_hours = 2
    app = Demo(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, access_log=False, proxy_headers=False,
                server_header=False, log_config=None, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
