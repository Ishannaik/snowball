"""Approval stores a locked version and does not start research."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


store_mod = load("generic_store")
contract = load("generic_contract")


def payload():
    return {
        "schema": contract.SCHEMA,
        "goal_id": "goal-1",
        "revision": 1,
        "parent_body_hash": None,
        "public_id": "cd" * 16,
        "questions": contract.opening_questions(),
        "answers": [
            {"question_id": "objective", "value": "Public objective", "state": "confirmed", "origin": "user_stated", "label": []},
            {"question_id": "deliverable", "value": "A note", "state": "confirmed", "origin": "user_stated", "label": []},
            {"question_id": "constraints", "value": None, "state": "declined", "origin": "user_stated", "label": ["private"]},
            {"question_id": "success", "value": None, "state": "unknown", "origin": "user_stated", "label": ["private"]},
            {"question_id": "limits", "value": "bounded", "state": "confirmed", "origin": "user_stated", "label": []},
            {"question_id": "disclosure", "value": None, "state": "unknown", "origin": "user_stated", "label": ["private"]},
        ],
        "rubric": {"criteria": [{
            "id": "supported", "description": "Deliverable is supported.", "required": True,
            "expected_evidence": "kept claims", "label": [], "qualifications": [],
        }]},
        "public_criterion_ids": ["supported"],
        "public_brief": {"text": "Public brief.", "derived_from": ["objective"], "label": []},
        "recipients": {"public_model": {"kind": "model", "attested": False, "clearance": []}},
        "seeds": [],
        "ceilings": {
            "expansion_tasks": 2, "sources": 2, "source_bytes": 1000, "model_calls": 2,
            "tool_calls": 2, "model_tokens": 1000, "elapsed_sec": 60,
        },
        "concurrency": 1,
        "stopping_policy": {"on_exhaustion": "partial"},
        "monitoring": None,
    }


class StoreTest(unittest.TestCase):
    def test_approval_does_not_grant_or_start(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_mod.open_store(Path(directory) / "goal.db")
            try:
                store.create_goal("goal-1", "Queue comparison")
                approved = store.approve("goal-1", payload(), 0)
                self.assertIsNone(approved["grant_id"])
                self.assertEqual(approved["goal"]["state"], "approved")
                granted = store.grant("goal-1", {"sources": 2})
                started = store.start("goal-1", granted["grant_id"])
                self.assertEqual(started["state"], "running")
                revised = store.revise("goal-1")
                self.assertEqual(revised["state"], "revising")
                self.assertEqual(revised["epoch"], 1)
            finally:
                store.close()

    def test_wrong_span_is_refused_after_start(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_mod.open_store(Path(directory) / "goal.db")
            try:
                store.create_goal("goal-1", "Queue comparison")
                store.approve("goal-1", payload(), 0)
                granted = store.grant("goal-1", {"sources": 2, "source_bytes": 100, "tool_calls": 2})
                store.start("goal-1", granted["grant_id"])
                binding = store.binding("goal-1")
                store.add_snapshot("goal-1", "s1", "alpha beta", "public_fetch", "public_fetch", binding=binding)
                with self.assertRaises(store_mod.StoreError):
                    store.add_claim("goal-1", {"id": "c1", "source_id": "s1", "quote": "gamma", "start": 0, "end": 5}, binding=binding)
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.add_snapshot("goal-1", "n1", "a note", "human_note", "public_fetch", binding=binding)
                self.assertEqual(caught.exception.field, "provenance")
            finally:
                store.close()

    def test_private_note_cannot_support_public_judgment(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_mod.open_store(Path(directory) / "goal.db")
            try:
                store.create_goal("goal-1", "Queue comparison")
                store.approve("goal-1", payload(), 0)
                granted = store.grant("goal-1", {"sources": 2, "source_bytes": 100, "tool_calls": 2})
                store.start("goal-1", granted["grant_id"])
                binding = store.binding("goal-1")
                store.add_snapshot("goal-1", "n1", "private note", "human_note", "human_note", binding=binding)
                store.add_claim("goal-1", {"id": "c1", "source_id": "n1", "quote": "private", "start": 0, "end": 7}, binding=binding)
                with self.assertRaises(store_mod.StoreError) as caught:
                    store.add_judgment("goal-1", {
                        "rows": [{"criterion_id": "supported", "verdict": "met", "support_ids": ["c1"], "reason": "The note asserts the claim."}],
                        "deliverable": {"supported": True, "reason": "The note is supported.", "evidence_ids": ["c1"]},
                    }, "public_model", binding=binding)
                self.assertEqual(caught.exception.field, "clearance")
            finally:
                store.close()

    def test_evidence_writes_are_refused_outside_running(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_mod.open_store(Path(directory) / "goal.db")
            try:
                store.create_goal("goal-1", "Queue comparison")
                store.approve("goal-1", payload(), 0)
                with self.assertRaises(store_mod.StoreError) as approved:
                    store.add_snapshot("goal-1", "s1", "alpha", "public_fetch", "public_fetch")
                self.assertEqual(approved.exception.field, "state")
                granted = store.grant("goal-1", {"sources": 1, "source_bytes": 20, "tool_calls": 1})
                store.start("goal-1", granted["grant_id"])
                store.revise("goal-1")
                with self.assertRaises(store_mod.StoreError) as revised:
                    store.add_claim("goal-1", {"id": "c1", "source_id": "s1", "quote": "alpha", "start": 0, "end": 5})
                self.assertEqual(revised.exception.field, "state")
            finally:
                store.close()

    def test_start_before_grant_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_mod.open_store(Path(directory) / "goal.db")
            try:
                store.create_goal("goal-1", "Queue comparison")
                store.approve("goal-1", payload(), 0)
                with self.assertRaises(store_mod.StoreError):
                    store.start("goal-1", 1)
                with self.assertRaises(store_mod.StoreError):
                    store.grant("goal-1", {"sources": 99})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
