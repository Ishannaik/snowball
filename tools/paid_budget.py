#!/usr/bin/env python3
"""Atomic paid-fetch reservations in integer micro-USD.

Spend is reserved under BEGIN IMMEDIATE before a provider call. The liability of a row is its
settled actual cost when that cost is known, otherwise the reserved worst case. Unsettled rows
and settled rows whose actual cost is NULL keep that worst case across IST day and month
rollover. A missing or zero tariff disables the provider. Confirmed non-billable failures are
the only settlement that releases the reservation.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _TOOLS_DIR]
if (
    getattr(sys.modules.get("queue"), "__file__", None)
    and Path(sys.modules["queue"].__file__).resolve().parent == _TOOLS_DIR
):
    del sys.modules["queue"]

import sqlite3
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))
CONFIRMED_NONBILLABLE = "confirmed_nonbillable_failure"
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS paid_budget_reservations (
        id INTEGER PRIMARY KEY,
        acquisition_id TEXT NOT NULL CHECK (length(acquisition_id) > 0),
        provider TEXT NOT NULL CHECK (length(provider) > 0),
        reserved_micro_usd INTEGER NOT NULL CHECK (reserved_micro_usd >= 0),
        actual_micro_usd INTEGER CHECK (actual_micro_usd IS NULL OR actual_micro_usd >= 0),
        settled INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1)),
        outcome TEXT,
        task_ref TEXT,
        is_solver INTEGER NOT NULL DEFAULT 0 CHECK (is_solver IN (0, 1)),
        created_at_utc TEXT NOT NULL,
        window_day_ist TEXT NOT NULL CHECK (
            window_day_ist GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        window_month_ist TEXT NOT NULL CHECK (window_month_ist GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]')
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paid_budget_reservations_day
        ON paid_budget_reservations(window_day_ist, provider, settled)
    """,
    """
    CREATE INDEX IF NOT EXISTS paid_budget_reservations_month
        ON paid_budget_reservations(window_month_ist, provider, is_solver)
    """,
)
_LOCKS_GUARD = threading.Lock()
_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_FALLBACK: dict[int, threading.Lock] = {}


class PaidBudgetExhausted(Exception):
    """The reservation would exceed a ceiling, or the provider route has no usable tariff."""


@dataclass(frozen=True)
class Reservation:
    id: int
    acquisition_id: str
    provider: str
    reserved_micro_usd: int
    settled: bool
    actual_micro_usd: int | None
    outcome: str | None
    created_at_utc: str
    window_day_ist: str
    window_month_ist: str


def ist_windows(moment: datetime | None = None) -> tuple[str, str]:
    """Return `(YYYY-MM-DD, YYYY-MM)` in IST. `moment` must be timezone-aware."""
    current = datetime.now(timezone.utc) if moment is None else moment
    if current.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    local = current.astimezone(IST)
    return local.date().isoformat(), local.strftime("%Y-%m")


def ensure_schema(store: Any) -> None:
    """Create the reservation table inside the store's immediate transaction."""
    with _tx(store):
        return


def reserve(
    store: Any,
    acquisition_id: str,
    provider: str,
    max_cost_micro_usd: int,
    is_solver: bool = False,
    caps: dict | None = None,
) -> Reservation:
    """Reserve worst-case spend. Concurrent callers share the SQLite reserved lock."""
    acquisition = _text(acquisition_id, "acquisition_id")
    provider_name = _text(provider, "provider")
    if not isinstance(is_solver, bool):
        raise ValueError("is_solver must be a bool")
    if caps is None:
        raise PaidBudgetExhausted("caps_missing")
    if not isinstance(caps, Mapping):
        raise PaidBudgetExhausted("bad_caps")
    cost = _micro_usd(max_cost_micro_usd, allow_zero=False)
    tariff = _tariff(caps, provider_name)
    if cost < tariff:
        raise PaidBudgetExhausted("reservation_below_tariff")
    moment = datetime.now(timezone.utc)
    day, month = ist_windows(moment)
    created = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _tx(store) as db:
        _reject_if_over(db, caps, provider_name, cost, is_solver, day, month)
        cursor = db.execute(
            """
            INSERT INTO paid_budget_reservations (
                acquisition_id, provider, reserved_micro_usd, actual_micro_usd, settled, outcome,
                task_ref, is_solver, created_at_utc, window_day_ist, window_month_ist
            ) VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?, ?, ?, ?)
            """,
            (acquisition, provider_name, cost, 1 if is_solver else 0, created, day, month),
        )
        row = db.execute(
            "SELECT * FROM paid_budget_reservations WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        if row is None:
            raise RuntimeError("reservation insert did not persist")
        return _reservation(row)


def settle(
    store: Any,
    reservation_id: int,
    actual_cost_micro_usd: int | None = None,
    outcome: str | None = None,
    task_ref: str | None = None,
) -> None:
    """Record the provider result. NULL or unconfirmed zero keeps the reserved worst case."""
    if isinstance(reservation_id, bool) or not isinstance(reservation_id, int):
        raise ValueError("reservation_id")
    if actual_cost_micro_usd is not None:
        _micro_usd(actual_cost_micro_usd, allow_zero=True)
    if outcome is not None and not isinstance(outcome, str):
        raise ValueError("outcome")
    if task_ref is not None and (not isinstance(task_ref, str) or "\x00" in task_ref):
        raise ValueError("task_ref")
    with _tx(store) as db:
        row = db.execute(
            "SELECT reserved_micro_usd FROM paid_budget_reservations WHERE id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise LookupError(reservation_id)
        reserved = _value(row, "reserved_micro_usd")
        stored_actual, stored_outcome = _settled_charge(reserved, actual_cost_micro_usd, outcome)
        updated = db.execute(
            """
            UPDATE paid_budget_reservations
               SET settled = 1, actual_micro_usd = ?, outcome = ?, task_ref = COALESCE(?, task_ref)
             WHERE id = ?
            """,
            (stored_actual, stored_outcome, task_ref, reservation_id),
        )
        if updated.rowcount == 0:
            raise LookupError(reservation_id)


def _settled_charge(reserved: int, actual: int | None, outcome: str | None) -> tuple[int | None, str | None]:
    if outcome == CONFIRMED_NONBILLABLE and (actual is None or actual == 0):
        return 0, outcome
    if actual is None or actual == 0:
        # Unconfirmed zero and unknown results stay at the reserved worst case.
        return None, outcome if outcome is not None else "unknown"
    if actual < 0:
        raise PaidBudgetExhausted("bad_amount")
    return min(actual, reserved), outcome


def _reject_if_over(
    db: sqlite3.Connection,
    caps: Mapping[str, Any],
    provider: str,
    cost: int,
    is_solver: bool,
    day: str,
    month: str,
) -> None:
    provider_caps = _provider_caps(caps, provider)
    _check_micro(db, cost, day, "window_day_ist", None, _optional_cap(caps, ("daily_micro_usd",)), "daily_micro_usd")
    _check_micro(
        db, cost, month, "window_month_ist", None, _optional_cap(caps, ("monthly_micro_usd",)), "monthly_micro_usd"
    )
    _check_micro(
        db,
        cost,
        day,
        "window_day_ist",
        provider,
        _optional_cap(provider_caps, ("daily_micro_usd",)),
        "provider_daily_micro_usd",
    )
    _check_micro(
        db,
        cost,
        month,
        "window_month_ist",
        provider,
        _optional_cap(provider_caps, ("monthly_micro_usd",)),
        "provider_monthly_micro_usd",
    )
    if is_solver:
        _check_count(
            db,
            day,
            "window_day_ist",
            True,
            None,
            _optional_cap(caps, ("daily_solver_count", "solver_count")),
            "daily_solver_count",
        )
        _check_count(
            db,
            month,
            "window_month_ist",
            True,
            None,
            _optional_cap(caps, ("monthly_solver_count",)),
            "monthly_solver_count",
        )
        _check_count(
            db,
            day,
            "window_day_ist",
            True,
            provider,
            _optional_cap(provider_caps, ("daily_solver_count",)),
            "provider_daily_solver_count",
        )
        _check_count(
            db,
            month,
            "window_month_ist",
            True,
            provider,
            _optional_cap(provider_caps, ("monthly_solver_count",)),
            "provider_monthly_solver_count",
        )
    else:
        _check_count(
            db,
            day,
            "window_day_ist",
            False,
            None,
            _optional_cap(caps, ("daily_retrieval_count", "retrieval_count")),
            "daily_retrieval_count",
        )
    _check_count(
        db,
        day,
        "window_day_ist",
        None,
        provider,
        _optional_cap(provider_caps, ("daily_count",)),
        "provider_daily_count",
    )
    _check_count(
        db,
        month,
        "window_month_ist",
        None,
        provider,
        _optional_cap(provider_caps, ("monthly_count",)),
        "provider_monthly_count",
    )


def _check_micro(
    db: sqlite3.Connection,
    cost: int,
    window_value: str,
    window_column: str,
    provider: str | None,
    cap: int | None,
    reason: str,
) -> None:
    if cap is None:
        return
    if _liability(db, window_value, window_column, provider) + cost > cap:
        raise PaidBudgetExhausted(reason)


def _check_count(
    db: sqlite3.Connection,
    window_value: str,
    window_column: str,
    solver: bool | None,
    provider: str | None,
    cap: int | None,
    reason: str,
) -> None:
    if cap is None:
        return
    if _volume(db, window_value, window_column, solver, provider) + 1 > cap:
        raise PaidBudgetExhausted(reason)


def _liability(db: sqlite3.Connection, window_value: str, window_column: str, provider: str | None) -> int:
    """Known actuals count in their own IST window. Everything else counts at reserved_micro_usd."""
    _window_column(window_column)
    where = ""
    args: list[object] = [window_value]
    if provider is not None:
        where = "WHERE provider = ?"
        args.append(provider)
    sql = f"""
        SELECT COALESCE(SUM(
            CASE
                WHEN settled = 1 AND actual_micro_usd IS NOT NULL AND {window_column} = ? THEN actual_micro_usd
                WHEN settled = 1 AND actual_micro_usd IS NOT NULL THEN 0
                ELSE reserved_micro_usd
            END
        ), 0) AS n
        FROM paid_budget_reservations
        {where}
    """
    return _cell(db.execute(sql, args).fetchone())


def _volume(
    db: sqlite3.Connection,
    window_value: str,
    window_column: str,
    solver: bool | None,
    provider: str | None,
) -> int:
    _window_column(window_column)
    clauses = [
        "NOT (settled = 1 AND actual_micro_usd = 0 AND outcome = ?)",
        f"({window_column} = ? OR settled = 0 OR actual_micro_usd IS NULL)",
    ]
    args: list[object] = [CONFIRMED_NONBILLABLE, window_value]
    if solver is not None:
        clauses.append("is_solver = ?")
        args.append(1 if solver else 0)
    if provider is not None:
        clauses.append("provider = ?")
        args.append(provider)
    where = " AND ".join(clauses)
    row = db.execute(f"SELECT COUNT(*) AS n FROM paid_budget_reservations WHERE {where}", args).fetchone()
    return _cell(row)


def _window_column(name: str) -> None:
    if name not in {"window_day_ist", "window_month_ist"}:
        raise ValueError(name)


def _tariff(caps: Mapping[str, Any], provider: str) -> int:
    tariffs = caps.get("tariffs") or {}
    if not isinstance(tariffs, Mapping):
        raise PaidBudgetExhausted("bad_tariffs")
    if provider in tariffs:
        value: object = tariffs[provider]
    else:
        nested = _provider_caps(caps, provider)
        if "tariff_micro_usd" not in nested:
            raise PaidBudgetExhausted(f"tariff_missing:{provider}")
        value = nested["tariff_micro_usd"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaidBudgetExhausted(f"tariff_disabled:{provider}")
    return value


def _provider_caps(caps: Mapping[str, Any], provider: str) -> Mapping[str, Any]:
    raw = caps.get("providers") or {}
    if not isinstance(raw, Mapping):
        raise PaidBudgetExhausted("bad_provider_caps")
    found = raw.get(provider) or {}
    if not isinstance(found, Mapping):
        raise PaidBudgetExhausted("bad_provider_caps")
    return found


def _optional_cap(caps: Mapping[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        if name in caps and caps[name] is not None:
            return _nonneg_cap(caps[name], name)
    return None


def _nonneg_cap(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaidBudgetExhausted(f"bad_cap:{name}")
    return value


def _micro_usd(value: object, *, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("micro-USD amounts must be integers")
    if value < 0 or (value == 0 and not allow_zero):
        raise PaidBudgetExhausted("bad_amount")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError(name)
    return value


def _reservation(row: sqlite3.Row | tuple) -> Reservation:
    actual = _optional(row, "actual_micro_usd")
    outcome = _optional(row, "outcome")
    return Reservation(
        id=int(_value(row, "id")),
        acquisition_id=str(_value(row, "acquisition_id")),
        provider=str(_value(row, "provider")),
        reserved_micro_usd=int(_value(row, "reserved_micro_usd")),
        settled=bool(_value(row, "settled")),
        actual_micro_usd=None if actual is None else int(actual),
        outcome=None if outcome is None else str(outcome),
        created_at_utc=str(_value(row, "created_at_utc")),
        window_day_ist=str(_value(row, "window_day_ist")),
        window_month_ist=str(_value(row, "window_month_ist")),
    )


def _value(row: sqlite3.Row | tuple, column: str) -> object:
    if isinstance(row, sqlite3.Row):
        return row[column]
    columns = (
        "id",
        "acquisition_id",
        "provider",
        "reserved_micro_usd",
        "actual_micro_usd",
        "settled",
        "outcome",
        "task_ref",
        "is_solver",
        "created_at_utc",
        "window_day_ist",
        "window_month_ist",
    )
    return row[columns.index(column)]


def _optional(row: sqlite3.Row | tuple, column: str) -> object:
    return _value(row, column)


def _cell(row: sqlite3.Row | tuple | None) -> int:
    if row is None:
        return 0
    if isinstance(row, sqlite3.Row):
        return int(row["n"])
    return int(row[0])


def _lock_for(key: object) -> threading.Lock:
    try:
        with _LOCKS_GUARD:
            lock = _LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _LOCKS[key] = lock
            return lock
    except TypeError:
        with _LOCKS_GUARD:
            return _FALLBACK.setdefault(id(key), threading.Lock())


@contextmanager
def _tx(store: Any) -> Iterator[sqlite3.Connection]:
    if not hasattr(store, "tx"):
        raise TypeError("store.tx() is required")
    db_key = getattr(store, "db", store)
    with _lock_for(db_key), store.tx() as db:
        _ensure(db)
        yield db


def _ensure(db: sqlite3.Connection) -> None:
    if db.row_factory is None:
        db.row_factory = sqlite3.Row
    for statement in _SCHEMA:
        db.execute(statement)


__all__ = [
    "CONFIRMED_NONBILLABLE",
    "IST",
    "PaidBudgetExhausted",
    "Reservation",
    "ensure_schema",
    "ist_windows",
    "reserve",
    "settle",
]
