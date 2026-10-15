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
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        cur.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
        self.assertEqual(str(cur.fetchone()[0]), "4")
        conn.close()

    def test_02_ask_engine_semantic_retrieval(self):
        """Verify snowball ask finds matching entities and formatted claims."""
        conn = sqlite3.connect(self.db_path)
        wiki_mod.compile_wiki(conn, self.wiki_dir)
        evidence = ask_mod.search_local_knowledge(conn, "Mozilla grants", wiki_dir=self.wiki_dir, limit=5)
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
            [sys.executable, str(ROOT / "snowball.py"), "status", "--db", str(self.db_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_status.returncode, 0, p_status.stderr)
        self.assertIn("Snowball status", p_status.stdout)

        # 2. Test ask command
        p_ask = subprocess.run(
            [sys.executable, str(ROOT / "snowball.py"), "ask", "grant", "--no-model", "--db", str(self.db_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_ask.returncode, 0, p_ask.stderr)

        # 3. Test visualize terminal
        p_vis = subprocess.run(
            [sys.executable, str(ROOT / "snowball.py"), "visualize", "--terminal", "--db", str(self.db_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_vis.returncode, 0)
        self.assertIn("TERMINAL DASHBOARD", p_vis.stdout)


    def test_07_supervisor_outcomes(self):
        """Verify supervisor machine-readable outcomes."""
        summary = self.orchestrator.run(1, None)
        self.assertEqual(summary["outcome"], "no_goal")
        self.assertEqual(summary["reason"], "no_goal")
        self.assertEqual(summary.get("external_calls", 0), 0)
        self.assertEqual(summary.get("planned_calls") or [], [])
        self.assertEqual(summary.get("topics") or [], [])


CANARY = "PRIVATE_CANARY_SYNTHETIC"
_POLICY = {
    "lifetime_limits": {
        "queued_nodes": 20,
        "visited_nodes": 20,
        "fetches": 20,
        "queries": 20,
        "model_tokens": 40000,
    },
    "max_depth": 2,
    "max_queue": 10,
    "per_domain_inflight": 1,
    "max_hypotheses_per_wave": 2,
    "exploration_share": 0.2,
    "usage_mode": "estimated",
}


class _LocalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._hit("GET")

    def do_POST(self):
        self._hit("POST")

    def do_PUT(self):
        self._hit("PUT")

    def do_CONNECT(self):
        self.server.hits.append(("CONNECT", self.path))
        self.send_error(403)

    def _hit(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.hits.append((method, self.path))
        body = b"Synthetic public notice: on-campus computing scholarship for 2027-fall."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _confirmed_answers(interview) -> dict:
    components = {
        key: key in {"tuition", "mandatory_fees", "living_amount", "living_duration"}
        for key in interview.COMPONENT_KEYS
    }
    raw = {
        "degree_level": "masters",
        "modality": "on_campus",
        "field_tags": ["computing"],
        "regions": ["CA"],
        "intake_windows": ["2027-fall"],
        "required_components": components,
        "grants_only": True,
        "accept_conditional_ra_ta": False,
        "accept_competitive_awards": False,
        "max_net_total_cost": {"amount": "0", "currency": "INR"},
        "max_upfront_cash": {"amount": "0", "currency": "INR"},
        "min_living_adequacy_rule": {
            "basis": "same_location_period",
            "count_housing_once": True,
            "count_insurance_once": True,
            "numeric_floor": None,
        },
        "max_work_hours_acceptable": 0,
        "allow_loans": False,
        "citizenship": "Synthetic",
        "prior_degree": {"level": "bachelors", "field": CANARY},
        "completion_min_candidates": 1,
        "execution_policy": _POLICY,
        "monitoring": {
            "permitted_work": ["recheck_official_sources"],
            "max_refreshes": 1,
            "interval_days": 30,
        },
        "consent_private_evaluation": False,
    }
    fields: dict = {}
    answers: dict = {}
    for _ in range(len(interview.QUESTIONS) + 1):
        question = interview.next_question(fields)
        if question is None:
            return answers
        if question.field_key not in raw:
            raise AssertionError(f"unexpected interview question {question.field_key}")
        parsed = interview.validate_answer(question, raw[question.field_key])
        answers[question.field_key] = {
            "value": raw[question.field_key],
            "confirm": True,
            "visibility": question.visibility_default,
        }
        fields[question.field_key] = {
            "value": parsed,
            "status": "known",
            "provenance": "user_confirmed",
            "visibility": question.visibility_default,
        }
    raise AssertionError("interview did not complete")


def _grant_limits(policy: dict) -> dict:
    life = {key: int(policy["lifetime_limits"][key]) for key in policy["lifetime_limits"]}
    return {
        "day": {
            "fetches": min(4, life["fetches"]),
            "queries": min(4, life["queries"]),
            "model_tokens": min(1000, life["model_tokens"]),
            "synthetic_pages": 2,
        },
        "lifetime": life,
        "max_depth": policy["max_depth"],
        "max_queue": policy["max_queue"],
        "per_domain_inflight": policy["per_domain_inflight"],
        "max_hypotheses_per_wave": policy["max_hypotheses_per_wave"],
        "exploration_share": policy["exploration_share"],
        "usage_mode": "estimated",
    }


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5)


def _wait_log(path: Path, needle: str, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            if path.is_file():
                last = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            last = f"<unreadable {exc}>"
        if needle in last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {needle!r} in {path}; have {last!r}")
        time.sleep(0.05)


class GoalSupervisorContracts(unittest.TestCase):
    """Throwaway schema-4 goals through the real interview gate, supervisor, and CLI."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="snowball-goal-sup-")
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.db = self.root / "goal.db"
        self.topics = self.root / "topics.yml"
        self.topics.write_text("families: {}\nseeds: []\n", encoding="utf-8")
        self.runtime = import_tool("snowball_goal_runtime", "tools/goal_runtime.py")
        self.orch = self.runtime._sibling("orchestrator")
        self.goal = self.runtime._sibling("goal")
        self.interview = self.runtime._sibling("interview")
        self.ledger = self.runtime._sibling("ledger")
        self.qualify = self.runtime._sibling("qualify")
        self.answers = _confirmed_answers(self.interview)
        self.hits: list = []
        self.attempts: list = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalHandler)
        self.server.hits = self.hits
        self.port = int(self.server.server_address[1])
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_server)
        personal = ROOT / "data" / "snowball.db"
        self.personal_existed = personal.exists()
        self.personal_stamp = personal.stat().st_mtime_ns if self.personal_existed else None
        self.repo_stop_existed = (ROOT / "STOP").exists()

    def tearDown(self):
        personal = ROOT / "data" / "snowball.db"
        if self.personal_existed:
            self.assertEqual(personal.stat().st_mtime_ns, self.personal_stamp)
        else:
            self.assertFalse(personal.exists())
        self.assertEqual((ROOT / "STOP").exists(), self.repo_stop_existed)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    @contextlib.contextmanager
    def _connected(self):
        store = self.orch.Store.connect_existing(self.db)
        try:
            yield store
        finally:
            store.db.close()

    @contextlib.contextmanager
    def _guard(self):
        self.hits.clear()
        self.attempts.clear()
        original = socket.create_connection

        def blocked(*args, **kwargs):
            self.attempts.append(args[:2])
            raise OSError("unauthorized external connection")

        socket.create_connection = blocked
        try:
            yield
        finally:
            socket.create_connection = original
        self.assertEqual(self.attempts, [], self.attempts)
        self.assertEqual(self.hits, [], self.hits)

    def _goal_bits(self):
        with self._connected() as store:
            row = store.one(
                "SELECT state, epoch, row_version, current_contract_version, current_strategy_version, stop_reason FROM goals"
            )
            return None if row is None else tuple(row)

    def _fetch_count(self) -> int:
        if not self.db.exists():
            return 0
        with self._connected() as store:
            return int(store.one("SELECT COUNT(*) AS n FROM fetches")["n"])

    def _build(self, stage: str) -> None:
        store = self.orch.Store.open(self.db)
        try:
            selected = self.goal.create_goal(store, "synthetic-funding", "Synthetic public funding", "cli")
            if stage == "draft":
                return
            for key, entry in self.answers.items():
                selected = self.goal.get_goal(store, selected.id)
                self.goal.record_answer(
                    store, selected.id, key, entry["value"], True, entry["visibility"], selected.row_version, "cli",
                )
            selected = self.goal.get_goal(store, selected.id)
            body = self.interview.build_contract(self.goal.fields(store, selected.id))
            contract = self.goal.propose_contract(
                store, selected.id, body, selected.current_contract_version, "cli",
            )
            if stage == "proposed":
                return
            selected = self.goal.get_goal(store, selected.id)
            selected = self.goal.approve_contract(
                store, selected.id, contract.version, contract.body_hash, selected.row_version, "cli", "approve-synthetic",
            )
            if stage == "approved":
                return
            selected = self.goal.get_goal(store, selected.id)
            limits = _grant_limits(contract.body["execution_policy"])
            self.goal.grant_execution(
                store, selected.id, contract.version, "research", limits,
                int(time.time()) + 7 * 86400, selected.row_version, "cli", "grant-synthetic",
            )
            if stage == "granted":
                return
            selected = self.goal.get_goal(store, selected.id)
            selected = self.goal.transition(
                store, selected.id, "start", "synthetic start", selected.row_version, "cli", "start-synthetic",
            )
            if stage == "running":
                return
            if stage == "paused":
                selected = self.goal.get_goal(store, selected.id)
                self.goal.transition(
                    store, selected.id, "pause", "synthetic pause", selected.row_version, "cli", "pause-synthetic",
                )
                paused = self.goal.finish_pause(store, selected.id)
                if paused.state != "paused":
                    raise AssertionError(paused.state)
                return
            if stage == "stopped":
                selected = self.goal.get_goal(store, selected.id)
                self.goal.transition(
                    store, selected.id, "stop", "synthetic stop", selected.row_version, "cli", "stop-synthetic",
                )
                return
            if stage == "research_ready":
                self._install_completion(store)
                selected = self.goal.get_goal(store, selected.id)
                status = self.qualify.completion_status(store, selected)
                if not status.ready:
                    raise AssertionError(status.detail)
                ready = self.goal.transition(
                    store, selected.id, "research_ready", "synthetic public completion",
                    selected.row_version, "worker", "ready-synthetic",
                )
                if ready.state != "research_ready":
                    raise AssertionError(ready.state)
                return
            raise AssertionError(stage)
        finally:
            store.db.close()

    def _install_completion(self, store) -> None:
        selected = self.goal.get_goal(store)
        contract = self.goal.load_contract(store, selected.id, selected.current_contract_version)
        critical = list(contract.body["completion"]["critical_fields"])
        now = int(time.time())
        quote = "Synthetic public scholarship notice for on-campus computing study."
        with store.tx() as db:
            db.execute("INSERT INTO domains(domain, status) VALUES ('synthetic.example', 'ok')")
            page = db.execute(
                """INSERT INTO pages(canon_url, domain, status, next_check_at, interval_sec, discovered_via, created_at)
                   VALUES ('http://127.0.0.1/synthetic-public-notice', 'synthetic.example', 'fetched', ?, 86400, 'seed', ?)""",
                (now + 86400, now),
            )
            program = "synthetic-public-program"
            db.execute(
                """INSERT INTO programs(key, name, provenance, first_seen_at, last_seen_at, field_tags_json, degree_level, modality)
                   VALUES (?, 'Synthetic Public Program', 'observed', ?, ?, '["computing"]', 'masters', 'on_campus')""",
                (program, now, now),
            )
            snapshot = db.execute(
                """INSERT INTO source_snapshots(page_id, content_hash, retrieved_at, text_path, visibility, provenance, applicability_json)
                   VALUES (?, 'synthetic-public-hash', ?, 'synthetic/public.txt', 'public', 'public_fetch', '{}')""",
                (page.lastrowid, now),
            )
            for field_key in critical:
                db.execute(
                    """INSERT INTO observations(
                           snapshot_id, page_id, program_key, field_key, value_json, subject_json, quote,
                           quote_start, quote_end, extractor_model, prompt_version, assurance, created_at,
                           goal_id, contract_version, strategy_version, epoch)
                       VALUES (?, ?, ?, ?, '{}', '{}', ?, 0, ?, 'synthetic-local', 'v0', 'reviewed_kept', ?, ?, ?, ?, ?)""",
                    (
                        snapshot.lastrowid, page.lastrowid, program, field_key, quote, len(quote), now,
                        selected.id, selected.current_contract_version, selected.current_strategy_version, selected.epoch,
                    ),
                )
            db.execute(
                """INSERT INTO funding_packages(
                       key, program_key, cycle_label, award_keys_json, components_json, compatibility_basis_ids_json,
                       cash_flow_json, basis_observation_ids_json, computed_at)
                   VALUES ('synthetic-package', ?, '2027-fall', '[]', '{}', '[]', '{}', '[]', ?)""",
                (program, now),
            )
            db.execute(
                """INSERT INTO constraint_evaluations(
                       goal_id, contract_version, program_key, constraint_key, package_key, status, basis_json,
                       reason, evaluated_at, epoch, stale_for_goal)
                   VALUES (?, ?, ?, 'degree_level', 'synthetic-package', 'met', '{}',
                           'synthetic reviewed public evidence', ?, ?, 0)""",
                (selected.id, selected.current_contract_version, program, now, selected.epoch),
            )
            db.execute(
                """INSERT INTO goal_programs(goal_id, contract_version, program_key, role, visibility)
                   VALUES (?, ?, ?, 'candidate', 'public')""",
                (selected.id, selected.current_contract_version, program),
            )

    def _binding(self, selected) -> dict:
        return {
            "goal_id": selected.id,
            "contract_version": selected.current_contract_version,
            "strategy_version": selected.current_strategy_version,
            "epoch": selected.epoch,
            "mode": "research",
        }

    def _reserve(self, operation_key: str, *, dispatch: bool) -> list:
        with self._connected() as store:
            selected = self.goal.get_goal(store)
            grant = self.goal.active_grant(store, selected.id)
            ids = self.ledger.reserve_batch(
                store, grant.id, operation_key, {"synthetic_pages": 1}, self._binding(selected),
            )
            if dispatch:
                self.assertTrue(self.ledger.mark_dispatched(store, operation_key, selected.id))
            return ids

    def drive(self, db_path: Path, *, interrupt: bool = False, stop_first: bool = False):
        stop = self.root / "supervisor.STOP"
        if stop.exists() and not stop_first:
            stop.unlink()
        if stop_first:
            stop.write_text("stop\n", encoding="utf-8")
        recorded: list[int] = []

        def sleeper(seconds):
            recorded.append(int(seconds))
            if interrupt:
                raise KeyboardInterrupt
            stop.write_text("stop\n", encoding="utf-8")

        original = self.runtime.time.sleep
        self.runtime.time.sleep = sleeper
        fetches_before = self._fetch_count() if Path(db_path).exists() else 0
        try:
            with self._guard():
                rc = self.runtime.supervise(
                    self.orch.Config(),
                    self.topics,
                    False,
                    db_path=db_path,
                    stop_file=stop,
                    max_topics=1,
                    interval_sec=60,
                )
        finally:
            self.runtime.time.sleep = original
        if Path(db_path).exists():
            self.assertEqual(self._fetch_count(), fetches_before)
        return rc, list(self.runtime.supervise.planned), recorded

    def test_missing_database_idles_without_creating_it(self):
        missing = self.root / "missing.db"
        rc, planned, slept = self.drive(missing)
        self.assertEqual(rc, 0)
        self.assertEqual(planned, [("no-runnable-goal", 300)])
        self.assertEqual(slept, [300])
        self.assertFalse(missing.exists())

    def test_paused_and_stopped_states_stay_put(self):
        self._build("paused")
        before = self._goal_bits()
        rc, planned, slept = self.drive(self.db)
        self.assertEqual(rc, 0)
        self.assertEqual(planned, [("goal-paused", 60)])
        self.assertEqual(slept, [60])
        self.assertEqual(self._goal_bits(), before)
        self.assertEqual(before[0], "paused")

        self.db.unlink()
        for suffix in ("-wal", "-shm"):
            extra = Path(str(self.db) + suffix)
            if extra.exists():
                extra.unlink()
        self._build("stopped")
        before = self._goal_bits()
        rc, planned, slept = self.drive(self.db)
        self.assertEqual(rc, 0)
        self.assertEqual(planned, [("no-runnable-goal", 300)])
        self.assertEqual(slept, [300])
        self.assertEqual(before[0], "stopped")
        self.assertEqual(self._goal_bits(), before)
        with self._connected() as store:
            self.assertIsNone(self.goal.active_grant(store, self.goal.get_goal(store).id))

    def test_keyboard_interrupt_does_not_change_paused_goal(self):
        self._build("paused")
        before = self._goal_bits()
        rc, planned, slept = self.drive(self.db, interrupt=True)
        self.assertEqual(rc, 0)
        self.assertEqual(planned, [("goal-paused", 60)])
        self.assertEqual(slept, [60])
        self.assertEqual(self._goal_bits(), before)

    def test_expired_grant_sleeps_without_extending_it(self):
        self._build("running")
        expired_at = int(time.time()) - 30
        with self._connected() as store:
            selected = self.goal.get_goal(store)
            before = (selected.state, selected.epoch, selected.row_version)
            store.db.execute(
                "UPDATE execution_grants SET expires_at=? WHERE goal_id=? AND state='active'",
                (expired_at, selected.id),
            )
        rc, planned, slept = self.drive(self.db)
        self.assertEqual(rc, 0)
        self.assertEqual(planned, [("expiry", 300)])
        self.assertEqual(slept, [300])
        with self._connected() as store:
            selected = self.goal.get_goal(store)
            grant = store.one("SELECT state, expires_at FROM execution_grants WHERE goal_id=?", (selected.id,))
            self.assertEqual((selected.state, selected.epoch, selected.row_version), before)
            self.assertEqual(selected.state, "running")
            self.assertEqual(grant["state"], "active")
            self.assertEqual(grant["expires_at"], expired_at)

    def test_overrun_exhausts_and_idles_without_resume(self):
        self._build("running")
        self._reserve("synthetic-overrun", dispatch=False)
        with self._connected() as store:
            selected = self.goal.get_goal(store)
            before = (selected.state, selected.epoch, selected.row_version)
            self.ledger.settle_operation(store, "synthetic-overrun", selected.id, {"synthetic_pages": 2})
            grant = store.one("SELECT state FROM execution_grants WHERE goal_id=? ORDER BY id DESC", (selected.id,))
            self.assertEqual(grant["state"], "exhausted")
        rc, planned, slept = self.drive(self.db)
        self.assertEqual(rc, 0)
        self.assertEqual(planned[0][0], "exhausted")
        self.assertGreaterEqual(planned[0][1], 60)
        self.assertLessEqual(planned[0][1], 1800)
        self.assertEqual(slept, [planned[0][1]])
        with self._connected() as store:
            selected = self.goal.get_goal(store)
            grant = store.one("SELECT state FROM execution_grants WHERE goal_id=? ORDER BY id DESC", (selected.id,))
            self.assertEqual((selected.state, selected.epoch, selected.row_version), before)
            self.assertEqual(grant["state"], "exhausted")
            self.assertIsNone(self.goal.active_grant(store, selected.id))

    def test_research_ready_idles_and_does_not_start_monitoring(self):
        self._build("research_ready")
        before = self._goal_bits()
        rc, planned, slept = self.drive(self.db)
        self.assertEqual(rc, 0)
        self.assertEqual(planned, [("research-ready", 1800)])
        self.assertEqual(slept, [1800])
        self.assertEqual(self._goal_bits(), before)
        self.assertEqual(before[0], "research_ready")
        with self._connected() as store:
            self.assertEqual(store.one("SELECT COUNT(*) AS n FROM goals WHERE state='monitoring'")["n"], 0)
            grant = self.goal.active_grant(store, self.goal.get_goal(store).id)
            self.assertEqual(grant.mode, "research")

    def test_outage_and_monitoring_delays_stay_bounded(self):
        now = int(time.time())
        store = self.orch.Store.in_memory()
        try:
            self.assertEqual(self.runtime._delay("outage", {}, {}, store, 60, now), min(900, 60 * 15))
            self.assertEqual(self.runtime._delay("monitoring", {}, {}, store, 60, now), 1800)
            with store.tx() as db:
                db.execute("INSERT INTO domains(domain, status) VALUES ('synthetic.example', 'ok')")
                db.execute(
                    """INSERT INTO pages(canon_url, domain, next_check_at, interval_sec, discovered_via, created_at)
                       VALUES ('http://127.0.0.1/synthetic-due', 'synthetic.example', ?, 86400, 'seed', ?)""",
                    (now + 120, now),
                )
            self.assertEqual(self.runtime._delay("monitoring", {}, {}, store, 60, now), 120)
            self.assertEqual(self.runtime._delay("monitoring", {}, {}, store, 60, now + 5000), 1800)
            self.assertEqual(self.runtime._delay("goal-paused", {}, {}, store, 60, now), 60)
            self.assertEqual(self.runtime._delay("research-ready", {}, {}, store, 60, now), 1800)
            self.assertEqual(self.runtime._delay("no-runnable-goal", {}, {}, store, 60, now), 300)
            self.assertEqual(self.runtime._delay("expiry", {}, {}, store, 60, now), 300)
        finally:
            store.db.close()

    def test_delegated_supervise_reclaims_on_caller_database(self):
        self._build("running")
        lost_ids = self._reserve("synthetic-dispatched", dispatch=True)
        self._reserve("synthetic-held", dispatch=False)
        before = self._goal_bits()
        stop = self.root / "delegated.STOP"
        stop.write_text("stop\n", encoding="utf-8")
        original_stop = self.orch.STOP_FILE
        original_sleep = self.runtime.time.sleep

        def boom(_seconds):
            raise AssertionError("delegated supervisor slept instead of honoring STOP")

        self.orch.STOP_FILE = stop
        self.runtime.time.sleep = boom
        try:
            with self._connected() as store:
                with self._guard():
                    runner = self.orch.SnowballOrchestrator(
                        self.orch.Config(), store, dry=False, topics_path=self.topics,
                    )
                    rc = runner.supervise(1, 60)
        finally:
            self.orch.STOP_FILE = original_stop
            self.runtime.time.sleep = original_sleep
        self.assertEqual(rc, 0)
        self.assertEqual(self.runtime.supervise.reclaim["lost_reservation_ids"], lost_ids)
        self.assertEqual(self.runtime.supervise.reclaim["released_operation_keys"], ["synthetic-held"])
        self.assertEqual(self._goal_bits(), before)
        with self._connected() as store:
            states = [row["state"] for row in store.all("SELECT state FROM resource_reservations ORDER BY id")]
            self.assertEqual(states, ["lost"])
            ops = {row["operation_key"]: row["state"] for row in store.all("SELECT operation_key, state FROM resource_operations")}
            self.assertEqual(ops["synthetic-dispatched"], "lost")
            self.assertEqual(ops["synthetic-held"], "cancelled")
            usage = store.one(
                "SELECT reserved, settled, lost FROM resource_ledger WHERE counter='synthetic_pages'"
            )
            self.assertEqual((usage["reserved"], usage["settled"], usage["lost"]), (0, 0, 1))

    def test_kill_restart_loses_dispatched_reservation_and_fences_occurrence(self):
        self._build("running")
        before = self._goal_bits()
        lost_ids = self._reserve("synthetic-dispatched", dispatch=True)
        with self._connected() as store:
            now = int(time.time())
            task_id = store.db.execute(
                """INSERT INTO tasks(kind, payload, priority, next_attempt_at, created_at)
                   VALUES ('fetch', '{}', 0.5, ?, ?)""",
                (now, now),
            ).lastrowid
        pc = sys.modules.get("snowball_process_control") or import_tool(
            "snowball_process_control", "tools/process_control.py",
        )
        script = self.root / "claim_once.py"
        script.write_text(
            "import importlib.util, sys, time, traceback\n"
            "from pathlib import Path\n"
            "root, db_path, task_raw = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])\n"
            "try:\n"
            "    path = root / 'tools' / 'orchestrator.py'\n"
            "    spec = importlib.util.spec_from_file_location('snowball_orchestrator', path)\n"
            "    module = importlib.util.module_from_spec(spec)\n"
            "    sys.modules['snowball_orchestrator'] = module\n"
            "    spec.loader.exec_module(module)\n"
            "    store = module.Store.connect_existing(Path(db_path))\n"
            "    runner = module.SnowballOrchestrator(module.Config(), store, dry=False)\n"
            "    seq = runner.claim_task(task_raw)\n"
            "    print(f'CLAIMED worker={runner.worker_id} seq={seq}', flush=True)\n"
            "    if seq is None:\n"
            "        raise SystemExit(2)\n"
            "    time.sleep(120)\n"
            "except Exception:\n"
            "    traceback.print_exc()\n"
            "    raise\n",
            encoding="utf-8",
        )
        log_path = self.root / "claim.log"
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
        self.addCleanup(_stop_process, unrelated)
        owned = pc.launch_owned_process(
            [sys.executable, "-u", str(script), str(ROOT), str(self.db), str(task_id)],
            cwd=str(ROOT),
            stdout_path=log_path,
        )
        self.addCleanup(pc.stop_owned_process_tree, owned)
        text = _wait_log(log_path, "CLAIMED ")
        self.assertIn("CLAIMED worker=", text)
        marker = text.split("CLAIMED ", 1)[1].splitlines()[0]
        worker = marker.split("worker=", 1)[1].split(" seq=", 1)[0]
        seq = int(marker.split("seq=", 1)[1].split()[0])
        pc.stop_owned_process_tree(owned)
        self.assertTrue(unrelated.poll() is None)
        rc, planned, _slept = self.drive(self.db, stop_first=True)
        self.assertEqual(rc, 0)
        self.assertEqual(planned, [])
        self.assertEqual(self.runtime.supervise.reclaim["lost_reservation_ids"], lost_ids)
        self.assertGreaterEqual(self.runtime.supervise.reclaim["reclaimed_tasks"], 1)
        self.assertEqual(self._goal_bits(), before)
        with self._connected() as store:
            usage = store.one("SELECT reserved, settled, lost FROM resource_ledger WHERE counter='synthetic_pages'")
            self.assertEqual((usage["reserved"], usage["settled"], usage["lost"]), (0, 0, 1))
            self.assertEqual(
                [row["state"] for row in store.all("SELECT state FROM resource_reservations")],
                ["lost"],
            )
            task = store.one("SELECT status, occurrence_seq, worker_id FROM tasks WHERE id=?", (task_id,))
            self.assertEqual(task["status"], "pending")
            self.assertNotEqual(task["occurrence_seq"], seq)
            self.assertIsNone(task["worker_id"])
            probe = self.orch.SnowballOrchestrator(self.orch.Config(), store, dry=False, topics_path=self.topics)
            probe.worker_id = worker
            self.assertFalse(probe.finish_task(task_id, seq, "done", {"stale": True}))
            stale = store.db.execute(
                "UPDATE tasks SET result='{}' WHERE id=? AND occurrence_seq=? AND status='claimed' AND worker_id=?",
                (task_id, seq, worker),
            )
            self.assertEqual(stale.rowcount, 0)
            self.assertEqual(store.one("SELECT COUNT(*) AS n FROM observations")["n"], 0)
        self.assertTrue(unrelated.poll() is None)

    def test_paused_process_stop_preserves_state_and_unrelated_process(self):
        self._build("paused")
        before = self._goal_bits()
        stop = self.root / "process.STOP"
        stop.write_text("stop\n", encoding="utf-8")
        script = self.root / "supervise_once.py"
        script.write_text(_SUPERVISE_ONCE, encoding="utf-8")
        pc = sys.modules.get("snowball_process_control") or import_tool(
            "snowball_process_control", "tools/process_control.py",
        )
        log_path = self.root / "supervise.log"
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
        self.addCleanup(_stop_process, unrelated)
        code, text = pc.run_owned_process_tree(
            [sys.executable, "-u", str(script), str(ROOT), str(self.db), str(stop)],
            cwd=str(ROOT),
            stdout_path=log_path,
            timeout=180,
        )
        self.assertEqual(code, 0, text)
        self.assertIn("SUPERVISE_RC=0", text)
        self.assertEqual(self._goal_bits(), before)
        self.assertEqual(before[0], "paused")
        self.assertTrue(unrelated.poll() is None)
        self.assertEqual(self._fetch_count(), 0)

    def _answers_file(self) -> Path:
        path = self.root / "answers.json"
        path.write_text(json.dumps(self.answers), encoding="utf-8")
        return path

    def _cli(self, *args, env=None, check=True):
        command = [sys.executable, str(ROOT / "snowball.py"), *args, "--db", str(self.db), "--json"]
        proc = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True, timeout=180, env=env,
        )
        blob = proc.stdout + "\n" + proc.stderr
        self.assertNotIn(CANARY, blob)
        if check:
            self.assertEqual(proc.returncode, 0, blob)
            return json.loads(proc.stdout)
        return proc

    def _proxy_env(self) -> dict:
        proxy = f"http://127.0.0.1:{self.port}"
        guard = self.root / "sitecustomize.py"
        guard.write_text(
            "import socket\n"
            "def _blocked(*args, **kwargs):\n"
            "    raise RuntimeError('unauthorized external connection')\n"
            "socket.create_connection = _blocked\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
        return env

    def test_c01_draft_interview_stays_draft(self):
        created = self._cli("goal", "new", "--slug", "synthetic-funding", "--title", "Synthetic public funding")
        self.assertEqual(created["state"], "draft")
        answered = self._cli("goal", "interview", "--answers-file", str(self._answers_file()))
        self.assertIsNone(answered["next_question"])
        self.assertEqual(len(answered["answered"]), len(self.answers))
        shown = self._cli("goal", "show")
        self.assertEqual(shown["state"], "draft")
        self.assertEqual(shown["contracts"], [])
        with self._connected() as store:
            version = store.one("SELECT value FROM schema_meta WHERE key='schema_version'")
            self.assertEqual(str(version["value"]), "4")

    def test_c02_incomplete_goal_is_rejected(self):
        self._cli("goal", "new", "--slug", "synthetic-funding", "--title", "Synthetic public funding")
        proc = self._cli("goal", "propose", "--from-fields", check=False)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("validation", proc.stdout)
        shown = self._cli("goal", "show")
        self.assertEqual(shown["state"], "draft")

    def test_c03_proposal_diff_and_approval_are_immutable(self):
        self._cli("goal", "new", "--slug", "synthetic-funding", "--title", "Synthetic public funding")
        self._cli("goal", "interview", "--answers-file", str(self._answers_file()))
        proposed = self._cli("goal", "propose", "--from-fields")
        self.assertTrue(proposed["body_hash"])
        self.assertEqual(proposed["profile_fields"]["prior_degree"], "[private]")
        diff = self._cli("goal", "diff", "--to", str(proposed["version"]))
        self.assertTrue(diff["changes"])
        for change in diff["changes"]:
            self.assertEqual(change["before"], "[private]")
            self.assertEqual(change["after"], "[private]")
        approved = self._cli(
            "goal", "approve", "--version", str(proposed["version"]), "--hash", proposed["body_hash"],
            "--idempotency-key", "approve-c03",
        )
        self.assertEqual(approved["state"], "approved")
        shown = self._cli("goal", "show")
        self.assertEqual(shown["contracts"][0]["body_hash"], proposed["body_hash"])
        proc = self._cli("goal", "interview", "--answers-file", str(self._answers_file()), check=False)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("illegal_transition", proc.stdout)
        again = self._cli("goal", "show")
        self.assertEqual(again["state"], "approved")
        self.assertEqual(again["contracts"][0]["body_hash"], proposed["body_hash"])

    def test_c04_hash_version_and_idempotency_conflicts(self):
        self._cli("goal", "new", "--slug", "synthetic-funding", "--title", "Synthetic public funding")
        self._cli("goal", "interview", "--answers-file", str(self._answers_file()))
        proposed = self._cli("goal", "propose", "--from-fields")
        wrong_hash = self._cli(
            "goal", "approve", "--version", str(proposed["version"]), "--hash", "0" * 64,
            "--idempotency-key", "bad-hash", check=False,
        )
        self.assertEqual(wrong_hash.returncode, 2, wrong_hash.stdout + wrong_hash.stderr)
        self.assertIn("validation", wrong_hash.stdout)
        self.assertEqual(self._cli("goal", "show")["state"], "contract_proposed")
        row_version = str(self._cli("goal", "show")["row_version"])
        wrong_version = self._cli(
            "goal", "approve", "--version", str(proposed["version"]), "--hash", proposed["body_hash"],
            "--expected-row-version", "1000000000", "--idempotency-key", "bad-version", check=False,
        )
        self.assertEqual(wrong_version.returncode, 3, wrong_version.stdout + wrong_version.stderr)
        self.assertIn("version_conflict", wrong_version.stdout)
        approved = self._cli(
            "goal", "approve", "--version", str(proposed["version"]), "--hash", proposed["body_hash"],
            "--expected-row-version", row_version, "--idempotency-key", "approve-once",
        )
        self.assertEqual(approved["state"], "approved")
        replay = self._cli(
            "goal", "approve", "--version", str(proposed["version"]), "--hash", proposed["body_hash"],
            "--expected-row-version", row_version, "--idempotency-key", "approve-once",
        )
        self.assertEqual(replay["state"], "approved")
        self.assertEqual(replay["row_version"], approved["row_version"])
        conflict = self._cli(
            "goal", "approve", "--version", str(proposed["version"]), "--hash", "1" * 64,
            "--expected-row-version", row_version, "--idempotency-key", "approve-once", check=False,
        )
        self.assertEqual(conflict.returncode, 3, conflict.stdout + conflict.stderr)
        self.assertIn("idempotency_conflict", conflict.stdout)
        shown = self._cli("goal", "show")
        self.assertEqual(shown["contracts"][0]["body_hash"], proposed["body_hash"])
        self.assertEqual(shown["state"], "approved")

    def _run_once(self, *, dry: bool):
        self.hits.clear()
        args = ["run-once", "--topics", str(self.topics), "--max-topics", "1"]
        if dry:
            args.append("--dry-run")
        proc = self._cli(*args, env=self._proxy_env(), check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(CANARY, proc.stdout + proc.stderr)
        self.assertEqual(self.hits, [])
        summary = json.loads(proc.stdout)
        self.assertEqual(summary.get("external_calls", 0), 0)
        self.assertEqual(self._fetch_count(), 0)
        return summary

    def test_c05_run_once_without_grant_or_start_makes_no_external_calls(self):
        self._cli("goal", "new", "--slug", "synthetic-funding", "--title", "Synthetic public funding")
        self._cli("goal", "interview", "--answers-file", str(self._answers_file()))
        proposed = self._cli("goal", "propose", "--from-fields")
        self._cli(
            "goal", "approve", "--version", str(proposed["version"]), "--hash", proposed["body_hash"],
            "--idempotency-key", "approve-c05",
        )
        before_grant = self._run_once(dry=False)
        self.assertEqual(before_grant["outcome"], "not_running")
        self.assertEqual(before_grant.get("topics") or [], [])
        self.assertEqual(before_grant.get("planned_calls") or [], [])
        limits = self.root / "limits.json"
        limits.write_text(json.dumps(_grant_limits(_POLICY)), encoding="utf-8")
        granted = self._cli(
            "goal", "grant", "--contract", str(proposed["version"]), "--mode", "research",
            "--limits", str(limits), "--expires", str(int(time.time()) + 7 * 86400),
            "--idempotency-key", "grant-c05",
        )
        self.assertEqual(granted["mode"], "research")
        self.assertEqual(granted["state"], "active")
        after_grant = self._run_once(dry=False)
        self.assertEqual(after_grant["outcome"], "not_running")
        self.assertEqual(self._cli("goal", "show")["state"], "approved")

    def test_c06_dry_run_after_start_makes_no_external_calls(self):
        self._cli("goal", "new", "--slug", "synthetic-funding", "--title", "Synthetic public funding")
        self._cli("goal", "interview", "--answers-file", str(self._answers_file()))
        proposed = self._cli("goal", "propose", "--from-fields")
        self._cli(
            "goal", "approve", "--version", str(proposed["version"]), "--hash", proposed["body_hash"],
            "--idempotency-key", "approve-c06",
        )
        limits = self.root / "limits.json"
        limits.write_text(json.dumps(_grant_limits(_POLICY)), encoding="utf-8")
        self._cli(
            "goal", "grant", "--contract", str(proposed["version"]), "--mode", "research",
            "--limits", str(limits), "--expires", str(int(time.time()) + 7 * 86400),
            "--idempotency-key", "grant-c06",
        )
        started = self._cli("goal", "start", "--reason", "synthetic start", "--idempotency-key", "start-c06")
        self.assertEqual(started["state"], "running")
        summary = self._run_once(dry=True)
        self.assertEqual(summary["outcome"], "dry-run")
        self.assertEqual(summary.get("external_calls", 0), 0)
        self.assertEqual(self._cli("goal", "show")["state"], "running")
        with self._connected() as store:
            self.assertEqual(store.one("SELECT COUNT(*) AS n FROM observations")["n"], 0)


_SUPERVISE_ONCE = """\
import importlib.util
import sys
from pathlib import Path

root = Path(sys.argv[1])
db_path = sys.argv[2]
stop_path = sys.argv[3]
runtime_path = root / "tools" / "goal_runtime.py"
spec = importlib.util.spec_from_file_location("snowball_goal_runtime", runtime_path)
module = importlib.util.module_from_spec(spec)
sys.modules["snowball_goal_runtime"] = module
spec.loader.exec_module(module)
orchestrator = module._sibling("orchestrator")
code = module.supervise(
    orchestrator.Config(),
    None,
    False,
    db_path=db_path,
    stop_file=stop_path,
    max_topics=1,
)
print("SUPERVISE_RC=" + str(code), flush=True)
"""


if __name__ == "__main__":
    unittest.main()

