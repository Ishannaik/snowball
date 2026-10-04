#!/usr/bin/env python3
"""Build the research digest; write it to digests/, optionally post it to Discord as the Mirror bot.

  digest.py RUN_JSON [--prefix "TEST:"] [--dry-run]
  digest.py --daily [--dry-run]     merge every non-test run JSON from the last 24 h (08:00 IST timer)

Always writes digests/YYYY-MM-DD.md. Posts to Discord only when DIGEST_CHANNEL_ID is set in
config.env (empty by default; Ishan, 2 Oct 2026: no digests in his main DM).

RUN_JSON: {"date": "...", "topics": [{"slug","title","report","ok","model"}], "created": [..],
           "updated": [..], "commit": "sha", "notes": [..]}
Findings are taken from each report's Summary bullets, with their [n] citations resolved
to URLs from the report's Sources list. No LLM call. The token is read from the env file and
never printed.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ENV = Path("/home/ubuntu/.claude/channels/discord/.env")
CONFIG = Path(__file__).resolve().parent.parent / "config.env"
DIGEST_DIR = Path(__file__).resolve().parent.parent / "digests"


def channel_id() -> str:
    """DIGEST_CHANNEL_ID from config.env (or the environment). Empty means do not post."""
    val = os.environ.get("DIGEST_CHANNEL_ID", "")
    if not val and CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            if line.strip().startswith("DIGEST_CHANNEL_ID="):
                val = line.split("=", 1)[1].strip().strip("'\"")
    return val
UA = "DiscordBot (https://github.com/Ishannaik, 1.0)"


def sources(text):
    out = {}
    for m in re.finditer(r"^\s*(?:-\s*)?\[(\d+)\][^\n]*?(https?://\S+)", text, re.M):
        out[m.group(1)] = m.group(2).rstrip(").,>")
    return out


def findings(report_path, n=3):
    p = Path(report_path)
    if not p.exists():
        return []
    text = p.read_text(errors="replace")
    srcs = sources(text)
    summ = re.search(r"^##\s*Summary\s*$(.*?)^##\s", text, re.M | re.S)
    out = []
    for line in (summ.group(1) if summ else "").splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        cites = re.findall(r"\[(\d+)\]", line)
        url = next((srcs[c] for c in cites if c in srcs), None)
        clean = re.sub(r"\s*\[\d+\]", "", line.lstrip("-* ")).strip()
        clean = re.sub(r"\s*—\s*", ", ", clean).replace("–", "-").replace("**", "")
        if url and clean:
            out.append(f"{clean[:220]} <{url}>")
        if len(out) >= n:
            break
    return out


def build(run, prefix):
    ok = [t for t in run["topics"] if t.get("ok")]
    bad = [t for t in run["topics"] if not t.get("ok")]
    head = "Research wiki, last 24h" if run.get("daily") else f"Research wiki run {run['date']}"
    lines = [f"{prefix}{head}".strip()]
    if run.get("daily") and not run["topics"]:
        lines.append("No research runs finished in the last 24 hours.")
    lines.append("Topics done: " + (", ".join(t["slug"] for t in ok) or "none") + ".")
    if bad:
        lines.append("Topics failed: " + ", ".join(t["slug"] for t in bad) + ".")
    lines.append(f"Wiki pages: {len(run.get('created', []))} created, {len(run.get('updated', []))} updated."
                 + (f" Commit {run['commit'][:8]}." if run.get("commit") else ""))
    picks = []
    for t in ok:
        picks += findings(t["report"], 3)
    if picks:
        lines.append("Findings:")
        lines += [f"{i}. {f}" for i, f in enumerate(picks[:3], 1)]
    for note in run.get("notes", [])[-4:]:
        lines.append(f"Note: {note}")
    msg = re.sub(r"\s*—\s*", ", ", "\n".join(lines))
    return msg[:1990]


def token():
    for line in ENV.read_text().splitlines():
        if line.strip().upper().startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("no DISCORD_BOT_TOKEN")


def post(msg, channel):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(json.dumps({"content": msg}))
        body = f.name
    with tempfile.NamedTemporaryFile("w", delete=False) as h:  # header file keeps the token out of argv
        h.write(f"Authorization: Bot {token()}\n")
        hdr = h.name
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "30", "-X", "POST", "-H", f"@{hdr}", "-H", "Content-Type: application/json",
             "-H", f"User-Agent: {UA}", "--data", f"@{body}",
             f"https://discord.com/api/v10/channels/{channel}/messages"],
            capture_output=True, text=True, timeout=45)
    finally:
        Path(hdr).unlink(missing_ok=True)
        Path(body).unlink(missing_ok=True)
    try:
        mid = json.loads(r.stdout).get("id")
    except ValueError:
        mid = None
    if not mid:
        sys.exit(f"discord post failed: {r.stdout[:200]}")
    print(f"posted message {mid}")


def merge_daily():
    import time
    logs = Path(__file__).resolve().parent.parent / "logs"
    run = {"daily": True, "date": time.strftime("%Y-%m-%d"), "topics": [], "created": [], "updated": [], "notes": []}
    for f in sorted(logs.glob("run-*.json")):
        if time.time() - f.stat().st_mtime > 24 * 3600:
            continue
        d = json.loads(f.read_text())
        if d.get("test"):
            continue
        run["topics"] += d.get("topics", [])
        run["created"] += [x for x in d.get("created", []) if x not in run["created"]]
        run["updated"] += [x for x in d.get("updated", []) if x not in run["updated"] and x not in run["created"]]
        run["notes"] += d.get("notes", [])
        run["commit"] = d.get("commit", run.get("commit"))
    # Exa burn summary (written by exa_budget.py once 5 keys are gone): include it once.
    burn = logs.parent / "state/exa_burn.json"
    try:
        b = json.loads(burn.read_text())
    except (OSError, ValueError):
        b = {}
    if b.get("summary") and not b.get("summary_in_digest"):
        run["notes"].append("Exa burn: " + b["summary"])
        if "--dry-run" not in sys.argv:
            b["summary_in_digest"] = True
            burn.write_text(json.dumps(b))
    return run


if __name__ == "__main__":
    args = sys.argv[1:]
    prefix = ""
    if "--prefix" in args:
        prefix = args[args.index("--prefix") + 1].rstrip() + " "
    run = merge_daily() if args[0] == "--daily" else json.loads(Path(args[0]).read_text())
    msg = build(run, prefix)
    if "--dry-run" in args:
        print(msg)
        sys.exit(0)
    DIGEST_DIR.mkdir(exist_ok=True)
    name = run["date"] + ("" if run.get("daily") else f"-run-{run.get('stamp', 'x')}") + ".md"
    (DIGEST_DIR / name).write_text(msg + "\n")
    print(f"wrote digests/{name}")
    ch = channel_id()
    if ch:
        post(msg, ch)
    else:
        print("DIGEST_CHANNEL_ID empty, not posting")
