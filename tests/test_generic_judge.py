"""Rubric-met follows validated rows, deliverable support, and conflict honesty."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("generic_judge", ROOT / "tools" / "generic_judge.py")
judge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge)

RUBRIC = {"criteria": [
    {"id": "supported", "required": True, "qualifications": [{"id": "dated", "text": "dated"}]},
    {"id": "private_fit", "required": True, "qualifications": []},
]}
CLAIMS = [
    {"id": "c1", "state": "kept", "quote_hash": "aaa"},
    {"id": "c2", "state": "disputed", "quote_hash": "bbb"},
]


def row(cid, verdict, support=("c1",), **extra):
    base = {
        "criterion_id": cid, "verdict": verdict, "reason": "because",
        "support_ids": list(support), "conflict_ids": [], "missing_evidence": None,
        "next_questions": None, "qualification_id": None,
    }
    base.update(extra)
    return base


class JudgeTest(unittest.TestCase):
    def test_public_scope_cannot_meet_private_criterion(self):
        result = judge.validate_judge_result(
            RUBRIC,
            {"rows": [row("supported", "unknown", support=(), missing_evidence="no public evidence")]},
            CLAIMS,
            ["supported"],
        )
        self.assertFalse(judge.rubric_met(RUBRIC, result, ["supported"]))

    def test_full_positive_requires_deliverable_support(self):
        result = judge.validate_judge_result(
            RUBRIC,
            {"rows": [
                row("supported", "met"),
                row("private_fit", "met", support=("c1",)),
            ]},
            CLAIMS,
            ["supported", "private_fit"],
            {"supported": True, "reason": "note cites kept claims", "evidence_ids": ["c1"]},
        )
        self.assertTrue(judge.rubric_met(RUBRIC, result, ["supported", "private_fit"]))
        lacking = judge.validate_judge_result(
            RUBRIC,
            {"rows": [row("supported", "met"), row("private_fit", "met", support=("c1",))]},
            CLAIMS,
            ["supported", "private_fit"],
            {"supported": False, "reason": "deliverable not assembled", "evidence_ids": ["c1"]},
        )
        self.assertFalse(judge.rubric_met(RUBRIC, lacking, ["supported", "private_fit"]))

    def test_positive_without_deliverable_check_is_refused(self):
        with self.assertRaises(judge.JudgeError) as caught:
            judge.validate_judge_result(
                RUBRIC,
                {"rows": [row("supported", "met"), row("private_fit", "met", support=("c1",))]},
                CLAIMS,
                ["supported", "private_fit"],
            )
        self.assertEqual(caught.exception.field, "deliverable")

    def test_disputed_claim_cannot_support_met(self):
        with self.assertRaises(judge.JudgeError) as caught:
            judge.validate_judge_result(
                RUBRIC,
                {"rows": [row("supported", "met", support=("c2",))]},
                CLAIMS,
                ["supported", "private_fit"],
            )
        self.assertEqual(caught.exception.field, "supported")

    def test_contested_needs_conflict_references(self):
        with self.assertRaises(judge.JudgeError) as caught:
            judge.validate_judge_result(
                RUBRIC,
                {"rows": [row("supported", "contested", conflict_ids=[])]},
                CLAIMS,
                ["supported", "private_fit"],
            )
        self.assertEqual(caught.exception.field, "supported")

    def test_unknown_rows_carry_missing_evidence(self):
        with self.assertRaises(judge.JudgeError) as caught:
            judge.validate_judge_result(
                RUBRIC,
                {"rows": [
                    row("supported", "unknown", support=(), missing_evidence="  "),
                    row("private_fit", "unknown", support=(), missing_evidence="no private input assessed"),
                ]},
                CLAIMS,
                ["supported", "private_fit"],
            )
        self.assertEqual(caught.exception.field, "supported")

    def test_vacuous_empty_assessment_is_refused(self):
        result = judge.validate_judge_result(
            RUBRIC,
            {"rows": [row("supported", "met")] + [row("private_fit", "met", support=["c1"])]},
            CLAIMS,
            ["supported", "private_fit"],
            {"supported": True, "reason": "r", "evidence_ids": ["c1"]},
        )
        self.assertTrue(result["rows"])
        with self.assertRaises(judge.JudgeError) as caught:
            judge.validate_judge_result(RUBRIC, {"rows": []}, CLAIMS, ["supported"])
        self.assertEqual(caught.exception.field, "rows")

    def test_conditional_requires_accepted_qualification(self):
        with self.assertRaises(judge.JudgeError):
            judge.validate_judge_result(
                RUBRIC,
                {"rows": [row("supported", "conditional", qualification_id="unaccepted"), row("private_fit", "unmet")]},
                CLAIMS,
                ["supported", "private_fit"],
            )
        clean = judge.validate_judge_result(
            RUBRIC,
            {"rows": [row("supported", "conditional", qualification_id="dated"), row("private_fit", "unmet", missing_evidence="no private input")]},
            CLAIMS,
            ["supported", "private_fit"],
            {"supported": False, "reason": "conditional support is not full support", "evidence_ids": ["c1"]},
        )
        self.assertEqual(clean["rows"][0]["verdict"], "conditional")

    def test_duplicate_and_out_of_scope_rows_are_refused(self):
        with self.assertRaises(judge.JudgeError):
            judge.validate_judge_result(
                RUBRIC,
                {"rows": [row("supported", "met"), row("supported", "unmet")]},
                CLAIMS,
                ["supported", "private_fit"],
            )
        with self.assertRaises(judge.JudgeError):
            judge.validate_judge_result(
                RUBRIC,
                {"rows": [row("ghost", "met")]},
                CLAIMS,
                ["supported", "private_fit"],
            )

    def test_fingerprint_ignores_uncited_claim(self):
        extra = CLAIMS + [{"id": "c3", "state": "kept", "quote_hash": "ccc"}]
        result = judge.validate_judge_result(
            RUBRIC,
            {"rows": [row("supported", "met"), row("private_fit", "met", support=["c1"])]},
            extra,
            ["supported", "private_fit"],
            {"supported": True, "reason": "note cites kept claims", "evidence_ids": ["c1"]},
        )
        self.assertEqual(result["fingerprint"], judge.evidence_fingerprint([CLAIMS[0]]))
        self.assertNotEqual(result["fingerprint"], judge.evidence_fingerprint(extra))

    def test_conflict_citations_enter_fingerprint(self):
        first = judge.validate_judge_result(
            RUBRIC,
            {"rows": [
                row("supported", "contested", support=["c1"], conflict_ids=["c2"]),
                row("private_fit", "unknown", support=[], missing_evidence="no private input"),
            ]},
            CLAIMS,
            ["supported", "private_fit"],
        )
        self.assertEqual(first["fingerprint"], judge.evidence_fingerprint([CLAIMS[0], CLAIMS[1]]))


if __name__ == "__main__":
    unittest.main()
