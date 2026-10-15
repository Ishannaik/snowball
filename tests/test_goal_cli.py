"""Goal CLI interview, approval, grant, and structured storage errors."""
import datetime as dt
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _interview():
    spec = importlib.util.spec_from_file_location('snowball_interview', ROOT / 'tools' / 'interview.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(args):
    return subprocess.run([sys.executable, str(ROOT / 'snowball.py'), *args], capture_output=True, text=True, timeout=30)


class GoalInterviewFlowTests(unittest.TestCase):
    def test_interview_approval_grant_and_stop_keep_private_values_out_of_cli_output(self):
        interview = _interview()
        cash = {'amount': '0', 'currency': 'INR'}
        raw = {
            'degree_level': 'masters',
            'modality': 'on_campus',
            'field_tags': ['synthetic-field'],
            'regions': ['synthetic-region'],
            'intake_windows': ['2031-fall'],
            'required_components': {
                'tuition': True, 'mandatory_fees': True, 'living_amount': True, 'living_duration': True,
                'health_insurance': True, 'visa_costs': False, 'application_costs': False, 'travel_costs': False,
                'deposits': False, 'proof_of_funds_liquidity': False,
            },
            'grants_only': True,
            'accept_conditional_ra_ta': False,
            'accept_competitive_awards': False,
            'max_net_total_cost': cash,
            'max_upfront_cash': cash,
            'min_living_adequacy_rule': {
                'basis': 'same_location_period', 'count_housing_once': True,
                'count_insurance_once': True, 'numeric_floor': None,
            },
            'max_work_hours_acceptable': 10,
            'allow_loans': False,
            'citizenship': 'SentinelCitizenship',
            'prior_degree': {'level': 'bachelors', 'field': 'synthetic'},
            'completion_min_candidates': 1,
            'execution_policy': {
                'lifetime_limits': {'queued_nodes': 20, 'visited_nodes': 20, 'fetches': 20, 'queries': 20, 'model_tokens': 40000},
                'max_depth': 2, 'max_queue': 10, 'per_domain_inflight': 1, 'max_hypotheses_per_wave': 2,
                'exploration_share': 0.2, 'usage_mode': 'estimated',
            },
            'monitoring': {'permitted_work': ['recheck_official_sources'], 'max_refreshes': 2, 'interval_days': 14},
            'consent_private_evaluation': True,
        }
        entries = {key: {'value': value, 'confirm': True, 'visibility': interview.question_for(key).visibility_default}
                   for key, value in raw.items()}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'goal.db'
            answers = Path(directory) / 'answers.json'
            limits = Path(directory) / 'limits.json'
            answers.write_text(json.dumps(entries), encoding='utf-8')
            limits.write_text(json.dumps({
                'day': {'fetches': 5, 'queries': 5, 'model_tokens': 10000},
                'lifetime': {'queued_nodes': 20, 'visited_nodes': 20, 'fetches': 20, 'queries': 20, 'model_tokens': 40000},
                'max_depth': 2, 'max_queue': 10, 'per_domain_inflight': 1, 'max_hypotheses_per_wave': 2,
                'exploration_share': '0.2', 'usage_mode': 'estimated',
            }), encoding='utf-8')
            created = _run(['goal', 'new', '--slug', 'cli-flow', '--title', 'CLI flow', '--db', str(database), '--json'])
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertNotIn('SentinelCitizenship', created.stdout)
            interviewed = _run(['goal', 'interview', '--answers-file', str(answers), '--db', str(database), '--json',
                                '--expected-row-version', str(json.loads(created.stdout)['row_version'])])
            self.assertEqual(interviewed.returncode, 0, interviewed.stderr)
            self.assertNotIn('SentinelCitizenship', interviewed.stdout + interviewed.stderr)
            self.assertNotIn('"amount": "0"', interviewed.stdout)
            payload = json.loads(interviewed.stdout)
            proposed = _run(['goal', 'propose', '--from-fields', '--db', str(database), '--json',
                             '--expected-row-version', str(payload['answered'][-1]['row_version'])])
            self.assertEqual(proposed.returncode, 0, proposed.stderr)
            proposal = json.loads(proposed.stdout)
            self.assertNotIn('SentinelCitizenship', proposed.stdout)
            self.assertNotIn('max_upfront_cash', json.dumps(proposal['public_frame']))
            self.assertEqual(proposal['public_frame']['public_criteria']['degree_level'], 'masters')
            self.assertEqual(proposal['public_frame']['public_criteria']['modality'], 'on_campus')
            stale = _run(['goal', 'approve', '--version', str(proposal['version']), '--hash', proposal['body_hash'],
                          '--db', str(database), '--json', '--idempotency-key', 'approve-cli',
                          '--expected-row-version', str(proposal['row_version'] + 9)])
            self.assertEqual(stale.returncode, 3, stale.stdout + stale.stderr)
            self.assertNotIn('SentinelCitizenship', stale.stdout + stale.stderr)
            approved = _run(['goal', 'approve', '--version', str(proposal['version']), '--hash', proposal['body_hash'],
                             '--db', str(database), '--json', '--idempotency-key', 'approve-cli',
                             '--expected-row-version', str(proposal['row_version'])])
            self.assertEqual(approved.returncode, 0, approved.stderr)
            replay = _run(['goal', 'approve', '--version', str(proposal['version']), '--hash', proposal['body_hash'],
                           '--db', str(database), '--json', '--idempotency-key', 'approve-cli',
                           '--expected-row-version', str(proposal['row_version'])])
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertEqual(json.loads(replay.stdout)['row_version'], json.loads(approved.stdout)['row_version'])
            conflict = _run(['goal', 'approve', '--version', str(proposal['version']), '--hash', 'different',
                             '--db', str(database), '--json', '--idempotency-key', 'approve-cli',
                             '--expected-row-version', str(proposal['row_version'])])
            self.assertEqual(conflict.returncode, 3, conflict.stdout + conflict.stderr)
            self.assertNotIn('SentinelCitizenship', conflict.stdout + conflict.stderr)
            expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=14)).strftime('%Y-%m-%d')
            granted = _run(['goal', 'grant', '--contract', str(proposal['version']), '--limits', str(limits),
                            '--expires', expires, '--db', str(database), '--json', '--idempotency-key', 'grant-cli',
                            '--expected-row-version', str(json.loads(approved.stdout)['row_version'])])
            self.assertEqual(granted.returncode, 0, granted.stderr)
            self.assertEqual(json.loads(granted.stdout)['state'], 'active')
            started = _run(['goal', 'start', '--db', str(database), '--json', '--idempotency-key', 'start-cli',
                            '--expected-row-version', str(json.loads(approved.stdout)['row_version'] + 1)])
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(json.loads(started.stdout)['state'], 'running')
            paused = _run(['goal', 'pause', '--reason', 'operator hold', '--db', str(database), '--json',
                           '--idempotency-key', 'pause-cli',
                           '--expected-row-version', str(json.loads(started.stdout)['row_version'])])
            self.assertEqual(paused.returncode, 0, paused.stderr)
            pause_payload = json.loads(paused.stdout)
            self.assertEqual(pause_payload['state'], 'paused')
            self.assertEqual(pause_payload['inflight'], 0)
            stopped = _run(['goal', 'stop', '--reason', ' operator halt for synthetic schedule ', '--db', str(database),
                            '--json', '--idempotency-key', 'stop-cli',
                            '--expected-row-version', str(pause_payload['row_version'])])
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertEqual(json.loads(stopped.stdout)['stop_reason'], 'operator halt for synthetic schedule')
            self.assertEqual(json.loads(stopped.stdout)['state'], 'stopped')

    def test_storage_errors_are_structured_and_do_not_echo_private_text(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'SentinelCitizenship.db'
            database.write_text('SentinelCitizenship', encoding='utf-8')
            shown = _run(['goal', 'show', '--db', str(database), '--json'])
            self.assertNotEqual(shown.returncode, 0)
            combined = shown.stdout + shown.stderr
            self.assertNotIn('SentinelCitizenship', combined)
            payload = json.loads(shown.stdout)
            self.assertEqual(payload['error'], 'storage')
            self.assertNotIn('SentinelCitizenship', payload['message'])



if __name__ == '__main__':
    unittest.main()
