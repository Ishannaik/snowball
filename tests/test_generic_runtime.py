"""Consumer-visible runtime safety and goal-independent orchestration."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import generic_contract as contract
from tools.generic_runtime import Service


class ScenarioRoles:
    """Deterministic external-role boundary; production code has no fixture mode."""
    def __init__(self, *, missing_review=False):
        self.calls = []
        self.missing_review = missing_review

    def recipient(self, role):
        return role

    def reservation(self, role, payload, max_tokens):
        return {"model_calls": 1, "model_tokens": 100}

    def diagnostics(self):
        return {"configured": True}

    def call(self, role, payload, *, max_tokens=2048):
        self.calls.append((role, payload))
        if role == "planner":
            out = {"tasks": [{"criterion_id": "supported", "tool": "fetch", "url": "https://example.org/source", "public_context_ids": ["brief"]}]}
        elif role == "research":
            source = payload["sources"][0]
            out = {"claims": [{"source_id": source["source_id"], "quote": "alpha", "start": 0, "end": 5, "text": "alpha is documented", "criterion_ids": ["supported"], "score": 9}]}
        elif role == "review":
            out = {"reviews": [] if self.missing_review else [{"claim_id": c["id"], "verdict": "kept", "reason": "The source directly supports the claim."} for c in payload["claims"]]}
        elif role == "synthesis":
            out = {"text": "The source documents alpha.", "evidence_ids": [c["id"] for c in payload["claims"]]}
        elif role == "judge":
            ids = [c["id"] for c in payload["claims"] if c["state"] == "kept"]
            out = {"rows": [{"criterion_id": "supported", "verdict": "met" if ids else "unknown", "reason": "Source support checked.", "support_ids": ids}], "deliverable": {"supported": bool(ids), "reason": "Cited source supports the note.", "evidence_ids": ids}}
        else:
            raise AssertionError(role)
        return {"output": out, "role": role, "requested_model": role + "-fixture", "reported_model": role + "-fixture", "model_id": role + "-fixture", "tokens_in": 20, "tokens_out": 20, "usage_kind": "actual", "transport": "scenario", "prompt_version": "test"}


class ScenarioSources:
    def __init__(self):
        self.calls = []

    def fetch(self, url, *, max_bytes):
        self.calls.append(url)
        return {"url": url, "title": "Example source", "text": "alpha is documented", "links": [], "provenance": "public_fetch", "source_kind": "public_fetch", "bytes": 19}


def approved_payload(goal_id, objective):
    questions = contract.opening_questions()
    answers = []
    for q in questions:
        value = {"objective": objective, "deliverable": "A source-backed note", "limits": "bounded"}.get(q["id"])
        answers.append({"question_id": q["id"], "value": value, "state": "confirmed" if value else "unknown", "origin": "user_stated", "label": q["label"]})
    return {"schema": contract.SCHEMA, "goal_id": goal_id, "revision": 1, "parent_body_hash": None, "public_id": "ab" * 16,
            "questions": questions, "answers": answers, "rubric": {"criteria": [{"id": "supported", "description": "The requested note is source-supported.", "required": True, "expected_evidence": "kept source claims", "label": [], "qualifications": []}]},
            "public_criterion_ids": ["supported"], "public_brief": {"text": objective, "derived_from": ["objective"], "label": []},
            "recipients": {**{r: {"kind": "model", "attested": False, "clearance": []} for r in ("planner", "research", "review", "synthesis", "judge")}, "fetch": {"kind": "fetch", "attested": False, "clearance": []}, "search": {"kind": "search", "attested": False, "clearance": []}},
            "seeds": [{"id": "source", "value": "https://example.org/source", "label": []}],
            "ceilings": {"expansion_tasks": 4, "sources": 2, "source_bytes": 10000, "model_calls": 20, "tool_calls": 8, "model_tokens": 100000, "elapsed_sec": 120}, "concurrency": 2, "stopping_policy": {"on_exhaustion": "partial", "stall_checkpoints": 2}, "monitoring": None}


class RuntimeTest(unittest.TestCase):
    def setup_service(self, path, *, missing_review=False, objective="Compare queue semantics", model_calls=20):
        roles, sources = ScenarioRoles(missing_review=missing_review), ScenarioSources()
        service = Service(path, roles=roles, acquisition=sources)
        created = service.create(objective, "goal-1")
        payload = approved_payload("goal-1", objective)
        payload["ceilings"]["model_calls"] = model_calls
        version = contract.build_version(payload)
        view = service.approve("goal-1", payload, created["goal"]["row_version"], version["body_hash"])
        return service, roles, sources, view

    def test_approval_and_grant_do_not_dispatch_or_imply_running(self):
        with tempfile.TemporaryDirectory() as directory:
            service, roles, sources, view = self.setup_service(Path(directory) / "db.sqlite")
            self.assertEqual(view["goal"]["state"], "approved")
            service.run("goal-1", max_waves=1)
            self.assertEqual(roles.calls, [])
            self.assertEqual(sources.calls, [])
            service.grant("goal-1")
            service.run("goal-1", max_waves=1)
            self.assertEqual(roles.calls, [])
            self.assertEqual(sources.calls, [])

    def test_model_budget_refuses_next_external_call_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            service, roles, sources, _ = self.setup_service(Path(directory) / "db.sqlite", model_calls=1)
            grant = service.grant("goal-1")
            service.start("goal-1", grant_id=grant["grant_id"])
            view = service.run("goal-1", max_waves=2)
            self.assertEqual(view["goal"]["outcome"], "exhausted")
            self.assertEqual([r for r, _ in roles.calls], ["planner"])
            self.assertEqual(view["claims"], [])

    def test_unrelated_goals_use_reviewed_evidence_before_success(self):
        for objective in ("Compare queue semantics", "Research archive preservation"):
            with self.subTest(objective=objective), tempfile.TemporaryDirectory() as directory:
                service, roles, sources, _ = self.setup_service(Path(directory) / "db.sqlite", objective=objective)
                grant = service.grant("goal-1")
                service.start("goal-1", grant_id=grant["grant_id"])
                view = service.run("goal-1", max_waves=2)
                self.assertEqual(view["goal"]["outcome"], "rubric_met")
                self.assertEqual(view["claims"][0]["state"], "kept")
                self.assertIn("alpha", view["result"]["text"])
                self.assertEqual(sources.calls, ["https://example.org/source"])
                self.assertLess([r for r, _ in roles.calls].index("review"), [r for r, _ in roles.calls].index("judge"))

    def test_missing_independent_review_never_becomes_positive_support(self):
        with tempfile.TemporaryDirectory() as directory:
            service, roles, sources, _ = self.setup_service(Path(directory) / "db.sqlite", missing_review=True)
            grant = service.grant("goal-1")
            service.start("goal-1", grant_id=grant["grant_id"])
            view = service.run("goal-1", max_waves=1)
            self.assertNotEqual(view["goal"]["outcome"], "rubric_met")
            self.assertEqual(view["claims"][0]["state"], "unknown")

    def test_duplicate_planner_tasks_dispatch_source_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            service, roles, sources, _ = self.setup_service(Path(directory) / "db.sqlite")
            original = roles.call

            def duplicate_plan(role, payload, *, max_tokens=2048):
                result = original(role, payload, max_tokens=max_tokens)
                if role == "planner":
                    result["output"]["tasks"] *= 2
                return result

            roles.call = duplicate_plan
            grant = service.grant("goal-1")
            service.start("goal-1", grant_id=grant["grant_id"])
            view = service.run("goal-1", max_waves=1)
            self.assertEqual(sources.calls, ["https://example.org/source"])
            self.assertEqual(view["goal"]["outcome"], "rubric_met")

    def test_stop_during_fetch_prevents_new_model_dispatch_and_keeps_goal(self):
        from unittest.mock import patch
        from tools import generic_runtime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, roles, sources, _ = self.setup_service(root / "db.sqlite")
            original = sources.fetch

            def fetch_and_stop(url, *, max_bytes):
                (root / "STOP").touch()
                return original(url, max_bytes=max_bytes)

            sources.fetch = fetch_and_stop
            grant = service.grant("goal-1")
            service.start("goal-1", grant_id=grant["grant_id"])
            with patch.object(generic_runtime, "ROOT", root):
                view = service.run("goal-1", max_waves=2)
            self.assertEqual([role for role, _ in roles.calls], ["planner"])
            self.assertEqual(view["goal"]["state"], "running")
            self.assertEqual(view["claims"], [])

    def test_public_role_requests_exclude_private_context_and_internal_hash(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            roles, sources = ScenarioRoles(), ScenarioSources()
            service = Service(Path(directory) / "db.sqlite", roles=roles, acquisition=sources)
            created = service.create("Public question", "goal-1")
            payload = approved_payload("goal-1", "Public question")
            private = next(a for a in payload["answers"] if a["question_id"] == "constraints")
            private.update(value="PRIVATE-CANARY-827461", state="confirmed")
            version = contract.build_version(payload)
            service.approve("goal-1", payload, created["goal"]["row_version"], version["body_hash"])
            grant = service.grant("goal-1")
            service.start("goal-1", grant_id=grant["grant_id"])
            service.run("goal-1", max_waves=1)
            emitted = json.dumps(roles.calls)
            self.assertNotIn(private["value"], emitted)
            self.assertNotIn(version["body_hash"], emitted)
            self.assertIn("judge", [r for r, _ in roles.calls])

    def test_revision_during_fetch_preserves_history_without_semantic_commit(self):
        from tools import generic_store
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            service, roles, sources, _ = self.setup_service(path)
            original = sources.fetch

            def fetch_and_revise(url, *, max_bytes):
                service.revise("goal-1")
                return original(url, max_bytes=max_bytes)

            sources.fetch = fetch_and_revise
            grant = service.grant("goal-1")
            service.start("goal-1", grant_id=grant["grant_id"])
            view = service.run("goal-1", max_waves=1)
            self.assertEqual(view["goal"]["state"], "revising")
            self.assertEqual(view["claims"], [])
            self.assertNotIn("research", [r for r, _ in roles.calls])
            store = generic_store.open_store(path, readonly=True)
            try:
                self.assertEqual(store.conn.execute("SELECT count(*) FROM generic_snapshots").fetchone()[0], 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
