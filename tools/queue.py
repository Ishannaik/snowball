#!/usr/bin/env python3
"""Topic queue for the research loop (topics.yml).

queue.py pick N                 print up to N "slug public|private" lines (queued first, else suggested)
queue.py prompt SLUG OUT MIN    print the filled research prompt (env EXA_LEFT caps web_search calls)
queue.py done SLUG REPORT       mark done, append up to 2 SUGGEST lines (max 5 new per day)
queue.py budget get             print today's budget counters as JSON
queue.py budget add FIELD N     add N to a counter (topic_runs, exa, suggestions)
"""

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "topics.yml"
BUDGET = ROOT / "state/budget.json"
MAX_SUGGESTIONS_PER_DAY = 5
DEEPEN_AFTER_DAYS = 2  # a done topic can be deepened once its last run is this old
HEADER = """# Research queue. run.sh picks status: queued topics in order (lowest priority number first).
# status: queued | done | suggested | skip.  private: true = Qwen lanes only, never Gemma (TMJ topic).
# The agent appends up to 2 "suggested" topics per run. Suggested topics run only when
# nothing is queued. Promote one by changing its status to queued.
"""
TMJ_EXTRA = (
    "This topic is health related. Label every finding as anecdotal. Do not give medical advice "
    "or instructions. Put this line under the title: '> Anecdotal forum reports, not medical advice.'"
)


def load():
    return yaml.safe_load(QUEUE.read_text()) or {"topics": []}


def save(d):
    QUEUE.write_text(HEADER + yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=100))


def find(d, slug):
    for t in d["topics"]:
        if t["slug"] == slug:
            return t
    sys.exit(f"no topic {slug}")


def budget_load():
    try:
        b = json.loads(BUDGET.read_text())
    except (OSError, ValueError):
        b = {}
    if b.get("day") != dt.date.today().isoformat():
        b = {"day": dt.date.today().isoformat(), "topic_runs": 0, "exa": 0, "suggestions": 0}
    return b


def budget_add(field, n):
    b = budget_load()
    b[field] = b.get(field, 0) + int(n)
    BUDGET.parent.mkdir(exist_ok=True)
    BUDGET.write_text(json.dumps(b))
    return b


def gaps_of(slug):
    """'Gaps and open questions' section of the newest report for slug (raw/*/slug.md)."""
    reports = sorted(ROOT.glob(f"raw/*/{slug}.md"))
    if not reports:
        return ""
    m = re.search(r"^##\s*Gaps[^\n]*\n(.*?)(?=^##\s)", reports[-1].read_text(errors="replace"), re.M | re.S)
    return " ".join(m.group(1).split())[:1500] if m else ""


def make_deepen(d):
    """Queue empty: add one 'deepen' topic that re-researches the gaps of the oldest done topic.
    It updates the same wiki pages instead of opening a new subject."""
    today = dt.date.today()
    cands = []
    for t in d["topics"]:
        if t.get("status") != "done" or t.get("deepen_of"):
            continue
        last = dt.date.fromisoformat(t.get("last_deepened") or t.get("last_run") or "2000-01-01")
        if (today - last).days >= DEEPEN_AFTER_DAYS and gaps_of(t["slug"]):
            cands.append((last, t))
    if not cands:
        return None
    _, base = min(cands, key=lambda x: x[0])
    base["last_deepened"] = today.isoformat()
    slug = f"{base['slug']}-deep-{today.strftime('%m%d')}"
    t = {
        "slug": slug,
        "priority": 40,
        "status": "queued",
        "deepen_of": base["slug"],
        "title": f"{base['title']} (deepening: open gaps)",
        "focus": "Fill the gaps the last report left open. Re-check its claims that changed since. Gaps: "
        + gaps_of(base["slug"]),
        "forums": base.get("forums", []),
    }
    if base.get("private"):
        t["private"] = True
    d["topics"].append(t)
    save(d)
    return t


def pick(n):
    """Order: queued topics; then suggested follow-ups of existing topics (they deepen pages that
    already exist) before other suggestions; then a new 'deepen' topic for the oldest done topic."""
    d = load()
    by_prio = sorted(d["topics"], key=lambda t: t.get("priority", 99))
    queued = [t for t in by_prio if t.get("status") == "queued"]
    known = {t["slug"] for t in d["topics"]}
    sugg = [t for t in by_prio if t.get("status") == "suggested"]
    sugg.sort(key=lambda t: (t.get("suggested_by") not in known, t.get("priority", 99)))
    pool = queued or sugg
    if not pool:
        t = make_deepen(d)
        pool = [t] if t else []
    for t in pool[:n]:
        print(t["slug"], "private" if t.get("private") else "public")


def prompt(slug, out, minutes):
    t = find(load(), slug)
    text = (ROOT / "prompts/research.md").read_text()
    health = any(w in (t["title"] + t.get("focus", "")).lower() for w in ("tmj", "medical", "health", "jaw"))
    # private marks the TMJ lane. The anecdote line must not depend on the title containing a health word.
    anecdotal = bool(t.get("private")) or health
    fills = {
        "TITLE": t["title"],
        "FOCUS": " ".join((t.get("focus") or "").split()),
        "SLUG": slug,
        "FORUMS": ", ".join(t.get("forums") or []) or "pick the most relevant ones",
        "DATE": dt.date.today().isoformat(),
        "OUT": out,
        "MINUTES": minutes,
        "FORUM_PY": str(ROOT / "tools/forum.py"),
        "EXTRA": TMJ_EXTRA if anecdotal else "",
        "EXA_LEFT": os.environ.get("EXA_LEFT", "10"),
    }
    for k, v in fills.items():
        text = text.replace("{{" + k + "}}", str(v))
    print(text)


def done(slug, report):
    d = load()
    t = find(d, slug)
    t["status"] = "done"
    t["last_run"] = dt.date.today().isoformat()
    known = {x["slug"] for x in d["topics"]}
    added = 0
    room = max(0, MAX_SUGGESTIONS_PER_DAY - budget_load().get("suggestions", 0))
    body = Path(report).read_text(errors="replace") if Path(report).exists() else ""
    for m in re.finditer(r"^\s*-\s*SUGGEST:\s*([a-z0-9][a-z0-9-]{2,60})\s*\|\s*(.+?)\s*$", body, re.M):
        s, title = m.group(1), m.group(2)
        if s in known or added >= min(2, room):
            continue
        row = {
            "slug": s,
            "priority": 50,
            "status": "suggested",
            "title": title,
            "focus": f"Follow-up suggested by the {slug} run on {t['last_run']}.",
            "suggested_by": slug,
        }
        # A private run must not mint a public follow-up. pick() would otherwise
        # print it as public and the research lane would leave the box.
        if t.get("private"):
            row["private"] = True
        d["topics"].append(row)
        known.add(s)
        added += 1
    save(d)
    if added:
        budget_add("suggestions", added)
    print(f"marked {slug} done, {added} suggestion(s) added")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "pick":
        pick(int(sys.argv[2]))
    elif cmd == "prompt":
        prompt(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "budget" and sys.argv[2] == "get":
        print(json.dumps(budget_load()))
    elif cmd == "budget" and sys.argv[2] == "add":
        print(json.dumps(budget_add(sys.argv[3], sys.argv[4])))
    elif cmd == "done":
        done(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
