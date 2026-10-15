"""Interview opens without a model; consent is consumed exactly once per dispatch."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


interview = load("generic_interview")
contract = load("generic_contract")


def ceilings():
    return {
        "expansion_tasks": 2, "sources": 2, "source_bytes": 1000, "model_calls": 2,
        "tool_calls": 2, "model_tokens": 1000, "elapsed_sec": 60,
    }


def answer_draft(draft):
    for qid, value in (("objective", "Public objective"), ("deliverable", "A note")):
        question = next(row for row in draft["questions"] if row["id"] == qid)
        interview.apply_answer(draft, question, {
            "question_id": qid, "value": value, "state": "confirmed",
            "origin": "user_stated", "label": [],
        })
    for qid in ("constraints", "success", "limits", "disclosure"):
        question = next(row for row in draft["questions"] if row["id"] == qid)
        interview.apply_answer(draft, question, {
            "question_id": qid, "value": None, "state": "unknown" if question["required"] else "declined",
            "origin": "user_stated", "label": question["label"],
        })


class InterviewTest(unittest.TestCase):
    def test_opening_makes_no_model_call(self):
        called = []
        with self.assertRaises(interview.InterviewError) as caught:
            interview.new_draft("goal-1", model_call=lambda: called.append(1))
        self.assertEqual(caught.exception.field, "model")
        self.assertEqual(called, [])
        draft = interview.new_draft("goal-1")
        self.assertEqual([row["id"] for row in draft["questions"]], [row["id"] for row in contract.opening_questions()])

    def test_consent_consumption_decrements_and_binds_digest(self):
        draft = interview.new_draft("goal-1")
        question = draft["questions"][0]
        interview.apply_answer(draft, question, {
            "question_id": "objective", "value": "Public objective", "state": "confirmed",
            "origin": "user_stated", "label": [],
        })
        recipients = {"public_model": {"kind": "model", "attested": False, "clearance": []}}
        interview.record_consent(draft, "public_model", ["objective"], {"model_calls": 1}, recipients)
        consumed = interview.consume_consent(draft, "public_model")
        self.assertEqual(consumed["inputs"]["objective"]["value"], "Public objective")
        self.assertEqual(consumed["remaining"], 0)
        self.assertFalse(interview.consent_current(draft))
        with self.assertRaises(interview.InterviewError) as caught:
            interview.consume_consent(draft, "public_model")
        self.assertEqual(caught.exception.field, "allowance")

    def test_changed_input_invalidates_consent(self):
        draft = interview.new_draft("goal-1")
        question = draft["questions"][0]
        interview.apply_answer(draft, question, {
            "question_id": "objective", "value": "Public objective", "state": "confirmed",
            "origin": "user_stated", "label": [],
        })
        recipients = {"public_model": {"kind": "model", "attested": False, "clearance": []}}
        interview.record_consent(draft, "public_model", ["objective"], {"model_calls": 2}, recipients)
        self.assertTrue(interview.consent_current(draft))
        with self.assertRaises(interview.InterviewError):
            interview.record_consent(draft, "public_model", ["constraints"], {"model_calls": 1}, recipients)
        interview.apply_answer(draft, question, {
            "question_id": "objective", "value": "Changed objective", "state": "confirmed",
            "origin": "user_stated", "label": [],
        })
        self.assertFalse(interview.consent_current(draft))
        with self.assertRaises(interview.InterviewError) as caught:
            interview.consume_consent(draft, "public_model")
        self.assertEqual(caught.exception.field, "consent")

    def test_authorized_inputs_limited_to_exact_consented_refs(self):
        draft = interview.new_draft("goal-1")
        objective = draft["questions"][0]
        interview.apply_answer(draft, objective, {
            "question_id": "objective", "value": "Public objective", "state": "confirmed",
            "origin": "user_stated", "label": [],
        })
        deliverable = next(row for row in draft["questions"] if row["id"] == "deliverable")
        interview.apply_answer(draft, deliverable, {
            "question_id": "deliverable", "value": "A note", "state": "confirmed",
            "origin": "user_stated", "label": [],
        })
        constraints = next(row for row in draft["questions"] if row["id"] == "constraints")
        interview.apply_answer(draft, constraints, {
            "question_id": "constraints", "value": "under 5000 total", "state": "confirmed",
            "origin": "user_stated", "label": ["private"],
        })
        recipients = {"public_model": {"kind": "model", "attested": False, "clearance": []}}
        interview.record_consent(draft, "public_model", ["objective"], {"model_calls": 2}, recipients)
        view = interview.authorized_inputs(draft, "public_model", recipients)
        # Only the consented ref crosses.
        self.assertEqual(view["input_refs"], ["objective"])
        self.assertEqual(list(view["inputs"]), ["objective"])
        self.assertEqual(view["inputs"]["objective"]["value"], "Public objective")
        # An unconsented PUBLIC answer is withheld just like private material.
        self.assertNotIn("deliverable", view["inputs"])
        self.assertNotIn("A note", str(view))
        # Private question presence, count, and refusal metadata never cross.
        self.assertNotIn("constraints", str(view))
        self.assertNotIn("under 5000", str(view))
        self.assertNotIn("question_count", view)
        self.assertNotIn("declined", str(view))
        # Disclosure classes for followups come from the consented inputs only.
        self.assertEqual(view["input_labels"], [])
        wrong_recipient = {"other_model": {"kind": "model", "attested": False, "clearance": []}}
        with self.assertRaises(interview.InterviewError) as caught:
            interview.authorized_inputs(draft, "other_model", wrong_recipient)
        self.assertEqual(caught.exception.field, "consent")
        # A private recipient without its own consent is also refused.
        private_recipient = {"private_model": {"kind": "model", "attested": True, "clearance": ["private"]}}
        with self.assertRaises(interview.InterviewError):
            interview.authorized_inputs(draft, "private_model", private_recipient)

    def test_authorized_inputs_private_recipient_gets_consented_private_value(self):
        draft = interview.new_draft("goal-1")
        constraints = next(row for row in draft["questions"] if row["id"] == "constraints")
        interview.apply_answer(draft, constraints, {
            "question_id": "constraints", "value": "under 5000 total", "state": "confirmed",
            "origin": "user_stated", "label": ["private"],
        })
        private_model = {"private_model": {"kind": "model", "attested": True, "clearance": ["private"]}}
        interview.record_consent(draft, "private_model", ["constraints"], {"model_calls": 1}, private_model)
        view = interview.authorized_inputs(draft, "private_model", private_model)
        self.assertEqual(view["inputs"]["constraints"]["value"], "under 5000 total")
        self.assertEqual(view["input_labels"], ["private"])

    def test_followup_ids_unique_across_batches_and_labels_inherit_union(self):
        first = interview.validate_followups(
            [{"prompt": "Which period matters?", "required": False}],
            existing_ids=(),
            input_labels=["private"],
        )
        self.assertEqual(first[0]["id"], "q1")
        with self.assertRaises(interview.InterviewError):
            interview.validate_followups(
                [{"prompt": "Second batch?", "required": False}],
                existing_ids=("q1",),
                input_labels=[],
            )
        narrowed = interview.validate_followups(
            [{"prompt": "Another?", "required": False, "label": []}],
            existing_ids=(),
            input_labels=["private", "health"],
        )
        self.assertIn("private", narrowed[0]["label"])
        self.assertIn("health", narrowed[0]["label"])

    def test_followups_default_private(self):
        rows = interview.validate_followups([{"prompt": "Which period matters?", "required": False}])
        self.assertEqual(rows[0]["label"], ["private"])

    def test_proposal_has_no_grant_and_supports_revision(self):
        draft = interview.new_draft("goal-1")
        answer_draft(draft)
        proposal = interview.propose_contract(
            draft,
            public_id="ab" * 16,
            rubric={"criteria": [{
                "id": "supported", "description": "Deliverable is supported.", "required": True,
                "expected_evidence": "kept claims", "label": [], "qualifications": [],
            }]},
            brief={"text": "Public brief.", "derived_from": ["objective"], "label": []},
            ceilings=ceilings(),
            recipients={"public_model": {"kind": "model", "attested": False, "clearance": []}},
            seeds=[],
            concurrency=1,
            stopping_policy={"on_exhaustion": "partial"},
        )
        self.assertEqual(proposal["status"], "proposal")
        self.assertIsNone(proposal["grant_id"])
        self.assertTrue(proposal["version"]["body_hash"])
        revision = interview.propose_contract(
            draft,
            public_id="cd" * 16,
            rubric={"criteria": [{
                "id": "supported", "description": "Deliverable is supported.", "required": True,
                "expected_evidence": "kept claims", "label": [], "qualifications": [],
            }]},
            brief={"text": "Revised public brief.", "derived_from": ["objective"], "label": []},
            ceilings=ceilings(),
            recipients={"public_model": {"kind": "model", "attested": False, "clearance": []}},
            seeds=[],
            concurrency=1,
            stopping_policy={"on_exhaustion": "partial"},
            revision=2,
            parent_body_hash=proposal["version"]["body_hash"],
        )
        self.assertEqual(revision["payload"]["revision"], 2)
        self.assertEqual(revision["payload"]["parent_body_hash"], proposal["version"]["body_hash"])

    def test_interview_role_output_shape_is_structured(self):
        # The role prompt output contract: questions + rubric + brief, never auto-approved.
        raw = {
            "questions": [{"prompt": "Which queue?", "required": False}],
            "rubric": {"criteria": [{
                "id": "supported", "description": "d", "required": True,
                "expected_evidence": "kept claims", "label": [], "qualifications": [],
            }]},
            "brief": {"text": "b", "derived_from": ["objective"], "label": []},
        }
        questions = interview.validate_followups(raw["questions"], existing_ids=tuple(
            row["id"] for row in interview.new_draft("goal-1")["questions"]
        ))
        self.assertTrue(contract.validate_rubric(raw["rubric"]))
        self.assertTrue(all(isinstance(item, dict) for item in questions))
        self.assertEqual(len(questions), 1)


if __name__ == "__main__":
    unittest.main()
