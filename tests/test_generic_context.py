"""Bounded label-scoped context: byte-honest costs, low score leaves selection stays stored."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("generic_context", ROOT / "tools" / "generic_context.py")
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


def item(row_id, *, score=0, criterion_ids=(), label=(), conflict=False, must_keep=False, text="t", tokens=None):
    row = {
        "id": row_id, "score": score, "criterion_ids": list(criterion_ids),
        "label": list(label), "conflict": conflict, "must_keep": must_keep, "text": text,
    }
    if tokens is not None:
        row["tokens"] = tokens
    return row


class ContextTest(unittest.TestCase):
    def test_outstanding_support_stays_and_low_score_is_not_deleted(self):
        items = [
            item("gap", criterion_ids=["supported"], text="gap text"),
            item("noise", criterion_ids=["other"], text="noise text"),
            item("useful", score=9, criterion_ids=["other"], text="useful text"),
        ]
        selected = context.select_context(items, item_limit=3, token_limit=1000, outstanding_ids=["supported"])
        self.assertIn("gap", selected["selected_ids"])
        self.assertNotIn("noise", selected["selected_ids"])
        self.assertEqual([row["id"] for row in items], ["gap", "noise", "useful"])
        self.assertEqual(
            [row["id"] for row in selected["deferred"] if row["id"] == "noise"],
            ["noise"],
        )

    def test_clearance_filters_labels_before_ranking(self):
        items = [
            item("scored", score=10, criterion_ids=["supported"], label=["private"]),
            item("public_gap", score=2, criterion_ids=["supported"]),
        ]
        selected = context.select_context(
            items, item_limit=5, token_limit=100_000, outstanding_ids=["supported"], clearance=(),
        )
        self.assertNotIn("scored", selected["selected_ids"])
        self.assertEqual(selected["selected_ids"], ["public_gap"])
        self.assertIn("scored", [row["id"] for row in items])

    def test_byte_cost_is_code_owned_and_never_trusts_underdeclared_tokens(self):
        # A claim with a 500-byte payload cannot be smuggled in by declaring tokens=1.
        big = item("big", score=10, criterion_ids=["supported"], text="x" * 500, tokens=1)
        selected = context.select_context([big], item_limit=5, token_limit=100, outstanding_ids=["supported"])
        self.assertEqual(selected["selected_ids"], [])
        self.assertEqual(selected["deferred"][0]["reason"], "token_limit")
        # The honest charge exceeds the lie.
        open_run = context.select_context([big], item_limit=5, token_limit=10_000, outstanding_ids=["supported"])
        self.assertGreater(open_run["tokens_used"], 100)

    def test_supplied_tokens_can_raise_not_lower_the_charge(self):
        inflated = item("inf", score=10, criterion_ids=["supported"], text="hi", tokens=900)
        selected = context.select_context([inflated], item_limit=5, token_limit=10_000, outstanding_ids=["supported"])
        self.assertEqual(selected["tokens_used"], 900)

    def test_invalid_costs_and_limits_are_refused(self):
        with self.assertRaises(ValueError):
            context.select_context([item("a", tokens=-5)], 5, 100, [])
        with self.assertRaises(ValueError):
            context.select_context([item("a", tokens=True)], 5, 100, [])
        with self.assertRaises(ValueError):
            context.select_context([], -1, 100, [])
        with self.assertRaises(ValueError):
            context.select_context([], 5, -100, [])
        with self.assertRaises(ValueError):
            context.select_context([], True, 100, [])

    def test_hard_bounds_defer_with_reason_never_flip_order(self):
        items = [
            item("big", score=10, criterion_ids=["supported"], text="b" * 800),
            item("small", score=1, criterion_ids=["supported"], text="s" * 20),
        ]
        selected = context.select_context(items, item_limit=1, token_limit=100_000, outstanding_ids=["supported"])
        self.assertEqual(selected["selected_ids"], ["big"])
        self.assertEqual(selected["deferred"][0], {"id": "small", "reason": "item_limit"})
        tight = context.select_context([items[1]], item_limit=5, token_limit=10, outstanding_ids=["supported"])
        self.assertEqual(tight["selected_ids"], [])
        self.assertEqual(tight["deferred"][0]["reason"], "token_limit")

    def test_conflicts_rank_first(self):
        items = [
            item("calm", score=9, criterion_ids=["other"], text="c" * 500),
            item("conflict", score=1, conflict=True, text="x" * 500),
        ]
        selected = context.select_context(items, item_limit=1, token_limit=50_000, outstanding_ids=[])
        self.assertEqual(selected["selected_ids"], ["conflict"])

    def test_refresh_cadence(self):
        self.assertFalse(context.refresh_due(9))
        self.assertTrue(context.refresh_due(10))
        self.assertTrue(context.refresh_due(3, checkpoint=True))

    def test_rephrased_work_has_the_same_fingerprint(self):
        first = context.work_fingerprint([{"criterion_id": "supported", "source_id": "s1", "query": "one"}])
        second = context.work_fingerprint([{"criterion_id": "supported", "source_id": "s1", "query": "another phrasing"}])
        third = context.work_fingerprint([{"criterion_id": "supported", "source_id": "s2", "query": "one"}])
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_context_digest_binds_selection_and_clearance(self):
        items = [item("a", score=5, criterion_ids=["x"])]
        first = context.select_context(items, 5, 10_000, ["x"], clearance=())
        second = context.select_context(items, 5, 10_000, ["x"], clearance=("public",))
        self.assertNotEqual(first["context_digest"], second["context_digest"])


if __name__ == "__main__":
    unittest.main()
