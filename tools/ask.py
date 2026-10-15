#!/usr/bin/env python3
"""Snowball Semantic Question Answering Engine (snowball ask).

Queries the local knowledge base (wiki_fts, wiki markdown) using SQLite FTS5 BM25 retrieval
and generates citation-grounded answers citing [path].
"""

from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
import math
import os
from pathlib import Path
import re
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


class BM25:
    """Pure-Python BM25 context filter (Section 7.3)."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = docs
        self.avgdl = sum(len(d) for d in docs) / max(1, len(docs))
        self.df = Counter(t for d in docs for t in set(d))
        self.N = len(docs)

    def idf(self, t: str) -> float:
        return math.log(1.0 + (self.N - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))

    def score(self, q: list[str], i: int) -> float:
        d = self.docs[i]
        tf = Counter(d)
        dl = len(d)
        return sum(
            self.idf(t) * tf[t] * (self.k1 + 1.0) / (tf[t] + self.k1 * (1.0 - self.b + self.b * dl / max(1.0, self.avgdl)))
            for t in q
            if t in tf
        )


def filter_chunks(
    chunks: list[dict[str, Any]],
    query: str,
    keep_ratio: float = 0.5,
    top: int = 25,
) -> list[dict[str, Any]]:
    """Filter candidate chunks to top <=25 by relative BM25 threshold."""
    if not chunks:
        return []
    toks = [re.sub(r"[^\w\s]", " ", c["text"].casefold()).split() for c in chunks]
    bm = BM25(toks)
    q = re.sub(r"[^\w\s]", " ", query.casefold()).split()
    scored = sorted(((bm.score(q, i), i) for i in range(len(chunks))), reverse=True)
    if not scored or scored[0][0] <= 0:
        return chunks[:top]
    mx = scored[0][0]
    keep = sorted(i for s, i in scored[:top] if s >= keep_ratio * mx)
    return [chunks[i] for i in keep]


def sanitize_fts_query(query: str) -> str:
    """Sanitize user query for SQLite FTS5 MATCH.

    Balances quotes, neutralizes operators, extracts safe tokens and phrases.
    """
    clean = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", query).strip()
    if not clean:
        return ""

    # Balance double quotes: if odd count, replace with space
    if clean.count('"') % 2 != 0:
        clean = clean.replace('"', " ")

    # Extract quoted phrases
    phrases: list[str] = []

    def _save_phrase(m: re.Match) -> str:
        phrases.append(m.group(1).strip())
        return " "

    remaining = re.sub(r'"([^"]*)"', _save_phrase, clean)

    reserved = {"AND", "OR", "NOT", "NEAR"}
    tokens: list[str] = []
    for raw in re.findall(r"[\w\d\-_]+", remaining):
        c = raw.strip("-_")
        if not c or c.upper() in reserved:
            continue
        tokens.append(c)

    clean_phrases: list[str] = []
    for ph in phrases:
        p_words = [w.strip("-_") for w in re.findall(r"[\w\d\-_]+", ph) if w.strip("-_")]
        if p_words:
            clean_phrases.append(" ".join(p_words))

    parts: list[str] = []
    for ph in clean_phrases:
        parts.append(f'"{ph}"')
    for tok in tokens:
        parts.append(f'"{tok}"')

    if not parts:
        return ""
    return " OR ".join(parts)


class KnowledgeReadError(RuntimeError):
    """A readonly knowledge read failed. This is not an empty search result."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


_REGION_RX = re.compile(
    r"<!-- snowball:([a-zA-Z0-9_-]+):start -->.*?<!-- snowball:\1:end -->\n?",
    re.S,
)
_SOURCE_REGION_KEYS = frozenset({
    "facts",
    "changes",
    "sources",
    "overview",
    "entities",
    "topics",
    "current_evidence",
    "historical_evidence",
    "provenance",
})
_BLOCKED_ENDPOINT_MARKERS = (
    "googleapis.com",
    "openrouter.ai",
    ":8088",
    "agy-pool",
    "api.openai.com",
)
_CLOUD_MODEL_MARKERS = (
    "agy-pool/",
    "openrouter",
    "claude",
    "gpt-",
    "grok",
    "gemini",
)
_PRIVATE_FIELD_KEYS = (
    "max_upfront_cash",
    "max_net_total_cost",
    "min_living_adequacy_rule",
)
_PROVENANCE_LABEL = {
    "human_note": "HUMAN_NOTE",
    "lead": "LEAD",
    "source_evidence": "source_evidence",
}


def _is_lead_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("- LEAD") or stripped.startswith("LEAD ")


def segment_page(body: str) -> list[tuple[str, str]]:
    """Split a page into human notes, legacy leads, and source-evidence text."""
    pieces: list[tuple[str, str]] = []
    cursor = 0
    for match in _REGION_RX.finditer(body):
        if match.start() > cursor:
            gap = body[cursor : match.start()]
            if gap:
                pieces.append(("human_note", gap))
        key = match.group(1).lower()
        block = match.group(0)
        if key in _SOURCE_REGION_KEYS:
            pieces.extend(_split_lead_block(block))
        else:
            pieces.append(("human_note", block))
        cursor = match.end()
    tail = body[cursor:]
    if tail:
        pieces.append(("human_note", tail))
    if not pieces and body:
        pieces.append(("human_note", body))
    return pieces


def _split_lead_block(block: str) -> list[tuple[str, str]]:
    runs: list[tuple[str, list[str]]] = []
    for line in block.splitlines(keepends=True):
        kind = "lead" if _is_lead_line(line) else "source_evidence"
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(line)
        else:
            runs.append((kind, [line]))
    return [(kind, "".join(lines)) for kind, lines in runs if "".join(lines).strip()]


def _chunks_from_body(path: str, title: str, body: str, chunk_chars: int = 800) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for provenance, segment in segment_page(body):
        for offset in range(0, max(1, len(segment)), chunk_chars):
            text = segment[offset : offset + chunk_chars].strip()
            if not text:
                continue
            chunks.append({
                "path": path,
                "title": title,
                "text": text,
                "provenance": provenance,
                "qualified": False,
            })
    return chunks


def get_db(path: Path | None = None) -> sqlite3.Connection:
    """Open one readonly connection. Never fall back to a writable handle."""
    p = path or DB_PATH
    if not p.exists() or not p.is_file():
        raise KnowledgeReadError(
            f"Database not found at {p}. Run orchestrator or compile first.",
            code="not_found",
        )
    uri = p.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise KnowledgeReadError(f"readonly open failed: {exc}", code="readonly_open") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise KnowledgeReadError(f"readonly read failed: {exc}", code="readonly_read") from exc
    return conn


def search_local_knowledge(
    conn: sqlite3.Connection | None,
    query: str,
    wiki_dir: Path | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """Retrieve matching wiki pages (top 12 BM25) and filter to <=25 chunks.

    Does not mutate DB or reindex during query reads.
    """
    w_dir = (wiki_dir or WIKI_DIR).resolve()
    limit = max(1, min(12, int(limit)))

    clean_fts = sanitize_fts_query(query)
    candidate_pages: list[dict[str, Any]] = []
    matched_entities: list[dict[str, Any]] = []
    matched_claims: list[dict[str, Any]] = []

    if conn is not None:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        if clean_fts:
            has_wiki_fts = bool(
                cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_fts'").fetchone()
            )
            if has_wiki_fts:
                cur.execute(
                    """
                    SELECT path, title, body, bm25(wiki_fts) AS rank
                    FROM wiki_fts
                    WHERE wiki_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (clean_fts, limit),
                )
                for r in cur.fetchall():
                    candidate_pages.append(dict(r))

        # Query entities table if present (e2e compatibility)
        has_entities = bool(
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities'").fetchone()
        )
        if has_entities:
                q_words = [w for w in re.findall(r"\w+", query) if len(w) > 2] or query.split() or ["grant"]
                like_clauses = " OR ".join(["e.name LIKE ? OR e.eligibility LIKE ? OR e.kind LIKE ?"] * len(q_words))
                params: list[Any] = []
                for w in q_words:
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

        # Query claims table if present
        has_claims = bool(
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
        )
        if has_claims:
                q_words = [w for w in re.findall(r"\w+", query) if len(w) > 2] or query.split()
                if q_words:
                    claim_cols = {
                        str(row[1]) for row in cur.execute("PRAGMA table_info(claims)")
                    }
                    provenance_sql = (
                        "c.provenance" if "provenance" in claim_cols else "'legacy_v3'"
                    )
                    cur.execute(
                        f"""
                        SELECT c.hash, c.text, c.n_sources, p.canon_url AS url,
                               {provenance_sql} AS provenance
                        FROM claims c
                        LEFT JOIN claim_sources cs ON c.hash = cs.claim_hash
                        LEFT JOIN pages p ON cs.page_id = p.id
                        WHERE c.text LIKE ?
                        LIMIT ?
                        """,
                        (f"%{q_words[0]}%", limit),
                    )
                    for r in cur.fetchall():
                        matched_claims.append({
                            "text": r["text"],
                            "url": r["url"],
                            "sightings": r["n_sources"],
                            "provenance": r["provenance"] or "legacy_v3",
                        })

    # Disk-first fallback or augment from wiki directory
    if w_dir.is_dir() and not candidate_pages:
        q_words = [w.casefold() for w in re.findall(r"\w+", query) if len(w) > 2]
        if not q_words:
            q_words = [w.casefold() for w in query.split() if w]
        if q_words:
            for md_file in sorted(w_dir.rglob("*.md")):
                if not md_file.is_file():
                    continue
                try:
                    rel_p = md_file.relative_to(w_dir).as_posix()
                    content = md_file.read_text(encoding="utf-8")
                    content_lower = content.casefold()
                    if any(w in content_lower for w in q_words) or any(w in rel_p.casefold() for w in q_words):
                        title = md_file.stem
                        m_title = re.search(r"^title:\s*[\"']?(.*?)[\"']?\s*$", content, re.MULTILINE)
                        if m_title:
                            title = m_title.group(1).strip()
                        else:
                            m_h1 = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                            if m_h1:
                                title = m_h1.group(1).strip()
                        candidate_pages.append({"path": rel_p, "title": title, "body": content})
                        if len(candidate_pages) >= limit:
                            break
                except Exception:
                    pass

    candidate_chunks: list[dict[str, Any]] = []
    chunk_chars = 800

    for page in candidate_pages:
        rel_path = str(page.get("path") or "")
        title = str(page.get("title") or "")
        body = str(page.get("body") or "")

        # Disk-first content with directory traversal guard
        if w_dir.is_dir() and rel_path and not rel_path.startswith(("/", "\\")) and ".." not in rel_path:
            try:
                disk_file = (w_dir / rel_path).resolve()
                if disk_file.is_file() and disk_file.is_relative_to(w_dir):
                    body = disk_file.read_text(encoding="utf-8")
            except Exception:
                pass

        candidate_chunks.extend(_chunks_from_body(rel_path, title, body, chunk_chars))

    filtered = filter_chunks(candidate_chunks, query, keep_ratio=0.5, top=25)
    retrieved_paths = sorted({c["path"] for c in filtered if c.get("path")})

    return {
        "query": query,
        "chunks": filtered,
        "retrieved_paths": retrieved_paths,
        "pages_count": len(candidate_pages),
        "entities": matched_entities,
        "claims": matched_claims,
        "provenance": [
            {
                "path": c.get("path"),
                "provenance": c.get("provenance") or "human_note",
                "qualified": False,
            }
            for c in filtered
        ],
    }

def _flag(cfg: Any, attr: str, env: str) -> bool:
    if cfg is not None and hasattr(cfg, attr):
        value = getattr(cfg, attr)
        if isinstance(value, bool):
            return value
        if value not in (None, ""):
            return str(value).strip().lower() in {"1", "true", "yes"}
    return os.environ.get(env, "").strip().lower() in {"1", "true", "yes"}


def _text(cfg: Any, attr: str, env: str) -> str:
    if cfg is not None:
        value = getattr(cfg, attr, None)
        if value not in (None, ""):
            return str(value).strip()
    return os.environ.get(env, "").strip()


def _endpoint_blocked(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in _BLOCKED_ENDPOINT_MARKERS)


def _model_blocked(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(marker in lowered for marker in _CLOUD_MODEL_MARKERS)


def _is_public_only(evidence: dict[str, Any]) -> bool:
    scope = str(evidence.get("scope") or "").strip().lower().replace("-", "_")
    return scope == "public_only"


def _private_scalars(evidence: dict[str, Any]) -> set[str]:
    """Long private values that must not be copied into a public-only prompt."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if len(text) >= 8:
                found.add(text)
            return
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    for key in ("profile", "candidates", "private_fields"):
        walk(evidence.get(key))
    return found


def _drop_private_readiness(answer: str, evidence: dict[str, Any]) -> str:
    """Unknown private financial and eligibility fields are not met and are not readiness."""
    if not _is_public_only(evidence):
        return answer
    kept: list[str] = []
    for line in answer.splitlines():
        lowered = line.casefold()
        if "readiness" in lowered:
            continue
        if any(key in lowered for key in _PRIVATE_FIELD_KEYS):
            continue
        if "eligibility" in lowered and re.search(r"\bmet\b", lowered):
            continue
        kept.append(line)
    return "\n".join(kept)


def _failure(reason: str, *, detail: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "status": "unavailable",
        "reason": reason,
        "requested_mode": "private_model",
        "mode": "private_model",
        "endpoint_role": "private_eval",
        "available": False,
        "usage_kind": "unknown",
        "transport": None,
        "downgraded": False,
        "qualified": False,
    }
    if detail:
        payload["detail"] = detail
    return payload


def _load_cfg() -> Any:
    orch = _sibling("orchestrator")
    if orch is None or not hasattr(orch, "load_config"):
        return None
    try:
        return orch.load_config()
    except (Exception, SystemExit):
        return None


def _isolation_documented(value: str) -> bool:
    """Match LocalPrivateEngine: a short attestation sentence, not a boolean alias."""
    text = value.strip()
    if len(text) < 20 or len(text) > 500:
        return False
    lowered = text.lower()
    return "egress" in lowered or "isolation" in lowered


def _configured_model_id(cfg: Any) -> str:
    """Canonical config field is private_model_id. There is no private_model alias."""
    return _text(cfg, "private_model_id", "PRIVATE_MODEL_ID")


def build_private_context(evidence: dict[str, Any]) -> str:
    """Compile the private prompt from wiki chunks only.

    Profile, candidate, and other dictionaries on the evidence object are ignored.
    Under an explicit public-only scope, non-source chunks and long private values
    stay out of the prompt, and unknown financial or eligibility fields stay unknown.
    """
    public_only = _is_public_only(evidence)
    hidden = _private_scalars(evidence) if public_only else set()
    parts = [f"Question: {evidence.get('query') or ''}", "Retrieved wiki chunks:"]
    for index, chunk in enumerate(evidence.get("chunks") or [], 1):
        if not isinstance(chunk, dict):
            continue
        provenance = str(chunk.get("provenance") or "source_evidence")
        if public_only and provenance != "source_evidence":
            continue
        text = str(chunk.get("text") or "")
        if hidden and any(secret in text for secret in hidden):
            continue
        label = _PROVENANCE_LABEL.get(provenance, provenance)
        parts.append(
            f"Chunk {index} provenance={label} path=[{chunk.get('path') or ''}] "
            f"title={chunk.get('title') or ''}\n{text}"
        )
    parts.append(
        "Answer only from these chunks. HUMAN_NOTE and LEAD text is not source evidence "
        "and is not reviewed. Cite [path] on every factual sentence."
    )
    if public_only:
        parts.append(
            "Scope is public_only. Unknown private financial and eligibility fields "
            "stay unknown. Do not call them met and do not report readiness."
        )
    return "\n\n".join(parts)


def query_local_model(
    prompt: str,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Private completion through LocalPrivateEngine, or a visible structured failure.

    Endpoint and model overrides are rejected. There is no second model and no
    retrieval substitute. ``timeout`` is retained for callers and is not a transport.
    """
    del timeout
    if base_url or model:
        return _failure(
            "endpoint_override_rejected",
            detail="Private ask does not accept an endpoint or model override.",
        )
    cfg = _load_cfg()
    endpoint = _text(cfg, "private_model_endpoint", "PRIVATE_MODEL_ENDPOINT")
    model_id = _configured_model_id(cfg)
    if (endpoint and _endpoint_blocked(endpoint)) or (model_id and _model_blocked(model_id)):
        return _failure("public_endpoint_blocked")
    attested = _flag(cfg, "private_model_attested", "PRIVATE_MODEL_ATTESTED")
    isolation = _text(cfg, "private_model_isolation", "PRIVATE_MODEL_ISOLATION")
    if not endpoint or not attested or not _isolation_documented(isolation) or not model_id:
        return _failure("local_model_required")
    transport = _sibling("engine_transport")
    if transport is None:
        return _failure("private_engine_unavailable")
    try:
        engine = transport.LocalPrivateEngine(cfg, dry=False)
        available, reason = engine.available()
        if not available:
            return _failure(str(reason or "local_model_required"))
        reply = engine.interpret(prompt, max_out=1024)
        text = reply.text
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        return {
            "ok": True,
            "status": "ok",
            "text": text,
            "model": reply.model,
            "requested_model": reply.requested_model,
            "reported_model": reply.reported_model,
            "tokens_in": reply.tokens_in,
            "tokens_out": reply.tokens_out,
            "usage_kind": reply.usage_kind or "unknown",
            "transport": reply.transport or "private_local",
            "endpoint_role": "private_eval",
            "requested_mode": "private_model",
            "downgraded": False,
            "available": True,
            "qualified": False,
        }
    except transport.EngineUnavailable as exc:
        return _failure(str(exc.reason or "local_model_required"))
    except Exception as exc:
        return _failure("private_engine_failed", detail=type(exc).__name__)


def split_sentences_with_citations(text: str) -> list[str]:
    """Conservative sentence splitting preserving trailing citations per sentence."""
    sentences = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split within line at sentence boundaries while keeping citation attached
        tokens = re.split(r"([.!?]+(?:\s*\[[a-zA-Z0-9_\-./]+\])?(?:\s+|$))", line)
        cur = ""
        for tok in tokens:
            cur += tok
            if re.search(r"[.!?]+(?:\s*\[[a-zA-Z0-9_\-./]+\])?(?:\s+|$)", tok):
                s = cur.strip()
                if s:
                    sentences.append(s)
                cur = ""
        if cur.strip():
            sentences.append(cur.strip())
    return sentences


def validate_and_filter_sentences(
    raw_text: str,
    retrieved_paths: set[str],
    query: str,
    chunks: list[dict[str, Any]] | None = None,
) -> tuple[str, bool]:
    """Validate that every factual sentence has a valid citation [path]
    matching retrieved_paths and that key factual numbers are grounded in that chunk's text.
    Unsupported sentences and invalid citations are dropped.
    Returns (filtered_text, has_sufficient_evidence).
    """
    if not retrieved_paths:
        return (f"Insufficient verified evidence in the local knowledge base to answer '{query}'.", False)

    valid_paths: set[str] = set()
    for p in retrieved_paths:
        norm = p.replace("\\", "/").lstrip("./")
        valid_paths.add(norm)
        if norm.startswith("wiki/"):
            valid_paths.add(norm[5:])

    chunk_texts_by_path: dict[str, str] = {}
    if chunks:
        for c in chunks:
            c_p = c.get("path", "").replace("\\", "/").lstrip("./")
            c_t = c.get("text", "")
            chunk_texts_by_path[c_p] = chunk_texts_by_path.get(c_p, "") + " " + c_t
            if c_p.startswith("wiki/"):
                chunk_texts_by_path[c_p[5:]] = chunk_texts_by_path.get(c_p[5:], "") + " " + c_t

    raw_sentences = split_sentences_with_citations(raw_text)
    valid_sentences: list[str] = []

    for sentence in raw_sentences:
        # [text](<raw/...>) and [raw](<raw/...>) are snapshot links, not wiki-path citations.
        pins = _PIN_RX.findall(sentence)
        masked = sentence
        for index, pin in enumerate(pins):
            masked = masked.replace(pin, f"«PIN{index}»", 1)
        raw_citations = re.findall(r"\[([a-zA-Z0-9_\-./]+)\]", masked)
        if not raw_citations:
            # Sentence without citation is dropped
            continue

        valid_cits = []
        for c in raw_citations:
            # Path traversal check
            if ".." in c or c.startswith(("/", "\\")):
                continue
            norm_c = c.replace("\\", "/").lstrip("./")
            if norm_c in valid_paths or (norm_c.startswith("wiki/") and norm_c[5:] in valid_paths):
                valid_cits.append(norm_c)

        if not valid_cits:
            # No valid citation in retrieved paths
            continue

        # Factual numeral grounding check. Digits inside a [path] citation or snapshot pin are not claims.
        prose = re.sub(r"\[[a-zA-Z0-9_\-./]+\]", " ", masked)
        sent_norm = re.sub(r"(?<=\d),(?=\d)", "", prose)
        sentence_nums = set()
        for raw_num in re.findall(r"\b\d+\b", sent_norm):
            if len(raw_num) >= 2:
                sentence_nums.add(raw_num)

        grounded = True
        if sentence_nums and chunk_texts_by_path:
            for c_path in valid_cits:
                source_blob = chunk_texts_by_path.get(c_path, "")
                source_norm = re.sub(r"(?<=\d),(?=\d)", "", source_blob)
                source_nums = set(re.findall(r"\b\d+\b", source_norm))
                for s_num in sentence_nums:
                    if s_num not in source_nums:
                        grounded = False
                        break
                if not grounded:
                    break

        if not grounded:
            # Dropped because numerals were not grounded in source evidence
            continue

        cleaned_sent = masked
        for c in raw_citations:
            norm_c = c.replace("\\", "/").lstrip("./")
            if norm_c not in valid_cits:
                cleaned_sent = cleaned_sent.replace(f"[{c}]", "")
        for index, pin in enumerate(pins):
            cleaned_sent = cleaned_sent.replace(f"«PIN{index}»", pin)
        valid_sentences.append(cleaned_sent.strip())

    if not valid_sentences:
        return (f"Insufficient verified evidence in the local knowledge base to answer '{query}'.", False)

    return ("\n".join(valid_sentences), True)


_PIN_RX = re.compile(r"\[(?:raw|text)\]\(<[^>\n]+>\)")


def _retrieval_answer(query: str, chunks: list[dict[str, Any]]) -> str:
    """One cited line per matching fact so the citation stays on that fact."""
    lines: list[str] = []
    q_words = [w.casefold() for w in re.findall(r"\w+", query) if len(w) > 2]
    for chunk in chunks:
        label = _PROVENANCE_LABEL.get(str(chunk.get("provenance") or "source_evidence"), "source_evidence")
        path = chunk.get("path") or ""
        text = str(chunk.get("text") or "")
        content_lines: list[str] = []
        pin_lines: list[str] = []
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("<!--"):
                continue
            if _PIN_RX.search(stripped):
                pin_lines.append(stripped)
            if q_words and any(word in stripped.casefold() for word in q_words):
                content_lines.append(stripped)
        if not content_lines:
            if len(lines) < 5:
                snippet = " ".join(part for part in text.split() if not part.startswith("<!--"))[:160]
                if snippet:
                    lines.append(f"- {label}: {snippet} [{path}]")
            continue
        for stripped in content_lines:
            lines.append(f"- {label}: {stripped} [{path}]")
        for pin in pin_lines:
            if pin in content_lines:
                continue
            lines.append(f"- {label}: {pin} [{path}]")
    return "\n".join(lines)


def _unqualified_footer(chunks: list[dict[str, Any]]) -> str:
    kinds = {str(chunk.get("provenance") or "") for chunk in chunks}
    if kinds & {"human_note", "lead"}:
        return "HUMAN_NOTE and LEAD text is not source evidence and is not reviewed."
    return ""


def synthesize_answer(
    evidence: dict[str, Any],
    use_model: bool = True,
    base_url: str | None = None,
    model: str | None = None,
) -> str | dict[str, Any]:
    """Cite retrieved wiki text, or return a structured private-model failure.

    ``use_model=False`` is the explicit retrieval path. A requested private model
    that is unavailable does not fall back to that path.
    """
    query = str(evidence.get("query") or "")
    chunks = list(evidence.get("chunks") or [])
    retrieved_paths = set(evidence.get("retrieved_paths") or [])

    if not chunks or not retrieved_paths:
        # Entity and claim rows are not wiki paths. Emitting them without a
        # retrieved [path] would be an uncited answer.
        return f"Insufficient verified evidence in the local knowledge base to answer '{query}'."

    if use_model:
        outcome = query_local_model(
            build_private_context(evidence),
            base_url=base_url,
            model=model,
        )
        if not outcome.get("ok"):
            return outcome
        raw_answer = str(outcome.get("text") or "")
        if not raw_answer.strip():
            failed = dict(outcome)
            failed.update({
                "ok": False,
                "status": "empty_reply",
                "reason": "empty_reply",
                "downgraded": False,
                "qualified": False,
                "text": "",
                "answer": "",
            })
            return failed
        answer, sufficient = validate_and_filter_sentences(
            raw_answer, retrieved_paths, query, chunks=chunks
        )
        answer = _drop_private_readiness(answer, evidence)
        if not sufficient or not answer.strip() or answer.startswith("Insufficient verified evidence"):
            failed = dict(outcome)
            failed.update({
                "ok": False,
                "status": "insufficient_evidence",
                "reason": "insufficient_evidence",
                "downgraded": False,
                "qualified": False,
                "text": answer,
                "answer": answer,
            })
            return failed
        success = dict(outcome)
        success["answer"] = answer
        success["text"] = answer
        success["qualified"] = False
        success["downgraded"] = False
        return success

    raw_answer = _retrieval_answer(query, chunks)
    answer, _sufficient = validate_and_filter_sentences(
        raw_answer, retrieved_paths, query, chunks=chunks
    )
    if not answer.startswith("Insufficient verified evidence"):
        footer = _unqualified_footer(chunks)
        if footer:
            answer = answer + "\n" + footer
    answer = _drop_private_readiness(answer, evidence)
    if _is_public_only(evidence) and not answer.strip():
        return f"Insufficient verified evidence in the local knowledge base to answer '{query}'."
    return answer


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Knowledge Base Answering CLI (snowball ask)")
    ap.add_argument("query", help="Question or search query")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    ap.add_argument("--wiki", type=Path, default=WIKI_DIR, help="Path to wiki directory")
    ap.add_argument("--limit", type=int, default=12, help="Max BM25 wiki pages to retrieve")
    ap.add_argument("--json", action="store_true", help="Output raw JSON evidence and answer")
    ap.add_argument("--no-model", action="store_true", help="Disable LLM synthesis, return deterministic facts")
    args = ap.parse_args()

    db_path = args.db
    wiki_path = args.wiki

    evidence: dict[str, Any] = {
        "query": args.query,
        "chunks": [],
        "retrieved_paths": [],
        "pages_count": 0,
        "entities": [],
        "claims": [],
    }

    # Open an existing database readonly. A read failure is not an empty search.
    if db_path.exists():
        try:
            conn = get_db(db_path)
        except KnowledgeReadError as exc:
            _emit_read_error(args.query, exc, as_json=args.json)
        try:
            try:
                evidence = search_local_knowledge(conn, args.query, wiki_dir=wiki_path, limit=args.limit)
            except sqlite3.Error as exc:
                _emit_read_error(
                    args.query,
                    KnowledgeReadError(str(exc), code="readonly_read"),
                    as_json=args.json,
                )
        finally:
            conn.close()
    elif wiki_path.is_dir():
        evidence = search_local_knowledge(None, args.query, wiki_dir=wiki_path, limit=args.limit)

    has_matches = bool(evidence.get("chunks") or evidence.get("entities") or evidence.get("claims"))

    if not has_matches:
        err_msg = f"Error: no matching pages found for query '{args.query}' in knowledge base."
        if args.json:
            payload = {
                "query": args.query,
                "error": err_msg,
                "answer": f"Insufficient verified evidence in the local knowledge base to answer '{args.query}'.",
                "retrieved_paths": [],
                "evidence_chunks": 0,
                "sufficient_evidence": False,
                "status": "no_matching_pages",
            }
            print(json.dumps(payload, indent=2))
        print(err_msg, file=sys.stderr)
        sys.exit(1)

    answer = synthesize_answer(evidence, use_model=not args.no_model)
    if isinstance(answer, dict):
        if not answer.get("ok"):
            payload = {"query": args.query, **answer}
            print(json.dumps(payload), file=sys.stderr)
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"Private model unavailable: {payload.get('reason')}", file=sys.stderr)
            sys.exit(2)
        text = str(answer.get("answer") or answer.get("text") or "")
        if args.json:
            payload = {
                "query": args.query,
                "answer": text,
                "retrieved_paths": evidence.get("retrieved_paths", []),
                "evidence_chunks": len(evidence.get("chunks", [])),
                "provenance": evidence.get("provenance", []),
                "mode": "private_model",
                "sufficient_evidence": True,
                "qualified": False,
                "downgraded": False,
                "status": "ok",
                "model": answer.get("model"),
                "requested_model": answer.get("requested_model"),
                "reported_model": answer.get("reported_model"),
                "usage_kind": answer.get("usage_kind"),
                "transport": answer.get("transport"),
                "tokens_in": answer.get("tokens_in"),
                "tokens_out": answer.get("tokens_out"),
            }
            print(json.dumps(payload, indent=2))
        else:
            print("\n" + text + "\n")
        sys.exit(0)
    sufficient = not answer.startswith("Insufficient verified evidence")
    if (
        not sufficient
        and args.no_model
        and (evidence.get("entities") or evidence.get("claims"))
    ):
        # Explicit retrieval can still report legacy rows. They stay LEAD, never proof.
        answer = _lead_inventory(evidence)
        sufficient = bool(answer.strip())

    if not sufficient:
        err_msg = f"Error: no matching pages or insufficient verified citations found for '{args.query}' in knowledge base."
        if args.json:
            payload = {
                "query": args.query,
                "error": err_msg,
                "answer": answer,
                "retrieved_paths": evidence.get("retrieved_paths", []),
                "evidence_chunks": len(evidence.get("chunks", [])),
                "sufficient_evidence": False,
                "status": "insufficient_evidence",
            }
            print(json.dumps(payload, indent=2))
        print(err_msg, file=sys.stderr)
        sys.exit(1)

    if args.json:
        payload = {
            "query": args.query,
            "answer": answer,
            "retrieved_paths": evidence.get("retrieved_paths", []),
            "evidence_chunks": len(evidence.get("chunks", [])),
            "provenance": evidence.get("provenance", []),
            "mode": "retrieval" if args.no_model else "private_model",
            "sufficient_evidence": True,
            "qualified": False,
            "status": "ok",
        }
        print(json.dumps(payload, indent=2))
        sys.exit(0)

    print("\n" + answer + "\n")
    sys.exit(0)

def _lead_inventory(evidence: dict[str, Any]) -> str:
    """Deterministic --no-model report for rows that have no wiki path yet."""
    lines: list[str] = []
    for entity in evidence.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        bits = [
            str(entity.get(key) or "").strip()
            for key in ("name", "link", "evidence_quote")
        ]
        text = " ".join(bit for bit in bits if bit)
        if text:
            lines.append(f"- LEAD: {text}")
    for claim in evidence.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        text = str(claim.get("text") or "").strip()
        if text:
            lines.append(f"- LEAD: {text}")
    if not lines:
        return ""
    lines.append("HUMAN_NOTE and LEAD text is not source evidence and is not reviewed.")
    return "\n".join(lines)


def _emit_read_error(query: str, exc: KnowledgeReadError, *, as_json: bool) -> None:
    payload = {
        "query": query,
        "ok": False,
        "status": "read_error",
        "code": exc.code,
        "error": str(exc),
        "downgraded": False,
        "sufficient_evidence": False,
        "qualified": False,
    }
    print(json.dumps(payload), file=sys.stderr)
    if as_json:
        print(json.dumps(payload, indent=2))
    sys.exit(3)


if __name__ == "__main__":
    main()
