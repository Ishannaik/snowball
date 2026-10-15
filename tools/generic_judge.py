"""Judge rows are checked by code. The judge cannot declare the goal met."""
from __future__ import annotations

import hashlib
import json
from typing import Any

VERDICTS = frozenset({"met", "unmet", "unknown", "conditional", "contested"})
ROW_KEYS = frozenset({
    "criterion_id", "verdict", "reason", "support_ids", "conflict_ids",
    "missing_evidence", "next_questions", "qualification_id",
})


class JudgeError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_fingerprint(claims: list[dict[str, Any]]) -> str:
    rows = [
        {"id": row["id"], "state": row["state"], "quote_hash": row["quote_hash"]}
        for row in sorted(claims, key=lambda item: item["id"])
    ]
    return _digest(rows)


def _refs(value: Any, field: str, cid: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise JudgeError(cid, f"{field} must be a list of claim ids")
    if len(set(value)) != len(value):
        raise JudgeError(cid, f"{field} contains duplicates")
    return list(value)


def validate_deliverable(deliverable: Any, claims: list[dict[str, Any]], *, required: bool) -> dict[str, Any]:
    if not isinstance(deliverable, dict):
        raise JudgeError("deliverable", "deliverable assessment is missing or malformed")
    extra = set(deliverable) - {"supported", "reason", "evidence_ids"}
    if extra:
        raise JudgeError("deliverable", "deliverable assessment has unknown fields")
    supported = deliverable.get("supported")
    if not isinstance(supported, bool):
        raise JudgeError("deliverable", "deliverable supported must be boolean")
    reason = deliverable.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise JudgeError("deliverable", "deliverable reason is required")
    by_claim = {row["id"]: row for row in claims}
    refs = _refs(deliverable.get("evidence_ids"), "deliverable.evidence_ids", "deliverable")
    bad = [ref for ref in refs if by_claim.get(ref, {}).get("state") != "kept"]
    if bad:
        raise JudgeError("deliverable", "deliverable evidence must cite kept claims")
    return {"supported": supported, "reason": reason.strip(), "evidence_ids": refs}


def validate_judge_result(
    rubric: dict[str, Any],
    result: dict[str, Any],
    claims: list[dict[str, Any]],
    authorized_ids: list[str],
    deliverable: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result.get("rows"), list) or not result["rows"]:
        raise JudgeError("rows", "judge result is malformed or empty")
    by_claim = {row["id"]: row for row in claims}
    criteria = {row["id"]: row for row in rubric["criteria"]}
    seen: set[str] = set()
    clean = []
    cited_support: list[str] = []
    cited_conflict: list[str] = []
    for row in result["rows"]:
        if not isinstance(row, dict) or set(row) - ROW_KEYS:
            raise JudgeError("rows", "judge row has unknown fields")
        cid = row.get("criterion_id")
        if not isinstance(cid, str) or cid not in criteria or cid in seen or cid not in authorized_ids:
            raise JudgeError(str(cid), "criterion is unknown, duplicate, or outside the authorized scope")
        seen.add(cid)
        verdict = row.get("verdict")
        if verdict not in VERDICTS:
            raise JudgeError(cid, "unknown verdict")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise JudgeError(cid, "a reason is required for every row")
        support = _refs(row.get("support_ids"), "support_ids", cid)
        conflict = _refs(row.get("conflict_ids"), "conflict_ids", cid)
        nxt = row.get("next_questions")
        if nxt is not None:
            if not isinstance(nxt, list) or any(not isinstance(item, str) or not item.strip() for item in nxt):
                raise JudgeError(cid, "next_questions must be a list of strings")
        missing = row.get("missing_evidence")
        if missing is not None and (not isinstance(missing, str) or (verdict in {"unmet", "unknown"} and not missing.strip())):
            raise JudgeError(cid, "missing_evidence must describe the gap for unmet/unknown rows")
        if verdict in {"met", "conditional"}:
            if not support or any(by_claim.get(item, {}).get("state") != "kept" for item in support):
                raise JudgeError(cid, "positive support requires kept claims")
        if verdict == "contested" and not conflict:
            raise JudgeError(cid, "a contested verdict must cite conflicting claims")
        if verdict == "conditional":
            allowed = {item["id"] for item in criteria[cid]["qualifications"]}
            if row.get("qualification_id") not in allowed:
                raise JudgeError(cid, "conditional verdict needs an accepted qualification")
        if verdict in {"unmet", "unknown"} and row.get("qualification_id") not in (None, ""):
            raise JudgeError(cid, "qualification_id only applies to conditional rows")
        cited_support.extend(support)
        cited_conflict.extend(conflict)
        clean.append({
            "criterion_id": cid,
            "verdict": verdict,
            "reason": reason.strip(),
            "support_ids": support,
            "conflict_ids": conflict,
            "missing_evidence": None if missing is None else str(missing) or None,
            "next_questions": None if nxt is None else [item.strip() for item in nxt],
            "qualification_id": row.get("qualification_id"),
        })
    required = [row["id"] for row in rubric["criteria"] if row["required"]]
    missing_rows = [cid for cid in required if cid in authorized_ids and cid not in seen]
    if missing_rows:
        raise JudgeError(missing_rows[0], "required criterion missing")
    if criteria and not seen:
        raise JudgeError("rows", "an empty assessment cannot pass a nonempty rubric")
    checked_deliverable = None
    if deliverable is not None:
        checked_deliverable = validate_deliverable(deliverable, claims, required=True)
    elif any(row["verdict"] in {"met", "conditional"} for row in clean):
        raise JudgeError("deliverable", "a positive full rubric assessment needs an actual deliverable check")
    fingerprint_rows = [
        by_claim[item] for item in sorted(set(cited_support + cited_conflict)) if item in by_claim
    ]
    return {
        "rows": clean,
        "deliverable": checked_deliverable,
        "fingerprint": evidence_fingerprint(fingerprint_rows),
    }


def rubric_met(rubric: dict[str, Any], assessments: dict[str, Any], authorized_ids: list[str]) -> bool:
    required = [row["id"] for row in rubric["criteria"] if row["required"]]
    if not required:
        return False
    if any(cid not in authorized_ids for cid in required):
        return False
    by_id = {row["criterion_id"]: row for row in assessments["rows"]}
    if set(required) - set(by_id):
        return False
    deliverable = assessments.get("deliverable")
    if not isinstance(deliverable, dict) or deliverable.get("supported") is not True:
        return False
    return all(by_id[cid]["verdict"] in {"met", "conditional"} for cid in required)
