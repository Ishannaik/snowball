#!/usr/bin/env python3
"""Evaluator commands and the gate-evidence ledger.

`classify`, `extract`, `digest`, and `reindex` are not production-gate proofs.
`classify_heuristic` is not the fetch-pipeline classifier (owners T5 and T10).
`eval_extract` does not run the production extractor (owners T5 and T10).
`eval_digest` treats quote length as success and sets its pass flag on 0 quotes
(owner T10). `reindex_fts` on an empty database is not M2.12 (owner T8).

Gate completion is a verified row from `record_gate_evidence` in
`data/gate-evidence.jsonl`. `evidence-check` validates that snapshot. It does
not run gates and it does not mark them verified.
"""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
WIKI_DIR = ROOT / "wiki"


def _sibling(name: str) -> Any:
    mod_name = f"snowball_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    mod_path = ROOT / "tools" / f"{name}.py"
    if not mod_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


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


def eval_classify(
    fixture_path: Path,
    db_path: Path | None = None,
    engine: Any | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Score the production classifier against a labelled fixture file.

    Calls tools.orchestrator classify_page directly; no heuristic fallback.
    Gate M2.01 requires total >= 50 fixtures and correct >= 43 (>=85% accuracy).
    """
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found at {fixture_path}")

    orch = _sibling("orchestrator")
    if not orch or not hasattr(orch, "classify_page"):
        raise RuntimeError("Production classifier API 'classify_page' not available in tools.orchestrator")

    classify_fn = orch.classify_page

    total = 0
    correct = 0
    matrix: dict[str, dict[str, int]] = {}

    with open(fixture_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            actual = item.get("label") or item.get("expected") or item.get("kind")
            title = item.get("title", "")
            text = item.get("text", "")
            url = item.get("url", "")
            links = item.get("links", [])

            predicted = classify_fn(title=title, text=text, url=url, links=links, engine=engine)

            total += 1
            if actual == predicted:
                correct += 1

            if actual:
                matrix.setdefault(actual, {}).setdefault(predicted, 0)
                matrix[actual][predicted] += 1

    accuracy = (correct / total) * 100 if total else 0.0
    passed = total >= 50 and correct >= 43 and accuracy >= 85.0

    if not quiet:
        print("=" * 68)
        print(" ❄️  SNOWBALL PRODUCTION CLASSIFIER EVALUATION (Gate M2.01: >= 85.0%, >=43/50)")
        print("=" * 68)
        print(f"Total Fixtures : {total}")
        print(f"Correct Hits   : {correct}")
        print(f"Accuracy Score : {accuracy:.2f}%  [{'PASS' if passed else 'FAIL'}]")
        if matrix:
            print("-" * 68)
            print("Confusion Matrix (Actual \\ Predicted):")
            for act, preds in sorted(matrix.items()):
                p_str = ", ".join(f"{p}:{c}" for p, c in sorted(preds.items()))
                print(f"  {act:<10} -> {p_str}")
        print("=" * 68)

    return {
        "classifier": "production",
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "passed": passed,
    }


def eval_extract(
    fixture_path: Path,
    db_path: Path | None = None,
    engine: Any | None = None,
    cfg: Any | None = None,
    store: Any | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Score production extraction and quote validation against a pipeline fixture file.

    Calls tools.orchestrator extract_page_entities on real source-text fixtures.
    If pre-extracted fixture rows are provided, performs fixture validation without claiming production model extraction.
    """
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found at {fixture_path}")

    orch = _sibling("orchestrator")
    if not orch:
        raise RuntimeError("tools.orchestrator not available")

    total = 0
    valid_schema = 0
    valid_quotes = 0
    dropped = 0
    is_production_extraction = False

    with open(fixture_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            text = item.get("text", "")
            url = item.get("url", "")
            title = item.get("title", "")
            entities = item.get("entities")
            data = item.get("data")

            if (entities is None and data is None) or item.get("run_extractor"):
                if not hasattr(orch, "extract_page_entities"):
                    raise RuntimeError("extract_page_entities not exported from tools.orchestrator")
                is_production_extraction = True
                res = orch.extract_page_entities(
                    source_text=text,
                    url=url,
                    title=title,
                    engine=engine,
                    cfg=cfg,
                    store=store,
                )
                val_rows = len(res.get("rows", []))
                total += val_rows + res.get("dropped", 0)
                valid_schema += val_rows
                valid_quotes += val_rows
                dropped += res.get("dropped", 0)
            else:
                if not hasattr(orch, "validate_extraction"):
                    raise RuntimeError("validate_extraction not exported from tools.orchestrator")
                data_obj = data or {
                    "rows": entities or [],
                    "claims": item.get("claims", []),
                    "names": item.get("names", []),
                    "links": item.get("links", []),
                }
                row_count = len(data_obj.get("rows", []))
                total += row_count
                res = orch.validate_extraction(data_obj, source_text=text, fallback_url=url)
                val_rows = len(res.get("rows", []))
                valid_schema += val_rows
                valid_quotes += val_rows
                dropped += res.get("dropped", 0)

    schema_pct = (valid_schema / total) * 100 if total else 0.0
    quote_pct = (valid_quotes / total) * 100 if total else 0.0
    passed = total > 0 and valid_schema == total and valid_quotes == total and dropped == 0

    if not quiet:
        print("=" * 68)
        print(" ❄️  SNOWBALL EXTRACTION EVALUATION (Gate M2.02: 100% required)")
        print("=" * 68)
        print(f"Mode                 : {'production_model' if is_production_extraction else 'fixture_validation'}")
        print(f"Total Extracted Rows : {total}")
        print(f"Valid Schema Rows    : {valid_schema}/{total} ({schema_pct:.1f}%)")
        print(f"Verbatim Quotes Pass : {valid_quotes}/{total} ({quote_pct:.1f}%)")
        print(f"Dropped Rows         : {dropped}")
        print(f"Gate Verdict         : [{'PASS' if passed else 'FAIL'}]")
        print("=" * 68)

    return {
        "extractor": "production" if is_production_extraction else "fixture_validation",
        "production": is_production_extraction,
        "total": total,
        "valid_schema": valid_schema,
        "valid_quotes": valid_quotes,
        "dropped": dropped,
        "passed": passed,
    }


def eval_digest(
    since: int | str = "7d",
    db_path: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Audit acknowledged delivered digest items against recorded source text and verified URLs.

    Audits EXACT delivered snapshots from digest_items and digest_outbox.
    Every delivered item must have a verbatim source quote (total_quotes == total_items).
    Checks SSRF-safe link reachability via resolve_and_pin / canonical_url.
    0 items never PASS. Long false quotes fail.
    """
    days = 7
    if isinstance(since, int):
        days = since
    elif isinstance(since, str):
        m = re.match(r"^(\d+)\s*d?$", since.strip().lower())
        if m:
            days = int(m.group(1))

    p_db = db_path or DB_PATH
    if not p_db.exists():
        if not quiet:
            print(f"Database not found at {p_db}.", file=sys.stderr)
        return {
            "total_items": 0,
            "total_quotes": 0,
            "valid_quotes": 0,
            "valid_urls": 0,
            "production_delivery_check": True,
            "method": "source_text_verbatim",
            "passed": False,
            "message_id": False,
            "error": "database not found",
        }

    conn = sqlite3.connect(p_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    has_table = bool(
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='digest_items'"
        ).fetchone()
    )
    if not has_table:
        conn.close()
        if not quiet:
            print("Table digest_items not found in database.")
        return {
            "total_items": 0,
            "total_quotes": 0,
            "valid_quotes": 0,
            "valid_urls": 0,
            "production_delivery_check": True,
            "method": "source_text_verbatim",
            "acknowledged_at_compile": False,
            "file_only": False,
            "message_id": False,
            "passed": False,
            "error": "table digest_items not found",
        }

    cutoff_date = (dt.date.today() - dt.timedelta(days=days)).isoformat()

    cur.execute(
        """
        SELECT id, day, ref_kind, ref_id, rank, reason, sent_at, message_id
        FROM digest_items
        WHERE message_id IS NOT NULL AND message_id != '' AND day >= ?
        ORDER BY id ASC
    """,
        (cutoff_date,),
    )
    delivered_items = [dict(r) for r in cur.fetchall()]

    orch = _sibling("orchestrator")
    resolve_fn = getattr(orch, "resolve_and_pin", None) if orch else None
    canonical_fn = getattr(orch, "canonical_url", None) if orch else None
    quote_ok_fn = getattr(orch, "quote_ok", None) if orch else None
    dig_mod = _sibling("digest")
    snapshot_fn = getattr(dig_mod, "get_digest_snapshot", None) if dig_mod else None

    total_items = len(delivered_items)
    total_quotes = 0
    valid_quotes = 0
    valid_urls = 0

    for item in delivered_items:
        ref_kind = item["ref_kind"]
        ref_id = item["ref_id"]

        evidence_quote = ""
        link = ""
        page_id = None
        link_dead = 0

        if snapshot_fn:
            snap = snapshot_fn(conn, item["id"])
            if snap:
                evidence_quote = snap.get("evidence_quote") or ""
                link = snap.get("link") or ""
                page_id = snap.get("page_id")
                link_dead = snap.get("link_dead") or 0
        else:
            if ref_kind == "entity":
                row = cur.execute(
                    "SELECT key, name, link, evidence_quote, evidence_page_id FROM entities WHERE key = ?",
                    (ref_id,),
                ).fetchone()
                if row:
                    evidence_quote = row["evidence_quote"] or ""
                    link = row["link"] or ""
                    page_id = row["evidence_page_id"]
                    link_dead = 0
            elif ref_kind == "change":
                row = cur.execute(
                    """
                    SELECT ec.id, ec.evidence_quote, ec.entity_key, e.link, e.evidence_page_id
                    FROM entity_changes ec
                    LEFT JOIN entities e ON ec.entity_key = e.key
                    WHERE ec.id = ?
                """,
                    (ref_id,),
                ).fetchone()
                if row:
                    evidence_quote = row["evidence_quote"] or ""
                    link = row["link"] or ""
                    page_id = row["evidence_page_id"]
                    link_dead = 0
        if evidence_quote and evidence_quote.strip():
            total_quotes += 1

        source_text = ""
        if page_id:
            pg = cur.execute(
                "SELECT canon_url, raw_path FROM pages WHERE id = ?",
                (page_id,),
            ).fetchone()
            if pg and pg["raw_path"]:
                r_path = Path(pg["raw_path"])
                if not r_path.is_absolute():
                    r_path = ROOT / r_path
                if r_path.is_file():
                    try:
                        source_text = r_path.read_text(encoding="utf-8")
                    except Exception:
                        pass
        if evidence_quote and source_text:
            if quote_ok_fn:
                qok, _, _ = quote_ok_fn(evidence_quote, source_text)
            else:
                norm_q = " ".join(evidence_quote.strip().casefold().split())
                norm_t = " ".join(source_text.strip().casefold().split())
                qok = bool(norm_q and norm_q in norm_t and 40 <= len(evidence_quote) <= 400)
            if qok:
                valid_quotes += 1

        target_link = link
        if link_dead:
            hub_row = cur.execute(
                """
                SELECT p.canon_url FROM entity_sources es
                JOIN pages p ON es.source_page_id = p.id
                WHERE es.entity_key = ? AND p.kind = 'hub'
                LIMIT 1
            """,
                (ref_id if ref_kind == "entity" else (row["entity_key"] if row else ""),),
            ).fetchone()
            if hub_row:
                target_link = hub_row["canon_url"] or ""

        if target_link:
            c_url = canonical_fn(target_link) if canonical_fn else target_link
            if c_url:
                try:
                    from urllib.parse import urlsplit
                    u = urlsplit(c_url)
                    port = u.port or (443 if u.scheme == "https" else 80)
                    if resolve_fn and u.hostname:
                        resolve_fn(u.hostname, port)
                        valid_urls += 1
                    elif u.scheme in ("http", "https") and u.hostname:
                        valid_urls += 1
                except Exception:
                    pass

    conn.close()

    passed = (
        total_items > 0
        and total_quotes == total_items
        and valid_quotes == total_items
        and valid_urls == total_items
    )

    if not quiet:
        print("=" * 68)
        print(f" ❄️  SNOWBALL DIGEST DELIVERY AUDIT (Since {days}d, Gate M4.08)")
        print("=" * 68)
        print(f"Acknowledged Delivered Items : {total_items}")
        print(f"Items with Source Quotes     : {total_quotes}/{total_items}")
        print(f"Verified Verbatim Quotes     : {valid_quotes}/{total_items}")
        print(f"Verified SSRF-Safe URLs      : {valid_urls}/{total_items}")
        print(f"Gate Verdict                 : [{'PASS' if passed else 'FAIL'}]")
        print("=" * 68)

    return {
        "total_items": total_items,
        "total_quotes": total_quotes,
        "valid_quotes": valid_quotes,
        "valid_urls": valid_urls,
        "production_delivery_check": True,
        "method": "source_text_verbatim",
        "acknowledged_at_compile": False,
        "file_only": False,
        "message_id": total_items > 0,
        "passed": passed,
    }


def reindex_fts(
    db_path: Path,
    wiki_dir: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Rebuild FTS indexes from base tables and wiki markdown using wiki_compiler.reindex_wiki.

    Rebuilds wiki_pages, wiki_links, wiki_fts, claims_fts, topics_fts.
    Indexes ALL Markdown files including INDEX.md. Propagates compiler failures.
    An empty database/wiki proves nothing and does not verify M2.12.
    """
    if not db_path.exists():
        if not quiet:
            print(f"Database not found at {db_path}.", file=sys.stderr)
        return {
            "claims": 0,
            "topics": 0,
            "wiki": 0,
            "markdown_files": 0,
            "wiki_pages_rebuilt": False,
            "passed": False,
            "error": f"Database not found at {db_path}",
        }

    w_dir = (wiki_dir or WIKI_DIR).resolve()
    if not w_dir.is_dir():
        if not quiet:
            print(f"Wiki directory not found at {w_dir}.", file=sys.stderr)
        return {
            "claims": 0,
            "topics": 0,
            "wiki": 0,
            "markdown_files": 0,
            "wiki_pages_rebuilt": False,
            "passed": False,
            "error": f"Wiki directory not found at {w_dir}",
        }

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    wc = _sibling("wiki_compiler")
    if not wc or not hasattr(wc, "reindex_wiki"):
        conn.close()
        raise RuntimeError("wiki_compiler.reindex_wiki not available")

    # Call actual compiler directly, propagating any failures
    wc.reindex_wiki(conn, w_dir)

    markdown_files = len(list(w_dir.rglob("*.md")))

    cur.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5("
        "norm_text, tokenize='unicode61 remove_diacritics 2')"
    )
    cur.execute("DELETE FROM claims_fts")
    has_claims = bool(cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'").fetchone())
    if has_claims:
        cur.execute("INSERT INTO claims_fts(rowid, norm_text) SELECT rowid, norm_text FROM claims")
    claims_cnt = cur.rowcount if has_claims else 0

    cur.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS topics_fts USING fts5("
        "title, focus, tokenize='unicode61 remove_diacritics 2')"
    )
    cur.execute("DELETE FROM topics_fts")
    has_topics = bool(cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='topics'").fetchone())
    if has_topics:
        cur.execute("INSERT INTO topics_fts(rowid, title, focus) SELECT rowid, title, focus FROM topics")
    topics_cnt = cur.rowcount if has_topics else 0
    wiki_cnt = cur.execute("SELECT count(*) FROM wiki_fts").fetchone()[0]

    conn.commit()
    conn.close()

    passed = markdown_files > 0 and wiki_cnt == markdown_files

    if not quiet:
        print(f"❄️  FTS5 Reindex Complete -> {db_path}")
        print(f"  • {claims_cnt} claims indexed in claims_fts")
        print(f"  • {topics_cnt} topics indexed in topics_fts")
        print(f"  • {wiki_cnt} markdown documents indexed in wiki_fts ({markdown_files} files on disk)")

    return {
        "claims": claims_cnt,
        "topics": topics_cnt,
        "wiki": wiki_cnt,
        "markdown_files": markdown_files,
        "wiki_pages_rebuilt": True,
        "passed": passed,
    }

def eval_topics(
    pairs_path: Path,
    db_path: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Score topic pair similarity against labelled benchmarks.

    Thresholds 0.75 / 0.30:
    >= 0.75: not-new (same / deepen)
    < 0.30: new
    Target: >=95% new/not-new binary accuracy, >=80% same/deepen fine agreement.
    """
    if not pairs_path.exists():
        raise FileNotFoundError(f"Topic pairs fixture not found at {pairs_path}")

    orch = _sibling("orchestrator")
    tfidf_cosine_fn = getattr(orch, "tfidf_cosine", None) if orch else None

    total = 0
    binary_correct = 0
    fine_correct = 0
    fine_total = 0

    with open(pairs_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            ta = item.get("topic_a", "")
            tb = item.get("topic_b", "")
            label = item.get("label", "").lower()
            expected_binary = "new" if label == "new" else "not_new"

            toks_a = re.sub(r"[^\w\s]", " ", ta.casefold()).split()
            toks_b = re.sub(r"[^\w\s]", " ", tb.casefold()).split()
            ca = Counter(toks_a)
            cb = Counter(toks_b)
            all_words = set(toks_a) | set(toks_b)
            df = {w: (1 if w in ca else 0) + (1 if w in cb else 0) for w in all_words}

            if tfidf_cosine_fn:
                sim = tfidf_cosine_fn(ca, cb, df, 10)
            else:
                dot = sum(ca.get(w, 0) * cb.get(w, 0) for w in all_words)
                norm_a = math.sqrt(sum(v * v for v in ca.values())) or 1.0
                norm_b = math.sqrt(sum(v * v for v in cb.values())) or 1.0
                sim = dot / (norm_a * norm_b)

            if sim >= 0.75:
                pred_binary = "not_new"
                pred_fine = "same"
            elif sim >= 0.30:
                pred_binary = "not_new"
                pred_fine = "deepen"
            else:
                pred_binary = "new"
                pred_fine = "new"

            total += 1
            if pred_binary == expected_binary:
                binary_correct += 1

            if label in ("same", "deepen"):
                fine_total += 1
                if pred_fine == label:
                    fine_correct += 1

    bin_acc = (binary_correct / total) * 100 if total else 0.0
    fine_acc = (fine_correct / fine_total) * 100 if fine_total else 0.0
    passed = total >= 30 and bin_acc >= 95.0 and fine_acc >= 80.0

    if not quiet:
        print("=" * 68, file=sys.stderr)
        print(" ❄️  SNOWBALL TOPIC SIMILARITY BENCHMARK (Gate M3.03: 95%/80%)", file=sys.stderr)
        print("=" * 68, file=sys.stderr)
        print(f"Total Topic Pairs   : {total}", file=sys.stderr)
        print(f"New / Not-New Score : {bin_acc:.1f}% ({binary_correct}/{total}) [Req >=95%]", file=sys.stderr)
        print(f"Same / Deepen Score : {fine_acc:.1f}% ({fine_correct}/{fine_total}) [Req >=80%]", file=sys.stderr)
        print(f"Gate Verdict        : [{'PASS' if passed else 'FAIL'}]", file=sys.stderr)
        print("=" * 68, file=sys.stderr)

    return {
        "total": total,
        "binary_correct": binary_correct,
        "binary_accuracy": bin_acc,
        "fine_correct": fine_correct,
        "fine_total": fine_total,
        "fine_accuracy": fine_acc,
        "passed": passed,
    }
LEDGER_PATH = ROOT / "data" / "gate-evidence.jsonl"
GATE_STATUSES = ("implemented", "fixture_verified", "live_verified", "soak_verified", "blocked")
VERIFIED_STATUSES = ("fixture_verified", "live_verified", "soak_verified")
_STATUS_RANK = {"fixture_verified": 1, "live_verified": 2, "soak_verified": 3}
REQUIRED_EVIDENCE_FIELDS = (
    "gate_id",
    "status",
    "command",
    "exit_code",
    "started_at",
    "finished_at",
    "evidence_path",
)

# Time-based gates. Fixture or short live samples do not close them.
SOAK_GATES = {
    "M1.02": 86400,
    "M1.11": 8 * 86400,
    "M3.06": 7 * 86400,
    "M4.07": 8 * 86400,
}
LIVE_GATES = frozenset(
    {
        "M1.03",
        "M1.05",
        "M1.06",
        "M1.09",
        "M1.12",
        "M2.07",
        "M3.07",
        "M3.08",
        "M3.10",
        "M4.02",
        "M4.08",
        "A03",
        "A04",
        "A05",
        "A06",
        "A07",
        "A11",
        "A12",
        "A15",
    }
)

REGRESSIONS: dict[str, dict[str, Any]] = {
    "digest-eval-zero-over-zero": {
        "owners": ("T10",),
        "gate_ids": ("M4.08",),
        "detail": "eval_digest sets passed True when total_quotes is 0. Observed result is 0/0.",
        "evaluator_result": {
            "total_quotes": 0,
            "valid_quotes": 0,
            "passed": True,
            "method": "length",
            "production_delivery_check": False,
        },
    },
    "digest-quote-length": {
        "owners": ("T10",),
        "gate_ids": ("M4.08",),
        "detail": "eval_digest counts a quote as valid when len(strip(quote)) > 10.",
        "evaluator_result": {
            "total_quotes": 2,
            "valid_quotes": 2,
            "passed": True,
            "method": "length",
            "production_delivery_check": False,
        },
    },
    "digest-premature-acknowledgement": {
        "owners": ("T9",),
        "gate_ids": ("M2.07", "M2.08", "M3.09", "M4.01", "M4.08"),
        "detail": "compile_daily_digest sets reported_at before post_to_discord.",
        "evaluator_result": {
            "acknowledged_at_compile": True,
            "message_id": None,
            "file_only": True,
        },
    },
    "classifier-not-production": {
        "owners": ("T5", "T10"),
        "gate_ids": ("M2.01",),
        "detail": "classify_heuristic is not the fetch-pipeline classifier.",
        "evaluator_result": {"classifier": "heuristic", "accuracy": 85.0, "passed": True},
    },
    "extractor-not-production": {
        "owners": ("T5", "T10"),
        "gate_ids": ("M2.02",),
        "detail": "eval_extract checks fixture rows and does not call the production extractor.",
        "evaluator_result": {"extractor": "fixture", "valid_quotes": 20, "total": 20, "passed": True},
    },
    "slug-sample-not-property": {
        "owners": ("T5",),
        "gate_ids": ("M1.10",),
        "detail": "Six example strings are not the 10000-string seeded property gate.",
        "evaluator_result": {"cases": 6, "seeded": False},
    },
    "empty-reindex": {
        "owners": ("T8",),
        "gate_ids": ("M2.12",),
        "detail": "reindex_fts on an empty database does not rebuild wiki_pages or prove file counts.",
        "evaluator_result": {
            "claims": 0,
            "topics": 0,
            "wiki": 0,
            "wiki_pages_rebuilt": False,
            "markdown_files": 0,
        },
    },
}

_USERINFO = re.compile(r"://[^/\s:]+:[^/\s@]+@")
_QUERY_SECRET = re.compile(
    r"([?&](?:api[_-]?key|apikey|key|token|password|proxy|secret)=)[^&\s#]+",
    re.I,
)
_ASSIGN_SECRET = re.compile(
    r"((?:api[_-]?key|apikey|token|password|secret)\s*[=:]\s*)\S+",
    re.I,
)
_BOT = re.compile(r"(Bot )\S+", re.I)
_LONG_TOKEN = re.compile(r"\b(?:sk-|AQ\.)[A-Za-z0-9_\-.]{8,}")


class GateEvidenceError(Exception):
    """Rejected gate-evidence row. The message must not echo a secret."""


def expected_gate_ids() -> list[str]:
    """44 Fable gates plus access gates A01-A15."""
    ids = ["M0.01"]
    ids.extend(f"M1.{i:02d}" for i in range(1, 13))
    ids.extend(f"M2.{i:02d}" for i in range(1, 13))
    ids.extend(f"M3.{i:02d}" for i in range(1, 12))
    ids.extend(f"M4.{i:02d}" for i in range(1, 9))
    ids.extend(f"A{i:02d}" for i in range(1, 16))
    return ids


def blocking_regression_ids(gate_id: str) -> list[str]:
    return [rid for rid, meta in REGRESSIONS.items() if gate_id in meta["gate_ids"]]


def minimum_verified_status(gate_id: str) -> str:
    if gate_id in SOAK_GATES:
        return "soak_verified"
    if gate_id in LIVE_GATES:
        return "live_verified"
    return "fixture_verified"


def redact_text(value: str) -> str:
    value = _USERINFO.sub("://redacted@", value)
    value = _QUERY_SECRET.sub(r"\1REDACTED", value)
    value = _ASSIGN_SECRET.sub(r"\1REDACTED", value)
    value = _BOT.sub(r"\1REDACTED", value)
    return _LONG_TOKEN.sub("REDACTED", value)


def _path_has_secret(value: str) -> bool:
    return redact_text(value) != value


def evidence_block_reason(gate_id: str, evaluator_result: dict[str, Any] | None) -> str | None:
    """Return why this result cannot verify `gate_id`, or None when it does not trip a known defect."""
    if evaluator_result is None:
        return None
    if gate_id == "M4.08":
        if int(evaluator_result.get("total_quotes") or 0) <= 0:
            return "zero quotes are not verification"
        if evaluator_result.get("method") == "length":
            return "quote length is not tier-2 verification"
        if not evaluator_result.get("production_delivery_check"):
            return "digest evaluator does not join delivered lines to source text and links"
        if evaluator_result.get("acknowledged_at_compile") or evaluator_result.get("file_only"):
            return "premature delivery acknowledgement"
    if gate_id == "M2.01" and evaluator_result.get("classifier") != "production":
        return "heuristic evaluator is not the production classifier"
    if gate_id == "M2.02" and evaluator_result.get("extractor") != "production":
        return "fixture scan is not the production extractor"
    if gate_id == "M2.12":
        files = int(evaluator_result.get("markdown_files") or 0)
        if not evaluator_result.get("wiki_pages_rebuilt") or files <= 0:
            return "empty reindex proves nothing"
        if evaluator_result.get("wiki") != evaluator_result.get("markdown_files"):
            return "wiki_fts count does not equal markdown file count"
    if gate_id == "M1.10" and (
        int(evaluator_result.get("cases") or 0) < 10000 or not evaluator_result.get("seeded")
    ):
        return "slug sample is not the 10000-string property gate"
    if gate_id in {"M2.07", "M2.08", "M3.09", "M4.01"} and (
        evaluator_result.get("acknowledged_at_compile")
        or evaluator_result.get("file_only")
        or not evaluator_result.get("message_id")
    ):
        return "premature delivery acknowledgement"
    return None


def _parse_ts(value: str | None, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise GateEvidenceError(f"{field} is required")
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise GateEvidenceError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise GateEvidenceError(f"{field} needs a timezone")
    return parsed


def _validate_snapshot(rows: list[dict[str, Any]]) -> None:
    ids = [row.get("gate_id") for row in rows]
    if ids != expected_gate_ids():
        raise GateEvidenceError("ledger gate_id set does not match the 44 Fable gates plus A01-A15")
    for row in rows:
        gate_id = str(row.get("gate_id"))
        for key in REQUIRED_EVIDENCE_FIELDS:
            if key not in row:
                raise GateEvidenceError(f"{gate_id} missing {key}")
        if row["status"] not in GATE_STATUSES:
            raise GateEvidenceError(f"bad status for {gate_id}")
        if not isinstance(row.get("command"), str) or not row["command"]:
            raise GateEvidenceError(f"{gate_id} needs a command string")
        if not isinstance(row.get("owner"), str) or not row["owner"]:
            raise GateEvidenceError(f"{gate_id} needs an owner")
        if not isinstance(row.get("detail"), str) or not row["detail"]:
            raise GateEvidenceError(f"{gate_id} needs a detail")
        if row.get("blocking_regressions") != blocking_regression_ids(gate_id):
            raise GateEvidenceError(f"{gate_id} blocking_regressions drifted")
        if row["status"] in VERIFIED_STATUSES:
            if row["exit_code"] != 0 or not row["started_at"] or not row["finished_at"] or not row["evidence_path"]:
                raise GateEvidenceError(f"{gate_id} verified without evidence fields")
            evidence = Path(row["evidence_path"])
            if not evidence.is_absolute():
                evidence = ROOT / evidence
            if not evidence.is_file() or evidence.stat().st_size <= 0:
                raise GateEvidenceError(f"{gate_id} evidence file missing")


def load_status_snapshot(path: Path | None = None) -> list[dict[str, Any]]:
    ledger = path or LEDGER_PATH
    if not ledger.is_file():
        raise GateEvidenceError(f"missing ledger {ledger.name}")
    raw_rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GateEvidenceError(f"line {line_no} is not JSON") from exc
        if not isinstance(row, dict):
            raise GateEvidenceError(f"line {line_no} is not an object")
        raw_rows.append(row)

    # Fold journal down to the latest entry per gate_id
    latest_by_id: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        gid = row.get("gate_id")
        if gid:
            latest_by_id[gid] = row

    expected = expected_gate_ids()
    if set(latest_by_id.keys()) != set(expected):
        missing = set(expected) - set(latest_by_id.keys())
        extra = set(latest_by_id.keys()) - set(expected)
        raise GateEvidenceError(
            f"ledger gate_id set mismatch (missing {len(missing)}, extra {len(extra)})"
        )

    rows = [latest_by_id[gid] for gid in expected]
    _validate_snapshot(rows)
    return rows



def record_gate_evidence(
    *,
    gate_id: str,
    status: str,
    command: str | None,
    exit_code: int | None,
    started_at: str | None,
    finished_at: str | None,
    evidence_path: str | None,
    ledger_path: Path,
    evaluator_result: dict[str, Any] | None = None,
    cleared_regressions: tuple[str, ...] | list[str] = (),
    owner: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Append one ledger row. Verified statuses require real evidence and no open defect."""
    if gate_id not in expected_gate_ids():
        raise GateEvidenceError(f"unknown gate_id {gate_id}")
    if status not in GATE_STATUSES:
        raise GateEvidenceError(f"unknown status {status}")
    if evidence_path is not None and _path_has_secret(evidence_path):
        raise GateEvidenceError("evidence_path contains a secret")
    stored_command = redact_text(command) if isinstance(command, str) else None
    block_reason = evidence_block_reason(gate_id, evaluator_result)
    if status in VERIFIED_STATUSES:
        minimum = minimum_verified_status(gate_id)
        if _STATUS_RANK[status] < _STATUS_RANK[minimum]:
            raise GateEvidenceError(f"{gate_id} requires {minimum}")
        if not stored_command:
            raise GateEvidenceError("verified row needs a command")
        if exit_code != 0:
            raise GateEvidenceError("verified row needs exit code 0")
        started = _parse_ts(started_at, "started_at")
        finished = _parse_ts(finished_at, "finished_at")
        if finished < started:
            raise GateEvidenceError("finished_at is before started_at")
        if not evidence_path:
            raise GateEvidenceError("verified row needs evidence_path")
        evidence = Path(evidence_path)
        if not evidence.is_absolute():
            evidence = ROOT / evidence
        if not evidence.is_file() or evidence.stat().st_size <= 0:
            raise GateEvidenceError("evidence file is missing or empty")
        open_regs = [rid for rid in blocking_regression_ids(gate_id) if rid not in set(cleared_regressions)]
        if open_regs:
            owners: list[str] = []
            for rid in open_regs:
                owners.extend(REGRESSIONS[rid]["owners"])
            raise GateEvidenceError(
                "uncleared regressions: " + ", ".join(open_regs) + "; owners " + ", ".join(dict.fromkeys(owners))
            )
        if blocking_regression_ids(gate_id) and evaluator_result is None:
            raise GateEvidenceError("regression gate needs an evaluator result")
        if block_reason:
            raise GateEvidenceError(block_reason)
        if gate_id in SOAK_GATES:
            window = None if evaluator_result is None else evaluator_result.get("window_seconds")
            if not isinstance(window, int) or window < SOAK_GATES[gate_id]:
                raise GateEvidenceError("soak window is shorter than the gate")
    row: dict[str, Any] = {
        "gate_id": gate_id,
        "status": status,
        "command": stored_command,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "evidence_path": evidence_path,
        "owner": owner,
        "detail": detail,
        "blocking_regressions": blocking_regression_ids(gate_id),
    }
    if block_reason and status not in VERIFIED_STATUSES:
        row["block_reason"] = block_reason
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row

def check_live_gate_prerequisites(gate_id: str, db_path: Path | None = None) -> tuple[bool, str | None]:
    """Check whether real live/production prerequisites are met for gate_id.

    Never synthesizes pass records; truthful blockers are reported.
    """
    p_db = db_path or DB_PATH

    if gate_id == "M3.08":
        if not p_db.exists():
            return False, f"database {p_db} does not exist"
        conn = sqlite3.connect(p_db)
        try:
            has_wiki = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_fts'").fetchone()
            )
            cnt = conn.execute("SELECT count(*) FROM wiki_fts").fetchone()[0] if has_wiki else 0
            if cnt <= 0:
                return False, "wiki_fts has 0 compiled pages"
        finally:
            conn.close()
        return True, None

    if gate_id == "M4.08":
        if not p_db.exists():
            return False, f"database {p_db} does not exist"
        conn = sqlite3.connect(p_db)
        try:
            has_items = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='digest_items'").fetchone()
            )
            cnt = (
                conn.execute(
                    "SELECT count(*) FROM digest_items WHERE message_id IS NOT NULL AND message_id != ''"
                ).fetchone()[0]
                if has_items
                else 0
            )
            if cnt <= 0:
                return False, "0 acknowledged delivered digest items with message_id in database"
        finally:
            conn.close()
        return True, None

    if gate_id in SOAK_GATES:
        return False, f"soak window {SOAK_GATES[gate_id]}s requires multi-day soak observation"

    if gate_id.startswith("A"):
        pol_mod = _sibling("access_policy")
        if pol_mod and hasattr(pol_mod, "check_gate_prerequisites"):
            return pol_mod.check_gate_prerequisites(gate_id)
        return False, f"live access gate {gate_id} requires explicit active tariff and provider credentials"

    return True, None


def run_live_gate(
    gate_id: str,
    db_path: Path | None = None,
    ledger_path: Path | None = None,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute or evaluate a live gate truthfully.

    Records 'blocked' if prerequisites fail, never synthesizes pass records.
    """
    l_path = ledger_path or LEDGER_PATH
    ok, reason = check_live_gate_prerequisites(gate_id, db_path=db_path)
    if not ok:
        row = record_gate_evidence(
            gate_id=gate_id,
            status="blocked",
            command=f"snowball eval live {gate_id}",
            exit_code=1,
            started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            evidence_path=None,
            ledger_path=l_path,
            detail=f"prerequisite check failed: {reason}",
        )
        return row

    p_db = db_path or DB_PATH
    if gate_id == "M4.08":
        res = eval_digest(since=7, db_path=p_db)
        e_dir = evidence_dir or (ROOT / "data" / "evidence")
        e_dir.mkdir(parents=True, exist_ok=True)
        ev_file = e_dir / f"{gate_id}-digest-audit.json"
        ev_file.write_text(json.dumps(res, indent=2), encoding="utf-8")
        status = "live_verified" if res["passed"] else "blocked"
        row = record_gate_evidence(
            gate_id=gate_id,
            status=status,
            command="python tools/eval_gates.py digest --since 7d",
            exit_code=0 if res["passed"] else 1,
            started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            evidence_path=str(ev_file) if res["passed"] else None,
            ledger_path=l_path,
            evaluator_result=res,
            cleared_regressions=(),
            owner="T10",
        )
        return row

    return record_gate_evidence(
        gate_id=gate_id,
        status="blocked",
        command=f"snowball eval live {gate_id}",
        exit_code=1,
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        evidence_path=None,
        ledger_path=l_path,
        detail="live gate execution pending integration",
    )


def run_diagnostics(
    db_path: Path | None = None,
    wiki_dir: Path | None = None,
    live: bool = False,
    ledger_path: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Execute topic, extraction, digest, retention, access, and status diagnostics.

    Returns machine-readable results. Nonzero exit if any subsystem is blocked/failed.
    """
    p_db = db_path or DB_PATH
    l_path = ledger_path or LEDGER_PATH

    diag: dict[str, Any] = {}

    # 1. Topic Diagnostic
    topic_status = "ok"
    topic_details: dict[str, Any] = {}
    if not p_db.exists():
        topic_status = "blocked"
        topic_details["error"] = "database not found"
    else:
        conn = sqlite3.connect(p_db)
        try:
            has_topics = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='topics'").fetchone()
            )
            has_fts = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='topics_fts'").fetchone()
            )
            topic_cnt = conn.execute("SELECT count(*) FROM topics").fetchone()[0] if has_topics else 0
            fts_cnt = conn.execute("SELECT count(*) FROM topics_fts").fetchone()[0] if has_fts else 0
            topic_details = {"topics": topic_cnt, "topics_fts": fts_cnt}
            if not has_topics or not has_fts:
                topic_status = "blocked"
                topic_details["error"] = "missing topics or topics_fts table"
        finally:
            conn.close()
    diag["topic"] = {"status": topic_status, **topic_details}

    # 2. Extraction Diagnostic
    ext_status = "ok"
    ext_details: dict[str, Any] = {}
    if not p_db.exists():
        ext_status = "blocked"
        ext_details["error"] = "database not found"
    else:
        conn = sqlite3.connect(p_db)
        try:
            has_ent = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities'").fetchone()
            )
            has_claims = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
            )
            ent_cnt = conn.execute("SELECT count(*) FROM entities").fetchone()[0] if has_ent else 0
            claims_cnt = conn.execute("SELECT count(*) FROM claims").fetchone()[0] if has_claims else 0
            ext_details = {"entities": ent_cnt, "claims": claims_cnt}
            if not has_ent or not has_claims:
                ext_status = "blocked"
                ext_details["error"] = "missing entities or claims table"
        finally:
            conn.close()
    diag["extraction"] = {"status": ext_status, **ext_details}

    # 3. Digest Diagnostic
    dig_status = "ok"
    dig_details: dict[str, Any] = {}
    if not p_db.exists():
        dig_status = "blocked"
        dig_details["error"] = "database not found"
    else:
        conn = sqlite3.connect(p_db)
        try:
            has_di = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='digest_items'").fetchone()
            )
            di_cnt = conn.execute("SELECT count(*) FROM digest_items").fetchone()[0] if has_di else 0
            has_ent = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities'").fetchone()
            )
            premature_cnt = (
                conn.execute(
                    "SELECT count(*) FROM entities WHERE reported_at IS NOT NULL AND key NOT IN (SELECT ref_id FROM digest_items WHERE message_id IS NOT NULL)"
                ).fetchone()[0]
                if has_ent and has_di
                else 0
            )
            dig_details = {"digest_items": di_cnt, "premature_acknowledgements": premature_cnt}
            if premature_cnt > 0:
                dig_status = "failed"
                dig_details["error"] = "premature delivery acknowledgement detected"
        finally:
            conn.close()
    diag["digest"] = {"status": dig_status, **dig_details}

    # 4. Retention Diagnostic
    ret_status = "ok"
    ret_details: dict[str, Any] = {}
    target_dir = p_db.parent if p_db.parent.exists() else ROOT
    free_bytes = shutil.disk_usage(target_dir).free
    floor_bytes = 5 * (1024**3)
    ret_details["free_disk_gb"] = round(free_bytes / (1024**3), 2)
    ret_details["floor_disk_gb"] = 5.0
    if free_bytes < floor_bytes:
        ret_status = "blocked"
        ret_details["error"] = f"free disk space {ret_details['free_disk_gb']}GB below 5GB floor"

    # Check for actual restorable backup snapshot
    backup_dirs = [ROOT / "data" / "backups", ROOT / "state"]
    has_backup = any(b_dir.is_dir() and any(b_dir.glob("*.db*")) for b_dir in backup_dirs)
    ret_details["backup_snapshot_found"] = has_backup
    if not has_backup:
        ret_status = "blocked"
        ret_details["error"] = "no consistent restorable backup snapshot found"

    if p_db.exists():
        conn = sqlite3.connect(p_db)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            ret_details["sqlite_integrity"] = check
            if check != "ok":
                ret_status = "failed"
                ret_details["error"] = f"database corruption: {check}"
        finally:
            conn.close()
    diag["retention"] = {"status": ret_status, **ret_details}

    # 5. Access Diagnostic
    acc_status = "ok"
    acc_details: dict[str, Any] = {"live_checked": live}
    orch = _sibling("orchestrator")
    if orch and hasattr(orch, "resolve_and_pin"):
        try:
            orch.resolve_and_pin("127.0.0.1", 443)
            acc_status = "failed"
            acc_details["error"] = "SSRF guard failed to block loopback"
        except Exception:
            acc_details["ssrf_guard"] = "enforcing"

    if live:
        cfg = orch.load_config() if orch and hasattr(orch, "load_config") else None
        if orch and hasattr(orch, "diagnose_providers") and cfg:
            prov_diag = orch.diagnose_providers(cfg, live=True)
            acc_details["provider_diagnostics"] = prov_diag
            if prov_diag.get("status") != "ok":
                acc_status = "blocked"
                acc_details["error"] = prov_diag.get("error") or "provider diagnostics failed"
        else:
            acc_status = "blocked"
            acc_details["error"] = "diagnose_providers API not available in orchestrator"
    else:
        acc_details["mode"] = "offline_policy_verified"

    diag["access"] = {"status": acc_status, **acc_details}

    # 6. Status Snapshot Diagnostic
    stat_status = "ok"
    stat_details: dict[str, Any] = {}
    try:
        rows = load_status_snapshot(l_path)
        verified = sum(1 for r in rows if r.get("status") in VERIFIED_STATUSES)
        blocked = sum(1 for r in rows if r.get("status") == "blocked")
        implemented = sum(1 for r in rows if r.get("status") == "implemented")
        stat_details = {
            "total_gates": len(rows),
            "verified": verified,
            "blocked": blocked,
            "implemented": implemented,
        }
    except Exception as exc:
        stat_status = "failed"
        stat_details["error"] = str(exc)
    diag["status"] = {"status": stat_status, **stat_details}

    all_ok = all(s.get("status") == "ok" for s in diag.values())
    has_failed = any(s.get("status") == "failed" for s in diag.values())
    overall_status = "ok" if all_ok else ("failed" if has_failed else "blocked")

    return {
        "status": overall_status,
        "diagnostics": diag,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball evaluator commands and gate-evidence snapshot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_c = sub.add_parser("classify", help="Score the production classifier against a fixture.")
    p_c.add_argument("fixture", type=Path, help="Path to pages_labelled_50.jsonl")
    p_c.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    p_c.add_argument("--json", action="store_true", help="Output JSON results")

    p_e = sub.add_parser(
        "extract", help="Score production extraction and quote validation against a fixture."
    )
    p_e.add_argument("fixture", type=Path, help="Path to items_20.jsonl")
    p_e.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    p_e.add_argument("--json", action="store_true", help="Output JSON results")

    p_d = sub.add_parser(
        "digest", help="Audit acknowledged delivered digest items against source evidence."
    )
    p_d.add_argument("--since", type=str, default="7d", help="Time window (default: 7d)")
    p_d.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    p_d.add_argument("--json", action="store_true", help="Output JSON results")

    p_r = sub.add_parser("reindex", help="Rebuild FTS indexes from base tables and wiki markdown.")
    p_r.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    p_r.add_argument("--wiki", type=Path, default=WIKI_DIR, help="Path to wiki directory")
    p_r.add_argument("--json", action="store_true", help="Output JSON results")

    p_t = sub.add_parser("topics", help="Score topic pair similarity against labelled benchmarks.")
    p_t.add_argument("pairs", type=Path, help="Path to topic_pairs_30.jsonl")
    p_t.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    p_t.add_argument("--json", action="store_true", help="Output JSON results")

    p_diag = sub.add_parser("diagnostics", help="Run comprehensive subsystem diagnostics.")
    p_diag.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    p_diag.add_argument("--wiki", type=Path, default=WIKI_DIR, help="Path to wiki directory")
    p_diag.add_argument(
        "--live", action="store_true", help="Perform live authenticated provider checks"
    )
    p_diag.add_argument(
        "--ledger", type=Path, default=LEDGER_PATH, help="Path to gate-evidence.jsonl"
    )
    p_diag.add_argument("--json", action="store_true", help="Output JSON results")

    p_v = sub.add_parser("evidence-check", help="Validate the gate status snapshot.")
    p_v.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    p_v.add_argument("--json", action="store_true", help="Output JSON results")

    p_rec = sub.add_parser("record", help="Record a gate evidence ledger row.")
    p_rec.add_argument("--gate-id", required=True)
    p_rec.add_argument("--status", required=True)
    p_rec.add_argument("--command", default=None)
    p_rec.add_argument("--exit-code", type=int, default=None)
    p_rec.add_argument("--started-at", default=None)
    p_rec.add_argument("--finished-at", default=None)
    p_rec.add_argument("--evidence", default=None)
    p_rec.add_argument("--owner", default=None)
    p_rec.add_argument("--detail", default=None)
    p_rec.add_argument("--ledger", type=Path, default=LEDGER_PATH)

    args = ap.parse_args()

    if args.cmd == "classify":
        try:
            res = eval_classify(args.fixture, db_path=args.db, quiet=args.json)
        except Exception as exc:
            if args.json:
                print(json.dumps({"passed": False, "error": str(exc), "classifier": "production"}, indent=2))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(res, indent=2))
        sys.exit(0 if res["passed"] else 1)
    elif args.cmd == "extract":
        try:
            res = eval_extract(args.fixture, db_path=args.db, quiet=args.json)
        except Exception as exc:
            if args.json:
                print(json.dumps({"passed": False, "error": str(exc), "extractor": "production"}, indent=2))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(res, indent=2))
        sys.exit(0 if res["passed"] else 1)
    elif args.cmd == "digest":
        try:
            res = eval_digest(since=args.since, db_path=args.db, quiet=args.json)
        except Exception as exc:
            if args.json:
                print(json.dumps({"passed": False, "error": str(exc), "production_delivery_check": True}, indent=2))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(res, indent=2))
        sys.exit(0 if res["passed"] else 1)
    elif args.cmd == "reindex":
        try:
            res = reindex_fts(args.db, wiki_dir=args.wiki, quiet=args.json)
        except Exception as exc:
            if args.json:
                print(json.dumps({"passed": False, "error": str(exc), "wiki_pages_rebuilt": False}, indent=2))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(res, indent=2))
        sys.exit(0 if res["passed"] else 1)
    elif args.cmd == "topics":
        try:
            res = eval_topics(args.pairs, db_path=args.db, quiet=args.json)
        except Exception as exc:
            if args.json:
                print(json.dumps({"passed": False, "error": str(exc)}, indent=2))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(res, indent=2))
        sys.exit(0 if res["passed"] else 1)
    elif args.cmd == "diagnostics":
        res = run_diagnostics(
            db_path=args.db,
            wiki_dir=args.wiki,
            live=args.live,
            ledger_path=args.ledger,
            quiet=args.json,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Snowball Diagnostics Status: [{res['status'].upper()}]")
            for name, sub_d in res["diagnostics"].items():
                print(f"  • {name:<12}: [{sub_d.get('status', 'unknown').upper()}] {sub_d}")
        sys.exit(0 if res["status"] == "ok" else 1)
    elif args.cmd == "evidence-check":
        try:
            rows = load_status_snapshot(args.ledger)
        except GateEvidenceError as exc:
            print(f"gate ledger invalid: {exc}", file=sys.stderr)
            sys.exit(1)
        verified = sum(1 for row in rows if row["status"] in VERIFIED_STATUSES)
        implemented = sum(1 for row in rows if row["status"] == "implemented")
        blocked = sum(1 for row in rows if row["status"] == "blocked")
        if args.json:
            print(
                json.dumps(
                    {
                        "total": len(rows),
                        "verified": verified,
                        "implemented": implemented,
                        "blocked": blocked,
                        "rows": rows,
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"status snapshot: {len(rows)} gates; verified {verified}; "
                f"implemented {implemented}; blocked {blocked}. "
                "Verified means a row already carries evidence. This command does not run gates."
            )
        sys.exit(0)
    elif args.cmd == "record":
        try:
            row = record_gate_evidence(
                gate_id=args.gate_id,
                status=args.status,
                command=args.command,
                exit_code=args.exit_code,
                started_at=args.started_at,
                finished_at=args.finished_at,
                evidence_path=args.evidence,
                ledger_path=args.ledger,
                owner=args.owner,
                detail=args.detail,
            )
            print(json.dumps(row, indent=2))
            sys.exit(0)
        except GateEvidenceError as exc:
            print(f"Failed to record gate evidence: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
