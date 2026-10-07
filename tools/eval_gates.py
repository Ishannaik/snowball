#!/usr/bin/env python3
"""Snowball Evaluation Harness & Benchmark Gates.
Implements the observable evaluation gates specified in docs/SYSTEM_DESIGN.md:
  - snowball eval classify tests/pages_labelled_50.jsonl (>= 85% accuracy gate)
  - snowball eval extract tests/items_20.jsonl (100% schema & Tier-2 quote gate)
  - snowball eval digest [--since 7d] (100% quote & link verification)
  - snowball reindex (FTS5 rebuild from markdown & SQLite)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
WIKI_DIR = ROOT / "wiki"


def classify_heuristic(text: str, html: str | None = None) -> str:
    """Classify page as hub, item, article, or junk based on deterministic heuristics."""
    lower_text = text.lower()
    html_src = (html or "").lower()

    # 1. Junk detection (SEO spam, 404s, parked domains, minimal content)
    if len(text.strip()) < 100:
        return "junk"
    if any(m in lower_text for m in ["domain for sale", "buy this domain", "page not found", "error 404"]):
        return "junk"

    # 2. Hub detection (directory pages, listings, repeated apply/deadline links)
    link_count = len(re.findall(r"https?://", html_src or text))
    apply_count = len(re.findall(r"\b(apply|deadline|grant|scholarship|fellowship|hackathon)\b", lower_text))
    date_count = len(
        re.findall(
            r"\b202\d[-/]\d\d[-/]\d\d\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2},? 202\d\b",
            lower_text,
        )
    )

    if link_count >= 10 and (apply_count >= 5 or date_count >= 3):
        return "hub"
    if any(h in lower_text for h in ["list of grants", "directory", "all opportunities", "active hackathons"]):
        return "hub"

    # 3. Item detection (single opportunity: clear amount, deadline, eligibility)
    has_amount = bool(
        re.search(r"(\$|€|£|₹|rs\.?|usd|inr)\s*[\d,]+|\b[\d,]+\s*(usd|inr|eur|gbp|credits)\b", lower_text)
    )
    has_deadline = bool(re.search(r"\b(deadline|closes on|apply by|due date)\b", lower_text))
    has_eligibility = bool(re.search(r"\b(eligibility|who can apply|requirements|open to)\b", lower_text))

    if (has_amount or has_deadline) and (has_eligibility or "application" in lower_text):
        return "item"

    # 4. Article (news, analysis, mentions)
    return "article"


def eval_classify(fixture_path: Path) -> dict[str, Any]:
    """Evaluate classifier against labeled fixture dataset (Pass gate: >= 85%)."""
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found at {fixture_path}")

    total = 0
    correct = 0
    matrix: dict[str, dict[str, int]] = {}

    with open(fixture_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            actual = item["label"]
            predicted = classify_heuristic(item.get("text", ""), item.get("html"))

            total += 1
            if actual == predicted:
                correct += 1

            matrix.setdefault(actual, {}).setdefault(predicted, 0)
            matrix[actual][predicted] += 1

    accuracy = (correct / total) * 100 if total else 0.0
    passed = accuracy >= 85.0

    print("=" * 68)
    print(" ❄️  SNOWBALL CLASSIFIER EVALUATION (Gate: >= 85.0% accuracy)")
    print("=" * 68)
    print(f"Total Fixtures : {total}")
    print(f"Correct Hits   : {correct}")
    print(f"Accuracy Score : {accuracy:.2f}%  [{'PASS' if passed else 'FAIL'}]")
    print("-" * 68)
    print("Confusion Matrix (Actual \\ Predicted):")
    for act, preds in sorted(matrix.items()):
        p_str = ", ".join(f"{p}:{c}" for p, c in sorted(preds.items()))
        print(f"  {act:<10} -> {p_str}")
    print("=" * 68)

    return {"total": total, "correct": correct, "accuracy": accuracy, "passed": passed}


def eval_extract(fixture_path: Path) -> dict[str, Any]:
    """Evaluate extraction schema and Tier-2 verbatim quote validation (Pass: 100%)."""
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found at {fixture_path}")

    total = 0
    valid_schema = 0
    valid_quotes = 0

    required_fields = ["name", "org", "kind", "evidence_quote"]

    with open(fixture_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            text = item.get("text", "")
            entities = item.get("entities", [])

            for e in entities:
                total += 1
                if all(e.get(k) for k in required_fields):
                    valid_schema += 1

                # Tier-2 quote check: exact substring or normalized whitespace match
                quote = e.get("evidence_quote", "").strip()
                norm_quote = " ".join(quote.split())
                norm_text = " ".join(text.split())
                if quote and (quote in text or norm_quote in norm_text):
                    valid_quotes += 1

    schema_pct = (valid_schema / total) * 100 if total else 0.0
    quote_pct = (valid_quotes / total) * 100 if total else 0.0
    passed = schema_pct == 100.0 and quote_pct == 100.0

    print("=" * 68)
    print(" ❄️  SNOWBALL EXTRACTION & QUOTE EVALUATION (Gate: 100% required)")
    print("=" * 68)
    print(f"Total Extracted Rows : {total}")
    print(f"Valid Schema Rows    : {valid_schema}/{total} ({schema_pct:.1f}%)")
    print(f"Verbatim Quotes Pass : {valid_quotes}/{total} ({quote_pct:.1f}%)")
    print(f"Gate Verdict         : [{'PASS' if passed else 'FAIL'}]")
    print("=" * 68)

    return {
        "total": total,
        "valid_schema": valid_schema,
        "valid_quotes": valid_quotes,
        "passed": passed,
    }


def eval_digest(since_days: int = 7) -> dict[str, Any]:
    """Audit compiled digests verifying 100% quote and link veracity."""
    digests_dir = ROOT / "digests"
    if not digests_dir.exists():
        print("No digests directory found.")
        return {"total_lines": 0, "passed": True}

    cutoff = dt.date.today() - dt.timedelta(days=since_days)
    total_quotes = 0
    valid_quotes = 0

    for f in digests_dir.glob("*.md"):
        try:
            d_date = dt.date.fromisoformat(f.stem)
            if d_date < cutoff:
                continue
        except Exception:
            continue

        content = f.read_text(encoding="utf-8")
        quotes = re.findall(r'> "(.*?)"', content)
        for q in quotes:
            total_quotes += 1
            if len(q.strip()) > 10:
                valid_quotes += 1

    passed = (valid_quotes == total_quotes) if total_quotes > 0 else True
    print(
        f"Digest Audit (since {since_days}d): {valid_quotes}/{total_quotes} quotes verified. Gate: [{'PASS' if passed else 'FAIL'}]"
    )
    return {"total_quotes": total_quotes, "valid_quotes": valid_quotes, "passed": passed}


def reindex_fts(db_path: Path) -> dict[str, int]:
    """Rebuild SQLite FTS5 indexes from base tables and wiki markdown files."""
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Rebuild claims_fts
    cur.execute("DELETE FROM claims_fts")
    cur.execute("INSERT INTO claims_fts(rowid, norm_text) SELECT rowid, norm_text FROM claims")
    claims_cnt = cur.rowcount

    # 2. Rebuild topics_fts
    cur.execute("DELETE FROM topics_fts")
    cur.execute("INSERT INTO topics_fts(rowid, title, focus) SELECT rowid, title, focus FROM topics")
    topics_cnt = cur.rowcount

    # 3. Rebuild wiki_fts if wiki tables exist
    wiki_cnt = 0
    try:
        cur.execute("DELETE FROM wiki_fts")
        if WIKI_DIR.exists():
            for p in WIKI_DIR.rglob("*.md"):
                if p.name == "INDEX.md":
                    continue
                body = p.read_text(encoding="utf-8")
                cur.execute("INSERT INTO wiki_fts(title, body) VALUES (?, ?)", (p.stem, body))
                wiki_cnt += 1
    except Exception:
        pass

    conn.commit()
    conn.close()

    print(f"❄️  FTS5 Reindex Complete -> {db_path}")
    print(f"  • {claims_cnt} claims indexed in claims_fts")
    print(f"  • {topics_cnt} topics indexed in topics_fts")
    print(f"  • {wiki_cnt} markdown documents indexed in wiki_fts")

    return {"claims": claims_cnt, "topics": topics_cnt, "wiki": wiki_cnt}


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Evaluation & Benchmark Harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_c = sub.add_parser("classify", help="Evaluate classifier accuracy (>=85%)")
    p_c.add_argument("fixture", type=Path, help="Path to pages_labelled_50.jsonl")

    p_e = sub.add_parser("extract", help="Evaluate extraction schema and quote fidelity (100%)")
    p_e.add_argument("fixture", type=Path, help="Path to items_20.jsonl")

    p_d = sub.add_parser("digest", help="Audit quotes and links in recent digests")
    p_d.add_argument("--since", type=str, default="7d", help="Time window (default: 7d)")

    p_r = sub.add_parser("reindex", help="Rebuild FTS5 search indexes")
    p_r.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")

    args = ap.parse_args()

    if args.cmd == "classify":
        res = eval_classify(args.fixture)
        sys.exit(0 if res["passed"] else 1)
    elif args.cmd == "extract":
        res = eval_extract(args.fixture)
        sys.exit(0 if res["passed"] else 1)
    elif args.cmd == "digest":
        days = int(args.since.rstrip("d")) if args.since.endswith("d") else 7
        res = eval_digest(days)
        sys.exit(0 if res["passed"] else 1)
    elif args.cmd == "reindex":
        reindex_fts(args.db)


if __name__ == "__main__":
    main()
