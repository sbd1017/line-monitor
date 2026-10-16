import hashlib
import hmac
import json
import secrets
import time
import uuid

from speedmon import canonical, timeutil

KEY = b"k" * 48


def measurement(t_ms=None, dl=930.0, status="ok", kind="scheduled", seq=None, **kw):
    t = t_ms or timeutil.now_ms() - 60_000
    raw = json.dumps({"x": 1})
    rec = {"uuid": str(uuid.uuid4()), "agent_id": "pi", "agent_seq": seq, "source": "agent", "kind": kind, "engine": "ookla",
           "engine_version": "1.2.0.84", "t_ms": t, "started_at": timeutil.iso(t), "ended_at": timeutil.iso(t + 20000),
           "duration_ms": 20000, "tz_name": "Europe/London", "utc_offset_min": timeutil.offset_minutes(t, "Europe/London"),
           "status": status, "download_mbps": dl if status == "ok" else None, "upload_mbps": 103.0 if status == "ok" else None,
           "ping_ms": 9.5, "failure_code": None if status == "ok" else "server_error", "raw_sha256": canonical.sha256_text(raw)}
    rec.update(kw)
    return rec, raw


def item(stream, rec, raw=None):
    rec = canonical.normalise(stream, rec)
    out = {"stream": stream, "record": rec, "record_hash": canonical.record_hash(stream, rec)}
    if stream == "measurement":
        out["raw"] = raw
    return out


def probe(t_ms, state="ok", seq=None, ext=11.0, interval=60):
    return {"uuid": str(uuid.uuid4()), "agent_id": "pi", "agent_seq": seq, "t_ms": t_ms, "at": timeutil.iso(t_ms),
            "interval_s": interval, "state": state, "gw_rtt_ms": 0.5, "gw_loss_pct": 0.0,
            "ext_rtt_ms": None if state == "wan_down" else ext, "ext_loss_pct": 100.0 if state == "wan_down" else 0.0,
            "dns_ok": int(state != "wan_down"), "http_ok": int(state != "wan_down"), "link_up": 1, "link_speed_mbps": 1000}


def signed(client, method, path, body=None, agent="pi", key=KEY, ts=None, nonce=None, headers=None):
    data = b"" if body is None else json.dumps(body).encode()
    ts = str(ts or int(time.time() * 1000))
    nonce = nonce or secrets.token_hex(16)
    msg = "\n".join([method, path, ts, nonce, hashlib.sha256(data).hexdigest()]).encode()
    h = {"X-Agent-Id": agent, "X-Agent-Time": ts, "X-Agent-Nonce": nonce, "X-Agent-Signature": hmac.new(key, msg, hashlib.sha256).hexdigest(),
         "Content-Type": "application/json", **(headers or {})}
    if method == "POST":
        return client.post(path, content=data, headers=h)
    return client.get(path, headers=h)
