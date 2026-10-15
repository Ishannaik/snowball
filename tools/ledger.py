"""Goal execution resource ledger.

Reservations are absolute per goal: day and lifetime usage survive grant
replacement and contract revision. Day rows keep the IST admission date.
Shared model/role scopes are parallel ledgers, not a second charge in the
goal total.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
LIFETIME_WINDOW = "all"
MODEL_COUNTERS = frozenset(
    {"model_tokens", "extract_tokens", "review_tokens", "plan_tokens"}
)
ROLE_FOR_COUNTER = {
    "extract_tokens": "extract",
    "review_tokens": "review",
    "plan_tokens": "plan",
}
# Unique identities charge once. queries/query/fetches charge on every new operation key.
IDENTITY_COUNTERS = frozenset(
    {"page", "pages", "nodes", "queued", "queued_nodes", "visited_nodes"}
)
REQUEUED_IDENTITIES = frozenset({"page", "pages", "nodes", "queued", "queued_nodes"})
ADMISSION_KEYS = frozenset(
    {
        "admitted_at",
        "admission_time",
        "admission_at",
        "admitted_on",
        "claimed_occurrence",
        "occurrence_fenced",
    }
)
PRIVATE_KEYS = frozenset(
    {
        "profile",
        "profile_snapshot",
        "profile_snapshot_json",
        "profile_snapshot_hash",
        "profile_fields",
        "body_hash",
        "frame_hash",
        "private_hash",
        "value_json",
    }
)
_CONN_ATTRS = ("conn", "connection", "_conn", "db")
_BUSY_MS = 8000

__all__ = [
    "GrantExhausted",
    "IST",
    "admission_state",
    "headroom",
    "ist_day",
    "mark_dispatched",
    "mark_lost",
    "now_unix",
    "release_undispatched",
    "reserve_batch",
    "settle",
    "settle_operation",
]


class GrantExhausted(Exception):
    """Admission denied because a configured ceiling cannot hold the request."""

    def __init__(self, counter: str, used: int, ceiling: int, resets_at: int | None):
        self.counter = counter
        self.used = int(used)
        self.ceiling = int(ceiling)
        self.resets_at = None if resets_at is None else int(resets_at)
        super().__init__(
            f"grant exhausted for {self.counter}: used {self.used} of {self.ceiling}"
        )


def now_unix() -> int:
    """Unix seconds. Tests may replace this function to pin admission time."""
    return int(time.time())


def ist_day(ts: int) -> str:
    """IST calendar date for a unix timestamp, YYYY-MM-DD."""
    return datetime.fromtimestamp(int(ts), tz=IST).date().isoformat()


def _resets_at(window_kind: str, window_key: str) -> int | None:
    if window_kind != "day":
        return None
    year, month, day = (int(part) for part in window_key.split("-"))
    nxt = datetime(year, month, day, tzinfo=IST) + timedelta(days=1)
    return int(nxt.timestamp())


def _sibling(name: str):
    """Load tools/<name>.py as snowball_<name> without putting tools/ on sys.path."""
    canonical = f"snowball_{name}"
    loaded = sys.modules.get(canonical)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / f"{name}.py"
    resolved = path.resolve()
    for mod in list(sys.modules.values()):
        mod_file = getattr(mod, "__file__", None)
        if not mod_file:
            continue
        try:
            if Path(mod_file).resolve() == resolved:
                sys.modules[canonical] = mod
                return mod
        except OSError:
            continue
    spec = importlib.util.spec_from_file_location(canonical, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling {name} from {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(canonical, None)
        raise
    return module


def _register_self() -> None:
    sys.modules.setdefault("snowball_ledger", sys.modules[__name__])


_register_self()


def _goal():
    return _sibling("goal")


def _validation(field: str, message: str):
    raise _goal().ValidationError(field, message)


def _raise_conflict(operation: str, key: str) -> None:
    raise _goal().IdempotencyConflict(
        f"idempotency conflict for {operation} key {key}"
    )


def _require_binding(conn, goal_id: int, contract_version: int, strategy_version: int, epoch: int) -> None:
    """Admission uses the goal helper directly. A prior strategy on the same epoch stays valid."""
    ok = _goal().binding_valid(
        conn,
        int(goal_id),
        int(contract_version),
        int(strategy_version),
        int(epoch),
    )
    if ok is not True:
        _validation("binding", "goal binding is not valid")


def canonical_json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decimal_string(text: str) -> bool:
    body = text[1:] if text.startswith("-") else text
    if not body:
        return False
    whole, dot, frac = body.partition(".")
    if not whole.isdigit():
        return False
    if len(whole) > 1 and whole.startswith("0"):
        return False
    if dot and (not frac or not frac.isdigit()):
        return False
    return True


def _iso_currency(text: str) -> bool:
    return len(text) == 3 and text.isalpha() and text.isupper()


def _jsonable(value, field: str):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if field == "exploration_share" or field.endswith(".exploration_share"):
            if not math.isfinite(value) or value < 0 or value > 1:
                _validation("exploration_share", "exploration_share must be within 0 and 1")
            return value
        _validation(field, "float values are not permitted")
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _validation(field, "object keys must be strings")
            clean[key] = _jsonable(item, f"{field}.{key}")
        if "amount" in clean and "currency" in clean:
            amount = clean["amount"]
            currency = clean["currency"]
            if not isinstance(amount, str) or not _decimal_string(amount):
                _validation(field, "monetary amount must be a canonical decimal string")
            if not isinstance(currency, str) or not _iso_currency(currency):
                _validation(field, "monetary currency must be an ISO 4217 code")
        return clean
    if isinstance(value, list):
        return [_jsonable(item, field) for item in value]
    _validation(field, f"value type {type(value).__name__} is not canonical JSON")


def _strip_admission(value):
    if isinstance(value, dict):
        return {
            key: _strip_admission(item)
            for key, item in value.items()
            if key not in ADMISSION_KEYS
        }
    if isinstance(value, list):
        return [_strip_admission(item) for item in value]
    return value


def _reject_public_private(binding: dict) -> None:
    public = (
        binding.get("visibility") == "public"
        or binding.get("public") is True
        or binding.get("audience") == "public"
    )
    if not public:
        return

    def walk(node) -> None:
        if isinstance(node, dict):
            if {"status", "provenance", "visibility"} <= set(node):
                _validation("profile", "private material is not permitted in public requests")
            for key, item in node.items():
                if key in PRIVATE_KEYS:
                    _validation(key, "private material is not permitted in public requests")
                if key == "visibility" and item == "private":
                    _validation("visibility", "private material is not permitted in public requests")
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(binding)


def _valid_counter(name: str) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    if not name[0].isalpha():
        return False
    return all(ch.isalnum() or ch == "_" for ch in name)


def _require_int(field: str, value, *, positive: bool = False, non_negative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _validation(field, f"{field} must be an integer")
    if positive and value <= 0:
        _validation(field, f"{field} must be a positive integer")
    if non_negative and value < 0:
        _validation(field, f"{field} must be a non-negative integer")
    return value


def _connection(store):
    for name in _CONN_ATTRS:
        value = getattr(store, name, None)
        if value is not None and hasattr(value, "execute") and hasattr(value, "in_transaction"):
            return value
    if hasattr(store, "execute") and hasattr(store, "in_transaction"):
        return store
    raise RuntimeError("Store connection is unavailable")


def _coerce_conn(handle, fallback):
    if handle is None:
        return fallback
    if hasattr(handle, "in_transaction") and hasattr(handle, "execute"):
        return handle
    for name in _CONN_ATTRS:
        value = getattr(handle, name, None)
        if value is not None and hasattr(value, "in_transaction") and hasattr(value, "execute"):
            return value
    return fallback


def _in_tx(conn, store) -> bool:
    if getattr(conn, "in_transaction", False):
        return True
    depth = getattr(store, "_tx_depth", None)
    if isinstance(depth, int) and not isinstance(depth, bool) and depth > 0:
        return True
    return getattr(store, "in_transaction", None) is True


def _busy(conn) -> None:
    conn.execute(f"PRAGMA busy_timeout={_BUSY_MS}")


def _autocommit(conn) -> None:
    """isolation_level must stay unset. Changing it inside a transaction commits."""
    if not conn.in_transaction and conn.isolation_level is not None:
        conn.isolation_level = None


@contextmanager
def _immediate(store):
    """Write path: BEGIN IMMEDIATE before admission reads, or a savepoint inside the caller's tx."""
    if store is None or not hasattr(store, "tx"):
        _validation("store", "store is required")
    conn = _connection(store)
    _autocommit(conn)
    _busy(conn)
    if _in_tx(conn, store):
        conn.execute("SAVEPOINT snowball_ledger_op")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK TO SAVEPOINT snowball_ledger_op")
            conn.execute("RELEASE SAVEPOINT snowball_ledger_op")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT snowball_ledger_op")
        return
    tx_attr = store.tx
    manager = tx_attr() if callable(tx_attr) else tx_attr
    with manager as handle:
        active = _coerce_conn(handle, conn)
        _busy(active)
        yield active


@contextmanager
def _read(store):
    """Read path. Never BEGIN IMMEDIATE, and never roll back a transaction the caller opened."""
    if store is None:
        _validation("store", "store is required")
    conn = _connection(store)
    _busy(conn)
    if _in_tx(conn, store):
        yield conn
        return
    _autocommit(conn)
    started = False
    try:
        conn.execute("BEGIN DEFERRED")
        started = True
    except sqlite3.OperationalError:
        started = False
    try:
        yield conn
    finally:
        if started and conn.in_transaction:
            try:
                conn.rollback()
            except sqlite3.OperationalError:
                pass


def _row_dict(cur, row):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return {desc[0]: row[index] for index, desc in enumerate(cur.description)}


def _one(conn, sql: str, params=()):
    cur = conn.execute(sql, params)
    return _row_dict(cur, cur.fetchone())


def _all(conn, sql: str, params=()):
    cur = conn.execute(sql, params)
    return [_row_dict(cur, row) for row in cur.fetchall()]


def _goal_scope(goal_id: int) -> str:
    return f"goal:{goal_id}"


def _limit_map(value, field: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _validation(field, "limits must be an object")
    out = {}
    for key, item in value.items():
        if not _valid_counter(key):
            _validation(field, "counter name is invalid")
        out[key] = _require_int(field, item, non_negative=True)
    return out


def _optional_nonneg(limits: dict, key: str) -> int | None:
    if key not in limits or limits[key] is None:
        return None
    return _require_int(key, limits[key], non_negative=True)


def _string_list(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        _validation(field, f"{field} must be a list of strings")
    return list(value)


def _merge_ceilings(grant_map: dict[str, int], contract_map: dict[str, int]) -> dict[str, int]:
    merged = {}
    for key in set(grant_map) | set(contract_map):
        grant_ceiling = grant_map.get(key)
        contract_ceiling = contract_map.get(key)
        if grant_ceiling is None:
            merged[key] = contract_ceiling
        elif contract_ceiling is None:
            merged[key] = grant_ceiling
        else:
            merged[key] = min(grant_ceiling, contract_ceiling)
    return merged


def _parse_limits(limits_json: str) -> dict:
    try:
        raw = json.loads(limits_json)
    except (TypeError, json.JSONDecodeError):
        _validation("limits_json", "grant limits are not valid JSON")
    _jsonable(raw, "limits_json")
    if not isinstance(raw, dict):
        _validation("limits_json", "grant limits must be an object")
    usage_mode = raw.get("usage_mode")
    if usage_mode is not None and not isinstance(usage_mode, str):
        _validation("usage_mode", "usage_mode must be a string")
    if usage_mode == "hard":
        _validation("usage_mode", "hard usage mode is not admitted")
    share = raw.get("exploration_share")
    if isinstance(share, bool):
        _validation("exploration_share", "exploration_share must be a finite number from 0 to 1")
    if isinstance(share, float) and (not math.isfinite(share) or not 0 <= share <= 1):
        _validation("exploration_share", "exploration_share must be a finite number from 0 to 1")
    if isinstance(share, str) and not _decimal_string(share):
        _validation("exploration_share", "exploration_share must be a canonical decimal string")
    if share is not None and not isinstance(share, (str, int, float)):
        _validation("exploration_share", "exploration_share must be a finite number from 0 to 1")
    return {
        "day": _limit_map(raw.get("day"), "day"),
        "lifetime": _limit_map(raw.get("lifetime"), "lifetime"),
        "max_depth": _optional_nonneg(raw, "max_depth"),
        "max_queue": _optional_nonneg(raw, "max_queue"),
        "per_domain_inflight": _optional_nonneg(raw, "per_domain_inflight"),
        "max_hypotheses_per_wave": _optional_nonneg(raw, "max_hypotheses_per_wave"),
        "usage_mode": usage_mode,
        "exploration_share": share,
    }


def _parse_policy(body_json: str) -> dict:
    try:
        body = json.loads(body_json)
    except (TypeError, json.JSONDecodeError):
        _validation("body_json", "contract body is not valid JSON")
    _jsonable(body, "body_json")
    if not isinstance(body, dict):
        _validation("body_json", "contract body must be an object")
    policy = body.get("execution_policy")
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        _validation("execution_policy", "execution_policy must be an object")
    _jsonable(policy, "execution_policy")
    return {
        "day": _limit_map(policy.get("day_limits"), "day_limits"),
        "lifetime": _limit_map(policy.get("lifetime_limits"), "lifetime_limits"),
        "blocked_domains": _string_list(policy.get("blocked_domains"), "blocked_domains"),
        "paused_domains": _string_list(policy.get("paused_domains"), "paused_domains"),
    }


def _load_bundle(conn, grant_id: int) -> dict:
    grant = _one(conn, "SELECT * FROM execution_grants WHERE id=?", (grant_id,))
    if grant is None:
        _validation("grant_id", "grant not found")
    goal = _one(conn, "SELECT * FROM goals WHERE id=?", (grant["goal_id"],))
    if goal is None:
        _validation("goal_id", "goal not found")
    contract = _one(
        conn,
        "SELECT * FROM goal_contracts WHERE goal_id=? AND version=?",
        (grant["goal_id"], grant["contract_version"]),
    )
    if contract is None:
        _validation("contract_version", "contract not found")
    limits = _parse_limits(grant["limits_json"])
    policy = _parse_policy(contract["body_json"])
    return {
        "grant": grant,
        "goal": goal,
        "contract": contract,
        "day_limits": _merge_ceilings(limits["day"], policy["day"]),
        "life_limits": _merge_ceilings(limits["lifetime"], policy["lifetime"]),
        "max_depth": limits["max_depth"],
        "max_queue": limits["max_queue"],
        "per_domain_inflight": limits["per_domain_inflight"],
        "max_hypotheses_per_wave": limits["max_hypotheses_per_wave"],
        "blocked_domains": policy["blocked_domains"],
        "paused_domains": policy["paused_domains"],
    }


def _mode_matches(mode: str, state: str) -> bool:
    if mode == "research":
        return state == "running"
    if mode == "monitoring":
        return state == "monitoring"
    return False


def _usage(conn, scope_key: str, window_kind: str, window_key: str, counter: str) -> tuple[int, int, int]:
    row = _one(
        conn,
        """SELECT reserved, settled, lost FROM resource_ledger
           WHERE scope_key=? AND window_kind=? AND window_key=? AND counter=?""",
        (scope_key, window_kind, window_key, counter),
    )
    if row is None:
        return (0, 0, 0)
    return (int(row["reserved"]), int(row["settled"]), int(row["lost"]))


def _used(parts: tuple[int, int, int]) -> int:
    return parts[0] + parts[1] + parts[2]


def _shared_scope(counter: str, role: str | None) -> str | None:
    if counter == "model_tokens":
        return "shared:model"
    if counter in ROLE_FOR_COUNTER:
        derived = ROLE_FOR_COUNTER[counter]
        if role is not None and role != derived:
            _validation("role", "role does not match the requested counter")
        return f"shared:role/{role or derived}"
    return None


def _normalize_amounts(amounts) -> dict[str, int]:
    if not isinstance(amounts, dict):
        _validation("amounts", "amounts must be an object")
    if len(amounts) > 64:
        _validation("amounts", "too many counters")
    clean = {}
    for key, value in amounts.items():
        if not _valid_counter(key):
            _validation("amounts", "counter name is invalid")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _validation("amounts", f"{key} amount must be a non-negative integer")
        clean[key] = value
    return clean


def _validate_binding_shape(binding) -> dict:
    if not isinstance(binding, dict):
        _validation("binding", "binding must be an object")
    clean = _strip_admission(_jsonable(binding, "binding"))
    _reject_public_private(clean)
    role = clean.get("role")
    if role is not None:
        if (
            not isinstance(role, str)
            or not role
            or "/" in role
            or any(ch.isspace() for ch in role)
        ):
            _validation("role", "role is invalid")
    for key in ("goal_id", "contract_version", "strategy_version", "epoch"):
        if key not in clean:
            _validation(key, f"{key} is required")
    _require_int("goal_id", clean["goal_id"], positive=True)
    _require_int("contract_version", clean["contract_version"], positive=True)
    _require_int("strategy_version", clean["strategy_version"], non_negative=True)
    _require_int("epoch", clean["epoch"], non_negative=True)
    if "mode" in clean and clean["mode"] not in {"research", "monitoring"}:
        _validation("mode", "mode is invalid")
    if "goal_id" in clean:
        _require_int("goal_id", clean["goal_id"], positive=True)
    if "contract_version" in clean:
        _require_int("contract_version", clean["contract_version"], positive=True)
    shared = clean.get("shared_day_limits")
    if shared is not None:
        if not isinstance(shared, dict):
            _validation("shared_day_limits", "shared_day_limits must be an object")
        for key, value in shared.items():
            if not _valid_counter(key):
                _validation("shared_day_limits", "counter name is invalid")
            if value is None:
                _validation("shared_day_limits", f"missing shared ceiling for {key}")
            _require_int("shared_day_limits", value, non_negative=True)
    text = canonical_json(clean)
    if len(text) > 262144:
        _validation("binding", "binding is too large")
    return clean


def _node_spec(binding: dict) -> dict | None:
    fields = ("node_kind", "node_key", "phase", "depth")
    present = [key for key in fields if key in binding]
    if not present:
        return None
    if len(present) != len(fields):
        _validation("binding", "node binding requires node_kind, node_key, phase, and depth")
    node_kind = binding["node_kind"]
    phase = binding["phase"]
    node_key = binding["node_key"]
    if node_kind not in {"page", "query"}:
        _validation("node_kind", "node_kind must be page or query")
    if phase not in {"queued", "visited"}:
        _validation("phase", "phase must be queued or visited")
    if not isinstance(node_key, str) or not node_key or len(node_key) > 2048 or "\x00" in node_key:
        _validation("node_key", "node_key is invalid")
    depth = _require_int("depth", binding["depth"], non_negative=True)
    return {
        "node_kind": node_kind,
        "node_key": node_key,
        "phase": phase,
        "depth": depth,
    }


def _domain_spec(binding: dict, now: int) -> dict | None:
    fields = ("domain", "task_id", "lease_token", "lease_until")
    present = [key for key in fields if key in binding]
    if not present:
        return None
    if len(present) != len(fields):
        _validation(
            "binding",
            "domain binding requires domain, task_id, lease_token, and lease_until",
        )
    domain = binding["domain"]
    if not isinstance(domain, str) or not domain.strip() or any(ch.isspace() for ch in domain):
        _validation("domain", "domain is invalid")
    token = binding["lease_token"]
    if not isinstance(token, str) or not token or len(token) > 200 or any(ch.isspace() for ch in token):
        _validation("lease_token", "lease_token is invalid")
    task_id = _require_int("task_id", binding["task_id"], positive=True)
    lease_until = _require_int("lease_until", binding["lease_until"], positive=True)
    if lease_until <= now:
        _validation("lease_until", "lease_until must be in the future")
    return {
        "domain": domain,
        "task_id": task_id,
        "lease_token": token,
        "lease_until": lease_until,
    }


def _operation_key(value) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or "\x00" in value
        or any(ch.isspace() for ch in value)
    ):
        _validation("operation_key", "operation_key is invalid")
    return value


def _request_hash(amounts: dict, binding: dict) -> str:
    payload = {
        "amounts": {key: amounts[key] for key in sorted(amounts)},
        "binding": binding,
    }
    return _sha256(canonical_json(payload))


def _reservation_ids(conn, grant_id: int, operation_key: str) -> list[int]:
    rows = _all(
        conn,
        """SELECT id FROM resource_reservations
           WHERE grant_id=? AND operation_key=?
           ORDER BY id""",
        (grant_id, operation_key),
    )
    return [int(row["id"]) for row in rows]


def _existing_operation(conn, goal_id: int, operation_key: str):
    return _one(
        conn,
        "SELECT * FROM resource_operations WHERE goal_id=? AND operation_key=?",
        (goal_id, operation_key),
    )


def _replay(conn, existing: dict, digest: str) -> list[int]:
    if existing["request_hash"] != digest:
        _raise_conflict("reserve_batch", existing["operation_key"])
    return _reservation_ids(conn, int(existing["grant_id"]), existing["operation_key"])


def _check_grant_time(grant: dict, now: int) -> None:
    starts = _require_int("starts_at", grant["starts_at"], non_negative=True)
    expires = _require_int("expires_at", grant["expires_at"], positive=True)
    if not (starts <= now < expires):
        _validation("grant_id", "grant is outside its time window")


def _queue_count(conn, goal_id: int) -> int:
    row = _one(
        conn,
        """SELECT COUNT(*) AS n FROM goal_node_identities
           WHERE goal_id=? AND queued_at IS NOT NULL AND visited_at IS NULL""",
        (goal_id,),
    )
    return int(row["n"])


def _apply_node(conn, goal_id: int, node: dict | None, limits: dict, now: int) -> set[str]:
    """Return identity counters that must not be charged again. Writes identity row."""
    if node is None:
        return set()
    if limits["max_depth"] is None:
        _validation("max_depth", "max_depth is not configured")
    if node["depth"] > limits["max_depth"]:
        _validation("depth", "depth exceeds max_depth")
    existing = _one(
        conn,
        """SELECT queued_at, visited_at FROM goal_node_identities
           WHERE goal_id=? AND node_kind=? AND node_key=?""",
        (goal_id, node["node_kind"], node["node_key"]),
    )
    if existing is None:
        if node["phase"] == "queued":
            if limits["max_queue"] is None:
                _validation("max_queue", "max_queue is not configured")
            count = _queue_count(conn, goal_id)
            if count >= limits["max_queue"]:
                raise GrantExhausted("max_queue", count, limits["max_queue"], None)
        visited = now if node["phase"] == "visited" else None
        conn.execute(
            """INSERT INTO goal_node_identities
               (goal_id, node_kind, node_key, queued_at, visited_at)
               VALUES (?, ?, ?, ?, ?)""",
            (goal_id, node["node_kind"], node["node_key"], now, visited),
        )
        return set()
    if node["phase"] == "queued":
        conn.execute(
            """UPDATE goal_node_identities
               SET queued_at=COALESCE(queued_at, ?)
               WHERE goal_id=? AND node_kind=? AND node_key=?""",
            (now, goal_id, node["node_kind"], node["node_key"]),
        )
    else:
        conn.execute(
            """UPDATE goal_node_identities
               SET queued_at=COALESCE(queued_at, ?), visited_at=COALESCE(visited_at, ?)
               WHERE goal_id=? AND node_kind=? AND node_key=?""",
            (now, now, goal_id, node["node_kind"], node["node_key"]),
        )
    skip = set(REQUEUED_IDENTITIES)
    if existing["visited_at"] is not None:
        skip.add("visited_nodes")
    return skip


def _release_dead_slots(conn, goal_id: int) -> None:
    """Drop slots whose operation is already terminal so a cancel cannot leak inflight capacity."""
    conn.execute(
        """DELETE FROM domain_slots
           WHERE operation_id IN (
             SELECT id FROM resource_operations
             WHERE goal_id=? AND state IN ('cancelled', 'settled', 'lost')
           )""",
        (goal_id,),
    )


def _domain_count(conn, goal_id: int, domain: str) -> int:
    row = _one(
        conn,
        """SELECT COUNT(*) AS n FROM domain_slots s
           JOIN resource_operations o ON o.id=s.operation_id
           WHERE o.goal_id=? AND s.domain=?""",
        (goal_id, domain),
    )
    return int(row["n"])


def _prepare_domain(conn, domain: dict | None, bundle: dict, now: int) -> None:
    del now
    if domain is None:
        return
    if bundle["per_domain_inflight"] is None:
        _validation("per_domain_inflight", "per_domain_inflight is not configured")
    if domain["domain"] in set(bundle["blocked_domains"]):
        _validation("domain", "domain is blocked")
    if domain["domain"] in set(bundle["paused_domains"]):
        _validation("domain", "domain is paused")
    goal_id = int(bundle["goal"]["id"])
    _release_dead_slots(conn, goal_id)
    count = _domain_count(conn, goal_id, domain["domain"])
    ceiling = bundle["per_domain_inflight"]
    if count >= ceiling:
        raise GrantExhausted("per_domain_inflight", count, ceiling, None)
    task = _one(conn, "SELECT id FROM tasks WHERE id=?", (domain["task_id"],))
    if task is None:
        _validation("task_id", "task not found")


def _task_origin(task: dict | None) -> str | None:
    if task is None:
        return None
    try:
        payload = json.loads(task.get("payload") or "{}")
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("origin"), str):
        return payload["origin"]
    return None


def _lookup_task(conn, goal_id: int, operation_key: str, task_id):
    if task_id is not None:
        return _one(conn, "SELECT * FROM tasks WHERE id=?", (int(task_id),))
    return _one(
        conn,
        """SELECT * FROM tasks
           WHERE goal_id=? AND operation_key=?
           ORDER BY id LIMIT 1""",
        (goal_id, operation_key),
    )


def _enforce_hypotheses(conn, goal_id: int, operation_key: str, binding: dict, bundle: dict) -> None:
    cap = bundle.get("max_hypotheses_per_wave")
    if cap is None:
        return
    task = _lookup_task(conn, goal_id, operation_key, binding.get("task_id"))
    origin = binding.get("origin") or _task_origin(task)
    if origin != "hypothesis":
        return
    wave_id = binding.get("wave_id")
    if wave_id is None and task is not None and task.get("wave_id") is not None:
        wave_id = task["wave_id"]
    if wave_id is None:
        _validation("wave_id", "hypothesis admission requires wave_id")
    wave_id = _require_int("wave_id", wave_id, positive=True)
    requested = binding.get("hypothesis_count", 1)
    requested = _require_int("hypothesis_count", requested, positive=True)
    row = _one(
        conn,
        """SELECT COUNT(*) AS n
           FROM resource_operations o
           JOIN tasks t ON t.goal_id=o.goal_id AND t.operation_key=o.operation_key
           WHERE o.goal_id=? AND o.state!='cancelled' AND t.wave_id=?
             AND json_extract(t.payload, '$.origin')='hypothesis'""",
        (goal_id, wave_id),
    )
    already = int(row["n"])
    if already + requested > cap:
        raise GrantExhausted("max_hypotheses_per_wave", already, cap, None)


def _claim_task(conn, binding: dict, domain: dict | None, grant_id: int, operation_key: str, now: int):
    """Claim a pending task and return (task_id, occurrence_seq). Domain admits require a claim."""
    goal_id = int(binding["goal_id"])
    explicit = domain["task_id"] if domain is not None else binding.get("task_id")
    task = _lookup_task(conn, goal_id, operation_key, explicit)
    if task is None:
        if domain is not None:
            _validation("task_id", "task not found")
        return None
    task_id = int(task["id"])
    lease_token = domain["lease_token"] if domain is not None else None
    lease_until = domain["lease_until"] if domain is not None else None
    claimed = conn.execute(
        """UPDATE tasks
           SET status='claimed',
               claimed_at=?,
               heartbeat_at=?,
               occurrence_seq=occurrence_seq+1,
               goal_id=?,
               contract_version=?,
               strategy_version=?,
               epoch=?,
               grant_id=?,
               lease_token=COALESCE(?, lease_token),
               lease_until=COALESCE(?, lease_until),
               operation_key=?
           WHERE id=? AND status='pending'
             AND (operation_key IS NULL OR operation_key=?)
             AND (lease_token IS NULL OR lease_token='' OR lease_until IS NULL OR lease_until<=?)
           RETURNING occurrence_seq""",
        (
            now,
            now,
            goal_id,
            int(binding["contract_version"]),
            int(binding["strategy_version"]),
            int(binding["epoch"]),
            grant_id,
            lease_token,
            lease_until,
            operation_key,
            task_id,
            operation_key,
            now,
        ),
    ).fetchone()
    if claimed is None:
        if domain is not None:
            _validation("task_id", "task is not claimable")
        return None
    return task_id, int(claimed["occurrence_seq"])


def _insert_slot(conn, operation_id: int, domain: dict) -> None:
    conn.execute(
        """INSERT INTO domain_slots (domain, task_id, operation_id, lease_token, lease_until)
           VALUES (?, ?, ?, ?, ?)""",
        (
            domain["domain"],
            domain["task_id"],
            operation_id,
            domain["lease_token"],
            domain["lease_until"],
        ),
    )


def _add_reserved(conn, plan: dict) -> None:
    conn.execute(
        """INSERT INTO resource_ledger
             (scope_key, window_kind, window_key, counter, reserved, settled, lost)
           VALUES (?, ?, ?, ?, ?, 0, 0)
           ON CONFLICT(scope_key, window_kind, window_key, counter)
           DO UPDATE SET reserved = reserved + excluded.reserved""",
        (
            plan["scope_key"],
            plan["window_kind"],
            plan["window_key"],
            plan["counter"],
            plan["amount"],
        ),
    )


def _plans_for(amounts: dict, bundle: dict, binding: dict, day_key: str, skip: set[str]) -> list[dict]:
    role = binding.get("role")
    shared = binding.get("shared_day_limits") or {}
    goal_scope = _goal_scope(int(bundle["goal"]["id"]))
    plans = []
    for counter, amount in sorted(amounts.items()):
        if amount == 0:
            if (
                counter not in bundle["day_limits"]
                and counter not in bundle["life_limits"]
                and counter not in MODEL_COUNTERS
            ):
                _validation("counter", f"unknown counter {counter}")
            continue
        windows = []
        if counter in bundle["day_limits"]:
            windows.append(("day", day_key, bundle["day_limits"][counter], goal_scope))
        if counter in bundle["life_limits"]:
            windows.append(("lifetime", LIFETIME_WINDOW, bundle["life_limits"][counter], goal_scope))
        if not windows:
            _validation("counter", f"unknown counter {counter}")
        if counter in skip:
            continue
        if counter in MODEL_COUNTERS:
            if counter not in shared or shared[counter] is None:
                _validation("shared_day_limits", f"missing shared ceiling for {counter}")
            windows.append(("day", day_key, shared[counter], _shared_scope(counter, role)))
        for window_kind, window_key, ceiling, scope_key in windows:
            plans.append(
                {
                    "scope_key": scope_key,
                    "window_kind": window_kind,
                    "window_key": window_key,
                    "counter": counter,
                    "amount": amount,
                    "ceiling": ceiling,
                }
            )
    return plans


def _enforce_plans(conn, plans: list[dict]) -> None:
    for plan in plans:
        used = _used(_usage(conn, plan["scope_key"], plan["window_kind"], plan["window_key"], plan["counter"]))
        if used + plan["amount"] > plan["ceiling"]:
            raise GrantExhausted(
                plan["counter"],
                used,
                plan["ceiling"],
                _resets_at(plan["window_kind"], plan["window_key"]),
            )


def _identity_skip(conn, goal_id: int, node: dict | None) -> set[str]:
    if node is None:
        return set()
    existing = _one(
        conn,
        """SELECT queued_at, visited_at FROM goal_node_identities
           WHERE goal_id=? AND node_kind=? AND node_key=?""",
        (goal_id, node["node_kind"], node["node_key"]),
    )
    if existing is None:
        return set()
    skip = set(REQUEUED_IDENTITIES)
    if existing["visited_at"] is not None:
        skip.add("visited_nodes")
    return skip


def _exhausted_report(conn, bundle: dict, amounts: dict, day_key: str) -> None:
    goal_scope = _goal_scope(int(bundle["goal"]["id"]))
    worst = None
    for window_kind, window_key, ceilings in (
        ("day", day_key, bundle["day_limits"]),
        ("lifetime", LIFETIME_WINDOW, bundle["life_limits"]),
    ):
        for counter, ceiling in ceilings.items():
            used = _used(_usage(conn, goal_scope, window_kind, window_key, counter))
            if used > ceiling and (worst is None or used - ceiling > worst[0]):
                worst = (used - ceiling, counter, used, ceiling, _resets_at(window_kind, window_key))
    if worst is not None:
        raise GrantExhausted(worst[1], worst[2], worst[3], worst[4])
    positive = sorted(key for key, amount in amounts.items() if amount > 0)
    counter = positive[0] if positive else "fetches"
    if counter in bundle["life_limits"]:
        window_kind, window_key, ceiling = "lifetime", LIFETIME_WINDOW, bundle["life_limits"][counter]
    elif counter in bundle["day_limits"]:
        window_kind, window_key, ceiling = "day", day_key, bundle["day_limits"][counter]
    else:
        _validation("grant_id", "grant is exhausted")
    used = _used(_usage(conn, goal_scope, window_kind, window_key, counter))
    raise GrantExhausted(counter, used, ceiling, _resets_at(window_kind, window_key))


def _binding_json(operation: dict) -> dict:
    try:
        binding = json.loads(operation["binding_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return binding if isinstance(binding, dict) else {}


def _fence_occurrence(conn, operation: dict) -> None:
    """Fail the claimed task and bump occurrence once. A second sync must not bump again."""
    conn.execute("DELETE FROM domain_slots WHERE operation_id=?", (operation["id"],))
    binding = _binding_json(operation)
    if binding.get("occurrence_fenced"):
        return
    task_id = binding.get("task_id")
    claimed = binding.get("claimed_occurrence")
    if task_id is not None and claimed is not None:
        conn.execute(
            """UPDATE tasks
               SET status='failed', error='lost', worker_id=NULL,
                   lease_token=NULL, lease_until=NULL,
                   occurrence_seq=occurrence_seq+1
               WHERE id=? AND occurrence_seq=?""",
            (int(task_id), int(claimed)),
        )
    binding["occurrence_fenced"] = True
    conn.execute(
        "UPDATE resource_operations SET binding_json=? WHERE id=?",
        (canonical_json(binding), operation["id"]),
    )


def _sync_operation(conn, operation_id: int, now: int) -> None:
    del now
    operation = _one(conn, "SELECT * FROM resource_operations WHERE id=?", (operation_id,))
    if operation is None or operation["state"] in {"cancelled"}:
        return
    rows = _all(
        conn,
        """SELECT state FROM resource_reservations
           WHERE grant_id=? AND operation_key=?""",
        (operation["grant_id"], operation["operation_key"]),
    )
    if not rows:
        return
    states = {row["state"] for row in rows}
    if "reserved" in states:
        return
    new_state = "lost" if "lost" in states else "settled"
    if operation["state"] != new_state:
        conn.execute(
            "UPDATE resource_operations SET state=? WHERE id=?",
            (new_state, operation_id),
        )
        operation["state"] = new_state
    if new_state == "lost":
        _fence_occurrence(conn, operation)
    elif new_state == "settled":
        conn.execute("DELETE FROM domain_slots WHERE operation_id=?", (operation_id,))


def _operation_for_reservation(conn, reservation: dict):
    operation = _one(
        conn,
        "SELECT * FROM resource_operations WHERE grant_id=? AND operation_key=?",
        (reservation["grant_id"], reservation["operation_key"]),
    )
    if operation is None:
        _validation("operation_key", "operation not found")
    return operation


def _assert_lease_owner(conn, reservation: dict, operation: dict) -> None:
    """Settle of a domain reservation requires the same lease and claimed occurrence.

    Epoch may have moved after a successful dispatch. Late usage is still recorded.
    A stolen lease or a reaped occurrence does not move usage.
    """
    task_id = reservation["task_id"]
    if task_id is None:
        return
    binding = _binding_json(operation)
    if not binding.get("domain"):
        return
    claimed = binding.get("claimed_occurrence")
    task = _one(conn, "SELECT * FROM tasks WHERE id=?", (task_id,))
    slot = _one(
        conn,
        "SELECT * FROM domain_slots WHERE operation_id=? AND task_id=?",
        (operation["id"], task_id),
    )
    token = binding.get("lease_token")
    if (
        task is None
        or slot is None
        or claimed is None
        or int(task["occurrence_seq"]) != int(claimed)
        or task.get("lease_token") != slot["lease_token"]
        or slot["lease_token"] != token
        or task.get("operation_key") != operation["operation_key"]
    ):
        _validation("lease", "lease or occurrence no longer matches this reservation")


def _fence_holds(conn, operation: dict, now: int) -> bool:
    """True only when the admitted goal, contract, epoch, grant, occurrence, and lease still match."""
    binding = _binding_json(operation)
    try:
        ok = _goal().binding_valid(
            conn,
            int(operation["goal_id"]),
            int(binding["contract_version"]),
            int(binding["strategy_version"]),
            int(binding["epoch"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    if ok is not True:
        return False
    active = _one(
        conn,
        "SELECT * FROM execution_grants WHERE goal_id=? AND state='active'",
        (operation["goal_id"],),
    )
    if active is None or int(active["id"]) != int(operation["grant_id"]):
        return False
    if not (int(active["starts_at"]) <= int(now) < int(active["expires_at"])):
        return False
    task_id = binding.get("task_id")
    domain = binding.get("domain")
    if task_id is None and not domain:
        return True
    if task_id is None:
        return False
    task = _one(conn, "SELECT * FROM tasks WHERE id=?", (int(task_id),))
    claimed = binding.get("claimed_occurrence")
    if (
        task is None
        or task["status"] != "claimed"
        or claimed is None
        or int(task["occurrence_seq"]) != int(claimed)
        or task["operation_key"] != operation["operation_key"]
        or int(task["goal_id"] or 0) != int(operation["goal_id"])
        or int(task["contract_version"] or 0) != int(binding["contract_version"])
        or int(task["strategy_version"] or 0) != int(binding["strategy_version"])
        or int(task["epoch"] or 0) != int(binding["epoch"])
        or int(task["grant_id"] or 0) != int(operation["grant_id"])
    ):
        return False
    if not domain:
        return True
    slot = _one(conn, "SELECT * FROM domain_slots WHERE operation_id=?", (operation["id"],))
    if (
        slot is None
        or slot["domain"] != domain
        or slot["lease_token"] != binding.get("lease_token")
        or int(slot["task_id"]) != int(task_id)
        or int(slot["lease_until"]) <= int(now)
        or task.get("lease_token") != slot["lease_token"]
    ):
        return False
    return True


def _disposition(conn, operation: dict | None, now: int) -> str:
    if operation is None:
        return "absent"
    state = operation["state"]
    if state == "cancelled":
        return "released"
    if state in {"dispatched", "settled", "lost"}:
        return state
    if state == "reserved":
        return "admitted" if _fence_holds(conn, operation, now) else "stale"
    return state


def _move_ledger(conn, reservation: dict, reserved_delta: int, settled_delta: int, lost_delta: int) -> None:
    cur = conn.execute(
        """UPDATE resource_ledger
           SET reserved = reserved + ?, settled = settled + ?, lost = lost + ?
           WHERE scope_key=? AND window_kind=? AND window_key=? AND counter=?
             AND reserved + ? >= 0""",
        (
            reserved_delta,
            settled_delta,
            lost_delta,
            reservation["scope_key"],
            reservation["window_kind"],
            reservation["window_key"],
            reservation["counter"],
            reserved_delta,
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError("ledger balance does not cover reservation movement")


def _settle_row(conn, reservation_id: int, actual: int, now: int) -> dict:
    reservation = _one(conn, "SELECT * FROM resource_reservations WHERE id=?", (reservation_id,))
    if reservation is None:
        _validation("reservation_id", "reservation not found")
    if reservation["state"] == "settled":
        if reservation["actual"] != actual:
            _raise_conflict("settle", str(reservation_id))
        return reservation
    if reservation["state"] != "reserved":
        _raise_conflict("settle", str(reservation_id))
    operation = _operation_for_reservation(conn, reservation)
    if operation["state"] in {"cancelled", "lost"}:
        _raise_conflict("settle", str(reservation_id))
    _assert_lease_owner(conn, reservation, operation)
    amount = int(reservation["amount"])
    _move_ledger(conn, reservation, -amount, actual, 0)
    cur = conn.execute(
        """UPDATE resource_reservations
           SET state='settled', actual=?, settled_at=?
           WHERE id=? AND state='reserved'""",
        (actual, now, reservation_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError("reservation settle did not apply")
    if actual > amount:
        conn.execute(
            """UPDATE execution_grants
               SET state='exhausted', row_version=row_version+1
               WHERE id=? AND state='active'""",
            (reservation["grant_id"],),
        )
    _sync_operation(conn, int(operation["id"]), now)
    reservation["state"] = "settled"
    reservation["actual"] = actual
    return reservation


def _lose_row(conn, reservation_id: int, now: int) -> None:
    reservation = _one(conn, "SELECT * FROM resource_reservations WHERE id=?", (reservation_id,))
    if reservation is None:
        _validation("reservation_id", "reservation not found")
    if reservation["state"] == "lost":
        return
    if reservation["state"] != "reserved":
        _raise_conflict("mark_lost", str(reservation_id))
    operation = _operation_for_reservation(conn, reservation)
    if operation["state"] != "dispatched" or operation["dispatched_at"] is None:
        _validation("reservation_id", "only dispatched work can be marked lost")
    amount = int(reservation["amount"])
    _move_ledger(conn, reservation, -amount, 0, amount)
    cur = conn.execute(
        """UPDATE resource_reservations
           SET state='lost', actual=NULL
           WHERE id=? AND state='reserved'""",
        (reservation_id,),
    )
    if cur.rowcount != 1:
        raise RuntimeError("reservation loss did not apply")
    _sync_operation(conn, int(operation["id"]), now)


def _identity_alone(conn, operation: dict) -> bool:
    binding = _binding_json(operation)
    kind = binding.get("node_kind")
    key = binding.get("node_key")
    if not isinstance(kind, str) or not isinstance(key, str) or not kind or not key:
        return True
    row = _one(
        conn,
        """SELECT COUNT(*) AS n FROM resource_operations
           WHERE goal_id=? AND id!=? AND state!='cancelled'
             AND json_extract(binding_json, '$.node_kind')=?
             AND json_extract(binding_json, '$.node_key')=?""",
        (operation["goal_id"], operation["id"], kind, key),
    )
    alone = int(row["n"]) == 0
    if alone:
        conn.execute(
            "DELETE FROM goal_node_identities WHERE goal_id=? AND node_kind=? AND node_key=?",
            (operation["goal_id"], kind, key),
        )
    return alone


def _release_operation(conn, operation: dict) -> None:
    """Refund provably undispatched work. Keep a unique-node charge another live op still needs."""
    alone = _identity_alone(conn, operation)
    rows = _all(
        conn,
        """SELECT * FROM resource_reservations
           WHERE grant_id=? AND operation_key=? AND state='reserved'""",
        (operation["grant_id"], operation["operation_key"]),
    )
    for reservation in rows:
        amount = int(reservation["amount"])
        if not alone and reservation["counter"] in IDENTITY_COUNTERS:
            _move_ledger(conn, reservation, -amount, amount, 0)
        else:
            _move_ledger(conn, reservation, -amount, 0, 0)
        conn.execute("DELETE FROM resource_reservations WHERE id=?", (reservation["id"],))
    conn.execute("DELETE FROM domain_slots WHERE operation_id=?", (operation["id"],))
    binding = _binding_json(operation)
    task_id = binding.get("task_id")
    if task_id is not None:
        conn.execute(
            """UPDATE tasks
               SET status='failed', error='released undispatched',
                   worker_id=NULL, lease_token=NULL, lease_until=NULL
               WHERE id=? AND status='claimed'""",
            (int(task_id),),
        )
    conn.execute(
        "UPDATE resource_operations SET state='cancelled' WHERE id=? AND state='reserved'",
        (operation["id"],),
    )


def _drop_cancelled_residue(conn, goal_id: int, operation_id: int, operation_key: str) -> None:
    """Drop slots and reserved rows left on a cancelled admit. The refund already happened."""
    conn.execute("DELETE FROM domain_slots WHERE operation_id=?", (operation_id,))
    conn.execute(
        """DELETE FROM resource_reservations
           WHERE state='reserved' AND operation_key=?
             AND grant_id IN (SELECT id FROM execution_grants WHERE goal_id=?)""",
        (operation_key, goal_id),
    )


def _drop_orphan_identities(conn, goal_id: int) -> None:
    """A node held only by cancelled admits was refunded. The next admit charges it again."""
    conn.execute(
        """DELETE FROM goal_node_identities
           WHERE goal_id=?
             AND NOT EXISTS (
               SELECT 1 FROM resource_operations o
               WHERE o.goal_id=goal_node_identities.goal_id
                 AND o.state!='cancelled'
                 AND json_extract(o.binding_json, '$.node_kind')=goal_node_identities.node_kind
                 AND json_extract(o.binding_json, '$.node_key')=goal_node_identities.node_key
             )""",
        (goal_id,),
    )


def _snapshot_now() -> int:
    current = now_unix()
    if isinstance(current, bool) or not isinstance(current, int):
        raise RuntimeError("now_unix must return an int")
    return current


def _coerce_day(day) -> str:
    if isinstance(day, str):
        parts = day.split("-")
        if len(parts) != 3 or len(parts[0]) != 4 or len(parts[1]) != 2 or len(parts[2]) != 2:
            _validation("day", "day must be an IST YYYY-MM-DD")
        if not all(part.isdigit() for part in parts):
            _validation("day", "day must be an IST YYYY-MM-DD")
        try:
            datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            _validation("day", "day must be an IST YYYY-MM-DD")
        return day
    if isinstance(day, int) and not isinstance(day, bool):
        return ist_day(day)
    _validation("day", "day must be an IST date or unix timestamp")


def reserve_batch(store, grant_id: int, operation_key: str, amounts: dict[str, int], binding: dict) -> list[int]:
    """Atomically reserve every counter or leave the ledger unchanged."""
    grant_id = _require_int("grant_id", grant_id, positive=True)
    operation_key = _operation_key(operation_key)
    normalized = _normalize_amounts(amounts)
    stable = _validate_binding_shape(binding)
    node = _node_spec(stable)
    now = _snapshot_now()
    domain = _domain_spec(stable, now)
    digest = _request_hash(normalized, stable)
    binding_text = canonical_json(stable)
    with _immediate(store) as conn:
        bundle = _load_bundle(conn, grant_id)
        goal = bundle["goal"]
        grant = bundle["grant"]
        contract = bundle["contract"]
        if stable["goal_id"] != goal["id"]:
            _validation("goal_id", "goal_id does not match grant")
        existing = _existing_operation(conn, int(goal["id"]), operation_key)
        if existing is not None and existing["state"] != "cancelled":
            return _replay(conn, existing, digest)
        cancelled = existing
        if stable["contract_version"] != grant["contract_version"]:
            _validation("contract_version", "contract_version does not match grant")
        if goal["current_contract_version"] != grant["contract_version"]:
            _validation("contract_version", "contract_version is not current")
        if contract["status"] != "approved":
            _validation("contract_version", "contract is not approved")
        _require_binding(
            conn,
            int(goal["id"]),
            int(stable["contract_version"]),
            int(stable["strategy_version"]),
            int(stable["epoch"]),
        )
        if stable.get("mode") is not None and stable["mode"] != grant["mode"]:
            _validation("mode", "grant mode does not match request")
        if not _mode_matches(grant["mode"], goal["state"]):
            _validation("mode", "grant mode does not match goal")
        _check_grant_time(grant, now)
        day_key = ist_day(now)
        positive = {key: value for key, value in normalized.items() if value > 0}
        if grant["state"] == "exhausted":
            if not positive:
                _validation("grant_id", "grant is exhausted")
            _exhausted_report(conn, bundle, normalized, day_key)
        if grant["state"] != "active":
            _validation("grant_id", f"grant is {grant['state']}")
        _enforce_hypotheses(conn, int(goal["id"]), operation_key, stable, bundle)
        if node is not None:
            if bundle["max_depth"] is None:
                _validation("max_depth", "max_depth is not configured")
            if node["depth"] > bundle["max_depth"]:
                _validation("depth", "depth exceeds max_depth")
        if cancelled is not None:
            _drop_cancelled_residue(conn, int(goal["id"]), int(cancelled["id"]), operation_key)
        _drop_orphan_identities(conn, int(goal["id"]))
        skip_probe = _identity_skip(conn, int(goal["id"]), node)
        if node is not None and not skip_probe and node["phase"] == "queued":
            if bundle["max_queue"] is None:
                _validation("max_queue", "max_queue is not configured")
            count = _queue_count(conn, int(goal["id"]))
            if count >= bundle["max_queue"]:
                raise GrantExhausted("max_queue", count, bundle["max_queue"], None)
        _prepare_domain(conn, domain, bundle, now)
        plans = _plans_for(normalized, bundle, stable, day_key, skip_probe)
        if not plans and node is None and domain is None:
            _validation("amounts", "amounts are required")
        _enforce_plans(conn, plans)
        if cancelled is not None:
            cur = conn.execute(
                """UPDATE resource_operations
                   SET grant_id=?, request_hash=?, binding_json=?, state='reserved',
                       created_at=?, dispatched_at=NULL
                   WHERE id=? AND state='cancelled'""",
                (grant["id"], digest, binding_text, now, cancelled["id"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError("cancelled operation was not reopened")
            operation_id = int(cancelled["id"])
        else:
            try:
                cur = conn.execute(
                    """INSERT INTO resource_operations
                       (goal_id, grant_id, operation_key, request_hash, binding_json, state, created_at)
                       VALUES (?, ?, ?, ?, ?, 'reserved', ?)""",
                    (goal["id"], grant["id"], operation_key, digest, binding_text, now),
                )
            except sqlite3.IntegrityError:
                raced = _existing_operation(conn, int(goal["id"]), operation_key)
                if raced is None or raced["state"] == "cancelled":
                    raise
                return _replay(conn, raced, digest)
            operation_id = int(cur.lastrowid)
        _apply_node(conn, int(goal["id"]), node, bundle, now)
        claimed = _claim_task(conn, stable, domain, int(grant["id"]), operation_key, now)
        if claimed is not None:
            stored = dict(stable)
            stored["task_id"] = claimed[0]
            stored["claimed_occurrence"] = claimed[1]
            conn.execute(
                "UPDATE resource_operations SET binding_json=? WHERE id=?",
                (canonical_json(stored), operation_id),
            )
        reservation_task = claimed[0] if domain is not None and claimed is not None else None
        ids = []
        for plan in plans:
            _add_reserved(conn, plan)
            cur = conn.execute(
                """INSERT INTO resource_reservations (
                     grant_id, task_id, operation_key, scope_key, window_kind, window_key,
                     counter, amount, actual, state, created_at, settled_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'reserved', ?, NULL)""",
                (
                    grant["id"],
                    reservation_task,
                    operation_key,
                    plan["scope_key"],
                    plan["window_kind"],
                    plan["window_key"],
                    plan["counter"],
                    plan["amount"],
                    now,
                ),
            )
            ids.append(int(cur.lastrowid))
        if domain is not None:
            _insert_slot(conn, operation_id, domain)
        return ids


def settle(store, reservation_id: int, actual: int) -> None:
    reservation_id = _require_int("reservation_id", reservation_id, positive=True)
    actual = _require_int("actual", actual, non_negative=True)
    now = _snapshot_now()
    with _immediate(store) as conn:
        _settle_row(conn, reservation_id, actual, now)


def mark_lost(store, reservation_id: int) -> None:
    reservation_id = _require_int("reservation_id", reservation_id, positive=True)
    now = _snapshot_now()
    with _immediate(store) as conn:
        _lose_row(conn, reservation_id, now)


def headroom(store, grant_id: int, day) -> dict:
    grant_id = _require_int("grant_id", grant_id, positive=True)
    day_key = _coerce_day(day)
    with _read(store) as conn:
        bundle = _load_bundle(conn, grant_id)
        scope = _goal_scope(int(bundle["goal"]["id"]))

        def section(window_kind: str, window_key: str, ceilings: dict[str, int]) -> dict:
            counters = {}
            for counter in sorted(ceilings):
                reserved, settled, lost = _usage(conn, scope, window_kind, window_key, counter)
                used = reserved + settled + lost
                ceiling = ceilings[counter]
                counters[counter] = {
                    "ceiling": ceiling,
                    "reserved": reserved,
                    "settled": settled,
                    "lost": lost,
                    "used": used,
                    "remaining": ceiling - used,
                }
            return {"window_key": window_key, "counters": counters}

        return {
            "day": section("day", day_key, bundle["day_limits"]),
            "lifetime": section("lifetime", LIFETIME_WINDOW, bundle["life_limits"]),
        }


def mark_dispatched(store, operation_key: str, goal_id: int) -> bool:
    operation_key = _operation_key(operation_key)
    goal_id = _require_int("goal_id", goal_id, positive=True)
    now = _snapshot_now()
    with _immediate(store) as conn:
        operation = _existing_operation(conn, goal_id, operation_key)
        if operation is None or operation["state"] != "reserved" or operation["dispatched_at"] is not None:
            return False
        if not _fence_holds(conn, operation, now):
            return False
        cur = conn.execute(
            """UPDATE resource_operations
               SET state='dispatched', dispatched_at=?
               WHERE id=? AND state='reserved' AND dispatched_at IS NULL""",
            (now, operation["id"]),
        )
        return cur.rowcount == 1


def release_undispatched(store, operation_key: str, goal_id: int) -> None:
    operation_key = _operation_key(operation_key)
    goal_id = _require_int("goal_id", goal_id, positive=True)
    with _immediate(store) as conn:
        operation = _existing_operation(conn, goal_id, operation_key)
        if operation is None:
            _validation("operation_key", "operation not found")
        if operation["state"] == "cancelled":
            return
        if operation["state"] != "reserved" or operation["dispatched_at"] is not None:
            _validation("operation_key", "only an undispatched reservation can be released")
        _release_operation(conn, operation)


def settle_operation(store, operation_key: str, goal_id: int, actuals: dict) -> None:
    operation_key = _operation_key(operation_key)
    goal_id = _require_int("goal_id", goal_id, positive=True)
    if not isinstance(actuals, dict):
        _validation("actuals", "actuals must be an object")
    clean = {}
    for key, value in actuals.items():
        if not _valid_counter(key):
            _validation("actuals", "counter name is invalid")
        clean[key] = _require_int("actual", value, non_negative=True)
    now = _snapshot_now()
    with _immediate(store) as conn:
        operation = _existing_operation(conn, goal_id, operation_key)
        if operation is None:
            _validation("operation_key", "operation not found")
        if operation["state"] in {"cancelled", "lost"}:
            _raise_conflict("settle_operation", operation_key)
        rows = _all(
            conn,
            """SELECT * FROM resource_reservations
               WHERE grant_id=? AND operation_key=?
               ORDER BY id""",
            (operation["grant_id"], operation_key),
        )
        counters = {row["counter"] for row in rows}
        if set(clean) != counters:
            _validation("actuals", "actuals must cover exactly the reserved counters")
        for reservation in rows:
            _settle_row(conn, int(reservation["id"]), clean[reservation["counter"]], now)


def admission_state(store, goal_id: int, operation_key: str) -> dict:
    goal_id = _require_int("goal_id", goal_id, positive=True)
    operation_key = _operation_key(operation_key)
    now = _snapshot_now()
    with _read(store) as conn:
        operation = _existing_operation(conn, goal_id, operation_key)
        if operation is None:
            return {
                "goal_id": goal_id,
                "operation_key": operation_key,
                "disposition": "absent",
                "state": None,
                "grant_id": None,
                "operation_id": None,
                "dispatched_at": None,
                "reservations": [],
            }
        rows = _all(
            conn,
            """SELECT id, counter, scope_key, window_kind, window_key, amount, actual, state, task_id
               FROM resource_reservations
               WHERE grant_id=? AND operation_key=?
               ORDER BY id""",
            (operation["grant_id"], operation_key),
        )
        return {
            "goal_id": goal_id,
            "operation_key": operation_key,
            "disposition": _disposition(conn, operation, now),
            "state": operation["state"],
            "grant_id": operation["grant_id"],
            "operation_id": operation["id"],
            "dispatched_at": operation["dispatched_at"],
            "reservations": rows,
        }
