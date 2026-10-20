"""Trust nginx's central auth result only over the actual local transport peer.

Production entrypoints MUST keep uvicorn proxy_headers=False. nginx MUST overwrite
all three headers after auth_request, including on machine-auth exceptions.
"""
import os
import re
import time


def enabled():
    return os.environ.get("SITE_AUTH") == "1"


def session_from_proxy(request):
    if not request.client or request.client.host not in ("127.0.0.1", "::1"):
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
