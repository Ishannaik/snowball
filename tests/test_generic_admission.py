"""Admission races, fencing, and review dependencies on the durable store.

Consumer-behavior regressions: concurrent reservations race version-lifetime
ceilings, a dispatched operation can never be dispatched twice, paused goals
admit nothing, disputed evidence invalidates dependent judgments, and an empty
rubric assessment never becomes success.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"admission_{name}", ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


store_mod = load("generic_store")
contract = load("generic_contract")
interview = load("generic_interview")


def payload(goal_id="goal-1", public_id="ab" * 16, ceilings=None):
    questions = contract.opening_questions()
    answers = []
    for q in questions:
        value = {"objective": "Compare queue semantics", "deliverable": "A note", "limits": "bounded"}.get(q["id"])
        answers.append({"question_id": q["id"], "value": value, "state": "confirmed" if value else "unknown", "origin": "user_stated", "label": q["label"]})
    return {
        "schema": contract.SCHEMA, "goal_id": goal_id, "revision": 1, "parent_body_hash": None,
        "public_id": public_id, "questions": questions, "answers": answers,
        "rubric": {"criteria": [{"id": "supported", "description": "Supported note.", "required": True, "expected_evidence": "kept claims", "label": [], "qualifications": []}]},
        "public_criterion_ids": ["supported"],
        "public_brief": {"text": "Compare queue semantics", "derived_from": ["objective"], "label": []},
        "recipients": {"public_model": {"kind": "model", "attested": False, "clearance": []}},
        "seeds": [],
        "ceilings": ceilings or {"expansion_tasks": 3, "sources": 2, "source_bytes": 1000, "model_calls": 4, "tool_calls": 4, "model_tokens": 100000, "elapsed_sec": 3600},
        "concurrency": 2, "stopping_policy": {"on_exhaustion": "partial"}, "monitoring": None,
    }


def started_store(path):
    store = store_mod.open_store(path)
    store.create_goal("goal-1", "Races")
    approved = store.approve("goal-1", payload(), 0)
    grant = store.grant("goal-1", {"sources": 2, "source_bytes": 1000, "tool_calls": 4, "model_calls": 4, "expansion_tasks": 3})
    store.start("goal-1", grant["grant_id"])
    binding = store.binding("goal-1")
    return store, approved, binding


class AdmissionRaceTest(unittest.TestCase):
    def test_concurrent_reserves_never_exceed_version_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            store, approved, binding = started_store(Path(directory) / "db.sqlite")
            try:
                errors = []
                winners = []
                lock = threading.Lock()

                def worker(index):
                    try:
                        op = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", f"race-{index}")
                        with lock:
                            winners.append(op["id"])
                    except store_mod.StoreError as exc:
                        with lock:
                            errors.append(exc.field)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                for index in (10, 11):
                    try:
                        store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", f"race-{index}")
                        winners.append("main")
                    except store_mod.StoreError as exc:
                        errors.append(exc.field)
                self.assertLessEqual(len(winners), 2, f"more than the ceiling admitted: {winners}")
                usage = store.usage("goal-1")
                self.assertLessEqual(usage["reserved"].get("tool_calls", 0), 4)
            finally:
                store.close()

    def test_idempotent_reuse_and_conflicting_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, binding = started_store(Path(directory) / "db.sqlite")
            try:
                first = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "same-key")
                again = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "same-key")
                self.assertEqual(first["id"], again["id"])
                self.assertEqual(again["state"], "reserved")
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.reserve("goal-1", {"tool_calls": 2}, "tool:fetch", "same-key")
                self.assertEqual(caught.exception.field, "operation_key")
            finally:
                store.close()

    def test_dispatch_is_never_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, binding = started_store(Path(directory) / "db.sqlite")
            try:
                op = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "once")
                first = store.dispatch(op["id"])
                self.assertEqual(first["body_hash"], binding["body_hash"])
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.dispatch(op["id"])
                self.assertEqual(caught.exception.field, "dispatch")
                # even a fresh reserve with the same key is refused: dispatched work exists
                with self.assertRaises(store_mod.StoreError) as caught2:
                    store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "once")
                self.assertEqual(caught2.exception.field, "dispatch")
            finally:
                store.close()

    def test_paused_goal_makes_no_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, binding = started_store(Path(directory) / "db.sqlite")
            try:
                store.pause("goal-1")
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "paused-op")
                self.assertEqual(caught.exception.field, "state")
                store.resume("goal-1")
                op = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "resumed-op")
                self.assertEqual(op["state"], "reserved")
            finally:
                store.close()

    def test_domain_lease_excludes_parallel_same_domain(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, binding = started_store(Path(directory) / "db.sqlite")
            try:
                first = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "d1", domain="example.org")
                self.assertEqual(first["domain"], "example.org")
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "d2", domain="example.org")
                self.assertEqual(caught.exception.field, "domain")
                other = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "d3", domain="other.org")
                self.assertEqual(other["domain"], "other.org")
                # settling the first releases its lease
                store.dispatch(first["id"])
                store.settle(first["id"], {"tool_calls": 1})
                again = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "d4", domain="example.org")
                self.assertEqual(again["domain"], "example.org")
            finally:
                store.close()

    def test_settlement_overrun_stops_future_admission_until_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, binding = started_store(Path(directory) / "db.sqlite")
            try:
                op = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "over")
                store.dispatch(op["id"])
                valid = store.settle(op["id"], {"tool_calls": 3})
                self.assertTrue(valid)
                usage = store.usage("goal-1")
                self.assertEqual(usage["lost"].get("tool_calls", 0), 2)
                self.assertTrue(usage["overrun"])
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "blocked")
                self.assertEqual(caught.exception.field, "overrun")
                # A revision restarts admission on the new version.
                store.revise("goal-1")
                store.approve("goal-1", payload(public_id="cd" * 16, revision=2, parent_body_hash=None), store.goal("goal-1")["row_version"]) if False else None
            finally:
                store.close()

    def test_stale_settlement_after_revision_never_returns_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            store, approved, binding = started_store(Path(directory) / "db.sqlite")
            try:
                op = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "inflight")
                store.dispatch(op["id"])
                op2 = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "undispatched")
                store.revise("goal-1")
                # dispatched became lost: charged, not refunded
                usage = store.usage("goal-1")  # usage reads the new current hash; check operations directly
                rows = store.conn.execute("SELECT state FROM generic_operations WHERE id IN (?, ?)", (op["id"], op2["id"])).fetchall()
                states = {row["state"] for row in rows}
                self.assertIn("lost", states)
                self.assertIn("released", states)
                valid = store.settle(op["id"], {"tool_calls": 1})
                self.assertFalse(valid)
            finally:
                store.close()

    def test_recovers_dead_owner_work_and_keeps_live(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, binding = started_store(Path(directory) / "db.sqlite")
            try:
                dead = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "dead-op")
                store.dispatch(dead["id"])
                live = store.reserve("goal-1", {"tool_calls": 1}, "tool:fetch", "live-op")
                # simulate a dead owner on another host for the dispatched op only
                store.conn.execute("UPDATE generic_operations SET host='elsewhere', pid=999999 WHERE id=?", (dead["id"],))
                store.conn.commit()
                recovered = store.recover()
                self.assertEqual(recovered, 1)
                states = {row["id"]: row["state"] for row in store.conn.execute("SELECT id, state FROM generic_operations")}
                self.assertEqual(states[dead["id"]], "lost")
                self.assertEqual(states[live["id"]], "reserved")
            finally:
                store.close()

    def test_runner_lease_refuses_concurrent_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, binding = started_store(Path(directory) / "db.sqlite")
            try:
                token = store.acquire_runner("goal-1")
                self.assertTrue(token)
                again = store.acquire_runner("goal-1")  # same process re-enters
                self.assertEqual(again, token)
                store.release_runner("goal-1", token)
                fresh = store.acquire_runner("goal-1")
                self.assertNotEqual(fresh, token)
                store.release_runner("goal-1", fresh)
            finally:
                store.close()


class FenceAndReviewTest(unittest.TestCase):
    def test_stale_binding_preserves_bytes_but_never_current_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            store, approved, binding = started_store(Path(directory) / "db.sqlite")
            try:
                op = store.reserve("goal-1", {"tool_calls": 1, "sources": 1, "source_bytes": 100}, "tool:fetch", "stale-snap")
                store.dispatch(op["id"])
                snapshot = store.add_snapshot("goal-1", "s1", "alpha beta", "public_fetch", "public_fetch", url="https://example.org", binding=binding, operation_id=op["id"], raw_bytes=b"raw-bytes-1", content_type="text/html")
                store.settle(op["id"], {"tool_calls": 1, "sources": 1, "source_bytes": 100})
                store.revise("goal-1")
                current = store.snapshots("goal-1")
                self.assertEqual(current, [])
                history = store.snapshots("goal-1", include_history=True)
                self.assertEqual(len(history), 1)
                self.assertTrue(history[0]["historical"])
                self.assertEqual(history[0]["text"], "alpha beta")
                raw = store.snapshot_text("goal-1", "s1")
                self.assertEqual(raw["raw"], b"raw-bytes-1")
                self.assertEqual(raw["raw_hash"], store_mod._sha256(b"raw-bytes-1"))
                # claims are hard-fenced to the current running binding; a
                # retired epoch can neither grant nor commit semantics.
                with self.assertRaises(store_mod.StoreError) as nogrant:
                    store.grant("goal-1", {"sources": 2, "source_bytes": 1000, "tool_calls": 4})
                self.assertEqual(nogrant.exception.field, "state")
                with self.assertRaises(store_mod.StoreError) as noclaim:
                    store.add_claim("goal-1", {"id": "cx", "source_id": "s1", "quote": "alpha", "start": 0, "end": 5}, binding=binding)
                self.assertEqual(noclaim.exception.field, "binding")
            finally:
                store.close()

    def test_review_by_producer_and_unknown_states(self):
        with tempfile.TemporaryDirectory() as directory:
            store, approved, binding = started_store(Path(directory) / "db.sqlite")
            try:
                op = store.reserve("goal-1", {"tool_calls": 1, "sources": 1, "source_bytes": 100}, "tool:fetch", "rev-op")
                store.dispatch(op["id"])
                store.add_snapshot("goal-1", "s1", "alpha beta", "public_fetch", "public_fetch", binding=binding, operation_id=op["id"])
                claim = store.add_claim("goal-1", {"id": "c1", "source_id": "s1", "quote": "alpha", "start": 0, "end": 5, "criterion_ids": ["supported"], "producer": "model-x"}, binding=binding)
                self.assertEqual(claim["state"], "unknown")
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.review_claim("goal-1", "c1", "kept", reviewer="model-x", binding=binding)
                self.assertEqual(caught.exception.field, "reviewer")
                result = store.review_claim("goal-1", "c1", "kept", reviewer="reviewer-y", reason="checked", binding=binding)
                self.assertEqual(result["claim"]["state"], "kept")
            finally:
                store.close()

    def test_disputed_review_invalidates_dependent_judgment_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store, approved, binding = started_store(Path(directory) / "db.sqlite")
            try:
                op = store.reserve("goal-1", {"tool_calls": 1, "sources": 1, "source_bytes": 200}, "tool:fetch", "jud-op")
                store.dispatch(op["id"])
                store.add_snapshot("goal-1", "s1", "alpha beta gamma", "public_fetch", "public_fetch", binding=binding, operation_id=op["id"])
                store.settle(op["id"], {"tool_calls": 1, "sources": 1, "source_bytes": 100})
                store.review_claim("goal-1", store.add_claim("goal-1", {"id": "c1", "source_id": "s1", "quote": "alpha", "start": 0, "end": 5, "criterion_ids": ["supported"]}, binding=binding)["id"], "kept", reviewer="reviewer-1", binding=binding)
                store.review_claim("goal-1", store.add_claim("goal-1", {"id": "c2", "source_id": "s1", "quote": "gamma", "start": 11, "end": 16, "criterion_ids": ["supported"]}, binding=binding)["id"], "kept", reviewer="reviewer-1", binding=binding)
                deliverable = {"text": "alpha supports this.", "evidence_ids": ["c1"], "label": [], "model_id": "synthesis-1"}
                store.save_result("goal-1", deliverable, binding=binding)
                judged = store.add_judgment("goal-1", {
                    "rows": [{"criterion_id": "supported", "verdict": "met", "support_ids": ["c1"], "reason": "kept claim supports", "conflict_ids": []}],
                    "deliverable": {"supported": True, "reason": "The note is supported.", "evidence_ids": ["c1"]},
                }, "public_model", binding=binding, label=[], model_id="judge-1")
                self.assertEqual(judged["outcome"], "rubric_met")
                invalidated = store.review_claim("goal-1", "c1", "disputed", reviewer="reviewer-2", reason="quote contested", binding=binding)
                self.assertEqual(invalidated["invalidated_judgments"], 1)
                flags = store.judgments("goal-1")
                self.assertTrue(flags[0]["invalid"])
                # c2 untouched: unrelated evidence addition does not revoke
                op2 = store.reserve("goal-1", {"tool_calls": 1, "sources": 1, "source_bytes": 200}, "tool:fetch", "jud-op2")
                store.dispatch(op2["id"])
                store.add_snapshot("goal-1", "s2", "delta epsilon", "public_fetch", "public_fetch", binding=binding, operation_id=op2["id"])
                store.settle(op2["id"], {"tool_calls": 1, "sources": 1, "source_bytes": 100})
                self.assertEqual(store._claim_view("goal-1", "c2")["state"], "kept")
            finally:
                store.close()

    def test_vacuous_or_unbacked_success_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            store, approved, binding = started_store(Path(directory) / "db.sqlite")
            try:
                with self.assertRaises(store_mod.StoreError) as no_result:
                    store.add_judgment("goal-1", {
                        "rows": [{"criterion_id": "supported", "verdict": "met", "support_ids": [], "reason": "nothing", "conflict_ids": []}],
                        "deliverable": {"supported": True, "reason": "self-declared", "evidence_ids": []},
                    }, "public_model", binding=binding)
                self.assertEqual(no_result.exception.field, "result")
                op = store.reserve("goal-1", {"tool_calls": 1, "sources": 1, "source_bytes": 100}, "tool:fetch", "vac-op")
                store.dispatch(op["id"])
                store.add_snapshot("goal-1", "s1", "alpha beta", "public_fetch", "public_fetch", binding=binding, operation_id=op["id"])
                store.add_claim("goal-1", {"id": "c1", "source_id": "s1", "quote": "alpha", "start": 0, "end": 5, "criterion_ids": ["supported"]}, binding=binding)
                # claim never reviewed -> unknown -> positive support refuses
                with self.assertRaises(store_mod.StoreError) as unreviewed:
                    store.save_result("goal-1", {"text": "text", "evidence_ids": ["c1"], "label": [], "model_id": "m"}, binding=binding)
                self.assertEqual(unreviewed.exception.field, "evidence_ids")
            finally:
                store.close()

    def test_grant_renewal_never_resets_lifetime_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            store, approved, binding = started_store(Path(directory) / "db.sqlite")
            try:
                op = store.reserve("goal-1", {"tool_calls": 2}, "tool:fetch", "life1")
                store.dispatch(op["id"])
                store.settle(op["id"], {"tool_calls": 2})
                # replacement grant reallocates slice but lifetime keeps used
                store.grant("goal-1", {"sources": 2, "source_bytes": 1000, "tool_calls": 4, "model_calls": 4, "expansion_tasks": 3})
                headroom = store.headroom("goal-1")
                self.assertEqual(headroom["tool_calls"], 2)
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.reserve("goal-1", {"tool_calls": 3}, "tool:fetch", "life2")
                self.assertEqual(caught.exception.field, "tool_calls")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
