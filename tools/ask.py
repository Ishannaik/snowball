#!/usr/bin/env python3
"""Snowball Semantic Question Answering Engine (snowball ask).
Queries the local knowledge base (claims, entities, wiki) using SQLite FTS5 BM25 retrieval
and generates citation-grounded answers with source URLs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"


def get_db(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    if not p.exists():
        raise FileNotFoundError(f"Database not found at {p}. Run orchestrator.py first.")
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def search_local_knowledge(conn: sqlite3.Connection, query: str, limit: int = 8) -> dict[str, Any]:
    """Retrieve matching claims, entities, and wiki chunks using SQLite FTS5."""
    conn.row_factory = sqlite3.Row
    clean_query = re.sub(r"[^\w\s]", " ", query).strip()
    words = [w for w in clean_query.split() if len(w) > 2]
    if not words:
        words = clean_query.split() or ["grants"]
    fts_query = " OR ".join(words)

    cur = conn.cursor()
    matched_claims = []
    matched_entities = []

    # 1. Query Claims via FTS5 if available
    try:
        cur.execute(
            """
            SELECT c.hash, c.text, c.n_sources, p.canon_url AS url
            FROM claims_fts f
            JOIN claims c ON f.rowid = c.rowid
            LEFT JOIN claim_sources cs ON c.hash = cs.claim_hash
            LEFT JOIN pages p ON cs.page_id = p.id
            WHERE claims_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """,
            (fts_query, limit),
        )
        for r in cur.fetchall():
            matched_claims.append(
                {
                    "text": r["text"],
                    "url": r["url"],
                    "sightings": r["n_sources"],
                }
            )
    except Exception:
        # Fallback to LIKE if FTS table empty or unpopulated
        cur.execute(
            """
            SELECT c.hash, c.text, c.n_sources, p.canon_url AS url
            FROM claims c
            LEFT JOIN claim_sources cs ON c.hash = cs.claim_hash
            LEFT JOIN pages p ON cs.page_id = p.id
            WHERE c.text LIKE ?
            LIMIT ?
        """,
            (f"%{words[0]}%", limit),
        )
        for r in cur.fetchall():
            matched_claims.append(
                {
                    "text": r["text"],
                    "url": r["url"],
                    "sightings": r["n_sources"],
                }
            )

    # 2. Query Entities
    like_clauses = " OR ".join(["e.name LIKE ? OR e.eligibility LIKE ? OR e.kind LIKE ?"] * len(words))
    params: list[Any] = []
    for w in words:
        pattern = f"%{w}%"
        params.extend([pattern, pattern, pattern])
    params.append(limit)

    cur.execute(
        f"""
        SELECT e.key, e.name, e.org, e.kind, e.amount, e.currency, e.deadline, e.eligibility, e.link, e.evidence_quote
        FROM entities e
        WHERE {like_clauses}
        LIMIT ?
    """,
        params,
    )

    for r in cur.fetchall():
        matched_entities.append(dict(r))

    return {
        "query": query,
        "claims": matched_claims,
        "entities": matched_entities,
    }


def synthesize_answer(evidence: dict[str, Any], use_model: bool = True) -> str:
    """Synthesize citation-backed answer from retrieved local facts."""
    claims = evidence["claims"]
    entities = evidence["entities"]

    if not claims and not entities:
        return (
            f"No verified local records found matching '{evidence['query']}'.\n"
            "Snowball will schedule this query to the crawl frontier on the next run."
        )

    # If GEMINI_API_KEY is available and model requested, do strict citation synthesis
    api_key = os.environ.get("GEMINI_API_KEY")
    if use_model and api_key and (claims or entities):
        context_lines = []
        for e in entities:
            amt = f"{e.get('currency', '')} {e.get('amount', '')}".strip() or "Variable"
            context_lines.append(
                f"- Entity: {e['name']} ({e['org']}), Kind: {e['kind']}, Value: {amt}, Deadline: {e['deadline']}, "
                f"Eligibility: {e['eligibility']}, Source: {e['link']}"
            )
        for c in claims:
            context_lines.append(f"- Claim: {c['text']} (Source: {c['url']})")

        prompt = (
            f"Question: {evidence['query']}\n\n"
            f"Verified Facts in Snowball Knowledge Base:\n"
            + "\n".join(context_lines)
            + "\n\nInstructions: Answer the question accurately and concisely relying ONLY on the facts above. "
            "For every assertion or opportunity named, cite its exact Source URL in brackets [url]. "
            "If details are not in the facts, state what is missing. Do not add outside speculation."
        )

        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={api_key}"
            req = urllib.request.Request(
                url,
                data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read())
                return res["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            pass  # Fall back to mechanical synthesis

    # Mechanical fallback synthesis
    out = [f"### Answer for: '{evidence['query']}'\n"]
    if entities:
        out.append("**Matching Opportunities & Grants:**")
        for e in entities:
            amt = f"{e.get('currency', '')} {e.get('amount', '')}".strip() or "Variable"
            out.append(f"- **{e['name']}** ({e['org']}) — {amt}")
            out.append(f"  • Deadline: {e['deadline'] or 'Rolling'} | Eligibility: {e['eligibility']}")
            out.append(f"  • Source: {e['link']}")
        out.append("")

    if claims:
        out.append("**Verified Facts & Citations:**")
        for c in claims:
            url = c["url"] or "Local Knowledge Base"
            out.append(f"- {c['text']} [{url}]")

    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Knowledge Base Answering CLI (snowball ask)")
    ap.add_argument("query", help="Question or search query")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    ap.add_argument("--json", action="store_true", help="Output raw JSON evidence")
    ap.add_argument("--no-model", action="store_true", help="Disable LLM synthesis, return deterministic facts")
    args = ap.parse_args()

    conn = get_db(args.db)
    evidence = search_local_knowledge(conn, args.query)
    conn.close()

    if args.json:
        print(json.dumps(evidence, indent=2, default=str))
        return

    answer = synthesize_answer(evidence, use_model=not args.no_model)
    print("\n" + answer + "\n")


if __name__ == "__main__":
    main()
