"""Quotes are located by unique literal occurrence; offsets are advisory only."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("generic_evidence", ROOT / "tools" / "generic_evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class EvidenceTest(unittest.TestCase):
    def test_exact_span_is_required_when_correct(self):
        text = "alpha beta gamma"
        checked = evidence.verify_quote(text, "beta", 6, 10)
        self.assertEqual(len(checked["quote_hash"]), 64)
        self.assertEqual(checked["start"], 6)
        self.assertEqual(checked["end"], 10)
        self.assertIn("snapshot_hash", checked)

    def test_wrong_offsets_are_derived_from_unique_literal(self):
        text = "alpha beta gamma"
        checked = evidence.verify_quote(text, "beta", 0, 4)
        self.assertEqual((checked["start"], checked["end"]), (6, 10))

    def test_absent_offsets_derive_span(self):
        text = "alpha beta gamma"
        checked = evidence.verify_quote(text, "gamma")
        self.assertEqual((checked["start"], checked["end"]), (11, 16))

    def test_absent_quote_fails_without_fuzzy_match(self):
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_quote("alpha beta gamma", "beta gamma plus", None, None)
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_quote("not here", "beta", None, None)

    def test_ambiguous_quote_requires_exact_span(self):
        text = "tick tock tick"
        checked = evidence.verify_quote(text, "tick", 0, 4)
        self.assertEqual(checked["start"], 0)
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_quote(text, "tick", None, None)

    def test_human_note_cannot_masquerade_as_fetch(self):
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.review_claim({
                "id": "n1", "provenance": "human_note", "source_kind": "public_fetch",
                "quote_hash": "abc", "kind": "source",
            }, "kept")
        self.assertEqual(caught.exception.field, "provenance")

    def test_reasoning_needs_kept_basis(self):
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.review_claim(
                {"id": "r1", "kind": "reasoning", "provenance": "model", "basis_ids": ["c1"]},
                "kept",
                [{"id": "c1", "state": "rejected"}],
            )
        self.assertEqual(caught.exception.field, "basis_ids")

    def test_review_states_are_verdict_controls(self):
        with self.assertRaises(evidence.EvidenceError):
            evidence.review_claim({"id": "c1", "quote_hash": "a"}, "approved_by_producer")
        receipt = evidence.review_claim({"id": "c1", "quote_hash": "a"}, "unknown")
        self.assertEqual(receipt["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
