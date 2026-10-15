"""Frontier priority, exploration, cooldown, and honest progress."""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
import unittest
from pathlib import Path


def _fixtures():
    name = "planner_contract_fixtures"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = Path(__file__).with_name("test_planner_contracts.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fx = _fixtures()


def _product(components):
    return (
        components["goal_value"]
        * components["uncertainty"]
        * components["authority"]
        * components["freshness_need"]
        / components["cost"]
    )


def _runtime(store, goal_id):
    return dict(store.db.execute("SELECT * FROM goal_runtime WHERE goal_id=?", (goal_id,)).fetchone())


def _identities(store, goal_id):
    return store.db.execute(
        "SELECT COUNT(*) AS n FROM goal_node_identities WHERE goal_id=?",
        (goal_id,),
    ).fetchone()["n"]


class FrontierPriorityTests(unittest.TestCase):
    def test_official_score_beats_novelty_and_exploration_is_capped_only_while_gaps_remain(self):
        lab = fx.Lab("priority")
        self.addCleanup(lab.close)
        key = lab.program("example-computing", "Example Computing", "admissions.example.edu")
        official_url = "https://admissions.example.edu/tuition"
        novel_url = "https://forum.example/new"
        stale_url = "https://forum.example/old"
        official = lab.page(official_url)
        lab.page(novel_url)
        stale = lab.page(stale_url)
        lab.store.db.execute("UPDATE pages SET fetch_count=20 WHERE id=?", (official,))
        lab.store.db.execute("UPDATE pages SET fetch_count=8 WHERE id=?", (stale,))
        proposals = [
            lab.fetch(stale_url, rationale="older forum thread"),
            lab.fetch(official_url, origin="gap", program_key=key, field_key="tuition", rationale="official tuition page"),
            lab.fetch(novel_url, rationale="newer forum thread"),
        ]
        _validated, plan = lab.run(proposals, 1)
        rows = fx.decisions(lab.store)
        self.assertEqual([row["decision"] for row in rows], ["selected", "deferred", "deferred"])
        self.assertEqual(fx.selected_canons(lab.store, plan), [fx.canon(official_url)])
        official_row, novel_row, stale_row = rows
        self.assertGreater(official_row["score"], novel_row["score"])
        self.assertGreater(novel_row["components"]["novelty"], official_row["components"]["novelty"])
        self.assertGreater(novel_row["components"]["novelty"], stale_row["components"]["novelty"])
        self.assertAlmostEqual(novel_row["score"], stale_row["score"])
        for row in rows:
            self.assertTrue(math.isclose(_product(row["components"]), row["score"]))
            self.assertEqual(row["components"]["paid"], 0)
            self.assertEqual(row["components"]["estimated_tokens"], 0)
            self.assertEqual(row["components"]["cost"], 1)
            self.assertEqual(row["components"]["exploration_share"], "0")
            self.assertTrue(row["components"]["critical_gaps"])
            self.assertEqual(row["components"]["exploration_cap"], 0)
            self.assertNotEqual(row["reason"], row["components"]["model_rationale"])
        self.assertEqual(official_row["components"]["goal_value"], 1.0)
        self.assertEqual(official_row["components"]["authority"], 1.0)
        self.assertEqual(novel_row["components"]["goal_value"], 0.6)
        self.assertEqual(novel_row["components"]["authority"], 0.3)
        self.assertIn("exploration share exhausted", novel_row["reason"])
        self.assertIn("exploration share exhausted", stale_row["reason"])
        self.assertTrue(novel_row["reason"].startswith("deferred:"))

        lab.keep_all(key, official)
        _validated, opened = lab.run([lab.fetch(novel_url, rationale="newer forum thread")], 2)
        forum = [row for row in fx.decisions(lab.store) if row["components"].get("node_key") == fx.canon(novel_url)]
        admitted = forum[-1]
        self.assertEqual(admitted["decision"], "selected")
        self.assertEqual(fx.selected_canons(lab.store, opened), [fx.canon(novel_url)])
        self.assertFalse(admitted["components"]["critical_gaps"])
        self.assertEqual(admitted["components"]["exploration_cap"], admitted["components"]["page_slots"])
        self.assertGreater(admitted["components"]["exploration_cap"], 0)
        self.assertNotIn("exploration share exhausted", admitted["reason"])

    def test_ineligible_pages_are_not_admitted(self):
        lab = fx.Lab("blocked")
        self.addCleanup(lab.close)
        good_url = "https://admissions.example.edu/good"
        paused_url = "https://paused.example.edu/info"
        quarantined_url = "https://quarantine.example.edu/item"
        walled_url = "https://robots.example.edu/item"
        rules_url = "https://rules.example.edu/fees"
        later_url = "https://later.example.edu/item"
        root_url = "https://deep.example.edu/root"
        child_url = "https://deep.example.edu/child"
        lab.page(good_url)
        lab.page(paused_url)
        quarantined = lab.page(quarantined_url)
        walled = lab.page(walled_url)
        lab.page(rules_url)
        later = lab.page(later_url)
        root = lab.page(root_url)
        child = lab.page(child_url, parent=root)
        lab.associate(root, depth=lab.policy["max_depth"])
        until = int(time.time()) + 5000
        future = int(time.time()) + 86400
        with lab.store.tx() as db:
            db.execute(
                "UPDATE domains SET status='paused', paused_until=? WHERE domain=?",
                (until, "paused.example.edu"),
            )
            db.execute("UPDATE pages SET quarantined=1 WHERE id=?", (quarantined,))
            db.execute("UPDATE pages SET status='walled', wall_kind='robots' WHERE id=?", (walled,))
            db.execute(
                "UPDATE domains SET robots_txt=? WHERE domain=?",
                ("User-agent: *\nDisallow: /fees", "rules.example.edu"),
            )
            db.execute("UPDATE pages SET next_check_at=? WHERE id=?", (future, later))
            db.execute(
                """INSERT INTO tasks(kind, payload, priority, status, next_attempt_at, created_at, goal_id, epoch)
                   VALUES ('fetch', ?, 0.1, 'pending', 1, 1, ?, ?)""",
                (json.dumps({"page_id": root, "node_key": fx.canon(root_url)}), lab.goal.id, lab.goal.epoch - 1),
            )
        _validated, plan = lab.run([
            lab.fetch(paused_url, rationale="paused domain"),
            lab.fetch(quarantined_url, rationale="quarantined page"),
            lab.fetch(walled_url, rationale="robots wall"),
            lab.fetch(rules_url, rationale="robots rule"),
            lab.fetch(later_url, rationale="not due"),
            lab.fetch(child_url, rationale="too deep"),
            lab.fetch(good_url, rationale="eligible official page"),
        ], 1)
        self.assertEqual(fx.selected_canons(lab.store, plan), [fx.canon(good_url)])
        reasons = [row["reason"] for row in fx.decisions(lab.store)]
        self.assertTrue(any(f"paused until {until}" in reason for reason in reasons))
        self.assertTrue(any("quarantined" in reason for reason in reasons))
        self.assertTrue(any("robots or wall" in reason for reason in reasons))
        self.assertTrue(any("robots disallow" in reason for reason in reasons))
        self.assertTrue(any("not due" in reason for reason in reasons))
        self.assertTrue(any("depth exceeds max_depth" in reason for reason in reasons))
        self.assertTrue(any(f"stale epoch {lab.goal.epoch - 1}" in reason and f"current epoch {lab.goal.epoch}" in reason for reason in reasons))
        self.assertIsNone(lab.store.db.execute("SELECT 1 FROM goal_pages WHERE page_id=?", (child,)).fetchone())
        self.assertEqual(
            lab.store.db.execute("SELECT depth FROM goal_pages WHERE page_id=?", (root,)).fetchone()["depth"],
            lab.policy["max_depth"],
        )
        self.assertEqual(fx.task_count(lab.store, lab.goal.id), 2)

    def test_topic_recheck_does_not_block_a_due_program_field(self):
        lab = fx.Lab("topic")
        self.addCleanup(lab.close)
        key = lab.program("example-computing", "Example Computing", "admissions.example.edu")
        coverage = {"programs": [{
            "program_key": key,
            "name": "Example Computing",
            "official_domain": "admissions.example.edu",
            "unknown_fields": ["tuition"],
            "contradictions": [],
        }]}
        now = int(time.time())
        with lab.store.tx() as db:
            db.execute(
                """INSERT INTO program_field_cooldowns(
                       goal_id, contract_version, program_key, field_key, next_query_at, snapshot_hash)
                   VALUES (?, ?, ?, 'tuition', ?, ?)""",
                (lab.goal.id, lab.contract.version, key, now + 100000, fx.goal.digest([])),
            )
        self.assertEqual(fx.planner.gap_proposals(lab.store, lab.runnable, coverage), [])
        lab.store.db.execute(
            "UPDATE program_field_cooldowns SET snapshot_hash='changed' WHERE program_key=?",
            (key,),
        )
        lab.store.db.execute("UPDATE topics SET last_run_at=? WHERE id=?", (now, lab.topic_id))
        proposals = fx.planner.gap_proposals(lab.store, lab.runnable, coverage)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].kind, "search")
        self.assertEqual(proposals[0].field_key, "tuition")
        self.assertIsNone(proposals[0].url)
        self.assertNotIn("http", proposals[0].query)
        _validated, plan = lab.run(proposals, 1)
        self.assertEqual(len(plan.selected_query_ids), 1)
        via = lab.store.db.execute(
            "SELECT discovered_via FROM queries WHERE id=?",
            (plan.selected_query_ids[0],),
        ).fetchone()["discovered_via"]
        self.assertEqual(via, "gap")
        self.assertEqual(lab.store.db.execute("SELECT COUNT(*) AS n FROM queries WHERE discovered_via='seed'").fetchone()["n"], 0)
        self.assertEqual(lab.store.db.execute("SELECT last_run_at FROM topics WHERE id=?", (lab.topic_id,)).fetchone()["last_run_at"], now)
        self.assertEqual(fx.planner.gap_proposals(lab.store, lab.runnable, coverage), [])
        forced = lab.search(proposals[0].query, origin="gap", program_key=key, field_key="tuition", rationale="force the cooled field")
        _validated, blocked = lab.run([forced], 2)
        self.assertEqual(blocked.selected_query_ids, ())
        self.assertTrue(any(row["reason"] == "deferred: program-field cooldown is active" for row in fx.decisions(lab.store)))
        self.assertEqual(lab.store.db.execute("SELECT last_run_at FROM topics WHERE id=?", (lab.topic_id,)).fetchone()["last_run_at"], now)

    def test_repeated_work_stalls_and_a_new_target_is_not_progress(self):
        lab = fx.Lab("stall")
        self.addCleanup(lab.close)
        first_url = "https://admissions.example.edu/alpha"
        second_url = "https://bursar.example.edu/beta"
        first = lab.page(first_url)
        lab.page(second_url)
        with lab.store.tx() as db:
            db.execute(
                """INSERT INTO source_snapshots(
                       page_id, content_hash, retrieved_at, text_path, visibility, provenance)
                   VALUES (?, 'snap-alpha', 5, 'synthetic/public.txt', 'public', 'fixture')""",
                (first,),
            )
        before_hash = lab.body_hash()
        _validated, _plan = lab.run([lab.fetch(first_url, rationale="first target")], 1)
        first_runtime = _runtime(lab.store, lab.goal.id)
        self.assertNotEqual(first_runtime["substate"], "stalled")
        self.assertNotEqual(first_runtime["substate"], "progressing")
        self.assertEqual(lab.strategy_body(1), {"mode": "research"})
        work = fx.planner.work_fingerprint(lab.store, lab.runnable)
        coverage = fx.planner.coverage_fingerprint(lab.store, lab.runnable)
        lab.store.db.execute("UPDATE source_snapshots SET retrieved_at=50")
        lab.store.db.execute("UPDATE pages SET novelty=0.05 WHERE id=?", (first,))
        self.assertEqual(fx.planner.work_fingerprint(lab.store, lab.runnable), work)
        self.assertEqual(fx.planner.coverage_fingerprint(lab.store, lab.runnable), coverage)
        _validated, _plan = lab.run([lab.fetch(first_url, rationale="same target")], 2)
        stalled = _runtime(lab.store, lab.goal.id)
        self.assertEqual(stalled["substate"], "stalled")
        self.assertIn("repeated work produced no new supported facts", stalled["reason"])
        self.assertEqual(stalled["work_fingerprint"], work)
        self.assertEqual(stalled["coverage_fingerprint"], coverage)
        self.assertNotEqual(stalled["substate"], "progressing")
        _validated, _plan = lab.run([lab.fetch(second_url, rationale="distinct target")], 3)
        moved = _runtime(lab.store, lab.goal.id)
        self.assertNotEqual(moved["substate"], "stalled")
        self.assertNotEqual(moved["substate"], "progressing")
        self.assertNotEqual(fx.planner.work_fingerprint(lab.store, lab.runnable), work)
        self.assertEqual(fx.planner.coverage_fingerprint(lab.store, lab.runnable), coverage)
        self.assertEqual(lab.body_hash(), before_hash)
        self.assertEqual(lab.strategy_body(1), {"mode": "research"})

    def test_two_goals_keep_separate_depths_for_one_page(self):
        first = fx.Lab("share-a")
        self.addCleanup(first.close)
        root_url = "https://admissions.example.edu/root"
        shared_url = "https://admissions.example.edu/shared"
        root = first.page(root_url)
        shared = first.page(shared_url, parent=root)
        page_depth = first.store.db.execute("SELECT depth, parent_page_id FROM pages WHERE id=?", (shared,)).fetchone()
        _validated, _plan = first.run([first.fetch(shared_url, rationale="goal a copy")], 1)
        self.assertEqual(
            first.store.db.execute("SELECT depth FROM goal_pages WHERE goal_id=? AND page_id=?", (first.goal.id, shared)).fetchone()["depth"],
            0,
        )
        second = first.another("share-b")
        first.share_topic(second)
        first.pause()
        second.associate(root, depth=0)
        second.start()
        _validated, _plan = second.run([second.fetch(shared_url, rationale="goal b copy")], 2)
        self.assertEqual(
            second.store.db.execute("SELECT depth FROM goal_pages WHERE goal_id=? AND page_id=?", (first.goal.id, shared)).fetchone()["depth"],
            0,
        )
        self.assertEqual(
            second.store.db.execute("SELECT depth FROM goal_pages WHERE goal_id=? AND page_id=?", (second.goal.id, shared)).fetchone()["depth"],
            1,
        )
        after = second.store.db.execute("SELECT depth, parent_page_id FROM pages WHERE id=?", (shared,)).fetchone()
        self.assertEqual(after["depth"], page_depth["depth"])
        self.assertEqual(after["parent_page_id"], page_depth["parent_page_id"])
        self.assertEqual(after["parent_page_id"], root)

    def test_lifetime_identity_survives_replan(self):
        lab = fx.Lab("identity", policy=fx.execution_policy(lifetime_limits={"queued_nodes": 2}), queued_nodes=2)
        self.addCleanup(lab.close)
        key = lab.program("example-computing", "Example Computing", "admissions.example.edu")
        urls = [
            "https://admissions.example.edu/one",
            "https://bursar.example.edu/two",
            "https://records.example.edu/three",
        ]
        for url in urls:
            lab.page(url)
        proposals = [
            lab.fetch(url, origin="gap", program_key=key, field_key="tuition", rationale=f"tuition evidence {index}")
            for index, url in enumerate(urls)
        ]
        _validated, plan = lab.run(proposals, 1)
        self.assertEqual(len(plan.selected_page_ids), 2)
        self.assertEqual(_identities(lab.store, lab.goal.id), 2)
        denied = [row for row in fx.decisions(lab.store) if "queued_nodes" in row["reason"]]
        self.assertEqual(len(denied), 1)
        self.assertTrue(denied[0]["reason"].startswith("deferred:"))
        lab.replan()
        self.assertEqual(_identities(lab.store, lab.goal.id), 2)
        self.assertTrue(fx.goal.binding_valid(
            lab.store.db, lab.goal.id, lab.contract.version, 1, lab.goal.epoch
        ))
        _validated, again = lab.run([
            lab.fetch(denied[0]["components"]["node_key"], origin="gap", program_key=key, field_key="tuition", rationale="still over the lifetime cap")
        ], 2)
        self.assertEqual(again.selected_page_ids, ())
        self.assertEqual(_identities(lab.store, lab.goal.id), 2)
        self.assertTrue(any("queued_nodes" in row["reason"] for row in fx.decisions(lab.store)))

    def test_prior_strategy_stays_valid_and_replay_does_not_add_a_task(self):
        lab = fx.Lab("prior")
        self.addCleanup(lab.close)
        url = "https://admissions.example.edu/prior"
        lab.page(url)
        before_hash = lab.body_hash()
        epoch = lab.goal.epoch
        admission = lab.goal.current_strategy_version
        proposal = lab.fetch(url, rationale="admission strategy")
        _validated, plan = lab.run([proposal], 4)
        self.assertEqual(len(plan.selected_page_ids), 1)
        task = lab.store.db.execute(
            "SELECT strategy_version, epoch, status FROM tasks WHERE goal_id=? AND kind='fetch'",
            (lab.goal.id,),
        ).fetchone()
        self.assertEqual(task["strategy_version"], admission)
        self.assertEqual(task["epoch"], epoch)
        self.assertEqual(task["status"], "claimed")
        current = fx.goal.get_goal(lab.store, lab.goal.id)
        self.assertEqual(current.current_strategy_version, admission + 1)
        self.assertEqual(current.epoch, epoch)
        self.assertTrue(fx.goal.binding_valid(lab.store.db, lab.goal.id, lab.contract.version, admission, epoch))
        self.assertTrue(fx.goal.binding_valid(lab.store.db, lab.goal.id, lab.contract.version, admission + 1, epoch))
        self.assertFalse(fx.goal.binding_valid(lab.store.db, lab.goal.id, lab.contract.version, admission, epoch + 1))
        self.assertEqual(lab.strategy_body(admission), {"mode": "research"})
        self.assertEqual(lab.body_hash(), before_hash)
        before_tasks = fx.task_count(lab.store, lab.goal.id)
        before_reservations = lab.store.db.execute("SELECT COUNT(*) AS n FROM resource_reservations").fetchone()["n"]
        _validated, replay = lab.run([proposal], 4)
        self.assertEqual(replay.selected_page_ids, ())
        self.assertEqual(fx.task_count(lab.store, lab.goal.id), before_tasks)
        self.assertEqual(lab.store.db.execute("SELECT COUNT(*) AS n FROM resource_reservations").fetchone()["n"], before_reservations)
        self.assertTrue(any(row["reason"] == "deferred: admission replay is not a new dispatch" for row in fx.decisions(lab.store)))
        self.assertEqual(lab.strategy_body(admission), {"mode": "research"})
        self.assertEqual(lab.body_hash(), before_hash)

    def test_reversed_proposal_order_selects_the_same_scores(self):
        def once(slug, reverse):
            lab = fx.Lab(slug)
            try:
                urls = [
                    "https://scholarship.example.edu/award",
                    "https://forum.example/thread",
                ]
                for url in urls:
                    lab.page(url)
                proposals = [lab.fetch(url, rationale="public candidate") for url in urls]
                if reverse:
                    proposals.reverse()
                _validated, plan = lab.run(proposals, 1)
                selected = [
                    (row["components"]["node_key"], row["score"])
                    for row in fx.decisions(lab.store)
                    if row["decision"] == "selected"
                ]
                return fx.selected_canons(lab.store, plan), selected
            finally:
                lab.close()

        forward_canons, forward_scores = once("order-a", False)
        reverse_canons, reverse_scores = once("order-b", True)
        self.assertEqual(forward_canons, reverse_canons)
        self.assertEqual(forward_canons[0], fx.canon("https://scholarship.example.edu/award"))
        self.assertEqual(len(forward_scores), 2)
        for left, right in zip(forward_scores, reverse_scores):
            self.assertEqual(left[0], right[0])
            self.assertTrue(math.isclose(left[1], right[1]))
        self.assertGreater(forward_scores[0][1], forward_scores[1][1])
