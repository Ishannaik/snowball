"""Transactional goal lifecycle. Private draft fields never mutate approved versions."""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import math
import re
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ProfileSnapshot = dict[str, dict[str, Any]]


def _sibling(name: str) -> Any:
    key = f'snowball_{name}'
    path = ROOT / 'tools' / f'{name}.py'
    existing = sys.modules.get(key)
    if existing is not None and Path(existing.__file__).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


class ValidationError(ValueError):
    def __init__(self, field: str, message: str):
        self.field, self.message = field, message
        super().__init__(message)


class VersionConflict(ValueError):
    def __init__(self, current_row_version: int):
        self.current_row_version = current_row_version
        super().__init__('The goal changed; reload the reviewed version.')


class IllegalTransition(ValueError):
    def __init__(self, from_state: str, to_state: str):
        self.from_state, self.to_state = from_state, to_state
        super().__init__(f'Cannot transition from {from_state} to {to_state}.')


class IdempotencyConflict(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Goal:
    id: int
    slug: str
    title: str
    state: str
    current_contract_version: int | None
    current_strategy_version: int
    epoch: int
    row_version: int
    revision_from_state: str | None
    stop_reason: str | None


@dataclasses.dataclass(frozen=True)
class ProfileField:
    key: str
    value: Any
    status: str
    provenance: str
    visibility: str
    row_version: int = 0


@dataclasses.dataclass(frozen=True)
class ContractVersion:
    goal_id: int
    version: int
    status: str
    body: dict[str, Any]
    body_hash: str
    profile_snapshot: ProfileSnapshot
    profile_snapshot_hash: str
    frame: dict[str, Any]
    frame_hash: str


@dataclasses.dataclass(frozen=True)
class Grant:
    id: int
    goal_id: int
    contract_version: int
    mode: str
    state: str
    limits: dict[str, Any]
    starts_at: int
    expires_at: int
    row_version: int


@dataclasses.dataclass(frozen=True)
class RunnableGoal:
    goal: Goal
    contract: ContractVersion
    grant: Grant
    frame: Any


def _goal(row: sqlite3.Row) -> Goal:
    return Goal(**{field.name: row[field.name] for field in dataclasses.fields(Goal)})


def get_goal(store: Any, goal_id: int | None = None) -> Goal | None:
    row = store.one('SELECT * FROM goals WHERE id=?', (goal_id,)) if goal_id is not None else store.one(
        "SELECT * FROM goals ORDER BY CASE WHEN state IN ('running','monitoring') THEN 0 "
        "WHEN state='pausing' THEN 1 WHEN state='stopped' THEN 3 ELSE 2 END, id DESC LIMIT 1")
    return _goal(row) if row else None


def require_goal(db: sqlite3.Connection, goal_id: int, expected: int | None = None) -> Goal:
    row = db.execute('SELECT * FROM goals WHERE id=?', (goal_id,)).fetchone()
    if not row:
        raise LookupError('Goal not found.')
    goal = _goal(row)
    if expected is not None and (type(expected) is not int or goal.row_version != expected):
        raise VersionConflict(goal.row_version)
    return goal


def _advance(db: sqlite3.Connection, goal: Goal, **changes: Any) -> Goal:
    changes.update(row_version=goal.row_version + 1, updated_at=int(time.time()))
    columns = ','.join(f'{key}=?' for key in changes)
    changed = db.execute(f'UPDATE goals SET {columns} WHERE id=? AND row_version=?',
                         (*changes.values(), goal.id, goal.row_version)).rowcount
    if changed != 1:
        raise VersionConflict(require_goal(db, goal.id).row_version)
    return require_goal(db, goal.id)


def _event(db: sqlite3.Connection, goal_id: int, actor: str, operation: str,
           before: str | None, after: str | None, payload: dict | None = None,
           key: str | None = None, request_hash: str | None = None,
           result: Any = None, status: int = 200) -> None:
    db.execute('INSERT INTO goal_events(goal_id,at,actor,kind,operation,from_state,to_state,payload_json,'
               'idempotency_key,request_hash,result_status,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (goal_id, int(time.time()), actor, operation, operation, before, after,
                canonical(payload or {}), key, request_hash, status,
                canonical(dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result)))


def _replay(db: sqlite3.Connection, actor: str, operation: str, key: str, request: Any) -> Any:
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ValidationError('idempotency_key', 'A nonempty idempotency key is required.')
    row = db.execute('SELECT request_hash,result_json FROM goal_events WHERE actor=? AND operation=? '
                     'AND idempotency_key=?', (actor, operation, key)).fetchone()
    if not row:
        return None
    if row['request_hash'] != digest(request):
        raise IdempotencyConflict('The idempotency key was used for a different request.')
    return json.loads(row['result_json'])


def create_goal(store: Any, slug: str, title: str, actor: str) -> Goal:
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', slug):
        raise ValidationError('slug', 'Use a short lowercase hyphenated identifier.')
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise ValidationError('title', 'A title of 1–300 characters is required.')
    with store.tx() as db:
        timestamp = int(time.time())
        try:
            goal_id = db.execute('INSERT INTO goals(slug,title,state,created_at,updated_at) '
                                 "VALUES(?,?,'draft',?,?)", (slug, title.strip(), timestamp, timestamp)).lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValidationError('slug', 'That identifier is already in use.') from exc
        goal = require_goal(db, goal_id)
        _event(db, goal_id, actor, 'create', None, goal.state)
        return goal


def fields(store: Any, goal_id: int) -> dict[str, ProfileField]:
    return {row['key']: ProfileField(row['key'], json.loads(row['value_json']), row['status'],
            row['provenance'], row['visibility'], row['row_version'])
            for row in store.all('SELECT * FROM profile_fields WHERE goal_id=?', (goal_id,))}


def _snapshot(db: sqlite3.Connection, goal_id: int) -> ProfileSnapshot:
    return {row['key']: {'value': json.loads(row['value_json']), 'status': row['status'],
                         'provenance': row['provenance'], 'visibility': row['visibility']}
            for row in db.execute('SELECT * FROM profile_fields WHERE goal_id=?', (goal_id,))}


def record_answer(store: Any, goal_id: int, field_key: str, value: Any, confirm: bool,
                  visibility: str, expected_row_version: int, actor: str) -> ProfileField:
    interview = _sibling('interview')
    question = next((question for question in interview.QUESTIONS if question.field_key == field_key), None)
    if question is None:
        raise ValidationError('field_key', 'Unknown interview field.')
    parsed = interview.validate_answer(question, value)
    if type(confirm) is not bool or visibility not in ('private', 'public_ok'):
        raise ValidationError('confirm', 'Confirmation and visibility must be explicit.')
    with store.tx() as db:
        goal = require_goal(db, goal_id, expected_row_version)
        if goal.state not in ('draft', 'needs_information', 'contract_proposed', 'revising'):
            raise IllegalTransition(goal.state, 'answer')
        previous = db.execute('SELECT row_version FROM profile_fields WHERE goal_id=? AND key=?',
                              (goal_id, field_key)).fetchone()
        version = previous['row_version'] + 1 if previous else 0
        status = 'known' if confirm else 'draft'
        provenance = 'user_confirmed' if confirm else 'user_stated'
        db.execute('INSERT INTO profile_fields(goal_id,key,value_json,status,provenance,visibility,row_version,updated_at) '
                   'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(goal_id,key) DO UPDATE SET value_json=excluded.value_json,'
                   'status=excluded.status,provenance=excluded.provenance,visibility=excluded.visibility,'
                   'row_version=excluded.row_version,updated_at=excluded.updated_at',
                   (goal_id, field_key, canonical(parsed), status, provenance, visibility, version, int(time.time())))
        db.execute("UPDATE goal_contracts SET status='discarded' WHERE goal_id=? AND status='proposed'", (goal_id,))
        new_state = 'revising' if goal.state == 'revising' else 'draft'
        _advance(db, goal, state=new_state)
        session = db.execute("SELECT id FROM interview_sessions WHERE goal_id=? AND state='open'", (goal_id,)).fetchone()
        session_id = session['id'] if session else db.execute(
            "INSERT INTO interview_sessions(goal_id,state,created_at) VALUES(?,'open',?)",
            (goal_id, int(time.time()))).lastrowid
        sequence = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM interview_turns WHERE session_id=?',
                              (session_id,)).fetchone()[0]
        db.execute("INSERT INTO interview_turns(session_id,seq,kind,field_key,text,created_at) VALUES(?,?,'answer',?,?,?)",
                   (session_id, sequence, field_key, canonical(parsed), int(time.time())))
        _event(db, goal_id, actor, 'answer', goal.state, new_state,
               {'field_key': field_key, 'confirmed': confirm, 'visibility': visibility})
        return ProfileField(field_key, parsed, status, provenance, visibility, version)


def apply_answers(store: Any, goal_id: int, entries: list[tuple[str, Any, bool, str]],
                  expected_row_version: int, actor: str) -> list[ProfileField]:
    """Apply an interview batch in one transaction. A later rejection rolls back earlier answers."""
    if not isinstance(entries, list) or not entries:
        raise ValidationError('answers', 'At least one interview answer is required.')
    with store.tx() as db:
        version = expected_row_version
        saved: list[ProfileField] = []
        for field_key, value, confirm, visibility in entries:
            saved.append(record_answer(store, goal_id, field_key, value, confirm, visibility, version, actor))
            version = require_goal(db, goal_id).row_version
        return saved


def load_contract(store: Any, goal_id: int, version: int) -> ContractVersion:
    row = store.one('SELECT * FROM goal_contracts WHERE goal_id=? AND version=?', (goal_id, version))
    if not row:
        raise LookupError('Contract version not found.')
    return ContractVersion(goal_id, version, row['status'], json.loads(row['body_json']), row['body_hash'],
                           json.loads(row['profile_snapshot_json']), row['profile_snapshot_hash'],
                           json.loads(row['frame_json'] or '{}'), row['frame_hash'])


def _frame_payload(body: dict, snapshot: ProfileSnapshot) -> dict[str, Any]:
    return dataclasses.asdict(_sibling('interview').compile_public_frame(body, snapshot))


def propose_contract(store: Any, goal_id: int, body: dict, base_version: int | None, actor: str,
                     *, expected_row_version: int) -> ContractVersion:
    interview = _sibling('interview')
    with store.tx() as db:
        goal = require_goal(db, goal_id, expected_row_version)
        if goal.state not in ('draft', 'needs_information', 'contract_proposed', 'revising'):
            raise IllegalTransition(goal.state, 'propose')
        if base_version != goal.current_contract_version:
            raise VersionConflict(goal.row_version)
        snapshot = _snapshot(db, goal_id)
        missing = interview.next_question(snapshot)
        if missing is not None:
            raise ValidationError(missing.field_key, 'Confirm required interview fields before proposing.')
        if not isinstance(body, dict):
            raise ValidationError('body', 'Contract must be an object.')
        required_body = interview.build_contract(snapshot)
        if body != required_body:
            raise ValidationError('body', 'Contract criteria must match the confirmed interview fields.')
        frame = _frame_payload(body, snapshot)
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM goal_contracts WHERE goal_id=?',
                             (goal_id,)).fetchone()[0]
        body_hash = digest({'body': body, 'profile_snapshot': snapshot})
        frame_hash = digest(frame)
        db.execute("UPDATE goal_contracts SET status='discarded' WHERE goal_id=? AND status='proposed'", (goal_id,))
        db.execute('INSERT INTO goal_contracts(goal_id,version,status,body_json,body_hash,profile_snapshot_json,'
                   'profile_snapshot_hash,frame_json,frame_hash,created_at,supersedes_version) '
                   "VALUES(?,?,'proposed',?,?,?,?,?,?,?,?)",
                   (goal_id, version, canonical(body), body_hash, canonical(snapshot), digest(snapshot),
                    canonical(frame), frame_hash, int(time.time()), base_version))
        next_state = 'revising' if goal.state == 'revising' else 'contract_proposed'
        _advance(db, goal, state=next_state)
        _event(db, goal_id, actor, 'propose', goal.state, next_state, {'version': version, 'body_hash': body_hash})
        return load_contract(store, goal_id, version)


def _release_undispatched(db: sqlite3.Connection, goal_id: int, reason: str) -> None:
    """Cancel reserved work that has not been dispatched, and free its task and domain leases.

    Dispatched operations keep their reservations, domain slots, and task leases. Epoch fencing
    rejects their late evaluation commits; usage stays reserved until the attempt settles or is lost.
    Observations and current-contract evaluations are not modified here.
    """
    operations = db.execute(
        "SELECT id, operation_key FROM resource_operations "
        "WHERE goal_id=? AND state='reserved' AND dispatched_at IS NULL",
        (goal_id,),
    ).fetchall()
    operation_ids = [int(row['id']) for row in operations]
    operation_keys = [row['operation_key'] for row in operations]
    if operation_keys:
        marks = ','.join('?' for _ in operation_keys)
        reservations = db.execute(
            f"SELECT * FROM resource_reservations WHERE state='reserved' AND operation_key IN ({marks}) "
            'AND grant_id IN (SELECT id FROM execution_grants WHERE goal_id=?)',
            (*operation_keys, goal_id),
        ).fetchall()
        for reservation in reservations:
            updated = db.execute(
                'UPDATE resource_ledger SET reserved=reserved-? WHERE scope_key=? AND window_kind=? '
                'AND window_key=? AND counter=? AND reserved>=?',
                (reservation['amount'], reservation['scope_key'], reservation['window_kind'],
                 reservation['window_key'], reservation['counter'], reservation['amount']),
            ).rowcount
            if updated != 1:
                raise ValidationError('ledger', 'Reserved usage is inconsistent and was not released.')
            db.execute("DELETE FROM resource_reservations WHERE id=? AND state='reserved'", (reservation['id'],))
        id_marks = ','.join('?' for _ in operation_ids)
        db.execute(f'DELETE FROM domain_slots WHERE operation_id IN ({id_marks})', operation_ids)
        db.execute(
            f"UPDATE tasks SET status='failed', error=?, worker_id=NULL, lease_token=NULL, lease_until=NULL, "
            f"operation_key=NULL WHERE goal_id=? AND operation_key IN ({marks}) "
            "AND status IN ('pending','claimed','paused')",
            (reason, goal_id, *operation_keys),
        )
        db.execute(
            f"UPDATE resource_operations SET state='cancelled' WHERE id IN ({id_marks}) "
            "AND state='reserved' AND dispatched_at IS NULL",
            operation_ids,
        )
    db.execute(
        "UPDATE tasks SET status='failed', error=?, worker_id=NULL, lease_token=NULL, lease_until=NULL, "
        "operation_key=NULL WHERE goal_id=? AND status IN ('pending','paused','claimed') AND ("
        'operation_key IS NULL OR operation_key NOT IN ('
        "SELECT operation_key FROM resource_operations WHERE goal_id=? AND state='dispatched'))",
        (reason, goal_id, goal_id),
    )
    db.execute(
        'DELETE FROM domain_slots WHERE task_id IN (SELECT id FROM tasks WHERE goal_id=? AND lease_token IS NULL) '
        'AND (operation_id IS NULL OR operation_id NOT IN ('
        "SELECT id FROM resource_operations WHERE goal_id=? AND state='dispatched'))",
        (goal_id, goal_id),
    )


def _reviewed_contract(contract: ContractVersion, body_hash: str) -> dict[str, Any]:
    recomputed = digest({'body': contract.body, 'profile_snapshot': contract.profile_snapshot})
    if contract.profile_snapshot_hash != digest(contract.profile_snapshot) or contract.body_hash != recomputed or recomputed != body_hash:
        raise ValidationError('body_hash', 'The reviewed contract hash does not match the stored body and snapshot.')
    frame = _frame_payload(contract.body, contract.profile_snapshot)
    frame_hash = digest(frame)
    if contract.frame != frame or contract.frame_hash != frame_hash or digest(contract.frame) != contract.frame_hash:
        raise ValidationError('frame_hash', 'The stored public frame does not match the frozen contract.')
    return frame


def approve_contract(store: Any, goal_id: int, version: int, body_hash: str,
                     expected_row_version: int, actor: str, idempotency_key: str) -> Goal:
    request = {'goal_id': goal_id, 'version': version, 'body_hash': body_hash, 'expected_row_version': expected_row_version}
    with store.tx() as db:
        replay = _replay(db, actor, 'approve', idempotency_key, request)
        if replay is not None:
            return Goal(**replay)
        goal = require_goal(db, goal_id, expected_row_version)
        if goal.state not in ('contract_proposed', 'revising'):
            raise IllegalTransition(goal.state, 'approved')
        contract = load_contract(store, goal_id, version)
        if contract.status != 'proposed':
            raise ValidationError('body_hash', 'The reviewed contract hash does not match.')
        _reviewed_contract(contract, body_hash)
        db.execute("UPDATE goal_contracts SET status='superseded' WHERE goal_id=? AND status='approved'", (goal_id,))
        db.execute("UPDATE goal_contracts SET status='approved',approved_at=?,approved_via=? WHERE goal_id=? AND version=?",
                   (int(time.time()), actor, goal_id, version))
        db.execute("UPDATE execution_grants SET state='revoked',revoked_at=?,revoke_reason='contract_changed' "
                   "WHERE goal_id=? AND state='active'", (int(time.time()), goal_id))
        _release_undispatched(db, goal_id, 'contract_changed')
        db.execute('UPDATE constraint_evaluations SET stale_for_goal=1 WHERE goal_id=? AND contract_version!=?',
                   (goal_id, version))
        result = _advance(db, goal, state='approved', current_contract_version=version, epoch=goal.epoch + 1,
                          revision_from_state=None)
        _event(db, goal_id, actor, 'approve', goal.state, result.state, {'version': version},
               idempotency_key, digest(request), result)
        return result


def _grant(row: sqlite3.Row) -> Grant:
    return Grant(row['id'], row['goal_id'], row['contract_version'], row['mode'], row['state'],
                 json.loads(row['limits_json']), row['starts_at'], row['expires_at'], row['row_version'])


def active_grant(store: Any, goal_id: int) -> Grant | None:
    row = store.one("SELECT * FROM execution_grants WHERE goal_id=? AND state='active'", (goal_id,))
    return _grant(row) if row else None


def _canonical_decimal(text: str) -> bool:
    """Match the ledger's decimal-string rule so a stored grant can be reserved."""
    body = text[1:] if text.startswith('-') else text
    if not body:
        return False
    whole, dot, frac = body.partition('.')
    if not whole.isdigit():
        return False
    if len(whole) > 1 and whole.startswith('0'):
        return False
    if dot and (not frac or not frac.isdigit()):
        return False
    return True


def _share_amount(value: Any, *, allow_float: bool) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not allow_float or not math.isfinite(value):
            return None
        return Decimal(str(value))
    if isinstance(value, str) and _canonical_decimal(value):
        try:
            parsed = Decimal(value)
        except Exception:
            return None
        if not parsed.is_finite():
            return None
        return parsed
    return None


def validate_limits(limits: dict, policy: dict) -> None:
    if not isinstance(limits, dict) or limits.get('usage_mode') not in ('estimated', 'hard'):
        raise ValidationError('limits', 'Explicit finite limits and usage mode are required.')
    if limits['usage_mode'] == 'hard':
        raise ValidationError('usage_mode', 'Hard provider-token mode requires attested bounded transport; unavailable.')
    for window in ('day', 'lifetime'):
        values = limits.get(window)
        if not isinstance(values, dict) or not values:
            raise ValidationError('limits', 'Daily and lifetime counter ceilings are required.')
        for counter, ceiling in values.items():
            if not isinstance(counter, str) or type(ceiling) is not int or ceiling < 0:
                raise ValidationError('limits', 'Counter ceilings must be finite nonnegative integers.')
        for counter in ('fetches', 'queries', 'model_tokens'):
            if counter not in values:
                raise ValidationError('limits', 'Fetch, query and model ceilings are required in both windows.')
    absolute = policy.get('lifetime_limits', {})
    for counter, ceiling in limits['lifetime'].items():
        if counter not in absolute or ceiling > absolute[counter]:
            raise ValidationError('limits', 'A grant cannot exceed the approved absolute lifetime policy.')
    for counter, ceiling in limits['day'].items():
        if counter in limits['lifetime'] and ceiling > limits['lifetime'][counter]:
            raise ValidationError('limits', 'A daily allowance cannot exceed its lifetime ceiling.')
    for counter in ('queued_nodes', 'visited_nodes'):
        if counter not in limits['lifetime']:
            raise ValidationError('limits', 'Lifetime unique-node ceilings are required.')
    for field in ('max_depth', 'max_queue', 'per_domain_inflight', 'max_hypotheses_per_wave'):
        value = limits.get(field)
        if type(value) is not int or value < 0 or value > policy.get(field, -1):
            raise ValidationError(field, 'Structural limits must fit the approved execution policy.')
    share = _share_amount(limits.get('exploration_share'), allow_float=False)
    ceiling = _share_amount(policy.get('exploration_share'), allow_float=True)
    if share is None or ceiling is None or share < 0 or share > ceiling:
        raise ValidationError(
            'exploration_share',
            'Exploration share must be an integer or canonical decimal string within the approved policy.',
        )


def _refresh_policy(body: dict) -> dict | None:
    """Return the approved refresh description, or None when monitoring was not permitted.

    The interview contract stores this at monitoring.refresh_policy. An enabled flag, a null
    policy, or an empty work list is not permission to monitor.
    """
    monitoring = body.get('monitoring') if isinstance(body, dict) else None
    if not isinstance(monitoring, dict) or set(monitoring) != {'refresh_policy'}:
        return None
    policy = monitoring.get('refresh_policy')
    if not isinstance(policy, dict) or set(policy) != {'permitted_work', 'max_refreshes', 'interval_days'}:
        return None
    work = policy.get('permitted_work')
    allowed = set(_sibling('interview').WORK_TOKENS)
    if (not isinstance(work, list) or not work or len(set(work)) != len(work)
            or any(type(item) is not str or item not in allowed for item in work)):
        return None
    refreshes, interval = policy.get('max_refreshes'), policy.get('interval_days')
    if type(refreshes) is not int or not 1 <= refreshes <= 100_000:
        return None
    if type(interval) is not int or not 1 <= interval <= 3660:
        return None
    return policy


def grant_execution(store: Any, goal_id: int, contract_version: int, mode: str, limits: dict,
                    expires_at: int, expected_row_version: int, actor: str, idempotency_key: str) -> Grant:
    request = {'goal_id': goal_id, 'contract_version': contract_version, 'mode': mode, 'limits': limits,
               'expires_at': expires_at, 'expected_row_version': expected_row_version}
    with store.tx() as db:
        replay = _replay(db, actor, 'grant', idempotency_key, request)
        if replay is not None:
            return Grant(**replay)
        goal = require_goal(db, goal_id, expected_row_version)
        if goal.state not in ('approved', 'paused', 'research_ready', 'running', 'monitoring'):
            raise IllegalTransition(goal.state, 'grant')
        if contract_version != goal.current_contract_version:
            raise VersionConflict(goal.row_version)
        contract = load_contract(store, goal_id, contract_version)
        if contract.status != 'approved' or mode not in ('research', 'monitoring'):
            raise ValidationError('mode', 'A current approved contract and explicit mode are required.')
        if mode == 'monitoring' and _refresh_policy(contract.body) is None:
            raise ValidationError('mode', 'Monitoring requires an explicit approved refresh policy.')
        validate_limits(limits, contract.body['execution_policy'])
        timestamp = int(time.time())
        if type(expires_at) is not int or not timestamp < expires_at <= timestamp + 366 * 86400:
            raise ValidationError('expires_at', 'Expiry must be a future finite interval of at most 366 days.')
        replacing = db.execute("SELECT 1 FROM execution_grants WHERE goal_id=? AND state='active'",
                               (goal_id,)).fetchone() is not None
        if replacing:
            _release_undispatched(db, goal_id, 'grant_replaced')
        db.execute("UPDATE execution_grants SET state='revoked',revoked_at=?,revoke_reason='replaced' "
                   "WHERE goal_id=? AND state='active'", (timestamp, goal_id))
        grant_id = db.execute('INSERT INTO execution_grants(goal_id,contract_version,mode,state,limits_json,'
                             "starts_at,expires_at,created_at) VALUES(?,?,?,'active',?,?,?,?)",
                             (goal_id, contract_version, mode, canonical(limits), timestamp, expires_at, timestamp)).lastrowid
        _advance(db, goal, epoch=goal.epoch + (1 if replacing else 0))
        result = _grant(db.execute('SELECT * FROM execution_grants WHERE id=?', (grant_id,)).fetchone())
        _event(db, goal_id, actor, 'grant', goal.state, goal.state, {'grant_id': grant_id, 'mode': mode},
               idempotency_key, digest(request), result, 201)
        return result


def _inflight(db: sqlite3.Connection, goal_id: int) -> int:
    return db.execute("SELECT COUNT(*) FROM resource_operations WHERE goal_id=? AND state='dispatched'",
                      (goal_id,)).fetchone()[0]


def inflight_count(store: Any, goal_id: int) -> int:
    row = store.one("SELECT COUNT(*) FROM resource_operations WHERE goal_id=? AND state='dispatched'", (goal_id,))
    return int(row[0] if row else 0)


def finish_pause(store: Any, goal_id: int) -> Goal:
    with store.tx() as db:
        goal = require_goal(db, goal_id)
        if goal.state == 'pausing' and _inflight(db, goal_id) == 0:
            result = _advance(db, goal, state='paused')
            _event(db, goal_id, 'worker', 'drained', 'pausing', 'paused', {'inflight': 0})
            return result
        return goal


def transition(store: Any, goal_id: int, to_state: str, reason: str, expected_row_version: int,
               actor: str, idempotency_key: str) -> Goal:
    aliases = {'start': 'running', 'pause': 'pausing', 'stop': 'stopped', 'revise': 'revising'}
    if not isinstance(reason, str) or len(reason) > 2000:
        raise ValidationError('reason', 'A reason must be text of at most 2000 characters.')
    reason = reason.strip()
    target = aliases.get(to_state, to_state)
    request = {'goal_id': goal_id, 'to': to_state, 'reason': reason, 'expected_row_version': expected_row_version}
    with store.tx() as db:
        replay = _replay(db, actor, 'transition', idempotency_key, request)
        if replay is not None:
            return Goal(**replay)
        goal = require_goal(db, goal_id, expected_row_version)
        old = goal.state
        changes: dict[str, Any] = {}
        payload: dict[str, Any] = {'reason': reason, 'operation': to_state}
        if target == 'stopped' and old != 'stopped':
            changes.update(state='stopped', stop_reason=reason, epoch=goal.epoch + 1)
            db.execute("UPDATE execution_grants SET state='revoked',revoked_at=?,revoke_reason='stopped' "
                       "WHERE goal_id=? AND state='active'", (int(time.time()), goal_id))
            _release_undispatched(db, goal_id, 'goal_stopped')
        elif target == 'pausing' and old in ('running', 'monitoring'):
            _release_undispatched(db, goal_id, 'goal_paused')
            changes.update(state='pausing', epoch=goal.epoch + 1)
            payload['inflight'] = _inflight(db, goal_id)
        elif target == 'paused' and old == 'pausing' and _inflight(db, goal_id) == 0:
            changes.update(state='paused')
            payload['inflight'] = 0
        elif target == 'revising' and old in ('approved', 'running', 'paused', 'monitoring', 'research_ready'):
            changes.update(state='revising', revision_from_state=old, epoch=goal.epoch + 1)
            _release_undispatched(db, goal_id, 'goal_revising')
        elif target == 'cancel_revision' and old == 'revising':
            previous = goal.revision_from_state
            safe = previous if previous in ('approved', 'research_ready') else 'paused'
            changes.update(state=safe, revision_from_state=None, epoch=goal.epoch + 1)
            db.execute("UPDATE goal_contracts SET status='discarded' WHERE goal_id=? AND status='proposed'", (goal_id,))
            contract = load_contract(store, goal_id, goal.current_contract_version)
            db.execute('DELETE FROM profile_fields WHERE goal_id=?', (goal_id,))
            for key, field in contract.profile_snapshot.items():
                db.execute('INSERT INTO profile_fields(goal_id,key,value_json,status,provenance,visibility,updated_at) '
                           'VALUES(?,?,?,?,?,?,?)', (goal_id, key, canonical(field['value']), field['status'],
                           field['provenance'], field['visibility'], int(time.time())))
        elif target in ('running', 'monitoring') and old in ('approved', 'paused', 'research_ready'):
            if old == 'research_ready' and target != 'monitoring':
                raise IllegalTransition(old, target)
            grant = active_grant(store, goal_id)
            mode = 'monitoring' if target == 'monitoring' else 'research'
            if not grant or grant.mode != mode or grant.contract_version != goal.current_contract_version or not grant.starts_at <= int(time.time()) < grant.expires_at:
                raise ValidationError('grant', 'A current active execution grant for the explicit mode is required.')
            if target == 'monitoring' and _refresh_policy(load_contract(store, goal_id, goal.current_contract_version).body) is None:
                raise ValidationError('mode', 'Monitoring requires an explicit approved refresh policy.')
            if old == 'approved' and target != 'running':
                raise IllegalTransition(old, target)
            if db.execute("SELECT 1 FROM goals WHERE id!=? AND state IN ('running','monitoring')",
                          (goal_id,)).fetchone():
                raise ValidationError('state', 'Another goal is already running; pause or stop it first.')
            changes.update(state=target, epoch=goal.epoch + (1 if old == 'paused' else 0), revision_from_state=None)
            version = goal.current_strategy_version + 1
            db.execute('INSERT INTO strategies(goal_id,version,contract_version,body_json,created_at) VALUES(?,?,?,?,?)',
                       (goal_id, version, goal.current_contract_version, canonical({'mode': mode}), int(time.time())))
            changes['current_strategy_version'] = version
        elif target == 'replan' and old == 'running':
            version = goal.current_strategy_version + 1
            db.execute('INSERT INTO strategies(goal_id,version,contract_version,body_json,reason,created_at) VALUES(?,?,?,?,?,?)',
                       (goal_id, version, goal.current_contract_version, canonical({'replan': True}), reason or 'operator_replan', int(time.time())))
            changes.update(current_strategy_version=version)
        elif target == 'research_ready' and old == 'running' and actor == 'worker':
            if not _sibling('qualify').completion_status(store, goal).ready:
                raise ValidationError('completion', 'The approved positive completion predicate is not satisfied.')
            changes.update(state='research_ready')
        else:
            raise IllegalTransition(old, target)
        result = _advance(db, goal, **changes)
        _event(db, goal_id, actor, 'transition', old, result.state, payload, idempotency_key, digest(request), result)
        return result


def binding_valid(db: sqlite3.Connection, goal_id: int, contract_version: int, strategy_version: int, epoch: int) -> bool:
    row = db.execute('SELECT g.state,g.epoch,g.current_contract_version,c.status FROM goals g '
                     'JOIN goal_contracts c ON c.goal_id=g.id AND c.version=? WHERE g.id=?',
                     (contract_version, goal_id)).fetchone()
    if not row or row['state'] not in ('running', 'monitoring') or row['epoch'] != epoch or row['current_contract_version'] != contract_version or row['status'] != 'approved':
        return False
    return db.execute('SELECT 1 FROM strategies WHERE goal_id=? AND version=? AND contract_version=?',
                      (goal_id, strategy_version, contract_version)).fetchone() is not None


def _usage_exhausted(store: Any, goal_id: int, limits: dict, now_ts: int) -> bool:
    ledger = _sibling('ledger')
    day_key = ledger.ist_day(now_ts)
    scope = f'goal:{goal_id}'
    windows = (('day', day_key, limits.get('day') or {}), ('lifetime', ledger.LIFETIME_WINDOW, limits.get('lifetime') or {}))
    for kind, window_key, ceilings in windows:
        if not isinstance(ceilings, dict):
            continue
        for counter, ceiling in ceilings.items():
            if type(ceiling) is not int:
                continue
            row = store.one('SELECT reserved,settled,lost FROM resource_ledger WHERE scope_key=? AND window_kind=? '
                            'AND window_key=? AND counter=?', (scope, kind, window_key, counter))
            used = 0 if row is None else int(row['reserved']) + int(row['settled']) + int(row['lost'])
            if used >= ceiling:
                return True
    return False


def runnable_reason(store: Any, now_ts: int) -> str:
    goal = get_goal(store)
    if not goal:
        return 'no_goal'
    if goal.state in ('paused', 'pausing', 'revising'):
        return 'goal_paused'
    if goal.state == 'research_ready':
        return 'research_ready'
    if goal.state not in ('running', 'monitoring'):
        return 'not_running'
    grant = active_grant(store, goal.id)
    if grant is None or grant.contract_version != goal.current_contract_version:
        latest = store.one('SELECT state FROM execution_grants WHERE goal_id=? AND contract_version=? '
                           'ORDER BY id DESC LIMIT 1', (goal.id, goal.current_contract_version))
        if latest is not None and latest['state'] == 'exhausted':
            return 'grant_exhausted'
        return 'no_grant'
    if not grant.starts_at <= now_ts < grant.expires_at:
        return 'grant_expired'
    if grant.mode != ('monitoring' if goal.state == 'monitoring' else 'research'):
        return 'mode_mismatch'
    row = store.one('SELECT status FROM goal_contracts WHERE goal_id=? AND version=?',
                    (goal.id, goal.current_contract_version))
    if not row or row['status'] != 'approved':
        return 'no_contract'
    if _usage_exhausted(store, goal.id, grant.limits, now_ts):
        return 'grant_exhausted'
    return 'runnable'


def runnable_goal(store: Any, now_ts: int) -> RunnableGoal | None:
    if runnable_reason(store, now_ts) != 'runnable':
        return None
    goal = get_goal(store)
    contract = load_contract(store, goal.id, goal.current_contract_version)
    grant = active_grant(store, goal.id)
    frame = _sibling('interview').PublicFrame(**contract.frame)
    safe_body = {key: contract.body[key] for key in ('objective', 'scope', 'completion', 'execution_policy') if key in contract.body}
    safe_contract = dataclasses.replace(contract, body=safe_body, body_hash='', profile_snapshot={}, profile_snapshot_hash='')
    return RunnableGoal(goal, safe_contract, grant, frame)


def redact(value: Any) -> str:
    return '[private]'


def contract_diff(store: Any, goal_id: int, from_version: int | None, to_version: int) -> list[dict]:
    new = load_contract(store, goal_id, to_version)
    old = load_contract(store, goal_id, from_version) if from_version else None
    before = old.profile_snapshot if old else {}
    changes = []
    for key in sorted(set(before) | set(new.profile_snapshot)):
        if before.get(key) != new.profile_snapshot.get(key):
            changes.append({'field_key': key, 'before': before.get(key), 'after': new.profile_snapshot.get(key)})
    if old and old.body != new.body:
        changes.append({'field_key': 'contract', 'before': old.body, 'after': new.body})
    return changes
