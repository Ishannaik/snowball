"""STOP-aware supervisor. Goal transitions stay with the operator.

The loop calls the canonical orchestrator on this thread. Network and model
workers stay inside that orchestrator; this module does not call engines and
does not share the Store across threads. Ledger methods own reservation
accounting. A STOP file or KeyboardInterrupt returns without changing goal state.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))
_ROOT = Path(__file__).resolve().parent.parent


def _sibling(name: str) -> Any:
    """Load tools/<name>.py as snowball_<name>. Never put tools/ on sys.path."""
    key = f"snowball_{name}"
    path = _ROOT / "tools" / f"{name}.py"
    existing = sys.modules.get(key)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _database_path(orch_mod: Any, cfg: Any, db_path: Path | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    configured = getattr(cfg, "db_path", None)
    if configured:
        return Path(configured)
    return Path(orch_mod.DB_PATH)


def _resolve_db_path(orch_mod: Any, cfg: Any, db_path: Path | None) -> Path | None:
    """Explicit path wins. A delegated caller with an open store must not fall through to the personal database.

    SnowballOrchestrator.supervise calls supervise() without db_path. Walk the caller frames for
    that orchestrator's store so a wrong frame offset cannot open data/snowball.db.
    """
    if db_path is not None:
        return Path(db_path)
    frame = sys._getframe(1)
    saw_open_store = False
    for _ in range(8):
        frame = frame.f_back
        if frame is None:
            break
        store = getattr(frame.f_locals.get("self"), "store", None)
        if store is None or not hasattr(store, "db"):
            continue
        saw_open_store = True
        path = getattr(store, "path", None)
        if path:
            return Path(path)
    if saw_open_store:
        return None
    return _database_path(orch_mod, cfg, None)


def _open_source(orch_mod: Any, path: Path, dry: bool) -> Any:
    if not path.exists():
        orch_mod.log("database missing; supervisor idling without creating a file")
        return orch_mod.Store.in_memory()
    return orch_mod.Store.connect_existing(path, readonly=bool(dry))


def _latest_grant(store: Any) -> Any:
    goal_mod = _sibling("goal")
    selected = goal_mod.get_goal(store)
    if selected is None:
        return None
    return store.one(
        "SELECT * FROM execution_grants WHERE goal_id=? ORDER BY id DESC LIMIT 1",
        (selected.id,),
    )


def _active_headroom_exhausted(store: Any, now_ts: int) -> bool:
    goal_mod = _sibling("goal")
    if goal_mod.runnable_reason(store, now_ts) != "runnable":
        return False
    selected = goal_mod.get_goal(store)
    grant = goal_mod.active_grant(store, selected.id) if selected is not None else None
    if grant is None:
        return False
    try:
        room = _sibling("ledger").headroom(store, grant.id, _sibling("ledger").ist_day(now_ts))
    except Exception as exc:
        _sibling("orchestrator").log(f"headroom unavailable: {type(exc).__name__}")
        return False
    for window in ("day", "lifetime"):
        counters = (room.get(window) or {}).get("counters") or {}
        for name in ("fetches", "queries"):
            info = counters.get(name)
            remaining = info.get("remaining", 1) if isinstance(info, dict) else 1
            if isinstance(remaining, int) and not isinstance(remaining, bool) and remaining <= 0:
                return True
    return False


def _classify(store: Any, result: dict, prior_reason: str) -> str:
    outcome = str(result.get("outcome") or "")
    reason = str(result.get("reason") or prior_reason or "")
    if outcome in ("goal-paused", "goal_paused") or reason == "goal_paused":
        return "goal-paused"
    if outcome in ("research-ready", "research_ready") or reason == "research_ready":
        return "research-ready"
    if outcome in ("expiry", "grant-expired", "grant_expired") or reason == "grant_expired":
        return "expiry"
    if outcome in ("exhausted", "grant-exhausted", "grant_exhausted") or reason == "grant_exhausted":
        return "exhausted"
    if outcome == "outage":
        return "outage"
    if outcome == "budget-sleep":
        return "budget-sleep"
    if outcome == "dry-frontier":
        return "dry-frontier"
    if outcome == "monitoring":
        return "monitoring"
    substate = result.get("substate")
    if substate in ("exhausted",):
        return "exhausted"
    if substate in ("outage", "verifier_unavailable"):
        return "outage"
    if outcome in ("no-runnable-goal", "no_runnable_goal") or reason in (
        "no_goal",
        "not_running",
        "no_grant",
        "no_contract",
        "mode_mismatch",
    ):
        grant = _latest_grant(store)
        if grant is not None and grant["state"] == "exhausted":
            return "exhausted"
        if grant is not None and grant["state"] == "expired":
            return "expiry"
        return "no-runnable-goal"
    return "ok"


def _as_due(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _reset_hint(result: dict, status: dict) -> int | None:
    for source in (result, status):
        if not isinstance(source, dict):
            continue
        for key in ("resets_at", "reset_at", "next_reset_at"):
            found = _as_due(source.get(key))
            if found is not None:
                return found
    return None


def _ist_midnight(now_ts: int) -> int:
    current = datetime.fromtimestamp(now_ts, tz=IST)
    nxt = datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    return int(nxt.timestamp())


def _monitoring_delay(store: Any, result: dict, status: dict, now_ts: int) -> int:
    sources: list[dict] = []
    for source in (result, status):
        if isinstance(source, dict):
            sources.append(source)
            goal = source.get("goal")
            if isinstance(goal, dict):
                sources.append(goal)
    for source in sources:
        for key in ("next_due", "next_refresh_at", "next_snapshot_at"):
            due = _as_due(source.get(key))
            if due is not None:
                return min(1800, max(1, due - now_ts))
    try:
        row = store.one(
            "SELECT MIN(next_check_at) AS due FROM pages WHERE next_check_at IS NOT NULL AND next_check_at > ?",
            (now_ts,),
        )
    except Exception:
        row = None
    due = _as_due(row["due"]) if row is not None else None
    if due is not None:
        return min(1800, max(1, due - now_ts))
    return 1800


def _delay(kind: str, result: dict, status: dict, store: Any, interval: int, now_ts: int) -> int:
    if kind == "goal-paused":
        return 60
    if kind == "research-ready":
        return 1800
    if kind == "no-runnable-goal":
        return 300
    if kind == "expiry":
        return 300
    if kind == "outage":
        return min(900, interval * 15)
    if kind == "budget-sleep":
        return min(1800, interval * 5)
    if kind == "dry-frontier":
        return min(1800, interval * 30)
    if kind == "exhausted":
        resets = _reset_hint(result, status)
        if resets is None:
            resets = _ist_midnight(now_ts)
        return min(1800, max(60, resets - now_ts))
    if kind == "monitoring":
        return _monitoring_delay(store, result, status, now_ts)
    return interval


def _read_status(orch: Any) -> dict:
    try:
        status = orch.status()
    except Exception as exc:
        _sibling("orchestrator").log(f"status unavailable: {type(exc).__name__}")
        return {}
    return status if isinstance(status, dict) else {}


def _one_cycle(orch: Any, max_topics: int, slug: str | None, interval: int) -> tuple[str, int] | None:
    orch_mod = _sibling("orchestrator")
    goal_mod = _sibling("goal")
    store = orch.store
    now_ts = orch_mod.now()
    reason = goal_mod.runnable_reason(store, now_ts)
    if reason == "runnable" and _active_headroom_exhausted(store, now_ts):
        status = _read_status(orch)
        kind = "exhausted"
        return kind, _delay(kind, {}, status, store, interval, now_ts)
    result = orch.run(max_topics, slug)
    if not isinstance(result, dict):
        result = {"outcome": "outage", "reason": "invalid-run-result"}
    if result.get("stopped") or result.get("outcome") == "stopped":
        return None
    status = _read_status(orch)
    kind = _classify(store, result, reason)
    if kind == "ok":
        selected = goal_mod.get_goal(store)
        if selected is not None and selected.state == "monitoring":
            kind = "monitoring"
    now_ts = orch_mod.now()
    return kind, _delay(kind, result, status, store, interval, now_ts)


def _abandoned_tasks(store: Any, worker_id: str, stale_before: int) -> list[Any]:
    return store.all(
        """SELECT id, worker_id, heartbeat_at FROM tasks
           WHERE status='claimed' AND (
             worker_id IS NULL OR worker_id!=? OR heartbeat_at IS NULL OR heartbeat_at<?
           )""",
        (worker_id, stale_before),
    )


def _live_claim(store: Any, task_id: int | None, worker_id: str, stale_before: int) -> bool:
    if task_id is None:
        return False
    task = store.one(
        "SELECT status, worker_id, heartbeat_at FROM tasks WHERE id=?",
        (task_id,),
    )
    if task is None or task["status"] != "claimed" or task["worker_id"] != worker_id:
        return False
    heartbeat = task["heartbeat_at"]
    return heartbeat is not None and int(heartbeat) >= stale_before


def reclaim_abandoned(orch: Any) -> dict[str, Any]:
    """Fence dead workers and hand unresolved usage to the ledger.

    Dispatched work whose owner is gone is marked lost. Reserved work that
    was never dispatched is released. Occurrence fences go through orch.reap
    after a dead worker's heartbeat is expired; this process's fresh claims
    are left alone. Goal rows are not updated.
    """
    store = orch.store
    ledger = _sibling("ledger")
    orch_mod = _sibling("orchestrator")
    now_ts = orch_mod.now()
    stale_before = now_ts - 600
    worker_id = orch.worker_id
    lost: list[int] = []
    released: list[str] = []
    rows = store.all(
        """SELECT o.goal_id AS goal_id, o.operation_key AS operation_key, o.state AS op_state,
                  r.id AS reservation_id, r.task_id AS task_id
           FROM resource_operations o
           JOIN resource_reservations r
             ON r.grant_id=o.grant_id AND r.operation_key=o.operation_key
           WHERE r.state='reserved' AND o.state IN ('reserved', 'dispatched')"""
    )
    grouped: dict[tuple[int, str, str], list[Any]] = {}
    for row in rows:
        grouped.setdefault((int(row["goal_id"]), str(row["operation_key"]), str(row["op_state"])), []).append(row)
    for (goal_id, operation_key, op_state), reservations in grouped.items():
        live = any(
            _live_claim(store, None if item["task_id"] is None else int(item["task_id"]), worker_id, stale_before)
            for item in reservations
        )
        if live:
            continue
        if op_state == "dispatched":
            for item in reservations:
                ledger.mark_lost(store, int(item["reservation_id"]))
                lost.append(int(item["reservation_id"]))
        elif op_state == "reserved":
            try:
                ledger.release_undispatched(store, operation_key, goal_id)
            except Exception as exc:
                orch_mod.log(f"restart release skipped {operation_key}: {type(exc).__name__}")
            else:
                released.append(operation_key)
    abandoned = _abandoned_tasks(store, worker_id, stale_before)
    reclaimed = 0
    if abandoned:
        ids = [int(row["id"]) for row in abandoned]
        marks = ",".join("?" for _ in ids)
        with store.tx() as db:
            db.execute(
                f"UPDATE tasks SET heartbeat_at=? WHERE status='claimed' AND id IN ({marks})",
                (stale_before - 30, *ids),
            )
        reclaimed = int(orch.reap() or 0)
    orch_mod.log(
        f"restart reclaim lost={len(lost)} released={len(released)} reclaimed_tasks={reclaimed}"
    )
    return {
        "lost_reservation_ids": lost,
        "released_operation_keys": released,
        "reclaimed_tasks": reclaimed,
    }


def supervise(
    cfg: Any,
    topics_file: Path | str | None,
    dry: bool = False,
    *,
    db_path: Path | str | None = None,
    max_topics: int = 1,
    interval_sec: int = 60,
    slug_filter: str | None = None,
    stop_file: Path | str | None = None,
) -> int:
    """Idle until a runnable goal exists. Returns 0 on STOP or KeyboardInterrupt."""
    orch_mod = _sibling("orchestrator")
    interval = 60 if isinstance(interval_sec, bool) or not isinstance(interval_sec, int) or interval_sec < 1 else interval_sec
    topics = 1 if isinstance(max_topics, bool) or not isinstance(max_topics, int) or max_topics < 1 else max_topics
    topics_path = None if topics_file in (None, "") else Path(topics_file)
    stop = Path(stop_file) if stop_file is not None else orch_mod.STOP_FILE
    source_path = _resolve_db_path(orch_mod, cfg, None if db_path is None else Path(db_path))
    source = orch_mod.Store.in_memory() if source_path is None else _open_source(orch_mod, source_path, dry)
    orch = None
    planned: list[tuple[str, int]] = []
    supervise.planned = planned
    supervise.reclaim = {"lost_reservation_ids": [], "released_operation_keys": [], "reclaimed_tasks": 0}
    try:
        try:
            orch = orch_mod.SnowballOrchestrator(
                cfg, source, mode=getattr(cfg, "mode", "auto") or "auto", dry=dry, topics_path=topics_path,
            )
            orch_mod.log(f"Starting Snowball supervisor loop (pid {os.getpid()})")
            if not dry:
                supervise.reclaim = reclaim_abandoned(orch)
        except KeyboardInterrupt:
            orch_mod.log("Supervisor interrupted by user")
            return 0
        while True:
            if stop.exists():
                orch_mod.log("STOP file present; supervisor stopping")
                return 0
            try:
                cycle = _one_cycle(orch, topics, slug_filter, interval)
                if cycle is None:
                    orch_mod.log("Supervisor wave finished: outcome=stopped")
                    return 0
                kind, delay = cycle
                planned.append((kind, delay))
                orch_mod.log(f"Supervisor wave finished: outcome={kind} sleep={delay}")
                time.sleep(delay)
            except KeyboardInterrupt:
                orch_mod.log("Supervisor interrupted by user")
                return 0
            except Exception as exc:
                orch_mod.log(f"Supervisor loop error: {type(exc).__name__}")
                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    orch_mod.log("Supervisor interrupted by user")
                    return 0
    finally:
        extra = getattr(orch, "store", None) if orch is not None else None
        if extra is not None and extra is not source:
            extra.db.close()
        source.db.close()
