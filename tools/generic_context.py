"""Working context is a bounded selection. Low score does not delete stored evidence."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _byte_cost(value: Any) -> int:
    """Conservative code-owned cost: UTF-8 bytes of the exact emitted payload."""
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8")
    return max(1, len(encoded))


def _validated_limit(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _item_tokens(row: dict[str, Any]) -> int:
    """Never trust under-declared costs: max(validated supplied tokens, byte bound)."""
    supplied = row.get("tokens")
    if supplied is not None:
        if isinstance(supplied, bool) or not isinstance(supplied, int) or supplied < 0:
            raise ValueError("tokens must be a nonnegative integer when supplied")
        floor = supplied
    else:
        floor = 0
    # The full emitted row payload (all metadata) is serialized for the bound.
    return max(floor, _byte_cost(row), 1)


def select_context(
    items: list[dict[str, Any]],
    item_limit: int,
    token_limit: int,
    outstanding_ids: list[str],
    clearance: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Hard bounded ranked working-context selection.

    - ``clearance`` is the context classes this recipient may see; labeled items
      are filtered out BEFORE ranking (never reordered into visibility).
    - costs are code-owned: the UTF-8 serialized byte length of the full item
      payload, raised to any validated nonnegative supplied ``tokens``; a
      declared cost can raise the charge but never lower it.
    - outstanding-criterion items and explicit conflicts rank first with a
      deferred reason recorded when bounds push them out.
    - limits must be nonnegative ints; bounds are hard and never displaced.
    """
    item_limit = _validated_limit(item_limit, "item_limit")
    token_limit = _validated_limit(token_limit, "token_limit")
    cleared: set[str] = set(clearance or ())
    visible = [row for row in items if set(row.get("label") or []) <= cleared]
    outstanding = set(outstanding_ids or [])
    # Validate every visible item's cost up front: invalid costs never defer quietly.
    costs = {row["id"]: _item_tokens(row) for row in visible}

    def priority(row: dict[str, Any]) -> tuple[int, int, int, str]:
        is_outstanding = bool(set(row.get("criterion_ids") or []) & outstanding)
        is_conflict = row.get("conflict") is True
        return (
            -int(is_outstanding or is_conflict),
            -int(row.get("must_keep", False)),
            -row.get("score", 0),
            row["id"],
        )

    ranked = sorted(visible, key=priority)
    selected: list[str] = []
    used = 0
    deferred: list[dict[str, Any]] = []
    for row in ranked:
        is_outstanding = bool(set(row.get("criterion_ids") or []) & outstanding)
        is_conflict = row.get("conflict") is True
        if not is_outstanding and not is_conflict and row.get("score", 0) < 1:
            deferred.append({"id": row["id"], "reason": "low_score"})
            continue
        cost = costs[row["id"]]
        if len(selected) >= item_limit or used + cost > token_limit:
            deferred.append({
                "id": row["id"],
                "reason": "item_limit" if len(selected) >= item_limit else "token_limit",
            })
            continue
        selected.append(row["id"])
        used += cost
    return {
        "selected_ids": selected,
        "retained_ids": [row["id"] for row in items],
        "deferred": deferred,
        "tokens_used": used,
        "token_limit": token_limit,
        "item_limit": item_limit,
        "context_digest": _digest({"ids": selected, "clearance": sorted(cleared)}),
    }


def context_view(selected: list[dict[str, Any]], *, label: str = "bounded") -> dict[str, Any]:
    rows = []
    for row in selected:
        rows.append({
            "id": row["id"],
            "score": row.get("score"),
            "criterion_ids": list(row.get("criterion_ids") or []),
            "label": sorted(set(row.get("label") or [])),
            "excerpt": (row.get("text") or "")[:400],
        })
    return {"items": rows, "note": label, "source": "store_view_only"}


def refresh_due(committed_turns: int, cadence: int = 10, checkpoint: bool = False) -> bool:
    if checkpoint:
        return True
    return committed_turns > 0 and committed_turns % cadence == 0


def work_fingerprint(tasks: list[dict[str, Any]]) -> str:
    rows = sorted(
        ({"criterion_id": row.get("criterion_id"), "source_id": row.get("source_id")} for row in tasks),
        key=lambda row: (row["criterion_id"] or "", row["source_id"] or ""),
    )
    return _digest(rows)
