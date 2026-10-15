#!/usr/bin/env python3
"""Comprehensive behavior tests for local wiki answers and evaluator gates.

Covers T0, T5, T6, T10, T13 specifications:
- Sanitized FTS MATCH and Section 7.3 BM25 context filter
- Configured local answer model only, zero external/Gemini calls
- Strict sentence-level [path] citation validation and unsupported sentence pruning
- Numeral grounding against cited chunk evidence (wrong amount with real path dropped)
- Same-line mixed grounded/ungrounded sentence handling
- Path traversal attack prevention in citations
- Honest insufficient evidence without invented facts or false 'scheduled' claims
- Handling human edits on disk and malformed queries
- Evaluator production classifier and extraction verification contracts
- Digest delivery audit with exact snapshot joins, 0 rows never PASS, long false quote fails, unquoted items fail
- Reindex FTS using wiki_compiler Markdown truth (all markdown files indexed)
- Topic similarity benchmark (95% binary, 80% fine agreement)
- Machine-readable diagnostics and truthful live gate prerequisite checks
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def import_tool(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod



ask_mod = import_tool("ask", "tools/ask.py")
eval_mod = import_tool("eval_gates", "tools/eval_gates.py")
wiki_mod = import_tool("wiki_compiler", "tools/wiki_compiler.py")
orch = import_tool("orchestrator", "tools/orchestrator.py")

ISOLATION = "Serving daemon has no outbound egress; network namespace is isolated."
SENTINEL = "synthetic-unknown-cash-9911"
QUOTE = "The December fellowship pays 64000."
HUMAN = "Human note says 4242 is not a quote."


class _LocalApp:
    def __init__(self, content: str, model: str):
        self.content = content
        self.model = model
        self.requests = []
        self.lock = threading.Lock()


def _local_handler(app: _LocalApp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            with app.lock:
                app.requests.append(raw)
            body = {
                "model": app.model,
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": app.content},
                }],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 30,
                    "total_tokens": 80,
                    "prompt_tokens_details": {"system_tokens": 12},
                    "completion_tokens_details": {"reasoning_tokens": 18},
                },
            }
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            return

    return Handler


@contextlib.contextmanager
def _serve(app: _LocalApp):
    """Bind a loopback daemon on a port that is not a configured public origin."""
    server = None
    for _attempt in range(8):
        candidate = ThreadingHTTPServer(("127.0.0.1", 0), _local_handler(app))
        # 8088 is the public AGY port; 8791 is the default rotator. Neither is a private daemon.
        if candidate.server_address[1] not in (8088, 8791):
            server = candidate
            break
        candidate.server_close()
    if server is None:
        raise RuntimeError("could not bind a non-public test port")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class TestAnswersAndRetrievalBehavior(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="snowball-answers-test-")
        self.db_path = Path(self.td.name) / "snowball.db"
        self.wiki_dir = Path(self.td.name) / "wiki"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        if hasattr(self, "conn"):
            self.conn.close()
        with contextlib.suppress(Exception):
            self.td.cleanup()

    def test_bm25_context_filter_top_25_and_relative_ratio(self):
        """Pure-Python BM25 context filter keeps chunks >= 0.5 * max score, max 25."""
        chunks = [
            {"path": f"entities/grant-{i}.md", "title": f"Grant {i}", "text": f"December cloud credits grant for AI startups {i}"}
            for i in range(40)
        ]
        filtered = ask_mod.filter_chunks(chunks, "December grant", keep_ratio=0.5, top=25)
        self.assertLessEqual(len(filtered), 25)
        self.assertGreaterEqual(len(filtered), 1)

    def test_fts_query_sanitizer_edge_cases(self):
        """Sanitizer neutralizes unbalanced quotes, reserved words, wildcards, and symbols."""
        cases = [
            ('December "grants', ['"December"', '"grants"']),
            ('"""', []),
            ('AND OR NOT NEAR ***', []),
            ('which grants close in December?', ['"which"', '"grants"', '"close"', '"in"', '"December"']),
            ('"open source" AI', ['"open source"', '"AI"']),
            ('grants:ai^2', ['"grants"', '"ai"', '"2"']),
        ]
        for query, expected_tokens in cases:
            res = ask_mod.sanitize_fts_query(query)
            for tok in expected_tokens:
                self.assertIn(tok, res)
            if not expected_tokens:
                self.assertEqual(res, "")

    def test_human_edits_on_disk_take_precedence(self):
        """Disk file edits take precedence over cached FTS body."""
        page_file = self.wiki_dir / "entities" / "december-fellowship.md"
        page_file.parent.mkdir(parents=True, exist_ok=True)
        page_file.write_text(
            "---\ntitle: December Fellowship\nkey: dec-fellowship\n---\n# December Fellowship\nOriginal amount $10,000.\n",
            encoding="utf-8",
        )
        wiki_mod.reindex_wiki(self.conn, self.wiki_dir)

        # Human edits the markdown file directly
        page_file.write_text(
            "---\ntitle: December Fellowship\nkey: dec-fellowship\n---\n# December Fellowship\nHuman updated amount to $50,000 closing December 31 2026.\n",
            encoding="utf-8",
        )

        evidence = ask_mod.search_local_knowledge(self.conn, "December Fellowship", wiki_dir=self.wiki_dir)
        self.assertGreaterEqual(len(evidence["chunks"]), 1)
        chunk_texts = " ".join(c["text"] for c in evidence["chunks"])
        self.assertIn("Human updated amount to $50,000", chunk_texts)
        self.assertIn("entities/december-fellowship.md", evidence["retrieved_paths"])

    def test_sentence_citation_pruning_and_honest_insufficient_evidence(self):
        """Sentences without citations, with unretrieved paths, wrong amounts, or traversal paths are dropped."""
        retrieved = {"wiki/entities/grant-a.md", "wiki/entities/grant-b.md"}
        chunks = [
            {"path": "wiki/entities/grant-a.md", "title": "Grant A", "text": "Mozilla Technology Fund awards up to $50,000 for open source."},
            {"path": "wiki/entities/grant-b.md", "title": "Grant B", "text": "Applications close on December 31 2026 for developer fellowship."},
        ]

        # Same line mixed
        mixed_same_line = "Invented grant is $9000000. Real deadline is Dec 31 [wiki/entities/grant-b.md]"
        answer, sufficient = ask_mod.validate_and_filter_sentences(mixed_same_line, retrieved, "grants", chunks=chunks)
        self.assertTrue(sufficient)
        self.assertIn("Real deadline is Dec 31 [wiki/entities/grant-b.md]", answer)
        self.assertNotIn("Invented grant is $9000000", answer)

        # Wrong amount with real path: $9,000,000 not in chunk ($50,000)
        wrong_amt = "Mozilla Technology Fund awards $9000000 [wiki/entities/grant-a.md]"
        ans_bad, suff_bad = ask_mod.validate_and_filter_sentences(wrong_amt, retrieved, "grants", chunks=chunks)
        self.assertFalse(suff_bad)
        self.assertIn("Insufficient verified evidence in the local knowledge base", ans_bad)

        # Path traversal attempt
        traversal = "Fellowship deadline is December 31 [../../etc/passwd]"
        ans_trav, suff_trav = ask_mod.validate_and_filter_sentences(traversal, retrieved, "grants", chunks=chunks)
        self.assertFalse(suff_trav)
        self.assertIn("Insufficient verified evidence in the local knowledge base", ans_trav)

        # Insufficient evidence without scheduled claims
        empty_evidence = {"query": "Quantum grants", "chunks": [], "retrieved_paths": []}
        ans = ask_mod.synthesize_answer(empty_evidence, use_model=False)
        self.assertIn("Insufficient verified evidence in the local knowledge base", ans)
        self.assertNotIn("schedule", ans.lower())
        self.assertNotIn("crawl frontier", ans.lower())

        uncited = {
            "query": "cloud credits",
            "chunks": [],
            "retrieved_paths": [],
            "entities": [{"name": "AWS Credits", "org": "Amazon", "amount": "100000", "currency": "USD", "deadline": "2026-12-01", "eligibility": "startups", "link": "https://aws.amazon.com/credits"}],
            "claims": [{"text": "AWS offers $100,000", "url": "https://aws.amazon.com/credits"}],
        }
        ans_uncited = ask_mod.synthesize_answer(uncited, use_model=False)
        self.assertIn("Insufficient verified evidence", ans_uncited)
        self.assertNotIn("AWS Credits", ans_uncited)
        self.assertNotIn("100,000", ans_uncited)

    def test_retrieval_labels_provenance_and_private_ask_does_not_downgrade(self):
        """Compiled corpus: retrieval stays labeled; a private model request fails closed."""
        self.conn.executescript(
            """
            CREATE TABLE entities (
              key TEXT PRIMARY KEY, name TEXT NOT NULL, org TEXT, year TEXT, kind TEXT,
              amount REAL, currency TEXT, deadline TEXT, eligibility TEXT, link TEXT,
              evidence_quote TEXT, evidence_page_id INTEGER, provenance TEXT
            );
            CREATE TABLE source_snapshots (
              id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL, content_hash TEXT NOT NULL,
              retrieved_at INTEGER NOT NULL, text_path TEXT NOT NULL, original_raw_path TEXT,
              visibility TEXT, provenance TEXT, source_cycle_label TEXT, superseded_by INTEGER
            );
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, page_id INTEGER NOT NULL,
              program_key TEXT, field_key TEXT NOT NULL, quote TEXT NOT NULL,
              quote_start INTEGER NOT NULL, quote_end INTEGER NOT NULL, assurance TEXT NOT NULL,
              invalidated_at INTEGER, invalidated_reason TEXT
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO entities (
              key, name, org, year, kind, amount, currency, deadline, eligibility, link,
              evidence_quote, evidence_page_id, provenance
            ) VALUES (
              'dec', 'December Fellowship', 'Acme', '2026', 'fellowship', 1000, 'USD',
              '2026-12-31', 'open', 'https://example.com/december',
              'Legacy entity blurb stays unqualified.', 7, 'legacy_v3'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO source_snapshots (
              id, page_id, content_hash, retrieved_at, text_path, original_raw_path,
              visibility, provenance, source_cycle_label, superseded_by
            ) VALUES (
              1, 7, 'hash-current', 100, 'raw/snap/tuition-2027.txt',
              'raw/original/tuition-2027.html', 'public', 'public_fetch', '2027', NULL
            )
            """
        )
        quote = "The December fellowship pays 64000."
        self.conn.execute(
            """
            INSERT INTO observations (
              id, snapshot_id, page_id, program_key, field_key, quote, quote_start, quote_end,
              assurance, invalidated_at, invalidated_reason
            ) VALUES (1, 1, 7, 'dec', 'stipend', ?, 3, 36, 'code_checked', NULL, NULL)
            """,
            (quote,),
        )
        self.conn.commit()
        wiki_mod.compile_wiki(self.conn, self.wiki_dir, full=True)
        page = self.wiki_dir / "entities" / "december-fellowship-acme-2026.md"
        self.assertTrue(page.is_file(), page)
        page.write_text(
            page.read_text(encoding="utf-8")
            + "\n## Notes\nHuman note says 4242 is not a quote.\n",
            encoding="utf-8",
        )
        compiled = page.read_text(encoding="utf-8")
        self.assertIn("[text](<raw/snap/tuition-2027.txt>)", compiled)
        self.assertIn("[raw](<raw/original/tuition-2027.html>)", compiled)
        self.assertIn(quote, compiled)

        source = ask_mod.search_local_knowledge(self.conn, "64000", wiki_dir=self.wiki_dir)
        source_answer = ask_mod.synthesize_answer(source, use_model=False)
        self.assertIn(quote, source_answer)
        self.assertIn("raw/snap/tuition-2027.txt", source_answer)
        self.assertIn("[text](<raw/snap/tuition-2027.txt>)", "\n".join(chunk["text"] for chunk in source["chunks"]))
        self.assertTrue(source["provenance"])
        self.assertTrue(all(item["qualified"] is False for item in source["provenance"]))
        self.assertTrue(any(item["provenance"] == "source_evidence" for item in source["provenance"]))
        for line in source_answer.splitlines():
            if "64000" in line:
                self.assertIn("source_evidence", line)
                self.assertNotIn("HUMAN_NOTE", line)
                self.assertNotIn("reviewed_kept", line)

        note = ask_mod.search_local_knowledge(self.conn, "4242", wiki_dir=self.wiki_dir)
        note_answer = ask_mod.synthesize_answer(note, use_model=False)
        self.assertIn("4242", note_answer)
        self.assertIn("not source evidence", note_answer)
        for line in note_answer.splitlines():
            if "4242" in line:
                self.assertIn("HUMAN_NOTE", line)
                self.assertNotIn("source_evidence", line)

        lead = ask_mod.search_local_knowledge(self.conn, "unqualified", wiki_dir=self.wiki_dir)
        lead_answer = ask_mod.synthesize_answer(lead, use_model=False)
        self.assertIn("Legacy entity blurb stays unqualified.", lead_answer)
        for line in lead_answer.splitlines():
            if "unqualified" in line.lower() and "Legacy entity blurb" in line:
                self.assertIn("LEAD", line)
                self.assertNotIn("reviewed", line.lower())

        secret = "synthetic-private-citizenship"
        source["profile"] = {"citizenship": secret, "max_upfront_cash": {"amount": 0, "currency": "INR"}}
        source["candidates"] = [{"name": "Secret Candidate", "gpa": 3.8}]
        context = ask_mod.build_private_context(source)
        self.assertIn("64000", context)
        self.assertNotIn(secret, context)
        self.assertNotIn("Secret Candidate", context)
        self.assertNotIn("max_upfront_cash", context)

        cleared = {
            "PRIVATE_MODEL_ENDPOINT": "",
            "PRIVATE_MODEL_ATTESTED": "",
            "PRIVATE_MODEL_EGRESS_ISOLATED": "",
            "OPENAI_BASE_URL": "http://127.0.0.1:8088/v1",
            "LOCAL_MODEL": "agy-pool/gemini-3.8-flash",
        }
        with patch.dict(os.environ, cleared, clear=False):
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                failure = ask_mod.synthesize_answer(source, use_model=True)
                override = ask_mod.query_local_model(
                    "question",
                    base_url="http://127.0.0.1:8088/v1",
                    model="agy-pool/gemini-3.8-flash",
                )
        self.assertIsInstance(failure, dict)
        self.assertFalse(failure["ok"])
        self.assertFalse(failure["downgraded"])
        self.assertEqual(failure["requested_mode"], "private_model")
        self.assertEqual(failure["reason"], "local_model_required")
        self.assertNotIn("64000", json.dumps(failure))
        self.assertNotIn(secret, json.dumps(failure))
        self.assertFalse(override["ok"])
        self.assertEqual(override["reason"], "endpoint_override_rejected")
        self.assertFalse(override["downgraded"])

        attested = {
            "PRIVATE_MODEL_ENDPOINT": "http://127.0.0.1:8088/v1",
            "PRIVATE_MODEL_ATTESTED": "1",
            "PRIVATE_MODEL_ISOLATION": "daemon egress isolation documented for this endpoint",
            "PRIVATE_MODEL_ID": "local-private",
        }
        with patch.dict(os.environ, attested, clear=False):
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                blocked = ask_mod.query_local_model("question only")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "public_endpoint_blocked")
        self.assertFalse(blocked["downgraded"])
        self.assertEqual(blocked["requested_mode"], "private_model")
        self.assertNotIn("64000", json.dumps(blocked))

    def test_readonly_v4_read_does_not_open_writable(self):
        page = self.wiki_dir / "entities" / "visible.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\ntitle: Visible Grant\nkey: visible\n---\n# Visible Grant\nPays 64000 in December.\n",
            encoding="utf-8",
        )
        self.conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '4')")
        self.conn.commit()
        wiki_mod.reindex_wiki(self.conn, self.wiki_dir)

        readonly = ask_mod.get_db(self.db_path)
        try:
            self.assertEqual(readonly.execute("PRAGMA query_only").fetchone()[0], 1)
            version = readonly.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            self.assertEqual(str(version), "4")
            found = ask_mod.search_local_knowledge(readonly, "64000", wiki_dir=self.wiki_dir)
            self.assertTrue(any("64000" in chunk["text"] for chunk in found["chunks"]))
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("CREATE TABLE writes_are_rejected (id INTEGER)")
        finally:
            readonly.close()

        bogus = Path(self.td.name) / "not-a-database"
        bogus.write_text("this is not a sqlite database", encoding="utf-8")
        with self.assertRaises(ask_mod.KnowledgeReadError) as caught:
            ask_mod.get_db(bogus)
        self.assertEqual(caught.exception.code, "readonly_read")

    def _v4_public_page(self):
        """Fresh v4 store plus a compiled-style page. Unknown private fields stay unknown."""
        store = orch.Store.open(Path(self.td.name) / "data" / "corpus.db")
        self.addCleanup(store.db.close)
        db = store.db
        db.execute("INSERT INTO families (name, recheck_days, focus) VALUES ('general', 30, 'public notes')")
        db.execute("INSERT INTO domains (domain) VALUES ('example.edu')")
        db.execute(
            """
            INSERT INTO pages (
                canon_url, domain, status, next_check_at, interval_sec, discovered_via, created_at
            ) VALUES ('https://example.edu/december', 'example.edu', 'fetched', 1, 86400, 'seed', 1)
            """
        )
        page_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.execute(
            """
            INSERT INTO source_snapshots (
                id, page_id, content_hash, retrieved_at, text_path, original_raw_path,
                visibility, provenance, source_cycle_label
            ) VALUES (
                1, ?, 'hash-current', 1, 'raw/snap/tuition-2027.txt',
                'raw/original/tuition-2027.html', 'public', 'public_fetch', '2027'
            )
            """,
            (page_id,),
        )
        db.execute(
            """
            INSERT INTO observations (
                snapshot_id, page_id, field_key, value_json, subject_json, quote,
                quote_start, quote_end, extractor_model, prompt_version, assurance, created_at
            ) VALUES (
                1, ?, 'stipend', '{"amount":"unknown"}', '{}', ?, 0, ?,
                'fixture', 'v1', 'review_pending', 1
            )
            """,
            (page_id, QUOTE, len(QUOTE)),
        )
        db.execute(
            """
            INSERT INTO entities (
                key, name, norm_name, kind, family, deadline, eligibility, evidence_quote,
                evidence_page_id, first_seen_at, last_seen_at, status
            ) VALUES (
                'dec', 'December Fellowship', 'december fellowship', 'fellowship', 'general',
                '2026-12-31', 'unknown', ?, ?, 1, 1, 'new'
            )
            """,
            (QUOTE, page_id),
        )
        page = self.wiki_dir / "entities" / "december-fellowship.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\ntitle: December Fellowship\nkey: dec\n---\n"
            "# December Fellowship\n\n"
            "<!-- snowball:current_evidence:start -->\n"
            "## Current source evidence\n\n"
            "- field `stipend` assurance `code_checked` snapshot `1` hash `hash-current`\n"
            f"  span 0:{len(QUOTE)}\n"
            "  [text](<raw/snap/tuition-2027.txt>)\n"
            "  [raw](<raw/original/tuition-2027.html>)\n"
            f'> "{QUOTE}"\n'
            "<!-- snowball:current_evidence:end -->\n\n"
            f"## Notes\n{HUMAN}\n",
            encoding="utf-8",
        )
        wiki_mod.reindex_wiki(db, self.wiki_dir)
        return store

    def test_public_only_unknown_fields_stay_unknown_and_off_cloud(self):
        """A public-only ask does not turn unknown private fields into met or readiness."""
        store = self._v4_public_page()
        db = store.db
        self.assertEqual(db.execute("SELECT COUNT(*) FROM constraint_evaluations").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT eligibility FROM entities WHERE key = 'dec'").fetchone()[0], "unknown")
        regions = db.execute("SELECT provenance, body FROM wiki_regions").fetchall()
        self.assertTrue(any(row[0] == "human_note" and HUMAN in row[1] for row in regions))
        self.assertTrue(any(
            row[0] == "source_evidence" and QUOTE in row[1] and "hash `hash-current`" in row[1]
            for row in regions
        ))
        self.assertFalse(any(row[0] == "source_evidence" and HUMAN in row[1] for row in regions))

        evidence = ask_mod.search_local_knowledge(db, "64000", wiki_dir=self.wiki_dir)
        evidence["scope"] = "public_only"
        evidence["profile"] = {
            "max_upfront_cash": {"amount": "unknown", "currency": "EUR"},
            "eligibility": "unknown",
            "note": SENTINEL,
        }
        answer = ask_mod.synthesize_answer(evidence, use_model=False)
        self.assertIn(QUOTE, answer)
        self.assertIn("[text](<raw/snap/tuition-2027.txt>)", answer)
        self.assertNotIn(SENTINEL, answer)
        self.assertNotIn("readiness", answer.casefold())
        self.assertNotRegex(answer.casefold(), r"\bmet\b")
        self.assertTrue(all(item["qualified"] is False for item in evidence["provenance"]))
        self.assertNotIn("verified", json.dumps(evidence["provenance"]))
        context = ask_mod.build_private_context(evidence)
        self.assertNotIn(SENTINEL, context)
        self.assertNotIn("max_upfront_cash", context)
        self.assertIn(QUOTE, context)

        note = ask_mod.search_local_knowledge(db, "4242", wiki_dir=self.wiki_dir)
        note["scope"] = "public_only"
        note["profile"] = evidence["profile"]
        note_answer = ask_mod.synthesize_answer(note, use_model=False)
        self.assertIn("4242", note_answer)
        self.assertIn("HUMAN_NOTE", note_answer)
        self.assertIn("not source evidence", note_answer)
        self.assertNotIn(SENTINEL, note_answer)
        note_context = ask_mod.build_private_context(note)
        self.assertNotIn(SENTINEL, note_context)
        self.assertNotIn(HUMAN, note_context)
        self.assertNotIn("not a quote", note_context)

        blocked_env = {
            "PRIVATE_MODEL_ENDPOINT": "http://127.0.0.1:8088/v1",
            "PRIVATE_MODEL_ID": "iso-local-ask",
            "PRIVATE_MODEL_ATTESTED": "1",
            "PRIVATE_MODEL_ISOLATION": ISOLATION,
        }
        cloud_env = {
            "PRIVATE_MODEL_ENDPOINT": "http://127.0.0.1:9/v1",
            "PRIVATE_MODEL_ID": "gemini-3.8-flash",
            "PRIVATE_MODEL_ATTESTED": "1",
            "PRIVATE_MODEL_ISOLATION": ISOLATION,
        }
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with patch.dict(os.environ, blocked_env, clear=False):
                blocked = ask_mod.synthesize_answer(evidence, use_model=True)
            with patch.dict(os.environ, cloud_env, clear=False):
                cloud = ask_mod.query_local_model(context)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "public_endpoint_blocked")
        self.assertFalse(blocked["downgraded"])
        self.assertFalse(blocked["qualified"])
        self.assertNotIn(SENTINEL, json.dumps(blocked))
        self.assertNotIn(QUOTE, json.dumps(blocked))
        self.assertFalse(cloud["ok"])
        self.assertEqual(cloud["reason"], "public_endpoint_blocked")
        self.assertNotIn(SENTINEL, json.dumps(cloud))
        self.assertEqual(
            db.execute("SELECT value_json FROM observations").fetchone()[0],
            '{"amount":"unknown"}',
        )
        self.assertEqual(db.execute("SELECT COUNT(*) FROM constraint_evaluations").fetchone()[0], 0)

    def test_attested_local_ask_returns_answer_and_call_identity(self):
        """A fully attested local ask returns the cited answer and the engine's call identity."""
        store = self._v4_public_page()
        evidence = ask_mod.search_local_knowledge(store.db, "64000", wiki_dir=self.wiki_dir)
        evidence["profile"] = {"max_upfront_cash": {"amount": "unknown"}, "note": SENTINEL}
        path = evidence["retrieved_paths"][0]
        cited = f"{QUOTE} [{path}]"
        app = _LocalApp(cited, "iso-reported-a")
        with _serve(app) as server:
            port = server.server_address[1]
            endpoint = f"http://127.0.0.1:{port}/v1"
            unattested = {
                "PRIVATE_MODEL_ENDPOINT": endpoint,
                "PRIVATE_MODEL_ID": "iso-local-ask",
                "PRIVATE_MODEL_ATTESTED": "0",
                "PRIVATE_MODEL_ISOLATION": ISOLATION,
            }
            attested = {
                "PRIVATE_MODEL_ENDPOINT": endpoint,
                "PRIVATE_MODEL_ID": "iso-local-ask",
                "PRIVATE_MODEL_ATTESTED": "1",
                "PRIVATE_MODEL_ISOLATION": ISOLATION,
            }
            with patch.dict(os.environ, unattested, clear=False):
                refused = ask_mod.synthesize_answer(evidence, use_model=True)
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["reason"], "local_model_required")
            self.assertFalse(refused["downgraded"])
            self.assertEqual(app.requests, [])
            self.assertNotIn(SENTINEL, json.dumps(refused))
            with patch.dict(os.environ, attested, clear=False):
                outcome = ask_mod.synthesize_answer(evidence, use_model=True)
        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["downgraded"])
        self.assertFalse(outcome["qualified"])
        self.assertIn(QUOTE, outcome["answer"])
        self.assertIn(f"[{path}]", outcome["answer"])
        self.assertEqual(outcome["requested_model"], "iso-local-ask")
        self.assertEqual(outcome["reported_model"], "iso-reported-a")
        self.assertEqual(outcome["model"], "iso-reported-a")
        self.assertNotEqual(outcome["requested_model"], outcome["reported_model"])
        self.assertEqual(outcome["usage_kind"], "actual")
        self.assertEqual(outcome["transport"], "local")
        self.assertEqual(outcome["tokens_in"], 50)
        self.assertEqual(outcome["tokens_out"], 30)
        self.assertNotIn("readiness", outcome["answer"].casefold())
        self.assertNotIn(SENTINEL, outcome["answer"])
        self.assertEqual(len(app.requests), 1)
        raw = app.requests[0].decode("utf-8")
        self.assertIn("64000", raw)
        self.assertNotIn(SENTINEL, raw)
        self.assertNotIn("max_upfront_cash", raw)
        self.assertIn("iso-local-ask", raw)
        self.assertNotIn("gemini", raw.casefold())
        self.assertEqual(
            store.db.execute("SELECT COUNT(*) FROM constraint_evaluations").fetchone()[0],
            0,
        )


class TestEvaluatorGatesBehavior(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="snowball-eval-test-")
        self.db_path = Path(self.td.name) / "snowball.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        if hasattr(self, "conn"):
            self.conn.close()
        with contextlib.suppress(Exception):
            self.td.cleanup()

    def test_eval_classify_contract_reports_production(self):
        """eval_classify exercises production classifier contract and enforces 50 samples."""
        fixture = Path(self.td.name) / "test_pages_50.jsonl"
        rows = [
            {"title": f"Directory {i}", "text": f"Apply for active grants directory {i} with links for open source programs and developer grants. " * 3, "url": f"https://example.com/h{i}", "links": [f"https://example.com/{i}"], "label": "hub"}
            for i in range(50)
        ]
        fixture.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        res = eval_mod.eval_classify(fixture, quiet=True)
        self.assertEqual(res["classifier"], "production")
        self.assertEqual(res["total"], 50)
        self.assertTrue(res["passed"])

    def test_eval_extract_contract_reports_distinction(self):
        """eval_extract reports fixture_validation for offline fixtures and production for live model extraction."""
        fixture = Path(self.td.name) / "test_items.jsonl"
        src = "Open Source Grant awards $25,000 for decentralized infrastructure projects in 2026."
        fixture.write_text(
            json.dumps({
                "text": src,
                "url": "https://example.com/grant",
                "entities": [{
                    "name": "Open Source Grant",
                    "org": "OS Foundation",
                    "kind": "grant",
                    "evidence_quote": "Open Source Grant awards $25,000 for decentralized infrastructure projects in 2026.",
                }],
            }) + "\n",
            encoding="utf-8",
        )
        res = eval_mod.eval_extract(fixture, quiet=True)
        self.assertEqual(res["extractor"], "fixture_validation")
        self.assertFalse(res["production"])
        self.assertEqual(res["total"], 1)

    def test_eval_digest_joins_source_evidence_and_rejects_false_quotes(self):
        """Digest audit joins delivered items to pages, verifies verbatim quotes, and rejects long false quotes."""
        self.conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, canon_url TEXT, raw_path TEXT)")
        self.conn.execute("CREATE TABLE entities (key TEXT PRIMARY KEY, name TEXT, link TEXT, evidence_quote TEXT, evidence_page_id INTEGER)")
        self.conn.execute("CREATE TABLE digest_items (id INTEGER PRIMARY KEY, day TEXT, ref_kind TEXT, ref_id TEXT, rank INTEGER, reason TEXT, sent_at INTEGER, message_id TEXT)")

        raw_file = Path(self.td.name) / "raw.txt"
        raw_file.write_text("Verbatim evidence: Mozilla Tech Fund offers grants up to $50,000.", encoding="utf-8")
        self.conn.execute("INSERT INTO pages VALUES (1, 'https://example.com/apply', ?)", (str(raw_file),))
        # Long false quote not found in page body
        self.conn.execute("INSERT INTO entities VALUES ('ent-1', 'Tech Fund', 'https://example.com/apply', 'Invented false quote of substantial length that does not appear in source.', 1)")
        self.conn.execute("INSERT INTO digest_items VALUES (1, '2026-10-08', 'entity', 'ent-1', 1, 'reason', 1000, 'msg-999')")
        self.conn.commit()

        # Long false quote fails audit
        res_fail = eval_mod.eval_digest(since="7d", db_path=self.db_path, quiet=True)
        self.assertFalse(res_fail["passed"])
        self.assertEqual(res_fail["total_quotes"], 1)
        self.assertEqual(res_fail["valid_quotes"], 0)

        # Update entity to verbatim quote in page body
        self.conn.execute("UPDATE entities SET evidence_quote = 'Mozilla Tech Fund offers grants up to $50,000.' WHERE key = 'ent-1'")
        self.conn.commit()

        res_pass = eval_mod.eval_digest(since="7d", db_path=self.db_path, quiet=True)
        self.assertTrue(res_pass["passed"])
        self.assertEqual(res_pass["valid_quotes"], 1)
        self.assertEqual(res_pass["total_quotes"], 1)
        self.assertTrue(res_pass["production_delivery_check"])

    def test_reindex_fts_synchronizes_markdown_truth(self):
        """reindex_fts rebuilds wiki_fts and verifies every markdown file on disk."""
        wiki_dir = Path(self.td.name) / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        (wiki_dir / "p1.md").write_text("# Page 1\nContent 1\n", encoding="utf-8")
        (wiki_dir / "p2.md").write_text("# Page 2\nContent 2\n", encoding="utf-8")
        (wiki_dir / "catalog.md").write_text("# Catalog\nDirectory listing\n", encoding="utf-8")

        res = eval_mod.reindex_fts(self.db_path, wiki_dir=wiki_dir, quiet=True)
        self.assertEqual(res["markdown_files"], 3)
        self.assertEqual(res["wiki"], 3)
        self.assertTrue(res["passed"])

        # Empty directory does not pass
        empty_dir = Path(self.td.name) / "empty_wiki"
        empty_dir.mkdir(parents=True, exist_ok=True)
        res_empty = eval_mod.reindex_fts(self.db_path, wiki_dir=empty_dir, quiet=True)
        self.assertEqual(res_empty["markdown_files"], 0)
        self.assertFalse(res_empty["passed"])

    def test_topic_pair_similarity_benchmark(self):
        """Topic pair benchmark scores >=95% binary and >=80% fine agreement."""
        pairs_file = Path(self.td.name) / "pairs.jsonl"
        rows = []
        for i in range(15):
            rows.append({"topic_a": f"Startup cloud GPU credits {i}", "topic_b": f"Startup cloud GPU credits {i}", "label": "same"})
            rows.append({"topic_a": f"Developer fellowship program {i}", "topic_b": f"Kubernetes infrastructure deployment guide {i}", "label": "new"})
        pairs_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        res = eval_mod.eval_topics(pairs_file, quiet=True)
        self.assertEqual(res["total"], 30)
        self.assertGreaterEqual(res["binary_accuracy"], 95.0)
        self.assertGreaterEqual(res["fine_accuracy"], 80.0)
        self.assertTrue(res["passed"])

    def test_diagnostics_and_truthful_live_prerequisites(self):
        """Subsystem diagnostics return machine-readable results with truthful blockers."""
        diag = eval_mod.run_diagnostics(db_path=self.db_path, live=False, quiet=True)
        self.assertIn("status", diag)
        self.assertIn("diagnostics", diag)
        for key in ["topic", "extraction", "digest", "retention", "access", "status"]:
            self.assertIn(key, diag["diagnostics"])

        ok_ask, reason_ask = eval_mod.check_live_gate_prerequisites("M3.08", db_path=self.db_path)
        self.assertFalse(ok_ask)
        self.assertIn("Schema migration required", reason_ask or "")

        ok_digest, reason_digest = eval_mod.check_live_gate_prerequisites("M4.08", db_path=self.db_path)
        self.assertFalse(ok_digest)
        self.assertIn("Schema migration required", reason_digest or "")


if __name__ == "__main__":
    unittest.main()
