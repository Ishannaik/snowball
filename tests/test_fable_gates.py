#!/usr/bin/env python3
"""Behavior regressions and the gate-evidence contract.

These tests do not execute the 44 Fable gates. A green run of this module is
not `fixture_verified`, `live_verified`, or `soak_verified` for those gates.
Completion is a row in `data/gate-evidence.jsonl` written by
`tools/eval_gates.py` `record_gate_evidence`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def import_tool(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


orch = import_tool("orchestrator", "tools/orchestrator.py")
eval_mod = import_tool("eval_gates", "tools/eval_gates.py")
wiki_mod = import_tool("wiki_compiler", "tools/wiki_compiler.py")
ask_mod = import_tool("ask", "tools/ask.py")


class TestBehaviorRegressions(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="snowball-fable-gates-")
        self.db_path = Path(self.td.name) / "snowball.db"
        self.store = orch.Store.open(self.db_path)
        self.cfg = orch.Config()
        self.orchestrator = orch.SnowballOrchestrator(self.cfg, self.store, dry=True)

    def tearDown(self):
        if hasattr(self, "store") and hasattr(self.store, "db"):
            self.store.db.close()
        with contextlib.suppress(Exception):
            self.td.cleanup()

    def test_resolve_and_pin_rejects_loopback_and_disallowed_port(self):
        """resolve_and_pin rejects loopback names and a port other than 80 or 443."""
        blocked_targets = [
            ("localhost", 443),
            ("127.0.0.1", 443),
            ("example.com", 8080),
        ]
        for host, port in blocked_targets:
            with self.assertRaises(orch.SSRFBlocked):
                orch.resolve_and_pin(host, port)

    def test_canonical_url_three_variants_collapse(self):
        """canonical_url collapses three variants of one article URL."""
        v1 = "http://www.example.com/grants/ai?utm_source=twitter&ref=newsletter"
        v2 = "https://example.com/grants/ai/"
        v3 = "https://m.example.com/grants/ai"

        c1 = orch.canonical_url(v1)
        c2 = orch.canonical_url(v2)
        c3 = orch.canonical_url(v3)

        self.assertEqual(c1, "https://example.com/grants/ai")
        self.assertEqual(c2, "https://example.com/grants/ai")
        self.assertEqual(c3, "https://example.com/grants/ai")

    def test_wall_handling_challenge(self):
        """classify_response marks a Turnstile body as walled/challenge."""
        challenge_html = "<html><head><title>Just a moment...</title></head><body><div class='cf-turnstile'>Please verify you are a human</div></body></html>"
        outcome, kind = orch.classify_response(200, challenge_html)
        self.assertEqual(outcome, "walled")
        self.assertEqual(kind, "challenge")

    def test_outage_vs_failure_split(self):
        """classify_response maps HTTP 429 and 503 to outage."""
        outcome, _ = orch.classify_response(429, "Too Many Requests")
        self.assertEqual(outcome, "outage")
        outcome_503, _ = orch.classify_response(503, "Service Unavailable")
        self.assertEqual(outcome_503, "outage")

    def test_compaction_loss_70_percent_floor(self):
        """merge_page keeps the old body when the replacement is far shorter."""
        old_body = "# Overview\n\n" + ("Extensive documentation of requirements and deadlines.\n" * 20)
        truncated_body = "Short summary only."
        merged = wiki_mod.merge_page(old_body, {"tags": ["grant"]}, truncated_body)
        self.assertIn("Extensive documentation of requirements", merged)
        self.assertIn("Short summary only", merged)

    def test_entity_key_normalization(self):
        """entity_key normalizes name, org, and year."""
        k1 = orch.entity_key("Mozilla Technology Fund", "Mozilla Foundation", "2026-12-15")
        k2 = orch.entity_key("mozilla-technology-fund", "mozilla foundation", "2026-01-01")
        self.assertEqual(k1, "mozilla-technology-fund|mozilla-foundation|2026")
        self.assertEqual(k2, "mozilla-technology-fund|mozilla-foundation|2026")

    def test_grok_snowflake_timestamp_validation(self):
        """snowflake_utc decodes a known id into year 2024."""
        dt_tweet = orch.snowflake_utc("1800000000000000000")
        self.assertIsNotNone(dt_tweet)
        self.assertEqual(dt_tweet.year, 2024)

    def test_topic_lexical_cosine_thresholds(self):
        """Identical token vectors score at least 0.75 and disjoint vectors score under 0.30."""
        from collections import Counter

        c1 = Counter(["free", "cloud", "gpu", "credits"])
        c2 = Counter(["free", "cloud", "gpu", "credits"])
        c3 = Counter(["kubernetes", "cluster", "orchestration"])
        df = {"free": 1, "cloud": 1, "gpu": 1, "credits": 1}

        sim_same = orch.tfidf_cosine(c1, c2, df, 10)
        self.assertGreaterEqual(sim_same, 0.75)

        sim_diff = orch.tfidf_cosine(c1, c3, df, 10)
        self.assertLess(sim_diff, 0.30)

    def test_malformed_fts_queries(self):
        """Sanitized FTS MATCH handles unbalanced quotes, operators, and malformed queries safely."""
        # Odd count of double quotes
        q1 = ask_mod.sanitize_fts_query('December "grants')
        self.assertIn('"December"', q1)
        self.assertIn('"grants"', q1)

        # Multiple unbalanced quotes and operators
        q2 = ask_mod.sanitize_fts_query('""" OR AND NOT ***')
        self.assertEqual(q2, "")

        # Natural question punctuation
        q3 = ask_mod.sanitize_fts_query("which grants close in December?")
        self.assertIn('"grants"', q3)
        self.assertIn('"December"', q3)

        # Execution against SQLite FTS5 does not crash with syntax error
        self.store.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS test_fts USING fts5(title, body, tokenize='unicode61')"
        )
        self.store.db.execute("INSERT INTO test_fts(title, body) VALUES ('grants', 'December grant opportunity')")
        if q1:
            rows = self.store.db.execute("SELECT * FROM test_fts WHERE test_fts MATCH ?", (q1,)).fetchall()
            self.assertGreaterEqual(len(rows), 1)

    def test_mixed_grounded_and_ungrounded_sentences(self):
        """Sentence validator drops uncited sentences, traversal paths, and ungrounded numbers on same line."""
        retrieved_paths = {"wiki/entities/grant-a.md", "wiki/entities/grant-b.md"}
        chunks = [
            {"path": "wiki/entities/grant-a.md", "title": "Grant A", "text": "Mozilla Technology Fund awards up to $50,000 for open source."},
            {"path": "wiki/entities/grant-b.md", "title": "Grant B", "text": "Applications close in December 31 2026 for fellowship."},
        ]

        # Same-line mixed: first sentence ungrounded, second grounded
        same_line = "Invented grant is $9000000. Real deadline is Dec 31 [wiki/entities/grant-b.md]"
        cleaned, sufficient = ask_mod.validate_and_filter_sentences(same_line, retrieved_paths, "grants", chunks=chunks)
        self.assertTrue(sufficient)
        self.assertNotIn("Invented grant is $9000000", cleaned)
        self.assertIn("Real deadline is Dec 31 [wiki/entities/grant-b.md]", cleaned)

        # Wrong amount with real path: $9,000,000 does not exist in chunk for grant-a ($50,000)
        wrong_amount = "Mozilla Technology Fund awards $9000000 [wiki/entities/grant-a.md]"
        cleaned_bad, suff_bad = ask_mod.validate_and_filter_sentences(wrong_amount, retrieved_paths, "grants", chunks=chunks)
        self.assertFalse(suff_bad)
        self.assertIn("Insufficient verified evidence", cleaned_bad)

        # Malformed path traversal
        traversal = "Grant info [../../etc/passwd]"
        cleaned_trav, suff_trav = ask_mod.validate_and_filter_sentences(traversal, retrieved_paths, "grants", chunks=chunks)
        self.assertFalse(suff_trav)
        self.assertIn("Insufficient verified evidence", cleaned_trav)

        # Grounded factual sentence with real numbers survives
        valid_line = "Mozilla Technology Fund awards up to $50,000 [wiki/entities/grant-a.md]"
        cleaned_ok, suff_ok = ask_mod.validate_and_filter_sentences(valid_line, retrieved_paths, "grants", chunks=chunks)
        self.assertTrue(suff_ok)
        self.assertIn("awards up to $50,000 [wiki/entities/grant-a.md]", cleaned_ok)

    def test_source_edits_and_reindex(self):
        """Human edits to markdown files take precedence and reindex synchronizes file count."""
        wiki_dir = Path(self.td.name) / "test_wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        grant_file = wiki_dir / "grant-december.md"
        grant_file.write_text(
            "---\ntitle: December Grant\nkey: dec-grant\n---\n# December Grant\nOriginal deadline November 30.\n",
            encoding="utf-8",
        )

        # Compile / reindex into DB
        wiki_mod.reindex_wiki(self.store.db, wiki_dir)

        # Human edit directly on disk
        grant_file.write_text(
            "---\ntitle: December Grant\nkey: dec-grant\n---\n# December Grant\nHuman edited deadline to December 31.\n",
            encoding="utf-8",
        )

        # Search reflects human edit from disk directly
        res = ask_mod.search_local_knowledge(self.store.db, "December Grant", wiki_dir=wiki_dir)
        self.assertGreaterEqual(len(res["chunks"]), 1)
        combined_text = " ".join(c["text"] for c in res["chunks"])
        self.assertIn("Human edited deadline to December 31", combined_text)

        # Reindex FTS verifies markdown count equals wiki count
        reindex_res = eval_mod.reindex_fts(self.db_path, wiki_dir=wiki_dir, quiet=True)
        self.assertEqual(reindex_res["markdown_files"], 1)
        self.assertEqual(reindex_res["wiki"], 1)
        self.assertTrue(reindex_res["passed"])

    def test_false_long_quote_fails_digest_audit(self):
        """Long false quote not in source text fails digest audit; unquoted items fail; 0 items never pass."""
        zero_res = eval_mod.eval_digest(since="7d", db_path=self.db_path, quiet=True)
        self.assertFalse(zero_res["passed"])

        raw_file = Path(self.td.name) / "raw_grant.txt"
        raw_file.write_text("Verified grant awards $50,000 to open source projects.", encoding="utf-8")
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        self.store.db.execute("INSERT OR IGNORE INTO domains(domain) VALUES ('example.com')")
        self.store.db.execute(
            """
            INSERT INTO pages(id, canon_url, domain, raw_path, status, next_check_at, interval_sec, discovered_via, created_at)
            VALUES (1, 'https://example.com/grant', 'example.com', ?, 'fetched', ?, 3600, 'seed', ?)
            """,
            (str(raw_file), now_ts, now_ts),
        )

        # Entity with a long false quote not in the page
        false_quote = "This is a long fabricated quote that does not appear in the page body text at all."
        self.store.db.execute("INSERT OR IGNORE INTO families(name, recheck_days, focus) VALUES ('grants', 7, 'grants')")
        self.store.db.execute(
            """
            INSERT INTO entities(key, name, norm_name, kind, family, link, evidence_quote, evidence_page_id, first_seen_at, last_seen_at)
            VALUES ('fake-key', 'Fake Grant', 'fake grant', 'grant', 'grants', 'https://example.com/grant', ?, 1, 1000, 1000)
            """,
            (false_quote,),
        )
        today_iso = dt.date.today().isoformat()
        self.store.db.execute(
            "INSERT INTO digest_items(id, day, ref_kind, ref_id, rank, reason, sent_at, message_id) VALUES (1, ?, 'entity', 'fake-key', 1, 'reason', 1000, 'msg-123')",
            (today_iso,),
        )
        self.store.db.commit()

        audit_res = eval_mod.eval_digest(since="7d", db_path=self.db_path, quiet=True)
        self.assertFalse(audit_res["passed"])
        self.assertEqual(audit_res["total_quotes"], 1)
        self.assertEqual(audit_res["valid_quotes"], 0)

    def test_honest_insufficient_evidence_no_false_scheduled_claims(self):
        """Synthesize answer returns honest insufficient evidence without invented facts or scheduled claims."""
        empty_evidence = {"query": "nonexistent opportunity query", "chunks": [], "retrieved_paths": []}
        ans = ask_mod.synthesize_answer(empty_evidence, use_model=False)
        self.assertIn("Insufficient verified evidence", ans)
        self.assertNotIn("schedule", ans.lower())
        self.assertNotIn("crawl frontier", ans.lower())

    def test_eval_classify_and_extract_production_contracts(self):
        """Evaluators report production classifier and extraction contracts."""
        classify_fixture = Path(self.td.name) / "pages_classify.jsonl"
        # 50 fixtures with labels
        rows = []
        for i in range(50):
            rows.append({"title": f"Directory {i}", "text": f"Apply for active grants directory {i} with 10 links.", "url": f"https://example.com/h{i}", "links": [f"https://example.com/{i}"], "label": "hub"})
        classify_fixture.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        c_res = eval_mod.eval_classify(classify_fixture, db_path=self.db_path, quiet=True)
        self.assertEqual(c_res["classifier"], "production")
        self.assertEqual(c_res["total"], 50)

        extract_fixture = Path(self.td.name) / "items_extract.jsonl"
        source_text = "Mozilla Foundation announces $50,000 Technology Fund grants closing December 15 2026."
        extract_fixture.write_text(
            json.dumps({
                "text": source_text,
                "url": "https://example.com/apply",
                "entities": [{
                    "name": "Technology Fund",
                    "org": "Mozilla Foundation",
                    "kind": "grant",
                    "evidence_quote": "Mozilla Foundation announces $50,000 Technology Fund grants closing December 15 2026.",
                }],
            }) + "\n",
            encoding="utf-8",
        )
        e_res = eval_mod.eval_extract(extract_fixture, db_path=self.db_path, quiet=True)
        self.assertEqual(e_res["extractor"], "fixture_validation")
        self.assertFalse(e_res["production"])

    def test_diagnostics_and_truthful_live_gates(self):
        """Diagnostics and live gate prerequisites truthfully report blockers without fake passes."""
        empty_db = Path(self.td.name) / "nonexistent.db"
        ok, reason = eval_mod.check_live_gate_prerequisites("M4.08", db_path=empty_db)
        self.assertFalse(ok)
        self.assertIn("does not exist", reason)

        d_res = eval_mod.run_diagnostics(db_path=self.db_path, live=False, quiet=True)
        self.assertIn("status", d_res)
        self.assertIn("topic", d_res["diagnostics"])
        self.assertIn("extraction", d_res["diagnostics"])
        self.assertIn("digest", d_res["diagnostics"])
        self.assertIn("retention", d_res["diagnostics"])
        self.assertIn("access", d_res["diagnostics"])
        self.assertIn("status", d_res["diagnostics"])
class TestGateEvidence(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="snowball-gate-evidence-")
        self.ledger = Path(self.td.name) / "gate-evidence.jsonl"

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.td.cleanup()

    def _record(self, **kwargs):
        payload = {
            "command": "recorded command",
            "exit_code": None,
            "started_at": None,
            "finished_at": None,
            "evidence_path": None,
            "ledger_path": self.ledger,
            "owner": "T0",
            "detail": "evidence contract",
        }
        payload.update(kwargs)
        return eval_mod.record_gate_evidence(**payload)

    def test_status_snapshot_records_no_verified_gate(self):
        rows = eval_mod.load_status_snapshot()
        self.assertEqual([row["gate_id"] for row in rows], eval_mod.expected_gate_ids())
        self.assertEqual(len(rows), 59)
        verified = [row["gate_id"] for row in rows if row["status"] in eval_mod.VERIFIED_STATUSES]
        self.assertEqual(verified, [])
        by_id = {row["gate_id"]: row for row in rows}
        self.assertEqual(by_id["M0.01"]["status"], "implemented")
        for gate_id, row in by_id.items():
            if gate_id != "M0.01":
                self.assertEqual(row["status"], "blocked")
            self.assertIsNone(row["exit_code"])
            self.assertIsNone(row["started_at"])
            self.assertIsNone(row["finished_at"])
            self.assertIsNone(row["evidence_path"])
            for key in eval_mod.REQUIRED_EVIDENCE_FIELDS:
                self.assertIn(key, row)
        self.assertIn("T5", by_id["M2.01"]["owner"])
        self.assertIn("T10", by_id["M2.01"]["owner"])
        self.assertIn("T5", by_id["M2.02"]["owner"])
        self.assertIn("T10", by_id["M2.02"]["owner"])
        self.assertIn("T9", by_id["M2.07"]["owner"])
        self.assertIn("T10", by_id["M4.08"]["owner"])
        self.assertEqual(
            by_id["M4.08"]["blocking_regressions"],
            [
                "digest-eval-zero-over-zero",
                "digest-quote-length",
                "digest-premature-acknowledgement",
            ],
        )

    def test_observed_zero_over_zero_cannot_verify_digest_gate(self):
        observed = eval_mod.REGRESSIONS["digest-eval-zero-over-zero"]
        self.assertEqual(observed["owners"], ("T10",))
        self.assertEqual(observed["evaluator_result"]["total_quotes"], 0)
        self.assertEqual(observed["evaluator_result"]["valid_quotes"], 0)
        evidence = Path(self.td.name) / "digest.txt"
        evidence.write_text("observed 0/0\n", encoding="utf-8")
        with self.assertRaises(eval_mod.GateEvidenceError) as caught:
            self._record(
                gate_id="M4.08",
                status="live_verified",
                command="python tools/eval_gates.py digest --since 7d",
                exit_code=0,
                started_at="2026-10-08T00:00:00Z",
                finished_at="2026-10-08T00:00:01Z",
                evidence_path=str(evidence),
                evaluator_result=observed["evaluator_result"],
                cleared_regressions=tuple(eval_mod.blocking_regression_ids("M4.08")),
                owner="T10",
            )
        self.assertIn("zero quotes", str(caught.exception))

    def test_quote_length_cannot_verify_digest_gate(self):
        observed = eval_mod.REGRESSIONS["digest-quote-length"]["evaluator_result"]
        self.assertEqual(
            eval_mod.evidence_block_reason("M4.08", observed),
            "quote length is not tier-2 verification",
        )

    def test_premature_acknowledgement_blocks_delivery_gates(self):
        observed = eval_mod.REGRESSIONS["digest-premature-acknowledgement"]
        self.assertEqual(observed["owners"], ("T9",))
        self.assertEqual(
            eval_mod.evidence_block_reason("M2.07", observed["evaluator_result"]),
            "premature delivery acknowledgement",
        )
        self.assertEqual(
            eval_mod.evidence_block_reason("M2.08", observed["evaluator_result"]),
            "premature delivery acknowledgement",
        )
        evidence = Path(self.td.name) / "delivery.txt"
        evidence.write_text("compile set reported_at\n", encoding="utf-8")
        with self.assertRaises(eval_mod.GateEvidenceError) as caught:
            self._record(
                gate_id="M2.07",
                status="live_verified",
                command="python tools/digest.py",
                exit_code=0,
                started_at="2026-10-08T00:00:00Z",
                finished_at="2026-10-08T00:00:01Z",
                evidence_path=str(evidence),
                evaluator_result=observed["evaluator_result"],
                cleared_regressions=("digest-premature-acknowledgement",),
                owner="T9",
            )
        self.assertIn("premature", str(caught.exception))

    def test_heuristic_classifier_and_fixture_extract_stay_blocked(self):
        self.assertEqual(eval_mod.REGRESSIONS["classifier-not-production"]["owners"], ("T5", "T10"))
        self.assertEqual(eval_mod.REGRESSIONS["extractor-not-production"]["owners"], ("T5", "T10"))
        self.assertEqual(
            eval_mod.evidence_block_reason("M2.01", {"accuracy": 100.0, "passed": True}),
            "heuristic evaluator is not the production classifier",
        )
        self.assertEqual(
            eval_mod.evidence_block_reason("M2.02", {"valid_quotes": 20, "total": 20, "passed": True}),
            "fixture scan is not the production extractor",
        )

    def test_six_slug_cases_and_empty_reindex_are_not_verification(self):
        self.assertEqual(eval_mod.REGRESSIONS["slug-sample-not-property"]["owners"], ("T5",))
        self.assertEqual(eval_mod.REGRESSIONS["empty-reindex"]["owners"], ("T8",))
        self.assertEqual(
            eval_mod.evidence_block_reason("M1.10", {"cases": 6, "seeded": True}),
            "slug sample is not the 10000-string property gate",
        )
        self.assertEqual(
            eval_mod.evidence_block_reason("M2.12", {"claims": 0, "topics": 0, "wiki": 0}),
            "empty reindex proves nothing",
        )

    def test_verified_status_requires_a_nonempty_evidence_file(self):
        with self.assertRaises(eval_mod.GateEvidenceError):
            self._record(
                gate_id="M1.08",
                status="fixture_verified",
                command="mock 404 three times",
                exit_code=0,
                started_at="2026-10-08T00:00:00Z",
                finished_at="2026-10-08T00:00:01Z",
                evidence_path=None,
            )
        empty = Path(self.td.name) / "empty.txt"
        empty.write_text("", encoding="utf-8")
        with self.assertRaises(eval_mod.GateEvidenceError):
            self._record(
                gate_id="M1.08",
                status="fixture_verified",
                command="mock 404 three times",
                exit_code=0,
                started_at="2026-10-08T00:00:00Z",
                finished_at="2026-10-08T00:00:01Z",
                evidence_path=str(empty),
            )

    def test_soak_gate_rejects_a_fixture_status(self):
        evidence = Path(self.td.name) / "soak.txt"
        evidence.write_text("short sample\n", encoding="utf-8")
        with self.assertRaises(eval_mod.GateEvidenceError) as caught:
            self._record(
                gate_id="M1.02",
                status="fixture_verified",
                command="systemctl --user status snowball-supervisor",
                exit_code=0,
                started_at="2026-10-08T00:00:00Z",
                finished_at="2026-10-08T00:00:01Z",
                evidence_path=str(evidence),
                evaluator_result={"window_seconds": 86400},
            )
        self.assertIn("soak_verified", str(caught.exception))

    def test_soak_gate_accepts_a_full_window_with_evidence(self):
        evidence = Path(self.td.name) / "soak-day.txt"
        evidence.write_text("24h supervisor sample\n", encoding="utf-8")
        row = self._record(
            gate_id="M1.02",
            status="soak_verified",
            command="systemctl --user status snowball-supervisor",
            exit_code=0,
            started_at="2026-10-08T00:00:00Z",
            finished_at="2026-10-09T00:00:00Z",
            evidence_path=str(evidence),
            evaluator_result={"window_seconds": 86400},
            owner="T11/T13",
        )
        self.assertEqual(row["status"], "soak_verified")
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["evidence_path"], str(evidence))

    def test_command_secrets_are_redacted_and_secret_paths_are_rejected(self):
        secret = "supersecretvalue"
        row = self._record(
            gate_id="M1.08",
            status="blocked",
            command=f"python tools/eval_gates.py --apikey={secret}",
            owner="T7",
        )
        text = self.ledger.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertIn("REDACTED", row["command"])
        with self.assertRaises(eval_mod.GateEvidenceError):
            self._record(
                gate_id="M1.08",
                status="blocked",
                command="mock 404",
                evidence_path=f"data/out?token={secret}",
            )

    def test_false_digest_pass_can_be_recorded_as_blocked(self):
        observed = eval_mod.REGRESSIONS["digest-eval-zero-over-zero"]["evaluator_result"]
        row = self._record(
            gate_id="M4.08",
            status="blocked",
            command="python tools/eval_gates.py digest --since 7d",
            exit_code=0,
            evaluator_result=observed,
            owner="T9/T10/T13",
            detail="observed pass flag on zero quotes",
        )
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["block_reason"], "zero quotes are not verification")
        self.assertEqual(row["exit_code"], 0)

    def test_production_classifier_payload_can_be_recorded_when_evidence_exists(self):
        evidence = Path(self.td.name) / "classify.txt"
        evidence.write_text("production classifier 43 of 50\n", encoding="utf-8")
        row = self._record(
            gate_id="M2.01",
            status="fixture_verified",
            command="snowball eval classify tests/pages_labelled_50.jsonl",
            exit_code=0,
            started_at="2026-10-08T01:00:00Z",
            finished_at="2026-10-08T01:00:02Z",
            evidence_path=str(evidence),
            evaluator_result={"classifier": "production", "correct": 43, "total": 50},
            cleared_regressions=("classifier-not-production",),
            owner="T5/T10",
            detail="recorder contract only; T0 does not run the production classifier",
        )
        self.assertEqual(row["status"], "fixture_verified")
        stored = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(stored["gate_id"], "M2.01")
        self.assertEqual(stored["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
