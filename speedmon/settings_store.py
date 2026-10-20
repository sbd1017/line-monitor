"""Owner-editable settings: one definition per key (default, type, limits), validated on every write and
recorded in settings_history. The agent re-validates the schedule against its own hard limits, so a bad
value here can never make it test more often than the floors below.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import quality, timeutil

# Hard floors/ceilings. Not editable from the UI; the agent enforces the same numbers independently.
HARD = {
    "min_interval_min": 5,          # scheduled full tests never closer than this
    "min_burst_interval_min": 5,
    "min_confirm_delay_s": 60,
    "max_confirm_count": 5,
    "max_tests_per_day": 300,
    "min_probe_interval_s": 10,
}


@dataclass(frozen=True)
class Field:
    key: str
    default: object
    kind: str                     # int | float | str | bool | tiers | servers | choice | datetime
    group: str
    label: str
    help: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    danger_below: float | None = None   # UI asks for confirmation below this value
    nullable: bool = False


FIELDS: list[Field] = [
    # package history: each test is judged against the package in force on its (local) date
    Field("packages", None, "packages", "package", "Packages over time",
          "Each entry applies from its start date until the next one. A test is judged against the package in force "
          "that day, so an upgrade doesn't rewrite how older results are assessed."),
    # thresholds that don't depend on the package
    Field("latency_degraded_ms", 40.0, "float", "thresholds", "Idle latency considered poor above (ms)", "", 1, 5000),
    # schedule
    Field("interval_min", 30, "int", "schedule", "Full test every (minutes)",
          "Each test is placed at a random moment inside its slot, so every minute of the hour gets sampled.",
          HARD["min_interval_min"], 1440, danger_below=15),
    Field("confirm_count", 2, "int", "schedule", "Confirmation tests after a poor result",
          "", 0, HARD["max_confirm_count"]),
    Field("confirm_delay_s", 120, "int", "schedule", "Wait before each confirmation test (seconds)",
          "", HARD["min_confirm_delay_s"], 3600),
    Field("burst_interval_min", 10, "int", "schedule", "While degraded, test every (minutes)",
          "Burst mode starts when confirmations agree the line is degraded.", HARD["min_burst_interval_min"], 720,
          danger_below=10),
    Field("burst_max_min", 120, "int", "schedule", "Leave burst mode after at most (minutes)", "", 10, 1440),
    Field("recovery_count", 2, "int", "schedule", "Good results needed to leave burst mode", "", 1, 10),
    Field("max_tests_per_day", 96, "int", "schedule", "Daily cap on full tests (all kinds)",
          "A safety net. Scheduled tests stop for the rest of the local day once it's reached.", 1,
          HARD["max_tests_per_day"], danger_below=None),
    Field("paused_until", None, "datetime", "schedule", "Pause full tests until",
          "Probes keep running. Empty = not paused.", nullable=True),
    Field("quiet_threshold_mbps", None, "float", "schedule", "Postpone a test if the Pi is already moving more than (Mbps)",
          "Measured on the Pi's own Ethernet port just before a test. Empty = automatic: 5% of the package's advertised "
          "download (at least 5 Mbps), the same line the 'Pi busy' label uses.", 1, 10000, nullable=True),
    Field("quiet_max_defer", 3, "int", "schedule", "Postpone at most (times, 30 s apart)", "", 0, 10),
    # probes
    Field("probe_interval_s", 60, "int", "probes", "Connectivity probe every (seconds)",
          "Light: a few pings, one DNS lookup, one tiny HTTPS request.", HARD["min_probe_interval_s"], 600),
    # engines
    Field("engine", "ookla", "choice", "engines", "Primary engine", "", choices=("ookla", "speedtest-go")),
    Field("fallback_engine", "speedtest-go", "choice", "engines", "Fallback engine (when the primary errors)",
          "", choices=("speedtest-go", "ookla", "none")),
    Field("server_mode", "auto", "choice", "engines", "Server selection",
          "auto = engine picks the lowest-latency server; pinned = rotate through the list below; "
          "prefer = use the list, fall back to auto if they all fail.", choices=("auto", "pinned", "prefer")),
    Field("servers", [], "servers", "engines", "Server IDs", "Ookla server IDs, as shown by speedtest -L on the Pi, e.g. 12345."),
    # fresh start
    Field("fresh_start_at", None, "datetime", "reset", "Fresh start at",
          "At this time everything recorded so far is archived (a verified copy; nothing is deleted) and a new, empty "
          "evidence database starts. Settings carry over. Empty = none planned. One-shot: it clears itself.", nullable=True),
    # display
    Field("timezone", "Europe/London", "str", "display", "Time zone for days and hours"),
    Field("default_range", "7d", "choice", "display", "Dashboard opens on",
          choices=("24h", "7d", "30d", "90d", "1y", "all")),
    Field("include_legacy", False, "bool", "display", "Include legacy speedtest-cli results in statistics",
          "Off by default: that data came from a different, less reliable method (see Methodology)."),
]
BY_KEY = {f.key: f for f in FIELDS}

# From Virgin Media's Gig1 contract summary ("Your Broadband Speeds Explained"): advertised 1,130/104;
# normally available 1,012-1,139 / 98-105; minimum 565/52; minimum guaranteed download 565 (50% of advertised).
# The on-target line is 900 because the Pi's own 1 GbE port tops out near 940 Mbps of real throughput (raise it with a
# 2.5 GbE USB adapter fitted; see OPERATIONS.md, Packages).
GIG1 = {"from": "2000-01-01", "name": "Virgin Media Gig1 Fibre Broadband", "advertised_down": 1130.0, "advertised_up": 104.0,
        "minimum_guaranteed_down": 565.0, "target_down": 900.0, "degraded_down": 565.0, "severe_down": 282.0,
        "target_up": 98.0, "degraded_up": 52.0, "tiers": [900, 800, 565]}
BY_KEY["packages"] = Field("packages", [GIG1], "packages", "package", BY_KEY["packages"].label, BY_KEY["packages"].help)
FIELDS[:] = [BY_KEY.get(f.key, f) for f in FIELDS]
PACKAGE_NUMBERS = ("advertised_down", "advertised_up", "minimum_guaranteed_down", "target_down", "degraded_down", "severe_down",
                   "target_up", "degraded_up")
# Current-package values exposed at the top level of the settings dict (read-only; edit "packages").
PACKAGE_KEYS = PACKAGE_NUMBERS + ("tiers",)


def _coerce_packages(value) -> list:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("Packages: 1-20 entries")
    out, seen = [], set()
    for i, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Package {i}: invalid")
        p = {}
        d = str(raw.get("from") or "").strip()
        try:
            from datetime import date as _d
            _d.fromisoformat(d)
        except ValueError:
            raise ValueError(f"Package {i}: start date must be YYYY-MM-DD") from None
        if d in seen:
            raise ValueError(f"Two packages start on {d}")
        seen.add(d)
        p["from"] = d
        name = str(raw.get("name") or "").strip()
        if not 1 <= len(name) <= 120:
            raise ValueError(f"Package {i}: name 1-120 characters")
        p["name"] = name
        for k in PACKAGE_NUMBERS:
            v = raw.get(k)
            if v is None or v == "":
                if k == "minimum_guaranteed_down":
                    p[k] = None
                    continue
                raise ValueError(f"{name}: {k.replace('_', ' ')} is required")
            v = float(v)
            if not 0 < v <= 100000:
                raise ValueError(f"{name}: {k.replace('_', ' ')} out of range")
            p[k] = v
        if not p["severe_down"] < p["degraded_down"] <= p["target_down"]:
            raise ValueError(f"{name}: thresholds must satisfy severe < degraded ≤ target")
        if p["degraded_up"] > p["target_up"]:
            raise ValueError(f"{name}: upload degraded threshold must be at or below the upload target")
        tiers = _coerce(Field("tiers", None, "tiers", "", f"{name}: tiers"), raw.get("tiers") or [p["target_down"]])
        p["tiers"] = tiers
        out.append(p)
    return sorted(out, key=lambda x: x["from"])


class PackageClock:
    """Package in force at a given UTC time (each entry starts at local midnight of its date)."""

    def __init__(self, packages: list, tz: str):
        import bisect
        from datetime import date as _d
        self._bisect = bisect
        items = sorted(packages or [GIG1], key=lambda x: x["from"])
        self.starts = [timeutil.local_midnight_ms(_d.fromisoformat(p["from"]), tz) for p in items]
        self.items = items

    def at(self, ms: int) -> dict:
        i = self._bisect.bisect_right(self.starts, ms) - 1
        return self.items[max(i, 0)]

    def periods(self, a: int, b: int) -> list:
        """[(start_ms, end_ms, package)] overlapping [a, b)."""
        out = []
        for i, p in enumerate(self.items):
            s0 = self.starts[i] if i else -10 ** 15
            s1 = self.starts[i + 1] if i + 1 < len(self.items) else 10 ** 15
            if s1 > a and s0 < b:
                out.append((max(s0, a), min(s1, b), p))
        return out


def sql_case(s: dict, key: str, col: str = "m.t_ms") -> tuple[str, list]:
    """SQL expression giving the package value `key` in force at each row's time (for filters and buckets)."""
    clk = PackageClock(s.get("packages"), s.get("timezone", "Europe/London"))
    if len(clk.items) == 1:
        return "?", [clk.items[0][key]]
    parts, args = [], []
    for start, pk in reversed(list(zip(clk.starts[1:], clk.items[1:]))):
        parts.append(f"WHEN {col} >= ? THEN ?")
        args += [start, pk[key]]
    return f"(CASE {' '.join(parts)} ELSE ? END)", args + [clk.items[0][key]]


def apply_current(s: dict, now_ms: int | None = None) -> dict:
    s["packages"] = s.get("packages") or [GIG1]
    cur = PackageClock(s["packages"], s.get("timezone", "Europe/London")).at(now_ms or timeutil.now_ms())
    s["package"] = cur
    s["package_name"] = cur["name"]
    for k in PACKAGE_KEYS:
        s[k] = cur.get(k)
    return s
AGENT_KEYS = ("interval_min", "confirm_count", "confirm_delay_s", "burst_interval_min", "burst_max_min",
              "recovery_count", "max_tests_per_day", "paused_until", "quiet_threshold_mbps", "quiet_max_defer",
              "probe_interval_s", "engine", "fallback_engine", "server_mode", "servers", "degraded_down",
              "target_down", "severe_down", "timezone")


def _coerce(f: Field, value):
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if f.nullable:
            return None
        raise ValueError(f"{f.label}: required")
    if f.kind == "packages":
        return _coerce_packages(value)
    if f.kind == "int":
        v = int(float(value))
    elif f.kind == "float":
        v = float(value)
    elif f.kind == "bool":
        v = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    elif f.kind == "str":
        v = str(value).strip()
        if not v or len(v) > 120:
            raise ValueError(f"{f.label}: 1-120 characters")
        if f.key == "timezone":
            try:
                timeutil.zone(v)
            except Exception:
                raise ValueError("Unknown time zone") from None
    elif f.kind == "choice":
        v = str(value)
        if v not in f.choices:
            raise ValueError(f"{f.label}: choose one of {', '.join(f.choices)}")
    elif f.kind == "tiers":
        items = value if isinstance(value, list) else [x for x in str(value).replace(",", " ").split() if x]
        v = sorted({round(float(x), 1) for x in items}, reverse=True)
        if not 1 <= len(v) <= 6 or any(x <= 0 or x > 100000 for x in v):
            raise ValueError("Tiers: 1-6 positive values")
        v = [int(x) if float(x).is_integer() else x for x in v]
    elif f.kind == "servers":
        items = value if isinstance(value, list) else [x for x in str(value).replace(",", " ").split() if x]
        v = []
        for x in items:
            s = str(x).strip()
            if not s.isdigit() or len(s) > 9:
                raise ValueError("Server IDs are numbers")
            if s not in v:
                v.append(s)
        if len(v) > 20:
            raise ValueError("At most 20 servers")
    elif f.kind == "datetime":
        v = timeutil.iso(timeutil.parse_iso_ms(str(value)) if "T" in str(value) and ("Z" in str(value) or "+" in str(value))
                         else timeutil.local_to_ms(str(value), "Europe/London"))
    else:
        raise ValueError(f.key)
    if f.kind in ("int", "float"):
        if f.lo is not None and v < f.lo:
            raise ValueError(f"{f.label}: at least {f.lo:g}")
        if f.hi is not None and v > f.hi:
            raise ValueError(f"{f.label}: at most {f.hi:g}")
    return v


def get_all(conn) -> dict:
    stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    out = {}
    for f in FIELDS:
        if f.key in stored:
            try:
                out[f.key] = json.loads(stored[f.key])
                continue
            except ValueError:
                pass
        out[f.key] = f.default
    for key in ("data_origin", "epoch"):
        if key in stored:
            out[key] = json.loads(stored[key])
    return apply_current(out)


def get(conn, key: str):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row:
        return json.loads(row["value"])
    return BY_KEY[key].default if key in BY_KEY else None


def validate(changes: dict, current: dict) -> dict:
    """Coerce and cross-check a set of changes. Raises ValueError with an owner-readable message."""
    clean = {}
    for key, value in changes.items():
        if key not in BY_KEY:
            raise ValueError(f"Unknown setting {key}")
        clean[key] = _coerce(BY_KEY[key], value)
    merged = {**current, **clean}
    if merged["burst_interval_min"] > merged["interval_min"]:
        raise ValueError("Burst interval should not be longer than the normal interval")
    if merged["server_mode"] in ("pinned", "prefer") and not merged["servers"]:
        raise ValueError("Pinned/prefer server modes need at least one server ID")
    if merged["engine"] == merged["fallback_engine"]:
        raise ValueError("The fallback engine must differ from the primary engine")
    return clean


def danger_notes(changes: dict) -> list[str]:
    """Settings that deserve an explicit 'yes, I mean it' in the UI."""
    notes = []
    if changes.get("fresh_start_at"):
        notes.append(f"Fresh start at {changes['fresh_start_at']} (UTC): every current record will be archived and a new, "
                     "empty database started. Nothing is deleted, but the dashboard and exports will begin from zero.")
    for key, value in changes.items():
        f = BY_KEY.get(key)
        if f and f.kind != "datetime" and f.danger_below is not None and value is not None and float(value) < f.danger_below:
            notes.append(f"{f.label} = {value}: below {f.danger_below:g}, which uses a lot of data at gigabit speeds")
    return notes


def download_band(mbps: float, pk: dict) -> str:
    """Where a successful download sits against its package: target, below (target missed), degraded or severe."""
    if mbps >= pk["target_down"]:
        return "target"
    if mbps < pk["severe_down"]:
        return "severe"
    if mbps < pk["degraded_down"]:
        return "degraded"
    return "below"


def save(conn, changes: dict, source: str = "settings") -> dict:
    now = timeutil.now_iso()
    for key, value in changes.items():
        old = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        new = json.dumps(value)
        if old and old["value"] == new:
            continue
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (key, new, now))
        conn.execute("INSERT INTO settings_history (key, old_value, new_value, changed_at, source) VALUES (?, ?, ?, ?, ?)",
                     (key, old["value"] if old else None, new, now, source))
    return changes


def set_internal(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (key, json.dumps(value), timeutil.now_iso()))


def agent_config(s: dict) -> dict:
    out = {k: s[k] for k in AGENT_KEYS}
    if out.get("quiet_threshold_mbps") is None:                # automatic, from the package in force now
        out["quiet_threshold_mbps"] = quality.busy_limit(s.get("package"))
    return out


def describe() -> list[dict]:
    return [{"key": f.key, "kind": f.kind, "group": f.group, "label": f.label, "help": f.help, "lo": f.lo, "hi": f.hi,
             "choices": list(f.choices), "danger_below": f.danger_below, "nullable": f.nullable, "default": f.default}
            for f in FIELDS]
