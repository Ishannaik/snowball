"""Planner and synthesis proposals. Neither role can mark the rubric met."""
from __future__ import annotations

from typing import Any

ALLOWED_TOOLS = frozenset({"search", "fetch", "read"})


class ResearchError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def _label_union(labels: list[list[str]]) -> list[str]:
    union: set[str] = set()
    for label in labels:
        union |= set(label or [])
    return sorted(union)


def validate_task(
    task: dict[str, Any],
    rubric: dict[str, Any],
    public_context: dict[str, dict[str, Any]],
    *,
    provenance_mode: str = "strict",
) -> dict[str, Any]:
    """Validate one planned task.

    Provenance policy per the approved design: a query must originate from
    approved public context or public evidence. Agent-generated hypotheses are
    allowed only when they cite typed originating references whose union of
    labels is public; the exact-substring rule stays for literal context
    recombination (provenance_mode="strict"), while "hypothesis" mode validates
    the typed references instead of requiring a verbatim substring for every
    agent-generated query.
    """
    if not isinstance(task, dict):
        raise ResearchError("task", "task must be an object")
    tool = task.get("tool", "search")
    if tool not in ALLOWED_TOOLS:
        raise ResearchError("tool", "that tool is not available")
    criterion_ids = {row["id"] for row in rubric["criteria"]}
    if task.get("criterion_id") not in criterion_ids and not task.get("exploration_question"):
        raise ResearchError("criterion_id", "a task needs a criterion or an exploration question")
    query = task.get("query_text")
    context_ids = task.get("public_context_ids")
    if not isinstance(query, str) or not query.strip() or not isinstance(context_ids, list) or not context_ids:
        raise ResearchError("query_text", "a task must carry the exact public query and its context ids")
    if not isinstance(public_context, dict) or any(item not in public_context for item in context_ids):
        raise ResearchError("public_context_ids", "query context is not in the public projection")
    rows = [public_context[item] for item in context_ids]
    labels = [
        list(row.get("label") or []) if isinstance(row, dict) else [] for row in rows
    ]
    union = _label_union(labels)
    if union:
        raise ResearchError("public_context_ids", "query context must be public; labeled references are unauthorized here")
    sources = [str(row.get("text")) if isinstance(row, dict) else str(row) for row in rows]
    question = task.get("exploration_question")
    if question is not None and (not isinstance(question, str) or not question.strip()):
        raise ResearchError("exploration_question", "exploration text is empty")
    if provenance_mode == "strict":
        allowed = "\n".join(sources)
        if query.strip() not in allowed:
            raise ResearchError("query_text", "query text is not contained in public context")
        if question is not None and question.strip() not in allowed:
            raise ResearchError("exploration_question", "exploration text is not contained in public context")
    else:
        # Hypothesis mode: the query may be model-generated phrasing, but it
        # must stand on typed originating refs (given via public_context_ids,
        # all already verified public) or an explicit exploration question.
        origin = task.get("origin_refs")
        has_typed_origin = isinstance(origin, list) and bool(origin) and all(
            isinstance(item, str) and item in public_context for item in origin
        )
        if not has_typed_origin and question is None:
            raise ResearchError(
                "origin_refs",
                "an agent-generated hypothesis needs typed originating references or an exploration question",
            )
        if has_typed_origin:
            origin_union = _label_union([
                list(public_context[item].get("label") or []) if isinstance(public_context[item], dict) else []
                for item in origin
            ])
            if origin_union:
                raise ResearchError("origin_refs", "originating references must be public")
        for word in query.strip().split():
            if len(word) > 64:
                raise ResearchError("query_text", "query must not embed raw source blocks")
    # fetch/read tasks must name their target explicitly; search stays query-only.
    if tool in {"fetch", "read"} and not (task.get("url") or task.get("source_id")):
        raise ResearchError("tool", "fetch/read tasks need a url or stored source_id")
    return {
        "criterion_id": task.get("criterion_id"),
        "exploration_question": None if question is None else question.strip(),
        "tool": tool,
        "query_text": query.strip(),
        "public_context_ids": list(context_ids),
        "origin_refs": list(task["origin_refs"]) if isinstance(task.get("origin_refs"), list) else list(context_ids),
        "url": task.get("url"),
        "source_id": task.get("source_id"),
        "label": [],
    }


def validate_synthesis(synthesis: dict[str, Any], claims: list[dict[str, Any]]) -> dict[str, Any]:
    if synthesis.get("rubric_met") is True:
        raise ResearchError("rubric_met", "synthesis cannot mark the rubric met")
    known = {row["id"]: row for row in claims}
    refs = synthesis.get("evidence_ids") or []
    if not refs or any(ref not in known for ref in refs):
        raise ResearchError("evidence_ids", "synthesis must cite stored claims")
    kept_only = all(known[ref].get("state", "kept") == "kept" for ref in refs)
    if not kept_only:
        raise ResearchError("evidence_ids", "synthesis may only cite kept claims")
    for field in ("context_class", "model_id", "goal_id", "version_id"):
        if not synthesis.get(field):
            raise ResearchError(field, "synthesis binding is incomplete")
    return {
        "evidence_ids": list(refs),
        "context_class": synthesis["context_class"],
        "model_id": synthesis["model_id"],
        "goal_id": synthesis["goal_id"],
        "version_id": synthesis["version_id"],
        "rubric_met": False,
        "label": _label_union([known[ref].get("label") or [] for ref in refs]),
    }


def next_action(judge_rows: list[dict[str, Any]], frontier: list[dict[str, Any]], reason: str | None = None) -> dict[str, Any]:
    if reason in {"limit", "outage", "pause", "stall"}:
        return {"status": "partial", "reason": reason, "task": None}
    unmet = [row for row in judge_rows if row.get("verdict") == "unmet" and row.get("support_ids")]
    if unmet:
        same = [task for task in frontier if task.get("criterion_id") == unmet[0]["criterion_id"]]
        alternative = [task for task in frontier if task.get("criterion_id") != unmet[0]["criterion_id"]]
        if alternative:
            return {"status": "progressing", "reason": "scoped_negative", "task": alternative[0]}
        return {"status": "partial", "reason": "scoped_negative", "task": None, "rejected_task_ids": [task["id"] for task in same]}
    if not frontier:
        return {"status": "partial", "reason": "empty_frontier", "task": None}
    return {"status": "progressing", "reason": None, "task": frontier[0]}
