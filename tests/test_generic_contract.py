"""Generic goal contract after the Opus privacy corrections."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("generic_contract", ROOT / "tools" / "generic_contract.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PUBLIC_ID = "ab" * 16


def question(qid, *, required=True, label=None, input_type="text", choices=None):
    row = {
        "id": qid,
        "prompt": f"Question {qid}",
        "input_type": input_type,
        "required": required,
        "label": [] if label is None else label,
    }
    if choices is not None:
        row["choices"] = choices
    return row


def answer(qid, value, state="confirmed", origin="user_stated", label=None, confirmation_ref=None):
    row = {
        "question_id": qid,
        "value": value,
        "state": state,
        "origin": origin,
        "label": ["private"] if label is None else label,
    }
    if confirmation_ref is not None:
        row["confirmation_ref"] = confirmation_ref
    return row


def ceilings():
    return {
        "expansion_tasks": 4,
        "sources": 8,
        "source_bytes": 200000,
        "model_calls": 12,
        "tool_calls": 12,
        "model_tokens": 20000,
        "elapsed_sec": 600,
    }


def payload(objective, *, private_value="secret-preference-value", private_state="confirmed"):
    return {
        "schema": "generic-goal-1",
        "goal_id": "goal-1",
        "revision": 1,
        "parent_body_hash": None,
        "public_id": PUBLIC_ID,
        "questions": [
            question("objective", label=[]),
            question("deliverable", label=[]),
            question("private_note", required=False, label=["private"]),
        ],
        "answers": [
            answer("objective", objective, label=[], origin="user_stated"),
            answer("deliverable", "A sourced research note.", label=[], origin="user_stated"),
            answer("private_note", None if private_state != "confirmed" else private_value, state=private_state, label=["private"]),
        ],
        "rubric": {
            "criteria": [
                {
                    "id": "supported",
                    "description": "The requested deliverable is supported by kept evidence.",
                    "required": True,
                    "expected_evidence": "kept claims",
                    "label": [],
                    "qualifications": [{"id": "dated", "text": "Sources are current for the stated period."}],
                },
                {
                    "id": "private_fit",
                    "description": "A private constraint is satisfied.",
                    "required": True,
                    "expected_evidence": "private notes",
                    "label": ["private"],
                    "qualifications": [],
                },
            ]
        },
        "public_criterion_ids": ["supported"],
        "public_brief": {
            "text": "Public research brief.",
            "derived_from": ["objective", "deliverable"],
            "label": [],
        },
        "recipients": {
            "public_model": {"kind": "model", "attested": False, "clearance": []},
        },
        "seeds": [{"id": "seed-1", "value": "https://example.test", "label": []}],
        "ceilings": ceilings(),
        "concurrency": 2,
        "stopping_policy": {"on_exhaustion": "partial", "on_empty_frontier": "partial", "stall_checkpoints": 2},
        "monitoring": None,
    }


class GenericContractTest(unittest.TestCase):
    def test_two_unrelated_goals_need_no_domain_schema(self):
        funding = payload("Find public funding options.")
        funding["questions"][2]["input_type"] = "object"
        funding["answers"][2]["value"] = {"tuition": "runtime context, not schema"}
        software = payload("Compare two open-source queue designs.")
        for row in (funding, software):
            version = MODULE.build_version(row)
            self.assertNotIn("degree_level", version["body"])
            self.assertEqual(version["body"]["ceilings"]["sources"], 8)
            self.assertEqual(version["body"]["goal_id"], "goal-1")

    def test_unconfirmed_import_blocks_approval(self):
        row = payload("Public objective", private_state="unconfirmed")
        row["answers"][2]["origin"] = "imported_history"
        with self.assertRaises(MODULE.ContractError) as caught:
            MODULE.build_version(row)
        self.assertEqual(caught.exception.field, "private_note")
        self.assertIn("unconfirmed", caught.exception.message)

    def test_private_change_does_not_change_public_hash(self):
        first = MODULE.build_version(payload("Public objective", private_value="one"))
        second = MODULE.build_version(payload("Public objective", private_value="two"))
        self.assertEqual(first["public_hash"], second["public_hash"])
        self.assertNotIn("one", MODULE.canonical(first["public"]))
        self.assertNotIn(first["body_hash"], MODULE.canonical(first["public"]))
        self.assertNotIn("private_fit", MODULE.canonical(first["public"]))
        self.assertNotEqual(first["public_id"], first["body_hash"][:32])
        self.assertNotIn(first["public_id"], MODULE.canonical(first["public"]))

    def test_public_brief_change_changes_public_hash(self):
        first = MODULE.build_version(payload("Public objective"))
        changed = payload("Public objective")
        changed["public_brief"]["text"] = "A different public brief."
        second = MODULE.build_version(changed)
        self.assertNotEqual(first["public_hash"], second["public_hash"])

    def test_opening_is_domain_neutral(self):
        ids = [item["id"] for item in MODULE.opening_questions()]
        self.assertEqual(ids, ["objective", "deliverable", "constraints", "success", "limits", "disclosure"])


if __name__ == "__main__":
    unittest.main()
