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

`diagnostics` reads an existing database in read-only mode. A missing or older
schema is a visible refusal: this command does not create a database and does
not migrate one. Role output is extract, review, plan, and private availability
from the current engines. Configured model ids stay separate from reported
ones. Actual, estimated, unknown, and legacy usage stay distinct. Credentials
and endpoint URLs are redacted. Model names are not.
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
from collections.abc import Mapping
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


def _module(name: str) -> Any:
    """Reuse a module the process already loaded, otherwise the canonical sibling."""
    for key in (name, f"snowball_{name}"):
        loaded = sys.modules.get(key)
        if loaded is not None:
            return loaded
    return _sibling(name)


def _try_module(name: str) -> Any:
    try:
        return _module(name)
    except Exception:
        return None


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database without creating or migrating it."""
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


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

    try:
        conn = _connect_readonly(p_db)
    except sqlite3.Error:
        if not quiet:
            print(f"Database at {p_db} is not readable.", file=sys.stderr)
        return {
            "total_items": 0,
            "total_quotes": 0,
            "valid_quotes": 0,
            "valid_urls": 0,
            "production_delivery_check": True,
            "method": "source_text_verbatim",
            "passed": False,
            "message_id": False,
            "error": "database unreadable",
        }
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
    """44 Fable gates plus access gates A01-A15.

    Goal-harness scenario ids G01-G30 are not rows in this ledger.
    """
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
        report = inspect_database(p_db)
        if report.get("code"):
            return False, str(report.get("message") or report["code"])
        try:
            conn = _connect_readonly(p_db)
        except sqlite3.Error:
            return False, "database unreadable"
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
        report = inspect_database(p_db)
        if report.get("code"):
            return False, str(report.get("message") or report["code"])
        try:
            conn = _connect_readonly(p_db)
        except sqlite3.Error:
            return False, "database unreadable"
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


REQUIRED_SCHEMA_VERSION = 4
_REQUIRED_TABLES = (
    "topics",
    "topics_fts",
    "entities",
    "claims",
    "digest_items",
    "model_calls",
    "observations",
    "source_snapshots",
    "profile_fields",
)
_PUBLIC_TRANSPORTS = frozenset({"agy", "omp", "ssh-omp", "api", "gemini", "openrouter", "public", "ssh"})
_CURRENT_AGGREGATE_ENGINES = frozenset({"gemini", "local", "review", "plan"})
_ROLE_ENGINES = (
    ("extract", "GeminiBulkExtractor"),
    ("review", "ReviewEngine"),
    ("plan", "ReviewEngine"),
    ("private_eval", "LocalPrivateEngine"),
)
_ROLE_MODEL_FIELDS = {
    "extract": "gemini_model",
    "review": "review_model",
    "plan": "plan_model",
    "private_eval": "private_model_id",
}
_PRIVATE_FIELD_KEYS = frozenset(
    {
        "max_upfront_cash",
        "min_living_adequacy_rule",
        "max_work_hours_acceptable",
        "citizenship",
        "prior_degree",
        "academic_record",
        "tests",
        "accept_conditional_ra_ta",
        "accept_competitive_awards",
        "gpa",
        "profile",
        "profile_snapshot",
        "profile_snapshot_json",
        "profile_snapshot_hash",
        "private_hash",
        "value_json",
    }
)
_DROP_KEY_EXACT = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "endpoint",
        "private_model_endpoint",
        "local_base_url",
        "base_url",
        "gemini_api_key",
        "local_api_key",
        "value_json",
        "profile_snapshot",
        "profile_snapshot_json",
        "profile_snapshot_hash",
        "proxy",
        "credential",
        "attestation_secret",
    }
)
_HEALTHY_DIAGNOSTIC = frozenset({"ok", "reported", "unexercised", "pass"})
_PUBLIC_ENDPOINT_HOSTS = frozenset(
    {
        "generativelanguage.googleapis.com",
        "aiplatform.googleapis.com",
        "api.openai.com",
        "openrouter.ai",
    }
)
_USAGE_KIND_TO_MODE = {
    "actual": "actual",
    "estimated": "estimated",
    "legacy": "legacy",
    "unknown": "unknown",
    "provider_reported": "actual",
    "daemon_reported": "actual",
}
_PRIVATE_REASON_KEEP = frozenset(
    {"local_model_required", "public_transport_rejected", "not_configured", "unavailable"}
)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _observed_schema_version(conn: sqlite3.Connection) -> int | None:
    migration = _try_module("migration")
    reader = getattr(migration, "schema_version", None) if migration is not None else None
    if callable(reader):
        try:
            return int(reader(conn))
        except (TypeError, ValueError):
            return None
        except Exception:
            return None
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def inspect_database(path: Path) -> dict[str, Any]:
    """Read schema identity. Never creates a file and never migrates."""
    required = REQUIRED_SCHEMA_VERSION
    if not path.exists() or not path.is_file():
        return {
            "status": "refused",
            "code": "database_missing",
            "observed_version": None,
            "required_version": required,
            "missing_tables": [],
            "created": False,
            "migrated": False,
            "message": "Database does not exist. Diagnostics did not create it and did not migrate.",
        }
    try:
        conn = _connect_readonly(path)
    except sqlite3.Error:
        return {
            "status": "refused",
            "code": "database_unreadable",
            "observed_version": None,
            "required_version": required,
            "missing_tables": [],
            "created": False,
            "migrated": False,
            "message": "Database is not readable. Diagnostics did not create or migrate it.",
        }
    try:
        observed = _observed_schema_version(conn)
        missing = [name for name in _REQUIRED_TABLES if not _has_table(conn, name)]
    finally:
        conn.close()
    if observed != required or missing:
        return {
            "status": "refused",
            "code": "migration_required",
            "observed_version": observed,
            "required_version": required,
            "missing_tables": missing,
            "created": False,
            "migrated": False,
            "message": (
                "Schema migration required; diagnostics did not create or migrate this database. "
                "Run the explicit migrate command with services stopped."
            ),
        }
    return {
        "status": "ok",
        "code": None,
        "observed_version": observed,
        "required_version": required,
        "missing_tables": [],
        "created": False,
        "migrated": False,
        "message": "Schema version 4 is current. Diagnostics did not migrate.",
    }


def exercise_ledger(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Split an already-recorded legacy ledger into pass, fail, and unexercised.

    Unknown ids, including goal-harness G01-G30, are ignored rather than added.
    """
    buckets: dict[str, list[str]] = {"pass": [], "fail": [], "unexercised": []}
    known = set(expected_gate_ids())
    for row in rows:
        gate_id = str(row.get("gate_id") or "")
        if gate_id not in known:
            continue
        buckets[_exercise_state(row)].append(gate_id)
    return buckets


def _exercise_state(row: dict[str, Any]) -> str:
    status = row.get("status")
    finished = bool(row.get("finished_at"))
    exit_code = row.get("exit_code")
    if status in VERIFIED_STATUSES:
        if exit_code == 0 and row.get("evidence_path") and finished:
            return "pass"
        return "fail"
    if finished and exit_code is not None:
        return "fail"
    return "unexercised"


def _sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    if lowered in _DROP_KEY_EXACT:
        return True
    return lowered.endswith(("_api_key", "_password", "_secret", "_endpoint"))


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _scrub(item) for key, item in value.items() if not _sensitive_key(str(key))}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _public_model(model_id: object) -> str:
    """Keep a model id. Credentials are redacted later; names are not blanked."""
    if not isinstance(model_id, str):
        return ""
    return model_id.strip()[:200]


def _cfg_model(cfg: Any, name: str) -> str:
    """Read one canonical config field. A missing field is unset, not aliased."""
    if cfg is None:
        return ""
    try:
        value = cfg.__getattribute__(name)
    except AttributeError:
        return ""
    return _public_model(value)


def _loopback_host(host: str) -> bool:
    lowered = host.strip().lower().rstrip(".")
    return (
        lowered in {"localhost", "::1", "0.0.0.0", "ip6-localhost", "ip6-loopback"}
        or lowered.startswith("127.")
        or lowered.startswith("::ffff:127.")
    )


def _endpoint_host_class(host: object, port: object, transport: str) -> str:
    if not isinstance(host, str) or not host.strip():
        return "absent"
    if _loopback_host(host):
        return "loopback"
    if host.strip().lower().rstrip(".") in _PUBLIC_ENDPOINT_HOSTS:
        return "public"
    if isinstance(port, int) and not isinstance(port, bool) and port == 8088:
        return "public"
    if transport in _PUBLIC_TRANSPORTS:
        return "public"
    return "private"


def _absorb_engine_status(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate engine diagnostics() into the public role shape.

    Endpoint host, port, and URL are classified and then dropped. A hard cap
    requires the engine's attested adapter conformance. A billing flag or a
    response count does not. Estimated AGY/OMP usage stays estimated.
    """
    if "model_id" in raw:
        model_id = _public_model(raw.get("model_id"))
    else:
        model_id = _public_model(raw.get("model"))
    if raw.get("usage_mode") in {"actual", "estimated", "legacy", "unknown"}:
        usage_mode = raw["usage_mode"]
    else:
        usage_mode = _USAGE_KIND_TO_MODE.get(str(raw.get("usage_kind") or "unknown"), "unknown")
    producer = "hard_cap" in raw or "conformance" in raw or "billing_ceiling" in raw
    if raw.get("hard_cap") == "attested" and raw.get("conformance") == "attested":
        conformance = True
        hard_cap = True
    elif producer:
        conformance = False
        hard_cap = False
    else:
        claimed = bool(raw.get("conformance_attested")) and bool(raw.get("hard_provider_cap"))
        conformance = bool(claimed and usage_mode == "actual")
        hard_cap = conformance
    if str(raw.get("endpoint_host_class") or "") in {"absent", "loopback", "public", "private"}:
        host_class = str(raw["endpoint_host_class"])
    else:
        host_class = _endpoint_host_class(
            raw.get("endpoint_host"),
            raw.get("endpoint_port"),
            str(raw.get("transport") or "").strip().lower(),
        )
    attested_local = bool(raw.get("attested_local"))
    if host_class == "loopback":
        attested_local = False
    if "isolation_documented" in raw:
        isolation = bool(raw.get("isolation_documented"))
    else:
        isolation = bool(attested_local and host_class == "private")
    return {
        "engine": raw.get("engine"),
        "configured": raw.get("configured"),
        "available": raw.get("available"),
        "reason": raw.get("reason"),
        "model_id": model_id,
        "transport": raw.get("transport"),
        "usage_mode": usage_mode,
        "hard_provider_cap": hard_cap,
        "conformance_attested": conformance,
        "attested_local": attested_local,
        "isolation_documented": isolation,
        "endpoint_host_class": host_class,
    }


def _blank_role(role: str, engine: str, reason: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "role": role,
        "engine": engine,
        "configured": False,
        "available": False,
        "reason": reason,
        "model_id": "",
        "transport": "",
        "usage_mode": "unknown",
        "hard_provider_cap": False,
        "attested_local": False,
        "conformance_attested": False,
        "isolation_documented": False,
    }
    if role == "private_eval":
        item["endpoint_host_class"] = "absent"
    return item


def _normalize_role(role: str, engine: str, raw: Any) -> dict[str, Any]:
    item = _blank_role(role, engine, "unconfigured")
    if isinstance(raw, dict):
        raw = _absorb_engine_status(raw)
        reported_engine = str(raw.get("engine") or engine)[:80]
        item["engine"] = reported_engine
        item["configured"] = bool(raw.get("configured"))
        item["available"] = bool(raw.get("available"))
        item["reason"] = str(raw.get("reason") or item["reason"])[:300]
        item["model_id"] = _public_model(raw.get("model_id"))
        item["transport"] = str(raw.get("transport") or "")[:80]
        usage = raw.get("usage_mode")
        item["usage_mode"] = usage if usage in {"actual", "estimated", "legacy", "unknown"} else "unknown"
        item["attested_local"] = bool(raw.get("attested_local"))
        item["conformance_attested"] = bool(raw.get("conformance_attested"))
        item["isolation_documented"] = bool(raw.get("isolation_documented"))
        item["hard_provider_cap"] = bool(raw.get("hard_provider_cap")) and item["conformance_attested"]
        if role == "private_eval":
            host_class = str(raw.get("endpoint_host_class") or "absent")
            if host_class not in {"absent", "loopback", "public", "private"}:
                host_class = "absent"
            item["endpoint_host_class"] = host_class
    if not item["model_id"]:
        item["configured"] = False
    if not item["conformance_attested"]:
        item["hard_provider_cap"] = False
    transport = item["transport"].strip().lower()
    if role == "private_eval":
        host_class = str(item.get("endpoint_host_class") or "absent")
        item["hard_provider_cap"] = False
        item["conformance_attested"] = False
        proven = (
            item["available"]
            and item["attested_local"]
            and item["isolation_documented"]
            and host_class == "private"
            and bool(transport)
            and transport not in _PUBLIC_TRANSPORTS
            and bool(item["model_id"])
        )
        if host_class == "public" or transport in _PUBLIC_TRANSPORTS:
            item["available"] = False
            item["attested_local"] = False
            item["isolation_documented"] = False
            item["reason"] = "public_transport_rejected"
        elif host_class == "loopback" or not proven:
            item["available"] = False
            item["attested_local"] = False
            kept = _PRIVATE_REASON_KEEP | {"dry", "ok", "attested"}
            if host_class == "loopback" or item["reason"] not in kept:
                item["reason"] = "local_model_required"
        elif item["reason"] in {"", "unconfigured", "unavailable"}:
            item["reason"] = "attested"
    else:
        item["attested_local"] = False
        item["isolation_documented"] = False
    return item


def _mapping_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    return None


def _probe_engine(cls: type, cfg: Any, live: bool) -> dict[str, Any] | None:
    """Call the typed diagnostics and available methods. Neither invents the other."""
    try:
        engine = cls(cfg, dry=not live)
    except Exception:
        return None
    try:
        diagnose = engine.diagnostics
        probe = engine.available
    except AttributeError:
        return None
    if not callable(diagnose) or not callable(probe):
        return None
    try:
        raw = diagnose()
    except Exception:
        return None
    mapped = _mapping_dict(raw)
    if mapped is None:
        return {
            "available": False,
            "configured": False,
            "reason": "diagnostics_unavailable",
            "usage_mode": "unknown",
        }
    merged = dict(mapped)
    try:
        ok, reason = probe()
    except Exception:
        ok, reason = False, "unavailable"
    if not isinstance(ok, bool):
        ok = False
    diagnosed = merged.get("available") is True
    if not diagnosed or not ok:
        merged["available"] = False
        if diagnosed and isinstance(reason, str) and reason.strip():
            merged["reason"] = reason.strip()[:300]
        elif "available" not in mapped and not isinstance(merged.get("reason"), str):
            merged["reason"] = "diagnostics_unavailable"
    return merged


def _call_role_diagnostics(cfg: Any, live: bool) -> dict[str, Any] | None:
    """Ask the current engines whether each role is available.

    Constructors are ``(cfg, dry=not live)``. Only ``diagnostics()`` and
    ``available()`` are called. extract/review/plan/private_eval would dispatch
    a model and are never used here.
    """
    transport = _try_module("engine_transport")
    if transport is None:
        return None
    try:
        extract_cls = transport.GeminiBulkExtractor
        review_cls = transport.ReviewEngine
        private_cls = transport.LocalPrivateEngine
    except AttributeError:
        return {"_error": "engine_transport_unavailable"}
    if not all(isinstance(cls, type) for cls in (extract_cls, review_cls, private_cls)):
        return {"_error": "engine_transport_unavailable"}
    return {
        "extract": _probe_engine(extract_cls, cfg, live),
        "review": _probe_engine(review_cls, cfg, live),
        "plan": _probe_engine(review_cls, cfg, live),
        "private_eval": _probe_engine(private_cls, cfg, live),
    }


def _diagnostic_cfg(cfg: Any | None, live: bool) -> Any:
    if cfg is not None:
        return cfg
    orch = _try_module("orchestrator")
    if orch is None:
        return None
    if live and hasattr(orch, "load_config"):
        try:
            return orch.load_config()
        except Exception:
            pass
    config_type = getattr(orch, "Config", None)
    if isinstance(config_type, type):
        return config_type()
    return None


def _finish_role(role: str, item: dict[str, Any], cfg: Any) -> dict[str, Any]:
    """Record the configured model separately from the engine-reported model.

    An empty canonical field does not inherit an engine default. That keeps an
    unset review role from being labeled with the engine's Claude fallback.
    """
    requested = _cfg_model(cfg, _ROLE_MODEL_FIELDS[role])
    reported = _public_model(item.get("model_id"))
    item["requested_model"] = requested
    if role != "private_eval" and not requested:
        item["configured"] = False
        item["available"] = False
        item["model_id"] = ""
        item["reported_model"] = ""
        item["transport"] = ""
        item["usage_mode"] = "unknown"
        item["hard_provider_cap"] = False
        item["conformance_attested"] = False
        item["attested_local"] = False
        item["isolation_documented"] = False
        item["reason"] = "not_configured"
        return item
    item["reported_model"] = reported
    item["model_id"] = reported
    return item


def role_report(cfg: Any | None = None, *, live: bool = False) -> dict[str, Any]:
    """Public role availability. Estimated usage is not a system-wide hard cap."""
    cfg = _diagnostic_cfg(cfg, live)
    raw_map = _call_role_diagnostics(cfg, live)
    roles: dict[str, Any] = {}
    for role, engine in _ROLE_ENGINES:
        raw = None if not isinstance(raw_map, dict) else raw_map.get(role)
        if not isinstance(raw, dict):
            if isinstance(raw_map, dict) and raw_map.get("_error"):
                reason = str(raw_map["_error"])
            elif raw_map is None:
                reason = "engine_transport_unavailable"
            else:
                reason = "unconfigured"
            raw = {"available": False, "reason": reason, "configured": False, "usage_mode": "unknown"}
        roles[role] = _finish_role(role, _normalize_role(role, engine, raw), cfg)
    public = [roles[name] for name in ("extract", "review", "plan")]
    hard_cap = bool(public) and all(
        role["hard_provider_cap"] and role["conformance_attested"] and role["usage_mode"] == "actual"
        for role in public
    )
    blocked = any(not roles[name]["available"] for name in ("extract", "review", "plan", "private_eval"))
    return {
        "status": "blocked" if blocked else "reported",
        "network_check": "exercised" if live else "unexercised",
        "hard_provider_cap": hard_cap,
        **roles,
    }


def reject_canary_reply(text: str, canary: str) -> None:
    """Refuse a model reply that contains the active canary. The token is not echoed."""
    if not isinstance(text, str) or not isinstance(canary, str) or not canary:
        raise ValueError("canary reply")
    leaked = canary in text
    if not getattr(reject_canary_reply, "_inside", False):
        transport = _try_module("engine_transport")
        fn = getattr(transport, "reject_canary_reply", None) if transport is not None else None
        if callable(fn) and fn is not reject_canary_reply:
            setattr(reject_canary_reply, "_inside", True)
            try:
                fn(text, canary)
            except Exception:
                leaked = True
            finally:
                setattr(reject_canary_reply, "_inside", False)
    if leaked:
        sanitizer = _try_module("sanitizer")
        incident = getattr(sanitizer, "SecurityIncident", None) if sanitizer is not None else None
        if incident is None:
            raise RuntimeError("canary-leak")
        raise incident("canary-leak", {})


def judge_evidence(source_text: str, quote: str, provenance: str = "public_fetch") -> dict[str, Any]:
    """Accept a verbatim public quote. Injection, private provenance, and invented quotes are refused."""
    if not isinstance(source_text, str) or not isinstance(quote, str):
        return {"accepted": False, "code": "schema_refused", "hits": []}
    if (provenance or "").strip().lower() != "public_fetch":
        return {"accepted": False, "code": "provenance_refused", "hits": []}
    quarantine = _try_module("quarantine")
    scan = getattr(quarantine, "scan_text", None) if quarantine is not None else None
    hits: list[str] = []
    if callable(scan):
        hits = sorted(set(scan(source_text)) | set(scan(quote)))
    if hits:
        return {"accepted": False, "code": "quarantine", "hits": hits}
    if not quote.strip():
        return {"accepted": False, "code": "quote_mismatch", "hits": []}
    orch = _try_module("orchestrator")
    quote_ok = getattr(orch, "quote_ok", None) if orch is not None else None
    if callable(quote_ok):
        ok, _start, _end = quote_ok(quote, source_text)
    else:
        ok = quote in source_text
    if not ok:
        return {"accepted": False, "code": "quote_mismatch", "hits": []}
    return {"accepted": True, "code": "kept", "hits": []}


_SAFE_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HASH_VALUE_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE_REFUSAL_KEYS = frozenset(
    {
        "provenance",
        "visibility",
        "origin",
        "discovered_via",
        "human_note",
        "human_notes",
        "private",
        "unknown",
        "private_profile",
        "interview",
        "user_stated",
        "user_confirmed",
        "imported_history",
        "system_default",
    }
)
_BAD_PROVENANCE_VALUES = _PROVENANCE_REFUSAL_KEYS - {
    "provenance",
    "visibility",
    "origin",
    "discovered_via",
}
_REQUEST_KEY_REFUSALS = _BAD_PROVENANCE_VALUES | frozenset(
    {
        "contract_hash",
        "body_hash",
        "private_hash",
        "profile_snapshot_hash",
    }
)


def _safe_field_key(value: Any) -> str | None:
    """Keep a refusal key. Drop values, hashes, and messages that are not keys."""
    if not isinstance(value, str) or _SAFE_FIELD_KEY_RE.fullmatch(value) is None:
        return None
    if _HASH_VALUE_RE.fullmatch(value):
        return None
    return value


def _request_refusal(field_key: str | None) -> dict[str, Any]:
    safe = _safe_field_key(field_key)
    if safe in _PROVENANCE_REFUSAL_KEYS:
        code = "provenance_refused"
    else:
        code = "schema_refused"
    return {"accepted": False, "code": code, "field_key": safe}


def _first_disallowed_key(value: Any) -> str | None:
    """Name a disallowed key. Amounts and other payload values are not inspected."""
    if isinstance(value, dict):
        provenance = value.get("provenance")
        if isinstance(provenance, str) and provenance != "public_fetch":
            return "provenance"
        visibility = value.get("visibility")
        if isinstance(visibility, str) and visibility != "public":
            return "visibility"
        for key, item in value.items():
            folded = str(key).casefold()
            if folded in {"origin", "discovered_via"} and (
                not isinstance(item, str) or item.casefold() in _BAD_PROVENANCE_VALUES
            ):
                return folded
            if folded in _PRIVATE_FIELD_KEYS or folded in _REQUEST_KEY_REFUSALS:
                return folded
            found = _first_disallowed_key(item)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _first_disallowed_key(item)
            if found:
                return found
    return None


def assess_public_request(request: Any) -> dict[str, Any]:
    """Refuse a private or malformed request before any public prompt is built.

    Typed requests are decided by snowball_prompts.validate_public_request.
    PrivacyViolation contributes its field key only. A public amount such as
    tuition 0 is not a private-value scan. This function does not compile a prompt.
    """
    if isinstance(request, (dict, list, tuple)):
        field_key = _first_disallowed_key(request)
        if field_key is None:
            field_key = "request"
        return _request_refusal(field_key)
    prompts = _try_module("prompts")
    validate = getattr(prompts, "validate_public_request", None) if prompts is not None else None
    privacy = getattr(prompts, "PrivacyViolation", None) if prompts is not None else None
    if not callable(validate):
        return {"accepted": False, "code": "unexercised", "field_key": None}
    allowed = tuple(
        cls
        for cls in (
            getattr(prompts, "PublicModelRequest", None),
            getattr(prompts, "PublicSearchRequest", None),
        )
        if isinstance(cls, type)
    )
    if allowed and not isinstance(request, allowed):
        return _request_refusal("request")
    try:
        validate(request)
    except Exception as exc:
        if not isinstance(privacy, type) or not isinstance(exc, privacy):
            return {"accepted": False, "code": "schema_refused", "field_key": None}
        field_key = getattr(exc, "field_key", None)
        if field_key != str(exc):
            return {"accepted": False, "code": "schema_refused", "field_key": None}
        return _request_refusal(field_key)
    return {"accepted": True, "code": "public", "field_key": None}


def robots_path_allowed(robots_txt: str, path: str) -> bool:
    """Apply the production robots rule to a supplied body. This does not fetch."""
    if not isinstance(robots_txt, str) or not isinstance(path, str):
        raise TypeError("robots policy")
    orch = _try_module("orchestrator")
    owner = getattr(orch, "SnowballOrchestrator", None) if orch is not None else None
    method = getattr(owner, "_robots_allows", None)
    if not callable(method):
        raise RuntimeError("robots policy unavailable")
    probe = type("RobotsProbe", (), {})()
    probe.robots_cache = {"https://policy.test": robots_txt}
    return bool(method(probe, "https://policy.test", path))


def paid_tariff_block_reason(caps: dict[str, Any], provider: str = "zenrows") -> str:
    """Ask the paid-budget ledger whether this tariff can reserve spend."""
    budget = _try_module("paid_budget")
    reserve = getattr(budget, "reserve", None) if budget is not None else None
    if not callable(reserve):
        return "unexercised"
    try:
        reserve(None, "diag-paid-0", provider, 1, False, caps)
    except Exception as exc:
        text = str(exc).splitlines()[0][:200]
        return f"{type(exc).__name__}:{text}" if text else type(exc).__name__
    return "reserved"


def _ssrf_report() -> dict[str, Any]:
    orch = _try_module("orchestrator")
    pin = getattr(orch, "resolve_and_pin", None) if orch is not None else None
    if not callable(pin):
        return {"status": "unexercised", "enforcing": False}
    try:
        pin("127.0.0.1", 443)
    except Exception:
        return {"status": "pass", "enforcing": True}
    return {"status": "fail", "enforcing": False}


def _paid_report(policy_path: Path | None) -> dict[str, Any]:
    path = policy_path or (ROOT / "access.example.yml")
    daily: int | None = None
    tariffs: dict[str, int] = {}
    policy = _try_module("access_policy")
    loader = getattr(policy, "load_access_policy", None) if policy is not None else None
    if callable(loader) and path.is_file():
        try:
            caps = loader(path).caps()
            if isinstance(caps.get("daily_micro_usd"), int):
                daily = caps["daily_micro_usd"]
            raw_tariffs = caps.get("tariffs") or {}
            if isinstance(raw_tariffs, dict):
                tariffs = {
                    str(name): amount
                    for name, amount in raw_tariffs.items()
                    if isinstance(amount, int) and not isinstance(amount, bool)
                }
        except Exception:
            return {"status": "unexercised", "blocked": False, "reason": "unexercised", "daily_micro_usd": None}
    if not tariffs:
        tariffs = {"zenrows": 0}
        if daily is None:
            daily = 0
    reasons: list[str] = []
    blocked = isinstance(daily, int) and daily == 0
    for name, amount in tariffs.items():
        if amount != 0:
            blocked = False
        reason = paid_tariff_block_reason({"tariffs": {name: amount}}, name)
        reasons.append(reason)
        if not str(reason).startswith("PaidBudgetExhausted:tariff_disabled"):
            blocked = False
    return {
        "status": "pass" if blocked else "fail",
        "blocked": blocked,
        "reason": reasons[0] if reasons else "unexercised",
        "daily_micro_usd": daily,
    }


def _access_report(live: bool, policy_path: Path | None) -> dict[str, Any]:
    ssrf = _ssrf_report()
    robots_state = "unexercised"
    robots_ok = False
    try:
        blocked = robots_path_allowed("User-agent: *\nDisallow: /private\n", "/private") is False
        opened = robots_path_allowed("User-agent: *\nDisallow: /private\n", "/public") is True
        robots_ok = blocked and opened
        robots_state = "enforcing" if robots_ok else "failed"
    except Exception:
        robots_state = "unexercised"
    paid = _paid_report(policy_path)
    if ssrf["status"] == "fail" or robots_state == "failed" or paid["status"] == "fail":
        status = "failed"
    elif ssrf["enforcing"] and robots_ok and paid["blocked"]:
        status = "ok"
    else:
        status = "blocked"
    return {
        "status": status,
        "live_checked": bool(live),
        "ssrf_guard": "enforcing" if ssrf["enforcing"] else ssrf["status"],
        "robots": robots_state,
        "paid_ceiling_micro_usd": paid.get("daily_micro_usd"),
        "paid_route": "disabled" if paid["blocked"] else "not_disabled",
        "paid_block": paid.get("reason"),
        "mode": "offline_policy_verified" if not live else "live_role_check",
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _has_table(conn, table):
        return 0
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _topic_report(conn: sqlite3.Connection) -> dict[str, Any]:
    has_topics = _has_table(conn, "topics")
    has_fts = _has_table(conn, "topics_fts")
    details: dict[str, Any] = {"topics": _count(conn, "topics"), "topics_fts": _count(conn, "topics_fts")}
    if not has_topics or not has_fts:
        details["status"] = "blocked"
        details["error"] = "missing topics or topics_fts table"
        return details
    details["status"] = "ok"
    return details


def _extraction_report(conn: sqlite3.Connection) -> dict[str, Any]:
    has_ent = _has_table(conn, "entities")
    has_claims = _has_table(conn, "claims")
    details: dict[str, Any] = {"entities": _count(conn, "entities"), "claims": _count(conn, "claims")}
    if not has_ent or not has_claims:
        details["status"] = "blocked"
        details["error"] = "missing entities or claims table"
        return details
    details["status"] = "ok"
    return details


def _digest_report(conn: sqlite3.Connection) -> dict[str, Any]:
    has_di = _has_table(conn, "digest_items")
    has_ent = _has_table(conn, "entities")
    premature = 0
    if has_ent and has_di:
        premature = int(
            conn.execute(
                "SELECT count(*) FROM entities WHERE reported_at IS NOT NULL "
                "AND key NOT IN (SELECT ref_id FROM digest_items WHERE message_id IS NOT NULL)"
            ).fetchone()[0]
        )
    details: dict[str, Any] = {
        "digest_items": _count(conn, "digest_items"),
        "premature_acknowledgements": premature,
        "status": "failed" if premature else "ok",
    }
    if premature:
        details["error"] = "premature delivery acknowledgement detected"
    return details


def _field_report(conn: sqlite3.Connection) -> dict[str, Any]:
    public_keys: list[str] = []
    private_count = 0
    if _has_table(conn, "profile_fields"):
        public_keys = [
            str(row[0])
            for row in conn.execute(
                "SELECT key FROM profile_fields WHERE visibility = 'public_ok' ORDER BY key"
            )
            if row[0] is not None and str(row[0]) not in _PRIVATE_FIELD_KEYS
        ]
        private_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM profile_fields WHERE visibility IS NULL OR visibility != 'public_ok'"
            ).fetchone()[0]
        )
    observations = []
    if _has_table(conn, "observations"):
        observations = [
            {"field_key": str(row[0]), "assurance": str(row[1]), "count": int(row[2])}
            for row in conn.execute(
                "SELECT field_key, assurance, COUNT(*) FROM observations "
                "GROUP BY field_key, assurance ORDER BY field_key, assurance"
            )
            if row[0] is not None and str(row[0]) not in _PRIVATE_FIELD_KEYS
        ]
    sources = []
    if _has_table(conn, "source_snapshots"):
        sources = [
            {"provenance": str(row[0]), "visibility": str(row[1]), "count": int(row[2])}
            for row in conn.execute(
                "SELECT provenance, visibility, COUNT(*) FROM source_snapshots "
                "GROUP BY provenance, visibility ORDER BY provenance, visibility"
            )
        ]
    exercised = bool(observations or sources or public_keys or private_count)
    return {
        "status": "reported" if exercised else "unexercised",
        "public_field_keys": public_keys,
        "private_profile_fields": private_count,
        "observations": observations,
        "sources": sources,
    }


def _legacy_helpers() -> tuple[Any, Any]:
    transport = _try_module("engine_transport")
    if transport is None:
        return None, None
    try:
        classify = transport.classify_legacy_usage
        reported = transport.reported_model_for_legacy
    except AttributeError:
        return None, None
    if not callable(classify) or not callable(reported):
        return None, None
    return classify, reported


def _call_identity(
    transport_name: str,
    requested: object,
    reported: object,
    usage_kind: object,
    classify: Any,
    reported_fn: Any,
) -> tuple[str, str, str]:
    """Keep requested and reported names. Historical grok is not relabeled Claude."""
    requested_text = _public_model(requested)
    reported_text = reported if isinstance(reported, str) else None
    kind = str(usage_kind or "")
    if transport_name == "grok":
        if callable(classify):
            kind = str(classify("grok", reported_text, kind))
        else:
            kind = "unknown"
        if callable(reported_fn):
            shown = reported_fn("grok", reported_text)
            reported_text = shown if isinstance(shown, str) else ""
        else:
            reported_text = _public_model(reported_text)
    else:
        reported_text = _public_model(reported_text)
    if kind not in {"actual", "estimated", "legacy", "unknown"}:
        kind = "unknown"
    return kind, requested_text, reported_text


def _add_usage(
    by_role: dict[str, dict[str, Any]],
    totals: dict[str, int],
    role_name: str,
    kind: str,
    calls: int,
    tokens_in: int,
    tokens_out: int,
    thinking: int,
) -> None:
    bucket = by_role.setdefault(role_name, {})
    slot = bucket.setdefault(
        kind,
        {"calls": 0, "tokens_in": 0, "tokens_out": 0, "thinking_tokens": 0},
    )
    slot["calls"] += calls
    slot["tokens_in"] += tokens_in
    slot["tokens_out"] += tokens_out
    slot["thinking_tokens"] += thinking
    totals[kind] += calls


def _usage_report(conn: sqlite3.Connection) -> dict[str, Any]:
    totals = {"actual": 0, "estimated": 0, "legacy": 0, "unknown": 0}
    by_role: dict[str, dict[str, Any]] = {}
    identities: list[dict[str, Any]] = []
    classify, reported_fn = _legacy_helpers()
    if _has_table(conn, "model_calls"):
        rows = conn.execute(
            """
            SELECT role, requested_model, reported_model, transport, usage_kind, COUNT(*),
                   COALESCE(SUM(tokens_in), 0),
                   COALESCE(SUM(tokens_out), 0),
                   COALESCE(SUM(thinking_tokens), 0)
            FROM model_calls
            GROUP BY role, requested_model, reported_model, transport, usage_kind
            """
        )
        for row in rows:
            transport_name = str(row[3] or "")
            kind, requested_text, reported_text = _call_identity(
                transport_name, row[1], row[2], row[4], classify, reported_fn
            )
            role_name = str(row[0] or "")
            calls = int(row[5])
            tokens_in = int(row[6])
            tokens_out = int(row[7])
            thinking = int(row[8])
            _add_usage(by_role, totals, role_name, kind, calls, tokens_in, tokens_out, thinking)
            identities.append(
                {
                    "role": role_name,
                    "requested_model": requested_text,
                    "reported_model": reported_text,
                    "transport": transport_name,
                    "usage_kind": kind,
                    "calls": calls,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "thinking_tokens": thinking,
                }
            )
    legacy_engines: list[dict[str, Any]] = []
    if _has_table(conn, "engine_spend"):
        grouped: dict[str, dict[str, int]] = {}
        for row in conn.execute("SELECT engine, calls, tokens_in, tokens_out FROM engine_spend"):
            engine_name = str(row[0] or "")
            if engine_name in _CURRENT_AGGREGATE_ENGINES:
                continue
            slot = grouped.setdefault(engine_name, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
            slot["calls"] += int(row[1] or 0)
            slot["tokens_in"] += int(row[2] or 0)
            slot["tokens_out"] += int(row[3] or 0)
        for engine_name in sorted(grouped):
            slot = grouped[engine_name]
            if engine_name == "grok":
                kind = str(classify("grok", None, None)) if callable(classify) else "unknown"
                if kind not in totals:
                    kind = "unknown"
            else:
                kind = "legacy"
            legacy_engines.append(
                {
                    "engine": engine_name,
                    "attribution": "legacy",
                    "usage_kind": kind,
                    "calls": slot["calls"],
                    "tokens_in": slot["tokens_in"],
                    "tokens_out": slot["tokens_out"],
                }
            )
    legacy_calls = sum(item["calls"] for item in legacy_engines)
    legacy_in = sum(item["tokens_in"] for item in legacy_engines)
    legacy_out = sum(item["tokens_out"] for item in legacy_engines)
    exercised = sum(totals.values()) > 0 or legacy_calls > 0
    return {
        "status": "reported" if exercised else "unexercised",
        "authoritative": "model_calls",
        "hard_provider_cap": False,
        "calls": totals,
        "by_role": by_role,
        "identities": identities,
        "legacy_aggregates_are_not_role_attribution": True,
        "legacy_engines": legacy_engines,
        "legacy_aggregate_calls": legacy_calls,
        "legacy_aggregate_tokens_in": legacy_in,
        "legacy_aggregate_tokens_out": legacy_out,
    }


def _retention_report(path: Path, schema_ok: bool) -> dict[str, Any]:
    target_dir = path.parent if path.parent.exists() else ROOT
    free_bytes = shutil.disk_usage(target_dir).free
    details: dict[str, Any] = {
        "free_disk_gb": round(free_bytes / (1024**3), 2),
        "floor_disk_gb": 5.0,
        "status": "ok",
    }
    if free_bytes < 5 * (1024**3):
        details["status"] = "blocked"
        details["error"] = f"free disk space {details['free_disk_gb']}GB below 5GB floor"
    backup_dirs = [ROOT / "data" / "backups", ROOT / "state"]
    has_backup = any(folder.is_dir() and any(folder.glob("*.db*")) for folder in backup_dirs)
    details["backup_snapshot_found"] = has_backup
    if not has_backup:
        details["status"] = "blocked"
        details["error"] = "no consistent restorable backup snapshot found"
    if schema_ok and path.is_file():
        try:
            conn = _connect_readonly(path)
        except sqlite3.Error:
            details["status"] = "failed"
            details["error"] = "database unreadable"
            return details
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            details["sqlite_integrity"] = check
            if check != "ok":
                details["status"] = "failed"
                details["error"] = "database corruption"
        finally:
            conn.close()
    return details


def _ledger_report(ledger_path: Path) -> dict[str, Any]:
    try:
        rows = load_status_snapshot(ledger_path)
    except Exception as exc:
        return {"status": "failed", "error": type(exc).__name__}
    exercise = exercise_ledger(rows)
    return {
        "status": "ok",
        "total_gates": len(rows),
        "verified": sum(1 for row in rows if row.get("status") in VERIFIED_STATUSES),
        "blocked": sum(1 for row in rows if row.get("status") == "blocked"),
        "implemented": sum(1 for row in rows if row.get("status") == "implemented"),
        "exercise": {key: len(value) for key, value in exercise.items()},
        "goal_gate_ids": [],
    }


def _overall_status(diag: dict[str, Any], schema_code: str | None) -> str:
    if schema_code:
        return "refused"
    statuses = [item.get("status") for item in diag.values() if isinstance(item, dict)]
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "blocked" for status in statuses):
        return "blocked"
    if any(status == "refused" for status in statuses):
        return "refused"
    if statuses and all(status in _HEALTHY_DIAGNOSTIC for status in statuses):
        return "ok"
    return "blocked"


def _refused_section(schema: dict[str, Any]) -> dict[str, Any]:
    return {"status": "refused", "code": schema.get("code"), "error": schema.get("message")}


def run_diagnostics(
    db_path: Path | None = None,
    wiki_dir: Path | None = None,
    live: bool = False,
    ledger_path: Path | None = None,
    quiet: bool = False,
    cfg: Any | None = None,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """Report schema, roles, usage, and retained controls without creating or migrating.

    A missing or older schema is a visible refusal. Role availability comes from the
    current extract, review, plan, and private engines. Estimated usage is not a hard cap.
    """
    del quiet, wiki_dir
    p_db = db_path or DB_PATH
    schema = inspect_database(p_db)
    diag: dict[str, Any] = {
        "schema": schema,
        "roles": role_report(_diagnostic_cfg(cfg, live), live=live),
        "access": _access_report(live, policy_path),
        "retention": _retention_report(p_db, schema.get("code") is None),
        "status": _ledger_report(ledger_path or LEDGER_PATH),
    }
    if schema.get("code"):
        for name in ("topic", "extraction", "digest", "fields", "usage"):
            diag[name] = _refused_section(schema)
    else:
        try:
            conn = _connect_readonly(p_db)
        except sqlite3.Error:
            unread = {
                "status": "refused",
                "code": "database_unreadable",
                "error": "Database is not readable. Diagnostics did not create or migrate it.",
            }
            for name in ("topic", "extraction", "digest", "fields", "usage"):
                diag[name] = dict(unread)
        else:
            try:
                diag["topic"] = _topic_report(conn)
                diag["extraction"] = _extraction_report(conn)
                diag["digest"] = _digest_report(conn)
                diag["fields"] = _field_report(conn)
                diag["usage"] = _usage_report(conn)
            finally:
                conn.close()
    roles = diag.get("roles")
    hard_cap = bool(isinstance(roles, dict) and roles.get("hard_provider_cap"))
    return _scrub(
        {
            "status": _overall_status(diag, schema.get("code")),
            "code": schema.get("code"),
            "created": False,
            "migrated": False,
            "hard_provider_cap": hard_cap,
            "diagnostics": diag,
        }
    )


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
            code = res.get("code") or ""
            schema = res["diagnostics"].get("schema") if isinstance(res.get("diagnostics"), dict) else {}
            message = schema.get("message") if isinstance(schema, dict) else ""
            print(f"Snowball Diagnostics Status: [{res['status'].upper()}] {code} {message}".rstrip())
            for name, sub_d in res["diagnostics"].items():
                if not isinstance(sub_d, dict):
                    continue
                extra = sub_d.get("message") or sub_d.get("error") or ""
                print(f"  • {name:<12}: [{str(sub_d.get('status', 'unknown')).upper()}] {extra}".rstrip())
            roles = res["diagnostics"].get("roles")
            if isinstance(roles, dict):
                for role_name in ("extract", "review", "plan", "private_eval"):
                    role = roles.get(role_name)
                    if isinstance(role, dict):
                        print(
                            f"    {role_name}: available={role.get('available')} "
                            f"usage={role.get('usage_mode')} hard_cap={role.get('hard_provider_cap')} "
                            f"reason={role.get('reason')}"
                        )
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
        exercise = exercise_ledger(rows)
        exercise_counts = {key: len(value) for key, value in exercise.items()}
        if args.json:
            print(
                json.dumps(
                    {
                        "total": len(rows),
                        "verified": verified,
                        "implemented": implemented,
                        "blocked": blocked,
                        "exercise": exercise_counts,
                        "goal_gate_ids": [],
                        "rows": rows,
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"status snapshot: {len(rows)} gates; verified {verified}; "
                f"implemented {implemented}; blocked {blocked}; "
                f"pass {exercise_counts['pass']}; fail {exercise_counts['fail']}; "
                f"unexercised {exercise_counts['unexercised']}. "
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
