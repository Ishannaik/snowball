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
import sqlite3
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

    def test_status_id_timestamp_decodes_year(self):
        """snowflake_utc decodes a known status id into year 2024."""
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
        self.assertIn("schema", d_res["diagnostics"])
        self.assertIn("roles", d_res["diagnostics"])
        self.assertIn("usage", d_res["diagnostics"])
        self.assertIn("fields", d_res["diagnostics"])
        self.assertFalse(d_res["created"])
        self.assertFalse(d_res["migrated"])
        self.assertFalse(d_res["hard_provider_cap"])

    def test_missing_and_old_schema_are_refused_without_create_or_migrate(self):
        """A missing or older database is a visible refusal and is left untouched."""
        missing = Path(self.td.name) / "missing.db"
        refused = eval_mod.run_diagnostics(db_path=missing, live=False, quiet=True)
        self.assertFalse(missing.exists())
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["code"], "database_missing")
        self.assertFalse(refused["created"])
        self.assertFalse(refused["migrated"])
        self.assertIn("does not exist", refused["diagnostics"]["schema"]["message"])

        old = Path(self.td.name) / "old.db"
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
        conn.commit()
        conn.close()
        before_size = old.stat().st_size
        old_report = eval_mod.run_diagnostics(db_path=old, live=False, quiet=True)
        self.assertEqual(old.stat().st_size, before_size)
        self.assertEqual(old_report["status"], "refused")
        self.assertEqual(old_report["code"], "migration_required")
        self.assertEqual(old_report["diagnostics"]["schema"]["observed_version"], 2)
        self.assertFalse(old_report["created"])
        self.assertFalse(old_report["migrated"])
        check = sqlite3.connect(old.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = check.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        finally:
            check.close()
        self.assertEqual(version, "2")
        self.assertNotIn("goals", tables)
        self.assertNotIn("model_calls", tables)

        older = Path(self.td.name) / "v3.db"
        conn = sqlite3.connect(older)
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '3')")
        conn.commit()
        conn.close()
        before = older.stat().st_size
        v3_report = eval_mod.run_diagnostics(db_path=older, live=False, quiet=True)
        self.assertEqual(older.stat().st_size, before)
        self.assertEqual(v3_report["status"], "refused")
        self.assertEqual(v3_report["code"], "migration_required")
        self.assertEqual(v3_report["diagnostics"]["schema"]["observed_version"], 3)
        self.assertFalse(v3_report["created"])
        self.assertFalse(v3_report["migrated"])
        check = sqlite3.connect(older.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            version = check.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            check.close()
        self.assertEqual(version, "3")
        self.assertNotIn("goals", tables)

    def test_diagnostics_separate_usage_and_hide_private_values(self):
        """Actual, estimated, and legacy usage stay distinct. Private values and credentials are omitted."""
        db = self.store.db
        db.execute("INSERT INTO domains(domain) VALUES ('example.test')")
        db.execute(
            """
            INSERT INTO pages (
              canon_url, domain, status, next_check_at, interval_sec, discovered_via, created_at
            ) VALUES ('https://example.test/tuition', 'example.test', 'fetched', 0, 86400, 'seed', 1)
            """
        )
        db.execute(
            """
            INSERT INTO source_snapshots (
              page_id, content_hash, retrieved_at, text_path, visibility, provenance
            ) VALUES (1, 'abc', 1, 'synthetic.txt', 'public', 'public_fetch')
            """
        )
        db.execute(
            """
            INSERT INTO observations (
              snapshot_id, page_id, field_key, value_json, subject_json, quote, quote_start, quote_end,
              extractor_model, prompt_version, assurance, created_at
            ) VALUES (
              1, 1, 'tuition', '{"amount":0}', '{}', 'Tuition is 0', 0, 12,
              'fixture', 'v1', 'code_checked', 1
            )
            """
        )
        db.execute(
            """
            INSERT INTO goals (slug, title, state, created_at, updated_at)
            VALUES ('synthetic-goal', 'Synthetic', 'draft', 1, 1)
            """
        )
        db.execute(
            """
            INSERT INTO profile_fields (
              goal_id, key, value_json, status, provenance, visibility, updated_at
            ) VALUES (
              1, 'max_upfront_cash', '{"amount":0,"currency":"ZZZSECRET"}',
              'known', 'user_confirmed', 'private', 1
            )
            """
        )
        db.execute(
            """
            INSERT INTO profile_fields (
              goal_id, key, value_json, status, provenance, visibility, updated_at
            ) VALUES (1, 'degree_level', '"masters"', 'known', 'user_confirmed', 'public_ok', 1)
            """
        )
        db.execute(
            """
            INSERT INTO model_calls (
              role, requested_model, reported_model, transport, source_snapshot_ids_json,
              tokens_in, tokens_out, thinking_tokens, usage_kind, state, started_at
            ) VALUES (
              'extract', 'public-extract', 'public-extract', 'gemini', '[]',
              11, 3, 2, 'actual', 'complete', 1
            )
            """
        )
        db.execute(
            """
            INSERT INTO model_calls (
              role, requested_model, transport, source_snapshot_ids_json,
              tokens_in, tokens_out, usage_kind, state, started_at
            ) VALUES ('review', 'public-review', 'agy', '[]', 5, 1, 'estimated', 'complete', 2)
            """
        )
        db.execute(
            """
            INSERT INTO model_calls (
              role, requested_model, transport, source_snapshot_ids_json,
              tokens_in, tokens_out, usage_kind, state, started_at
            ) VALUES ('review', 'historical-model', 'legacy', '[]', 9, 4, 'legacy', 'complete', 3)
            """
        )
        db.execute(
            """
            INSERT INTO model_calls (
              role, requested_model, reported_model, transport, source_snapshot_ids_json,
              tokens_in, tokens_out, usage_kind, state, started_at
            ) VALUES (
              'review', 'xai-oauth/grok-4.7', 'agy-pool/claude-sonnet-4-6', 'grok', '[]',
              7, 1, 'actual', 'complete', 4
            )
            """
        )
        db.execute(
            """
            INSERT INTO engine_spend (day, engine, calls, tokens_in, tokens_out)
            VALUES ('2026-10-01', 'grok', 4, 100, 20)
            """
        )
        cfg = type(
            "Cfg",
            (),
            {
                "gemini_api_key": "sk-supersecretvalue",
                "private_model_endpoint": "http://user:super-secret-token@127.0.0.1:9/v1",
                "private_model_attested": False,
            },
        )()
        report = eval_mod.run_diagnostics(db_path=self.db_path, live=False, quiet=True, cfg=cfg)
        blob = json.dumps(report)
        self.assertNotIn("ZZZSECRET", blob)
        self.assertNotIn("super-secret-token", blob)
        self.assertNotIn("sk-supersecretvalue", blob)
        self.assertIn("xai-oauth/grok-4.7", blob)
        self.assertNotIn("agy-pool/claude-sonnet-4-6", blob)
        self.assertFalse(report["hard_provider_cap"])
        usage = report["diagnostics"]["usage"]
        self.assertEqual(usage["calls"]["actual"], 1)
        self.assertEqual(usage["calls"]["estimated"], 1)
        self.assertEqual(usage["calls"]["legacy"], 1)
        self.assertEqual(usage["calls"]["unknown"], 1)
        self.assertEqual(usage["by_role"]["extract"]["actual"]["tokens_in"], 11)
        self.assertEqual(usage["by_role"]["extract"]["actual"]["thinking_tokens"], 2)
        self.assertEqual(usage["by_role"]["review"]["estimated"]["tokens_in"], 5)
        self.assertEqual(usage["by_role"]["review"]["legacy"]["tokens_in"], 9)
        self.assertEqual(usage["legacy_aggregate_tokens_in"], 100)
        self.assertFalse(usage["hard_provider_cap"])
        self.assertEqual(
            usage["legacy_engines"],
            [
                {
                    "engine": "grok",
                    "attribution": "legacy",
                    "usage_kind": "unknown",
                    "calls": 4,
                    "tokens_in": 100,
                    "tokens_out": 20,
                }
            ],
        )
        identities = {
            (row["transport"], row["requested_model"], row["reported_model"], row["usage_kind"]): row
            for row in usage["identities"]
        }
        self.assertEqual(identities[("grok", "xai-oauth/grok-4.7", "", "unknown")]["tokens_in"], 7)
        self.assertEqual(identities[("legacy", "historical-model", "", "legacy")]["tokens_in"], 9)
        self.assertNotIn("claude", json.dumps(usage["legacy_engines"]).lower())
        fields = report["diagnostics"]["fields"]
        self.assertIn("degree_level", fields["public_field_keys"])
        self.assertNotIn("max_upfront_cash", fields["public_field_keys"])
        self.assertEqual(fields["private_profile_fields"], 1)
        self.assertEqual(fields["observations"][0]["field_key"], "tuition")
        self.assertEqual(fields["sources"][0]["provenance"], "public_fetch")
        private = report["diagnostics"]["roles"]["private_eval"]
        self.assertFalse(private["available"])
        self.assertFalse(private["attested_local"])
        self.assertEqual(private["reason"], "local_model_required")
        self.assertNotIn("private_model_endpoint", private)

    def test_estimated_and_loopback_roles_cannot_claim_a_hard_cap_or_local_inference(self):
        """Role normalization keeps estimated usage and loopback endpoints unattested."""
        estimated = eval_mod._normalize_role(
            "review",
            "ReviewEngine",
            {
                "configured": True,
                "available": True,
                "reason": "ready",
                "model_id": "claude-sonnet-4-6",
                "transport": "agy",
                "usage_mode": "estimated",
                "hard_provider_cap": True,
                "conformance_attested": True,
            },
        )
        self.assertEqual(estimated["usage_mode"], "estimated")
        self.assertFalse(estimated["hard_provider_cap"])
        self.assertFalse(estimated["conformance_attested"])

        loopback = eval_mod._normalize_role(
            "private_eval",
            "LocalPrivateEngine",
            {
                "configured": True,
                "available": True,
                "reason": "loopback",
                "model_id": "local-private",
                "transport": "local",
                "usage_mode": "actual",
                "attested_local": True,
                "isolation_documented": True,
                "endpoint_host_class": "loopback",
                "hard_provider_cap": True,
                "endpoint": "http://user:super-secret-token@127.0.0.1:9/v1",
            },
        )
        self.assertFalse(loopback["available"])
        self.assertFalse(loopback["attested_local"])
        self.assertEqual(loopback["reason"], "local_model_required")
        self.assertFalse(loopback["hard_provider_cap"])
        self.assertNotIn("endpoint", loopback)
        self.assertNotIn("super-secret-token", json.dumps(loopback))

        attested = eval_mod._normalize_role(
            "private_eval",
            "LocalPrivateEngine",
            {
                "configured": True,
                "available": True,
                "model_id": "local-private",
                "transport": "local",
                "usage_mode": "actual",
                "attested_local": True,
                "isolation_documented": True,
                "endpoint_host_class": "private",
                "conformance_attested": True,
                "hard_provider_cap": True,
            },
        )
        self.assertTrue(attested["available"])
        self.assertTrue(attested["attested_local"])
        self.assertEqual(attested["reason"], "attested")
        self.assertFalse(attested["hard_provider_cap"])

        legacy = eval_mod._normalize_role(
            "review",
            "ReviewEngine",
            {
                "available": True,
                "configured": True,
                "model_id": "xai-oauth/grok-4.7",
                "reason": "verifier ready",
            },
        )
        self.assertEqual(legacy["model_id"], "xai-oauth/grok-4.7")
        self.assertTrue(legacy["available"])
        self.assertEqual(legacy["reason"], "verifier ready")
        self.assertIn("grok", json.dumps(legacy).lower())

        named = eval_mod._normalize_role(
            "extract",
            "GeminiBulkExtractor",
            {
                "available": True,
                "configured": True,
                "model_id": "openrouter/ornith-india",
                "transport": "openrouter",
                "usage_mode": "estimated",
                "reason": "ornith route kept",
                "hard_provider_cap": True,
                "conformance_attested": True,
            },
        )
        self.assertEqual(named["model_id"], "openrouter/ornith-india")
        self.assertEqual(named["transport"], "openrouter")
        self.assertIn("ornith", named["reason"])
        self.assertFalse(named["hard_provider_cap"])
        self.assertFalse(named["conformance_attested"])

        counted = eval_mod._normalize_role(
            "extract",
            "GeminiBulkExtractor",
            {
                "available": True,
                "configured": True,
                "model": "gemini-3.8-flash",
                "transport": "gemini",
                "usage_kind": "provider_reported",
                "hard_cap": "unavailable",
                "conformance": "unattested",
                "billing_ceiling": True,
                "candidates": 4,
            },
        )
        self.assertEqual(counted["usage_mode"], "actual")
        self.assertEqual(counted["model_id"], "gemini-3.8-flash")
        self.assertFalse(counted["hard_provider_cap"])
        self.assertFalse(counted["conformance_attested"])
        self.assertNotIn("candidates", counted)
        self.assertNotIn("billing_ceiling", counted)

        attested_agy = eval_mod._normalize_role(
            "review",
            "ReviewEngine",
            {
                "available": True,
                "configured": True,
                "model": "agy-pool/claude-sonnet-4-6",
                "transport": "agy",
                "usage_kind": "estimated",
                "hard_cap": "attested",
                "conformance": "attested",
                "billing_ceiling": True,
                "endpoint_host": "127.0.0.1",
                "endpoint_port": 8088,
            },
        )
        self.assertEqual(attested_agy["usage_mode"], "estimated")
        self.assertTrue(attested_agy["hard_provider_cap"])
        self.assertTrue(attested_agy["conformance_attested"])
        self.assertEqual(attested_agy["model_id"], "agy-pool/claude-sonnet-4-6")
        self.assertNotIn("127.0.0.1", json.dumps(attested_agy))
        self.assertNotIn("8088", json.dumps(attested_agy))

    def test_role_report_uses_live_engine_contracts_without_dispatch(self):
        """Availability comes from the current engines and never sends a model call."""
        transport = eval_mod._try_module("engine_transport")
        self.assertIsNotNone(transport)
        dispatched = []

        def trap(name):
            def _blocked(*_args, **_kwargs):
                dispatched.append(name)
                raise AssertionError(name)
            return _blocked

        originals = {}
        for cls_name, method in (
            ("GeminiBulkExtractor", "extract"),
            ("ReviewEngine", "review"),
            ("ReviewEngine", "plan"),
            ("LocalPrivateEngine", "private_eval"),
        ):
            cls = getattr(transport, cls_name)
            originals[(cls_name, method)] = getattr(cls, method)
            setattr(cls, method, trap(method))
        runtime = eval_mod._try_module("goal_runtime")
        original_supervise = getattr(runtime, "supervise", None) if runtime is not None else None
        if runtime is not None:
            def _no_supervise(*_args, **_kwargs):
                raise AssertionError("supervise")
            runtime.supervise = _no_supervise
        try:
            omp = type("Cfg", (), {"gemini_transport": "omp", "gemini_model": "gemini-3.8-flash"})()
            omp_roles = eval_mod.role_report(omp, live=False)
            self.assertEqual(omp_roles["network_check"], "unexercised")
            self.assertEqual(omp_roles["extract"]["engine"], "GeminiBulkExtractor")
            self.assertEqual(omp_roles["extract"]["usage_mode"], "estimated")
            self.assertTrue(omp_roles["extract"]["available"])
            self.assertFalse(omp_roles["extract"]["hard_provider_cap"])
            self.assertEqual(omp_roles["review"]["engine"], "ReviewEngine")
            self.assertEqual(omp_roles["plan"]["engine"], "ReviewEngine")
            self.assertEqual(omp_roles["review"]["reason"], "not_configured")
            self.assertEqual(omp_roles["plan"]["reason"], "not_configured")
            self.assertFalse(omp_roles["review"]["available"])
            self.assertFalse(omp_roles["plan"]["available"])
            self.assertEqual(omp_roles["review"]["model_id"], "")
            self.assertEqual(omp_roles["review"]["requested_model"], "")
            self.assertNotIn("claude", json.dumps(omp_roles["review"]).lower())
            self.assertNotIn("claude", json.dumps(omp_roles["plan"]).lower())
            self.assertFalse(omp_roles["hard_provider_cap"])

            bounded = type(
                "Cfg",
                (),
                {
                    "review_model": "agy-pool/claude-sonnet-4-6",
                    "agy_base_url": "http://127.0.0.1:8088/v1",
                    "adapter_conformance": {
                        "adapter": "openai_compatible",
                        "evidence": "operator-evidence-abcdef123456",
                        "base_url": "http://127.0.0.1:8088/v1",
                        "hard_provider_bound": True,
                    },
                },
            )()
            bounded_roles = eval_mod.role_report(bounded, live=False)
            review_diag = dict(transport.ReviewEngine(bounded, dry=True).diagnostics())
            self.assertEqual(review_diag["usage_kind"], "estimated")
            self.assertEqual(review_diag["hard_cap"], "attested")
            self.assertEqual(review_diag["conformance"], "attested")
            self.assertEqual(bounded_roles["review"]["usage_mode"], "estimated")
            self.assertEqual(bounded_roles["review"]["requested_model"], "agy-pool/claude-sonnet-4-6")
            self.assertEqual(bounded_roles["review"]["reported_model"], review_diag["model"])
            self.assertTrue(bounded_roles["review"]["hard_provider_cap"])
            self.assertTrue(bounded_roles["review"]["conformance_attested"])
            self.assertFalse(bounded_roles["hard_provider_cap"])
            self.assertEqual(bounded_roles["plan"]["reason"], "not_configured")
            self.assertFalse(bounded_roles["plan"]["hard_provider_cap"])
            self.assertNotIn("8088", json.dumps(bounded_roles))
            self.assertNotIn("127.0.0.1", json.dumps(bounded_roles))

            loopback = type(
                "Cfg",
                (),
                {
                    "private_model_endpoint": "http://127.0.0.1:11434/v1",
                    "private_model_id": "local-qwen",
                    "private_model_attested": True,
                    "private_model_isolation": "serving daemon has no egress and isolation is documented",
                },
            )()
            loop_roles = eval_mod.role_report(loopback, live=False)
            private_loop = loop_roles["private_eval"]
            self.assertEqual(private_loop["engine"], "LocalPrivateEngine")
            self.assertEqual(private_loop["requested_model"], "local-qwen")
            self.assertEqual(private_loop["reported_model"], "local-qwen")
            self.assertEqual(private_loop["model_id"], "local-qwen")
            self.assertFalse(private_loop["available"])
            self.assertFalse(private_loop["attested_local"])
            self.assertEqual(private_loop["reason"], "local_model_required")
            self.assertFalse(private_loop["hard_provider_cap"])
            self.assertNotIn("endpoint", private_loop)
            self.assertNotIn("127.0.0.1", json.dumps(private_loop))

            attested = type(
                "Cfg",
                (),
                {
                    "private_model_endpoint": "http://10.9.8.7:11434/v1",
                    "private_model_id": "local-qwen",
                    "private_model_attested": True,
                    "private_model_isolation": "serving daemon has no egress and isolation is documented",
                },
            )()
            attested_roles = eval_mod.role_report(attested, live=False)
            private_ok = attested_roles["private_eval"]
            private_diag = dict(transport.LocalPrivateEngine(attested, dry=True).diagnostics())
            self.assertTrue(private_diag["available"])
            self.assertTrue(private_diag["attested_local"])
            self.assertEqual(private_diag["model"], "local-qwen")
            self.assertEqual(private_diag["reason"], "dry")
            self.assertEqual(private_ok["requested_model"], "local-qwen")
            self.assertEqual(private_ok["reported_model"], "local-qwen")
            self.assertEqual(private_ok["model_id"], "local-qwen")
            self.assertTrue(private_ok["available"])
            self.assertTrue(private_ok["attested_local"])
            self.assertEqual(private_ok["reason"], private_diag["reason"])
            self.assertFalse(private_ok["hard_provider_cap"])
            self.assertNotIn("10.9.8.7", json.dumps(attested_roles))
            self.assertNotIn("11434", json.dumps(attested_roles))

            leaked = type(
                "Cfg",
                (),
                {
                    "gemini_api_key": "sk-supersecretvalue",
                    "review_model": "xai-oauth/grok-4.7",
                    "private_model_endpoint": "http://user:super-secret-token@10.1.2.3:11434/v1",
                    "private_model_id": "local-qwen",
                    "private_model_attested": True,
                    "private_model_isolation": "serving daemon has no egress and isolation is documented",
                },
            )()
            hidden = eval_mod.role_report(leaked, live=False)
            blob = json.dumps(hidden).lower()
            self.assertIn("xai-oauth/grok-4.7", blob)
            self.assertNotIn("super-secret-token", blob)
            self.assertNotIn("sk-supersecretvalue", blob)
            self.assertNotIn("10.1.2.3", blob)
            self.assertTrue(hidden["review"]["available"])
            self.assertEqual(hidden["review"]["model_id"], "xai-oauth/grok-4.7")
            self.assertEqual(hidden["review"]["requested_model"], "xai-oauth/grok-4.7")
            self.assertEqual(hidden["review"]["usage_mode"], "estimated")
            self.assertFalse(hidden["review"]["hard_provider_cap"])
            self.assertFalse(hidden["private_eval"]["available"])
            self.assertEqual(hidden["private_eval"]["requested_model"], "local-qwen")
            self.assertEqual(dispatched, [])
            diag = eval_mod.run_diagnostics(db_path=self.db_path, live=False, quiet=True, cfg=omp)
            self.assertFalse(diag["created"])
            self.assertFalse(diag["migrated"])
            self.assertFalse(diag["hard_provider_cap"])
        finally:
            for (cls_name, method), fn in originals.items():
                setattr(getattr(transport, cls_name), method, fn)
            if runtime is not None and original_supervise is not None:
                runtime.supervise = original_supervise

    def test_robots_disallow_and_zero_paid_tariff_stay_enforced(self):
        """A disallow rule blocks the path, and a zero tariff cannot reserve paid spend."""
        body = "User-agent: *\nDisallow: /private\n"
        self.assertFalse(eval_mod.robots_path_allowed(body, "/private"))
        self.assertTrue(eval_mod.robots_path_allowed(body, "/public"))
        reason = eval_mod.paid_tariff_block_reason({"tariffs": {"zenrows": 0}}, "zenrows")
        self.assertTrue(reason.startswith("PaidBudgetExhausted:tariff_disabled"))
        report = eval_mod.run_diagnostics(db_path=self.db_path, live=False, quiet=True)
        access = report["diagnostics"]["access"]
        self.assertEqual(access["ssrf_guard"], "enforcing")
        self.assertEqual(access["robots"], "enforcing")
        self.assertEqual(access["paid_ceiling_micro_usd"], 0)
        self.assertEqual(access["paid_route"], "disabled")


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

    def test_goal_harness_ids_are_not_ledger_gates(self):
        """G01-G30 stay outside the legacy ledger."""
        with self.assertRaises(eval_mod.GateEvidenceError):
            self._record(gate_id="G01", status="blocked", owner="T10", detail="not a legacy gate")
        self.assertFalse(self.ledger.exists())

    def test_ledger_exercise_matches_recorded_runs(self):
        """The checked-in ledger has no pass or fail until a real run is recorded."""
        rows = eval_mod.load_status_snapshot()
        exercise = eval_mod.exercise_ledger(rows)
        self.assertEqual(exercise["pass"], [])
        self.assertEqual(exercise["fail"], [])
        self.assertEqual(exercise["unexercised"], eval_mod.expected_gate_ids())
        failed = eval_mod.exercise_ledger(
            [
                {
                    "gate_id": "M1.08",
                    "status": "blocked",
                    "finished_at": "2026-10-08T00:00:01Z",
                    "exit_code": 1,
                },
                {
                    "gate_id": "G08",
                    "status": "live_verified",
                    "finished_at": "2026-10-08T00:00:01Z",
                    "exit_code": 0,
                    "evidence_path": "x",
                },
            ]
        )
        self.assertEqual(failed["fail"], ["M1.08"])
        self.assertEqual(failed["pass"], [])
        self.assertNotIn("G08", failed["pass"] + failed["fail"] + failed["unexercised"])


if __name__ == "__main__":
    unittest.main()
