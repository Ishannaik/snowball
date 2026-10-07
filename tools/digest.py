#!/usr/bin/env python3
"""Snowball Daily Digest & Alert Engine.
Reads unnotified opportunities, deadline changes, and verified claims from data/snowball.db,
computes urgency scores (<=7 days urgent alerts), compiles digests/YYYY-MM-DD.md,
and optionally posts to Discord.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
CONFIG_PATH = ROOT / "config.env"
DIGEST_DIR = ROOT / "digests"


def get_channel_id() -> str:
    """Retrieve DIGEST_CHANNEL_ID from config.env or environment."""
    ch = os.environ.get("DIGEST_CHANNEL_ID", "").strip()
    if not ch and CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DIGEST_CHANNEL_ID="):
                ch = line.split("=", 1)[1].strip().strip("'\"")
    return ch


def compute_urgency_badge(deadline_str: str | None) -> tuple[str, int]:
    """Calculate days to deadline and format urgency badge."""
    if not deadline_str:
        return ("Rolling", 999)
    try:
        dl = dt.date.fromisoformat(deadline_str[:10])
        today = dt.date.today()
        days = (dl - today).days
        if days < 0:
            return ("Expired", -1)
        if days <= 7:
            return (f"🚨 URGENT ({days}d left)", days)
        if days <= 30:
            return (f"⏳ {days}d left", days)
        return (f"📅 {dl.isoformat()}", days)
    except Exception:
        return (deadline_str, 999)


def compile_daily_digest(conn: sqlite3.Connection) -> tuple[str, int]:
    """Compile digest markdown from unnotified entities and entity changes."""
    cur = conn.cursor()
    today_str = dt.date.today().isoformat()

    # 1. Unreported New Entities
    cur.execute("""
        SELECT key, name, org, kind, amount, currency, deadline, eligibility, link, evidence_quote
        FROM entities
        WHERE reported_at IS NULL OR reported_at = 0
        ORDER BY deadline ASC
    """)
    new_entities = [dict(r) for r in cur.fetchall()]

    # 2. Unreported Entity Changes (e.g. deadline moved)
    cur.execute("""
        SELECT ec.id, ec.entity_key, ec.field, ec.old, ec.new, e.name, e.org, e.link
        FROM entity_changes ec
        JOIN entities e ON ec.entity_key = e.key
        WHERE ec.reported_at IS NULL OR ec.reported_at = 0
    """)
    changes = [dict(r) for r in cur.fetchall()]

    # 3. New Claims
    cur.execute("""
        SELECT c.hash, c.text, c.n_sources, p.canon_url
        FROM claims c
        LEFT JOIN claim_sources cs ON c.hash = cs.claim_hash
        LEFT JOIN pages p ON cs.page_id = p.id
        ORDER BY c.first_seen_at DESC
        LIMIT 10
    """)
    recent_claims = [dict(r) for r in cur.fetchall()]

    total_items = len(new_entities) + len(changes)

    lines = [
        f"# ❄️ Snowball Research Digest — {today_str}",
        "**Autonomous 24/7 Opportunity & Intelligence Briefing**",
        f"**Summary:** {len(new_entities)} new opportunities, {len(changes)} modified programs.\n",
    ]

    # Section 1: New Opportunities
    if new_entities:
        lines.append("## 🎯 Discovered Opportunities & Grants\n")
        for e in new_entities:
            badge, _ = compute_urgency_badge(e.get("deadline"))
            amt = f"{e.get('currency', '')} {e.get('amount', '')}".strip() or "Variable Amount"
            lines.append(f"### {e['name']} ({e['org']}) — {amt}")
            lines.append(f"- **Status / Deadline:** {badge}")
            lines.append(f"- **Category:** {e.get('kind', 'Opportunity')}")
            lines.append(f"- **Eligibility:** {e.get('eligibility', 'Open')}")
            if e.get("link"):
                lines.append(f"- **Source / Apply:** [{e['link']}]({e['link']})")
            if e.get("evidence_quote"):
                lines.append(f'> "{e["evidence_quote"][:300]}"')
            lines.append("")
    else:
        lines.append("## 🎯 Discovered Opportunities & Grants")
        lines.append("*No new unnotified opportunities discovered since last digest.*\n")

    # Section 2: Modified Programs
    if changes:
        lines.append("## 🔄 Modified Deadlines & Terms\n")
        for c in changes:
            lines.append(f"- **{c['name']}** ({c['org']}): `{c['field']}` updated from `{c['old']}` ➔ `{c['new']}`")
            lines.append(f"  Source: [{c['link']}]({c['link']})")
        lines.append("")

    # Section 3: Verified Claims
    if recent_claims:
        lines.append("## 🔍 Verified Intelligence & Alpha Claims\n")
        for rc in recent_claims:
            url_part = f" ([source]({rc['canon_url']}))" if rc.get("canon_url") else ""
            lines.append(f"- {rc['text']}{url_part}")
        lines.append("")

    digest_text = "\n".join(lines)

    # Mark reported in DB
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    cur.execute("UPDATE entities SET reported_at = ? WHERE reported_at IS NULL OR reported_at = 0", (now_ts,))
    cur.execute("UPDATE entity_changes SET reported_at = ? WHERE reported_at IS NULL OR reported_at = 0", (now_ts,))
    conn.commit()

    return digest_text, total_items


def save_digest_file(text: str) -> Path:
    """Save digest markdown to digests/YYYY-MM-DD.md."""
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    today_str = dt.date.today().isoformat()
    out_file = DIGEST_DIR / f"{today_str}.md"
    out_file.write_text(text, encoding="utf-8")
    return out_file


def post_to_discord(channel_id: str, content: str) -> bool:
    """Post digest markdown to Discord channel if token available."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if webhook_url:
        payload = json.dumps({"content": content[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Snowball/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            print(f"Discord webhook post failed: {e}")
            return False

    if token and channel_id:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        payload = json.dumps({"content": content[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "Snowball/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            print(f"Discord bot post failed: {e}")
            return False

    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Daily Digest Generator")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    ap.add_argument("--channel", type=str, default="", help="Override Discord channel ID")
    ap.add_argument("--post", action="store_true", help="Post to Discord channel/webhook")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"Database not found at {args.db}. Nothing to digest.")
        return

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    digest_text, count = compile_daily_digest(conn)
    conn.close()

    out_file = save_digest_file(digest_text)
    print(f"\n❄️  Digest compiled ({count} items) -> {out_file}\n")
    print(digest_text)

    channel = args.channel or get_channel_id()
    if args.post and channel:
        posted = post_to_discord(channel, digest_text)
        print(f"Posted to Discord channel {channel}: {'Success' if posted else 'Skipped/Failed'}")


if __name__ == "__main__":
    main()
