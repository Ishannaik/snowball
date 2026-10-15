"""Resource ledger against a real v4 Store and an approved interview goal."""
from __future__ import annotations

import importlib.util
import json
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    key = f"snowball_{name}"
    path = ROOT / "tools" / f"{name}.py"
    existing = sys.modules.get(key)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


goal = _load("goal")
orch = goal._sibling("orchestrator")
ledger = goal._sibling("ledger")
_ORIGINAL_NOW = ledger.now_unix

POLICY = {
    "lifetime_limits": {
        "queued_nodes": 1000,
        "visited_nodes": 1000,
        "fetches": 1000,
        "queries": 1000,
        "model_tokens": 1_000_000,
    },
    "max_depth": 8,
    "max_queue": 100,
    "per_domain_inflight": 10,
    "max_hypotheses_per_wave": 20,
    "exploration_share": 0.25,
    "usage_mode": "estimated",
}
COMPONENTS = {
    "tuition": True,
    "mandatory_fees": True,
    "living_amount": True,
    "living_duration": True,
    "health_insurance": True,
    "visa_costs": False,
    "application_costs": False,
    "travel_costs": False,
    "deposits": False,
    "proof_of_funds_liquidity": False,
}


def answers():
    return {
        "degree_level": "masters",
        "modality": "on_campus",
        "field_tags": ["synthetic-field"],
        "regions": ["synthetic-region"],
        "intake_windows": ["2031-fall"],
        "required_components": dict(COMPONENTS),
        "grants_only": True,
        "accept_conditional_ra_ta": False,
        "accept_competitive_awards": False,
        "max_net_total_cost": {"amount": "0", "currency": "INR"},
        "max_upfront_cash": {"amount": "0", "currency": "INR"},
        "min_living_adequacy_rule": {
            "basis": "same_location_period",
            "count_housing_once": True,
            "count_insurance_once": True,
            "numeric_floor": None,
        },
        "max_work_hours_acceptable": 10,
        "allow_loans": False,
        "citizenship": "SentinelCitizenship",
        "prior_degree": {"level": "bachelors", "field": "synthetic"},
        "completion_min_candidates": 1,
        "execution_policy": POLICY,
        "monitoring": {
            "permitted_work": ["recheck_official_sources"],
            "max_refreshes": 2,
            "interval_days": 14,
        },
        "consent_private_evaluation": True,
    }


def limits(
    *,
    day_fetches=100,
    life_fetches=1000,
    day_queries=100,
    life_queries=1000,
    day_model=10_000,
    life_model=1_000_000,
    queued=1000,
    visited=1000,
    max_depth=2,
    max_queue=10,
    per_domain=2,
    hypotheses=6,
):
    return {
        "day": {"fetches": day_fetches, "queries": day_queries, "model_tokens": day_model},
        "lifetime": {
            "queued_nodes": queued,
            "visited_nodes": visited,
            "fetches": life_fetches,
            "queries": life_queries,
            "model_tokens": life_model,
        },
        "max_depth": max_depth,
        "max_queue": max_queue,
        "per_domain_inflight": per_domain,
        "max_hypotheses_per_wave": hypotheses,
        "exploration_share": "0.25",
        "usage_mode": "estimated",
    }


class ResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        ledger.now_unix = _ORIGINAL_NOW
        self.temp = tempfile.TemporaryDirectory()
        self.store = orch.Store.open(Path(self.temp.name) / "ledger.db")
        self.seq = 0

    def tearDown(self):
        ledger.now_unix = _ORIGINAL_NOW
        self.store.db.close()
        self.temp.cleanup()

    def key(self, name="op"):
        self.seq += 1
        return f"plan-{name}-{self.seq:04d}" + "a" * 8

    def answer_all(self, selected):
        interview = goal._sibling("interview")
        for field, value in answers().items():
            question = interview.question_for(field)
            selected = goal.get_goal(self.store, selected.id)
            goal.record_answer(
                self.store,
                selected.id,
                field,
                value,
                True,
                question.visibility_default,
                selected.row_version,
                "fixture",
            )
        return goal.get_goal(self.store, selected.id)

    def approved(self, slug):
        selected = goal.create_goal(self.store, slug, "Synthetic research control", "fixture")
        selected = self.answer_all(selected)
        interview = goal._sibling("interview")
        selected = goal.get_goal(self.store, selected.id)
        body = interview.build_contract(goal.fields(self.store, selected.id))
        contract = goal.propose_contract(
            self.store,
            selected.id,
            body,
            selected.current_contract_version,
            "fixture",
            expected_row_version=selected.row_version,
        )
        selected = goal.get_goal(self.store, selected.id)
        approved = goal.approve_contract(
            self.store,
            selected.id,
            contract.version,
            contract.body_hash,
            selected.row_version,
            "fixture",
            slug + "-approve",
        )
        return approved

    def grant(self, selected, grant_limits, mode="research", suffix="grant"):
        selected = goal.get_goal(self.store, selected.id)
        granted = goal.grant_execution(
            self.store,
            selected.id,
            selected.current_contract_version,
            mode,
            grant_limits,
            int(time.time()) + 3 * 86400,
            selected.row_version,
            "fixture",
            f"{selected.slug}-{suffix}-{self.seq}",
        )
        self.seq += 1
        return goal.get_goal(self.store, selected.id), granted

    def running(self, slug, grant_limits=None):
        selected = self.approved(slug)
        selected, granted = self.grant(selected, grant_limits or limits())
        selected = goal.transition(
            self.store, selected.id, "start", "", selected.row_version, "fixture", slug + "-start"
        )
        return selected, granted

    def binding(self, selected, granted, **extra):
        payload = {
            "goal_id": selected.id,
            "contract_version": selected.current_contract_version,
            "strategy_version": selected.current_strategy_version,
            "epoch": selected.epoch,
        }
        payload.update(extra)
        return payload

    def reserve(self, selected, granted, operation_key, amounts, **extra):
        return ledger.reserve_batch(
            self.store, granted.id, operation_key, amounts, self.binding(selected, granted, **extra)
        )

    def usage(self, goal_id, counter, window="day", window_key=None):
        if window == "lifetime":
            kind, key = "lifetime", ledger.LIFETIME_WINDOW
        else:
            kind, key = "day", window_key or ledger.ist_day(ledger.now_unix())
        row = self.store.one(
            """SELECT reserved, settled, lost FROM resource_ledger
               WHERE scope_key=? AND window_kind=? AND window_key=? AND counter=?""",
            (f"goal:{goal_id}", kind, key, counter),
        )
        if row is None:
            return (0, 0, 0)
        return (int(row["reserved"]), int(row["settled"]), int(row["lost"]))

    def widen_day(self, grant_id, *, fetches, queries):
        raw = json.loads(self.store.one("SELECT limits_json FROM execution_grants WHERE id=?", (grant_id,))["limits_json"])
        raw["day"]["fetches"] = fetches
        raw["day"]["queries"] = queries
        self.store.db.execute(
            "UPDATE execution_grants SET limits_json=? WHERE id=?", (goal.canonical(raw), grant_id)
        )

    def count(self, table):
        return int(self.store.one(f"SELECT COUNT(*) AS n FROM {table}")["n"])

    def profile_rows(self, goal_id):
        return [
            (row["key"], row["value_json"], row["visibility"])
            for row in self.store.all(
                "SELECT key, value_json, visibility FROM profile_fields WHERE goal_id=? ORDER BY key",
                (goal_id,),
            )
        ]

    def insert_wave(self):
        now = int(time.time())
        return int(
            self.store.db.execute(
                "INSERT INTO waves(started_at, status) VALUES (?, 'running')", (now,)
            ).lastrowid
        )

    def insert_task(self, selected, granted, operation_key, *, kind="fetch", origin="gap", wave_id=None, node_key=None):
        now = int(time.time())
        if wave_id is None:
            wave_id = self.insert_wave()
        payload = {"origin": origin, "node_key": node_key or operation_key}
        task_id = int(
            self.store.db.execute(
                """INSERT INTO tasks(
                     kind, payload, priority, status, next_attempt_at, created_at, wave_id,
                     goal_id, contract_version, strategy_version, epoch, grant_id, operation_key)
                   VALUES (?, ?, 0.5, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    kind,
                    goal.canonical(payload),
                    now,
                    now,
                    wave_id,
                    selected.id,
                    selected.current_contract_version,
                    selected.current_strategy_version,
                    selected.epoch,
                    granted.id,
                    operation_key,
                ),
            ).lastrowid
        )
        return task_id, wave_id

    def test_only_running_or_monitoring_and_replay_is_not_a_dispatch(self):
        selected = self.approved("approved-only")
        selected, granted = self.grant(selected, limits(day_fetches=5, life_fetches=5))
        self.assertEqual(selected.state, "approved")
        profile = self.profile_rows(selected.id)
        with self.assertRaises(goal.ValidationError) as raised:
            self.reserve(selected, granted, self.key(), {"fetches": 1})
        self.assertEqual(raised.exception.field, "binding")
        self.assertEqual(self.count("resource_operations"), 0)
        self.assertEqual(self.profile_rows(selected.id), profile)

        selected = goal.transition(
            self.store, selected.id, "start", "", selected.row_version, "fixture", "approved-only-start"
        )
        operation_key = self.key()
        binding = self.binding(selected, granted, fee={"amount": "1.50", "currency": "USD"})
        first = ledger.reserve_batch(self.store, granted.id, operation_key, {"fetches": 1, "queries": 0}, binding)
        self.assertTrue(first)
        state = ledger.admission_state(self.store, selected.id, operation_key)
        self.assertEqual(state["disposition"], "admitted")
        self.assertEqual(state["state"], "reserved")
        self.assertNotIn("binding_json", state)
        self.assertNotIn("profile_snapshot", json.dumps(state))
        self.assertTrue(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.assertFalse(ledger.mark_dispatched(self.store, operation_key, selected.id))
        again = ledger.reserve_batch(self.store, granted.id, operation_key, {"fetches": 1, "queries": 0}, binding)
        self.assertEqual(again, first)
        self.assertEqual(ledger.admission_state(self.store, selected.id, operation_key)["disposition"], "dispatched")
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (1, 0, 0))
        with self.assertRaises(goal.IdempotencyConflict):
            ledger.reserve_batch(self.store, granted.id, operation_key, {"fetches": 2}, binding)
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        missing = ledger.admission_state(self.store, selected.id, self.key("missing"))
        self.assertEqual(missing["disposition"], "absent")
        self.assertIsNone(missing["state"])
        self.assertEqual(missing["reservations"], [])
        self.assertNotIn("binding_json", missing)

    def test_private_material_and_float_money_write_nothing(self):
        selected, granted = self.running("public-bind", limits())
        profile = self.profile_rows(selected.id)
        before = self.count("resource_operations")
        with self.assertRaises(goal.ValidationError):
            self.reserve(
                selected,
                granted,
                self.key(),
                {"fetches": 1},
                visibility="public",
                profile_snapshot_hash="private-hash",
            )
        with self.assertRaises(goal.ValidationError):
            self.reserve(
                selected,
                granted,
                self.key(),
                {"fetches": 1},
                fee={"amount": 1.5, "currency": "USD"},
            )
        self.assertEqual(self.count("resource_operations"), before)
        self.assertEqual(self.profile_rows(selected.id), profile)
        with self.assertRaises(goal.ValidationError) as raised:
            self.reserve(selected, granted, self.key(), {"fetches": 1}, mode="monitoring")
        self.assertEqual(raised.exception.field, "mode")
        self.assertEqual(self.count("resource_operations"), before)

    def test_shared_role_isolation_and_missing_ceiling(self):
        selected, granted = self.running("shared-role", limits(day_model=100, life_model=100))
        raw = json.loads(self.store.one("SELECT limits_json FROM execution_grants WHERE id=?", (granted.id,))["limits_json"])
        raw["day"]["extract_tokens"] = 50
        raw["day"]["review_tokens"] = 50
        raw["lifetime"]["extract_tokens"] = 50
        raw["lifetime"]["review_tokens"] = 50
        self.store.db.execute(
            "UPDATE execution_grants SET limits_json=? WHERE id=?", (goal.canonical(raw), granted.id)
        )
        before = self.count("resource_reservations")
        with self.assertRaises(goal.ValidationError):
            self.reserve(selected, granted, self.key(), {"model_tokens": 1})
        with self.assertRaises(goal.ValidationError):
            self.reserve(
                selected,
                granted,
                self.key(),
                {"extract_tokens": 1},
                role="review",
                shared_day_limits={"extract_tokens": 40},
            )
        self.assertEqual(self.count("resource_reservations"), before)
        ids = self.reserve(
            selected,
            granted,
            self.key(),
            {"model_tokens": 30, "extract_tokens": 20, "fetches": 1},
            role="extract",
            shared_day_limits={"model_tokens": 80, "extract_tokens": 40, "review_tokens": 40},
        )
        self.assertEqual(len(ids), 8)
        day = ledger.ist_day(ledger.now_unix())

        def shared(scope, counter):
            row = self.store.one(
                """SELECT reserved FROM resource_ledger
                   WHERE scope_key=? AND window_kind='day' AND window_key=? AND counter=?""",
                (scope, day, counter),
            )
            return 0 if row is None else int(row["reserved"])

        self.assertEqual(shared("shared:model", "model_tokens"), 30)
        self.assertEqual(shared("shared:role/extract", "extract_tokens"), 20)
        self.assertEqual(shared("shared:role/review", "review_tokens"), 0)
        self.assertEqual(shared("shared:model", "extract_tokens"), 0)
        self.assertEqual(self.usage(selected.id, "fetches")[0], 1)

    def test_multi_counter_and_failed_claim_roll_back(self):
        selected, granted = self.running("rollback", limits(day_model=10, life_model=10))
        with self.assertRaises(ledger.GrantExhausted):
            self.reserve(
                selected,
                granted,
                self.key(),
                {"fetches": 1, "model_tokens": 11},
                role="extract",
                shared_day_limits={"model_tokens": 10},
            )
        self.assertEqual(self.count("resource_operations"), 0)
        self.assertEqual(self.count("resource_reservations"), 0)
        self.assertEqual(self.count("resource_ledger"), 0)
        self.assertEqual(self.count("goal_node_identities"), 0)
        operation_key = self.key("task")
        self.insert_task(selected, granted, operation_key)
        self.store.db.execute("UPDATE tasks SET status='done' WHERE operation_key=?", (operation_key,))
        with self.assertRaises(goal.ValidationError):
            self.reserve(
                selected,
                granted,
                operation_key,
                {"fetches": 1},
                node_kind="page",
                node_key="https://example.edu/a",
                phase="queued",
                depth=0,
                domain="example.edu",
                task_id=int(self.store.one("SELECT id FROM tasks WHERE operation_key=?", (operation_key,))["id"]),
                lease_token=secrets.token_hex(16),
                lease_until=ledger.now_unix() + 900,
            )
        self.assertEqual(self.count("resource_operations"), 0)
        self.assertEqual(self.count("domain_slots"), 0)

    def test_nested_savepoint_keeps_the_outer_transaction(self):
        selected, granted = self.running("nested", limits(day_fetches=1, life_fetches=1))
        with self.store.tx() as db:
            db.execute("UPDATE goals SET title=? WHERE id=?", ("kept-title", selected.id))
            with self.assertRaises(ledger.GrantExhausted):
                self.reserve(selected, granted, self.key(), {"fetches": 2})
            self.assertEqual(db.execute("SELECT title FROM goals WHERE id=?", (selected.id,)).fetchone()["title"], "kept-title")
        self.assertEqual(self.store.one("SELECT title FROM goals WHERE id=?", (selected.id,))["title"], "kept-title")
        self.assertEqual(self.count("resource_reservations"), 0)
        self.assertFalse(self.store.db.in_transaction)

    def test_headroom_is_read_only_inside_a_transaction_and_on_a_readonly_connection(self):
        selected, granted = self.running("headroom", limits(day_fetches=5, life_fetches=5))
        operation_key = self.key()
        with self.store.tx() as db:
            self.reserve(selected, granted, operation_key, {"fetches": 1})
            room = ledger.headroom(self.store, granted.id, ledger.now_unix())
            self.assertEqual(room["day"]["counters"]["fetches"]["reserved"], 1)
            self.assertTrue(self.store.db.in_transaction)
            db.execute("UPDATE goals SET title=? WHERE id=?", ("visible", selected.id))
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.one("SELECT title FROM goals WHERE id=?", (selected.id,))["title"], "visible")
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        readonly = orch.Store.connect_existing(self.store.path, readonly=True)
        try:
            room = ledger.headroom(readonly, granted.id, ledger.ist_day(ledger.now_unix()))
            self.assertEqual(room["day"]["counters"]["fetches"]["remaining"], 4)
            self.assertFalse(readonly.db.in_transaction)
            traced = []
            readonly.db.set_trace_callback(traced.append)
            again = ledger.headroom(readonly, granted.id, ledger.ist_day(ledger.now_unix()))
            readonly.db.set_trace_callback(None)
            self.assertEqual(again["day"]["counters"]["fetches"]["remaining"], 4)
            self.assertFalse(readonly.db.in_transaction)
            statement = " ".join(traced).upper()
            self.assertNotIn("BEGIN IMMEDIATE", statement)
            with self.assertRaises(sqlite3.OperationalError) as raised:
                readonly.db.execute("UPDATE goals SET title=? WHERE id=?", ("nope", selected.id))
            self.assertIn("readonly", str(raised.exception).lower())
        finally:
            readonly.db.close()

    def test_eight_workers_race_five_allowances_and_old_occurrence_cannot_commit(self):
        selected, granted = self.running(
            "race",
            limits(day_fetches=5, life_fetches=5, day_queries=5, life_queries=5, per_domain=8, max_queue=20),
        )
        binding = self.binding(selected, granted)
        keys = [f"plan-{index:032x}" for index in range(8)]
        tasks = []
        until = ledger.now_unix() + 900
        for index, operation_key in enumerate(keys):
            task_id, _wave = self.insert_task(
                selected, granted, operation_key, node_key=f"https://w{index}.example.edu/page"
            )
            tasks.append(task_id)
        barrier = threading.Barrier(8)
        admitted, denied, problems = [], [], []

        def work(index):
            local = orch.Store.connect_existing(self.store.path)
            try:
                barrier.wait(30)
                payload = dict(binding)
                payload.update(
                    node_kind="page",
                    node_key=f"https://w{index}.example.edu/page",
                    phase="queued",
                    depth=0,
                    domain=f"w{index}.example.edu",
                    task_id=tasks[index],
                    lease_token=f"{index:032x}",
                    lease_until=until,
                )
                try:
                    ids = ledger.reserve_batch(local, granted.id, keys[index], {"fetches": 1}, payload)
                    admitted.append((keys[index], tasks[index], ids))
                except ledger.GrantExhausted:
                    denied.append(tasks[index])
                except Exception as exc:
                    problems.append(exc)
            except Exception as exc:
                problems.append(exc)
            finally:
                local.db.close()

        threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(problems, [])
        self.assertEqual(len(admitted), 5)
        self.assertEqual(len(denied), 3)
        self.assertEqual(self.usage(selected.id, "fetches"), (5, 0, 0))
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (5, 0, 0))
        claimed_ids = {task_id for _key, task_id, _ids in admitted}
        for task_id in claimed_ids:
            row = self.store.one("SELECT status, occurrence_seq FROM tasks WHERE id=?", (task_id,))
            self.assertEqual(row["status"], "claimed")
            self.assertEqual(int(row["occurrence_seq"]), 1)
        for task_id in denied:
            row = self.store.one("SELECT status, occurrence_seq FROM tasks WHERE id=?", (task_id,))
            self.assertEqual(row["status"], "pending")
            self.assertEqual(int(row["occurrence_seq"]), 0)
        stale_key, stale_task, stale_ids = admitted[0]
        self.store.db.execute(
            """UPDATE tasks
               SET status='pending', worker_id=NULL, occurrence_seq=occurrence_seq+1
               WHERE id=? AND status='claimed' AND occurrence_seq=1""",
            (stale_task,),
        )
        self.assertFalse(ledger.mark_dispatched(self.store, stale_key, selected.id))
        self.assertEqual(ledger.admission_state(self.store, selected.id, stale_key)["disposition"], "stale")
        with self.assertRaises(goal.ValidationError):
            ledger.settle(self.store, stale_ids[0], 1)
        self.assertEqual(self.usage(selected.id, "fetches"), (5, 0, 0))
        self.assertEqual(
            self.store.one("SELECT status, occurrence_seq FROM tasks WHERE id=?", (stale_task,))["status"],
            "pending",
        )
        self.assertEqual(
            int(self.store.one("SELECT occurrence_seq FROM tasks WHERE id=?", (stale_task,))["occurrence_seq"]),
            2,
        )
        live_key, live_task, live_ids = admitted[1]
        self.assertTrue(ledger.mark_dispatched(self.store, live_key, selected.id))
        self.store.db.execute(
            """UPDATE tasks
               SET status='pending', worker_id=NULL, occurrence_seq=occurrence_seq+1
               WHERE id=? AND status='claimed' AND occurrence_seq=1""",
            (live_task,),
        )
        with self.assertRaises(goal.ValidationError):
            ledger.settle(self.store, live_ids[0], 1)
        self.assertEqual(self.usage(selected.id, "fetches"), (5, 0, 0))
        self.assertEqual(ledger.admission_state(self.store, selected.id, live_key)["disposition"], "dispatched")
        self.assertFalse(ledger.mark_dispatched(self.store, live_key, selected.id))
        stale_index = keys.index(stale_key)
        replay_binding = dict(binding)
        replay_binding.update(
            node_kind="page",
            node_key=f"https://w{stale_index}.example.edu/page",
            phase="queued",
            depth=0,
            domain=f"w{stale_index}.example.edu",
            task_id=stale_task,
            lease_token=f"{stale_index:032x}",
            lease_until=until,
        )
        replay = ledger.reserve_batch(self.store, granted.id, stale_key, {"fetches": 1}, replay_binding)
        self.assertEqual(replay, stale_ids)
        self.assertEqual(self.usage(selected.id, "fetches"), (5, 0, 0))
        with self.assertRaises(ledger.GrantExhausted):
            self.reserve(selected, granted, "plan-" + "f" * 32, {"fetches": 1})
        self.assertEqual(self.count("resource_operations"), 5)

    def test_expiry_hard_mode_and_revoked_grant_write_nothing(self):
        selected, granted = self.running("expiry", limits())
        before = self.count("resource_operations")
        ledger.now_unix = lambda: int(granted.expires_at)
        with self.assertRaises(goal.ValidationError):
            self.reserve(selected, granted, self.key(), {"fetches": 1})
        ledger.now_unix = lambda: int(granted.starts_at) - 10
        with self.assertRaises(goal.ValidationError):
            self.reserve(selected, granted, self.key(), {"fetches": 1})
        ledger.now_unix = _ORIGINAL_NOW
        raw = json.loads(self.store.one("SELECT limits_json FROM execution_grants WHERE id=?", (granted.id,))["limits_json"])
        raw["usage_mode"] = "hard"
        self.store.db.execute(
            "UPDATE execution_grants SET limits_json=? WHERE id=?", (goal.canonical(raw), granted.id)
        )
        with self.assertRaises(goal.ValidationError) as raised:
            self.reserve(selected, granted, self.key(), {"fetches": 1})
        self.assertEqual(raised.exception.field, "usage_mode")
        raw["usage_mode"] = "estimated"
        self.store.db.execute(
            "UPDATE execution_grants SET limits_json=? WHERE id=?", (goal.canonical(raw), granted.id)
        )
        selected = goal.get_goal(self.store, selected.id)
        _selected, replaced = self.grant(selected, limits(), suffix="replace")
        with self.assertRaises(goal.ValidationError):
            self.reserve(selected, granted, self.key(), {"fetches": 1})
        self.assertEqual(self.count("resource_operations"), before)
        self.assertNotEqual(replaced.id, granted.id)
        raw = json.loads(self.store.one("SELECT limits_json FROM execution_grants WHERE id=?", (replaced.id,))["limits_json"])
        raw["exploration_share"] = 0.25
        self.store.db.execute(
            "UPDATE execution_grants SET limits_json=? WHERE id=?",
            (json.dumps(raw), replaced.id),
        )
        selected = goal.get_goal(self.store, selected.id)
        self.reserve(selected, replaced, self.key("float-share"), {"fetches": 1})
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        raw["exploration_share"] = True
        self.store.db.execute(
            "UPDATE execution_grants SET limits_json=? WHERE id=?",
            (json.dumps(raw), replaced.id),
        )
        with self.assertRaises(goal.ValidationError) as raised:
            self.reserve(selected, replaced, self.key("bool-share"), {"fetches": 1})
        self.assertEqual(raised.exception.field, "exploration_share")
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        self.assertEqual(self.count("resource_operations"), before + 1)

    def test_renewal_keeps_lifetime_usage_and_requires_the_new_epoch(self):
        selected, granted = self.running(
            "renew", limits(day_fetches=5, life_fetches=5, day_queries=5, life_queries=5)
        )
        self.widen_day(granted.id, fetches=100, queries=5)
        original = self.binding(selected, granted)
        epoch_before = selected.epoch
        keys = []
        for _ in range(5):
            operation_key = self.key("life")
            keys.append(operation_key)
            ledger.reserve_batch(self.store, granted.id, operation_key, {"fetches": 1}, original)
            self.assertTrue(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (5, 0, 0))
        selected = goal.get_goal(self.store, selected.id)
        selected, replaced = self.grant(
            selected, limits(day_fetches=5, life_fetches=5, day_queries=5, life_queries=5), suffix="renew"
        )
        self.widen_day(replaced.id, fetches=100, queries=5)
        self.assertGreater(selected.epoch, epoch_before)
        replay = ledger.reserve_batch(self.store, replaced.id, keys[0], {"fetches": 1}, original)
        self.assertTrue(replay)
        self.assertEqual(ledger.admission_state(self.store, selected.id, keys[0])["disposition"], "dispatched")
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (5, 0, 0))
        room = ledger.headroom(self.store, replaced.id, ledger.now_unix())
        self.assertEqual(room["day"]["counters"]["fetches"]["remaining"], 95)
        self.assertEqual(room["lifetime"]["counters"]["fetches"]["remaining"], 0)
        self.assertEqual(room["lifetime"]["window_key"], ledger.LIFETIME_WINDOW)
        stale = dict(self.binding(selected, replaced))
        stale["epoch"] = selected.epoch - 1
        with self.assertRaises(goal.ValidationError) as raised:
            ledger.reserve_batch(self.store, replaced.id, self.key("old-epoch"), {"fetches": 1}, stale)
        self.assertEqual(raised.exception.field, "binding")
        with self.assertRaises(ledger.GrantExhausted) as exhausted:
            self.reserve(selected, replaced, self.key("sixth"), {"fetches": 1})
        self.assertEqual(exhausted.exception.counter, "fetches")
        self.assertEqual(exhausted.exception.used, 5)
        self.assertEqual(exhausted.exception.ceiling, 5)
        self.assertIsNone(exhausted.exception.resets_at)
        self.assertIn("grant exhausted for fetches: used 5 of 5", str(exhausted.exception))
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (5, 0, 0))
        with self.assertRaises(goal.ValidationError) as revoked:
            self.reserve(selected, granted, self.key("revoked"), {"fetches": 1})
        self.assertIn("revoked", revoked.exception.message)
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (5, 0, 0))

    def test_prior_strategy_dispatches_and_stale_fences_do_not(self):
        selected, granted = self.running("fence", limits())
        operation_key = self.key("page")
        task_id, _wave = self.insert_task(selected, granted, operation_key, node_key="https://example.edu/fence")
        lease = secrets.token_hex(16)
        until = ledger.now_unix() + 900
        self.reserve(
            selected,
            granted,
            operation_key,
            {"fetches": 1, "queued_nodes": 1},
            node_kind="page",
            node_key="https://example.edu/fence",
            phase="queued",
            depth=0,
            domain="example.edu",
            task_id=task_id,
            lease_token=lease,
            lease_until=until,
        )
        self.assertEqual(
            int(self.store.one("SELECT occurrence_seq FROM tasks WHERE id=?", (task_id,))["occurrence_seq"]), 1
        )
        self.store.db.execute("UPDATE goals SET epoch=epoch+1 WHERE id=?", (selected.id,))
        self.assertFalse(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.assertEqual(ledger.admission_state(self.store, selected.id, operation_key)["disposition"], "stale")
        self.assertEqual(self.store.one("SELECT state FROM resource_operations WHERE operation_key=?", (operation_key,))["state"], "reserved")
        self.store.db.execute("UPDATE goals SET epoch=epoch-1 WHERE id=?", (selected.id,))
        self.store.db.execute("UPDATE tasks SET occurrence_seq=occurrence_seq+1 WHERE id=?", (task_id,))
        self.assertFalse(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.store.db.execute("UPDATE tasks SET occurrence_seq=occurrence_seq-1 WHERE id=?", (task_id,))
        self.store.db.execute("UPDATE domain_slots SET lease_token=? WHERE task_id=?", ("stolen-token", task_id))
        self.assertFalse(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.store.db.execute("UPDATE domain_slots SET lease_token=? WHERE task_id=?", (lease, task_id))
        self.store.db.execute("DELETE FROM domain_slots WHERE task_id=?", (task_id,))
        self.assertFalse(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        self.store.db.execute(
            """INSERT INTO domain_slots(domain, task_id, operation_id, lease_token, lease_until)
               VALUES (?, ?, (SELECT id FROM resource_operations WHERE operation_key=?), ?, ?)""",
            ("example.edu", task_id, operation_key, lease, until),
        )
        self.store.db.execute("UPDATE tasks SET lease_token=? WHERE id=?", (lease, task_id))
        selected = goal.get_goal(self.store, selected.id)
        epoch = selected.epoch
        strategy = selected.current_strategy_version
        selected = goal.transition(
            self.store, selected.id, "replan", "keep the admitted strategy", selected.row_version, "fixture", "fence-replan"
        )
        self.assertEqual(selected.epoch, epoch)
        self.assertEqual(selected.current_strategy_version, strategy + 1)
        self.assertTrue(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.assertEqual(ledger.admission_state(self.store, selected.id, operation_key)["disposition"], "dispatched")
        fresh = self.key("prior")
        self.reserve(
            selected,
            granted,
            fresh,
            {"fetches": 1},
            strategy_version=strategy,
        )
        self.assertEqual(self.usage(selected.id, "fetches"), (2, 0, 0))

    def test_queries_charge_every_operation_and_node_keys_may_contain_spaces(self):
        selected, granted = self.running("queries", limits())
        node_key = "masters funding query"
        first = self.key("q1")
        second = self.key("q2")
        extra = {
            "node_kind": "query",
            "node_key": node_key,
            "phase": "queued",
            "depth": 0,
        }
        self.reserve(selected, granted, first, {"queries": 1, "queued_nodes": 1}, **extra)
        self.reserve(selected, granted, second, {"queries": 1, "queued_nodes": 1}, **extra)
        self.assertEqual(self.usage(selected.id, "queries"), (2, 0, 0))
        self.assertEqual(self.usage(selected.id, "queries", "lifetime"), (2, 0, 0))
        self.assertEqual(self.usage(selected.id, "queued_nodes", "lifetime"), (1, 0, 0))
        self.assertEqual(self.count("goal_node_identities"), 1)

    def test_depth_queue_domain_and_hypothesis_caps(self):
        selected, granted = self.running(
            "caps",
            limits(max_depth=1, max_queue=1, per_domain=1, hypotheses=2, day_queries=10, life_queries=10),
        )
        with self.assertRaises(goal.ValidationError) as raised:
            self.reserve(
                selected,
                granted,
                self.key("deep"),
                {"fetches": 1, "queued_nodes": 1},
                node_kind="page",
                node_key="https://example.edu/deep",
                phase="queued",
                depth=2,
            )
        self.assertEqual(raised.exception.field, "depth")
        self.assertEqual(self.count("goal_node_identities"), 0)
        blocked_key = self.key("blocked")
        blocked_task, _wave = self.insert_task(selected, granted, blocked_key)
        body = json.loads(
            self.store.one(
                "SELECT body_json FROM goal_contracts WHERE goal_id=? AND version=?",
                (selected.id, selected.current_contract_version),
            )["body_json"]
        )
        body["execution_policy"]["blocked_domains"] = ["evil.example"]
        body["execution_policy"]["paused_domains"] = ["paused.example"]
        self.store.db.execute(
            "UPDATE goal_contracts SET body_json=? WHERE goal_id=? AND version=?",
            (goal.canonical(body), selected.id, selected.current_contract_version),
        )
        with self.assertRaises(goal.ValidationError):
            self.reserve(
                selected,
                granted,
                blocked_key,
                {"fetches": 1},
                node_kind="page",
                node_key="https://evil.example/a",
                phase="queued",
                depth=0,
                domain="evil.example",
                task_id=blocked_task,
                lease_token=secrets.token_hex(16),
                lease_until=ledger.now_unix() + 900,
            )
        self.assertEqual(self.count("domain_slots"), 0)
        self.assertEqual(self.count("goal_node_identities"), 0)
        self.reserve(
            selected,
            granted,
            self.key("q1"),
            {"fetches": 1, "queued_nodes": 1},
            node_kind="page",
            node_key="https://example.edu/one",
            phase="queued",
            depth=1,
        )
        with self.assertRaises(ledger.GrantExhausted) as exhausted:
            self.reserve(
                selected,
                granted,
                self.key("q2"),
                {"fetches": 1, "queued_nodes": 1},
                node_kind="page",
                node_key="https://example.edu/two",
                phase="queued",
                depth=1,
            )
        self.assertEqual(exhausted.exception.counter, "max_queue")
        self.assertEqual(self.count("goal_node_identities"), 1)
        raw = json.loads(self.store.one("SELECT limits_json FROM execution_grants WHERE id=?", (granted.id,))["limits_json"])
        raw["max_queue"] = 10
        self.store.db.execute(
            "UPDATE execution_grants SET limits_json=? WHERE id=?", (goal.canonical(raw), granted.id)
        )

        wave = self.insert_wave()
        for index, origin in enumerate(("hypothesis", "hypothesis", "hypothesis", "gap")):
            operation_key = self.key(origin + str(index))
            self.insert_task(
                selected,
                granted,
                operation_key,
                kind="search",
                origin=origin,
                wave_id=wave,
                node_key=f"hypothesis {index}",
            )
            kwargs = {
                "node_kind": "query",
                "node_key": f"hypothesis {index}",
                "phase": "queued",
                "depth": 0,
            }
            if index == 2:
                with self.assertRaises(ledger.GrantExhausted) as exhausted:
                    self.reserve(selected, granted, operation_key, {"queries": 1, "queued_nodes": 1}, **kwargs)
                self.assertEqual(exhausted.exception.counter, "max_hypotheses_per_wave")
                self.assertEqual(exhausted.exception.used, 2)
                self.assertEqual(exhausted.exception.ceiling, 2)
            else:
                self.reserve(selected, granted, operation_key, {"queries": 1, "queued_nodes": 1}, **kwargs)
        self.assertEqual(self.usage(selected.id, "queries"), (3, 0, 0))

    def test_domain_loss_releases_the_slot_and_fences_occurrence_once(self):
        selected, granted = self.running("domain", limits(per_domain=1))
        first_key = self.key("d1")
        second_key = self.key("d2")
        first_task, _wave = self.insert_task(selected, granted, first_key, node_key="https://example.edu/a")
        second_task, _wave = self.insert_task(selected, granted, second_key, node_key="https://example.edu/b")
        lease = secrets.token_hex(16)
        until = ledger.now_unix() + 900
        ids = self.reserve(
            selected,
            granted,
            first_key,
            {"fetches": 1},
            node_kind="page",
            node_key="https://example.edu/a",
            phase="queued",
            depth=0,
            domain="example.edu",
            task_id=first_task,
            lease_token=lease,
            lease_until=until,
        )
        self.store.db.execute("UPDATE domain_slots SET lease_until=? WHERE task_id=?", (1, first_task))
        with self.assertRaises(ledger.GrantExhausted) as exhausted:
            self.reserve(
                selected,
                granted,
                second_key,
                {"fetches": 1},
                node_kind="page",
                node_key="https://example.edu/b",
                phase="queued",
                depth=0,
                domain="example.edu",
                task_id=second_task,
                lease_token=secrets.token_hex(16),
                lease_until=until,
            )
        self.assertEqual(exhausted.exception.counter, "per_domain_inflight")
        self.assertEqual(self.count("domain_slots"), 1)
        self.assertFalse(ledger.mark_dispatched(self.store, first_key, selected.id))
        self.store.db.execute("UPDATE domain_slots SET lease_until=? WHERE task_id=?", (until, first_task))
        self.assertTrue(ledger.mark_dispatched(self.store, first_key, selected.id))
        claimed = int(self.store.one("SELECT occurrence_seq FROM tasks WHERE id=?", (first_task,))["occurrence_seq"])
        for reservation_id in ids:
            ledger.mark_lost(self.store, reservation_id)
        self.assertEqual(self.count("domain_slots"), 0)
        self.assertEqual(int(self.store.one("SELECT occurrence_seq FROM tasks WHERE id=?", (first_task,))["occurrence_seq"]), claimed + 1)
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (first_task,))["status"], "failed")
        self.assertEqual(self.usage(selected.id, "fetches"), (0, 0, 1))
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (0, 0, 1))
        for reservation_id in ids:
            ledger.mark_lost(self.store, reservation_id)
        self.assertEqual(int(self.store.one("SELECT occurrence_seq FROM tasks WHERE id=?", (first_task,))["occurrence_seq"]), claimed + 1)
        self.assertEqual(ledger.admission_state(self.store, selected.id, first_key)["disposition"], "lost")
        with self.assertRaises(goal.ValidationError):
            ledger.release_undispatched(self.store, first_key, selected.id)
        self.assertFalse(ledger.mark_dispatched(self.store, first_key, selected.id))

    def test_stolen_lease_cannot_settle_and_overrun_is_not_clamped(self):
        selected, granted = self.running("settle", limits(day_fetches=100, life_fetches=100))
        operation_key = self.key("pay")
        task_id, _wave = self.insert_task(selected, granted, operation_key)
        lease = secrets.token_hex(16)
        ids = self.reserve(
            selected,
            granted,
            operation_key,
            {"queries": 5},
            node_kind="page",
            node_key="https://example.edu/pay",
            phase="queued",
            depth=0,
            domain="example.edu",
            task_id=task_id,
            lease_token=lease,
            lease_until=ledger.now_unix() + 900,
        )
        self.assertTrue(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.store.db.execute("UPDATE tasks SET lease_token=? WHERE id=?", ("other-token", task_id))
        with self.assertRaises(goal.ValidationError) as raised:
            ledger.settle(self.store, ids[0], 2)
        self.assertEqual(raised.exception.field, "lease")
        self.assertEqual(self.usage(selected.id, "queries"), (5, 0, 0))
        self.store.db.execute("UPDATE tasks SET lease_token=? WHERE id=?", (lease, task_id))
        self.store.db.execute("UPDATE goals SET epoch=epoch+1 WHERE id=?", (selected.id,))
        ledger.settle_operation(self.store, operation_key, selected.id, {"queries": 2})
        self.assertEqual(self.usage(selected.id, "queries"), (0, 2, 0))
        room = ledger.headroom(self.store, granted.id, ledger.now_unix())
        self.assertEqual(room["day"]["counters"]["queries"]["remaining"], 98)
        ledger.settle(self.store, ids[0], 2)
        with self.assertRaises(goal.IdempotencyConflict):
            ledger.settle(self.store, ids[0], 3)
        self.assertEqual(self.usage(selected.id, "queries"), (0, 2, 0))
        selected = goal.get_goal(self.store, selected.id)

        overrun_key = self.key("over")
        self.reserve(selected, granted, overrun_key, {"fetches": 100})
        ledger.settle_operation(self.store, overrun_key, selected.id, {"fetches": 150})
        self.assertEqual(self.usage(selected.id, "fetches"), (0, 150, 0))
        room = ledger.headroom(self.store, granted.id, ledger.now_unix())
        self.assertEqual(room["day"]["counters"]["fetches"]["used"], 150)
        self.assertEqual(room["day"]["counters"]["fetches"]["remaining"], -50)
        self.assertEqual(self.store.one("SELECT state FROM execution_grants WHERE id=?", (granted.id,))["state"], "exhausted")
        with self.assertRaises(ledger.GrantExhausted) as exhausted:
            self.reserve(selected, granted, self.key("later"), {"fetches": 1})
        self.assertEqual(exhausted.exception.used, 150)
        self.assertEqual(exhausted.exception.ceiling, 100)
        self.assertIn("grant exhausted for fetches: used 150 of 100", str(exhausted.exception))

    def test_release_only_provably_undispatched_work(self):
        selected, granted = self.running("release", limits())
        node = {
            "node_kind": "page",
            "node_key": "https://example.edu/once",
            "phase": "queued",
            "depth": 0,
        }
        first = self.key("u1")
        second = self.key("u2")
        self.reserve(selected, granted, first, {"fetches": 1, "queued_nodes": 1}, **node)
        self.reserve(selected, granted, second, {"fetches": 1, "queued_nodes": 1}, **node)
        with self.assertRaises(goal.ValidationError):
            ledger.mark_lost(self.store, int(self.store.one(
                "SELECT id FROM resource_reservations WHERE operation_key=? ORDER BY id LIMIT 1", (first,)
            )["id"]))
        ledger.release_undispatched(self.store, first, selected.id)
        ledger.release_undispatched(self.store, first, selected.id)
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        self.assertEqual(self.usage(selected.id, "queued_nodes", "lifetime"), (0, 1, 0))
        self.assertEqual(self.count("goal_node_identities"), 1)
        self.assertEqual(ledger.admission_state(self.store, selected.id, first)["disposition"], "released")
        self.assertFalse(ledger.mark_dispatched(self.store, first, selected.id))
        self.assertTrue(ledger.mark_dispatched(self.store, second, selected.id))
        with self.assertRaises(goal.ValidationError):
            ledger.release_undispatched(self.store, second, selected.id)
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))

    def test_cancelled_key_admits_again_and_a_dispatched_key_does_not(self):
        selected, granted = self.running(
            "cancel-key",
            limits(day_fetches=5, life_fetches=5, max_queue=2),
        )
        node = {
            "node_kind": "page",
            "node_key": "https://example.edu/again",
            "phase": "queued",
            "depth": 0,
        }
        operation_key = self.key("again")
        self.reserve(selected, granted, operation_key, {"fetches": 1, "queued_nodes": 1}, **node)
        ledger.release_undispatched(self.store, operation_key, selected.id)
        self.assertEqual(self.usage(selected.id, "fetches"), (0, 0, 0))
        self.assertEqual(self.usage(selected.id, "queued_nodes", "lifetime"), (0, 0, 0))
        self.assertEqual(self.count("goal_node_identities"), 0)
        self.assertEqual(ledger.admission_state(self.store, selected.id, operation_key)["disposition"], "released")
        again = self.reserve(selected, granted, operation_key, {"fetches": 1, "queued_nodes": 1}, **node)
        self.assertTrue(again)
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        self.assertEqual(self.usage(selected.id, "queued_nodes", "lifetime"), (1, 0, 0))
        self.assertEqual(self.reserve(selected, granted, operation_key, {"fetches": 1, "queued_nodes": 1}, **node), again)
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        self.assertTrue(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.assertEqual(self.reserve(selected, granted, operation_key, {"fetches": 1, "queued_nodes": 1}, **node), again)
        self.assertEqual(ledger.admission_state(self.store, selected.id, operation_key)["disposition"], "dispatched")
        self.assertFalse(ledger.mark_dispatched(self.store, operation_key, selected.id))
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))

        fresh = self.key("fresh")
        fresh_node = {
            "node_kind": "page",
            "node_key": "https://example.edu/fresh",
            "phase": "queued",
            "depth": 0,
        }
        self.reserve(selected, granted, fresh, {"fetches": 1, "queued_nodes": 1}, **fresh_node)
        self.assertEqual(self.usage(selected.id, "fetches"), (2, 0, 0))
        self.assertEqual(self.count("resource_operations"), 2)
        selected = goal.get_goal(self.store, selected.id)
        selected, replaced = self.grant(
            selected, limits(day_fetches=5, life_fetches=5, max_queue=2), suffix="replace"
        )
        self.assertEqual(
            self.store.one("SELECT state FROM resource_operations WHERE operation_key=?", (fresh,))["state"],
            "cancelled",
        )
        self.assertEqual(
            self.store.one("SELECT state FROM resource_operations WHERE operation_key=?", (operation_key,))["state"],
            "dispatched",
        )
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        self.assertEqual(self.usage(selected.id, "queued_nodes", "lifetime"), (1, 0, 0))
        reopened = self.reserve(selected, replaced, fresh, {"fetches": 1, "queued_nodes": 1}, **fresh_node)
        self.assertTrue(reopened)
        self.assertEqual(self.count("resource_operations"), 2)
        self.assertEqual(self.usage(selected.id, "fetches"), (2, 0, 0))
        self.assertEqual(self.usage(selected.id, "queued_nodes", "lifetime"), (2, 0, 0))
        self.assertEqual(ledger.admission_state(self.store, selected.id, fresh)["disposition"], "admitted")
        self.assertEqual(ledger.admission_state(self.store, selected.id, operation_key)["disposition"], "dispatched")
        with self.assertRaises(ledger.GrantExhausted) as raised:
            self.reserve(
                selected,
                replaced,
                self.key("third"),
                {"fetches": 1, "queued_nodes": 1},
                node_kind="page",
                node_key="https://example.edu/third",
                phase="queued",
                depth=0,
            )
        self.assertEqual(raised.exception.counter, "max_queue")
        self.assertEqual(self.usage(selected.id, "fetches"), (2, 0, 0))
        self.assertEqual(self.usage(selected.id, "queued_nodes", "lifetime"), (2, 0, 0))

    def test_ist_admission_day_survives_midnight_and_loss_is_not_refunded(self):
        selected, granted = self.running("ist", limits(day_fetches=1, life_fetches=5, day_queries=1, life_queries=5))
        start = datetime.fromtimestamp(int(granted.starts_at), ledger.IST)
        if start.hour < 23 or (start.hour == 23 and start.minute < 30):
            evening = start.replace(hour=23, minute=30, second=0, microsecond=0)
        else:
            evening = (start + timedelta(days=1)).replace(hour=23, minute=30, second=0, microsecond=0)
        morning = evening + timedelta(hours=1)
        self.assertLess(int(morning.timestamp()), int(granted.expires_at))
        evening_stamp = int(evening.timestamp())
        morning_stamp = int(morning.timestamp())
        ledger.now_unix = lambda: evening_stamp
        kept = self.key("kept")
        lost_key = self.key("lost")
        self.reserve(selected, granted, kept, {"fetches": 1})
        lost_ids = self.reserve(selected, granted, lost_key, {"queries": 1})
        self.assertTrue(ledger.mark_dispatched(self.store, lost_key, selected.id))
        for reservation_id in lost_ids:
            ledger.mark_lost(self.store, reservation_id)
        admission_day = ledger.ist_day(evening_stamp)
        ledger.now_unix = lambda: morning_stamp
        ledger.settle_operation(self.store, kept, selected.id, {"fetches": 1})
        next_day = ledger.ist_day(morning_stamp)
        self.assertNotEqual(admission_day, next_day)
        windows = {
            row["window_key"]
            for row in self.store.all(
                "SELECT window_key FROM resource_reservations WHERE operation_key=? AND window_kind='day'",
                (kept,),
            )
        }
        self.assertEqual(windows, {admission_day})
        old = ledger.headroom(self.store, granted.id, admission_day)
        new = ledger.headroom(self.store, granted.id, next_day)
        self.assertEqual(old["day"]["counters"]["fetches"]["settled"], 1)
        self.assertEqual(old["day"]["counters"]["fetches"]["remaining"], 0)
        self.assertEqual(old["day"]["counters"]["queries"]["lost"], 1)
        self.assertEqual(new["day"]["counters"]["fetches"]["remaining"], 1)
        self.assertEqual(new["day"]["counters"]["queries"]["remaining"], 1)
        self.assertEqual(new["day"]["counters"]["queries"]["lost"], 0)
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (0, 1, 0))
        self.assertEqual(self.usage(selected.id, "queries", "lifetime"), (0, 0, 1))
        self.reserve(selected, granted, self.key("next"), {"fetches": 1})
        self.assertEqual(self.usage(selected.id, "fetches", "lifetime"), (1, 1, 0))
        self.assertEqual(self.usage(selected.id, "queries", "lifetime"), (0, 0, 1))

    def test_monitoring_grant_uses_the_new_epoch(self):
        selected, granted = self.running("monitor", limits())
        selected = goal.transition(
            self.store, selected.id, "pause", "switch", selected.row_version, "fixture", "monitor-pause"
        )
        selected = goal.finish_pause(self.store, selected.id)
        self.assertEqual(selected.state, "paused")
        selected, monitoring = self.grant(selected, limits(), mode="monitoring", suffix="monitor")
        selected = goal.transition(
            self.store,
            selected.id,
            "monitoring",
            "",
            selected.row_version,
            "fixture",
            "monitor-arm",
        )
        self.assertEqual(selected.state, "monitoring")
        self.reserve(selected, monitoring, self.key("mon"), {"fetches": 1}, mode="monitoring")
        self.assertEqual(self.usage(selected.id, "fetches"), (1, 0, 0))
        with self.assertRaises(goal.ValidationError):
            self.reserve(selected, monitoring, self.key("bad-mode"), {"fetches": 1}, mode="research")
        with self.assertRaises(goal.ValidationError):
            self.reserve(selected, granted, self.key("revoked"), {"fetches": 1}, mode="monitoring")
        self.assertEqual(self.count("resource_operations"), 1)


if __name__ == "__main__":
    unittest.main()
