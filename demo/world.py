"""The made-up line behind the public demo: what a Virgin Media cable connection in a busy street does over three
years, as a deterministic function of time.

Everything is derived from a seed and an anchor day (the local date the dataset ends on), so the same anchor always
gives the same history, and the live updater (tick.py) carries on with exactly the line the generator described.
Randomness comes from hash-keyed generators (one per test, probe minute or day), never from shared state, so any
moment can be recomputed on its own.

The story, counted back from the anchor day:
  * an M350 package (362/36) for the first ~16 months, then Gig1 (1,130/104);
  * on Gig1 the Pi's built-in 1 GbE port caps results near 940 Mbps until a 2.5 GbE USB adapter is fitted;
  * evening congestion (deeper on weekdays and in winter), a lighter daytime dip at weekends;
  * a handful of Internet outages (minutes to hours), a DNS fault, a router restart (a local fault);
  * a week-long ISP fault (noise on the line) that drags speeds below the guarantee until an engineer fixes it;
  * a couple of times the monitor itself was off (a power cut, a house move of the router), and a bad cable that
    negotiated 100 Mb/s for two days;
  * the odd failed test (server errors, timeouts) that the fallback engine covers.
"""
from __future__ import annotations

import hashlib
import math
import random
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import date, timedelta

from speedmon import timeutil

TZ = "Europe/London"
MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000
YEARS_DAYS = 1096                          # three years of history

M350 = {"name": "Virgin Media M350 Fibre Broadband", "advertised_down": 362.0, "advertised_up": 36.0,
        "minimum_guaranteed_down": 181.0, "target_down": 330.0, "degraded_down": 181.0, "severe_down": 90.0,
        "target_up": 34.0, "degraded_up": 18.0, "tiers": [350, 330, 181]}
GIG1 = {"name": "Virgin Media Gig1 Fibre Broadband", "advertised_down": 1130.0, "advertised_up": 104.0,
        "minimum_guaranteed_down": 565.0, "target_down": 900.0, "degraded_down": 565.0, "severe_down": 282.0,
        "target_up": 98.0, "degraded_up": 52.0, "tiers": [900, 800, 565]}

# Ookla servers the agent's automatic selection lands on (sponsor, location, made-up id, weight, speed factor).
SERVERS = [("Virgin Media O2", "London", "61421", 46, 1.0), ("Community Fibre", "London", "58113", 18, 0.99),
           ("Vodafone UK", "London", "47290", 14, 0.985), ("Virgin Media O2", "Manchester", "61422", 12, 0.975),
           ("Hyperoptic", "London", "52866", 10, 0.97)]
GO_SERVERS = [("Virgin Media O2", "London", "61421"), ("Community Fibre", "London", "58113")]


def h01(*key) -> float:
    """A uniform number in [0, 1) from a key: the same key always gives the same number."""
    d = hashlib.blake2b(":".join(map(str, key)).encode(), digest_size=8).digest()
    return int.from_bytes(d, "big") / 2 ** 64


def rng(*key) -> random.Random:
    return random.Random(hashlib.blake2b(":".join(map(str, key)).encode(), digest_size=16).digest())


def rng_uuid(r: random.Random) -> str:
    return str(uuidlib.UUID(int=r.getrandbits(128), version=4))


@dataclass
class Span:
    a: int
    b: int
    kind: str
    info: dict = field(default_factory=dict)

    def has(self, t: int) -> bool:
        return self.a <= t < self.b


class World:
    """The line, the monitor and the house, for one anchor day."""

    def __init__(self, anchor: date, seed: int = 20231, days: int = YEARS_DAYS):
        self.anchor, self.seed, self.days = anchor, seed, days
        self.clock = timeutil.LocalClock(TZ)
        self.end_day = anchor + timedelta(days=1)                         # the day after the anchor, local midnight
        self.start_day = self.end_day - timedelta(days=days)          # (tests use a shorter history)
        self.start_ms = self.ms(self.start_day)
        day = lambda back: self.end_day - timedelta(days=back)            # a date counted back from the anchor
        self.gig1_day = day(610)
        self.adapter_day = day(538)
        self.packages = [{"from": (self.start_day - timedelta(days=400)).isoformat(), **M350},
                         {"from": self.gig1_day.isoformat(), **GIG1}]
        self.fault = Span(self.at(day(151), 18, 40), self.at(day(144), 13, 15), "fault")
        self.events: list[Span] = []
        add = lambda d, hh, mm, minutes, kind, **info: self.events.append(
            Span(self.at(d, hh, mm), self.at(d, hh, mm) + int(minutes * MIN), kind, info))
        # Internet outages (router answers, the Internet doesn't)
        add(day(1071), 2, 14, 38, "outage", why="overnight network work")
        add(day(1003), 19, 52, 7, "outage")
        add(day(911), 9, 31, 212, "outage", why="area outage (cable damage)")
        add(day(847), 23, 5, 64, "outage", why="planned maintenance")
        add(day(760), 16, 47, 4, "outage")
        add(day(688), 7, 12, 19, "outage")
        add(day(560), 13, 26, 365, "outage", why="area outage (power to the street cabinet)")
        add(day(470), 1, 3, 95, "outage", why="planned maintenance")
        add(day(402), 20, 33, 11, "outage")
        add(day(331), 11, 18, 27, "outage")
        add(day(148), 21, 40, 46, "outage")                                # during the fault week
        add(day(146), 8, 2, 16, "outage")
        add(day(73), 18, 9, 6, "outage")
        add(day(4), 14, 22, 23, "outage")
        add(day(617), 22, 10, 18, "dns", why="DNS fault at the ISP")
        add(day(298), 10, 31, 4, "lan", why="router restarted itself (firmware update)")
        add(day(612), 9, 40, 6, "lan", why="router restarted (package change)")
        # the monitor itself was off: no probes, no tests
        add(day(782), 17, 4, 163, "offline", clean=False, rebooted=True, why="power cut")
        add(day(421), 10, 15, 547, "offline", clean=True, rebooted=True, why="router and Pi moved to the front room")
        add(day(95), 3, 30, 14, "offline", clean=True, rebooted=True, why="system updates")
        # a bad Ethernet cable negotiated 100 Mb/s (monitor-side: results flagged, not counted against the ISP)
        add(day(212), 17, 5, 19 * 60 + 11, "slowlink")
        self.events.sort(key=lambda s: s.a)
        # things the agent logs on its own: the new package's thresholds reaching it at midnight
        self.agent_events = [(self.ms(self.gig1_day) + 9_400, "config_applied",
                              {"keys": ["degraded_down", "quiet_threshold_mbps", "severe_down", "target_down"]})]
        self.manual = sorted(self._manual_requests())

    # ------------------------------------------------------------ calendar

    def ms(self, d: date) -> int:
        return timeutil.local_midnight_ms(d, TZ)

    def at(self, d: date, hh: int, mm: int) -> int:
        return timeutil.local_to_ms(f"{d.isoformat()}T{hh:02d}:{mm:02d}", TZ)

    def package(self, t: int) -> dict:
        return GIG1 if t >= self.ms(self.gig1_day) else M350

    def span(self, t: int, *kinds) -> Span | None:
        for s in self.events:
            if s.a > t:
                return None
            if s.kind in kinds and s.has(t):
                return s
        return None

    def offline_spans(self) -> list[Span]:
        return [s for s in self.events if s.kind == "offline"]

    def link_speed(self, t: int) -> int:
        if self.span(t, "slowlink"):
            return 100
        return 2500 if t >= self.ms(self.adapter_day) else 1000

    def iface(self, t: int) -> str:
        return "eth1" if t >= self.ms(self.adapter_day) else "eth0"

    def _manual_requests(self):
        """'Run a test now' presses: a few a month, more while the fault was being chased."""
        out = []
        r = rng(self.seed, "manual")
        t = self.start_ms + 3 * DAY
        while t < self.ms(self.end_day):
            t += int(r.expovariate(1 / (9 * DAY)))
            hh = r.choice([8, 12, 13, 18, 19, 20, 21, 22])
            out.append(self.at(date.fromisoformat(self.clock.parts(t)[0]), hh, r.randint(0, 59)))
        for k in range(9):
            out.append(self.fault.a + int((k + 0.5) * (self.fault.b - self.fault.a) / 9) + r.randint(-40, 40) * MIN)
        return [x for x in out if self.start_ms < x]

    # ------------------------------------------------------------ the line

    def day_parts(self, t: int):
        """(local date 'YYYY-MM-DD', minute of the day, weekday 0=Monday, month)."""
        d, _, dow, minute = self.clock.parts(t)
        return d, minute + (t % MIN) / MIN, dow, int(d[5:7])

    def congestion(self, t: int) -> float:
        """Share of speed lost to evening (and weekend daytime) contention at t: 0 = none."""
        d, m, dow, month = self.day_parts(t)
        depth = 0.05 + 0.32 * h01(self.seed, "depth", d) ** 1.7
        if h01(self.seed, "badnight", d) < 0.04:
            depth += 0.25                                                  # a big match or a release night
        winter = {11: 1.25, 12: 1.4, 1: 1.3, 2: 1.15, 6: 0.8, 7: 0.75, 8: 0.8}.get(month, 1.0)
        if dow >= 5:
            evening = 0.75 * depth * math.exp(-((m - 20.5 * 60) / 170) ** 2)
            daytime = 0.35 * depth * math.exp(-((m - 14.5 * 60) / 160) ** 2)
            c = (evening + daytime) * winter
        else:
            c = depth * math.exp(-((m - 21.1 * 60) / 95) ** 2) * winter
            c += 0.03 * math.exp(-((m - 8.4 * 60) / 50) ** 2)              # school-run streaming blip
        if t < self.ms(self.gig1_day):
            c *= 0.8                                                       # the M350 tier hit the shared limit less
        return min(c, 0.85)

    def fault_factor(self, t: int) -> float:
        """Extra loss of speed during the ISP fault week (upstream noise): worst in the evenings and mid-week."""
        if not self.fault.has(t):
            return 1.0
        d, m, dow, _ = self.day_parts(t)
        progress = (t - self.fault.a) / (self.fault.b - self.fault.a)
        base = 0.42 - 0.18 * math.sin(math.pi * progress)                  # worst in the middle of the week
        evening = 0.55 if 17 * 60 <= m <= 23 * 60 else 1.0
        return max(0.08, base * evening * (0.75 + 0.5 * h01(self.seed, "fault", t // (20 * MIN))))

    def line_capacity(self, t: int) -> float:
        """What the line would deliver to a perfect tester at t (Mbps, download)."""
        if t >= self.ms(self.gig1_day):
            drift = 1 - 0.03 * h01(self.seed, "week", (t - self.start_ms) // (7 * DAY))
            return 1106.0 * drift
        return 378.0 * (1 - 0.02 * h01(self.seed, "week", (t - self.start_ms) // (7 * DAY)))

    def upload_capacity(self, t: int) -> float:
        return 109.2 if t >= self.ms(self.gig1_day) else 38.6

    def latency(self, t: int) -> float:
        """Idle round-trip time to the outside world (ms) at t, before per-probe noise."""
        c = self.congestion(t)
        base = 10.6 if t >= self.ms(self.gig1_day) else 12.1
        extra = 14 * c ** 1.3
        if self.fault.has(t):
            extra += 9 + 25 * (1 - self.fault_factor(t))
        return base + extra

    def state(self, t: int) -> tuple[str, Span | None]:
        """'ok' | 'outage' | 'dns' | 'lan' | 'offline' and the span causing it."""
        s = self.span(t, "offline", "outage", "dns", "lan")
        return (s.kind, s) if s else ("ok", None)
