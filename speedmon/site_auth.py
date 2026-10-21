"""Trust nginx's central auth result only over the actual local transport peer.

Production entrypoints MUST keep uvicorn proxy_headers=False. nginx MUST overwrite
all three headers after auth_request, including on machine-auth exceptions.

Being on 127.0.0.1 is not proof that nginx sent the request: any process on the Pi can connect
there. So when a proxy key is configured (systemd LoadCredential "site-proxy-key", or
SITE_PROXY_KEY_FILE), the request must also carry it in X-Site-Proxy-Key. nginx adds that header
from a root-only snippet; the key is different for every app.
"""
import functools
import hmac
import os
import re
import time
from pathlib import Path


def enabled():
    return os.environ.get("SITE_AUTH") == "1"


@functools.lru_cache(maxsize=4)
def _read_key(cred_dir, key_file):
    paths = [Path(cred_dir) / "site-proxy-key"] if cred_dir else []
    if key_file:
        paths.append(Path(key_file))
    for path in paths:
        try:
            key = path.read_text().strip()
        except OSError:
            continue
        if len(key) >= 32:
            return key
    return None


def proxy_key():
    return _read_key(os.environ.get("CREDENTIALS_DIRECTORY"), os.environ.get("SITE_PROXY_KEY_FILE"))


def session_from_proxy(request):
    if not request.client or request.client.host not in ("127.0.0.1", "::1"):
        return None
    key = proxy_key()
    if key is not None:
        sent = request.headers.getlist("x-site-proxy-key")
        if len(sent) != 1 or not hmac.compare_digest(sent[0].encode(), key.encode()):
            return None
    names = ("x-site-user", "x-site-csrf", "x-site-expires")
    if any(len(request.headers.getlist(name)) != 1 for name in names):
        return None
    user, csrf, expiry = (request.headers[name] for name in names)
    if user != "owner" or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", csrf):
        return None
    if not re.fullmatch(r"[0-9]{1,12}", expiry) or int(expiry) <= time.time():
        return None
    return {"u": user, "csrf": csrf, "expires": int(expiry)}
