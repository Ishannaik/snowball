"""Real HTTP regressions for the console. Handlers are not mocked."""
from __future__ import annotations

import http.client
import importlib.util
import json
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('snowball_console', ROOT / 'tools' / 'console.py')
console = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = console
spec.loader.exec_module(console)
goal = console._sibling('goal')
orch = console._sibling('orchestrator')

ORIGIN = 'https://snowball.ishannaik.com'
POLICY = {
    'lifetime_limits': {'queued_nodes': 20, 'visited_nodes': 20, 'fetches': 20, 'queries': 20, 'model_tokens': 40000},
    'max_depth': 2, 'max_queue': 10, 'per_domain_inflight': 1, 'max_hypotheses_per_wave': 2,
    'exploration_share': 0.2, 'usage_mode': 'estimated',
}
LIMITS = {
    'day': {'fetches': 5, 'queries': 5, 'model_tokens': 10000},
    'lifetime': dict(POLICY['lifetime_limits']),
    'max_depth': 2, 'max_queue': 10, 'per_domain_inflight': 1, 'max_hypotheses_per_wave': 2,
    'exploration_share': '0.2', 'usage_mode': 'estimated',
}
CANARY = 'UPFRONT_CANARY_99'
SENTINEL = 'SentinelCitizenship'
PRIVATE_NAME = 'PRIVATE_TOPIC_CANARY'
UNKNOWN_NAME = 'UNKNOWN_PROVENANCE_CANARY'
PRIVATE_TITLE = 'PRIVATE_TOPIC_TITLE'
PRIVATE_FOCUS = 'PRIVATE_FOCUS_CANARY'
SECRET_SLUG = 'secret-goal-slug'
COMPONENTS = {
    'tuition': True, 'mandatory_fees': True, 'living_amount': True, 'living_duration': True,
    'health_insurance': True, 'visa_costs': False, 'application_costs': False, 'travel_costs': False,
    'deposits': False, 'proof_of_funds_liquidity': False,
}


class ConsoleApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'console.db'
        store = orch.Store.open(self.db_path)
        store.db.close()
        self.httpd = None
        self.thread = None
        self._start(self.db_path)

    def tearDown(self):
        self._stop()
        self.tmp.cleanup()

    def _start(self, path: Path) -> None:
        self._stop()
        self.httpd = console.bind_console('127.0.0.1', 0, path, (ORIGIN,), True)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True,
        )
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def _stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(5)
            self.httpd = None

    def request(self, method, path, body=None, *, origin=None, cookie=None, csrf=None,
                headers=None, raw=None, content_type='application/json'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        hdrs = dict(headers or {})
        if origin is not False and method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            hdrs.setdefault('Origin', ORIGIN if origin is None else origin)
        if cookie:
            hdrs['Cookie'] = f'session_id={cookie}'
        if csrf:
            hdrs['X-Snowball-CSRF'] = csrf
        data = raw
        if body is not None:
            data = json.dumps(body).encode('utf-8')
        if data is not None and content_type and 'Content-Type' not in hdrs:
            hdrs['Content-Type'] = content_type
        conn.request(method, path, body=data, headers=hdrs)
        response = conn.getresponse()
        payload = response.read()
        status, info = response.status, response.headers
        conn.close()
        return status, info, payload

    def json_body(self, status, info, payload, expected):
        self.assertEqual(status, expected, payload)
        self.assertIn('application/json', info.get('Content-Type', ''))
        return json.loads(payload.decode('utf-8'))

    def login(self, token=None):
        token = token or console.issue_bootstrap_token(self.db_path, 600)
        status, info, payload = self.request('POST', '/api/session', {'token': token})
        body = self.json_body(status, info, payload, 201)
        self.assertNotIn(token, payload.decode('utf-8'))
        cookie = self.session_cookie(info)
        self.assertNotIn(token, cookie)
        self.assertEqual(set(body), {'csrf', 'expires_at'})
        self.assertIsInstance(body['csrf'], str)
        self.assertGreater(body['expires_at'], int(time.time()))
        return cookie, body['csrf'], token

    def session_cookie(self, info):
        raw = info.get('Set-Cookie') or ''
        self.assertIn('HttpOnly', raw)
        self.assertIn('Secure', raw)
        self.assertIn('SameSite=Strict', raw)
        self.assertIn('Path=/', raw)
        marker = 'session_id='
        self.assertIn(marker, raw)
        value = raw.split(marker, 1)[1].split(';', 1)[0]
        self.assertTrue(value)
        return value

    def auth_get(self, path, cookie):
        return self.request('GET', path, cookie=cookie)

    def store(self):
        return orch.Store.connect_existing(self.db_path)

    def answers(self):
        return {
            'degree_level': 'masters',
            'modality': 'on_campus',
            'field_tags': ['synthetic-field'],
            'regions': ['synthetic-region'],
            'intake_windows': ['2031-fall'],
            'required_components': dict(COMPONENTS),
            'grants_only': True,
            'completion_min_candidates': 1,
            'execution_policy': POLICY,
            'monitoring': {'permitted_work': ['recheck_official_sources'], 'max_refreshes': 2, 'interval_days': 14},
            'consent_private_evaluation': True,
            'accept_conditional_ra_ta': False,
            'accept_competitive_awards': False,
            'max_net_total_cost': {'amount': '0', 'currency': 'INR'},
            'max_upfront_cash': {'amount': '0', 'currency': 'INR'},
            'min_living_adequacy_rule': {
                'basis': 'same_location_period', 'count_housing_once': True,
                'count_insurance_once': True, 'numeric_floor': None,
            },
            'max_work_hours_acceptable': 10,
            'allow_loans': False,
            'citizenship': SENTINEL,
            'prior_degree': {'level': 'bachelors', 'field': 'synthetic'},
        }

    def proposed(self, slug='synthetic'):
        store = self.store()
        try:
            interview = goal._sibling('interview')
            selected = goal.create_goal(store, slug, 'Synthetic research control', 'fixture')
            for key, value in self.answers().items():
                question = interview.question_for(key)
                selected = goal.get_goal(store, selected.id)
                goal.record_answer(
                    store, selected.id, key, value, True, question.visibility_default,
                    selected.row_version, 'fixture',
                )
            selected = goal.get_goal(store, selected.id)
            body = interview.build_contract(goal.fields(store, selected.id))
            contract = goal.propose_contract(
                store, selected.id, body, selected.current_contract_version, 'fixture',
                expected_row_version=selected.row_version,
            )
            return goal.get_goal(store, selected.id), contract.body_hash
        finally:
            store.db.close()

    def test_static_csp_has_no_cors_or_inline(self):
        status, info, payload = self.request('GET', '/')
        self.assertEqual(status, 200)
        csp = info.get('Content-Security-Policy', '')
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("style-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertNotIn('unsafe-inline', csp)
        self.assertNotIn('unsafe-eval', csp)
        self.assertEqual(info.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(info.get('Referrer-Policy'), 'no-referrer')
        self.assertIsNone(info.get('Access-Control-Allow-Origin'))
        text = payload.decode('utf-8')
        self.assertIn('<script src="/app.js"></script>', text)
        self.assertIsNone(re.search(r'<script(?![^>]*\bsrc\s*=)', text, re.I))
        status, info, _ = self.request('OPTIONS', '/api/goal')
        self.assertEqual(status, 405)
        self.assertIsNone(info.get('Access-Control-Allow-Origin'))

    def test_session_returns_csrf_and_token_is_one_use(self):
        cookie, csrf, token = self.login()
        status, info, payload = self.auth_get('/api/session', cookie)
        body = self.json_body(status, info, payload, 200)
        self.assertEqual(body, {'authenticated': True, 'expires_at': body['expires_at'], 'csrf': csrf})
        self.assertGreater(body['expires_at'], int(time.time()))
        status, info, payload = self.request('POST', '/api/session', {'token': token})
        body = self.json_body(status, info, payload, 401)
        self.assertEqual(body['error'], 'unauthorized')
        self.assertNotIn(token, payload.decode('utf-8'))
        status, _, payload = self.request('GET', '/api/session')
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(payload)['error'], 'unauthorized')

    def test_concurrent_bootstrap_creates_one_session(self):
        token = console.issue_bootstrap_token(self.db_path, 600)
        barrier = threading.Barrier(2)
        results = []

        def attempt():
            barrier.wait()
            status, _, _payload = self.request('POST', '/api/session', {'token': token})
            results.append(status)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(results), [201, 401])
        store = self.store()
        try:
            self.assertEqual(store.one('SELECT COUNT(*) FROM console_sessions')[0], 1)
            self.assertEqual(store.one('SELECT COUNT(*) FROM console_tokens WHERE consumed_at IS NOT NULL')[0], 1)
        finally:
            store.db.close()

    def test_origin_is_independent_of_host_and_forwarded_headers(self):
        cookie, csrf, _token = self.login()
        status, _, payload = self.request(
            'POST', '/api/goals', {'title': 'Foreign origin'}, cookie=cookie, csrf=csrf,
            origin='https://evil.example', headers={'X-Forwarded-Host': f'127.0.0.1:{self.port}'},
        )
        body = self.json_body(status, _, payload, 403)
        self.assertEqual(body['error'], 'forbidden')
        status, _, payload = self.request(
            'POST', '/api/goals', {'title': 'Missing origin'}, cookie=cookie, csrf=csrf, origin=False,
        )
        self.assertEqual(status, 403)
        status, _, payload = self.request(
            'POST', '/api/goals', {'title': 'Forwarded host is not authority'}, cookie=cookie, csrf=csrf,
            headers={'X-Forwarded-Host': 'https://evil.example', 'X-Forwarded-For': '203.0.113.8'},
        )
        body = self.json_body(status, _, payload, 201)
        self.assertEqual(body['state'], 'draft')
        fresh = console.issue_bootstrap_token(self.db_path, 600)
        status, _, payload = self.request('POST', '/api/session', {'token': fresh}, origin='https://evil.example')
        self.assertEqual(status, 403, payload)
        status, _, payload = self.request('POST', '/api/session', {'token': fresh})
        self.assertEqual(status, 201, payload)

    def test_logout_requires_csrf_and_origin_and_ends_the_session(self):
        cookie, csrf, _token = self.login()
        status, _, _ = self.request('DELETE', '/api/session', {}, cookie=cookie, csrf=None)
        self.assertEqual(status, 403)
        status, _, payload = self.auth_get('/api/session', cookie)
        self.assertEqual(status, 200, payload)
        status, _, _ = self.request('DELETE', '/api/session', {}, cookie=cookie, csrf=csrf, origin='https://evil.example')
        self.assertEqual(status, 403)
        status, info, payload = self.request('DELETE', '/api/session', raw=b'', content_type=False, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 204)
        self.assertEqual(payload, b'')
        self.assertIn('Max-Age=0', info.get('Set-Cookie', ''))
        status, _, _ = self.auth_get('/api/session', cookie)
        self.assertEqual(status, 401)
        cookie, csrf, _token = self.login()
        status, _, payload = self.request('DELETE', '/api/session', {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 204)
        self.assertEqual(payload, b'')

    def test_expired_and_revoked_sessions_are_rejected(self):
        cookie, _csrf, _token = self.login()
        store = self.store()
        try:
            with store.tx() as db:
                db.execute('UPDATE console_sessions SET expires_at=?', (1,))
        finally:
            store.db.close()
        status, _, payload = self.auth_get('/api/goal', cookie)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(payload)['error'], 'unauthorized')
        cookie, _csrf, _token = self.login()
        store = self.store()
        try:
            with store.tx() as db:
                db.execute('UPDATE console_sessions SET revoked_at=?', (int(time.time()),))
        finally:
            store.db.close()
        status, _, _ = self.auth_get('/api/session', cookie)
        self.assertEqual(status, 401)

    def test_framing_host_and_body_limits(self):
        status, data = self.raw(
            f'GET /api/status HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n'
        )
        self.assertEqual(status, 421)
        status, _data = self.raw(
            f'GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nHost: evil.example\r\nConnection: close\r\n\r\n'
        )
        self.assertEqual(status, 400)
        status, _data = self.raw(
            f'POST /api/session HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
            f'Origin: {ORIGIN}\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n'
            f'Connection: close\r\n\r\n{{}}'
        )
        self.assertEqual(status, 400)
        status, _data = self.raw(
            f'POST /api/session HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
            f'Origin: {ORIGIN}\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n'
            f'Connection: close\r\n\r\n{{}}'
        )
        self.assertEqual(status, 400)
        status, info, payload = self.request('POST', '/api/session', raw=b'{' * 65537)
        body = self.json_body(status, info, payload, 413)
        self.assertEqual(body['error'], 'payload_too_large')
        status, _, payload = self.request('POST', '/api/session', raw=b'{')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)['error'], 'invalid_json')
        status, _, payload = self.request('POST', '/api/session', {'token': 'x'}, content_type='text/plain')
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(payload)['error'], 'unsupported_media')

    def raw(self, text: str) -> tuple[int, bytes]:
        sock = socket.create_connection(('127.0.0.1', self.port), 5)
        sock.settimeout(5)
        sock.sendall(text.encode('utf-8'))
        data = b''
        while b'\r\n\r\n' not in data and len(data) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        status = int(data.split(b' ', 2)[1])
        return status, data

    def test_login_and_write_rates_are_bounded(self):
        for _ in range(console.LOGIN_LIMIT):
            status, _, payload = self.request('POST', '/api/session', {'token': 'not-a-token'})
            self.assertEqual(status, 401, payload)
        token = console.issue_bootstrap_token(self.db_path, 600)
        status, _, payload = self.request('POST', '/api/session', {'token': token})
        body = self.json_body(status, _, payload, 429)
        self.assertEqual(body['error'], 'rate_limited')
        self.assertEqual(body['details'], {'retry_after': 60})
        store = self.store()
        try:
            self.assertIsNone(store.one('SELECT consumed_at FROM console_tokens WHERE token_hash=?', (console._hash(token),))[0])
        finally:
            store.db.close()

    def test_write_rate_limit(self):
        cookie, csrf, _token = self.login()
        for _ in range(console.WRITE_LIMIT):
            status, _, payload = self.request('POST', '/api/goals', {}, cookie=cookie, csrf=csrf)
            self.assertEqual(status, 422, payload)
        status, _, payload = self.request('POST', '/api/goals', {'title': 'Too many'}, cookie=cookie, csrf=csrf)
        body = self.json_body(status, _, payload, 429)
        self.assertEqual(body['error'], 'rate_limited')

    def test_new_routes_require_a_session_and_legacy_reads_do_not(self):
        for path in (
            '/api/goal', '/api/goals/1/interview', '/api/goals/1/shortlist', '/api/goals/1/activity',
            '/api/goals/1/evidence/prog-1', '/api/goals/1/events', '/api/goals/1/contract/diff?from=1&to=2',
        ):
            status, _, payload = self.request('GET', path)
            self.assertEqual(status, 401, path)
            self.assertEqual(json.loads(payload)['error'], 'unauthorized')
        status, _, payload = self.request('POST', '/api/goals', {'title': 'No session'})
        self.assertEqual(status, 401)
        status, info, payload = self.request('GET', '/api/status')
        self.assertEqual(status, 200, payload)
        self.assertIsNone(info.get('Access-Control-Allow-Origin'))
        self.assertIn('Content-Security-Policy', info)

    def test_public_reads_omit_private_topics_unknown_provenance_and_goal_data(self):
        self.seed_mixed_records()
        cookie, _csrf, _token = self.login()
        blobs = []
        for path in ('/api/status', '/api/entities', '/api/graph'):
            status, _, payload = self.request('GET', path)
            self.assertEqual(status, 200, payload)
            blobs.append(payload.decode('utf-8'))
        public = '\n'.join(blobs)
        for secret in (CANARY, PRIVATE_NAME, UNKNOWN_NAME, PRIVATE_TITLE, PRIVATE_FOCUS, SECRET_SLUG):
            self.assertNotIn(secret, public)
        status_body = json.loads(blobs[0])
        entities = json.loads(blobs[1])
        graph = json.loads(blobs[2])
        self.assertEqual(status_body['total_entities'], 1)
        self.assertEqual(status_body['total_claims'], 1)
        self.assertFalse(any('goal' in key for key in status_body))
        self.assertEqual([item['name'] for item in entities], ['Public Legacy Grant'])
        names = [node['name'] for node in graph['nodes']]
        self.assertIn('Public Legacy Grant', names)
        self.assertNotIn(PRIVATE_NAME, names)
        self.assertNotIn(PRIVATE_TITLE, names)
        status, _, payload = self.auth_get(f'/api/goals/1/interview', cookie)
        self.assertEqual(status, 200, payload)
        self.assertIn(CANARY, payload.decode('utf-8'))
        for path in ('/api/goals/1/shortlist', '/api/goals/1/activity', '/api/goals/1/events'):
            status, _, payload = self.auth_get(path, cookie)
            self.assertEqual(status, 200, payload)
            self.assertNotIn(CANARY, payload.decode('utf-8'))
            self.assertNotIn(SECRET_SLUG, payload.decode('utf-8'))

    def seed_mixed_records(self):
        store = self.store()
        try:
            now = 1_700_000_000
            with store.tx() as db:
                db.execute("INSERT INTO families(name, recheck_days, focus) VALUES('opportunities', 14, 'public')")
                db.execute("INSERT INTO domains(domain) VALUES('example.edu')")
                db.execute(
                    'INSERT INTO topics(key, family, title, focus, discovered_via, created_at, private) '
                    "VALUES('public-topic','opportunities','Public topic','public focus','seed', ?, 0)",
                    (now,),
                )
                public_topic = db.execute("SELECT id FROM topics WHERE key='public-topic'").fetchone()[0]
                db.execute(
                    'INSERT INTO topics(key, family, title, focus, discovered_via, created_at, private) '
                    "VALUES('private-topic','opportunities', ?, ?, 'seed', ?, 1)",
                    (PRIVATE_TITLE, PRIVATE_FOCUS, now),
                )
                private_topic = db.execute("SELECT id FROM topics WHERE key='private-topic'").fetchone()[0]
                db.execute(
                    'INSERT INTO pages(canon_url, domain, topic_id, title, status, next_check_at, interval_sec, '
                    "discovered_via, created_at) VALUES('https://example.edu/public','example.edu',?,'Public page',"
                    "'fetched', ?, 86400, 'seed', ?)",
                    (public_topic, now, now),
                )
                public_page = db.execute("SELECT id FROM pages WHERE canon_url='https://example.edu/public'").fetchone()[0]
                db.execute(
                    'INSERT INTO pages(canon_url, domain, topic_id, title, status, next_check_at, interval_sec, '
                    "discovered_via, created_at) VALUES('https://example.edu/private','example.edu',?,'Private page',"
                    "'fetched', ?, 86400, 'seed', ?)",
                    (private_topic, now, now),
                )
                private_page = db.execute("SELECT id FROM pages WHERE canon_url='https://example.edu/private'").fetchone()[0]
                db.execute(
                    'INSERT INTO entities(key, name, norm_name, kind, family, evidence_quote, evidence_page_id, '
                    'first_seen_at, last_seen_at, provenance) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('public-grant', 'Public Legacy Grant', 'public legacy grant', 'grant', 'opportunities',
                     'Public quote', public_page, now, now, 'legacy_v3'),
                )
                db.execute(
                    'INSERT INTO entities(key, name, norm_name, kind, family, evidence_quote, evidence_page_id, '
                    'first_seen_at, last_seen_at, provenance) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('private-grant', PRIVATE_NAME, 'private topic canary', 'grant', 'opportunities',
                     PRIVATE_NAME, private_page, now, now, 'legacy_v3'),
                )
                db.execute(
                    'INSERT INTO entities(key, name, norm_name, kind, family, evidence_quote, evidence_page_id, '
                    'first_seen_at, last_seen_at, provenance) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('unknown-grant', UNKNOWN_NAME, 'unknown provenance canary', 'grant', 'opportunities',
                     UNKNOWN_NAME, public_page, now, now, 'unknown'),
                )
                db.execute(
                    'INSERT INTO claims(hash, text, norm_text, n_sources, topic_id, first_seen_at, last_seen_at, provenance) '
                    "VALUES('public-claim','A public historical claim','a public historical claim',1,?,?,?,'legacy_v3')",
                    (public_topic, now, now),
                )
                db.execute(
                    'INSERT INTO claims(hash, text, norm_text, n_sources, topic_id, first_seen_at, last_seen_at, provenance) '
                    "VALUES('private-claim', ?, ?, 2, ?, ?, ?, 'legacy_v3')",
                    (PRIVATE_NAME, PRIVATE_NAME, private_topic, now, now),
                )
                db.execute(
                    'INSERT INTO claims(hash, text, norm_text, n_sources, first_seen_at, last_seen_at, provenance) '
                    "VALUES('unknown-claim', ?, ?, 1, ?, ?, 'unknown')",
                    (UNKNOWN_NAME, UNKNOWN_NAME, now, now),
                )
            goal.create_goal(store, SECRET_SLUG, 'Secret goal', 'fixture')
            selected = goal.get_goal(store)
            with store.tx() as db:
                db.execute(
                    'INSERT INTO profile_fields(goal_id, key, value_json, status, provenance, visibility, row_version, updated_at) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (selected.id, 'max_upfront_cash', goal.canonical({'amount': CANARY, 'currency': 'INR'}),
                     'known', 'user_confirmed', 'private', 0, now),
                )
        finally:
            store.db.close()

    def test_goal_interview_draft_and_second_goal_conflict(self):
        cookie, csrf, _token = self.login()
        status, _, payload = self.request('POST', '/api/goals', {'title': 'First goal', 'slug': 'nope'}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 422, payload)
        status, _, payload = self.request('POST', '/api/goals', {'title': 'First goal'}, cookie=cookie, csrf=csrf)
        created = self.json_body(status, _, payload, 201)
        self.assertEqual(created['state'], 'draft')
        self.assertEqual(created['row_version'], 0)
        status, _, payload = self.request('POST', '/api/goals', {'title': 'Another goal'}, cookie=cookie, csrf=csrf)
        self.assertEqual(json.loads(payload)['error'], 'conflict')
        self.assertEqual(status, 409)
        status, _, payload = self.auth_get(f"/api/goals/{created['id']}/interview", cookie)
        interview = self.json_body(status, _, payload, 200)
        self.assertEqual(interview['row_version'], 0)
        self.assertEqual(interview['next']['field_key'], 'degree_level')
        self.assertTrue(interview['next']['fixed'])
        status, _, payload = self.request(
            'POST', f"/api/goals/{created['id']}/interview/answers",
            {'field_key': 'degree_level', 'value': 'masters', 'confirm': True, 'visibility': 'private',
             'expected_row_version': 0},
            cookie=cookie, csrf=csrf,
        )
        self.assertEqual(status, 422, payload)
        status, _, payload = self.request(
            'POST', f"/api/goals/{created['id']}/interview/answers",
            {'field_key': 'degree_level', 'value': 'masters', 'confirm': True, 'visibility': 'public_ok',
             'expected_row_version': 0},
            cookie=cookie, csrf=csrf,
        )
        answer = self.json_body(status, _, payload, 200)
        self.assertEqual(answer['value'], 'masters')
        self.assertEqual(answer['visibility'], 'public_ok')
        status, _, payload = self.request(
            'POST', f"/api/goals/{created['id']}/contract/draft",
            {'base_version': None, 'from_fields': True}, cookie=cookie, csrf=csrf,
        )
        self.assertEqual(status, 422, payload)
        self.assertEqual(json.loads(payload)['error'], 'validation')

    def test_approval_hash_cas_and_idempotency(self):
        selected, body_hash = self.proposed()
        cookie, csrf, _token = self.login()
        path = f'/api/goals/{selected.id}/contract/approve'
        status, _, payload = self.request(
            'POST', path, {'version': 1, 'body_hash': 'f' * 64, 'expected_row_version': selected.row_version},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'approve-wrong'},
        )
        body = self.json_body(status, _, payload, 409)
        self.assertEqual(body['error'], 'hash_mismatch')
        status, _, payload = self.request(
            'POST', path, {'version': 1, 'body_hash': body_hash, 'expected_row_version': selected.row_version + 9},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'approve-stale'},
        )
        self.assertEqual(json.loads(payload)['error'], 'version_conflict')
        self.assertEqual(status, 409)
        status, _, payload = self.request(
            'POST', path, {'version': 1, 'body_hash': body_hash, 'expected_row_version': selected.row_version},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'approve-ok'},
        )
        approved = self.json_body(status, _, payload, 200)
        self.assertEqual(approved['state'], 'approved')
        status, _, payload = self.request(
            'POST', path, {'version': 1, 'body_hash': body_hash, 'expected_row_version': selected.row_version},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'approve-ok'},
        )
        replay = self.json_body(status, _, payload, 200)
        self.assertEqual(replay['row_version'], approved['row_version'])
        status, _, payload = self.request(
            'POST', path, {'version': 1, 'body_hash': 'a' * 64, 'expected_row_version': selected.row_version},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'approve-ok'},
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)['error'], 'idempotency_conflict')
        store = self.store()
        try:
            self.assertEqual(goal.get_goal(store, selected.id).row_version, approved['row_version'])
            self.assertEqual(goal.get_goal(store, selected.id).state, 'approved')
        finally:
            store.db.close()

    def test_concurrent_approvals_with_separate_connections(self):
        selected, body_hash = self.proposed('racing')
        cookie, csrf, _token = self.login()
        barrier = threading.Barrier(2)
        results = []

        def attempt(key):
            barrier.wait()
            status, _, payload = self.request(
                'POST', f'/api/goals/{selected.id}/contract/approve',
                {'version': 1, 'body_hash': body_hash, 'expected_row_version': selected.row_version},
                cookie=cookie, csrf=csrf, headers={'Idempotency-Key': key},
            )
            results.append((status, json.loads(payload).get('error')))

        threads = [threading.Thread(target=attempt, args=(f'race-{index}',)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        statuses = sorted(status for status, _error in results)
        self.assertEqual(statuses, [200, 409])
        self.assertIn('version_conflict', [error for _status, error in results])
        store = self.store()
        try:
            self.assertEqual(goal.get_goal(store, selected.id).state, 'approved')
        finally:
            store.db.close()

    def test_grant_transition_and_pausing_keeps_inflight_visible(self):
        selected, body_hash = self.proposed('running-goal')
        cookie, csrf, _token = self.login()
        status, _, payload = self.request(
            'POST', f'/api/goals/{selected.id}/contract/approve',
            {'version': 1, 'body_hash': body_hash, 'expected_row_version': selected.row_version},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'run-approve'},
        )
        approved = self.json_body(status, _, payload, 200)
        status, _, payload = self.request(
            'POST', f'/api/goals/{selected.id}/grants',
            {'contract_version': 1, 'mode': 'research', 'limits': LIMITS, 'expires_at': int(time.time()) + 3600,
             'expected_row_version': approved['row_version']},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'run-grant'},
        )
        grant = self.json_body(status, _, payload, 201)
        self.assertEqual(grant['state'], 'active')
        view = self.json_body(*self.auth_get('/api/goal', cookie), 200)
        status, _, payload = self.request(
            'POST', f'/api/goals/{selected.id}/grants',
            {'contract_version': 1, 'mode': 'research', 'limits': {'usage_mode': 'hard'},
             'expires_at': int(time.time()) + 60, 'expected_row_version': view['goal']['row_version']},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'bad-grant'},
        )
        self.assertEqual(status, 422, payload)
        self.assertEqual(json.loads(payload)['error'], 'validation')
        status, _, payload = self.request(
            'POST', f'/api/goals/{selected.id}/transition',
            {'to': 'start', 'reason': 'begin', 'expected_row_version': view['goal']['row_version']},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'run-start'},
        )
        started = self.json_body(status, _, payload, 200)
        self.assertEqual(started['state'], 'running')
        store = self.store()
        try:
            with store.tx() as db:
                db.execute(
                    'INSERT INTO resource_operations(goal_id, grant_id, operation_key, request_hash, binding_json, '
                    "state, created_at) VALUES(?,?, 'inflight', 'hash', '{}', 'dispatched', ?)",
                    (selected.id, grant['id'], int(time.time())),
                )
        finally:
            store.db.close()
        status, _, payload = self.request(
            'POST', f'/api/goals/{selected.id}/transition',
            {'to': 'pause', 'reason': 'hold', 'expected_row_version': started['row_version']},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'run-pause'},
        )
        paused = self.json_body(status, _, payload, 200)
        self.assertEqual(paused['state'], 'pausing')
        view = self.json_body(*self.auth_get('/api/goal', cookie), 200)
        self.assertEqual(view['goal']['state'], 'pausing')
        self.assertGreaterEqual(view['inflight_count'], 1)
        self.assertEqual(view['substate'], 'pausing')
        self.assertNotEqual(view['goal']['state'], 'paused')
        activity = self.json_body(*self.auth_get(f"/api/goals/{selected.id}/activity", cookie), 200)
        self.assertEqual(activity['state'], 'pausing')
        self.assertGreaterEqual(activity['inflight_count'], 1)
        status, _, payload = self.request(
            'POST', f'/api/goals/{selected.id}/transition',
            {'to': 'paused', 'reason': 'too soon', 'expected_row_version': paused['row_version']},
            cookie=cookie, csrf=csrf, headers={'Idempotency-Key': 'run-paused'},
        )
        self.assertEqual(status, 422, payload)
        view = self.json_body(*self.auth_get('/api/goal', cookie), 200)
        self.assertEqual(view['goal']['state'], 'pausing')

    def test_diff_events_and_evidence_shapes_keep_private_bytes_authenticated(self):
        selected, _body_hash = self.proposed('diff-goal')
        cookie, _csrf, _token = self.login()
        status, _, payload = self.request('GET', f'/api/goals/{selected.id}/contract/diff?to=1')
        self.assertEqual(status, 401)
        status, _, payload = self.auth_get(f'/api/goals/{selected.id}/contract/diff?to=1', cookie)
        diff = self.json_body(status, _, payload, 200)
        self.assertEqual(diff['to'], 1)
        self.assertIsNone(diff['from'])
        self.assertTrue(any(
            change['path'] == 'citizenship' and change['after'] == SENTINEL and change['visibility'] == 'private'
            for change in diff['changes']
        ))
        store = self.store()
        try:
            digest = 'ab' * 32
            with store.tx() as db:
                db.execute(
                    'INSERT INTO goal_events(goal_id, at, actor, kind, operation, from_state, to_state, payload_json) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (selected.id, int(time.time()), 'operator', 'note', 'approve', 'contract_proposed', 'approved',
                     json.dumps({'reason': 'reviewed', 'version': 4, 'value': CANARY, 'profile_snapshot_hash': digest})),
                )
                self.insert_evidence(db, selected.id)
        finally:
            store.db.close()
        events = self.json_body(*self.auth_get(f'/api/goals/{selected.id}/events', cookie), 200)
        encoded = json.dumps(events)
        self.assertNotIn(CANARY, encoded)
        self.assertNotIn(SENTINEL, encoded)
        self.assertNotIn(digest, encoded)
        matched = [item for item in events['events'] if item['version'] == 4]
        self.assertEqual(matched[0]['reason'], 'reviewed')
        self.assertNotIn('value', matched[0])
        evidence = self.json_body(*self.auth_get(f'/api/goals/{selected.id}/evidence/prog-1', cookie), 200)
        self.assertEqual(evidence['observations'][0]['quote'], '<script>alert(1)</script>')
        self.assertIsNone(evidence['observations'][0]['source_url'])
        self.assertEqual(evidence['observations'][0]['assurance'], 'review_pending')
        self.assertTrue(evidence['review_pending'])
        self.assertEqual(evidence['human_notes'], [{'text': 'A labelled note.', 'provenance': 'human_note'}])
        status, _, payload = self.auth_get(f'/api/goals/{selected.id}/evidence/missing', cookie)
        self.assertEqual(status, 404)
        status, _, payload = self.request('GET', '/api/status')
        self.assertNotIn(b'<script>alert(1)</script>', payload)
        self.assertNotIn(CANARY.encode(), payload)

    def insert_evidence(self, db, goal_id: int) -> None:
        now = int(time.time())
        db.execute("INSERT INTO domains(domain) VALUES('scripts.example')")
        db.execute(
            'INSERT INTO pages(canon_url, domain, title, status, next_check_at, interval_sec, discovered_via, created_at) '
            "VALUES('javascript:alert(1)','scripts.example','Script page','fetched', ?, 86400, 'seed', ?)",
            (now, now),
        )
        page_id = db.execute("SELECT id FROM pages WHERE domain='scripts.example'").fetchone()[0]
        db.execute(
            'INSERT INTO programs(key, name, field_tags_json, provenance, first_seen_at, last_seen_at) '
            "VALUES('prog-1','Program','[]','observed', ?, ?)",
            (now, now),
        )
        db.execute(
            'INSERT INTO source_snapshots(page_id, content_hash, retrieved_at, text_path, visibility, provenance) '
            "VALUES(?, 'abc', ?, 'mem.txt', 'public', 'public_fetch')",
            (page_id, now),
        )
        snapshot_id = db.execute('SELECT id FROM source_snapshots').fetchone()[0]
        db.execute(
            'INSERT INTO observations(snapshot_id, page_id, program_key, field_key, value_json, subject_json, quote, '
            'quote_start, quote_end, extractor_model, prompt_version, assurance, goal_id, created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (snapshot_id, page_id, 'prog-1', 'tuition', '{}', '{}', '<script>alert(1)</script>',
             0, 24, 'none', 'v1', 'review_pending', goal_id, now),
        )
        db.execute(
            "INSERT INTO wiki_regions(path, region_key, provenance, body) VALUES('prog-1.md','note','human_note','A labelled note.')"
        )

    def test_unknown_schema_is_unavailable_and_does_not_migrate(self):
        self._stop()
        path = Path(self.tmp.name) / 'old.db'
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute("INSERT INTO schema_meta VALUES('schema_version','3')")
        db.commit()
        db.close()
        self._start(path)
        status, info, payload = self.request('GET', '/api/status')
        body = self.json_body(status, info, payload, 503)
        self.assertEqual(body['error'], 'unavailable')
        self.assertEqual(body['details'], {'schema': 'unsupported'})
        self.assertNotIn('migration', json.dumps(body))
        check = sqlite3.connect(path)
        try:
            self.assertEqual(check.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], '3')
            self.assertIsNone(check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'").fetchone())
        finally:
            check.close()

    def test_missing_database_is_not_created(self):
        missing = Path(self.tmp.name) / 'missing.db'
        self._start(missing)
        status, _, payload = self.request('GET', '/api/goal')
        body = self.json_body(status, _, payload, 503)
        self.assertEqual(body['error'], 'unavailable')
        self.assertFalse(missing.exists())

    def test_non_loopback_bind_is_refused(self):
        with self.assertRaises(ValueError):
            console.serve_console('0.0.0.0', 0, self.db_path, (ORIGIN,), True)
        with self.assertRaises(ValueError):
            console.bind_console('10.0.0.8', 0, self.db_path, (ORIGIN,), True)


if __name__ == '__main__':
    unittest.main()
