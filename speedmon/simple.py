"""The Simple tab: the same evidence as the rest of the dashboard, said in plain words for someone who just wants
to know "am I getting what I pay for, and what do I do if not?".

Same rules underneath as everything else: scheduled tests only (analytics.basis), tests the monitor itself might
have held down are set aside (quality labels), and dropouts are counted on their own rather than as zero-speed
tests. Only the package in force at the end of the period is judged, so a period that spans an upgrade isn't
judged against the wrong speed. No statistics jargon reaches the page: the wording is built here so it can be
tested.
"""
from __future__ import annotations

from datetime import date

from . import analytics, timeutil

PERIODS = {"7d": ("this week", "the past week"), "30d": ("this month", "the past month"),
           "90d": ("in the last 3 months", "the last 3 months")}
MIN_CLEAN = 10                 # fewer clean tests than this: no verdict yet
OFTEN_SLOW = 5.0               # % of tests below the package's "degraded" line before it counts as a problem
MOSTLY_SLOW = 25.0             # ... and before it's "well below"
DROPOUT_MINUTES = 60           # this much time without internet in the period is worth raising
DROPOUT_COUNT = 3


def mbps(v: float | None) -> str:
    return "?" if v is None else f"{v:,.0f}"


def clock(ms: int, tz: str) -> str:
    """'Sun 12 Oct, 3:51pm'"""
    d = timeutil.local(ms, tz)
    return f"{d:%a} {d.day} {d:%b}, {hour(d.hour * 60 + d.minute)}"


def hour(minute: int) -> str:
    """minutes after midnight -> '8pm', '8:30pm', 'midnight', 'noon'"""
    minute %= 1440
    if minute == 0:
        return "midnight"
    if minute == 720:
        return "noon"
    h, m = divmod(minute, 60)
    return f"{(h % 12) or 12}{f':{m:02d}' if m else ''}{'am' if h < 12 else 'pm'}"


def day_name(iso_day: str) -> str:
    d = date.fromisoformat(iso_day)
    return f"{d:%a} {d.day} {d:%b}"


def part_of_day(minute: int) -> str:
    h = minute // 60
    return "mornings" if 5 <= h < 12 else "afternoons" if 12 <= h < 17 else "evenings" if 17 <= h < 23 else "late nights"


def slow_times(tests: list, s: dict, pk: dict) -> dict:
    """When it's slow, from the recurring-slowdown detector (which needs two weeks before it says anything). A slow
    time only counts as a problem when the speed then is below the package's target: a dip from 900 to 600 Mbps on
    a 132 Mbps package is worth mentioning, not complaining about."""
    c = analytics.congestion(tests, s)
    if c["status"] == "insufficient":
        return {"known": False, "findings": [], "short": None,
                "text": "We need about two weeks of tests before we can tell whether it's slower at certain times of day."}
    if not c["findings"]:
        return {"known": True, "findings": [], "short": None,
                "text": "There's no regular slow time. Your speed is about the same all day."}
    found = sorted(c["findings"], key=lambda f: -(f["drop_pct"] or 0))
    problems = [f for f in found if f["median_in"] < pk["target_down"]]
    parts = []
    for f in problems or found:
        days = "weekday" if f["daytype"] == "weekday" else "weekend"
        parts.append(f"{days} {part_of_day(f['start_min'])}, {hour(f['start_min'])} to {hour(f['end_min'])} "
                     f"(about {mbps(f['median_in'])} Mbps, against {mbps(f['median_out'])} Mbps the rest of the day)")
    if problems:
        text = "It's slowest on " + "; and on ".join(parts) + "."
    else:
        enough = "more than" if min(f["median_in"] for f in found) >= pk["advertised_down"] else "close to"
        text = f"It's a little slower on {'; and on '.join(parts)}, but that's still {enough} what you pay for."
    return {"known": True, "problem": bool(problems),
            "findings": [{"daytype": f["daytype"], "start_min": f["start_min"], "end_min": f["end_min"],
                          "typical_mbps": f["median_in"], "rest_mbps": f["median_out"]} for f in (problems or found)],
            "short": part_of_day(problems[0]["start_min"]) if problems else None, "text": text}


def dropouts(outages: list, tz: str, when: str) -> dict:
    """Confirmed outages from the probes. Each lasted somewhere between its two bounds; 'about' is the middle."""
    mins = []
    for o in outages:
        lo, hi = o["min_duration_ms"], o["max_duration_ms"] or o["min_duration_ms"]
        mins.append(max(1, round((lo + hi) / 2 / 60_000)))
    total = sum(mins)
    if not outages:
        return {"count": 0, "minutes": 0, "longest": None, "text": f"Your internet didn't drop out {when}."}
    longest = max(range(len(outages)), key=lambda i: mins[i])
    times = "once" if len(outages) == 1 else "twice" if len(outages) == 2 else f"{len(outages)} times"
    text = f"Your internet dropped out {times} {when}, for about {_minutes(total)} in total."
    if len(outages) > 1:
        text += f" The longest was about {_minutes(mins[longest])}, on {clock(outages[longest]['first_ms'], tz)}."
    else:
        text = f"Your internet dropped out once {when}, for about {_minutes(total)}, on {clock(outages[0]['first_ms'], tz)}."
    return {"count": len(outages), "minutes": total,
            "longest": {"minutes": mins[longest], "t_ms": outages[longest]["first_ms"]}, "text": text}


def _minutes(m: int) -> str:
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, r = divmod(m, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {r} minute{'s' if r != 1 else ''}" if r else "")


def promise(clean: list, pk: dict, s: dict, today: str) -> dict:
    """Virgin's minimum guaranteed speed, in plain words, with the 3-days-in-a-row rule."""
    g = analytics.guarantee_watch(clean)
    if not pk.get("minimum_guaranteed_down"):
        nxt = next((p for p in sorted(s.get("packages") or [], key=lambda p: p["from"])
                    if p["from"] > today and p.get("minimum_guaranteed_down")), None)
        text = "Your current package doesn't come with a guaranteed minimum speed."
        if nxt:
            text += (f" From {day_name(nxt['from'])}, your new package guarantees at least "
                     f"{mbps(nxt['minimum_guaranteed_down'])} Mbps, and this section will start checking it.")
        return {"applies": False, "text": text, "can_claim": False}
    mg = pk["minimum_guaranteed_down"]
    if not g.get("applies"):
        return {"applies": True, "mgds": mg, "days_below": 0, "can_claim": False,
                "text": f"Virgin guarantees at least {mbps(mg)} Mbps. There are no tests to check it against yet."}
    n_days, below = g["days_with_data"], g["days_below"]
    text = (f"Virgin guarantees at least {mbps(mg)} Mbps. You got less than that on {below} of the {n_days} "
            f"day{'s' if n_days != 1 else ''} we tested.")
    runs = g["qualifying"]
    cur = g["current"]
    out = {"applies": True, "mgds": mg, "days_below": below, "days_tested": n_days, "can_claim": bool(runs),
           "run_days": max((r["days"] for r in runs), default=(cur or {}).get("days", 0))}
    if runs:
        r = max(runs, key=lambda r: r["days"])
        out["claim_text"] = (f"It was below that on {r['days']} days in a row ({day_name(r['first'])} to {day_name(r['last'])}). "
                             "That means you can make a complaint under Virgin's guarantee. If they can't fix it within 30 "
                             "days of you telling them, you can leave your contract without paying a fee.")
    elif cur:
        out["claim_text"] = (f"It's been below for {cur['days']} day{'s' if cur['days'] != 1 else ''} in a row so far. If it "
                             "reaches 3 days in a row, you can make a complaint under Virgin's guarantee.")
    out["text"] = text
    return out


def build(tests: list, outages: list, s: dict, preset: str, now_ms: int) -> dict:
    tz = s["timezone"]
    when, period_words = PERIODS.get(preset, ("in this period", "this period"))
    pk = (tests[-1].pk if tests and tests[-1].pk else None) or s["package"]
    on = [x for x in tests if x.pk and x.pk.get("from") == pk.get("from")]
    since = day_name(pk["from"]) if on and on[0] is not tests[0] else None      # the period spans a package change
    ok = [x for x in analytics.basis(on) if x.ok]
    clean = [x for x in ok if x.clean]
    dls = sorted(x.dl for x in clean)
    uls = sorted(x.ul for x in clean if x.ul is not None)
    typical = analytics.percentile(dls, 50)
    slowest = min(clean, key=lambda x: x.dl) if clean else None
    adv, tgt, deg = pk["advertised_down"], pk["target_down"], pk["degraded_down"]
    pct_slow = analytics.share(sum(1 for v in dls if v < deg), len(dls)) or 0.0
    today = timeutil.local(now_ms, tz).date().isoformat()
    # the slow-times detector reads the clean tests only (and failures, which it ignores), like everything here
    slow = slow_times([x for x in on if not x.ok or x.clean], s, pk) if clean else {"known": False, "findings": [], "short": None, "text": ""}
    drop = dropouts(outages, tz, when)
    prom = promise([x for x in on if x.ok and x.clean], pk, s, today)

    out = {"package": {"name": pk["name"], "advertised_down": adv, "advertised_up": pk["advertised_up"],
                       "minimum_guaranteed_down": pk.get("minimum_guaranteed_down")},
           "since": since, "period": period_words,
           "tests": len(ok), "set_aside": len(ok) - len(clean),
           "typical_down": typical, "typical_up": analytics.percentile(uls, 50),
           "typical_pct": (100 * typical / adv) if typical else None,
           "slowest": {"mbps": slowest.dl, "t_ms": slowest.t, "when": clock(slowest.t, tz)} if slowest else None,
           "pct_slow": pct_slow, "slow_times": slow, "dropouts": drop, "promise": prom}

    # ------------------------------------------------------------ the one-line answer
    if len(clean) < MIN_CLEAN:
        have = ("There are no usable tests" if not clean else
                f"So far there {'is' if len(clean) == 1 else 'are'} only {len(clean)} usable test{'s' if len(clean) != 1 else ''}")
        out.update(level="unknown", verdict="Not enough tests yet",
                   detail=f"We need a few more tests before we can give you an answer. {have} for {period_words}.")
    elif prom.get("can_claim"):
        out.update(level="bad", verdict="No. Your speed has dropped below Virgin's guaranteed minimum",
                   detail=f"On {prom['run_days']} days in a row you got less than the {mbps(prom['mgds'])} Mbps Virgin promises.")
    elif typical < deg or pct_slow >= MOSTLY_SLOW:
        out.update(level="bad", verdict="No. Your speed is well below what you pay for",
                   detail=f"You pay for {mbps(adv)} Mbps but usually get about {mbps(typical)} Mbps.")
    elif typical < tgt:
        out.update(level="warn", verdict="Not quite. You usually get less than you pay for",
                   detail=f"You pay for {mbps(adv)} Mbps and usually get about {mbps(typical)} Mbps.")
    elif slow["short"]:
        out.update(level="warn", verdict=f"Mostly, but it slows down in the {slow['short']}",
                   detail=f"Most of the time you get about {mbps(typical)} Mbps, but it drops at certain times of day.")
    elif pct_slow >= OFTEN_SLOW:
        out.update(level="warn", verdict="Mostly, but it's often slow",
                   detail=f"You usually get about {mbps(typical)} Mbps, but {pct_slow:.0f}% of tests were well below that.")
    elif drop["minutes"] >= DROPOUT_MINUTES or drop["count"] >= DROPOUT_COUNT:
        out.update(level="warn", verdict=("Your speed is fine, but your internet keeps dropping out" if drop["count"] >= DROPOUT_COUNT
                                           else f"Your speed is fine, but your internet was off for over {drop['minutes'] // 60} hour{'s' if drop['minutes'] >= 120 else ''}"),
                   detail=f"You usually get about {mbps(typical)} Mbps. {drop['text']}")
    else:
        share = "all of it" if typical >= adv else f"{min(99, 100 * typical / adv):.0f}% of it"
        out.update(level="good", verdict="Yes. You're getting what you pay for",
                   detail=f"You pay for {mbps(adv)} Mbps and usually get about {mbps(typical)} Mbps ({share}).")

    out["steps"] = steps(out, pk, when)
    return out


def steps(r: dict, pk: dict, when: str) -> dict:
    """What to do now: nothing, or how to make a complaint and where to go if it isn't sorted."""
    if r["level"] == "unknown":
        return {"action": False, "intro": "Nothing to do yet. Tests run on their own, so check back in a day or two.", "list": []}
    if r["level"] == "good":
        return {"action": False, "intro": "Nothing to do. Keep the monitor running, so you have a record if things change.", "list": []}
    adv, typ = mbps(pk["advertised_down"]), mbps(r["typical_down"])
    say = f"I pay for {adv} Mbps broadband, but my own tests show I usually get about {typ} Mbps."
    if r["promise"].get("can_claim"):
        say = (f"My download speed has been below your minimum guaranteed speed of {mbps(r['promise']['mgds'])} Mbps on "
               f"{r['promise']['run_days']} days in a row. I'd like to make a complaint under the guarantee.")
    elif r["slow_times"]["short"] and r["typical_down"] >= pk["target_down"]:
        f = r["slow_times"]["findings"][0]
        say = (f"My broadband slows down to about {mbps(f['typical_mbps'])} Mbps between {hour(f['start_min'])} and "
               f"{hour(f['end_min'])} on {'weekdays' if f['daytype'] == 'weekday' else 'weekends'}. I pay for {adv} Mbps.")
    elif r["level"] == "warn" and r["dropouts"]["count"] and r["typical_down"] >= pk["target_down"]:
        say = f"My internet keeps dropping out. {r['dropouts']['text']}"
    say += (f" I have a record of {r['tests']:,} automatic tests from {r['period']}, and I can send it to you. "
            "Please fix it and tell me what you'll do and by when.")
    return {"action": True, "intro": "Here's how to get it sorted.", "say": say, "list": [
        "Download your evidence (the button below). It shows every test, so it can't be brushed off as a one-off.",
        "Contact Virgin Media by phone, web chat or the My Virgin Media app, and tell them what's in the box below.",
        "Write down the date, the name of the person you spoke to and any reference number they give you.",
        "If it isn't sorted after 8 weeks, or Virgin writes to say they can't do any more (a \"deadlock letter\"), take it "
        "to the ombudsman, CISAS. It's free, and if you accept their decision, Virgin has to follow it.",
    ]}
