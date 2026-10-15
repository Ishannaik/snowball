#!/usr/bin/env python3
"""Local goal interview and explicit approval/execution controls; never acquire sources."""
from __future__ import annotations

import argparse
import dataclasses
import sqlite3
import datetime as dt
import getpass
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from goal import ROOT, _sibling


def configure_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest='goal_command', required=True)
    for name in ('new', 'show', 'interview', 'propose', 'diff', 'approve', 'grant', 'start',
                 'pause', 'resume', 'stop', 'revise', 'cancel-revision', 'replan', 'adopt-topics'):
        command = commands.add_parser(name)
        command.add_argument('--db', type=Path, default=ROOT / 'data' / 'snowball.db')
        command.add_argument('--json', action='store_true')
        command.add_argument('--goal', type=int, help='Explicit goal ID; otherwise current active/latest goal')
        command.add_argument('--expected-row-version', type=int)
        if name == 'new':
            command.add_argument('--slug', required=True)
            command.add_argument('--title', required=True)
        elif name == 'interview':
            command.add_argument('--field')
            command.add_argument('--value-file', type=Path, help='Local JSON value; private values do not enter argv')
            command.add_argument('--answers-file', type=Path, help='Local map of field:{value,confirm,visibility}')
            command.add_argument('--confirm', action='store_true')
            command.add_argument('--visibility', choices=('private', 'public_ok'), default='private')
        elif name == 'propose':
            command.add_argument('--from-fields', action='store_true', required=True)
        elif name == 'diff':
            command.add_argument('--from', dest='from_version', type=int)
            command.add_argument('--to', dest='to_version', type=int, required=True)
        elif name == 'approve':
            command.add_argument('--version', type=int, required=True)
            command.add_argument('--hash', dest='body_hash', required=True)
            command.add_argument('--idempotency-key', default=None)
        elif name == 'grant':
            command.add_argument('--contract', type=int, required=True)
            command.add_argument('--mode', choices=('research', 'monitoring'), default='research')
            command.add_argument('--limits', type=Path, required=True)
            command.add_argument('--expires', required=True, help='UTC ISO date, timestamp, or Unix epoch')
            command.add_argument('--idempotency-key', default=None)
        elif name == 'adopt-topics':
            command.add_argument('--topics', type=Path, required=True)
            command.add_argument('--role', choices=('candidate', 'alternative'), required=True)
        elif name not in ('show',):
            command.add_argument('--reason', default='')
            command.add_argument('--idempotency-key', default=None)
            if name == 'resume':
                command.add_argument('--mode', choices=('research', 'monitoring'))


def _expiry(raw: str) -> int:
    if raw.isdigit():
        return int(raw)
    parsed = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    return int(parsed.replace(tzinfo=dt.timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp())


def _public_summary(store: Any, selected: Any) -> dict:
    goal = _sibling('goal')
    summary = dataclasses.asdict(selected)
    summary['fields'] = {key: {'status': field.status, 'provenance': field.provenance,
                              'visibility': field.visibility, 'value': '[private]'}
                         for key, field in goal.fields(store, selected.id).items()}
    proposals = store.all('SELECT version,status,body_hash,frame_json,created_at FROM goal_contracts '
                          'WHERE goal_id=? ORDER BY version', (selected.id,))
    summary['contracts'] = [{'version': row['version'], 'status': row['status'], 'body_hash': row['body_hash'],
                             'public_frame': json.loads(row['frame_json'] or '{}'), 'created_at': row['created_at']}
                            for row in proposals]
    grant = goal.active_grant(store, selected.id)
    summary['grant'] = dataclasses.asdict(grant) if grant else None
    return summary


def _answer(store: Any, selected: Any, field: str, entry: dict) -> dict:
    goal = _sibling('goal')
    result = goal.record_answer(store, selected.id, field, entry['value'], entry.get('confirm', False),
                                entry.get('visibility', 'private'), selected.row_version, 'cli')
    return {'field_key': result.key, 'status': result.status, 'visibility': result.visibility,
            'value': '[private]', 'row_version': goal.get_goal(store, selected.id).row_version}


def interview(store: Any, selected: Any, args: argparse.Namespace) -> dict:
    goal, questions = _sibling('goal'), _sibling('interview')
    if args.answers_file:
        entries = json.loads(args.answers_file.read_text(encoding='utf-8'))
        if not isinstance(entries, dict) or any(not isinstance(entry, dict) or 'value' not in entry for entry in entries.values()):
            raise goal.ValidationError('answers_file', 'Expected a map of field names to explicit answer records.')
        batch = [(field, entry['value'], entry.get('confirm', False), entry.get('visibility', 'private'))
                 for field, entry in entries.items()]
        saved = goal.apply_answers(store, selected.id, batch, selected.row_version, 'cli')
        answered = [{'field_key': item.key, 'status': item.status, 'visibility': item.visibility,
                     'value': '[private]', 'row_version': goal.get_goal(store, selected.id).row_version}
                    for item in saved]
        return {'goal_id': selected.id, 'answered': answered, 'next_question': _next(store, selected.id)}
    if args.field:
        if args.value_file is None:
            raise goal.ValidationError('value_file', 'Supply a local JSON value file for noninteractive entry.')
        value = json.loads(args.value_file.read_text(encoding='utf-8'))
        result = _answer(store, selected, args.field,
                         {'value': value, 'confirm': args.confirm, 'visibility': args.visibility})
        return {**result, 'next_question': _next(store, selected.id)}
    if args.json or not sys.stdin.isatty():
        raise goal.ValidationError('interview', 'Use --answers-file or --field/--value-file without a terminal.')
    while True:
        selected = goal.get_goal(store, selected.id)
        question = questions.next_question(goal.fields(store, selected.id))
        if question is None:
            return {'goal_id': selected.id, 'interview_complete': True, 'next': 'propose --from-fields'}
        print(f'\n{question.text}\nType: {question.answer_type}. Enter JSON for arrays, objects, booleans and numbers. :quit saves and exits.')
        raw = getpass.getpass('Answer (not echoed): ') if question.visibility_default == 'private' else input('Answer: ')
        if raw == ':quit':
            return {'goal_id': selected.id, 'interview_complete': False, 'next_question': dataclasses.asdict(question)}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        confirmed = input('Confirm this answer as your current intent/fact? [y/N] ').strip().lower() == 'y'
        visibility = question.visibility_default
        if visibility == 'public_ok' and input('Permit this scope field in public research requests? [y/N] ').strip().lower() != 'y':
            visibility = 'private'
        try:
            _answer(store, selected, question.field_key, {'value': value, 'confirm': confirmed, 'visibility': visibility})
        except (ValueError, TypeError):
            print('Invalid answer. It was not recorded or echoed; use the requested type and allowed scope.')


def _next(store: Any, goal_id: int) -> dict | None:
    question = _sibling('interview').next_question(_sibling('goal').fields(store, goal_id))
    return dataclasses.asdict(question) if question else None


def adopt_topics(store: Any, selected: Any, path: Path, role: str) -> dict:
    """Associate deterministic topic seeds only. Queries/pages require a separate live grant."""
    import yaml
    orch, goal = _sibling('orchestrator'), _sibling('goal')
    if selected.current_contract_version is None or selected.state in ('stopped', 'revising'):
        raise goal.ValidationError('contract', 'Adopt topics into a current approved contract outside revision.')
    data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    families = data.get('families') or {}
    seeds = list(data.get('seeds') or [])
    seeds.extend({'id': item['slug'], 'family': item.get('family', 'opportunities'),
                  'focus': item.get('focus') or item.get('question') or item.get('title') or item['slug'],
                  'title': item.get('title'), 'private': item.get('private', False)}
                 for item in data.get('topics', []) if item.get('slug'))
    locks = orch.LockEngine(store)
    associated = []
    with store.tx() as db:
        goal.require_goal(db, selected.id, selected.row_version)
        for seed in seeds:
            name = seed.get('family', 'opportunities')
            default = orch.FAMILY_DEFAULTS.get(name, (14, 1.0, name))
            family = families.get(name) or {}
            db.execute('INSERT OR IGNORE INTO families(name,recheck_days,weight,relevance_floor,focus) VALUES(?,?,?,?,?)',
                       (name, int(family.get('recheck_days', default[0])), float(family.get('weight', default[1])),
                        float(family.get('relevance_floor', 0.08)), family.get('focus', default[2])))
            _, topic_id = locks.topic_lock(db, seed.get('title') or str(seed['id']).replace('-', ' '), seed['focus'],
                                           name, 'seed', private=bool(seed.get('private')), key=orch.slug(str(seed['id'])))
            visibility = 'private' if seed.get('private') else 'public'
            db.execute('INSERT INTO goal_topics(goal_id,contract_version,topic_id,role,visibility) VALUES(?,?,?,?,?) '
                       'ON CONFLICT(goal_id,contract_version,topic_id) DO UPDATE SET role=excluded.role,visibility=excluded.visibility',
                       (selected.id, selected.current_contract_version, topic_id, role, visibility))
            associated.append(topic_id)
        changed = goal._advance(db, selected)
        goal._event(db, selected.id, 'cli', 'adopt_topics', selected.state, changed.state,
                    {'topic_ids': associated, 'role': role})
    return {'goal_id': selected.id, 'topic_ids': associated, 'role': role, 'external_calls': 0}


def execute(args: argparse.Namespace) -> int:
    goal, orch = _sibling('goal'), _sibling('orchestrator')
    store = None
    try:
        store = orch.Store.open(args.db) if args.goal_command == 'new' else orch.Store.connect_existing(args.db, readonly=args.goal_command in ('show', 'diff'))
        if args.goal_command == 'new':
            result = dataclasses.asdict(goal.create_goal(store, args.slug, args.title, 'cli'))
        else:
            selected = goal.get_goal(store, args.goal)
            if selected is None:
                raise LookupError('No selected goal. Create a goal first.')
            expected = args.expected_row_version if args.expected_row_version is not None else selected.row_version
            if expected != selected.row_version and args.goal_command in ('interview', 'propose', 'adopt-topics'):
                raise goal.VersionConflict(selected.row_version)
            command = args.goal_command
            if command == 'show':
                result = _public_summary(store, selected)
            elif command == 'interview':
                result = interview(store, selected, args)
            elif command == 'propose':
                body = _sibling('interview').build_contract(goal.fields(store, selected.id))
                contract = goal.propose_contract(store, selected.id, body, selected.current_contract_version, 'cli',
                                                  expected_row_version=expected)
                result = {'goal_id': selected.id, 'version': contract.version, 'body_hash': contract.body_hash,
                          'row_version': goal.get_goal(store, selected.id).row_version,
                          'public_frame': contract.frame, 'profile_fields': {key: '[private]' for key in contract.profile_snapshot},
                          'diff': [{'field_key': change['field_key'], 'before': '[private]', 'after': '[private]'}
                                   for change in goal.contract_diff(store, selected.id, selected.current_contract_version, contract.version)],
                          'next': 'Review the authenticated console contract, then approve its exact version and hash.'}
            elif command == 'diff':
                result = {'changes': [{'field_key': change['field_key'], 'before': '[private]', 'after': '[private]'}
                                      for change in goal.contract_diff(store, selected.id, args.from_version, args.to_version)]}
            elif command == 'approve':
                result = dataclasses.asdict(goal.approve_contract(store, selected.id, args.version, args.body_hash,
                                            expected, 'cli', args.idempotency_key or uuid.uuid4().hex))
            elif command == 'grant':
                limits = json.loads(args.limits.read_text(encoding='utf-8'))
                result = dataclasses.asdict(goal.grant_execution(store, selected.id, args.contract, args.mode, limits,
                                            _expiry(args.expires), expected, 'cli', args.idempotency_key or uuid.uuid4().hex))
            elif command == 'adopt-topics':
                result = adopt_topics(store, selected, args.topics, args.role)
            else:
                if command == 'resume':
                    grant = goal.active_grant(store, selected.id)
                    requested = args.mode or (grant.mode if grant else 'research')
                    target = 'monitoring' if requested == 'monitoring' else 'running'
                else:
                    target = {'cancel-revision': 'cancel_revision'}.get(command, command)
                result = dataclasses.asdict(goal.transition(store, selected.id, target, args.reason, expected,
                                            'cli', args.idempotency_key or uuid.uuid4().hex))
                if command == 'pause':
                    current = goal.finish_pause(store, selected.id)
                    result = dataclasses.asdict(current)
                    result['inflight'] = goal.inflight_count(store, selected.id)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except goal.VersionConflict as exc:
        error, code = {'error': 'version_conflict', 'current_row_version': exc.current_row_version}, 3
    except goal.IllegalTransition as exc:
        error, code = {'error': 'illegal_transition', 'from_state': exc.from_state, 'to_state': exc.to_state}, 4
    except goal.ValidationError as exc:
        error, code = {'error': 'validation', 'field': exc.field, 'message': exc.message}, 2
    except goal.IdempotencyConflict:
        error, code = {'error': 'idempotency_conflict'}, 3
    except LookupError:
        error, code = {'error': 'goal_not_found'}, 2
    except _sibling('migration').MigrationRequired:
        error, code = {'error': 'schema_migration_required', 'message': 'Run snowball migrate explicitly.'}, 2
    except sqlite3.Error:
        error, code = {'error': 'storage', 'message': 'The goal store could not complete the request.'}, 1
    except (ValueError, TypeError, OSError) as exc:
        error, code = {'error': 'invalid_input_or_storage', 'kind': type(exc).__name__,
                       'message': 'Check input types, local files and schema; private data is not echoed.'}, 2
    finally:
        if store is not None:
            store.db.close()
    print(json.dumps(error), file=sys.stdout if args.json else sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    configure_parser(parser)
    return execute(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
