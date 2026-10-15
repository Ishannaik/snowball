"""Code-rendered interview. Follow-ups are proposals until the user confirms them."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _contract():
    key = "snowball_generic_contract"
    existing = sys.modules.get(key)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(key, ROOT / "tools" / "generic_contract.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


class InterviewError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def new_draft(goal_id: str, model_call: Any = None) -> dict[str, Any]:
    contract = _contract()
    if not isinstance(goal_id, str) or not goal_id:
        raise InterviewError("goal_id", "goal id is required")
    if model_call is not None:
        raise InterviewError("model", "opening is rendered without a model call")
    return {
        "goal_id": goal_id,
        "questions": contract.opening_questions(),
        "answers": [],
        "consent": None,
        "model_calls": 0,
        "status": "open",
    }


def record_consent(draft: dict[str, Any], recipient: str, input_refs: list[str], allowance: dict[str, int], recipients: dict[str, Any]) -> dict[str, Any]:
    contract = _contract()
    if recipient not in recipients or not isinstance(recipients[recipient], dict):
        raise InterviewError("recipient", "recipient is not approved")
    clearance = set(recipients[recipient].get("clearance") or [])
    attested = recipients[recipient].get("attested") is True
    if not isinstance(allowance, dict) or allowance.get("model_calls", 0) < 1:
        raise InterviewError("allowance", "interview consent needs a finite call allowance")
    known = {row["id"]: row for row in draft["questions"]}
    if not input_refs or any(ref not in known for ref in input_refs):
        raise InterviewError("input_refs", "consent must name existing questions")
    for ref in input_refs:
        answer = next((row for row in draft["answers"] if row["question_id"] == ref), None)
        label = set((answer or known[ref]).get("label") or [])
        if not label <= clearance or (label and not attested):
            raise InterviewError(ref, "recipient clearance does not cover this answer")
    snapshot = {ref: next((row for row in draft["answers"] if row["question_id"] == ref), None) for ref in input_refs}
    draft["consent"] = {
        "recipient": recipient,
        "input_refs": json_unique(list(input_refs)),
        "allowance": dict(allowance),
        "input_digest": contract.digest(snapshot),
        "used": 0,
    }
    return draft


def json_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def consent_current(draft: dict[str, Any]) -> bool:
    consent = draft.get("consent")
    if not consent:
        return False
    contract = _contract()
    snapshot = {ref: next((row for row in draft["answers"] if row["question_id"] == ref), None) for ref in consent["input_refs"]}
    return contract.digest(snapshot) == consent["input_digest"] and consent["used"] < consent["allowance"]["model_calls"]


def consume_consent(draft: dict[str, Any], recipient: str) -> dict[str, Any]:
    """Check the digest and decrement the finite allowance BEFORE any dispatch.

    Returns the exact authorized input snapshot the dispatch may carry. Mutating
    answers afterwards keeps the consumed call durably accounted for.
    """
    contract = _contract()
    consent = draft.get("consent")
    if not consent or consent.get("recipient") != recipient:
        raise InterviewError("consent", "no active consent for this recipient")
    snapshot = {ref: next((row for row in draft["answers"] if row["question_id"] == ref), None) for ref in consent["input_refs"]}
    if contract.digest(snapshot) != consent["input_digest"]:
        raise InterviewError("consent", "authorized inputs changed; the prior consent is void")
    if consent["used"] >= consent["allowance"]["model_calls"]:
        raise InterviewError("allowance", "interview call allowance is exhausted")
    consent["used"] += 1
    draft["model_calls"] = int(draft.get("model_calls") or 0) + 1
    return {
        "recipient": recipient,
        "inputs": snapshot,
        "allowance": dict(consent["allowance"]),
        "used": consent["used"],
        "remaining": consent["allowance"]["model_calls"] - consent["used"],
        "input_digest": consent["input_digest"],
    }


def authorized_inputs(draft: dict[str, Any], recipient: str, recipients: dict[str, Any]) -> dict[str, Any]:
    """Exact consented inputs for a model dispatch; nothing else crosses.

    Only `draft["consent"]["input_refs"]` answers are emitted: an unconsented
    public answer, a private question's presence, or the total question count
    never appear. Consent must name the same recipient and match the current
    answer digest.
    """
    contract = _contract()
    if recipient not in recipients or not isinstance(recipients[recipient], dict):
        raise InterviewError("recipient", "recipient is not approved")
    row = recipients[recipient]
    clearance = set(row.get("clearance") or [])
    attested = row.get("attested") is True
    consent = draft.get("consent")
    if not consent or consent.get("recipient") != recipient:
        raise InterviewError("consent", "no active consent for this recipient")
    refs = list(consent.get("input_refs") or [])
    by_ref = {answer["question_id"]: answer for answer in draft["answers"]}
    known = {question["id"]: question for question in draft["questions"]}
    if any(ref not in known for ref in refs):
        raise InterviewError("consent", "consent references an unknown question")
    inputs: dict[str, dict[str, Any]] = {}
    labels: list[str] = []
    for ref in refs:
        answer = by_ref.get(ref)
        question = known[ref]
        label = set((answer or question).get("label") or [])
        if not label <= clearance or (label and not attested):
            raise InterviewError(ref, "recipient clearance does not cover this consented input")
        payload: dict[str, Any] = {"question_id": ref, "state": answer["state"] if answer else "unanswered", "label": sorted(label)}
        if answer is None:
            # The question definition itself is consented material.
            payload["prompt"] = question["prompt"]
            payload["input_type"] = question["input_type"]
        elif answer["state"] == "confirmed":
            payload["value"] = answer["value"]
        elif answer["state"] == "unknown":
            payload["value"] = None
        inputs[ref] = payload
        labels.extend(label)
    return {
        "recipient": recipient,
        "clearance": sorted(clearance),
        "attested": attested,
        "inputs": inputs,
        "input_labels": sorted(set(labels)),
        "input_refs": refs,
        "input_digest": consent.get("input_digest"),
    }


def apply_answer(draft: dict[str, Any], question: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    contract = _contract()
    stored = next((row for row in draft["questions"] if row["id"] == question.get("id")), None)
    clean_question = contract.validate_question(stored or question)
    clean_answer = contract.validate_answer(clean_question, {**answer, "question_id": clean_question["id"]})
    if stored is None:
        draft["questions"].append(clean_question)
    draft["answers"] = [row for row in draft["answers"] if row["question_id"] != clean_answer["question_id"]]
    draft["answers"].append(clean_answer)
    return draft


def validate_followups(raw: list[dict[str, Any]], existing_ids: tuple[str, ...] | list[str] = (), input_labels: list[str] | None = None) -> list[dict[str, Any]]:
    """Validate model-proposed follow-up questions.

    - IDs are opaque q1..qn per batch; they must be unique across batches via
      existing_ids, since each caller supplies the ids already stored.
    - Input labels are the disclosure context of what produced the question;
      a follow-up inherits the UNION of those labels — model rewriting never
      declassifies.
    """
    contract = _contract()
    if not isinstance(raw, list):
        raise InterviewError("followups", "follow-ups must be a list")
    existing = {row_id for row_id in existing_ids}
    clean = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("prompt"), str):
            raise InterviewError("followups", "follow-up prompt is required")
        proposed_id = f"q{index}"
        if proposed_id in existing:
            raise InterviewError(proposed_id, "follow-up id collides with a stored batch")
        labels: set[str] = set(input_labels or [])
        if isinstance(item.get("label"), list):
            # Model narrowing never declassifies: the union always holds.
            labels |= {str(row) for row in item["label"]}
        proposed = {
            "id": proposed_id,
            "prompt": item["prompt"],
            "input_type": item.get("input_type", "text"),
            "required": bool(item.get("required", False)),
            "label": sorted(labels) or ["private"],
        }
        if "choices" in item:
            proposed["choices"] = item["choices"]
        clean.append(contract.validate_question(proposed))
    return clean


def propose_contract(draft: dict[str, Any], *, public_id: str, rubric: dict, brief: dict, ceilings: dict, recipients: dict, seeds: list, concurrency: int, stopping_policy: dict, revision: int = 1, parent_body_hash: str | None = None) -> dict[str, Any]:
    contract = _contract()
    try:
        contract.assert_approval_ready(draft["questions"], draft["answers"])
    except contract.ContractError as exc:
        raise InterviewError(exc.field, exc.message) from exc
    payload = {
        "schema": contract.SCHEMA,
        "goal_id": draft["goal_id"],
        "revision": revision,
        "parent_body_hash": parent_body_hash,
        "public_id": public_id,
        "questions": draft["questions"],
        "answers": draft["answers"],
        "rubric": rubric,
        "public_criterion_ids": [row["id"] for row in rubric["criteria"] if row.get("label") == []],
        "public_brief": brief,
        "recipients": recipients,
        "seeds": seeds,
        "ceilings": ceilings,
        "concurrency": concurrency,
        "stopping_policy": stopping_policy,
        "monitoring": None,
    }
    version = contract.build_version(payload)
    return {"status": "proposal", "version": version, "payload": payload, "grant_id": None}
