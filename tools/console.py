"""Authenticated Snowball console.

Each request opens and closes its own SQLite connection. Schema mismatches are
reported and never migrated. Private profile values stay on the authenticated
interview and contract routes.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import http.server
import inspect
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
MAX_BODY = 65536
LOGIN_LIMIT = 5
WRITE_LIMIT = 30
SESSION_TTL = 12 * 60 * 60
ACTOR = 'operator'
_LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
_PUBLIC_PROVENANCE = {'legacy_v3', 'public', 'observed', 'public_fetch'}
_URL_KEYS = {'link', 'url', 'evidence_url', 'source_url', 'canon_url'}
_DROP_KEYS = {
    'profile', 'profile_snapshot', 'profile_snapshot_hash', 'body_hash', 'frame_hash',
    'fit', 'fits', 'shortlist', 'threshold', 'thresholds', 'goals', 'goal', 'citizenship',
}
_HIDDEN_COMPONENT_FIELDS = {
    'citizenship', 'citizenship_conditions', 'gpa_conditions', 'academic_record', 'tests',
    'max_upfront_cash', 'max_net_total_cost', 'min_living_adequacy_rule', 'eligibility_text',
}
_PRIVATE_TEXT = re.compile(r'\b[0-9a-f]{64}\b|upfront|threshold|citizenship|gpa|snapshot|canary', re.I)
_HOST_NAME = re.compile(r'[A-Za-z0-9.-]{1,253}')
_PROGRAM_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}')
_INT = re.compile(r'0|[1-9][0-9]*')
_TRANSITIONS = {
    'start', 'pause', 'stop', 'revise', 'running', 'monitoring', 'cancel_revision', 'paused', 'replan',
}
_UI_TYPES = {
    'degree_level': 'enum', 'modality': 'enum', 'string_list': 'list', 'region_list': 'list',
    'intake_list': 'list', 'bool': 'bool', 'optional_bool': 'bool', 'components': 'object',
    'money': 'money', 'living_rule': 'object', 'hours': 'int', 'citizenship': 'text',
    'prior_degree': 'object', 'academic_record': 'object', 'tests': 'list', 'positive_int': 'int',
    'execution_policy': 'object', 'monitoring': 'object',
}
_CHOICES = {
    'degree_level': ['masters', 'phd', 'other'],
    'modality': ['on_campus', 'online', 'hybrid'],
}
_FIXED_FIELDS = {'degree_level', 'modality'}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = (
    ('Content-Security-Policy', CSP),
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'no-referrer'),
    ('Cross-Origin-Opener-Policy', 'same-origin'),
    ('Cross-Origin-Resource-Policy', 'same-origin'),
    ('Cache-Control', 'no-store'),
)
FALLBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Snowball console</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<main id="main">
<h1>Snowball</h1>
<p id="status" role="status" aria-live="polite"></p>
</main>
<script src="/app.js"></script>
</body>
</html>
"""
FALLBACK_CSS = (
    ":root{--met:#0d6457;--unmet:#b23c12;--unknown:#635e53;--conditional:#f0a65c}"
    "*{box-sizing:border-box}body{margin:0;font:16px/1.5 sans-serif}"
    ":focus-visible{outline:3px solid #1d4ed8}"
    "@media (max-width:720px){main{display:block}}"
    "@media (prefers-reduced-motion:reduce){*{animation:none;transition:none}}"
)
FALLBACK_JS = "'use strict';\n"


class HTTPError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _sibling(name: str) -> Any:
    key = f'snowball_{name}'
    path = ROOT / 'tools' / f'{name}.py'
    existing = sys.modules.get(key)
    if existing is not None and getattr(existing, '__file__', None) and Path(existing.__file__).resolve() == path.resolve():
        return existing
    import importlib.util
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _same(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def _normalize_host(host: str) -> str:
    name = host.strip()
    if name.startswith('[') and name.endswith(']'):
        name = name[1:-1]
    bare = name.lower()
    if bare not in _LOOPBACK:
        raise ValueError('Console control routes require a loopback bind.')
    return bare


def _normalize_origin(origin: str) -> str:
    if not isinstance(origin, str):
        raise ValueError('Allowed origins must be http(s) origin strings.')
    text = origin.strip()
    if text.endswith('/'):
        text = text[:-1]
    parsed = urlsplit(text)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('Allowed origins must be http(s) origins.')
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError('Allowed origins must not include a path, query, or user.')
    return text


def _allowed_hosts(port: int) -> set[str]:
    allowed: set[str] = set()
    for name in _LOOPBACK:
        literal = f'[{name}]' if ':' in name else name
        allowed.add(literal)
        allowed.add(f'{literal}:{port}')
    return allowed


def _json_media(value: str) -> bool:
    parts = [part.strip().lower() for part in value.split(';')]
    if not parts or parts[0] != 'application/json':
        return False
    return all(part in {'', 'charset=utf-8'} for part in parts[1:])


def _loads(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise HTTPError(400, 'bad_request', 'Duplicate JSON fields are not allowed.', None)
        return dict(items)

    def reject_constant(_value: str) -> Any:
        raise HTTPError(400, 'invalid_json', 'The request body is not valid JSON.', None)

    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject_constant)
    except HTTPError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise HTTPError(400, 'invalid_json', 'The request body is not valid JSON.', None) from None


def _object(payload: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPError(422, 'validation', 'Expected a JSON object.', {'field': 'body'})
    allowed = set(required) | set(optional)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise HTTPError(422, 'validation', 'The request contains unknown fields.', {'field': unknown[0]})
    missing = [key for key in required if key not in payload]
    if missing:
        raise HTTPError(422, 'validation', 'A required field is missing.', {'field': missing[0]})
    return payload


def _integer(value: Any, field: str) -> int:
    if type(value) is not int:
        raise HTTPError(422, 'validation', 'Expected an integer.', {'field': field})
    return value


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2000:
        return None
    parsed = urlsplit(value)
    if parsed.scheme in {'http', 'https'} and parsed.hostname and parsed.username is None:
        return value
    return None


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in _DROP_KEYS or key.startswith('goal_'):
                continue
            if key in _URL_KEYS:
                clean[key] = _http_url(item)
            else:
                clean[key] = _sanitize(item, depth + 1)
        return clean
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _private_text(value: str) -> bool:
    return _PRIVATE_TEXT.search(value) is not None


def _cookie_header(value: str, secure: bool, max_age: int | None = None) -> tuple[str, str]:
    parts = [f'session_id={value}', 'HttpOnly', 'SameSite=Strict', 'Path=/']
    if secure:
        parts.append('Secure')
    if max_age is not None:
        parts.append(f'Max-Age={max_age}')
    return ('Set-Cookie', '; '.join(parts))


def _cookies(header: str | None) -> tuple[dict[str, str], set[str]]:
    found: dict[str, str] = {}
    duplicates: set[str] = set()
    if not header:
        return found, duplicates
    for part in header.split(';'):
        if '=' not in part:
            continue
        name, raw = part.split('=', 1)
        name, raw = name.strip(), raw.strip()
        if name in found:
            duplicates.add(name)
        found[name] = raw
    return found, duplicates


def _bump(db: sqlite3.Connection, scope: str, limit: int) -> bool:
    window = int(time.time()) // 60
    db.execute(
        'INSERT INTO console_rates(scope, window, attempts) VALUES(?, ?, 1) '
        'ON CONFLICT(scope, window) DO UPDATE SET attempts = attempts + 1',
        (scope, window),
    )
    count = db.execute(
        'SELECT attempts FROM console_rates WHERE scope=? AND window=?', (scope, window)
    ).fetchone()[0]
    db.execute('DELETE FROM console_rates WHERE scope=? AND window < ?', (scope, window - 1))
    return count <= limit


def _table(store: Any, name: str) -> bool:
    return store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


def _columns(store: Any, table: str) -> set[str]:
    return {row[1] for row in store.all(f'PRAGMA table_info({table})')}


def _model(value: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    raise TypeError('Expected a dataclass.')


def _completion(store: Any, selected: Any) -> dict[str, Any]:
    qualify = _sibling('qualify')
    try:
        status = qualify.completion_status(store, selected)
    except qualify.QualificationError:
        return {'ready': False, 'substate': None, 'detail': None}
    detail = status.detail
    if isinstance(detail, str):
        detail = None if _private_text(detail) else {'message': detail}
    elif not isinstance(detail, dict):
        detail = None
    else:
        detail = _sanitize(detail)
    return {'ready': bool(status.ready), 'substate': status.substate, 'detail': detail}


def _inflight(store: Any, goal_id: int) -> int:
    if not _table(store, 'resource_operations'):
        return 0
    return int(store.one(
        "SELECT COUNT(*) FROM resource_operations WHERE goal_id=? AND state='dispatched'", (goal_id,)
    )[0])


def _question_view(question: Any) -> dict[str, Any]:
    native = question.answer_type
    if question.field_key == 'consent_private_evaluation':
        kind = 'consent'
        allow_unknown = False
    else:
        kind = _UI_TYPES.get(native, 'text')
        allow_unknown = native == 'optional_bool' or not question.required
    return {
        'field_key': question.field_key,
        'text': question.text,
        'answer_type': kind,
        'required': bool(question.required),
        'visibility_default': question.visibility_default,
        'allow_unknown': allow_unknown,
        'choices': list(_CHOICES.get(question.field_key, [])),
        'fixed': question.field_key in _FIXED_FIELDS,
    }


def _confirmed(field: Any) -> bool:
    return field is not None and field.status == 'known' and field.provenance == 'user_confirmed'


def _private_evaluation_enabled(rows: dict[str, Any]) -> bool:
    consent = rows.get('consent_private_evaluation')
    return _confirmed(consent) and consent.value is True


def _missing_fields(interview: Any, rows: dict[str, Any]) -> list[str]:
    """Same blocker as next_question. A confirmed no leaves private eligibility rows unconfirmed."""
    missing: list[str] = []
    regions = rows.get('regions')
    regions_open = regions is None or regions.value in ([], ())
    enabled = _private_evaluation_enabled(rows)
    private = frozenset(getattr(interview, 'PRIVATE_ELIGIBILITY_FIELDS', ()))
    for question in interview.QUESTIONS:
        key = question.field_key
        if key in private and not enabled:
            continue
        if key == 'scope_broad_acknowledged' and not regions_open:
            continue
        present = key in rows
        if not question.required and not (enabled and key in private and present):
            continue
        current = rows.get(key)
        if not _confirmed(current):
            missing.append(key)
            continue
        if key == 'scope_broad_acknowledged' and current.value is not True:
            missing.append(key)
        elif key == 'consent_private_evaluation' and type(current.value) is not bool:
            missing.append(key)
    return missing


def _diff_parts(entry: Any) -> tuple[Any, str]:
    if isinstance(entry, dict) and 'value' in entry and 'visibility' in entry:
        visibility = entry['visibility'] if entry['visibility'] in {'private', 'public_ok'} else 'private'
        return entry['value'], visibility
    return entry, 'private'


def _money_bits(value: Any) -> tuple[str, str | None, str | None] | None:
    period = None
    if isinstance(value, dict):
        period = value.get('period') if isinstance(value.get('period'), str) else None
        amount = value.get('amount', value.get('value'))
        currency = value.get('currency')
    else:
        amount, currency = value, None
    if isinstance(amount, bool) or amount is None:
        return None
    if isinstance(amount, (int, float)):
        amount = format(amount, 'f') if isinstance(amount, float) else str(amount)
    if not isinstance(amount, str) or (currency is not None and not isinstance(currency, str)):
        return None
    return amount, currency if isinstance(currency, str) else None, period


def _components(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, dict):
        return []
    rows: list[dict[str, Any]] = []

    def add(label: Any, value: Any, period: str | None) -> None:
        if not isinstance(label, str) or label in _HIDDEN_COMPONENT_FIELDS:
            return
        bits = _money_bits(value)
        if bits is None:
            return
        amount, currency, nested_period = bits
        rows.append({
            'label': label,
            'amount': amount,
            'currency': currency,
            'period': period or nested_period,
        })

    for cost in raw.get('costs') or []:
        if isinstance(cost, dict):
            add(cost.get('field_key'), cost.get('value'), cost.get('period') if isinstance(cost.get('period'), str) else None)
    for award in raw.get('awards') or []:
        if not isinstance(award, dict):
            continue
        for field_key, item in (award.get('values') or {}).items():
            if isinstance(item, dict):
                add(field_key, item.get('value'), item.get('period') if isinstance(item.get('period'), str) else None)
    return rows


def _engines() -> list[dict[str, Any]]:
    roles = ('extract', 'review', 'plan', 'private_eval')
    unavailable = [
        {'role': role, 'model': None, 'available': False, 'reason': 'unavailable', 'usage_kind': None}
        for role in roles
    ]
    try:
        transport = _sibling('engine_transport')
    except Exception:
        return unavailable
    reporter = getattr(transport, 'console_status', None)
    if not callable(reporter):
        return unavailable
    try:
        reported = reporter()
    except Exception:
        return unavailable
    if not isinstance(reported, list):
        return unavailable
    by_role = {}
    for item in reported:
        if isinstance(item, dict) and item.get('role') in roles and type(item.get('available')) is bool:
            by_role[item['role']] = {
                'role': item['role'],
                'model': item.get('model') if isinstance(item.get('model'), str) else None,
                'available': item['available'],
                'reason': item.get('reason') if isinstance(item.get('reason'), str) else None,
                'usage_kind': item.get('usage_kind') if item.get('usage_kind') in {'actual', 'estimated', 'legacy', 'unknown'} else None,
            }
    return [by_role.get(role, unavailable[index]) for index, role in enumerate(roles)]


def _resets_at(now: int) -> int:
    current = datetime.fromtimestamp(now, IST)
    nxt = (current + timedelta(days=1)).date()
    return int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=IST).timestamp())


def _map_goal(exc: Exception) -> HTTPError | None:
    goal = _sibling('goal')
    qualify = _sibling('qualify')
    if isinstance(exc, goal.VersionConflict):
        return HTTPError(409, 'version_conflict', 'The goal changed; reload the reviewed version.',
                         {'row_version': exc.current_row_version})
    if isinstance(exc, goal.IdempotencyConflict):
        return HTTPError(409, 'idempotency_conflict', 'The idempotency key was used for a different request.', None)
    if isinstance(exc, goal.IllegalTransition):
        return HTTPError(422, 'validation', 'That transition is not available.',
                         {'from': exc.from_state, 'to': exc.to_state})
    if isinstance(exc, goal.ValidationError):
        if exc.field == 'body_hash':
            return HTTPError(409, 'hash_mismatch', 'The reviewed contract hash does not match.', {'field': 'body_hash'})
        return HTTPError(422, 'validation', exc.message, {'field': exc.field})
    if isinstance(exc, qualify.QualificationError):
        return HTTPError(422, 'validation', 'The request does not match the current goal.', {'field': exc.field})
    if isinstance(exc, LookupError):
        return HTTPError(404, 'not_found', 'Not found.', None)
    return None


def _propose(goal: Any, store: Any, goal_id: int, body: dict, base: int | None, row_version: int) -> Any:
    kwargs: dict[str, Any] = {
        'store': store, 'goal_id': goal_id, 'body': body, 'base_version': base, 'actor': ACTOR,
    }
    if 'expected_row_version' in inspect.signature(goal.propose_contract).parameters:
        kwargs['expected_row_version'] = row_version
    return goal.propose_contract(**kwargs)


def _slug(title: str) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:72] or 'goal'
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', base):
        return 'goal'
    return base


def issue_bootstrap_token(db_path: Path, ttl_seconds: int) -> str:
    """Store only a hash. The returned token is shown once by the caller."""
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
        raise ValueError('Bootstrap token lifetime must be between 1 and 86400 seconds.')
    token = secrets.token_urlsafe(32)
    store = _sibling('orchestrator').Store.connect_existing(db_path)
    try:
        now = int(time.time())
        with store.tx() as db:
            db.execute(
                'INSERT INTO console_tokens(token_hash, expires_at, created_at) VALUES(?,?,?)',
                (_hash(token), now + ttl_seconds, now),
            )
    finally:
        store.db.close()
    return token


def _assets() -> tuple[str, str, str]:
    try:
        ui = _sibling('console_ui')
        html, css, js = ui.HTML, ui.CSS, ui.JS
        if all(isinstance(item, str) and item for item in (html, css, js)):
            return html, css, js
    except Exception:
        pass
    return FALLBACK_HTML, FALLBACK_CSS, FALLBACK_JS


class ConsoleServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, host: str, port: int, db_path: Path, allowed_origins: tuple[str, ...], cookie_secure: bool):
        self.db_path = Path(db_path)
        self.allowed_origins = frozenset(_normalize_origin(item) for item in allowed_origins)
        if not self.allowed_origins:
            raise ValueError('At least one allowed origin is required.')
        if type(cookie_secure) is not bool:
            raise ValueError('cookie_secure must be a boolean.')
        self.cookie_secure = cookie_secure
        bind_host = _normalize_host(host)
        if bind_host == '::1':
            self.address_family = socket.AF_INET6
        super().__init__((bind_host, port), ConsoleHandler)
        self.allowed_hosts = _allowed_hosts(self.server_address[1])


class ConsoleHandler(http.server.BaseHTTPRequestHandler):
    server: ConsoleServer
    server_version = 'SnowballConsole'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def setup(self) -> None:
        super().setup()
        self.request.settimeout(15)
        self._sent = False

    def do_GET(self) -> None:
        self._handle('GET')

    def do_POST(self) -> None:
        self._handle('POST')

    def do_DELETE(self) -> None:
        self._handle('DELETE')

    def do_PUT(self) -> None:
        self._handle('PUT')

    def do_PATCH(self) -> None:
        self._handle('PATCH')

    def do_OPTIONS(self) -> None:
        self._handle('OPTIONS')

    def _handle(self, method: str) -> None:
        self._sent = False
        try:
            self._check_host()
            if method not in {'GET', 'POST', 'DELETE'}:
                raise HTTPError(405, 'method_not_allowed', 'Method not allowed.', None)
            body = self._read_body()
            path = urlsplit(self.path).path or '/'
            if method == 'GET' and path in {'/', '/index.html', '/app.css', '/app.js'}:
                self._static(path)
                return
            if not path.startswith('/api/'):
                raise HTTPError(404, 'not_found', 'Not found.', None)
            self._api(method, path, body)
        except HTTPError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self._sent = True
        except Exception as exc:
            mapped = _map_goal(exc)
            if mapped is not None:
                self._error(mapped)
            elif not self._sent:
                self._error(HTTPError(500, 'internal', 'The request could not be completed.', None))

    def _check_host(self) -> None:
        values = self.headers.get_all('Host')
        if not values or len(values) != 1:
            raise HTTPError(400, 'bad_request', 'Exactly one Host header is required.', None)
        host = values[0].strip().lower()
        if not host or ',' in host or host not in self.server.allowed_hosts:
            raise HTTPError(421, 'bad_host', 'Unexpected Host header.', None)

    def _check_origin(self) -> None:
        values = self.headers.get_all('Origin')
        if not values or len(values) != 1:
            raise HTTPError(403, 'forbidden', 'The request origin is not allowed.', None)
        origin = values[0].strip()
        if origin.endswith('/'):
            origin = origin[:-1]
        if origin not in self.server.allowed_origins:
            raise HTTPError(403, 'forbidden', 'The request origin is not allowed.', None)

    def _read_body(self) -> bytes:
        if self.headers.get_all('Transfer-Encoding'):
            raise HTTPError(400, 'bad_request', 'Transfer-Encoding is not accepted.', None)
        lengths = self.headers.get_all('Content-Length')
        if not lengths:
            if self.command == 'POST':
                raise HTTPError(400, 'bad_request', 'Content-Length is required.', None)
            return b''
        if len(lengths) != 1 or not _INT.fullmatch(lengths[0].strip()):
            raise HTTPError(400, 'bad_request', 'Content-Length is invalid.', None)
        length = int(lengths[0])
        if length > MAX_BODY:
            if length <= 1_000_000:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            raise HTTPError(413, 'payload_too_large', 'The request body exceeds 65536 bytes.', None)
        if length and self.command == 'GET':
            raise HTTPError(400, 'bad_request', 'GET does not accept a body.', None)
        if length == 0:
            return b''
        data = self.rfile.read(length)
        if len(data) != length:
            raise HTTPError(400, 'bad_request', 'The request body ended early.', None)
        return data

    def _static(self, path: str) -> None:
        html, css, js = _assets()
        if path in {'/', '/index.html'}:
            body, kind = html, 'text/html; charset=utf-8'
        elif path == '/app.css':
            body, kind = css, 'text/css; charset=utf-8'
        else:
            body, kind = js, 'text/javascript; charset=utf-8'
        self._send(200, body.encode('utf-8'), kind)

    def _api(self, method: str, path: str, body: bytes) -> None:
        if method in {'POST', 'DELETE'}:
            self._check_origin()
        readonly = method == 'GET' and path in {'/api/entities', '/api/graph', '/api/status'}
        try:
            store = _open_store(self.server.db_path, readonly=readonly)
        except HTTPError:
            raise
        try:
            self._dispatch(store, method, path, body)
        finally:
            store.db.close()

    def _dispatch(self, store: Any, method: str, path: str, body: bytes) -> None:
        if path == '/api/session':
            self._session_route(store, method, body)
            return
        if path == '/api/entities' and method == 'GET':
            self._json(200, _sanitize(_sibling('visualize').fetch_opportunities(store.db)))
            return
        if path == '/api/graph' and method == 'GET':
            self._json(200, _sanitize(_sibling('visualize').fetch_graph_data(store.db)))
            return
        if path == '/api/status' and method == 'GET':
            payload = _sanitize(_sibling('visualize').fetch_status_metrics(store.db, self.server.db_path))
            self._json(200, payload if isinstance(payload, dict) else {})
            return
        session = self._session(store)
        if session is None:
            raise HTTPError(401, 'unauthorized', 'Sign in required.', None)
        if method in {'POST', 'DELETE'}:
            self._check_csrf(session)
            self._write_budget(store, int(session['id']))
        if path == '/api/goal' and method == 'GET':
            self._json(200, _goal_view(store, _require_current(store)))
            return
        if path == '/api/goals' and method == 'POST':
            self._json(201, _model(_create_goal(store, _object(_loads(body), ('title',)))))
            return
        match = re.fullmatch(r'/api/goals/(0|[1-9][0-9]*)(?:/(.*))?', path)
        if match is None:
            raise HTTPError(404, 'not_found', 'Not found.', None)
        goal_id = int(match.group(1))
        rest = match.group(2) or ''
        if rest == 'interview' and method == 'GET':
            self._json(200, _interview_view(store, goal_id))
            return
        if rest == 'interview/answers' and method == 'POST':
            self._json(200, _model(_save_answer(store, goal_id, _object(
                _loads(body), ('field_key', 'value', 'confirm', 'visibility', 'expected_row_version')))))
            return
        if rest == 'contract/draft' and method == 'POST':
            self._json(201, _draft(store, goal_id, _object(_loads(body), ('base_version', 'from_fields'))))
            return
        if rest == 'contract/diff' and method == 'GET':
            self._json(200, _diff(store, goal_id, self._query()))
            return
        if rest == 'contract/approve' and method == 'POST':
            self._json(200, _model(_approve(store, goal_id, _object(
                _loads(body), ('version', 'body_hash', 'expected_row_version')), self._idempotency())))
            return
        if rest == 'grants' and method == 'POST':
            self._json(201, _model(_grant(store, goal_id, _object(
                _loads(body), ('contract_version', 'mode', 'limits', 'expires_at', 'expected_row_version')
            ), self._idempotency())))
            return
        if rest == 'transition' and method == 'POST':
            self._json(200, _model(_transition(store, goal_id, _object(
                _loads(body), ('to', 'reason', 'expected_row_version')), self._idempotency())))
            return
        if rest == 'shortlist' and method == 'GET':
            self._json(200, _shortlist(store, _require_id(store, goal_id)))
            return
        if rest == 'activity' and method == 'GET':
            self._json(200, _activity(store, _require_id(store, goal_id)))
            return
        if rest == 'events' and method == 'GET':
            _require_id(store, goal_id)
            self._json(200, {'events': _events(store, goal_id)})
            return
        evidence = re.fullmatch(r'evidence/([^/]+)', rest or '')
        if evidence and method == 'GET':
            self._json(200, _evidence(store, _require_id(store, goal_id), _program_key(evidence.group(1))))
            return
        raise HTTPError(404, 'not_found', 'Not found.', None)

    def _session_route(self, store: Any, method: str, body: bytes) -> None:
        if method == 'POST':
            self._login(store, body)
            return
        session = self._session(store)
        if session is None:
            raise HTTPError(401, 'unauthorized', 'Sign in required.', None)
        if method == 'GET':
            self._json(200, {
                'authenticated': True,
                'expires_at': int(session['expires_at']),
                'csrf': session['csrf_value'],
            })
            return
        if method == 'DELETE':
            self._check_csrf(session)
            self._write_budget(store, int(session['id']))
            payload = _loads(body) if body else None
            if payload not in (None, {}):
                raise HTTPError(422, 'validation', 'Logout accepts an empty body.', {'field': 'body'})
            with store.tx() as db:
                db.execute(
                    'UPDATE console_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                    (int(time.time()), int(session['id'])),
                )
            self._send(204, b'', 'application/json; charset=utf-8', (
                _cookie_header('', self.server.cookie_secure, 0),
            ))
            return
        raise HTTPError(405, 'method_not_allowed', 'Method not allowed.', None)

    def _login(self, store: Any, body: bytes) -> None:
        address = self.client_address[0] if self.client_address else 'local'
        with store.tx() as db:
            allowed = _bump(db, f'login:{address}', LOGIN_LIMIT)
        if not allowed:
            raise HTTPError(429, 'rate_limited', 'Too many sign-in attempts.', {'retry_after': 60})
        self._require_json_type()
        payload = _object(_loads(body), ('token',))
        token = payload['token']
        if not isinstance(token, str) or not token or len(token) > 200:
            raise HTTPError(401, 'unauthorized', 'The sign-in token is not valid.', None)
        cookie = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = int(time.time())
        expires = now + SESSION_TTL
        with store.tx() as db:
            before = db.total_changes
            db.execute(
                'UPDATE console_tokens SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?',
                (now, _hash(token), now),
            )
            if db.total_changes == before:
                raise HTTPError(401, 'unauthorized', 'The sign-in token is not valid.', None)
            db.execute(
                'INSERT INTO console_sessions(session_hash, csrf_value, actor, expires_at, created_at) '
                'VALUES(?,?,?,?,?)',
                (_hash(cookie), csrf, ACTOR, expires, now),
            )
        self._json(201, {'csrf': csrf, 'expires_at': expires}, (
            _cookie_header(cookie, self.server.cookie_secure, SESSION_TTL),
        ))

    def _session(self, store: Any) -> sqlite3.Row | None:
        found, duplicates = _cookies(self.headers.get('Cookie'))
        if 'session_id' in duplicates:
            return None
        token = found.get('session_id', '')
        if not token or len(token) > 200:
            return None
        row = store.one('SELECT * FROM console_sessions WHERE session_hash=?', (_hash(token),))
        if row is None or row['revoked_at'] is not None or int(row['expires_at']) <= int(time.time()):
            return None
        return row

    def _check_csrf(self, session: sqlite3.Row) -> None:
        values = self.headers.get_all('X-Snowball-CSRF')
        if not values or len(values) != 1 or not _same(values[0], session['csrf_value']):
            raise HTTPError(403, 'forbidden', 'The request is missing a valid CSRF token.', None)

    def _write_budget(self, store: Any, session_id: int) -> None:
        with store.tx() as db:
            allowed = _bump(db, f'write:{session_id}', WRITE_LIMIT)
        if not allowed:
            raise HTTPError(429, 'rate_limited', 'Too many writes.', {'retry_after': 60})

    def _require_json_type(self) -> None:
        values = self.headers.get_all('Content-Type')
        if not values or len(values) != 1 or not _json_media(values[0]):
            raise HTTPError(415, 'unsupported_media', 'Content-Type must be application/json.', None)

    def _idempotency(self) -> str:
        values = self.headers.get_all('Idempotency-Key')
        if not values or len(values) != 1:
            raise HTTPError(422, 'validation', 'An idempotency key is required.', {'field': 'Idempotency-Key'})
        key = values[0].strip()
        if not key or len(key) > 200:
            raise HTTPError(422, 'validation', 'An idempotency key is required.', {'field': 'Idempotency-Key'})
        return key

    def _query(self) -> dict[str, str]:
        pairs = parse_qsl(urlsplit(self.path).query, keep_blank_values=True)
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise HTTPError(400, 'bad_request', 'Duplicate query fields are not allowed.', None)
        return dict(pairs)

    def _json(self, status: int, payload: Any, extra: tuple[tuple[str, str], ...] = ()) -> None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        self._send(status, raw, 'application/json; charset=utf-8', extra)

    def _error(self, exc: HTTPError) -> None:
        if self._sent:
            return
        self._json(exc.status, {'error': exc.code, 'message': exc.message, 'details': exc.details})

    def _send(self, status: int, body: bytes, content_type: str, extra: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        if status != 204:
            self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)
        self._sent = True

    def log_request(self, code: int | str = '-', size: int | str = '-') -> None:
        try:
            status = int(code)
        except (TypeError, ValueError):
            return
        if status >= 400:
            sys.stderr.write(f'console: {status}\n')

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write('console: error\n')

    def log_error(self, fmt: str, *args: Any) -> None:
        sys.stderr.write('console: error\n')


def _open_store(path: Path, readonly: bool) -> Any:
    if not Path(path).is_file():
        raise HTTPError(503, 'unavailable', 'The database is not available.', {'reason': 'missing'})
    migration = _sibling('migration')
    try:
        return _sibling('orchestrator').Store.connect_existing(path, readonly=readonly)
    except migration.MigrationRequired:
        raise HTTPError(503, 'unavailable', 'The database schema is not the current version.',
                        {'schema': 'unsupported'}) from None
    except (OSError, sqlite3.Error):
        raise HTTPError(503, 'unavailable', 'The database is not available.', {'reason': 'unavailable'}) from None


def _require_current(store: Any) -> Any:
    selected = _sibling('goal').get_goal(store)
    if selected is None:
        raise HTTPError(404, 'not_found', 'No goal exists.', None)
    return selected


def _require_id(store: Any, goal_id: int) -> Any:
    selected = _sibling('goal').get_goal(store, goal_id)
    if selected is None:
        raise HTTPError(404, 'not_found', 'Not found.', None)
    return selected


def _program_key(segment: str) -> str:
    key = unquote(segment)
    if not _PROGRAM_KEY.fullmatch(key) or key in {'.', '..'}:
        raise HTTPError(404, 'not_found', 'Not found.', None)
    return key


def _goal_view(store: Any, selected: Any) -> dict[str, Any]:
    goal = _sibling('goal')
    versions = store.all('SELECT version FROM goal_contracts WHERE goal_id=? ORDER BY version', (selected.id,))
    contracts = []
    for row in versions:
        try:
            contracts.append(_model(goal.load_contract(store, selected.id, int(row['version']))))
        except LookupError:
            continue
    grant = goal.active_grant(store, selected.id)
    runtime = store.one('SELECT substate, reason FROM goal_runtime WHERE goal_id=?', (selected.id,)) if _table(store, 'goal_runtime') else None
    completion = _completion(store, selected)
    inflight = _inflight(store, selected.id)
    substate = runtime['substate'] if runtime is not None and runtime['substate'] else completion.get('substate')
    reason = runtime['reason'] if runtime is not None else None
    if selected.state == 'pausing':
        substate = 'pausing'
        reason = 'in-flight work is still draining' if inflight else 'draining'
    if isinstance(reason, str) and _private_text(reason):
        reason = None
    blocker = goal.runnable_reason(store, int(time.time()))
    return {
        'goal': _model(selected),
        'contracts': contracts,
        'grant': None if grant is None else _model(grant),
        'inflight_count': inflight,
        'substate': substate,
        'substate_reason': reason,
        'blocker': None if blocker == 'runnable' else blocker,
        'completion': completion,
    }


def _create_goal(store: Any, payload: dict[str, Any]) -> Any:
    goal = _sibling('goal')
    title = payload['title']
    if not isinstance(title, str):
        raise HTTPError(422, 'validation', 'A title of 1–300 characters is required.', {'field': 'title'})
    if store.one("SELECT 1 FROM goals WHERE state != 'stopped'"):
        raise HTTPError(409, 'conflict', 'A non-stopped goal already exists.', None)
    base = _slug(title)
    last_error: Exception | None = None
    for index in range(1, 40):
        slug = base if index == 1 else f'{base[:68]}-{index}'
        try:
            return goal.create_goal(store, slug, title, ACTOR)
        except goal.ValidationError as exc:
            last_error = exc
            if exc.field != 'slug':
                raise
    raise HTTPError(409, 'conflict', 'A goal with that title could not be created.', None) from last_error


def _interview_view(store: Any, goal_id: int) -> dict[str, Any]:
    goal = _sibling('goal')
    interview = _sibling('interview')
    selected = _require_id(store, goal_id)
    rows = goal.fields(store, goal_id)
    nxt = interview.next_question(rows)
    turns = []
    if _table(store, 'interview_turns'):
        for row in store.all(
            'SELECT t.seq, t.kind, t.field_key, t.text, t.created_at FROM interview_turns t '
            'JOIN interview_sessions s ON s.id=t.session_id WHERE s.goal_id=? ORDER BY t.id LIMIT 200',
            (goal_id,),
        ):
            turns.append({key: row[key] for key in ('seq', 'kind', 'field_key', 'text', 'created_at')})
    return {
        'goal_id': goal_id,
        'row_version': selected.row_version,
        'next': None if nxt is None else _question_view(nxt),
        'questions': [_question_view(question) for question in interview.QUESTIONS],
        'fields': [_model(field) for _key, field in sorted(rows.items())],
        'missing': _missing_fields(interview, rows),
        'turns': turns,
    }


def _save_answer(store: Any, goal_id: int, payload: dict[str, Any]) -> Any:
    goal = _sibling('goal')
    interview = _sibling('interview')
    field_key = payload['field_key']
    if not isinstance(field_key, str):
        raise HTTPError(422, 'validation', 'Unknown interview field.', {'field': 'field_key'})
    question = next((item for item in interview.QUESTIONS if item.field_key == field_key), None)
    if question is None:
        raise HTTPError(422, 'validation', 'Unknown interview field.', {'field': 'field_key'})
    if type(payload['confirm']) is not bool:
        raise HTTPError(422, 'validation', 'Confirmation must be explicit.', {'field': 'confirm'})
    if payload['visibility'] not in {'private', 'public_ok'}:
        raise HTTPError(422, 'validation', 'Visibility must be explicit.', {'field': 'visibility'})
    if question.field_key in _FIXED_FIELDS and payload['visibility'] != question.visibility_default:
        raise HTTPError(422, 'validation', 'This field has a fixed visibility.', {'field': 'visibility'})
    return goal.record_answer(
        store, goal_id, field_key, payload['value'], payload['confirm'], payload['visibility'],
        _integer(payload['expected_row_version'], 'expected_row_version'), ACTOR,
    )


def _draft(store: Any, goal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    goal = _sibling('goal')
    interview = _sibling('interview')
    if payload['from_fields'] is not True:
        raise HTTPError(422, 'validation', 'A draft is built from confirmed fields.', {'field': 'from_fields'})
    base = payload['base_version']
    if base is not None:
        base = _integer(base, 'base_version')
    selected = _require_id(store, goal_id)
    contract = _propose(goal, store, goal_id, interview.build_contract(goal.fields(store, goal_id)), base, selected.row_version)
    fresh = goal.get_goal(store, goal_id)
    body = _model(contract)
    body['goal_row_version'] = None if fresh is None else fresh.row_version
    return body


def _diff(store: Any, goal_id: int, query: dict[str, str]) -> dict[str, Any]:
    goal = _sibling('goal')
    _require_id(store, goal_id)
    if 'to' not in query or not _INT.fullmatch(query['to']):
        raise HTTPError(422, 'validation', 'A destination contract version is required.', {'field': 'to'})
    source = query.get('from')
    if source in (None, ''):
        from_version = None
    elif _INT.fullmatch(source):
        from_version = int(source)
    else:
        raise HTTPError(422, 'validation', 'The source contract version is invalid.', {'field': 'from'})
    to_version = int(query['to'])
    changes = []
    for change in goal.contract_diff(store, goal_id, from_version, to_version):
        before, before_visibility = _diff_parts(change.get('before'))
        after, after_visibility = _diff_parts(change.get('after'))
        visibility = 'private' if 'private' in {before_visibility, after_visibility} else 'public_ok'
        changes.append({
            'path': change.get('field_key'),
            'before': before,
            'after': after,
            'visibility': visibility,
        })
    from_hash = None if from_version is None else goal.load_contract(store, goal_id, from_version).body_hash
    to_hash = goal.load_contract(store, goal_id, to_version).body_hash
    return {'from': from_version, 'to': to_version, 'from_hash': from_hash, 'to_hash': to_hash, 'changes': changes}


def _approve(store: Any, goal_id: int, payload: dict[str, Any], key: str) -> Any:
    goal = _sibling('goal')
    if not isinstance(payload['body_hash'], str):
        raise HTTPError(422, 'validation', 'The reviewed contract hash is required.', {'field': 'body_hash'})
    return goal.approve_contract(
        store, goal_id, _integer(payload['version'], 'version'), payload['body_hash'],
        _integer(payload['expected_row_version'], 'expected_row_version'), ACTOR, key,
    )


def _grant(store: Any, goal_id: int, payload: dict[str, Any], key: str) -> Any:
    goal = _sibling('goal')
    if payload['mode'] not in {'research', 'monitoring'}:
        raise HTTPError(422, 'validation', 'Mode must be research or monitoring.', {'field': 'mode'})
    if not isinstance(payload['limits'], dict):
        raise HTTPError(422, 'validation', 'Limits must be an object.', {'field': 'limits'})
    return goal.grant_execution(
        store, goal_id, _integer(payload['contract_version'], 'contract_version'), payload['mode'],
        payload['limits'], _integer(payload['expires_at'], 'expires_at'),
        _integer(payload['expected_row_version'], 'expected_row_version'), ACTOR, key,
    )


def _transition(store: Any, goal_id: int, payload: dict[str, Any], key: str) -> Any:
    goal = _sibling('goal')
    if payload['to'] not in _TRANSITIONS:
        raise HTTPError(422, 'validation', 'That transition is not available.', {'field': 'to'})
    if not isinstance(payload['reason'], str):
        raise HTTPError(422, 'validation', 'A reason must be text.', {'field': 'reason'})
    return goal.transition(
        store, goal_id, payload['to'], payload['reason'],
        _integer(payload['expected_row_version'], 'expected_row_version'), ACTOR, key,
    )


def _shortlist(store: Any, selected: Any) -> dict[str, Any]:
    version = selected.current_contract_version
    programs: dict[str, dict[str, Any]] = {}
    if version is not None and _table(store, 'goal_programs'):
        for row in store.all(
            'SELECT gp.program_key, gp.role, p.name FROM goal_programs gp '
            'JOIN programs p ON p.key=gp.program_key WHERE gp.goal_id=? AND gp.contract_version=? '
            'ORDER BY gp.program_key',
            (selected.id, version),
        ):
            programs[row['program_key']] = {
                'program_key': row['program_key'], 'name': row['name'], 'role': row['role'],
                'evaluated': False, 'packages': [], 'matrix': [],
            }
    package_rows = []
    if _table(store, 'funding_packages') and programs:
        marks = ','.join('?' * len(programs))
        package_rows = store.all(
            f'SELECT key, program_key, cycle_label, components_json FROM funding_packages '
            f'WHERE program_key IN ({marks}) ORDER BY key',
            tuple(programs),
        )
    for row in package_rows:
        bucket = programs.get(row['program_key'])
        if bucket is None:
            continue
        bucket['packages'].append({
            'key': row['key'],
            'cycle_label': row['cycle_label'],
            'components': _components(row['components_json']),
        })
    contested: set[tuple[str, str]] = set()
    if _table(store, 'contradictions') and programs:
        marks = ','.join('?' * len(programs))
        for row in store.all(
            f'SELECT program_key, field_key FROM contradictions WHERE winner IS NULL AND resolved_at IS NULL '
            f'AND program_key IN ({marks})',
            tuple(programs),
        ):
            contested.add((row['program_key'], row['field_key']))
    pending: set[str] = set()
    if _table(store, 'observations') and programs:
        marks = ','.join('?' * len(programs))
        for row in store.all(
            f"SELECT DISTINCT program_key FROM observations WHERE assurance IN ('review_pending','review_unavailable') "
            f'AND invalidated_at IS NULL AND program_key IN ({marks})',
            tuple(programs),
        ):
            pending.add(row['program_key'])
    if version is not None and _table(store, 'constraint_evaluations'):
        for row in store.all(
            'SELECT program_key, package_key, constraint_key, status, reason FROM constraint_evaluations '
            'WHERE goal_id=? AND contract_version=? AND stale_for_goal=0 AND epoch=? ORDER BY program_key, constraint_key',
            (selected.id, version, selected.epoch),
        ):
            bucket = programs.get(row['program_key'])
            if bucket is None:
                continue
            status = 'contested' if (row['program_key'], row['constraint_key']) in contested else row['status']
            if status not in {'met', 'unmet', 'unknown', 'conditional', 'contested'}:
                status = 'unknown'
            needs_review = status in {'unknown', 'contested'} or row['program_key'] in pending
            reason = row['reason'] if isinstance(row['reason'], str) and not _private_text(row['reason']) else 'private comparison withheld'
            bucket['matrix'].append({
                'constraint_key': row['constraint_key'],
                'package_key': row['package_key'],
                'status': status,
                'reason': reason,
                'needs_human_review': needs_review,
                'next': _next_node(store, selected, row['program_key']) if needs_review else None,
            })
            bucket['evaluated'] = True
    engines = _engines()
    completion = _completion(store, selected)
    return {
        'programs': [programs[key] for key in sorted(programs)],
        'completion': completion,
        'reviewer_available': any(item['role'] == 'review' and item['available'] for item in engines),
        'outcome': completion.get('substate'),
    }


def _next_node(store: Any, selected: Any, program_key: str) -> dict[str, str]:
    if selected.current_contract_version is None or not _table(store, 'goal_queries'):
        return {'kind': 'review', 'label': 'needs human review'}
    row = store.one(
        'SELECT q.raw FROM goal_queries gq JOIN queries q ON q.id=gq.query_id '
        "WHERE gq.goal_id=? AND gq.contract_version=? AND gq.program_key=? AND gq.visibility='public' "
        'ORDER BY gq.query_id DESC LIMIT 1',
        (selected.id, selected.current_contract_version, program_key),
    )
    if row is None or not isinstance(row['raw'], str) or _private_text(row['raw']):
        return {'kind': 'review', 'label': 'needs human review'}
    return {'kind': 'query', 'label': row['raw'][:240]}


def _activity(store: Any, selected: Any) -> dict[str, Any]:
    goal = _sibling('goal')
    inflight = _inflight(store, selected.id)
    runtime = store.one('SELECT substate, reason FROM goal_runtime WHERE goal_id=?', (selected.id,)) if _table(store, 'goal_runtime') else None
    substate = runtime['substate'] if runtime is not None else None
    reason = runtime['reason'] if runtime is not None else None
    if selected.state == 'pausing':
        substate = 'pausing'
        reason = 'in-flight work is still draining' if inflight else 'draining'
    if isinstance(reason, str) and _private_text(reason):
        reason = None
    grant = goal.active_grant(store, selected.id)
    ledger = None
    resets = None
    if grant is not None:
        try:
            ledger = _sibling('ledger').headroom(store, grant.id, int(time.time()))
            resets = _resets_at(int(time.time()))
        except sqlite3.Error:
            raise
        except Exception:
            ledger = None
    version = selected.current_contract_version
    coverage = None
    if version is not None:
        try:
            covered = _sibling('qualify').coverage(store, selected.id, version)
        except _sibling('qualify').QualificationError:
            covered = None
        if covered is not None and covered.programs:
            coverage = {
                'program_count': len(covered.programs),
                'unknown_fields': sum(len(item.public_unknown_fields) for item in covered.programs),
                'contested_fields': sum(len(item.public_contested_fields) for item in covered.programs),
            }
    strategy = None
    if _table(store, 'strategies'):
        row = store.one(
            'SELECT version, outcome, reason FROM strategies WHERE goal_id=? ORDER BY version DESC LIMIT 1',
            (selected.id,),
        )
        if row is not None:
            text = row['reason'] if isinstance(row['reason'], str) and not _private_text(row['reason']) else None
            strategy = {'version': row['version'], 'outcome': row['outcome'], 'reason': text}
    return {
        'state': selected.state,
        'substate': substate,
        'reason': reason,
        'stop_reason': selected.stop_reason,
        'inflight_count': inflight,
        'tasks': _tasks(store, selected.id),
        'strategy': strategy,
        'decisions': _decisions(store, selected.id),
        'coverage': coverage,
        'ledger': ledger,
        'engines': _engines(),
        'grant_state': None if grant is None else grant.state,
        'resets_at': resets,
    }


def _task_node(payload: dict[str, Any]) -> str | None:
    for key in ('node_key', 'canon_url', 'query'):
        value = payload.get(key)
        if not isinstance(value, str) or _private_text(value):
            continue
        lowered = value.lower()
        if lowered.startswith(('javascript:', 'data:', 'vbscript:')):
            continue
        if key == 'canon_url':
            return _http_url(value)
        return value[:200]
    return None


def _tasks(store: Any, goal_id: int) -> list[dict[str, Any]]:
    if not _table(store, 'tasks') or 'goal_id' not in _columns(store, 'tasks'):
        return []
    columns = _columns(store, 'tasks')
    lease = ', lease_until' if 'lease_until' in columns else ''
    rows = store.all(
        f'SELECT id, kind, status, payload{lease} FROM tasks WHERE goal_id=? '
        "AND status IN ('pending','claimed','paused') ORDER BY id",
        (goal_id,),
    )
    states = {'claimed': 'leased', 'pending': 'queued', 'paused': 'paused'}
    result = []
    for row in rows:
        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(row['payload'] or '{}')
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
        domain = payload.get('domain')
        node = _task_node(payload)
        result.append({
            'task_id': row['id'],
            'lease_state': states.get(row['status'], row['status']),
            'phase': row['kind'],
            'domain': domain if isinstance(domain, str) and _HOST_NAME.fullmatch(domain) else None,
            'node_key': node if isinstance(node, str) else None,
            'lease_until': row['lease_until'] if 'lease_until' in columns else None,
        })
    return result


def _decisions(store: Any, goal_id: int) -> list[dict[str, Any]]:
    if not _table(store, 'frontier_decisions'):
        return []
    rows = []
    for row in store.all(
        'SELECT id, at, reason, score, node_kind, node_id, components_json FROM frontier_decisions '
        'WHERE goal_id=? ORDER BY id DESC LIMIT 50',
        (goal_id,),
    ):
        deferred = None
        try:
            components = json.loads(row['components_json'] or '{}')
        except json.JSONDecodeError:
            components = {}
        if isinstance(components, dict):
            for key in ('deferred_until', 'paused_until'):
                if type(components.get(key)) is int:
                    deferred = components[key]
        reason = row['reason'] if isinstance(row['reason'], str) and not _private_text(row['reason']) else None
        rows.append({
            'id': row['id'],
            'at': row['at'],
            'reason': reason,
            'priority': row['score'],
            'node_key': f"{row['node_kind']}:{row['node_id']}",
            'deferred_until': deferred,
        })
    return rows


def _events(store: Any, goal_id: int) -> list[dict[str, Any]]:
    if not _table(store, 'goal_events'):
        return []
    events = []
    for row in store.all(
        'SELECT id, at, actor, operation, from_state, to_state, payload_json FROM goal_events '
        'WHERE goal_id=? ORDER BY id',
        (goal_id,),
    ):
        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(row['payload_json'] or '{}')
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
        reason = payload.get('reason') if isinstance(payload.get('reason'), str) else None
        if isinstance(reason, str) and _private_text(reason):
            reason = None
        version = payload.get('version') if type(payload.get('version')) is int else None
        events.append({
            'id': row['id'],
            'at': row['at'],
            'actor': row['actor'],
            'operation': row['operation'],
            'from_state': row['from_state'],
            'to_state': row['to_state'],
            'reason': reason,
            'version': version,
        })
    return events


def _evidence(store: Any, selected: Any, program_key: str) -> dict[str, Any]:
    program = store.one('SELECT key, name, provenance FROM programs WHERE key=?', (program_key,))
    linked = False
    if selected.current_contract_version is not None and _table(store, 'goal_programs'):
        linked = store.one(
            'SELECT 1 FROM goal_programs WHERE goal_id=? AND contract_version=? AND program_key=?',
            (selected.id, selected.current_contract_version, program_key),
        ) is not None
    observed = store.one(
        'SELECT 1 FROM observations WHERE goal_id=? AND program_key=? LIMIT 1', (selected.id, program_key)
    ) if _table(store, 'observations') else None
    if program is None or (not linked and observed is None):
        raise HTTPError(404, 'not_found', 'Not found.', None)
    observations = []
    review_pending = False
    if _table(store, 'observations'):
        for row in store.all(
            'SELECT o.id, o.field_key, o.quote, o.snapshot_id, o.assurance, o.review_model, o.period, '
            's.retrieved_at, s.visibility, s.provenance, s.source_cycle_label, p.canon_url, p.title '
            'FROM observations o JOIN source_snapshots s ON s.id=o.snapshot_id '
            'LEFT JOIN pages p ON p.id=o.page_id WHERE o.program_key=? AND o.goal_id=? AND o.invalidated_at IS NULL '
            'ORDER BY o.id LIMIT 200',
            (program_key, selected.id),
        ):
            if row['assurance'] in {'review_pending', 'review_unavailable'}:
                review_pending = True
            observations.append({
                'id': row['id'],
                'field_key': row['field_key'],
                'quote': row['quote'][:8000],
                'snapshot_id': row['snapshot_id'],
                'snapshot_at': row['retrieved_at'],
                'assurance': row['assurance'],
                'reviewer_model': row['review_model'],
                'source_url': _http_url(row['canon_url']),
                'source_title': row['title'] if isinstance(row['title'], str) else None,
                'cycle_label': row['source_cycle_label'],
                'period': row['period'],
            })
    contradictions = []
    if _table(store, 'contradictions'):
        for row in store.all(
            'SELECT c.id, c.field_key, c.resolution_reason, c.winner, c.resolved_at, a.quote AS quote_a, b.quote AS quote_b '
            'FROM contradictions c JOIN observations a ON a.id=c.observation_a '
            'JOIN observations b ON b.id=c.observation_b WHERE c.program_key=? ORDER BY c.id',
            (program_key,),
        ):
            contradictions.append({
                'id': row['id'],
                'field_key': row['field_key'],
                'quote_a': (row['quote_a'] or '')[:8000],
                'quote_b': (row['quote_b'] or '')[:8000],
                'resolution': row['resolution_reason'],
                'unresolved': row['winner'] is None or row['resolved_at'] is None,
            })
    leads = []
    if program['provenance'] == 'lead_legacy':
        leads.append({'name': program['name'], 'quote': None, 'source_url': None, 'provenance': 'lead_legacy'})
    notes = []
    if _table(store, 'wiki_regions'):
        for row in store.all(
            "SELECT body, provenance FROM wiki_regions WHERE provenance='human_note' AND path LIKE ? ORDER BY path LIMIT 20",
            (f'%{program_key}%',),
        ):
            notes.append({'text': row['body'][:4000], 'provenance': 'human_note'})
    return {
        'program_key': program_key,
        'review_pending': review_pending,
        'observations': observations,
        'contradictions': contradictions,
        'legacy_leads': leads,
        'human_notes': notes,
    }


def bind_console(host: str, port: int, db_path: Path, allowed_origins: tuple[str, ...], cookie_secure: bool) -> ConsoleServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Port must be between 0 and 65535.')
    return ConsoleServer(host, port, db_path, tuple(allowed_origins), cookie_secure)


def serve_console(host: str, port: int, db_path: Path, allowed_origins: tuple[str, ...], cookie_secure: bool) -> None:
    server = bind_console(host, port, db_path, allowed_origins, cookie_secure)
    bound = server.server_address
    shown = bound[0] if ':' not in str(bound[0]) else f'[{bound[0]}]'
    print(f'Snowball console: http://{shown}:{bound[1]}/')
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('\nShutting down console.')
    finally:
        server.server_close()


def _env_origins() -> tuple[str, ...]:
    raw = os.environ.get('CONSOLE_ALLOWED_ORIGINS', '')
    return tuple(part.strip() for part in raw.split(',') if part.strip())


def _env_secure() -> bool:
    return os.environ.get('CONSOLE_COOKIE_SECURE', '1').strip().lower() not in {'0', 'false', 'no'}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in {'token', 'serve', '-h', '--help'} and any(arg.startswith('--') for arg in argv):
        argv = ['serve', *argv]
    parser = argparse.ArgumentParser(description='Snowball authenticated console')
    commands = parser.add_subparsers(dest='command', required=True)
    token = commands.add_parser('token', help='Print one single-use bootstrap token')
    token.add_argument('--ttl', type=int, default=900)
    token.add_argument('--db', type=Path, default=ROOT / 'data' / 'snowball.db')
    serve = commands.add_parser('serve', help='Serve the console on loopback')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8989)
    serve.add_argument('--db', type=Path, default=ROOT / 'data' / 'snowball.db')
    args = parser.parse_args(argv)
    try:
        if args.command == 'token':
            print(issue_bootstrap_token(args.db, args.ttl))
            return 0
        origins = _env_origins()
        if origins:
            serve_console(args.host, args.port, args.db, origins, _env_secure())
            return 0
        server = bind_console(args.host, args.port, args.db, ('http://127.0.0.1',), _env_secure())
        port = server.server_address[1]
        server.allowed_origins = frozenset(_normalize_origin(item) for item in (
            f'http://127.0.0.1:{port}', f'http://localhost:{port}',
        ))
        shown = server.server_address[0]
        print(f'Snowball console: http://{shown}:{port}/')
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            print('\nShutting down console.')
        finally:
            server.server_close()
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'error: {exc.__class__.__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
