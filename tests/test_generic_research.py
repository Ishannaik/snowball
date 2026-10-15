"""Planned work stays inside public provenance; synthesis cannot declare success."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("generic_research", ROOT / "tools" / "generic_research.py")
research = importlib.util.module_from_spec(spec)
spec.loader.exec_module(research)

RUBRIC = {"criteria": [{"id": "supported"}, {"id": "other"}]}


class ResearchTest(unittest.TestCase):
    def test_shell_task_is_rejected(self):
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_task({
                "criterion_id": "supported", "tool": "shell", "query_text": "bounded queues",
                "public_context_ids": ["objective"],
            }, RUBRIC, {"objective": {"text": "bounded queues", "label": []}})
        self.assertEqual(caught.exception.field, "tool")

    def test_private_context_is_rejected(self):
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_task({
                "criterion_id": "supported", "tool": "search", "query_text": "bounded queues",
                "public_context_ids": ["objective"],
            }, RUBRIC, {"objective": {"text": "bounded queues", "label": ["private"]}})
        self.assertEqual(caught.exception.field, "public_context_ids")

    def test_strict_mode_requires_literal_context(self):
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_task({
                "criterion_id": "supported", "tool": "search",
                "query_text": "queue bounding comparisons 2026",
                "public_context_ids": ["objective"],
            }, RUBRIC, {"objective": {"text": "bounded queues", "label": []}})
        self.assertEqual(caught.exception.field, "query_text")

    def test_hypothesis_mode_needs_typed_origin_refs(self):
        ok = research.validate_task({
            "criterion_id": "supported", "tool": "search",
            "query_text": "queue bounding comparisons 2026",
            "public_context_ids": ["objective"],
            "origin_refs": ["objective"],
        }, RUBRIC, {"objective": {"text": "bounded queues", "label": []}}, provenance_mode="hypothesis")
        self.assertEqual(ok["label"], [])
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_task({
                "criterion_id": "supported", "tool": "search",
                "query_text": "queue bounding comparisons 2026",
                "public_context_ids": ["objective"],
            }, RUBRIC, {"objective": {"text": "bounded queues", "label": []}}, provenance_mode="hypothesis")
        self.assertEqual(caught.exception.field, "origin_refs")

    def test_hypothesis_mode_rejects_private_origins(self):
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_task({
                "criterion_id": "supported", "tool": "search",
                "query_text": "compare queue designs",
                "public_context_ids": ["note"],
                "origin_refs": ["note"],
            }, RUBRIC, {"note": {"text": "private queue note", "label": ["private"]}}, provenance_mode="hypothesis")
        self.assertEqual(caught.exception.field, "public_context_ids")
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_task({
                "criterion_id": "supported", "tool": "search",
                "query_text": "compare queue designs",
                "exploration_question": "what do the private notes say",
                "public_context_ids": [],
            }, RUBRIC, {}, provenance_mode="hypothesis")
        self.assertEqual(caught.exception.field, "query_text")

    def test_private_exploration_text_is_rejected_in_strict_mode(self):
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_task({
                "criterion_id": "supported", "tool": "search", "query_text": "bounded queues",
                "exploration_question": "private funding note", "public_context_ids": ["objective"],
            }, RUBRIC, {"objective": {"text": "bounded queues", "label": []}})
        self.assertEqual(caught.exception.field, "exploration_question")

    def test_fetch_task_needs_a_target(self):
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_task({
                "criterion_id": "supported", "tool": "fetch", "query_text": "bounded queues",
                "public_context_ids": ["objective"],
            }, RUBRIC, {"objective": {"text": "bounded queues", "label": []}})
        self.assertEqual(caught.exception.field, "tool")

    def test_synthesis_cannot_set_rubric_met(self):
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_synthesis({
                "rubric_met": True, "evidence_ids": ["c1"], "context_class": "public",
                "model_id": "fixture", "goal_id": "goal-1", "version_id": "v1",
            }, [{"id": "c1", "state": "kept", "label": []}])
        self.assertEqual(caught.exception.field, "rubric_met")

    def test_synthesis_only_cites_kept_claims(self):
        with self.assertRaises(research.ResearchError) as caught:
            research.validate_synthesis({
                "evidence_ids": ["c2"], "context_class": "public",
                "model_id": "fixture", "goal_id": "goal-1", "version_id": "v1",
            }, [
                {"id": "c1", "state": "kept", "label": []},
                {"id": "c2", "state": "rejected", "label": []},
            ])
        self.assertEqual(caught.exception.field, "evidence_ids")
        clean = research.validate_synthesis({
            "evidence_ids": ["c1"], "context_class": "public",
            "model_id": "fixture", "goal_id": "goal-1", "version_id": "v1",
        }, [{"id": "c1", "state": "kept", "label": []}])
        self.assertEqual(clean["label"], [])
        self.assertIs(clean["rubric_met"], False)

    def test_negative_evidence_does_not_repeat_exhausted_task(self):
        action = research.next_action(
            [{"criterion_id": "supported", "verdict": "unmet", "support_ids": ["c1"]}],
            [{"id": "t1", "criterion_id": "supported"}, {"id": "t2", "criterion_id": "other"}],
        )
        self.assertEqual(action["task"]["id"], "t2")
        self.assertEqual(action["status"], "progressing")

    def test_stop_reasons_yield_partial_not_success(self):
        for reason in ("limit", "outage", "pause", "stall"):
            action = research.next_action([], [{"id": "t1", "criterion_id": "supported"}], reason=reason)
            self.assertEqual(action["status"], "partial")
            self.assertIsNone(action["task"])
        action = research.next_action([], [], reason=None)
        self.assertEqual(action["reason"], "empty_frontier")


if __name__ == "__main__":
    unittest.main()
