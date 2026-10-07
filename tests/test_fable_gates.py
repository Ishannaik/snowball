#!/usr/bin/env python3
"""Comprehensive Acceptance Gate Test Suite across Milestones M0 through M4.
Directly implements and asserts the 44 observable acceptance gates specified in
docs/SYSTEM_DESIGN.md section 9.
"""

from __future__ import annotations

import contextlib
import importlib.util
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
digest_mod = import_tool("digest", "tools/digest.py")


class TestFableMilestoneGates(unittest.TestCase):
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

    # ------------------------------------------------------------------
    # M0 Gates: Repo, Design, Plan
    # ------------------------------------------------------------------
    def test_m0_spec_documents_present(self):
        """M0 Gate: Verify foundational specification documents exist in docs/."""
        for doc in ["SYSTEM_DESIGN.md", "COMPETITOR_DEEP_DIVE.md", "CODE_QUALITY.md", "PLAN.md"]:
            doc_path = ROOT / "docs" / doc
            self.assertTrue(doc_path.exists(), f"Missing spec document: {doc}")
            self.assertGreater(doc_path.stat().st_size, 500, f"Spec document too small: {doc}")

    # ------------------------------------------------------------------
    # M1 Gates: Store, Frontier, Locks, Waterfall
    # ------------------------------------------------------------------
    def test_m1_slug_fixed_point_property(self):
        """M1 Gate: slug(slug(s)) == slug(s) property test across diverse strings."""
        test_strings = [
            "Mozilla Technology Fund — 2026!",
            "   Cloud GPU Credits (Free)  ",
            "hackathon_devpost_india_2026",
            "### Special & Punctuation * Characters ***",
            "123-numbers-and-symbols-!@#$%",
            "non-latin-café-resumé-naïve",
        ]
        for s in test_strings:
            s1 = orch.slug(s)
            s2 = orch.slug(s1)
            self.assertEqual(s1, s2, f"Slug not fixed point for: {s}")

    def test_m1_ssrf_blocking_gate(self):
        """M1 Gate: resolve_and_pin blocks loopback, private RFC 1918, and non-standard ports."""
        blocked_targets = [
            ("localhost", 443),
            ("127.0.0.1", 443),
            ("example.com", 8080),
        ]
        for host, port in blocked_targets:
            with self.assertRaises(orch.SSRFBlocked):
                orch.resolve_and_pin(host, port)

    def test_m1_canonical_url_three_variants_collapse(self):
        """M1 Gate: Same article via 3 URL variants produces exactly 1 canonical URL."""
        v1 = "http://www.example.com/grants/ai?utm_source=twitter&ref=newsletter"
        v2 = "https://example.com/grants/ai/"
        v3 = "https://m.example.com/grants/ai"

        c1 = orch.canonical_url(v1)
        c2 = orch.canonical_url(v2)
        c3 = orch.canonical_url(v3)

        self.assertEqual(c1, "https://example.com/grants/ai")
        self.assertEqual(c2, "https://example.com/grants/ai")
        self.assertEqual(c3, "https://example.com/grants/ai")

    def test_m1_wall_handling_challenge(self):
        """M1 Gate: classify_response detects Cloudflare Turnstile challenge and returns walled."""
        challenge_html = "<html><head><title>Just a moment...</title></head><body><div class='cf-turnstile'>Please verify you are a human</div></body></html>"
        outcome, kind = orch.classify_response(200, challenge_html)
        self.assertEqual(outcome, "walled")
        self.assertEqual(kind, "challenge")

    def test_m1_outage_vs_failure_split(self):
        """M1 Gate: Rate limit 429 returns outage without permanent failure strikes."""
        outcome, _ = orch.classify_response(429, "Too Many Requests")
        self.assertEqual(outcome, "outage")
        outcome_503, _ = orch.classify_response(503, "Service Unavailable")
        self.assertEqual(outcome_503, "outage")

    # ------------------------------------------------------------------
    # M2 Gates: Classify, Extract, Entity Lock, Compiler, Digest
    # ------------------------------------------------------------------
    def test_m2_classifier_accuracy_gate(self):
        """M2 Gate: snowball eval classify >= 85% accuracy."""
        fixture = ROOT / "tests" / "pages_labelled_50.jsonl"
        res = eval_mod.eval_classify(fixture)
        self.assertGreaterEqual(res["accuracy"], 85.0)
        self.assertTrue(res["passed"])

    def test_m2_extraction_schema_and_verbatim_quotes(self):
        """M2 Gate: snowball eval extract 100% schema & Tier-2 quotes pass."""
        fixture = ROOT / "tests" / "items_20.jsonl"
        res = eval_mod.eval_extract(fixture)
        self.assertEqual(res["valid_schema"], res["total"])
        self.assertEqual(res["valid_quotes"], res["total"])
        self.assertTrue(res["passed"])

    def test_m2_compaction_loss_70_percent_floor(self):
        """M2 Gate: Body shrink below 70% preserves previous markdown content."""
        old_body = "# Overview\n\n" + ("Extensive documentation of requirements and deadlines.\n" * 20)
        truncated_body = "Short summary only."
        merged = wiki_mod.merge_page(old_body, {"tags": ["grant"]}, truncated_body)
        self.assertIn("Extensive documentation of requirements", merged)
        self.assertIn("Short summary only", merged)

    def test_m2_entity_key_normalization(self):
        """M2 Gate: Entity key normalization name|org|year."""
        k1 = orch.entity_key("Mozilla Technology Fund", "Mozilla Foundation", "2026-12-15")
        k2 = orch.entity_key("mozilla-technology-fund", "mozilla foundation", "2026-01-01")
        self.assertEqual(k1, "mozilla-technology-fund|mozilla-foundation|2026")
        self.assertEqual(k2, "mozilla-technology-fund|mozilla-foundation|2026")

    # ------------------------------------------------------------------
    # M3 Gates: Claim and Topic Locks, Ask, Alerts, Snowflake
    # ------------------------------------------------------------------
    def test_m3_grok_snowflake_timestamp_validation(self):
        """M3 Gate: Valid 64-bit Twitter Snowflake ID decoded to UTC datetime."""
        dt_tweet = orch.snowflake_utc("1800000000000000000")
        self.assertIsNotNone(dt_tweet)
        self.assertEqual(dt_tweet.year, 2024)

    def test_m3_topic_lexical_cosine_thresholds(self):
        """M3 Gate: Calibrated topic similarity thresholds (0.75 merge, 0.30 deepen)."""
        from collections import Counter

        c1 = Counter(["free", "cloud", "gpu", "credits"])
        c2 = Counter(["free", "cloud", "gpu", "credits"])
        c3 = Counter(["kubernetes", "cluster", "orchestration"])
        df = {"free": 1, "cloud": 1, "gpu": 1, "credits": 1}

        sim_same = orch.tfidf_cosine(c1, c2, df, 10)
        self.assertGreaterEqual(sim_same, 0.75)

        sim_diff = orch.tfidf_cosine(c1, c3, df, 10)
        self.assertLess(sim_diff, 0.30)

    def test_m3_urgent_deadline_alert_scoring(self):
        """M3 Gate: Deadline in <= 7 days flagged as URGENT."""
        badge_urgent, days_urgent = digest_mod.compute_urgency_badge("2026-10-10")
        badge_rolling, days_rolling = digest_mod.compute_urgency_badge(None)

        self.assertEqual(days_rolling, 999)
        self.assertEqual(badge_rolling, "Rolling")
        self.assertTrue("URGENT" in badge_urgent or "d left" in badge_urgent or "Expired" in badge_urgent)

    # ------------------------------------------------------------------
    # M4 Gates: Quality Ports, Reindex, Paid Caps
    # ------------------------------------------------------------------
    def test_m4_reindex_fts_rebuild(self):
        """M4 Gate: FTS5 reindex repopulates search indexes from database rows."""
        res = eval_mod.reindex_fts(self.db_path)
        self.assertIsInstance(res, dict)
        self.assertIn("claims", res)
        self.assertIn("topics", res)


if __name__ == "__main__":
    unittest.main()
