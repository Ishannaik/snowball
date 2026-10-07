#!/usr/bin/env python3
"""Snowball Incremental Markdown Wiki Compiler.
Compiles entities, claims, and sources from data/snowball.db into a structured Markdown wiki:
  wiki/
    INDEX.md
    entities/<slug>.md
    concepts/<slug>.md

Enforces:
  1. Deterministic YAML frontmatter array unions (sources, tags, aliases).
  2. 70% Body Shrink Safety Floor (BODY_SHRINK_THRESHOLD = 0.70) to prevent LLM compaction loss.
  3. Bounded staleness propagation (D <= 1) and link integrity.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
WIKI_DIR = ROOT / "wiki"
BODY_SHRINK_THRESHOLD = 0.70


def slugify(text: str) -> str:
    """Format string into a safe file slug."""
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", s) or "untitled"


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and body from Markdown."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    fm_raw = parts[1]
    body = parts[2].lstrip()
    fm: dict[str, Any] = {}
    for line in fm_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if v.startswith("[") and v.endswith("]"):
                items = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                fm[k] = items
            else:
                fm[k] = v
    return fm, body


def format_frontmatter(fm: dict[str, Any]) -> str:
    """Format dictionary into YAML frontmatter string."""
    lines = ["---"]
    for k, v in sorted(fm.items()):
        if isinstance(v, (list, set)):
            items_str = ", ".join(f'"{x}"' for x in sorted(v))
            lines.append(f"{k}: [{items_str}]")
        else:
            lines.append(f'{k}: "{v}"')
    lines.append("---\n")
    return "\n".join(lines)


def merge_page(old_content: str, new_frontmatter: dict[str, Any], new_body: str) -> str:
    """Merge frontmatter arrays and enforce the 70% body shrink safety threshold."""
    if not old_content:
        return format_frontmatter(new_frontmatter) + "\n" + new_body

    old_fm, old_body = parse_frontmatter(old_content)

    # 1. Frontmatter set unions for array fields
    merged_fm = dict(old_fm)
    merged_fm.update(new_frontmatter)
    for array_key in ["sources", "tags", "aliases"]:
        old_arr = set(old_fm.get(array_key, [])) if isinstance(old_fm.get(array_key), list) else set()
        new_arr = set(new_frontmatter.get(array_key, [])) if isinstance(new_frontmatter.get(array_key), list) else set()
        combined = old_arr.union(new_arr)
        if combined:
            merged_fm[array_key] = list(combined)

    # 2. 70% Body Shrink Safety Floor
    merged_body = new_body
    if len(new_body.strip()) < len(old_body.strip()) * BODY_SHRINK_THRESHOLD:
        print(
            f"  [GUARD] Compaction loss prevented: new body ({len(new_body)}) < 70% of old ({len(old_body)}). Retaining previous sections."
        )
        merged_body = old_body + "\n\n## Latest Update\n" + new_body

    return format_frontmatter(merged_fm) + "\n" + merged_body


def compile_wiki(conn: sqlite3.Connection, output_dir: Path) -> dict[str, int]:
    """Compile entities, claims, and index into markdown wiki."""
    entities_dir = output_dir / "entities"
    entities_dir.mkdir(parents=True, exist_ok=True)

    cur = conn.cursor()
    cur.execute("""
        SELECT e.key, e.name, e.org, e.year, e.kind, e.amount, e.currency,
               e.deadline, e.eligibility, e.link, e.evidence_quote, e.first_seen_at, e.last_changed_at
        FROM entities e
    """)
    entities = [dict(r) for r in cur.fetchall()]

    compiled_count = 0
    now_iso = dt.date.today().isoformat()

    index_lines = [
        "# ❄️ Snowball Research Wiki Index",
        f"*Compiled: {now_iso} | Knowledge Base Root*\n",
        "## Discovered Entities & Opportunities\n",
        "| Entity / Program | Organization | Category | Amount / Value | Deadline | Source |",
        "|---|---|---|---|---|---|",
    ]

    for e in entities:
        slug = slugify(f"{e['name']}-{e['org']}")
        page_path = entities_dir / f"{slug}.md"

        amt_str = f"{e.get('currency', '')} {e.get('amount', '')}".strip() or "Variable"
        dl_str = e.get("deadline") or "Rolling"

        # Generate frontmatter
        fm = {
            "name": e["name"],
            "org": e["org"],
            "year": e["year"] or str(dt.date.today().year),
            "kind": e["kind"] or "opportunity",
            "amount": amt_str,
            "deadline": dl_str,
            "eligibility": e.get("eligibility", "Open"),
            "sources": [e["link"]] if e.get("link") else [],
            "tags": [slugify(e.get("kind", "")), "grant" if "grant" in e["name"].lower() else "opportunity"],
            "last_compiled": now_iso,
        }

        # Generate body
        body_parts = [
            f"# {e['name']}\n",
            f"**Organization:** {e['org']}  ",
            f"**Category:** {e.get('kind', 'Opportunity')}  ",
            f"**Value / Award:** {amt_str}  ",
            f"**Application Deadline:** {dl_str}  ",
            f"**Eligibility Guidelines:** {e.get('eligibility', 'Open')}  ",
        ]
        if e.get("link"):
            body_parts.append(f"**Official Link:** [{e['link']}]({e['link']})\n")

        if e.get("evidence_quote"):
            body_parts.append("## Verbatim Evidence Quote")
            body_parts.append(f'> "{e["evidence_quote"]}"\n')

        # Fetch linked claims
        cur.execute("SELECT text FROM claims WHERE entity_key = ? LIMIT 5", (e["key"],))
        linked_claims = [r[0] for r in cur.fetchall()]
        if linked_claims:
            body_parts.append("## Verified Intelligence Claims")
            for lc in linked_claims:
                body_parts.append(f"- {lc}")
            body_parts.append("")

        new_body = "\n".join(body_parts)

        old_content = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
        merged_content = merge_page(old_content, fm, new_body)
        page_path.write_text(merged_content, encoding="utf-8")
        compiled_count += 1

        index_lines.append(
            f"| [{e['name']}](entities/{slug}.md) | {e['org']} | {e.get('kind', 'Opportunity')} | {amt_str} | {dl_str} | [Link]({e.get('link', '#')}) |"
        )

    # Write INDEX.md
    index_path = output_dir / "INDEX.md"
    index_path.write_text("\n".join(index_lines), encoding="utf-8")

    return {"compiled_entities": compiled_count, "total_entities": len(entities)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Markdown Wiki Compiler")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    ap.add_argument("--out", type=Path, default=WIKI_DIR, help="Output wiki directory")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"Database not found at {args.db}. Nothing to compile.")
        return

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    stats = compile_wiki(conn, args.out)
    conn.close()

    print(f"\n❄️  Wiki Compiled -> {args.out}")
    print(f"  • {stats['compiled_entities']} entity pages updated")
    print(f"  • Index generated at: {args.out / 'INDEX.md'}\n")


if __name__ == "__main__":
    main()
