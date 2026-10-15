"""Domain-neutral goal contract. Models propose; this module only validates and hashes."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

SCHEMA = "generic-goal-1"
INPUT_TYPES = frozenset({
    "text", "boolean", "integer", "decimal", "choice", "list", "object", "document_reference",
})
ANSWER_STATES = frozenset({"confirmed", "unknown", "declined", "unconfirmed"})
ORIGINS = frozenset({"user_stated", "imported_history", "model_proposed"})
RECIPIENT_KINDS = frozenset({"model", "search", "fetch"})
LIMIT_UNITS = (
    "expansion_tasks", "sources", "source_bytes", "model_calls", "tool_calls", "model_tokens", "elapsed_sec",
)
OPERATOR_MAX = {
    "expansion_tasks": 1000,
    "sources": 10000,
    "source_bytes": 1_000_000_000,
    "model_calls": 10000,
    "tool_calls": 10000,
    "model_tokens": 100_000_000,
    "elapsed_sec": 86_400,
    "concurrency": 32,
}
OPENING = (
    ("objective", "What should the research establish?", True, []),
    ("deliverable", "What research deliverable do you want?", True, []),
    ("constraints", "Which constraints must the research respect?", False, ["private"]),
    ("success", "What would count as a supported result?", True, ["private"]),
    ("limits", "What execution ceilings should apply?", True, []),
    ("disclosure", "Which recipients may see which context?", True, ["private"]),
)
ID_RX = re.compile(r"^[a-z0-9_.-]{1,64}$")
PUBLIC_ID_RX = re.compile(r"^[0-9a-f]{32}$")
QUESTION_KEYS = frozenset({"id", "prompt", "input_type", "required", "label", "choices"})
ANSWER_KEYS = frozenset({"question_id", "value", "state", "origin", "label", "confirmation_ref"})
PAYLOAD_KEYS = frozenset({
    "schema", "goal_id", "revision", "parent_body_hash", "public_id", "questions", "answers",
    "rubric", "public_criterion_ids", "public_brief", "recipients", "seeds", "ceilings",
    "concurrency", "stopping_policy", "monitoring",
})


class ContractError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _closed(value: dict[str, Any], allowed: frozenset[str], field: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ContractError(field, "unknown field")


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or ID_RX.fullmatch(value) is None:
        raise ContractError(field, "identifier must match [a-z0-9_.-]{1,64}")
    return value


def _label(value: Any, field: str, *, default: list[str] | None = None) -> list[str]:
    if value is None and default is not None:
        value = default
    if not isinstance(value, list) or any(not isinstance(item, str) or ID_RX.fullmatch(item) is None for item in value):
        raise ContractError(field, "label must be a list of class identifiers")
    if len(set(value)) != len(value):
        raise ContractError(field, "duplicate label")
    return sorted(value)


def _public(label: list[str]) -> bool:
    return label == []


def opening_questions() -> list[dict[str, Any]]:
    return [
        {"id": qid, "prompt": prompt, "input_type": "text", "required": required, "label": list(label)}
        for qid, prompt, required, label in OPENING
    ]


def validate_question(question: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(question, dict):
        raise ContractError("question", "question must be an object")
    _closed(question, QUESTION_KEYS, "question")
    qid = _id(question.get("id"), "question.id")
    prompt = question.get("prompt")
    if not isinstance(prompt, str) or not _nfc(prompt).strip():
        raise ContractError(qid, "prompt is required")
    input_type = question.get("input_type")
    if input_type not in INPUT_TYPES:
        raise ContractError(qid, "unsupported input type")
    if not isinstance(question.get("required"), bool):
        raise ContractError(qid, "required must be boolean")
    clean = {
        "id": qid,
        "prompt": _nfc(prompt).strip(),
        "input_type": input_type,
        "required": question["required"],
        "label": _label(question.get("label"), qid, default=["private"]),
    }
    if input_type == "choice":
        choices = question.get("choices")
        if not isinstance(choices, list) or not choices or not all(isinstance(item, str) and item for item in choices):
            raise ContractError(qid, "choice questions need choices")
        clean["choices"] = [_nfc(item) for item in choices]
    elif "choices" in question:
        raise ContractError(qid, "choices are only valid for choice questions")
    return clean


def _value_ok(question: dict[str, Any], value: Any) -> Any:
    kind = question["input_type"]
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise ContractError(question["id"], "text value required")
        return _nfc(value).strip()
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ContractError(question["id"], "boolean value required")
        return value
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(question["id"], "integer value required")
        return value
    if kind == "decimal":
        if not isinstance(value, str) or not re.fullmatch(r"-?\d+(\.\d+)?", value):
            raise ContractError(question["id"], "decimal must be a canonical string")
        return value
    if kind == "choice":
        if value not in question.get("choices", []):
            raise ContractError(question["id"], "choice is not allowed")
        return value
    if kind in {"list", "object"}:
        if kind == "list" and not isinstance(value, list):
            raise ContractError(question["id"], "list value required")
        if kind == "object" and not isinstance(value, dict):
            raise ContractError(question["id"], "object value required")
        if len(canonical(value)) > 8000:
            raise ContractError(question["id"], "value is too large")
        return value
    if not isinstance(value, str) or not value.startswith("doc:"):
        raise ContractError(question["id"], "document reference required")
    return value


def validate_answer(question: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    question = validate_question(question)
    if not isinstance(answer, dict):
        raise ContractError(question["id"], "answer must be an object")
    _closed(answer, ANSWER_KEYS, question["id"])
    if answer.get("question_id") != question["id"]:
        raise ContractError(question["id"], "answer is not bound to the question")
    state = answer.get("state")
    origin = answer.get("origin")
    if state not in ANSWER_STATES:
        raise ContractError(question["id"], "unknown answer state")
    if origin not in ORIGINS:
        raise ContractError(question["id"], "unknown origin")
    label = _label(answer.get("label"), question["id"], default=["private"])
    if not set(question["label"]).issubset(label):
        raise ContractError(question["id"], "answer label must include the question label")
    if state == "declined" and question["required"]:
        raise ContractError(question["id"], "a required answer cannot be declined")
    if state in {"unknown", "declined", "unconfirmed"} and answer.get("value") is not None:
        raise ContractError(question["id"], "only confirmed answers have a value")
    value = None
    if state == "confirmed":
        value = _value_ok(question, answer.get("value"))
        if origin != "user_stated":
            ref = answer.get("confirmation_ref")
            if not isinstance(ref, dict) or set(ref) != {"actor", "at", "value_digest"}:
                raise ContractError(question["id"], "imported confirmation requires a confirmation reference")
            if ref["value_digest"] != digest(value) or not isinstance(ref["actor"], str) or not isinstance(ref["at"], int):
                raise ContractError(question["id"], "confirmation reference does not match the value")
    elif "confirmation_ref" in answer:
        raise ContractError(question["id"], "confirmation reference is only valid when confirmed")
    if _public(label) and origin != "user_stated" and state == "confirmed":
        if "confirmation_ref" not in answer:
            raise ContractError(question["id"], "publishing an answer needs confirmation")
    return {
        "question_id": question["id"],
        "value": value,
        "state": state,
        "origin": origin,
        "label": label,
        **({"confirmation_ref": answer["confirmation_ref"]} if state == "confirmed" and origin != "user_stated" else {}),
    }


def assert_approval_ready(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> None:
    by_id = {}
    for question in questions:
        clean = validate_question(question)
        if clean["id"] in by_id:
            raise ContractError(clean["id"], "duplicate question")
        by_id[clean["id"]] = clean
    seen = set()
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("question_id") not in by_id:
            raise ContractError("answers", "answer references an unknown question")
        qid = answer["question_id"]
        if qid in seen:
            raise ContractError(qid, "duplicate answer")
        seen.add(qid)
        clean = validate_answer(by_id[qid], answer)
        if clean["state"] == "unconfirmed":
            raise ContractError(qid, "unconfirmed answers block approval")
    missing = [qid for qid, question in by_id.items() if question["required"] and qid not in seen]
    if missing:
        raise ContractError(missing[0], "required answer missing")


def validate_rubric(rubric: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rubric, dict) or set(rubric) != {"criteria"}:
        raise ContractError("rubric", "rubric must contain criteria")
    criteria = []
    seen = set()
    qualifications = set()
    required = 0
    for raw in rubric["criteria"]:
        if not isinstance(raw, dict) or set(raw) != {"id", "description", "required", "expected_evidence", "label", "qualifications"}:
            raise ContractError("rubric", "criterion has an unknown field")
        cid = _id(raw.get("id"), "criterion.id")
        if cid in seen:
            raise ContractError(cid, "duplicate criterion")
        seen.add(cid)
        if not isinstance(raw["required"], bool):
            raise ContractError(cid, "required must be boolean")
        required += int(raw["required"])
        quals = []
        for item in raw["qualifications"]:
            qid = _id(item.get("id"), cid)
            if qid in qualifications:
                raise ContractError(qid, "duplicate qualification")
            qualifications.add(qid)
            quals.append({"id": qid, "text": _nfc(item["text"]).strip()})
        criteria.append({
            "id": cid,
            "description": _nfc(raw["description"]).strip(),
            "required": raw["required"],
            "expected_evidence": _nfc(raw["expected_evidence"]).strip(),
            "label": _label(raw["label"], cid),
            "qualifications": quals,
        })
    if required < 1:
        raise ContractError("rubric", "at least one required criterion is needed")
    return {"criteria": sorted(criteria, key=lambda item: item["id"])}


def validate_ceilings(ceilings: dict[str, Any]) -> dict[str, int]:
    if not isinstance(ceilings, dict) or set(ceilings) != set(LIMIT_UNITS):
        raise ContractError("ceilings", "all limit units are required")
    clean = {}
    for unit in LIMIT_UNITS:
        value = ceilings[unit]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > OPERATOR_MAX[unit]:
            raise ContractError(unit, "ceiling is outside the operator range")
        clean[unit] = value
    return clean
def _recipients(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("recipients", "recipients must be an object")
    clean = {}
    for key, row in value.items():
        _id(key, "recipient")
        if not isinstance(row, dict) or set(row) != {"kind", "attested", "clearance"}:
            raise ContractError(key, "recipient is incomplete")
        if row["kind"] not in RECIPIENT_KINDS or not isinstance(row["attested"], bool):
            raise ContractError(key, "recipient kind is not allowed")
        clearance = _label(row["clearance"], key)
        if clearance and row["attested"] is not True:
            raise ContractError(key, "private clearance requires an attested recipient")
        clean[key] = {"kind": row["kind"], "attested": row["attested"], "clearance": clearance}
    return clean


def _seeds(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError("seeds", "seeds must be a list")
    clean = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"id", "value", "label"}:
            raise ContractError("seeds", "seed is incomplete")
        clean.append({"id": _id(row["id"], "seed"), "value": row["value"], "label": _label(row["label"], "seed")})
    return clean


def _stopping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) <= {"on_exhaustion", "on_empty_frontier", "stall_checkpoints"}:
        raise ContractError("stopping_policy", "stopping policy has an unknown field")
    if value.get("on_exhaustion") not in {None, "partial"} or value.get("on_empty_frontier") not in {None, "partial"}:
        raise ContractError("stopping_policy", "stopping policy cannot invent success")
    return dict(value)


def _confirmed(answers: list[dict[str, Any]], qid: str) -> dict[str, Any]:
    found = next((row for row in answers if row["question_id"] == qid and row["state"] == "confirmed"), None)
    if found is None:
        raise ContractError(qid, "confirmed answer required")
    return found


def build_version(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("payload", "payload must be an object")
    _closed(payload, PAYLOAD_KEYS, "payload")
    if payload.get("schema") != SCHEMA:
        raise ContractError("schema", "unsupported contract schema")
    goal_id = _id(payload.get("goal_id"), "goal_id")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ContractError("revision", "revision must be a positive integer")
    parent = payload.get("parent_body_hash")
    if parent is not None and (not isinstance(parent, str) or len(parent) != 64):
        raise ContractError("parent_body_hash", "parent hash is invalid")
    public_id = payload.get("public_id")
    if not isinstance(public_id, str) or PUBLIC_ID_RX.fullmatch(public_id) is None:
        raise ContractError("public_id", "public id must be a random 32-hex token")
    questions = sorted((validate_question(row) for row in payload["questions"]), key=lambda item: item["id"])
    answers = list(payload["answers"])
    assert_approval_ready(questions, answers)
    by_id = {row["id"]: row for row in questions}
    clean_answers = sorted(
        (validate_answer(by_id[row["question_id"]], row) for row in answers),
        key=lambda item: item["question_id"],
    )
    objective = _confirmed(clean_answers, "objective")
    deliverable = _confirmed(clean_answers, "deliverable")
    rubric = validate_rubric(payload["rubric"])
    selected = payload["public_criterion_ids"]
    if not isinstance(selected, list):
        raise ContractError("public_criterion_ids", "public criteria must be a list")
    by_criterion = {row["id"]: row for row in rubric["criteria"]}
    if any(item not in by_criterion or not _public(by_criterion[item]["label"]) for item in selected):
        raise ContractError("public_criterion_ids", "public criteria must be a public subset")
    brief = payload["public_brief"]
    if not isinstance(brief, dict) or set(brief) != {"text", "derived_from", "label"}:
        raise ContractError("public_brief", "public brief is incomplete")
    brief_label = _label(brief["label"], "public_brief")
    if not isinstance(brief.get("derived_from"), list):
        raise ContractError("public_brief", "brief sources are required")
    public_answers = {
        row["question_id"] for row in clean_answers if row["state"] == "confirmed" and _public(row["label"])
    }
    if any(ref not in public_answers for ref in brief["derived_from"]):
        raise ContractError("public_brief", "a public brief cannot cite a private or unconfirmed answer")
    recipients = _recipients(payload["recipients"])
    seeds = _seeds(payload["seeds"])
    stopping = _stopping(payload["stopping_policy"])
    if payload["monitoring"] is not None:
        raise ContractError("monitoring", "monitoring requires a separate grant")
    ceilings = validate_ceilings(payload["ceilings"])
    concurrency = payload["concurrency"]
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= OPERATOR_MAX["concurrency"]:
        raise ContractError("concurrency", "concurrency is outside the operator range")
    body = {
        "schema": SCHEMA,
        "goal_id": goal_id,
        "revision": revision,
        "parent_body_hash": parent,
        "objective": objective["value"],
        "deliverable": deliverable["value"],
        "questions": questions,
        "answers": clean_answers,
        "rubric": rubric,
        "public_criterion_ids": sorted(selected),
        "public_brief": {"text": _nfc(brief["text"]).strip(), "derived_from": list(brief["derived_from"]), "label": brief_label},
        "recipients": recipients,
        "seeds": seeds,
        "ceilings": ceilings,
        "concurrency": concurrency,
        "stopping_policy": stopping,
        "monitoring": None,
    }
    body_hash = digest({"kind": "contract", "body": body})
    if public_id == body_hash[:32]:
        raise ContractError("public_id", "public id must not be derived from the contract")
    public = {
        "brief": body["public_brief"]["text"] if _public(brief_label) else None,
        "criteria": [row for row in rubric["criteria"] if row["id"] in selected],
        "ceilings": ceilings,
        "seeds": [row for row in payload["seeds"] if isinstance(row, dict) and _public(_label(row.get("label"), "seed"))],
    }
    return {
        "body": body,
        "body_hash": body_hash,
        "public_id": public_id,
        "public": public,
        "public_hash": digest({"kind": "public", "projection": public}),
    }
