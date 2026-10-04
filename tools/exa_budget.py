#!/usr/bin/env python3
"""Exa use gate and burn tracker for the research loop.

Ishan, 2 Oct 2026 18:08: no daily Exa cap. The loop uses Exa only while the rotator has more
than RESERVE live keys, so Claude Code, Hermes and omp keep some search.

Reads (never writes) the exa_rotator pool at ~/.exa/keys.json, with the same rule as
journal/scripts/exa_rotator.py live_indices(): a key is parked while now - exhausted_at is
under its cooldown (6 h, or 7 days for a rejected key). Also reads omp's own Exa pool state
(~/.omp/agent/exa-rotate-state.json), because omp's web_search draws from that pool.

  exa_budget.py status                 {"live", "omp_live", "use_exa"}
  exa_budget.py track SEARCHES TOPICS  add this run's loop searches/topics, append EXA_BURN.md
                                       lines for keys that went spent/parked since the last call,
                                       and write the summary once the 5th key is gone.
"""
import json
import time
from pathlib import Path

RESERVE = 3
ROOT = Path(__file__).resolve().parent.parent
BURN_MD = ROOT / "EXA_BURN.md"
BURN_STATE = ROOT / "state/exa_burn.json"
ROTATOR = Path.home() / ".exa/keys.json"
OMP_KEYS = Path.home() / ".omp/agent/exa-keys.json"
OMP_STATE = Path.home() / ".omp/agent/exa-rotate-state.json"
COOLDOWN = 6 * 3600
INVALID_COOLDOWN = 7 * 24 * 3600
INVALID_REASONS = ("key rejected", "http 401", "http 403")


def rotator_keys():
    """[(label, parked: bool, exhausted_at, reason)] without the secret."""
    try:
        keys = json.loads(ROTATOR.read_text()).get("keys", [])
    except (OSError, ValueError):
        return []
    now, out = time.time(), []
    for n, e in enumerate(keys):
        spent = e.get("exhausted_at")
        cd = INVALID_COOLDOWN if e.get("last_reason") in INVALID_REASONS else COOLDOWN
        parked = bool(spent) and now - float(spent) < cd
        out.append((e.get("label") or f"key{n}", parked, spent, e.get("last_reason")))
    return out


def rotator_live() -> int:
    return sum(1 for k in rotator_keys() if not k[1])


def omp_live() -> int:
    try:
        n = len(json.loads(OMP_KEYS.read_text()).get("keys", []))
        cool = json.loads(OMP_STATE.read_text()).get("cooldownUntil", {})
    except (OSError, ValueError):
        return 0
    now_ms = time.time() * 1000
    return n - sum(1 for v in cool.values() if float(v) > now_ms)


def track(searches: int, topics: int) -> None:
    now = time.time()
    try:
        st = json.loads(BURN_STATE.read_text())
    except (OSError, ValueError):
        st = {}
    keys = rotator_keys()
    if not st:  # first call: baseline. Keys already parked now are not counted as loop burn.
        st = {"start": now, "searches": 0, "topics": 0, "baseline_parked": [k[0] for k in keys if k[1]],
              "gone": [], "summary": None, "summary_in_digest": False}
        BURN_MD.write_text("# Exa burn log (append-only)\n\nStarted "
                           f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} IST with {len(keys)} keys, "
                           f"{len(st['baseline_parked'])} already parked. Searches = loop web_search calls only; "
                           "other users of the pool (Claude Code, Hermes, omp) also burn keys.\n\n")
    st["searches"] += searches
    st["topics"] += topics
    lines = []
    for label, parked, spent, reason in keys:
        if parked and label not in st["gone"] and label not in st["baseline_parked"]:
            st["gone"].append(label)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(spent))) if spent else "?"
            lines.append(f"- {when} key `{label}` parked ({reason or 'spent'}). Gone since start: {len(st['gone'])}. "
                         f"Loop searches so far: {st['searches']}. Topics so far: {st['topics']}. Live now: "
                         f"{sum(1 for k in keys if not k[1])}.\n")
    if len(st["gone"]) >= 5 and not st["summary"]:
        hours = (now - st["start"]) / 3600
        gone = len(st["gone"])
        live = sum(1 for k in keys if not k[1])
        hpk = hours / gone
        st["summary"] = (f"SUMMARY after {gone} keys gone: {hours:.1f} h elapsed, {st['searches'] / gone:.0f} loop "
                         f"searches per key, {st['topics'] / gone:.1f} topics per key. At {hpk:.1f} h per key the "
                         f"{live} live keys last about {live * hpk / 24:.1f} days (counting the {RESERVE}-key reserve "
                         f"the loop leaves: {max(0, live - RESERVE) * hpk / 24:.1f} days of loop use).")
        lines.append(st["summary"] + "\n")
    if lines:
        with BURN_MD.open("a") as f:
            f.writelines(lines)
    BURN_STATE.parent.mkdir(exist_ok=True)
    BURN_STATE.write_text(json.dumps(st))


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "track":
        track(int(sys.argv[2]), int(sys.argv[3]))
    live, ol = rotator_live(), omp_live()
    print(json.dumps({"live": live, "omp_live": ol, "use_exa": live > RESERVE and ol > RESERVE}))
