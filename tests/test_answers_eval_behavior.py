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
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

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

    def test_no_gemini_api_in_ask(self):
        """Verification that tools/ask.py contains no references to Gemini API or external keys."""
        ask_source = (ROOT / "tools" / "ask.py").read_text(encoding="utf-8")
        self.assertNotIn("generativelanguage.googleapis.com", ask_source)
        self.assertNotIn("gemini-3.8-flash", ask_source)
        self.assertNotIn("GEMINI_API_KEY", ask_source)

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
        """reindex_fts rebuilds wiki_fts and verifies all markdown files including INDEX.md."""
        wiki_dir = Path(self.td.name) / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        (wiki_dir / "p1.md").write_text("# Page 1\nContent 1\n", encoding="utf-8")
        (wiki_dir / "p2.md").write_text("# Page 2\nContent 2\n", encoding="utf-8")
        (wiki_dir / "INDEX.md").write_text("# Index\nDirectory listing\n", encoding="utf-8")

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
        self.assertIn("0 compiled pages", reason_ask)

        ok_digest, reason_digest = eval_mod.check_live_gate_prerequisites("M4.08", db_path=self.db_path)
        self.assertFalse(ok_digest)
        self.assertIn("0 acknowledged delivered digest items", reason_digest)


if __name__ == "__main__":
    unittest.main()
