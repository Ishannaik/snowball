"""Offline tests for tools/orchestrator.py. No network, no model: engines and the router are stubbed.

python3 -m unittest tests/test_orchestrator.py -v
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("orchestrator", ROOT / "tools/orchestrator.py")
o = importlib.util.module_from_spec(_spec)
sys.modules["orchestrator"] = o  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(o)

_ft_spec = importlib.util.spec_from_file_location("fetch_types", ROOT / "tools/fetch_types.py")
ft = importlib.util.module_from_spec(_ft_spec)
sys.modules["fetch_types"] = ft
_ft_spec.loader.exec_module(ft)
FetchResult = ft.FetchResult
SearchHit = ft.SearchHit

PAGE = (
    "Mozilla Technology Fund 2026 is open. The Mozilla Foundation offers grants of up to $50,000 for "
    "open-source projects that make AI more accountable. Applications close on 2026-12-15. "
    "Individuals and organisations from any country, including India, may apply. "
) * 3


class Primitives(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(
            o.canonical_url("http://www.Example.com/a/index.html?utm_source=x&b=2&a=1#frag"),
            "https://example.com/a?a=1&b=2",
        )
        self.assertEqual(
            o.canonical_url("https://old.reddit.com/r/x/comments/abc123/title/"), "https://reddit.com/comments/abc123"
        )
        self.assertEqual(o.canonical_url("https://github.com/Foo/Bar/tree/main"), "https://github.com/foo/bar")
        self.assertIsNone(o.canonical_url("ftp://x.com/a"))

    def test_query_and_slug(self):
        self.assertEqual(o.norm_query("grants India"), o.norm_query("india  GRANTS!"))
        s = o.slug("Mozilla Technology Fund — 2026!")
        self.assertEqual(o.slug(s), s)
        self.assertEqual(
            o.entity_key("Mozilla Technology Fund", "Mozilla Foundation", "2026-12-15"),
            "mozilla-technology-fund|mozilla-foundation|2026",
        )

    def test_similarity_and_quote(self):
        self.assertGreaterEqual(
            o.hybrid_similarity("The fund gives $50,000 grants", "The fund gives $50,000 grants."), 0.70
        )
        ok, s, e = o.quote_ok("grants of up to $50,000 for open-source projects", PAGE)
        self.assertTrue(ok)
        self.assertFalse(o.quote_ok("grants of up to $90,000 for closed projects only", PAGE)[0])

    def test_snowflake(self):
        # 1800000000000000000 decodes to 2024-07-10; garbage ids do not decode
        self.assertEqual(o.snowflake_utc("1800000000000000000").year, 2024)
        self.assertIsNone(o.snowflake_utc("12"))
        self.assertIsNone(o.snowflake_utc("abc"))
        self.assertEqual(
            o.validate_x_posts([{"url": "https://x.com/a/status/1800000000000000000", "text": "old"}], 30), []
        )
        self.assertFalse(o.status_id_in_window("https://x.com/a/status/1800000000000000000", 30))
        fresh_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        fresh = str((fresh_ms - o.SNOWFLAKE_EPOCH_MS) << 22)
        fresh_url = f"https://x.com/a/status/{fresh}"
        self.assertTrue(o.status_id_in_window(fresh_url, 30))
        self.assertEqual(o.validate_x_posts([{"url": fresh_url, "text": "model wrote this post"}], 30), [])

    def test_ssrf(self):
        for host, port in (("localhost", 443), ("127.0.0.1", 443), ("example.com", 8080)):
            with self.assertRaises(o.SSRFBlocked):
                o.resolve_and_pin(host, port)

    def test_substring_guard_drops_hallucination(self):
        data = {
            "rows": [
                {
                    "name": "Mozilla Technology Fund",
                    "kind": "grant",
                    "amount": 50000,
                    "deadline": "2026-12-15",
                    "evidence_quote": "The Mozilla Foundation offers grants of up to $50,000 for open-source projects",
                },
                {
                    "name": "Imaginary Fellowship",
                    "kind": "fellowship",
                    "evidence_quote": "The Imaginary Fellowship pays $1M to anyone who asks nicely, no questions asked.",
                },
            ],
            "names": ["Mozilla Foundation", "Made Up Org"],
            "claims": [],
        }
        g = o.substring_guard(data, PAGE)
        self.assertEqual([r["name"] for r in g["rows"]], ["Mozilla Technology Fund"])
        self.assertEqual(g["names"], ["Mozilla Foundation"])
        self.assertEqual(g["dropped"], 1)

    def test_mechanical_check(self):
        row = {"amount": 50000.0, "deadline": "2026-12-15"}
        self.assertTrue(o.mechanical_check(row, PAGE)[0])
        self.assertFalse(o.mechanical_check({"amount": 75000.0}, PAGE)[0])
        self.assertTrue(o.mechanical_check({"amount": 50000.0}, "grants up to $50k each")[0])


class _Reply:
    def __init__(self, text: str, requested: str, reported: str, transport: str = "api") -> None:
        self.text = text
        self.tokens_in = 12
        self.tokens_out = 8
        self.model = reported
        self.requested_model = requested
        self.reported_model = reported
        self.transport = transport
        self.usage_kind = "estimated" if transport in {"omp", "ssh-omp", "agy"} else "unknown"


class _PlanEngine:
    def available(self):
        return True, "ok"

    def plan(self, *_args, **_kwargs):
        return _Reply('{"hypotheses":[]}', "plan-test", "plan-test")


_SOURCE = (
    "Northwind Masters publishes a public computing programme for 2026-fall. "
    "Tuition is 12000 USD total for 2026-fall."
)
_ENGINE_BINDING = {"goal_id", "contract_version", "strategy_version", "epoch", "task_id"}


class _ExtractEngine:
    def available(self):
        return True, "ok"

    def extract(self, frame, program, field_keys, source, binding, operation_key, max_out):
        o._sibling("prompts").compile_extract_fields(frame, program, field_keys, source)
        if set(binding) != _ENGINE_BINDING or not isinstance(operation_key, str) or type(max_out) is not int:
            raise TypeError("binding")
        text = source.text
        name = "Northwind Masters"
        name_at = text.index(name)
        tuition = "Tuition is 12000 USD total for 2026-fall."
        tuition_at = text.index(tuition)
        payload = {
            "program": {"name": name},
            "observations": [
                {
                    "field_key": "program_name",
                    "value": name,
                    "quote": name,
                    "quote_start": name_at,
                    "quote_end": name_at + len(name),
                },
                {
                    "field_key": "tuition",
                    "value": {"amount": "12000", "currency": "USD"},
                    "currency": "USD",
                    "period": "total",
                    "quote": tuition,
                    "quote_start": tuition_at,
                    "quote_end": tuition_at + len(tuition),
                    "subject": {"cycle_label": "2026-fall"},
                },
            ],
        }
        return _Reply(json.dumps(payload), "gemini-3.8-flash", "gemini-3.8-flash")


class _ReviewEngine:
    def available(self):
        return True, "ok"

    def review(self, frame, observations, source, binding, operation_key, max_out):
        o._sibling("prompts").compile_review_observations(frame, observations, source)
        if set(binding) != _ENGINE_BINDING or not isinstance(operation_key, str) or type(max_out) is not int:
            raise TypeError("binding")
        verdicts = [
            {"id": item["id"], "verdict": "keep", "reason": "the quote is in the public source"}
            for item in observations
        ]
        return _Reply(json.dumps({"verdicts": verdicts}), "review-test", "review-test")


class GoalWave(unittest.TestCase):
    LIMITS = {
        "day": {"fetches": 5, "queries": 5, "model_tokens": 100000},
        "lifetime": {
            "fetches": 20, "queries": 20, "model_tokens": 400000,
            "queued_nodes": 20, "visited_nodes": 20,
        },
        "max_depth": 2, "max_queue": 10, "per_domain_inflight": 2,
        "max_hypotheses_per_wave": 2, "exploration_share": 0, "usage_mode": "estimated",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "snowball.db"
        self.cfg = o.load_config()
        self.cfg.plan_tokens_per_wave = 100
        self.cfg.review_budget = (1000, 100)
        self.store = o.Store.open(self.db)
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO families(name, recheck_days, weight, relevance_floor, focus) "
                "VALUES ('opportunities', 14, 1.0, 0.0, 'public programmes')"
            )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def _policy(self):
        return {
            "max_depth": 2, "max_queue": 10, "per_domain_inflight": 2,
            "max_hypotheses_per_wave": 2, "exploration_share": 0,
            "lifetime_limits": self.LIMITS["lifetime"],
        }

    def _running(self, slug="northwind"):
        goal = o._sibling("goal")
        interview = o._sibling("interview")
        selected = goal.create_goal(self.store, slug, "Synthetic public programmes", "fixture")
        components = {key: False for key in interview.COMPONENT_KEYS}
        policy = {
            "lifetime_limits": {
                "queued_nodes": 20, "visited_nodes": 20, "fetches": 20,
                "queries": 20, "model_tokens": 400000,
            },
            "max_depth": 2, "max_queue": 10, "per_domain_inflight": 2,
            "max_hypotheses_per_wave": 2, "exploration_share": 0, "usage_mode": "estimated",
        }
        monitoring = {
            "permitted_work": ["recheck_official_sources"],
            "max_refreshes": 1, "interval_days": 30,
        }
        goal.apply_answers(
            self.store, selected.id,
            [
                ("degree_level", "masters", True, "public_ok"),
                ("modality", "on_campus", True, "public_ok"),
                ("field_tags", ["computing"], True, "public_ok"),
                ("regions", [], True, "public_ok"),
                ("intake_windows", ["2026-fall"], True, "public_ok"),
                ("scope_broad_acknowledged", True, True, "public_ok"),
                ("required_components", components, True, "public_ok"),
                ("grants_only", True, True, "public_ok"),
                ("completion_min_candidates", 1, True, "public_ok"),
                ("execution_policy", policy, True, "public_ok"),
                ("monitoring", monitoring, True, "public_ok"),
                ("consent_private_evaluation", False, True, "private"),
            ],
            selected.row_version, "fixture",
        )
        selected = goal.get_goal(self.store, selected.id)
        snapshot = {
            key: {
                "value": item.value, "status": item.status,
                "provenance": item.provenance, "visibility": item.visibility,
            }
            for key, item in goal.fields(self.store, selected.id).items()
        }
        proposed = goal.propose_contract(
            self.store, selected.id, interview.build_contract(snapshot), None, "fixture",
            expected_row_version=selected.row_version,
        )
        selected = goal.get_goal(self.store, selected.id)
        goal.approve_contract(
            self.store, selected.id, proposed.version, proposed.body_hash,
            selected.row_version, "fixture", slug + "-approve",
        )
        selected = goal.get_goal(self.store, selected.id)
        goal.grant_execution(
            self.store, selected.id, proposed.version, "research", self.LIMITS, o.now() + 3600,
            selected.row_version, "fixture", slug + "-grant",
        )
        selected = goal.get_goal(self.store, selected.id)
        return goal.transition(self.store, selected.id, "start", "", selected.row_version, "fixture", slug + "-start")

    def test_extract_rejects_raw_source_text(self):
        with self.assertRaises(TypeError):
            o.extract_page_entities("raw page text", "https://example.com/programme")
        interview = o._sibling("interview")
        prompts = o._sibling("prompts")
        text = _SOURCE
        source = prompts.PublicSourceSnapshot(
            id=1, url="https://example.com/northwind", title="Northwind Masters", text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source_cycle_label="2026-fall", applicability={"cycle_label": "2026-fall"},
        )
        frame = interview.PublicFrame(
            domain=interview.DOMAIN,
            public_objective="masters_funding: masters on_campus programs in computing; scope explicitly acknowledged broad scope; intakes 2026-fall",
            public_criteria={
                "degree_level": "masters",
                "modality": "on_campus",
                "required_components": {key: False for key in interview.COMPONENT_KEYS},
                "grants_only": True,
            },
            field_tags=["computing"],
            regions=[],
            intake_windows=["2026-fall"],
            required_source_fields=["tuition"],
        )
        binding = {"goal_id": 1, "contract_version": 1, "strategy_version": 1, "epoch": 1, "task_id": 1}

        class _Refuse:
            def extract(self, *_args, **_kwargs):
                raise AssertionError("engine must not run")

        with self.assertRaises(TypeError):
            o.extract_page_entities(
                frame, {"name": "Northwind Masters", "profile_snapshot_hash": "abc"},
                ["tuition"], source, binding, "extract-op", _Refuse(), 100,
            )
        with self.assertRaises(TypeError):
            o.extract_page_entities(
                frame, {"name": "Northwind Masters"}, ["tuition"], "raw page text",
                binding, "extract-op", _Refuse(), 100,
            )

    def test_absent_paused_ready_and_expired_make_no_external_calls(self):
        idle = o.SnowballOrchestrator(self.cfg, self.store)
        absent = idle.run(1, "forced-topic")
        self.assertEqual(absent["reason"], "no_goal")
        self.assertEqual(absent["external_calls"], 0)
        self.assertEqual(self.store.one("SELECT count(*) n FROM fetches")["n"], 0)

        running = self._running()
        goal = o._sibling("goal")
        paused = goal.transition(
            self.store, running.id, "pause", "", running.row_version, "fixture", "pause-northwind",
        )
        held = o.SnowballOrchestrator(self.cfg, self.store).run(1, "forced-topic")
        self.assertEqual(held["reason"], "goal_paused")
        self.assertEqual(held["external_calls"], 0)

        with self.store.tx() as db:
            db.execute("UPDATE goals SET state='research_ready' WHERE id=?", (paused.id,))
        ready = o.SnowballOrchestrator(self.cfg, self.store).run(1, None)
        self.assertEqual(ready["reason"], "research_ready")
        self.assertEqual(ready["external_calls"], 0)

        with self.store.tx() as db:
            db.execute("UPDATE goals SET state='running' WHERE id=?", (paused.id,))
            db.execute(
                "UPDATE execution_grants SET expires_at=? WHERE goal_id=? AND state='active'",
                (o.now() - 10, paused.id),
            )
        expired = o.SnowballOrchestrator(self.cfg, self.store).run(1, "forced-topic")
        self.assertEqual(expired["reason"], "grant_expired")
        self.assertEqual(expired["external_calls"], 0)
        self.assertEqual(self.store.one("SELECT count(*) n FROM fetches")["n"], 0)

    def test_local_source_fact_is_reviewed_kept(self):
        running = self._running("local-fact")
        text = _SOURCE
        with self.store.tx() as db:
            _verdict, topic_id = o.SnowballOrchestrator(self.cfg, self.store).locks.topic_lock(
                db, "Northwind", "public programmes", "opportunities", "seed", key="northwind",
            )
            page_id = o.SnowballOrchestrator(self.cfg, self.store).locks.enqueue_url(
                db, "https://example.com/northwind", topic_id, "seed", 14,
            )
            self.assertIsNotNone(page_id)
            db.execute(
                """INSERT INTO tasks(
                       kind, payload, priority, status, next_attempt_at, created_at,
                       goal_id, contract_version, strategy_version, epoch, grant_id)
                   VALUES ('fetch', ?, 1, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    json.dumps({"page_id": page_id, "url": "https://example.com/northwind"}),
                    o.now(), o.now(), running.id, running.current_contract_version,
                    running.current_strategy_version, running.epoch,
                    o._sibling("goal").active_grant(self.store, running.id).id,
                ),
            )
        orch = o.SnowballOrchestrator(self.cfg, self.store)
        extract, review = _ExtractEngine(), _ReviewEngine()
        orch.extract_engine = lambda: extract
        orch.review_engine = lambda: review
        orch.plan_engine = lambda: _PlanEngine()
        orch.router.fetch = lambda *_args, **_kwargs: FetchResult(
            "https://example.com/northwind", "direct", "ok", 200, "Northwind Masters", text,
        )
        orch.router.search = lambda *_args, **_kwargs: []
        result = orch.run(1, None)
        self.assertNotEqual(result["reason"], "research_ready")
        self.assertGreater(result["external_calls"], 0)
        kept = {
            row["field_key"]: row
            for row in self.store.all(
                """SELECT field_key, assurance, quote, goal_id, contract_version, strategy_version, epoch
                     FROM observations"""
            )
        }
        self.assertEqual(kept["program_name"]["assurance"], "reviewed_kept")
        self.assertEqual(kept["program_name"]["quote"], "Northwind Masters")
        self.assertEqual(kept["tuition"]["assurance"], "reviewed_kept")
        self.assertIn("12000", kept["tuition"]["quote"])
        self.assertIn("USD", kept["tuition"]["quote"])
        self.assertIn("total", kept["tuition"]["quote"])
        self.assertIn("2026-fall", kept["tuition"]["quote"])
        for row in kept.values():
            self.assertEqual(row["goal_id"], running.id)
            self.assertEqual(row["contract_version"], running.current_contract_version)
            self.assertEqual(row["strategy_version"], running.current_strategy_version)
            self.assertEqual(row["epoch"], running.epoch)
        snap = self.store.one(
            """SELECT content_hash, visibility, provenance, source_cycle_label
                 FROM source_snapshots"""
        )
        self.assertEqual(snap["content_hash"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(snap["visibility"], "public")
        self.assertEqual(snap["provenance"], "public_fetch")
        self.assertEqual(snap["source_cycle_label"], "2026-fall")
        prompts = o._sibling("prompts")
        calls = {
            row["role"]: row
            for row in self.store.all(
                """SELECT role, requested_model, reported_model, usage_kind, prompt_version
                     FROM model_calls"""
            )
        }
        self.assertEqual(calls["extract"]["requested_model"], "gemini-3.8-flash")
        self.assertEqual(calls["extract"]["reported_model"], "gemini-3.8-flash")
        self.assertEqual(calls["extract"]["usage_kind"], "unknown")
        self.assertEqual(calls["extract"]["prompt_version"], prompts.PROMPTS["extract_fields"]["version"])
        self.assertNotEqual(calls["extract"]["prompt_version"], "public-source-1")
        self.assertEqual(calls["review"]["requested_model"], "review-test")
        self.assertEqual(calls["review"]["reported_model"], "review-test")
        self.assertEqual(calls["review"]["usage_kind"], "unknown")
        self.assertEqual(calls["review"]["prompt_version"], prompts.PROMPTS["review_observations"]["version"])
        self.assertIsNone(calls.get("private_eval"))
        self.assertEqual(self.store.one("SELECT count(*) n FROM model_calls WHERE role='private_eval'")["n"], 0)
        self.assertGreater(self.store.one("SELECT count(*) n FROM constraint_evaluations")["n"], 0)
        self.assertIn(
            "unknown",
            {row["status"] for row in self.store.all("SELECT status FROM constraint_evaluations")},
        )
        completion = o._sibling("qualify").completion_status(self.store, running)
        self.assertFalse(completion.ready)
        fresh = o._sibling("goal").get_goal(self.store, running.id)
        self.assertNotEqual(fresh.state, "research_ready")
        private = {
            row["key"] for row in self.store.all(
                """SELECT key FROM profile_fields
                    WHERE key IN ('max_upfront_cash', 'citizenship', 'prior_degree')"""
            )
        }
        self.assertEqual(private, set())
        consent = self.store.one(
            "SELECT value_json FROM profile_fields WHERE key='consent_private_evaluation'"
        )
        self.assertIn("false", consent["value_json"])

    def test_status_and_dry_run_do_not_write(self):
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        status = o.SnowballOrchestrator(self.cfg, self.store).status()
        self.assertIn("domain_yield", status)
        self.assertIn("goal", status)
        self.assertNotIn("gemini_api_key", json.dumps(status))
        dry = o.SnowballOrchestrator(self.cfg, self.store, dry=True)
        waved = dry.run(2, "forced-topic")
        self.assertEqual(waved["external_calls"], 0)
        self.assertTrue(waved["dry_run"])
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)


def _link_dir(src: Path, dst: Path) -> None:
    src.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=True)
    except OSError:
        subprocess.check_call(["cmd", "/c", "mklink", "/J", str(dst), str(src)])


def _drop_reparse(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        path.unlink()
    except OSError:
        os.rmdir(path)


_PATH_RESOLVE = Path.resolve


def _refuse_db_resolve(self, *args, **kwargs):
    if self.name in {"fresh.db", "leaf.db", "secret.db", "link"} or "link" in self.parts:
        raise AssertionError("resolve")
    return _PATH_RESOLVE(self, *args, **kwargs)


class StorePathGuard(unittest.TestCase):
    def test_reparse_is_refused_before_resolve_or_sqlite(self):
        migration = o._sibling("migration")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real"
            real.mkdir()
            sentinel = real / "secret.db"
            sentinel.write_bytes(b"do-not-touch")
            link = root / "link"
            leaf = root / "leaf.db"
            try:
                _link_dir(real, link)
                self.assertTrue(migration.is_reparse(link))
                with patch.object(Path, "resolve", _refuse_db_resolve):
                    for opening in (
                        lambda: o.Store.open(link / "fresh.db"),
                        lambda: o.Store.connect_existing(link / "secret.db"),
                        lambda: o.Store.migrate(link / "secret.db"),
                        lambda: o.Store.open(link / "nested" / "fresh.db"),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "reparse"):
                            opening()
                self.assertEqual(sorted(p.name for p in real.iterdir()), ["secret.db"])
                self.assertEqual(sentinel.read_bytes(), b"do-not-touch")

                target = root / "target.db"
                try:
                    target.write_bytes(b"leaf-secret")
                    os.symlink(target, leaf)
                except OSError:
                    if target.exists():
                        target.unlink()
                    _link_dir(target, leaf)
                self.assertTrue(migration.is_reparse(leaf))
                with patch.object(Path, "resolve", _refuse_db_resolve):
                    for opening in (
                        lambda: o.Store.open(leaf),
                        lambda: o.Store.connect_existing(leaf),
                        lambda: o.Store.migrate(leaf),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "reparse"):
                            opening()
                if target.is_file():
                    self.assertEqual(target.read_bytes(), b"leaf-secret")
                else:
                    self.assertEqual(list(target.iterdir()), [])
            finally:
                _drop_reparse(link)
                _drop_reparse(leaf)


class Cli(unittest.TestCase):
    def test_status_and_dry_run_leave_db_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "s.db"
            opened = o.Store.open(db)
            opened.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            opened.db.close()

            def run(*a):
                return subprocess.run(
                    [sys.executable, str(ROOT / "tools/orchestrator.py"), "--db", str(db), *a],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

            r = run("--status")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("domain yield", r.stdout)
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            r = run("--dry-run", "--max-topics", "2")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("dry run", r.stdout)
            self.assertNotIn("would call: search", r.stdout)
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)

    def test_run_once_without_a_goal_makes_no_search(self):
        with tempfile.TemporaryDirectory() as td:
            topics = Path(td) / "topics.yml"
            topics.write_text(
                "seeds:\n"
                "  - id: funded-on-campus-masters\n"
                "    family: opportunities\n"
                "    focus: funded on-campus computer science masters\n"
                "    regions: [global]\n"
                "    audiences: [students]\n",
                encoding="utf-8",
            )
            db = Path(td) / "research.db"
            opened = o.Store.open(db)
            opened.db.close()
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "snowball.py"), "run-once",
                    "--topics", str(topics), "--topic", "funded-on-campus-masters",
                    "--db", str(db), "--dry-run", "--json",
                ],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wave = json.loads(result.stdout)
            self.assertEqual(wave["reason"], "no_goal")
            self.assertEqual(wave["topics"], [])
            self.assertEqual(wave["external_calls"], 0)


class RuntimeBehaviorGates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "snowball.db"
        self.cfg = o.load_config()
        self.store = o.Store.open(self.db)
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO families(name, recheck_days, weight, relevance_floor, focus) VALUES "
                "('opportunities', 14, 1.0, 0.08, 'non-dilutive grants, scholarships, fellowships and credit programs') "
                "ON CONFLICT DO NOTHING"
            )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_topic_due_boundary_and_explicit_override(self):
        clock = 1_800_000_000
        interval = 14 * 86400
        # SQLite's real strftime returns TEXT, including for Unix timestamps.
        self.store.db.create_function("strftime", 2, lambda fmt, value: str(clock))
        cases = {
            "never-run": None,
            "recent": clock - interval + 1,
            "exactly-due": clock - interval,
            "overdue": clock - interval - 86400,
            "future": clock + 86400,
        }
        with self.store.tx() as db:
            for key, last_run in cases.items():
                db.execute(
                    "INSERT INTO topics(key, family, title, focus, discovered_via, created_at, last_run_at) "
                    "VALUES (?, 'opportunities', ?, ?, 'seed', ?, ?)",
                    (key, key, key, clock - interval * 2, last_run),
                )
        worker = o.SnowballOrchestrator(self.cfg, self.store)
        selected = {topic["key"]: topic["due"] for topic in worker.select_topics(10, None)}
        self.assertEqual(set(selected), {"never-run", "exactly-due", "overdue"})
        self.assertEqual(selected["exactly-due"], 1.0)
        self.assertEqual(selected["overdue"], 2.0)
        manual = worker.select_topics(1, "recent")
        self.assertEqual([topic["key"] for topic in manual], ["recent"])
        self.assertEqual(manual[0]["due"], 0.0)

    def test_hub_rows_diff_20_to_22_queues_two_and_halves_interval(self):
        """T5: 20->22 rows queues exactly two new pages and halves interval. Unchanged hub doubles interval and uses zero extraction tokens."""
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            _, tid = orch.locks.topic_lock(
                db, "Test Grants Hub", "grants and awards directory", "opportunities", "seed", key="test-grants"
            )
            pid = orch.locks.enqueue_url(db, "https://example.com/directory", tid, "seed", 14)
            db.execute("UPDATE pages SET status='fetched', kind='hub' WHERE id=?", (pid,))

        topic = self.store.one("SELECT * FROM topics WHERE id=?", (tid,))
        fam = self.store.one("SELECT * FROM families WHERE name='opportunities'")
        run = o.TopicRun(topic, fam, False)
        page_row = self.store.one("SELECT * FROM pages WHERE id=?", (pid,))

        # 1. First fetch with 20 rows
        links_20 = [(f"https://example.com/grant/{i}", f"Grant Program {i}") for i in range(1, 21)]
        res_20 = FetchResult("https://example.com/directory", "direct", "ok", 200, "Active Grants Directory", "List of grants.\n" * 20, links=links_20)
        orch.record_fetch(run, page_row, res_20)

        initial_hub_rows = self.store.one("SELECT count(*) n FROM hub_rows WHERE hub_page_id=?", (pid,))["n"]
        self.assertEqual(initial_hub_rows, 20)

        # 2. Second fetch with 22 rows (20 existing + 2 new)
        links_22 = links_20 + [
            ("https://example.com/grant/21", "Grant Program 21"),
            ("https://example.com/grant/22", "Grant Program 22"),
        ]
        res_22 = FetchResult("https://example.com/directory", "direct", "ok", 200, "Active Grants Directory", "List of grants.\n" * 22, links=links_22)
        page_row = self.store.one("SELECT * FROM pages WHERE id=?", (pid,))
        orch.record_fetch(run, page_row, res_22)

        # Check: 20 -> 22 rows queues exactly two new pages
        queued_pages = self.store.all("SELECT canon_url FROM pages WHERE discovered_via='hub' ORDER BY canon_url")
        new_hub_urls = [p["canon_url"] for p in queued_pages]
        expected_two_urls = ["https://example.com/grant/21", "https://example.com/grant/22"]
        self.assertEqual(new_hub_urls, expected_two_urls)

        # Check: halves interval
        page_after_diff = self.store.one("SELECT interval_sec FROM pages WHERE id=?", (pid,))
        self.assertEqual(page_after_diff["interval_sec"], 7 * 86400)

        # 3. Third fetch with unchanged rows (22 rows again)
        tokens_before_unchanged_hub = orch.budget.tokens_today("local")
        res_unchanged = FetchResult("https://example.com/directory", "direct", "ok", 200, "Active Grants Directory", "List of grants.\n" * 22, links=links_22)
        page_row = self.store.one("SELECT * FROM pages WHERE id=?", (pid,))
        orch.record_fetch(run, page_row, res_unchanged)

        # Check: unchanged hub doubles interval and uses zero extraction tokens
        page_after_unchanged = self.store.one("SELECT interval_sec FROM pages WHERE id=?", (pid,))
        self.assertEqual(page_after_unchanged["interval_sec"], 14 * 86400)
        tokens_after_unchanged_hub = orch.budget.tokens_today("local")
        self.assertEqual(tokens_after_unchanged_hub, tokens_before_unchanged_hub)

    def test_five_paraphrases_yield_one_claim_five_sources_four_variants(self):
        """T6: Verify five paraphrases/five independent domains yield one claim, five sources, and four variants.
        Exclude reprints from independent counts; preserve numeric differences as material facts."""
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            _, tid = orch.locks.topic_lock(db, "Mozilla Tech Fund", "grants", "opportunities", "seed", key="moz-fund")

        paraphrases = [
            ("Mozilla Technology Fund offers grants of up to $50,000 for open-source AI projects in 2026.", "domain1.org", False),
            ("The Mozilla Technology Fund provides up to $50,000 in grant funding for open-source AI builders.", "domain2.com", False),
            ("In 2026, Mozilla Technology Fund awards grants reaching $50,000 to open-source developers.", "domain3.net", False),
            ("Open-source developers can receive grants up to $50,000 from Mozilla Technology Fund.", "domain4.io", False),
            ("Grants up to $50,000 are awarded by Mozilla Technology Fund for open-source accountability projects.", "domain5.ai", False),
        ]

        with self.store.tx() as db:
            for text, dom, reprint in paraphrases:
                pid = orch.locks.enqueue_url(db, f"https://{dom}/page", tid, "seed", 14)
                orch.locks.claim_lock(db, text, pid, tid, dom, (0, len(text)), reprint)

        claims = self.store.all("SELECT * FROM claims")
        self.assertEqual(len(claims), 1, "Five paraphrases must merge into exactly one canonical claim")
        self.assertEqual(claims[0]["n_sources"], 5, "Five independent domains must yield 5 sources")

        variants = self.store.all("SELECT * FROM claim_variants WHERE claim_hash=?", (claims[0]["hash"],))
        self.assertEqual(len(variants), 4, "Four subsequent paraphrases must produce exactly four variants")

        # Reprint domain does NOT increment n_sources
        with self.store.tx() as db:
            reprint_pid = orch.locks.enqueue_url(db, "https://syndicated-news.com/copy", tid, "seed", 14)
            orch.locks.claim_lock(db, paraphrases[0][0], reprint_pid, tid, "syndicated-news.com", (0, len(paraphrases[0][0])), reprint=True)
        claim_after_reprint = self.store.one("SELECT n_sources FROM claims WHERE hash=?", (claims[0]["hash"],))
        self.assertEqual(claim_after_reprint["n_sources"], 5, "Reprint must not increment independent source count")

        # Claim with a DIFFERENT number ($90,000 instead of $50,000) is a material fact and must NOT merge
        diff_number_claim = "Mozilla Technology Fund offers grants of up to $90,000 for open-source AI projects in 2026."
        with self.store.tx() as db:
            diff_pid = orch.locks.enqueue_url(db, "https://different-fact.org/story", tid, "seed", 14)
            verdict = orch.locks.claim_lock(db, diff_number_claim, diff_pid, tid, "different-fact.org", (0, len(diff_number_claim)), False)
        self.assertEqual(verdict, "new", "Claim with different numbers must not merge")
        self.assertEqual(self.store.one("SELECT count(*) n FROM claims")["n"], 2)

    def test_private_topic_does_not_reuse_public_key(self):
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            verdict, public_id = orch.locks.topic_lock(
                db, "Forum Notes", "what people tried", "opportunities", "seed", private=False, key="forum-notes"
            )
            self.assertEqual(verdict, "new")
            verdict2, private_id = orch.locks.topic_lock(
                db, "Forum Notes", "what people tried", "opportunities", "seed", private=True, key="forum-notes"
            )
        self.assertEqual(verdict2, "new")
        self.assertNotEqual(public_id, private_id)
        pub = self.store.one("SELECT private, key FROM topics WHERE id=?", (public_id,))
        priv = self.store.one("SELECT private, key FROM topics WHERE id=?", (private_id,))
        self.assertEqual(pub["private"], 0)
        self.assertEqual(priv["private"], 1)
        self.assertEqual(priv["key"], "forum-notes-private")

    def test_30_labelled_topic_pairs_benchmark(self):
        """T6: Benchmark 30 labelled topic pairs at existing 0.75/0.30 thresholds; meet >=95% new/not-new and >=80% same/deepen agreement."""
        from collections import Counter
        fixture_path = ROOT / "tests/topic_pairs_30.jsonl"
        self.assertTrue(fixture_path.exists(), "tests/topic_pairs_30.jsonl must exist")

        pairs = []
        with open(fixture_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    pairs.append(json.loads(line))
        self.assertEqual(len(pairs), 30)

        # Build IDF dictionary
        all_terms = []
        for p in pairs:
            all_terms.append(o.topic_terms(p["topic_a"]["title"], p["topic_a"]["focus"]))
            all_terms.append(o.topic_terms(p["topic_b"]["title"], p["topic_b"]["focus"]))

        df = Counter()
        for t in all_terms:
            for term in set(t.keys()):
                df[term] += 1
        n_docs = len(all_terms)

        correct_new_not_new = 0
        correct_exact_label = 0

        for p in pairs:
            ta = o.topic_terms(p["topic_a"]["title"], p["topic_a"]["focus"])
            tb = o.topic_terms(p["topic_b"]["title"], p["topic_b"]["focus"])
            score = o.tfidf_cosine(ta, tb, df, n_docs)

            pred_new = "new" if score < 0.30 else "not-new"
            actual_new = "new" if p["label"] == "new" else "not-new"
            if pred_new == actual_new:
                correct_new_not_new += 1

            pred_label = "same" if score >= 0.75 else ("deepen" if score >= 0.30 else "new")
            if pred_label == p["label"]:
                correct_exact_label += 1

        new_agreement = (correct_new_not_new / len(pairs)) * 100.0
        exact_agreement = (correct_exact_label / len(pairs)) * 100.0

        self.assertGreaterEqual(new_agreement, 95.0, f"New/Not-new agreement {new_agreement}% must meet >= 95%")
        self.assertGreaterEqual(exact_agreement, 80.0, f"Same/Deepen exact agreement {exact_agreement}% must meet >= 80%")

    def test_drift_exclusion_drops_off_topic_page_and_prevents_reentry(self):
        """T6: Enforce family relevance floor before frontier expansion. An off-topic page becomes dropped and cannot re-enter."""
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            _, tid = orch.locks.topic_lock(db, "Developer Grants", "software startup grants", "opportunities", "seed", key="dev-grants")
            pid = orch.locks.enqueue_url(db, "https://offtopic-baking.com/recipe", tid, "seed", 14)

        topic = self.store.one("SELECT * FROM topics WHERE id=?", (tid,))
        fam = self.store.one("SELECT * FROM families WHERE name='opportunities'")
        run = o.TopicRun(topic, fam, False)
        page_row = self.store.one("SELECT * FROM pages WHERE id=?", (pid,))

        off_topic_text = (
            "Traditional sourdough baking requires 80% hydration and active wild yeast levain. "
            "Mix the flour and water, allow autolyse for 60 minutes, then fold gently every half hour. "
            "Bake in a Dutch oven at 230 degrees Celsius for optimal crust and crumb texture."
        ) * 5
        off_topic_res = FetchResult("https://offtopic-baking.com/recipe", "direct", "ok", 200, "Sourdough Bread Recipe", off_topic_text, links=[("https://offtopic-baking.com/croissants", "Croissants")])

        orch.record_fetch(run, page_row, off_topic_res)

        # Page must be marked 'dropped'
        page_after = self.store.one("SELECT status, relevance FROM pages WHERE id=?", (pid,))
        self.assertEqual(page_after["status"], "dropped")
        self.assertLess(page_after["relevance"], 0.08)

        # Cannot re-enter through a different discovered link in the same window
        with self.store.tx() as db:
            reentered_id = orch.locks.enqueue_url(db, "https://offtopic-baking.com/recipe", tid, "hub", 14)
            self.assertIsNone(reentered_id, "Dropped off-topic page must not re-enter the frontier")

    def test_task_heartbeat_and_reaper_invalidation(self):
        """T7: Heartbeat active tasks every 60s, reap after 600s, increment occurrence sequence at reclamation,
        and verify that stale commits affect zero rows."""
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            tid = orch.new_task(db, "fetch", {"url": "https://test.com"}, 1)

        seq = orch.claim_task(tid)
        self.assertIsNotNone(seq)
        self.assertTrue(orch.heartbeat(tid, seq))

        # Simulate staleness (> 600s)
        with self.store.tx() as db:
            db.execute("UPDATE tasks SET heartbeat_at=strftime('%s','now') - 700 WHERE id=?", (tid,))

        reaped = orch.reap()
        self.assertEqual(reaped, 1)

        # Sequence must be invalidated on reclamation
        task_reaped = self.store.one("SELECT status, occurrence_seq, worker_id FROM tasks WHERE id=?", (tid,))
        self.assertEqual(task_reaped["status"], "pending")
        self.assertEqual(task_reaped["occurrence_seq"], seq + 1)
        self.assertIsNone(task_reaped["worker_id"])

        # Stale commit with old sequence must fail and write zero rows
        self.assertFalse(orch.commit_with_reclaimed_occurrence(seq, task_id=tid))
        self.assertFalse(orch.finish_task(tid, seq, "done", result={"status": "late"}))

        # Check due=0 fetch count stays zero
        self.assertEqual(self.store.one("SELECT count(*) n FROM fetches WHERE due=0")["n"], 0)

    def test_10000_deterministic_seeded_slug_property_cases(self):
        """T5: 10,000 deterministic seeded slug-property cases verifying fixed point slug(slug(x)) == slug(x)."""
        import random
        rng = random.Random(42)
        words = ["Grants", "AI", "Startup", "2026", "Fellowship", "Scholarship", "Österreich", "Tech", "Fund", "Gov.in", "Web3", "Open-Source"]
        for _ in range(10000):
            sample = " ".join(rng.choices(words, k=rng.randint(1, 5)))
            punct = rng.choice(["!", "@", "#", "$", "%", "—", "_", "/", ""])
            raw = f"{sample} {punct} {rng.randint(0, 9999)}"
            s = o.slug(raw)
            self.assertEqual(o.slug(s), s, f"slug must be idempotent fixed-point for {raw!r}")
            self.assertLessEqual(len(s), 80)
            self.assertFalse(s.startswith("-"))
            self.assertFalse(s.endswith("-"))

    def test_classifier_accuracy_on_50_labelled_pages(self):
        """T5: Run classifier on 50 labelled pages meeting >= 85% accuracy."""
        fixture_path = ROOT / "tests/pages_labelled_50.jsonl"
        self.assertTrue(fixture_path.exists())
        total, correct = 0, 0
        with open(fixture_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                actual = item["label"]
                pred = o.classify_page(item.get("title", ""), item.get("text", "") or item.get("html", ""), item.get("url", ""))
                total += 1
                if pred == actual:
                    correct += 1
        accuracy = (correct / total) * 100.0 if total else 0.0
        self.assertGreaterEqual(accuracy, 85.0, f"Classifier accuracy {accuracy}% must meet >= 85%")

    def test_validate_extraction_on_20_pipeline_fixtures(self):
        """T5: Extraction validation on 20 real pipeline fixtures."""
        fixture_path = ROOT / "tests/items_20.jsonl"
        self.assertTrue(fixture_path.exists())
        with open(fixture_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                res = o.validate_extraction({"rows": item.get("entities", [])}, item.get("text", ""), fallback_url=item.get("url", ""))
                self.assertEqual(len(res["rows"]), len(item.get("entities", [])))
                for row in res["rows"]:
                    self.assertTrue(row["name"])
                    self.assertTrue(row["evidence_quote"])
                    self.assertTrue(row["link"])

    def test_provider_diagnostics_detects_missing_credentials(self):
        """T1: Provider diagnostics report authentic availability checks without fake key-presence success."""
        cfg = o.Config()
        cfg.gemini_transport = "api"
        cfg.gemini_api_key = None
        diag = o.diagnose_providers(cfg, live=False)
        self.assertFalse(diag["all_ok"])
        self.assertEqual(diag["gemini"]["status"], "blocked")
        self.assertIn("not configured", diag["gemini"]["error"])
        self.assertIn("exa", diag)
        self.assertIn("review", diag)
        self.assertIn("private", diag)
        self.assertNotIn("grok", diag)
        self.assertNotIn("local", diag)

    def test_provider_diagnostics_ssh_omp_transport(self):
        """T1: Provider diagnostics supports explicit ssh-omp transport with ssh host check."""
        cfg = o.Config()
        cfg.gemini_transport = "ssh-omp"
        cfg.ssh_model_host = "nonexistent-oracle-host.test"
        cfg.ssh_timeout_sec = 2
        diag = o.diagnose_providers(cfg, live=False)
        self.assertEqual(diag["gemini"]["transport"], "ssh-omp")
        # Should fail reachability on nonexistent host without throwing uncaught exceptions
        self.assertIn(diag["gemini"]["status"], {"error", "blocked"})

    def test_reserve_rejects_when_daily_cap_would_be_exceeded(self):
        """A reservation that does not fit the remaining daily cap writes nothing."""
        store = o.Store.in_memory()
        cfg = o.Config()
        cfg.caps = {"gemini": 100, "grok": 100, "local": 100}
        cfg.llm_tokens_per_day = 100
        budget = o.TokenBudgetMiddleware(store, cfg)
        tb = budget.task("local", max_in=10_000, max_out=10_000)
        budget.reserve(tb, 40, 40)
        self.assertEqual(budget.tokens_today("local"), 80)
        with self.assertRaises(o.BudgetExhausted):
            budget.reserve(tb, 30, 0)
        self.assertEqual(budget.tokens_today("local"), 80)
        self.assertEqual(sum(budget.spent()), 80)

    def test_reconcile_cannot_drive_spend_negative(self):
        """A second release cannot reopen budget by pushing stored totals below zero."""
        store = o.Store.in_memory()
        cfg = o.Config()
        cfg.caps = {"gemini": 100, "grok": 100, "local": 100}
        cfg.llm_tokens_per_day = 100
        budget = o.TokenBudgetMiddleware(store, cfg)
        tb = budget.task("local", 1000, 1000)
        budget.reserve(tb, 40, 10)
        budget.reconcile(tb, 40, 10, 0, 0)
        budget.reconcile(tb, 40, 10, 0, 0)
        self.assertEqual(budget.tokens_today("local"), 0)
        self.assertEqual(sum(budget.spent()), 0)
        self.assertEqual(tb.used_in, 0)
        self.assertEqual(tb.used_out, 0)
        budget.reserve(tb, 100, 0)
        self.assertEqual(budget.tokens_today("local"), 100)
if __name__ == "__main__":
    unittest.main()
