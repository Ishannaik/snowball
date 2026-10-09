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
import time
from typing import Any
import urllib.error
import urllib.request
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


def get_db(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    if not p.exists():
        raise FileNotFoundError(f"Database not found at {p}. Run orchestrator or compile first.")
    try:
        conn = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True)
    except Exception:
        conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
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
                try:
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
                except sqlite3.OperationalError:
                    candidate_pages = []

        # Query entities table if present (e2e compatibility)
        try:
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
        except Exception:
            pass

        # Query claims table if present
        try:
            has_claims = bool(
                cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
            )
            if has_claims:
                q_words = [w for w in re.findall(r"\w+", query) if len(w) > 2] or query.split()
                if q_words:
                    cur.execute(
                        """
                        SELECT c.hash, c.text, c.n_sources, p.canon_url AS url
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
                        })
        except Exception:
            pass

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

        for i in range(0, max(1, len(body)), chunk_chars):
            c_text = body[i : i + chunk_chars].strip()
            if c_text:
                candidate_chunks.append({
                    "path": rel_path,
                    "title": title,
                    "text": c_text,
                })

    filtered = filter_chunks(candidate_chunks, query, keep_ratio=0.5, top=25)
    retrieved_paths = sorted({c["path"] for c in filtered if c.get("path")})

    return {
        "query": query,
        "chunks": filtered,
        "retrieved_paths": retrieved_paths,
        "pages_count": len(candidate_pages),
        "entities": matched_entities,
        "claims": matched_claims,
    }

def query_local_model(
    prompt: str,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 30.0,
) -> str | None:
    """Call the configured local answer model via OpenAI-compatible API.

    Uses load_config to read config.env / SNOWBALL_ENV_FILE. Never calls external APIs.
    """
    orch = _sibling("orchestrator")
    cfg = orch.load_config() if orch and hasattr(orch, "load_config") else None

    raw_base = (
        base_url
        or (cfg.local_base_url if cfg else None)
        or os.environ.get("OPENAI_BASE_URL")
        or "http://127.0.0.1:8800/v1"
    )
    url = raw_base.rstrip("/") + "/chat/completions"
    model_name = (
        model
        or (cfg.local_model if cfg else None)
        or os.environ.get("LOCAL_MODEL")
        or "ornith-1.5-35b-a3b-abliterated"
    )
    fallback_model = (
        (cfg.local_fallback_model if cfg else None)
        or os.environ.get("LOCAL_FALLBACK_MODEL")
        or "qwen3.8-27b-abliterated"
    )
    api_key = (
        (cfg.local_api_key if cfg else None)
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("QWEN_API_KEY")
    )

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Snowball's local library question answering assistant. You answer questions strictly and solely using the provided retrieved context chunks from the local wiki.\n"
                    "Every single factual sentence in your answer MUST cite its source using [path] at the end of the sentence.\n"
                    "If the provided chunks do not contain sufficient evidence to answer the question, state honestly: 'Insufficient verified evidence in the local knowledge base to answer the question.'\n"
                    "Do NOT use outside knowledge, model memory, or speculation. Do NOT invent facts or URLs. Never make claims about future scheduling or background tasks."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
        "enable_thinking": False,
        "chat_template_kwargs": {"thinking": False},
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    start_t = time.time()
    for m in (model_name, fallback_model):
        elapsed = time.time() - start_t
        remaining = timeout - elapsed
        if remaining <= 1.0:
            break
        call_timeout = min(15.0, remaining)
        payload["model"] = m
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=call_timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices")
                if choices and len(choices) > 0:
                    msg = choices[0].get("message", {})
                    content = msg.get("content", "").strip()
                    if content:
                        return content
        except Exception:
            continue

    return None
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
        raw_citations = re.findall(r"\[([a-zA-Z0-9_\-./]+)\]", sentence)
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

        # Factual numeral grounding check
        sent_norm = re.sub(r"(?<=\d),(?=\d)", "", sentence)
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

        cleaned_sent = sentence
        for c in raw_citations:
            norm_c = c.replace("\\", "/").lstrip("./")
            if norm_c not in valid_cits:
                cleaned_sent = cleaned_sent.replace(f"[{c}]", "")
        valid_sentences.append(cleaned_sent.strip())

    if not valid_sentences:
        return (f"Insufficient verified evidence in the local knowledge base to answer '{query}'.", False)

    return ("\n".join(valid_sentences), True)


def synthesize_answer(
    evidence: dict[str, Any],
    use_model: bool = True,
    base_url: str | None = None,
    model: str | None = None,
) -> str:
    """Synthesize citation-backed answer from retrieved local facts."""
    query = evidence.get("query", "")
    chunks = evidence.get("chunks", [])
    retrieved_paths = set(evidence.get("retrieved_paths", []))

    if not chunks or not retrieved_paths:
        # Entity and claim rows are not wiki paths. Emitting them without a
        # retrieved [path] would be an uncited answer.
        return f"Insufficient verified evidence in the local knowledge base to answer '{query}'."

    raw_answer: str | None = None
    if use_model:
        context_parts = []
        for i, c in enumerate(chunks, 1):
            context_parts.append(f"Chunk {i} (from [{c['path']}] - {c['title']}):\n{c['text']}")
        context_text = "\n\n".join(context_parts)
        prompt = (
            f"Question: {query}\n\n"
            f"Retrieved Wiki Context Chunks:\n{context_text}\n\n"
            "Instructions:\n"
            "- Answer the question using ONLY facts stated directly in the retrieved chunks above.\n"
            "- Every single factual sentence must cite its source path at the end of the sentence as [path].\n"
            "- If the chunks do not contain enough facts to answer, say: 'Insufficient verified evidence in the local knowledge base to answer the question.'\n"
            "- Do not invent facts, URLs, or future schedules."
        )
        raw_answer = query_local_model(prompt, base_url=base_url, model=model)

    if not raw_answer:
        lines: list[str] = []
        q_words = [w.casefold() for w in re.findall(r"\w+", query) if len(w) > 2]
        for c in chunks:
            c_text = c["text"]
            found = False
            for sentence in re.split(r"(?<=[.!?\n])\s+", c_text):
                s_strip = sentence.strip()
                if not s_strip:
                    continue
                s_lower = s_strip.casefold()
                if any(w in s_lower for w in q_words):
                    lines.append(f"- {s_strip} [{c['path']}]")
                    found = True
            if not found and len(lines) < 5:
                snippet = c_text[:160].replace("\n", " ").strip()
                lines.append(f"- {snippet} [{c['path']}]")
        raw_answer = "\n".join(lines)

    answer, _ = validate_and_filter_sentences(raw_answer, retrieved_paths, query, chunks=chunks)
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

    # Open DB only if it already exists; never create a database
    if db_path.exists():
        try:
            conn = get_db(db_path)
            try:
                evidence = search_local_knowledge(conn, args.query, wiki_dir=wiki_path, limit=args.limit)
            finally:
                conn.close()
        except Exception:
            pass

    # Fallback to direct local wiki search if no chunks from DB
    if not evidence.get("chunks") and not evidence.get("entities"):
        if wiki_path.is_dir():
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
    sufficient = not answer.startswith("Insufficient verified evidence")

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
            "sufficient_evidence": True,
            "status": "ok",
        }
        print(json.dumps(payload, indent=2))
        sys.exit(0)

    print("\n" + answer + "\n")
    sys.exit(0)

if __name__ == "__main__":
    main()
