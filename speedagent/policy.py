"""When to run the next full test. Pure logic (no I/O) so it can be tested exhaustively.

Scheduled tests: time is divided into slots of `interval_min` (aligned to the UTC epoch). Each slot gets one test
at a pseudo-random moment between 5% and 95% of the slot, derived from the agent id and slot number, so
  * every minute of the hour gets sampled (no "always at :00" bias), and
  * a restart doesn't re-roll the dice: a slot is tested at most once, and late if the agent was down at the
    due moment but is back within the slot.
Confirmation: a scheduled/manual result below `degraded_down` (or an ISP-side failure) triggers up to
`confirm_count` confirmation tests, `confirm_delay_s` apart. One good confirmation ends it (an isolated blip).
Burst: if every confirmation is poor too, tests run every `burst_interval_min` until `recovery_count`
consecutive good results, or at most `burst_max_min`. Then back to normal.
Persistent degradation: when at least 80% of the last 12 scheduled tests were poor, the line is known to be
degraded and extra tests add nothing but load, so confirmations and bursts are suspended; scheduled sampling
carries on, and extra tests resume once the scheduled results recover.
Safety: never more than `max_tests_per_day` per local day (all kinds); nothing while paused; scheduled tests keep
their slots during bursts (a burst test due within 2 minutes of a scheduled one is dropped).
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

MIN = 60_000
GAP = 90_000                             # minimum time between the start of one full test and the next
PERSIST_WINDOW, PERSIST_MIN, PERSIST_SHARE = 12, 6, 0.8


@dataclass
class State:
    mode: str = "normal"                 # normal | confirming | burst
    confirm_left: int = 0
    confirm_due_ms: int | None = None
    confirm_parent: str | None = None
    burst_until_ms: int | None = None
    burst_next_ms: int | None = None
    recovery_good: int = 0
    last_slot: str | None = None         # "<interval>:<slot index>" of the last scheduled test
    day: str | None = None
    tests_today: int = 0
    last_manual_ms: int | None = None
    last_test_ms: int | None = None
    recent: list = None                  # 1 = poor, 0 = fine, for the last PERSIST_WINDOW scheduled tests

    def __post_init__(self):
        if self.recent is None:
            self.recent = []

    @property
    def persistent(self) -> bool:
        return len(self.recent) >= PERSIST_MIN and sum(self.recent) / len(self.recent) >= PERSIST_SHARE

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


def slot_due(agent_id: str, interval_min: int, k: int) -> int:
    span = interval_min * MIN
    h = int.from_bytes(hashlib.sha256(f"{agent_id}:{interval_min}:{k}".encode()).digest()[:8], "big") / 2 ** 64
    return k * span + int(span * (0.05 + 0.9 * h))


def next_scheduled(agent_id: str, cfg: dict, st: State, now: int) -> tuple[int, str]:
    """(due time, slot key) of the next scheduled test not yet done. The due time may be in the past: that slot
    is still open and its test is overdue (the agent was busy or down at the due moment)."""
    iv = cfg["interval_min"]
    k = max(now // (iv * MIN), _slot_cmp(st.last_slot, iv) + 1)
    return slot_due(agent_id, iv, k), f"{iv}:{k}"


def _slot_cmp(last_slot: str | None, iv: int) -> int:
    if not last_slot:
        return -1
    try:
        liv, lk = last_slot.split(":")
        return int(lk) if int(liv) == iv else -1
    except ValueError:
        return -1


def is_poor(result: dict, cfg: dict) -> bool:
    """A result that should be confirmed. Local (Pi/LAN) failures and confirmed outages are excluded: the probes
    track outages, and re-testing a dead line only burns the daily cap."""
    if result["status"] != "ok":
        return result.get("failure_code") not in ("lan_fault", "link_down", "wan_down", "dns_fail", "paused", "interrupted",
                                                  "agent_error")
    return result["download_mbps"] < cfg["degraded_down"]


def decide(agent_id: str, cfg: dict, st: State, now: int, today: str, paused: bool, manual_requested: bool) -> dict:
    """What to do now: {"action": "run", "kind", "reason", "slot"?} or {"action": "wait", "until", "kind"}."""
    if st.day != today:
        st.day, st.tests_today = today, 0
    if st.mode == "burst" and st.burst_until_ms and now >= st.burst_until_ms:
        st.mode, st.burst_next_ms, st.burst_until_ms, st.recovery_good = "normal", None, None, 0
    due, slot = next_scheduled(agent_id, cfg, st, now)
    if paused:
        return {"action": "wait", "until": now + MIN, "kind": "paused", "next_scheduled_ms": due}
    if st.tests_today >= cfg["max_tests_per_day"]:
        return {"action": "wait", "until": now + MIN, "kind": "cap", "next_scheduled_ms": due}
    if st.last_test_ms and now - st.last_test_ms < GAP:            # never two full tests back to back
        return {"action": "wait", "until": st.last_test_ms + GAP, "kind": "spacing", "next_scheduled_ms": due}
    if manual_requested and (st.last_manual_ms is None or now - st.last_manual_ms >= 120_000):
        return {"action": "run", "kind": "manual", "reason": "requested from the dashboard"}
    if now >= due:
        late = now - due > 2 * MIN
        return {"action": "run", "kind": "scheduled", "slot": slot,
                "reason": f"slot {slot}" + (f" (started {round((now - due) / MIN)} min late)" if late else "")}
    cands = [(due, "scheduled")]
    if st.mode == "confirming" and st.confirm_due_ms:
        if now >= st.confirm_due_ms:
            return {"action": "run", "kind": "confirmation", "reason": f"confirming a poor result ({st.confirm_left} left)",
                    "related": st.confirm_parent}
        cands.append((st.confirm_due_ms, "confirmation"))
    if st.mode == "burst" and st.burst_next_ms:
        if now >= st.burst_next_ms and due - now > 2 * MIN:
            return {"action": "run", "kind": "burst", "reason": "degradation confirmed: sampling more often"}
        if st.burst_next_ms < due - 2 * MIN:
            cands.append((st.burst_next_ms, "burst"))
    until, kind = min(cands)
    return {"action": "wait", "until": until, "kind": kind, "next_scheduled_ms": due}


def after(cfg: dict, st: State, kind: str, result: dict, now: int, slot: str | None = None, uuid: str | None = None) -> str | None:
    """Update the state after a test. Returns a mode-change description (for an event) or None."""
    st.tests_today += 1
    st.last_test_ms = now
    if kind == "scheduled" and slot:
        st.last_slot = slot
    if kind == "manual":
        st.last_manual_ms = now
    poor = is_poor(result, cfg)
    before = st.mode
    was_persistent = st.persistent
    if kind == "scheduled" and (result["status"] == "ok" or poor):
        st.recent = (st.recent + [int(poor)])[-PERSIST_WINDOW:]
    if st.persistent:
        if st.mode != "normal":
            st.mode, st.confirm_due_ms, st.confirm_left, st.burst_next_ms, st.burst_until_ms = "normal", None, 0, None, None
        return "persistent degradation: confirmations and bursts suspended" if not was_persistent else (
            f"{before} -> normal (persistent degradation)" if before != "normal" else None)
    if was_persistent:
        return "degradation no longer persistent: confirmations and bursts resume"
    if st.mode == "normal":
        if poor and kind in ("scheduled", "manual") and cfg["confirm_count"] > 0:
            st.mode, st.confirm_left, st.confirm_parent = "confirming", cfg["confirm_count"], uuid
            st.confirm_due_ms = now + cfg["confirm_delay_s"] * 1000
    elif st.mode == "confirming":
        if kind == "confirmation" or kind == "scheduled":
            if not poor:
                st.mode, st.confirm_due_ms, st.confirm_left = "normal", None, 0
            else:
                st.confirm_left -= 1
                if st.confirm_left <= 0:
                    st.mode, st.confirm_due_ms = "burst", None
                    st.burst_until_ms = now + cfg["burst_max_min"] * MIN
                    st.burst_next_ms = now + cfg["burst_interval_min"] * MIN
                    st.recovery_good = 0
                else:
                    st.confirm_due_ms = now + cfg["confirm_delay_s"] * 1000
    elif st.mode == "burst":
        st.recovery_good = 0 if poor else st.recovery_good + 1
        if st.recovery_good >= cfg["recovery_count"]:
            st.mode, st.burst_next_ms, st.burst_until_ms = "normal", None, None
        else:
            st.burst_next_ms = now + cfg["burst_interval_min"] * MIN
    return f"{before} -> {st.mode}" if st.mode != before else None
