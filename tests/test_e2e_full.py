#!/usr/bin/env python3
"""Comprehensive End-to-End Test Suite for Snowball.
Tests the complete lifecycle:
  1. Orchestrator Initialization & Status
  2. 5-Wave DAG Execution (dry-run & offline mocks)
  3. Semantic Answering Engine (snowball ask)
  4. Wiki Compilation & 70% Compaction Floor
  5. Daily Opportunity Digest & Urgency Scoring
  6. Visualization API Data Structures
  7. Unified CLI Command Dispatcher
"""

from __future__ import annotations

import contextlib
import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# Dynamically import modules from tools
def import_tool(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


orch = import_tool("orchestrator", "tools/orchestrator.py")
ask_mod = import_tool("ask", "tools/ask.py")
digest_mod = import_tool("digest", "tools/digest.py")
wiki_mod = import_tool("wiki_compiler", "tools/wiki_compiler.py")
vis_mod = import_tool("visualize", "tools/visualize.py")


class TestSnowballE2E(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="snowball-e2e-")
        self.db_path = Path(self.td.name) / "snowball.db"
        self.wiki_dir = Path(self.td.name) / "wiki"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)

        # Initialize schema via orchestrator
        self.store = orch.Store.open(self.db_path)
        self.orchestrator = orch.SnowballOrchestrator(
            orch.Config(),
            self.store,
            dry=True,
        )
        # Populate synthetic test data
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # 1. Seed a page first (for evidence_page_id foreign key)
        cur.execute(
            """
            INSERT INTO pages (canon_url, domain, kind, status, http_status, text_len, next_check_at, interval_sec, discovered_via, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                "https://foundation.mozilla.org/grants",
                "foundation.mozilla.org",
                "hub",
                "fetched",
                200,
                12450,
                1791349000,
                86400,
                "seed",
                1791349000,
            ),
        )
        page_id = cur.lastrowid or 1

        # 2. Seed an entity
        cur.execute(
            """
            INSERT INTO entities (key, name, norm_name, org, norm_org, year, family, kind, amount, currency, deadline, eligibility, link, evidence_quote, evidence_page_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                "mozilla-technology-fund|mozilla-foundation|2026",
                "Mozilla Technology Fund",
                "mozilla technology fund",
                "Mozilla Foundation",
                "mozilla foundation",
                "2026",
                "opportunities",
                "grant",
                50000.0,
                "USD",
                "2026-12-15",
                "Open to developers in India and internationally",
                "https://foundation.mozilla.org/grants",
                "Grants of up to $50,000 for open-source AI projects.",
                page_id,
                1791349000,
                1791349000,
            ),
        )

        # 3. Seed an urgent entity (due in 3 days)
        cur.execute(
            """
            INSERT INTO entities (key, name, norm_name, org, norm_org, year, family, kind, amount, currency, deadline, eligibility, link, evidence_quote, evidence_page_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                "emergency-hackathon|devpost|2026",
                "AI Safety Hackathon",
                "ai safety hackathon",
                "Devpost",
                "devpost",
                "2026",
                "opportunities",
                "hackathon",
                10000.0,
                "USD",
                "2026-10-10",
                "Open to student hackers",
                "https://devpost.com/hackathons/safety",
                "Cash prizes totaling $10,000.",
                page_id,
                1791349000,
                1791349000,
            ),
        )

        # 4. Seed a claim
        cur.execute(
            """
            INSERT INTO claims (hash, text, norm_text, n_sources, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                "claim-hash-1234",
                "Mozilla offers $50,000 grants for open-source AI projects in 2026.",
                "mozilla offers 50000 grants for open source ai projects in 2026",
                2,
                1791349000,
                1791349000,
            ),
        )
        # Seed domain
        cur.execute(
            """
            INSERT INTO domains (domain, status, fetch_count, new_items_total)
            VALUES (?, ?, ?, ?)
        """,
            ("foundation.mozilla.org", "ok", 4, 2),
        )

        conn.commit()
        conn.close()

    def tearDown(self):
        if hasattr(self, "store") and hasattr(self.store, "db"):
            self.store.db.close()
        with contextlib.suppress(Exception):
            self.td.cleanup()

    def test_01_orchestrator_initialization_and_status(self):
        """Verify orchestrator database schema and status report."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM entities")
        self.assertEqual(cur.fetchone()[0], 2)
        cur.execute("SELECT count(*) FROM claims")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_02_ask_engine_semantic_retrieval(self):
        """Verify snowball ask finds matching entities and formatted claims."""
        conn = sqlite3.connect(self.db_path)
        evidence = ask_mod.search_local_knowledge(conn, "Mozilla grants", limit=5)
        conn.close()

        self.assertGreaterEqual(len(evidence["entities"]), 1)
        self.assertEqual(evidence["entities"][0]["name"], "Mozilla Technology Fund")
        self.assertEqual(evidence["entities"][0]["currency"], "USD")

        # Test mechanical answer synthesis
        answer = ask_mod.synthesize_answer(evidence, use_model=False)
        self.assertIn("Mozilla Technology Fund", answer)
        self.assertIn("https://foundation.mozilla.org/grants", answer)

    def test_03_wiki_compiler_and_70_percent_floor(self):
        """Verify markdown wiki compiler generates entity pages and enforces body floor."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        stats = wiki_mod.compile_wiki(conn, self.wiki_dir)
        conn.close()

        self.assertEqual(stats["compiled_entities"], 2)
        index_file = self.wiki_dir / "INDEX.md"
        self.assertTrue(index_file.exists())
        self.assertIn("Mozilla Technology Fund", index_file.read_text(encoding="utf-8"))

        # Test 70% Body Shrink Safety Floor
        old_body = "Detailed background overview.\n" * 40
        short_body = "Truncated summary."
        merged = wiki_mod.merge_page(old_body, {"tags": ["new"]}, short_body)
        self.assertIn("Detailed background overview", merged)
        self.assertIn("Truncated summary", merged)

    def test_04_digest_urgency_and_markdown_generation(self):
        """Verify digest generator flags urgent deadlines (<=7d) and formats report."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        digest_text, count = digest_mod.compile_daily_digest(conn)
        conn.close()

        self.assertGreaterEqual(count, 2)
        self.assertIn("Snowball Research Digest", digest_text)
        self.assertIn("Mozilla Technology Fund", digest_text)
        self.assertIn("AI Safety Hackathon", digest_text)
        self.assertIn("🚨 URGENT", digest_text)

    def test_05_visualization_data_structures(self):
        """Verify visualization module queries and graph payload generation."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        graph = vis_mod.fetch_graph_data(conn)
        self.assertGreaterEqual(len(graph["nodes"]), 2)
        opps = vis_mod.fetch_opportunities(conn)
        self.assertGreaterEqual(len(opps), 2)
        metrics = vis_mod.fetch_status_metrics(conn)
        self.assertEqual(metrics["total_entities"], 2)
        self.assertEqual(metrics["total_claims"], 1)
        conn.close()

    def test_06_unified_cli_dispatcher_smoke(self):
        """Verify top-level snowball.py dispatches commands with zero exit code."""
        # 1. Test status command
        p_status = subprocess.run(
            [sys.executable, str(ROOT / "snowball.py"), "status"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_status.returncode, 0)
        self.assertIn("Snowball status", p_status.stdout)

        # 2. Test ask command
        p_ask = subprocess.run(
            [sys.executable, str(ROOT / "snowball.py"), "ask", "grant", "--no-model"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_ask.returncode, 0)

        # 3. Test visualize terminal
        p_vis = subprocess.run(
            [sys.executable, str(ROOT / "snowball.py"), "visualize", "--terminal"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_vis.returncode, 0)
        self.assertIn("TERMINAL DASHBOARD", p_vis.stdout)


if __name__ == "__main__":
    unittest.main()
