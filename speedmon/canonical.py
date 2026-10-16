"""Canonical record form and hashes for evidence records (KEEP IDENTICAL to speedagent/canonical.py; a test checks).

A record's ``record_hash`` is SHA-256 over its canonical JSON:
  * a flat object holding exactly the stream's fields (``FIELDS[stream]``), missing ones as null;
  * keys sorted, separators ``,`` and ``:``, UTF-8, no ASCII escaping;
  * integers as integers, floats in Python's shortest round-trip form (``repr``), booleans as 0/1.

The ledger then chains every record in insertion order:
  ``chain_hash[n] = SHA-256( chain_hash[n-1] + ":" + stream + ":" + uuid + ":" + record_hash[n] )``
  with ``chain_hash[-1] = GENESIS``. Changing, removing or reordering any stored record changes every later
  chain hash, so a head hash written down (or exported) at one moment proves later history wasn't altered.
This is tamper-evidence, not a legal certification: someone with full control of the database could rebuild
the whole chain. Exported head hashes held elsewhere are what make that detectable.
"""
from __future__ import annotations

import hashlib
import json

HASH_VERSION = 1
GENESIS = "0" * 64

FIELDS = {
    "measurement": (
        "uuid", "agent_id", "agent_seq", "source", "kind", "trigger_reason", "related_uuid", "engine", "engine_version",
        "t_ms", "started_at", "ended_at", "duration_ms", "tz_name", "utc_offset_min", "status", "failure_code",
        "failure_detail", "download_mbps", "upload_mbps", "download_bytes", "upload_bytes", "download_ms", "upload_ms",
        "ping_ms", "jitter_ms", "ping_low_ms", "ping_high_ms", "dl_latency_ms", "ul_latency_ms", "packet_loss_pct",
        "server_id", "server_name", "server_sponsor", "server_location", "server_country", "server_host", "distance_km",
        "result_id", "result_url", "isp", "iface", "link_speed_mbps", "link_duplex", "link_state", "raw_sha256",
    ),
    "diagnostic": (
        "uuid", "measurement_uuid", "agent_id", "agent_seq", "t_ms", "cpu_load1", "cpu_load5", "cpu_temp_c",
        "throttled", "mem_available_pct", "swap_used_pct", "iface", "link_speed_mbps", "link_duplex", "link_state",
        "rx_errors_delta", "tx_errors_delta", "rx_dropped_delta", "tx_dropped_delta", "rx_bytes_delta",
        "tx_bytes_delta", "window_ms", "background_rx_mbps", "background_tx_mbps", "pretest_rx_mbps",
        "pretest_tx_mbps", "gateway_rtt_ms", "gateway_loss_pct", "external_rtt_ms", "external_loss_pct", "dns_ok",
        "public_ip", "public_ip_changed", "pi_uptime_s", "boot_id", "agent_version", "extra",
    ),
    "probe": (
        "uuid", "agent_id", "agent_seq", "t_ms", "at", "interval_s", "state", "gw_rtt_ms", "gw_loss_pct",
        "ext_rtt_ms", "ext_loss_pct", "ext_detail", "dns_ms", "dns_ok", "http_ms", "http_ok", "link_up",
        "link_speed_mbps",
    ),
    "event": ("uuid", "agent_id", "agent_seq", "t_ms", "at", "type", "detail"),
}


# Numeric fields stored in REAL columns. They are always hashed as floats (132 -> 132.0), so a value
# survives the database round trip unchanged. Every other number is an integer.
REAL_FIELDS = frozenset((
    "download_mbps", "upload_mbps", "ping_ms", "jitter_ms", "ping_low_ms", "ping_high_ms", "dl_latency_ms",
    "ul_latency_ms", "packet_loss_pct", "distance_km", "cpu_load1", "cpu_load5", "cpu_temp_c", "mem_available_pct",
    "swap_used_pct", "background_rx_mbps", "background_tx_mbps", "pretest_rx_mbps", "pretest_tx_mbps",
    "gateway_rtt_ms", "gateway_loss_pct", "external_rtt_ms", "external_loss_pct", "pi_uptime_s", "gw_rtt_ms",
    "gw_loss_pct", "ext_rtt_ms", "ext_loss_pct", "dns_ms", "http_ms",
))


# Integer fields. Everything not listed here or in REAL_FIELDS is text; empty text is stored as null.
INT_FIELDS = frozenset((
    "agent_seq", "t_ms", "duration_ms", "utc_offset_min", "download_bytes", "upload_bytes", "download_ms", "upload_ms",
    "link_speed_mbps", "rx_errors_delta", "tx_errors_delta", "rx_dropped_delta", "tx_dropped_delta", "rx_bytes_delta",
    "tx_bytes_delta", "window_ms", "interval_s", "dns_ok", "http_ok", "link_up", "public_ip_changed",
))


def _norm(key, value):
    if value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):   # NaN/inf are never stored
            return None
        return value if key in REAL_FIELDS else (int(value) if value.is_integer() else value)
    if isinstance(value, int) and key in REAL_FIELDS:
        return float(value)
    return value


def normalise(stream: str, record: dict) -> dict:
    """The record exactly as it will be stored and hashed."""
    return {k: _norm(k, record.get(k)) for k in FIELDS[stream]}


def canonical_json(stream: str, record: dict) -> str:
    obj = normalise(stream, record)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def record_hash(stream: str, record: dict) -> str:
    return hashlib.sha256(canonical_json(stream, record).encode("utf-8")).hexdigest()


def chain_next(prev: str, stream: str, uuid: str, rhash: str) -> str:
    return hashlib.sha256(f"{prev}:{stream}:{uuid}:{rhash}".encode("utf-8")).hexdigest()


def sha256_text(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()
