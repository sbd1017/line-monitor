"""Time handling. Storage is always UTC (epoch ms + ISO text); grouping by day/hour uses the owner's zone
(Europe/London by default), DST-aware via zoneinfo. On the October fall-back day, 01:00-01:59 local happens
twice and both hours land in the "01" bucket; the March spring-forward day has no 01:00 bucket at all.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = timezone.utc
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


@lru_cache(maxsize=8)
def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def now_iso() -> str:
    return iso(now_ms())


def parse_iso_ms(text: str) -> int:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp needs a timezone")
    return int(round(dt.timestamp() * 1000))


def local(ms: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ms / 1000, zone(tz))


def local_parts(ms: int, tz: str) -> tuple[str, int, int]:
    """(YYYY-MM-DD, hour 0-23, weekday 0=Mon) in the given zone."""
    d = local(ms, tz)
    return d.strftime("%Y-%m-%d"), d.hour, d.weekday()


def local_midnight_ms(day: date, tz: str) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=zone(tz)).timestamp() * 1000)


def local_to_ms(text: str, tz: str) -> int:
    """'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM[:SS]' in the owner's zone (or with an explicit offset) -> UTC ms."""
    text = text.strip()
    if len(text) == 10:
        return local_midnight_ms(date.fromisoformat(text), tz)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone(tz))
    return int(dt.timestamp() * 1000)


def fmt_local(ms: int | None, tz: str, with_seconds: bool = False) -> str:
    if ms is None:
        return ""
    return local(ms, tz).strftime("%Y-%m-%d %H:%M:%S %Z" if with_seconds else "%Y-%m-%d %H:%M %Z")


def offset_minutes(ms: int, tz: str) -> int:
    off = local(ms, tz).utcoffset() or timedelta(0)
    return int(off.total_seconds() // 60)


def resolve_range(preset: str, tz: str, start: str | None = None, end: str | None = None,
                  now: int | None = None, first_ms: int | None = None) -> tuple[int, int, str]:
    """Named ranges -> (from_ms, to_ms exclusive, human label). Calendar presets follow the local zone."""
    now = now or now_ms()
    today = local(now, tz).date()
    mid = lambda d: local_midnight_ms(d, tz)
    if preset == "1h":
        return now - HOUR_MS, now + 1, "Last hour"
    if preset == "6h":
        return now - 6 * HOUR_MS, now + 1, "Last 6 hours"
    if preset == "24h":
        return now - DAY_MS, now + 1, "Last 24 hours"
    if preset in ("7d", "30d", "90d"):
        n = int(preset[:-1])
        return now - n * DAY_MS, now + 1, f"Last {n} days"
    if preset == "1y":
        return now - 365 * DAY_MS, now + 1, "Last 12 months"
    if preset == "today":
        return mid(today), now + 1, f"Today ({today.isoformat()})"
    if preset == "yesterday":
        y = today - timedelta(days=1)
        return mid(y), mid(today), f"Yesterday ({y.isoformat()})"
    if preset == "this_month":
        first = today.replace(day=1)
        return mid(first), now + 1, first.strftime("%B %Y (to date)")
    if preset == "last_month":
        first_this = today.replace(day=1)
        first_last = (first_this - timedelta(days=1)).replace(day=1)
        return mid(first_last), mid(first_this), first_last.strftime("%B %Y")
    if preset == "this_year":
        first = date(today.year, 1, 1)
        return mid(first), now + 1, f"{today.year} (to date)"
    if preset == "all":
        return (first_ms if first_ms is not None else 0), now + 1, "All data"
    if preset == "custom":
        if not start or not end:
            raise ValueError("custom range needs a start and an end")
        a = local_to_ms(start, tz)
        b = local_to_ms(end, tz)
        if len(end.strip()) == 10:                      # an end *date* includes that whole day
            b = local_midnight_ms(date.fromisoformat(end.strip()) + timedelta(days=1), tz)
        if b <= a:
            raise ValueError("the end must be after the start")
        return a, b, f"{start} to {end}"
    raise ValueError(f"unknown range {preset!r}")


class LocalClock:
    """Fast UTC-ms -> local (date, hour, weekday, minute-of-day) for bulk analytics. Offsets are looked up once per
    15-minute UTC bucket (every real-world DST transition falls on a quarter hour), so this is exact."""
    _Q = 900_000

    def __init__(self, tz: str):
        self.tz = tz
        self._off: dict[int, int] = {}
        self._day: dict[int, str] = {}

    def shifted(self, ms: int) -> int:
        ms = int(ms)
        q = ms // self._Q
        off = self._off.get(q)
        if off is None:
            off = int((local(q * self._Q, self.tz).utcoffset() or timedelta(0)).total_seconds() * 1000)
            self._off[q] = off
        return ms + off

    def parts(self, ms: int) -> tuple[str, int, int, int]:
        s = self.shifted(ms)
        days = s // DAY_MS
        d = self._day.get(days)
        if d is None:
            d = date.fromordinal(719163 + days).isoformat()
            self._day[days] = d
        minute = (s % DAY_MS) // 60_000
        return d, minute // 60, (days + 3) % 7, minute
