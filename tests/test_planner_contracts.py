"""Planner contracts on a real Store and an interview-built goal."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

COMPONENTS = {
    "tuition": True,
    "mandatory_fees": True,
    "living_amount": True,
    "living_duration": True,
    "health_insurance": True,
    "visa_costs": False,
    "application_costs": True,
    "travel_costs": False,
    "deposits": True,
    "proof_of_funds_liquidity": True,
}


def execution_policy(**replace):
    """Finite interview policy. Exploration share stays an integer so reserve can parse it."""
    policy = {
        "lifetime_limits": {
            "queued_nodes": 20,
            "visited_nodes": 20,
            "fetches": 20,
            "queries": 20,
            "model_tokens": 40000,
        },
        "max_depth": 4,
        "max_queue": 12,
        "per_domain_inflight": 4,
        "max_hypotheses_per_wave": 6,
        "exploration_share": 0,
        "usage_mode": "estimated",
    }
    life = replace.pop("lifetime_limits", None)
    if life:
        policy["lifetime_limits"].update(life)
    policy.update(replace)
    return policy


def load_goal():
    path = ROOT / "tools" / "goal.py"
    key = "snowball_goal"
    existing = sys.modules.get(key)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


goal = load_goal()
interview = goal._sibling("interview")
orch = goal._sibling("orchestrator")
planner = goal._sibling("planner")


def _answers(policy):
    return {
        "degree_level": "masters",
        "modality": "on_campus",
        "field_tags": ["computing"],
        "regions": ["example-region"],
        "intake_windows": ["2027-fall"],
        "required_components": dict(COMPONENTS),
        "grants_only": True,
        "accept_conditional_ra_ta": False,
        "accept_competitive_awards": False,
        "max_net_total_cost": {"amount": 0, "currency": "INR"},
        "max_upfront_cash": {"amount": 0, "currency": "INR"},
        "min_living_adequacy_rule": {
            "basis": "same_location_period",
            "count_housing_once": True,
            "count_insurance_once": True,
            "numeric_floor": None,
        },
        "max_work_hours_acceptable": 0,
        "allow_loans": False,
        "citizenship": "example-citizen",
        "prior_degree": {"level": "bachelors", "field": "computing"},
        "completion_min_candidates": 1,
        "execution_policy": policy,
        "monitoring": {
            "permitted_work": ["recheck_official_sources"],
            "max_refreshes": 1,
            "interval_days": 30,
        },
        "consent_private_evaluation": False,
    }


def grant_limits(policy, queued_nodes=None, hypotheses=None):
    life = dict(policy["lifetime_limits"])
    if queued_nodes is not None:
        life["queued_nodes"] = min(int(queued_nodes), int(life["queued_nodes"]))
    hypotheses_cap = policy["max_hypotheses_per_wave"]
    if hypotheses is not None:
        hypotheses_cap = min(int(hypotheses), int(policy["max_hypotheses_per_wave"]))
    return {
        "usage_mode": "estimated",
        "exploration_share": 0,
        "max_depth": policy["max_depth"],
        "max_queue": policy["max_queue"],
        "per_domain_inflight": policy["per_domain_inflight"],
        "max_hypotheses_per_wave": hypotheses_cap,
        "day": {
            "fetches": int(life["fetches"]),
            "queries": int(life["queries"]),
            "model_tokens": min(1000, int(life["model_tokens"])),
        },
        "lifetime": life,
    }


def decisions(store):
    rows = []
    for row in store.db.execute("SELECT * FROM frontier_decisions ORDER BY id"):
        item = dict(row)
        item["components"] = json.loads(item["components_json"])
        rows.append(item)
    return rows


def reservation_counters(store):
    return {row["counter"] for row in store.db.execute("SELECT counter FROM resource_reservations")}


def task_count(store, goal_id=None):
    if goal_id is None:
        return store.db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    return store.db.execute("SELECT COUNT(*) AS n FROM tasks WHERE goal_id=?", (goal_id,)).fetchone()["n"]


class Lab:
    """One temporary v4 store and an interview-approved running goal."""

    def __init__(self, slug, policy=None, start=True, queued_nodes=None, hypotheses=None, store=None, tmp=None):
        self.slug = slug
        self.policy = policy or execution_policy()
        self.owns = store is None
        self.tmp = tmp
        if store is None:
            self.tmp = tempfile.TemporaryDirectory()
            self.store = orch.Store.open(Path(self.tmp.name) / "lab.db")
        else:
            self.store = store
        self.goal = None
        self.contract = None
        self.grant = None
        self.runnable = None
        self.topic_id = None
        self._boot(start=start, queued_nodes=queued_nodes, hypotheses=hypotheses)

    def close(self):
        if not self.owns:
            return
        self.store.db.close()
        self.tmp.cleanup()

    def another(self, slug, start=False, queued_nodes=None, hypotheses=None):
        return Lab(
            slug,
            self.policy,
            start=start,
            queued_nodes=queued_nodes,
            hypotheses=hypotheses,
            store=self.store,
            tmp=self.tmp,
        )

    def _boot(self, start, queued_nodes, hypotheses):
        self.goal = goal.create_goal(self.store, self.slug, "Synthetic public goal", "fixture")
        answers = _answers(self.policy)
        for _ in range(40):
            current = goal.get_goal(self.store, self.goal.id)
            question = interview.next_question(goal.fields(self.store, self.goal.id))
            if question is None:
                break
            if question.field_key not in answers:
                raise AssertionError(question.field_key)
            goal.record_answer(
                self.store,
                self.goal.id,
                question.field_key,
                answers[question.field_key],
                True,
                question.visibility_default,
                current.row_version,
                "fixture",
            )
        else:
            raise AssertionError("interview did not finish")
        current = goal.get_goal(self.store, self.goal.id)
        body = interview.build_contract(goal.fields(self.store, self.goal.id))
        self.contract = goal.propose_contract(
            self.store, self.goal.id, body, None, "fixture", expected_row_version=current.row_version
        )
        current = goal.get_goal(self.store, self.goal.id)
        goal.approve_contract(
            self.store,
            self.goal.id,
            self.contract.version,
            self.contract.body_hash,
            current.row_version,
            "fixture",
            f"{self.slug}-approve",
        )
        current = goal.get_goal(self.store, self.goal.id)
        self.grant = goal.grant_execution(
            self.store,
            self.goal.id,
            self.contract.version,
            "research",
            grant_limits(self.policy, queued_nodes=queued_nodes, hypotheses=hypotheses),
            int(time.time()) + 7 * 86400,
            current.row_version,
            "fixture",
            f"{self.slug}-grant",
        )
        self.topic_id = self._topic()
        self._link_topic(self.goal.id, self.contract.version, self.topic_id)
        if start:
            self.start()

    def start(self):
        current = goal.get_goal(self.store, self.goal.id)
        goal.transition(self.store, self.goal.id, "start", "start", current.row_version, "fixture", f"{self.slug}-start")
        self.refresh()

    def pause(self):
        current = goal.get_goal(self.store, self.goal.id)
        goal.transition(self.store, self.goal.id, "pause", "pause", current.row_version, "fixture", f"{self.slug}-pause")
        goal.finish_pause(self.store, self.goal.id)
        self.runnable = None

    def replan(self):
        current = goal.get_goal(self.store, self.goal.id)
        goal.transition(
            self.store, self.goal.id, "replan", "operator replan", current.row_version, "fixture", f"{self.slug}-replan"
        )
        self.refresh()

    def refresh(self):
        self.runnable = goal.runnable_goal(self.store, int(time.time()))
        if self.runnable is None:
            raise AssertionError(goal.runnable_reason(self.store, int(time.time())))
        self.goal = self.runnable.goal
        self.contract = goal.load_contract(self.store, self.goal.id, self.goal.current_contract_version)

    def _topic(self):
        with self.store.tx() as db:
            db.execute(
                """INSERT INTO families(name, recheck_days, weight, relevance_floor, focus)
                   VALUES ('opportunities', 14, 1.0, 0.08, 'public programme funding')
                   ON CONFLICT(name) DO NOTHING"""
            )
            _verdict, topic_id = orch.LockEngine(self.store).topic_lock(
                db, f"public {self.slug}", "public synthetic focus", "opportunities", "hub", key=f"topic-{self.slug}"
            )
        return int(topic_id)

    def _link_topic(self, goal_id, version, topic_id):
        with self.store.tx() as db:
            db.execute(
                """INSERT OR IGNORE INTO goal_topics(goal_id, contract_version, topic_id, role, visibility)
                   VALUES (?, ?, ?, 'candidate', 'public')""",
                (goal_id, version, topic_id),
            )

    def share_topic(self, other):
        version = other.contract.version
        self._link_topic(other.goal.id, version, self.topic_id)
        other.topic_id = self.topic_id

    def program(self, key, name, domain, degree="masters", modality="on_campus"):
        now = int(time.time())
        version = self.contract.version
        with self.store.tx() as db:
            db.execute(
                """INSERT INTO programs(
                       key, name, institution, country, official_domain, degree_level, modality,
                       field_tags_json, provenance, first_seen_at, last_seen_at)
                   VALUES (?, ?, 'Example Institute', 'EX', ?, ?, ?, '[]', 'observed', ?, ?)""",
                (key, name, domain, degree, modality, now, now),
            )
            db.execute(
                """INSERT INTO goal_programs(goal_id, contract_version, program_key, role, visibility)
                   VALUES (?, ?, ?, 'candidate', 'public')""",
                (self.goal.id, version, key),
            )
        return key

    def page(self, url, parent=None):
        with self.store.tx() as db:
            page_id = orch.LockEngine(self.store).enqueue_url(
                db, url, self.topic_id, "hub", 7, parent=parent, depth=0
            )
            if page_id is None:
                canon = orch.canonical_url(url)
                page_id = db.execute("SELECT id FROM pages WHERE canon_url=?", (canon,)).fetchone()["id"]
            db.execute(
                """UPDATE pages
                     SET next_check_at=1, next_attempt_at=NULL, status='queued', quarantined=0
                   WHERE id=?""",
                (int(page_id),),
            )
        return int(page_id)

    def associate(self, page_id, depth=0, origin="hub", goal_id=None, version=None):
        goal_id = self.goal.id if goal_id is None else goal_id
        version = self.contract.version if version is None else version
        with self.store.tx() as db:
            db.execute(
                """INSERT OR IGNORE INTO goal_pages(
                       goal_id, contract_version, page_id, parent_page_id, depth, origin, visibility, queued_at)
                   VALUES (?, ?, ?, NULL, ?, ?, 'public', ?)""",
                (goal_id, version, page_id, depth, origin, int(time.time())),
            )

    def keep(self, program_key, field_key, page_id, content_hash, retrieved_at=10):
        version = self.contract.version
        with self.store.tx() as db:
            snapshot_id = db.execute(
                """INSERT INTO source_snapshots(
                       page_id, content_hash, retrieved_at, text_path, visibility, provenance)
                   VALUES (?, ?, ?, 'synthetic/public.txt', 'public', 'fixture')""",
                (page_id, content_hash, retrieved_at),
            ).lastrowid
            db.execute(
                """INSERT INTO observations(
                       snapshot_id, page_id, program_key, field_key, value_json, subject_json,
                       quote, quote_start, quote_end, extractor_model, prompt_version, assurance,
                       goal_id, contract_version, created_at)
                   VALUES (?, ?, ?, ?, '{}', '{}', 'public fees', 0, 11, 'fixture', 'v1',
                           'reviewed_kept', ?, ?, ?)""",
                (snapshot_id, page_id, program_key, field_key, self.goal.id, version, retrieved_at),
            )
        return int(snapshot_id)

    def keep_all(self, program_key, page_id):
        for field in self.contract.body["completion"]["critical_fields"]:
            self.keep(program_key, field, page_id, f"kept-{program_key}-{field}")

    def fetch(self, url, origin="hypothesis", program_key=None, field_key=None, rationale="public page"):
        return planner.Proposal("fetch", program_key, field_key, None, url, rationale, origin)

    def search(self, query, origin="hypothesis", program_key=None, field_key=None, rationale="public search"):
        return planner.Proposal("search", program_key, field_key, query, None, rationale, origin)

    def run(self, proposals, wave_id):
        validated = planner.validate_proposals(self.store, self.runnable, list(proposals))
        plan = planner.schedule(self.store, self.runnable, validated, wave_id)
        return validated, plan

    def body_hash(self):
        return self.store.db.execute(
            "SELECT body_hash FROM goal_contracts WHERE goal_id=? AND version=?",
            (self.goal.id, self.contract.version),
        ).fetchone()["body_hash"]

    def strategy_body(self, version):
        row = self.store.db.execute(
            "SELECT body_json FROM strategies WHERE goal_id=? AND version=?",
            (self.goal.id, version),
        ).fetchone()
        return json.loads(row["body_json"])


def canon(url):
    return orch.canonical_url(url)


def selected_canons(store, plan):
    found = []
    for page_id in plan.selected_page_ids:
        found.append(store.db.execute("SELECT canon_url FROM pages WHERE id=?", (page_id,)).fetchone()[0])
    return found


class PlannerContractTests(unittest.TestCase):
    def test_engine_sees_only_the_public_frame_and_drops_are_recorded(self):
        lab = Lab("engine")
        self.addCleanup(lab.close)
        frame = lab.runnable.frame
        self.assertIsInstance(frame, interview.PublicFrame)
        budget = {
            "db": lab.store.db,
            "binding": {
                "goal_id": lab.goal.id,
                "contract_version": lab.contract.version,
                "strategy_version": lab.goal.current_strategy_version,
                "epoch": lab.goal.epoch,
            },
            "operation_key": "plan-public-1",
            "max_out": 5,
            "exploration_budget": 3,
        }
        coverage = {
            "programs": [{
                "program_key": "example-computing",
                "name": "Example Computing",
                "official_domain": "admissions.example.edu",
                "unknown_fields": ["tuition"],
                "contradictions": [],
            }],
            "known_domains": ["admissions.example.edu"],
        }

        class Engine:
            def __init__(self, text):
                self.calls = []
                self.text = text

            def plan(self, public, summary, exploration_budget, binding, operation_key, max_out):
                self.calls.append((public, summary, exploration_budget, binding, operation_key, max_out))
                return {"text": self.text}

        engine = Engine(json.dumps({"hypotheses": []}))
        stale = dict(budget)
        stale["binding"] = dict(budget["binding"], epoch=budget["binding"]["epoch"] + 9)
        with self.assertRaises(planner.PlannerError):
            planner.planner_proposals(engine, frame, coverage, stale)
        with self.assertRaises(planner.PlannerError):
            planner.planner_proposals(engine, frame, {"programs": [], "profile": {"citizenship": "hidden"}}, budget)
        with self.assertRaises(planner.PlannerError):
            planner.planner_proposals(engine, {"domain": frame.domain}, coverage, budget)
        self.assertEqual(engine.calls, [])

        engine.text = json.dumps({
            "hypotheses": [
                {"kind": "search", "query": "computing tuition", "rationale": "ignore", "note": "extra"},
                {"kind": "fetch", "rationale": "missing url"},
                {
                    "kind": "search",
                    "program_key": "not-a-program",
                    "field_key": "tuition",
                    "query": "computing official tuition 2027",
                    "rationale": "maybe this source",
                },
            ]
        })
        proposals = planner.planner_proposals(engine, frame, coverage, budget)
        self.assertEqual(len(engine.calls), 1)
        public, summary, exploration_budget, binding, operation_key, max_out = engine.calls[0]
        self.assertIs(public, frame)
        self.assertEqual(set(summary), {"programs", "known_domains"})
        self.assertEqual(summary["programs"][0]["unknown_fields"], ["tuition"])
        blob = json.dumps(summary)
        for banned in ("profile", "citizenship", "max_upfront_cash", "thresholds", "body_hash", "passport"):
            self.assertNotIn(banned, blob)
        self.assertIs(type(exploration_budget), int)
        self.assertEqual(exploration_budget, 3)
        self.assertEqual(set(binding), {"goal_id", "contract_version", "strategy_version", "epoch"})
        self.assertNotIn("db", binding)
        self.assertEqual(operation_key, "plan-public-1")
        self.assertEqual(max_out, 5)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].program_key, "not-a-program")
        self.assertEqual(proposals[0].origin, "hypothesis")

        before = lab.body_hash()
        _validated, plan = lab.run(proposals, 1)
        self.assertEqual(plan.selected_query_ids, ())
        self.assertEqual(plan.selected_page_ids, ())
        dropped = [row for row in decisions(lab.store) if row["decision"] == "dropped"]
        self.assertTrue(dropped)
        self.assertIn("programme", dropped[0]["reason"])
        self.assertNotEqual(dropped[0]["reason"], proposals[0].rationale)
        self.assertEqual(lab.body_hash(), before)

        class Boom(Engine):
            def plan(self, *args):
                super().plan(*args)
                return {"text": "not-json"}

        boom = Boom("not-json")
        existing = len(decisions(lab.store))
        with self.assertRaises(planner.PlannerOutage):
            planner.planner_proposals(boom, frame, coverage, budget)
        self.assertEqual(len(decisions(lab.store)), existing)
        self.assertEqual(len(boom.calls), 1)

    def test_source_backed_urls_are_admitted_and_invented_or_out_of_scope_urls_are_not(self):
        lab = Lab("links")
        self.addCleanup(lab.close)
        hub_id = lab.page("https://admissions.example.edu/hub")
        lab.associate(hub_id)
        linked = canon("https://admissions.example.edu/fees")
        listed = canon("https://bursar.example.edu/aid")
        invented = "https://not-discovered.example/secret"
        scoped = "https://admissions.example.edu/phd"
        lab.page(scoped)
        now = int(time.time())
        with lab.store.tx() as db:
            db.execute(
                "INSERT INTO page_links(from_page_id, to_canon_url, position) VALUES (?, ?, 0)",
                (hub_id, linked),
            )
            db.execute("INSERT INTO hubs(page_id) VALUES (?)", (hub_id,))
            db.execute(
                """INSERT INTO hub_rows(hub_page_id, row_hash, to_canon_url, first_seen_at, last_seen_at)
                   VALUES (?, 'row-1', ?, ?, ?)""",
                (hub_id, listed, now, now),
            )
        before_pages = {
            row["canon_url"] for row in lab.store.db.execute("SELECT canon_url FROM pages")
        }
        self.assertNotIn(invented, before_pages)
        self.assertNotIn(linked, before_pages)
        proposals = [
            lab.fetch(linked, rationale="fees linked from the hub"),
            lab.fetch(listed, rationale="aid listed on the hub"),
            lab.fetch(invented, rationale="model invented this url"),
            lab.fetch(scoped, rationale="doctoral page"),
        ]
        _validated, plan = lab.run(proposals, 1)
        canons = selected_canons(lab.store, plan)
        self.assertEqual(canons, [linked, listed])
        pages = {row["canon_url"] for row in lab.store.db.execute("SELECT canon_url FROM pages")}
        self.assertNotIn(canon(invented), pages)
        dropped = [row["reason"] for row in decisions(lab.store) if row["decision"] == "dropped"]
        self.assertTrue(any("discovered" in reason for reason in dropped))
        self.assertTrue(any("phd" in reason for reason in dropped))
        self.assertTrue(all(
            row["reason"].startswith("dropped:") or row["reason"].startswith("selected:")
            for row in decisions(lab.store)
        ))
        self.assertEqual(task_count(lab.store, lab.goal.id), 2)
        counters = reservation_counters(lab.store)
        self.assertTrue(counters)
        self.assertTrue(counters <= {"fetches", "queries", "queued_nodes"})
        self.assertEqual(lab.store.db.execute("SELECT COUNT(*) AS n FROM paid_fetches").fetchone()["n"], 0)

    def test_credentials_zero_duplicate_and_programme_scope(self):
        lab = Lab("words")
        self.addCleanup(lab.close)
        lab.program("phd-prog", "Doctoral Example", "doctor.example.edu", degree="phd")
        zero = "computing example-region tuition 0 2027"
        proposals = [
            lab.search(zero, rationale="public zero and year"),
            lab.search(zero, rationale="same query again"),
            lab.search("computing passport requirement example-region", rationale="needs the passport scan"),
            lab.search("computing example-region masters funding", program_key="phd-prog", rationale="wrong degree"),
            lab.search("computing phd scholarship example-region", rationale="doctoral wording"),
        ]
        validated, plan = lab.run(proposals, 1)
        self.assertEqual([item.decision for item in validated], ["valid", "dropped", "dropped", "dropped", "dropped"])
        self.assertIn("duplicate", validated[1].reason)
        self.assertIn("credential", validated[2].reason)
        self.assertIn("degree", validated[3].reason)
        self.assertIn("phd", validated[4].reason)
        self.assertEqual(len(plan.selected_query_ids), 1)
        raw = lab.store.db.execute("SELECT raw FROM queries WHERE id=?", (plan.selected_query_ids[0],)).fetchone()["raw"]
        self.assertIn("0", raw)
        self.assertIn("2027", raw)
        recorded = decisions(lab.store)
        self.assertEqual(sum(row["decision"] == "dropped" for row in recorded), 4)
        self.assertTrue(all(row["reason"].startswith("dropped:") for row in recorded if row["decision"] == "dropped"))
        selected = [row for row in recorded if row["decision"] == "selected"]
        self.assertEqual(len(selected), 1)
        self.assertNotEqual(selected[0]["reason"], proposals[0].rationale)
        self.assertEqual(selected[0]["components"]["model_rationale"], proposals[0].rationale)
        self.assertEqual(selected[0]["components"]["paid"], 0)
        self.assertEqual(reservation_counters(lab.store), {"queries", "queued_nodes"})

    def test_hypothesis_cap_and_paid_zero(self):
        lab = Lab("cap", hypotheses=2)
        self.addCleanup(lab.close)
        proposals = [
            lab.search(f"computing tuition hypothesis {name}", rationale=f"hypothesis {name}")
            for name in ("alpha", "beta", "gamma", "delta")
        ]
        _validated, plan = lab.run(proposals, 1)
        self.assertEqual(len(plan.selected_query_ids), 2)
        deferred = [row for row in decisions(lab.store) if row["decision"] == "deferred"]
        self.assertGreaterEqual(len(deferred), 2)
        self.assertTrue(all("hypothesis cap" in row["reason"] for row in deferred))
        for row in decisions(lab.store):
            if row["score"] is None:
                continue
            self.assertEqual(row["components"]["paid"], 0)
            self.assertEqual(row["components"]["estimated_tokens"], 0)
            self.assertEqual(row["components"]["cost"], 1)
        self.assertTrue(reservation_counters(lab.store) <= {"fetches", "queries", "queued_nodes"})
        self.assertEqual(lab.store.db.execute("SELECT COUNT(*) AS n FROM paid_fetches").fetchone()["n"], 0)

    def test_coverage_fingerprint_ignores_retrieval_time_and_novelty(self):
        lab = Lab("prints")
        self.addCleanup(lab.close)
        key = lab.program("example-computing", "Example Computing", "admissions.example.edu")
        page_id = lab.page("https://admissions.example.edu/tuition")
        before = planner.coverage_fingerprint(lab.store, lab.runnable)
        work_before = planner.work_fingerprint(lab.store, lab.runnable)
        lab.keep(key, "tuition", page_id, "hash-tuition", retrieved_at=10)
        kept = planner.coverage_fingerprint(lab.store, lab.runnable)
        self.assertNotEqual(kept, before)
        lab.store.db.execute("UPDATE source_snapshots SET retrieved_at=99")
        self.assertEqual(planner.coverage_fingerprint(lab.store, lab.runnable), kept)
        lab.store.db.execute("UPDATE pages SET novelty=0.01 WHERE id=?", (page_id,))
        self.assertEqual(planner.work_fingerprint(lab.store, lab.runnable), work_before)
        lab.store.db.execute("UPDATE source_snapshots SET content_hash='hash-tuition-revised'")
        self.assertNotEqual(planner.coverage_fingerprint(lab.store, lab.runnable), kept)

    def test_finished_query_window_is_not_reused(self):
        lab = Lab("window")
        self.addCleanup(lab.close)
        proposal = lab.search("computing example-region official tuition", rationale="gap style search")
        _validated, plan = lab.run([proposal], 1)
        self.assertEqual(len(plan.selected_query_ids), 1)
        lab.store.db.execute("UPDATE queries SET status='done' WHERE id=?", (plan.selected_query_ids[0],))
        before_tasks = task_count(lab.store, lab.goal.id)
        _validated, replay = lab.run([proposal], 2)
        self.assertEqual(replay.selected_query_ids, ())
        self.assertEqual(task_count(lab.store, lab.goal.id), before_tasks)
        self.assertTrue(any(row["reason"] == "deferred: query window is locked" for row in decisions(lab.store)))
