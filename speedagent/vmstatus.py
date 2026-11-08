"""Ask Virgin's own service-status checker what it says, while our probes say the line is down.

The checker is a browser app on virginmedia.com, but its calls are plain JSON to a public API. Identify with a
postcode and the account surname, take the conversation header it returns, then read the status. Their answer is
timestamped into the ledger the moment it is given, which is worth a lot more than a screenshot taken later:
it is the ISP's own acknowledgement inside the outage, and it also separates a known area fault from a fault that
is only yours.

Caveats kept in the record itself: the answer is about the service at that postcode, not about this line; "no
fault" is not proof there is none; and an API change or a 403 is recorded as unknown rather than guessed at.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid

BASE = "https://api.vmo2digital.co.uk"
IDENTIFY = BASE + "/faults/workflow/v1/identify"
STATUS = BASE + "/faults/workflow-service-status/v1"
APP_ID = "PR2HJ4yVbIK6vg0uQLXvqR1GjNX40gLOxkFoKMYeGGlGBrZC"
CHANNEL = "Care-SelfCare-WEB"
CLIENT_SECRET = "lnlzYDi9zqxr7NiLS1N7YZPZ1tNN27uK1yY7ktRveZZGAMhaRGmkNd3o3iQRQHI8"
ORIGIN = "https://www.virginmedia.com/"
UA = "Mozilla/5.0 (X11; Linux aarch64) line-monitor"
MAX_BODY = 20_000
RECHECK_S = 1800                    # at most one check per half hour, whatever happens
MAX_PER_INCIDENT = 4
TRIGGER_STREAK = 3                  # failures in a row before asking at all


def _headers(conversation: str | None = None) -> dict:
    rid = str(uuid.uuid4())
    h = {"Accept": "application/json, text/plain, */*", "Content-Type": "application/json; charset=UTF-8",
         "Origin": "https://www.virginmedia.com", "Referer": ORIGIN, "User-Agent": UA,
         "DAPI-AppID": APP_ID, "DAPI-ChannelID": CHANNEL, "DAPI-ClientSecret": CLIENT_SECRET,
         "DAPI-CorrelationId": rid, "DAPI-RequestID": rid, "DAPI-OriginatorID": rid,
         "DAPI-RequestTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    if conversation:
        h["DAPI-ConversationId"] = conversation
    return h


def _call(url: str, timeout: float, body: dict | None = None, conversation: str | None = None,
          extra: dict | None = None) -> tuple[int, str, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={**_headers(conversation), **(extra or {})},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(MAX_BODY).decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_BODY).decode("utf-8", "replace"), dict(exc.headers or {})
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}", {}


def _verdict(status: int, body: str) -> tuple[str, str]:
    """(state, short message). Deliberately literal: an unknown shape stays unknown."""
    low = body.lower()
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    fault = False
    if isinstance(parsed, dict):
        for key in ("outages", "faults", "incidents", "plannedOutages"):
            if isinstance(parsed.get(key), list) and parsed[key]:
                fault = True
        for key in ("hasOutage", "hasFault", "outage"):
            if parsed.get(key) is True:
                fault = True
    if status == 404 or "unable to identify" in low:
        return "unidentified", "the checker could not find the account from that postcode and surname"
    if status == 403 and "session" in low:
        return "unidentified", "the checker did not accept the session"
    if status == 0:
        return "unknown", body[:200]
    if status >= 400:
        return "unknown", f"HTTP {status}"
    if fault or re.search(r"\b(fault|outage|issue(s)?) (reported|in your area)\b", low):
        return "fault_reported", "the checker reports a fault or outage at this postcode"
    if parsed is None:
        return "unknown", f"unrecognised answer: {body[:120]}"
    return "no_fault", "the checker reports no fault at this postcode"


def due(now_ms: int, last_ms: int, count: int) -> bool:
    """Bound the asking: three failures first, then one per half hour, four per incident."""
    return count < MAX_PER_INCIDENT and (count == 0 or now_ms - last_ms >= RECHECK_S * 1000)


def check(postcode: str, surname: str, timeout: float = 10.0) -> dict:
    """One check. Never raises; an unusable answer comes back as state 'unknown'."""
    started = int(time.time() * 1000)
    status, body, headers = _call(IDENTIFY, timeout, {"postCode": postcode, "lastName": surname})
    conversation = next((v for k, v in headers.items() if k.lower() == "dapi-conversationid"), None)
    if status != 200 or not conversation:
        state, msg = _verdict(status, body)
        return {"state": state if state != "no_fault" else "unidentified", "message": msg, "http": status,
                "body": body[:500], "checked_ms": started}
    status2, body2, _ = _call(STATUS, timeout, conversation=conversation,
                              extra={"Cache-Control": "no-cache, no-store, must-revalidate"})
    state, msg = _verdict(status2, body2)
    return {"state": state, "message": msg, "http": status2, "body": body2[:500], "checked_ms": started}
