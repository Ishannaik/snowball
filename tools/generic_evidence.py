"""Source quotes are checked by span and hash. Notes are not public fetches."""
from __future__ import annotations

import hashlib
from typing import Any

REVIEW_STATES = frozenset({"kept", "disputed", "rejected", "unknown"})


class EvidenceError(ValueError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def verify_quote(snapshot_text: str, quote: str, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    """Check a claim's quote against the stored snapshot.

    Models are unreliable at char counting, so offsets are advisory: when absent
    (or at least when start is None/0), code derives the exact span from the
    UNIQUE literal occurrence of the quote in the snapshot text. No fuzzy
    matching is performed; a quote that is absent or ambiguous is an error.
    """
    if not isinstance(snapshot_text, str) or not isinstance(quote, str) or not quote:
        raise EvidenceError("quote", "quote and snapshot must be nonempty text")
    snapshot_hash = hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest()
    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()

    def result(begin: int, finish: int) -> dict[str, Any]:
        return {
            "quote_hash": quote_hash,
            "snapshot_hash": snapshot_hash,
            "start": begin,
            "end": finish,
        }

    has_offsets = isinstance(start, int) and isinstance(end, int)
    if has_offsets:
        if start < 0 or end < start or end > len(snapshot_text):
            raise EvidenceError("quote", "quote span is outside the snapshot")
        actual = snapshot_text[start:end]
        if actual == quote:
            return result(start, end)
        # offsets were wrong; fall through to unique literal derivation
    first = snapshot_text.find(quote)
    if first < 0:
        raise EvidenceError("quote", "quote does not occur in the snapshot text")
    if snapshot_text.find(quote, first + 1) >= 0:
        raise EvidenceError("quote", "quote occurs more than once; an exact span is required")
    return result(first, first + len(quote))


def review_claim(claim: dict[str, Any], verdict: str, claims: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if verdict not in REVIEW_STATES:
        raise EvidenceError("verdict", "unknown review state")
    provenance = claim.get("provenance")
    if provenance in {"human_note", "legacy_lead"} and claim.get("source_kind") == "public_fetch":
        raise EvidenceError("provenance", "a note cannot be labeled as a public fetch")
    if verdict == "kept" and claim.get("kind") != "reasoning" and not claim.get("quote_hash"):
        raise EvidenceError("quote_hash", "kept claims require a checked quote hash")
    if claim.get("kind") == "reasoning" and verdict == "kept":
        known = {row["id"]: row for row in claims or []}
        basis = claim.get("basis_ids") or []
        if not basis or any(known.get(item, {}).get("state") != "kept" for item in basis):
            raise EvidenceError("basis_ids", "reasoning must resolve to kept claims")
    return {"id": claim["id"], "state": verdict, "quote_hash": claim.get("quote_hash"), "provenance": provenance}


def invalidated_judgments(judgments: list[dict[str, Any]], claim: dict[str, Any]) -> list[str]:
    """Judgment ids whose fingerprint depends on this claim, per contract review rules.

    A disputed/rejected/superseded claim invalidates dependent judgments; an
    unrelated evidence addition does not touch them.
    """
    invalidated = []
    for judgment in judgments:
        citation = judgment.get("claim_ids") or judgment.get("cited_claim_ids") or []
        if claim["id"] in citation:
            invalidated.append(judgment["id"])
        elif judgment.get("fingerprint") and claim.get("quote_hash") in judgment.get("fingerprint", ""):
            # A hash alone cannot prove dependence; runtime recomputes fingerprints.
            invalidated.append(judgment["id"])
    return invalidated
