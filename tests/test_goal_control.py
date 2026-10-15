"""Goal authorization, immutable approvals and epoch fences against the actual v4 Store."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('goal_control_loader', ROOT / 'tools' / 'goal.py')
loader = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = loader
spec.loader.exec_module(loader)
goal, orch = loader._sibling('goal'), loader._sibling('orchestrator')

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
COMPONENTS = {
    'tuition': True, 'mandatory_fees': True, 'living_amount': True, 'living_duration': True,
    'health_insurance': True, 'visa_costs': False, 'application_costs': False, 'travel_costs': False,
    'deposits': False, 'proof_of_funds_liquidity': False,
}
SENTINEL = 'SentinelCitizenship'


def answers(cash='0'):
    return {
        'degree_level': 'masters',
        'modality': 'on_campus',
        'field_tags': ['synthetic-field'],
        'regions': ['synthetic-region'],
        'intake_windows': ['2031-fall'],
        'required_components': dict(COMPONENTS),
        'grants_only': True,
        'accept_conditional_ra_ta': False,
        'accept_competitive_awards': False,
        'max_net_total_cost': {'amount': cash, 'currency': 'INR'},
        'max_upfront_cash': {'amount': cash, 'currency': 'INR'},
        'min_living_adequacy_rule': {
            'basis': 'same_location_period', 'count_housing_once': True,
            'count_insurance_once': True, 'numeric_floor': None,
        },
        'max_work_hours_acceptable': 10,
        'allow_loans': False,
        'citizenship': SENTINEL,
        'prior_degree': {'level': 'bachelors', 'field': 'synthetic'},
        'completion_min_candidates': 1,
        'execution_policy': POLICY,
        'monitoring': {'permitted_work': ['recheck_official_sources'], 'max_refreshes': 2, 'interval_days': 14},
        'consent_private_evaluation': True,
    }


class GoalControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = orch.Store.open(Path(self.temp.name) / 'control.db')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def answer_all(self, selected, raw=None):
        interview = goal._sibling('interview')
        for key, value in (raw or answers()).items():
            question = interview.question_for(key)
            selected = goal.get_goal(self.store, selected.id)
            goal.record_answer(self.store, selected.id, key, value, True, question.visibility_default,
                               selected.row_version, 'fixture')
        return goal.get_goal(self.store, selected.id)

    def propose(self, selected):
        interview = goal._sibling('interview')
        selected = goal.get_goal(self.store, selected.id)
        body = interview.build_contract(goal.fields(self.store, selected.id))
        contract = goal.propose_contract(self.store, selected.id, body, selected.current_contract_version, 'fixture',
                                         expected_row_version=selected.row_version)
        return goal.get_goal(self.store, selected.id), contract

    def approved(self, slug='synthetic', cash='0'):
        selected = goal.create_goal(self.store, slug, 'Synthetic research control', 'fixture')
        selected = self.answer_all(selected, answers(cash))
        selected, contract = self.propose(selected)
        approved = goal.approve_contract(self.store, selected.id, contract.version, contract.body_hash,
                                         selected.row_version, 'fixture', slug + '-approve')
        return approved, contract

    def granted(self, selected):
        grant = goal.grant_execution(self.store, selected.id, selected.current_contract_version, 'research', LIMITS,
                                     int(time.time()) + 3600, selected.row_version, 'fixture', selected.slug + '-grant')
        return goal.get_goal(self.store, selected.id), grant

    def running(self, slug='synthetic', cash='0'):
        selected, _contract = self.approved(slug, cash)
        selected, grant = self.granted(selected)
        selected = goal.transition(self.store, selected.id, 'start', '', selected.row_version, 'fixture', slug + '-start')
        return selected, grant

    def frame_text(self, goal_id, version):
        row = self.store.one('SELECT frame_json,frame_hash,body_hash,profile_snapshot_json FROM goal_contracts '
                             'WHERE goal_id=? AND version=?', (goal_id, version))
        return row['frame_json'], row['frame_hash'], row['body_hash'], row['profile_snapshot_json']

    def test_approve_checks_hash_and_cas_then_replays_before_cas_without_changing_snapshot(self):
        selected = goal.create_goal(self.store, 'synthetic', 'Synthetic research control', 'fixture')
        selected = self.answer_all(selected)
        selected, contract = self.propose(selected)
        reviewed_hash = contract.body_hash
        with self.assertRaises(goal.ValidationError):
            goal.approve_contract(self.store, selected.id, contract.version, 'wrong', selected.row_version, 'fixture', 'wrong-hash')
        with self.assertRaises(goal.VersionConflict):
            goal.approve_contract(self.store, selected.id, contract.version, reviewed_hash, selected.row_version + 1, 'fixture', 'wrong-version')
        approved = goal.approve_contract(self.store, selected.id, contract.version, reviewed_hash, selected.row_version, 'fixture', 'approve')
        replay = goal.approve_contract(self.store, selected.id, contract.version, reviewed_hash, selected.row_version, 'fixture', 'approve')
        self.assertEqual(replay, approved)
        self.assertEqual(goal.get_goal(self.store, selected.id).row_version, approved.row_version)
        with self.assertRaises(goal.IdempotencyConflict):
            goal.approve_contract(self.store, selected.id, contract.version, 'different', selected.row_version, 'fixture', 'approve')
        locked = goal.load_contract(self.store, selected.id, contract.version)
        self.assertEqual(locked.body_hash, reviewed_hash)
        self.assertEqual(locked.profile_snapshot['citizenship']['value'], SENTINEL)
        self.assertEqual(locked.profile_snapshot['max_upfront_cash']['value'], {'amount': '0', 'currency': 'INR'})
        self.assertNotIn(SENTINEL, locked.frame_json if False else json.dumps(locked.frame))
        self.assertNotIn('max_upfront_cash', json.dumps(locked.frame))
        self.assertNotIn(locked.profile_snapshot_hash, json.dumps(locked.frame))
        self.assertEqual(locked.frame['public_criteria']['degree_level'], 'masters')
        self.assertEqual(locked.frame['public_criteria']['modality'], 'on_campus')
        self.assertIsNone(goal.active_grant(self.store, selected.id))
        self.assertEqual(approved.state, 'approved')

    def test_persisted_frame_or_snapshot_tamper_is_rejected(self):
        selected = goal.create_goal(self.store, 'tamper', 'Synthetic research control', 'fixture')
        selected = self.answer_all(selected)
        selected, contract = self.propose(selected)
        swapped = {'domain': 'masters_funding', 'public_objective': 'replaced public objective'}
        with self.store.tx() as db:
            db.execute('UPDATE goal_contracts SET frame_json=?, frame_hash=? WHERE goal_id=? AND version=?',
                       (goal.canonical(swapped), goal.digest(swapped), selected.id, contract.version))
        with self.assertRaises(goal.ValidationError) as raised:
            goal.approve_contract(self.store, selected.id, contract.version, contract.body_hash, selected.row_version, 'fixture', 'tampered-frame')
        self.assertEqual(raised.exception.field, 'frame_hash')
        self.assertEqual(goal.get_goal(self.store, selected.id).state, 'contract_proposed')
        with self.store.tx() as db:
            db.execute('UPDATE goal_contracts SET frame_json=?, frame_hash=? WHERE goal_id=? AND version=?',
                       (goal.canonical(contract.frame), contract.frame_hash, selected.id, contract.version))
            snapshot = json.loads(db.execute('SELECT profile_snapshot_json FROM goal_contracts WHERE goal_id=? AND version=?',
                                             (selected.id, contract.version)).fetchone()[0])
            snapshot['max_upfront_cash']['value'] = {'amount': '500', 'currency': 'INR'}
            db.execute('UPDATE goal_contracts SET profile_snapshot_json=? WHERE goal_id=? AND version=?',
                       (goal.canonical(snapshot), selected.id, contract.version))
        with self.assertRaises(goal.ValidationError) as raised:
            goal.approve_contract(self.store, selected.id, contract.version, contract.body_hash, selected.row_version, 'fixture', 'tampered-snapshot')
        self.assertEqual(raised.exception.field, 'body_hash')
        self.assertEqual(goal.get_goal(self.store, selected.id).state, 'contract_proposed')

    def test_draft_edit_does_not_change_approved_frame_and_revision_does_not_resume(self):
        selected, grant = self.running(cash='0')
        del grant
        original = self.frame_text(selected.id, 1)
        self.assertNotIn(SENTINEL, original[0])
        selected = goal.transition(self.store, selected.id, 'revise', 'edit thresholds', selected.row_version, 'fixture', 'revise')
        self.assertEqual(selected.state, 'revising')
        self.assertNotEqual(selected.state, 'running')
        question = goal._sibling('interview').question_for('grants_only')
        selected = goal.get_goal(self.store, selected.id)
        goal.record_answer(self.store, selected.id, 'grants_only', True, True, 'private', selected.row_version, 'fixture')
        self.assertEqual(self.frame_text(selected.id, 1), original)
        selected = goal.get_goal(self.store, selected.id)
        goal.record_answer(self.store, selected.id, 'grants_only', True, True, question.visibility_default, selected.row_version, 'fixture')
        selected = goal.get_goal(self.store, selected.id)
        goal.record_answer(self.store, selected.id, 'max_upfront_cash', {'amount': '500', 'currency': 'INR'}, True,
                           'private', selected.row_version, 'fixture')
        self.assertEqual(self.frame_text(selected.id, 1)[0], original[0])
        cancelled = goal.transition(self.store, goal.get_goal(self.store, selected.id).id, 'cancel_revision', 'discard',
                                    goal.get_goal(self.store, selected.id).row_version, 'fixture', 'cancel-running')
        self.assertEqual(cancelled.state, 'paused')
        self.assertNotIn(cancelled.state, ('running', 'monitoring'))
        self.assertEqual(self.frame_text(selected.id, 1)[0], original[0])
        self.assertEqual(json.loads(self.frame_text(selected.id, 1)[3])['max_upfront_cash']['value']['amount'], '0')
        self.assertIsNone(goal.runnable_goal(self.store, int(time.time())))
        selected = goal.transition(self.store, cancelled.id, 'revise', 'edit after pause', cancelled.row_version, 'fixture', 'revise-paused')
        selected = goal.get_goal(self.store, selected.id)
        goal.record_answer(self.store, selected.id, 'max_upfront_cash', {'amount': '500', 'currency': 'INR'}, True,
                           'private', selected.row_version, 'fixture')
        selected, proposed = self.propose(goal.get_goal(self.store, selected.id))
        self.assertEqual(self.frame_text(selected.id, 1)[0], original[0])
        self.assertNotIn('500', json.dumps(proposed.frame))
        self.assertNotIn(SENTINEL, json.dumps(proposed.frame))
        approved = goal.approve_contract(self.store, selected.id, proposed.version, proposed.body_hash,
                                         selected.row_version, 'fixture', 'approve-v2')
        self.assertEqual(approved.state, 'approved')
        self.assertNotEqual(approved.state, 'running')
        self.assertEqual(self.frame_text(selected.id, 1)[0], original[0])
        self.assertEqual(goal.runnable_reason(self.store, int(time.time())), 'not_running')

    def test_grant_share_is_a_decimal_string_the_ledger_can_reserve(self):
        selected, _contract = self.approved()
        floated = dict(LIMITS)
        floated['exploration_share'] = 0.2
        with self.assertRaises(goal.ValidationError) as raised:
            goal.grant_execution(self.store, selected.id, selected.current_contract_version, 'research', floated,
                                 int(time.time()) + 3600, selected.row_version, 'fixture', 'float-share')
        self.assertEqual(raised.exception.field, 'exploration_share')
        self.assertNotIn('0.2', str(raised.exception))
        wider = dict(LIMITS)
        wider['exploration_share'] = '0.3'
        with self.assertRaises(goal.ValidationError) as raised:
            goal.grant_execution(self.store, selected.id, selected.current_contract_version, 'research', wider,
                                 int(time.time()) + 3600, selected.row_version, 'fixture', 'wide-share')
        self.assertEqual(raised.exception.field, 'exploration_share')
        self.assertIsNone(goal.active_grant(self.store, selected.id))
        granted = goal.grant_execution(self.store, selected.id, selected.current_contract_version, 'research', LIMITS,
                                       int(time.time()) + 3600, selected.row_version, 'fixture', 'string-share')
        self.assertEqual(granted.limits['exploration_share'], '0.2')
        stored = self.store.one('SELECT limits_json FROM execution_grants WHERE id=?', (granted.id,))[0]
        self.assertIsInstance(json.loads(stored)['exploration_share'], str)
        parsed = goal._sibling('ledger')._parse_limits(stored)
        self.assertEqual(parsed['exploration_share'], '0.2')
        self.assertEqual(goal.get_goal(self.store, selected.id).state, 'approved')

    def test_grant_does_not_start_and_start_requires_a_live_matching_grant(self):
        selected, _contract = self.approved()
        with self.assertRaises(goal.ValidationError):
            goal.transition(self.store, selected.id, 'start', '', selected.row_version, 'fixture', 'premature-start')
        selected, grant = self.granted(selected)
        self.assertEqual(selected.state, 'approved')
        self.assertEqual(grant.contract_version, selected.current_contract_version)
        self.assertEqual(self.store.one('SELECT COUNT(*) FROM tasks')[0], 0)
        with self.store.tx() as db:
            db.execute('UPDATE execution_grants SET expires_at=? WHERE id=?', (int(time.time()) - 1, grant.id))
        with self.assertRaises(goal.ValidationError):
            goal.transition(self.store, selected.id, 'start', '', selected.row_version, 'fixture', 'expired-start')
        self.assertEqual(goal.get_goal(self.store, selected.id).state, 'approved')

    def test_expired_exhausted_and_mode_changed_grants_are_not_runnable(self):
        selected, grant = self.running('gates')
        now = int(time.time())
        version = selected.row_version
        with self.store.tx() as db:
            db.execute('UPDATE execution_grants SET expires_at=? WHERE id=?', (now - 10, grant.id))
        self.assertEqual(goal.runnable_reason(self.store, now), 'grant_expired')
        self.assertEqual(self.store.one('SELECT state FROM execution_grants WHERE id=?', (grant.id,))[0], 'active')
        self.assertEqual(goal.get_goal(self.store, selected.id).row_version, version)
        with self.store.tx() as db:
            db.execute('UPDATE execution_grants SET expires_at=? WHERE id=?', (now + 3600, grant.id))
        day = goal._sibling('ledger').ist_day(now)
        scope = f'goal:{selected.id}'
        with self.store.tx() as db:
            db.execute("INSERT INTO resource_ledger(scope_key,window_kind,window_key,counter,reserved,settled,lost) "
                       "VALUES(?,'day',?,'fetches',0,5,0)", (scope, day))
        self.assertEqual(goal.runnable_reason(self.store, now), 'grant_exhausted')
        self.assertEqual(self.store.one('SELECT state FROM execution_grants WHERE id=?', (grant.id,))[0], 'active')
        self.assertEqual(goal.get_goal(self.store, selected.id).row_version, version)
        with self.store.tx() as db:
            db.execute('DELETE FROM resource_ledger WHERE scope_key=? AND window_kind=\'day\'', (scope,))
            db.execute("UPDATE execution_grants SET state='exhausted' WHERE id=?", (grant.id,))
        self.assertEqual(goal.runnable_reason(self.store, now), 'grant_exhausted')
        with self.store.tx() as db:
            db.execute("UPDATE execution_grants SET state='active' WHERE id=?", (grant.id,))
        current = goal.get_goal(self.store, selected.id)
        replacement = goal.grant_execution(self.store, current.id, current.current_contract_version, 'monitoring', LIMITS,
                                           now + 3600, current.row_version, 'fixture', 'mode-change')
        self.assertEqual(replacement.mode, 'monitoring')
        self.assertEqual(goal.get_goal(self.store, selected.id).state, 'running')
        self.assertNotEqual(goal.get_goal(self.store, selected.id).state, 'monitoring')
        self.assertEqual(goal.runnable_reason(self.store, int(time.time())), 'mode_mismatch')
        paused = goal.transition(self.store, current.id, 'pause', 'mode hold', goal.get_goal(self.store, current.id).row_version,
                                 'fixture', 'pause-mode')
        paused = goal.finish_pause(self.store, paused.id)
        self.assertEqual(paused.state, 'paused')
        with self.assertRaises(goal.ValidationError):
            goal.transition(self.store, paused.id, 'running', '', paused.row_version, 'fixture', 'resume-research')
        self.assertEqual(goal.get_goal(self.store, paused.id).state, 'paused')

    def test_replan_preserves_admitted_strategy_but_pause_fences_and_releases_undispatched(self):
        selected, grant = self.running('replan')
        original_strategy = selected.current_strategy_version
        original_epoch = selected.epoch
        selected = goal.transition(self.store, selected.id, 'replan', 'Synthetic replan', selected.row_version, 'fixture', 'replan')
        self.assertTrue(goal.binding_valid(self.store.db, selected.id, selected.current_contract_version, original_strategy, original_epoch))
        self.assertTrue(goal.binding_valid(self.store.db, selected.id, selected.current_contract_version, selected.current_strategy_version, original_epoch))
        self.assertFalse(goal.binding_valid(self.store.db, selected.id, selected.current_contract_version, 99, original_epoch))
        self._seed_work(selected, grant, reserved_amount=1, dispatched_amount=4, settled=3, lost=2)
        selected = goal.transition(self.store, selected.id, 'pause', 'Synthetic pause', selected.row_version, 'fixture', 'pause')
        self.assertEqual(selected.state, 'pausing')
        self.assertFalse(goal.binding_valid(self.store.db, selected.id, 1, original_strategy, original_epoch))
        self.assertEqual(goal.inflight_count(self.store, selected.id), 1)
        self.assertEqual(self.store.one('SELECT reserved,settled,lost FROM resource_ledger')[0:3], (4, 3, 2))
        self.assertEqual(self.store.one("SELECT state FROM resource_operations WHERE operation_key='reserved-op'")[0], 'cancelled')
        self.assertEqual(self.store.one("SELECT state FROM resource_operations WHERE operation_key='dispatched-op'")[0], 'dispatched')
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM tasks WHERE status='failed' AND lease_token IS NULL AND operation_key IS NULL")[0], 1)
        dispatched_task = self.store.one("SELECT status,lease_token FROM tasks WHERE operation_key='dispatched-op'")
        self.assertEqual(dispatched_task['status'], 'claimed')
        self.assertEqual(dispatched_task['lease_token'], 'dispatched-op-lease')
        self.assertIsNone(self.store.one("SELECT id FROM domain_slots WHERE lease_token='reserved-op-lease'"))
        self.assertIsNotNone(self.store.one("SELECT id FROM domain_slots WHERE lease_token='dispatched-op-lease'"))
        self.assertEqual(json.loads(self.store.one("SELECT payload_json FROM goal_events WHERE operation='transition' "
                                                   "ORDER BY id DESC")[0])['inflight'], 1)
        held = goal.finish_pause(self.store, selected.id)
        self.assertEqual(held.state, 'pausing')
        with self.store.tx() as db:
            db.execute("UPDATE resource_operations SET state='settled' WHERE operation_key='dispatched-op'")
        paused = goal.finish_pause(self.store, selected.id)
        self.assertEqual(paused.state, 'paused')
        self.assertEqual(goal.inflight_count(self.store, selected.id), 0)

    def test_replacing_grant_releases_reservations_and_fences_old_callbacks_without_refilling_usage(self):
        selected, original = self.running('replace')
        self._seed_work(selected, original, reserved_amount=1, dispatched_amount=4, settled=3, lost=2)
        replacement = goal.grant_execution(self.store, selected.id, selected.current_contract_version, 'research', LIMITS,
                                            int(time.time()) + 3600, selected.row_version, 'fixture', 'replacement-grant')
        current = goal.get_goal(self.store, selected.id)
        self.assertEqual(current.state, 'running')
        self.assertEqual(replacement.state, 'active')
        self.assertGreater(current.epoch, selected.epoch)
        self.assertFalse(goal.binding_valid(self.store.db, selected.id, selected.current_contract_version, selected.current_strategy_version, selected.epoch))
        self.assertEqual(self.store.one("SELECT state FROM resource_operations WHERE operation_key='reserved-op'")[0], 'cancelled')
        self.assertEqual(self.store.one("SELECT state FROM resource_operations WHERE operation_key='dispatched-op'")[0], 'dispatched')
        self.assertEqual(self.store.one('SELECT reserved,settled,lost FROM resource_ledger')[0:3], (4, 3, 2))
        self.assertEqual(self.store.one('SELECT state FROM execution_grants WHERE id=?', (original.id,))[0], 'revoked')
        self.assertIsNone(self.store.one('SELECT stale_for_goal FROM constraint_evaluations'))

    def test_pause_and_renewal_keep_current_evaluations_until_a_new_contract_is_approved(self):
        selected, grant = self.running('ready', cash='0')
        self._seed_readiness(selected)
        qualify = goal._sibling('qualify')
        contract = goal.load_contract(self.store, selected.id, selected.current_contract_version)
        self.assertEqual(contract.body['completion']['min_candidates'], 1)
        self.assertNotIn('enabled', contract.body['monitoring'])
        ready = qualify.completion_status(self.store, selected)
        self.assertTrue(ready.ready)
        self.assertEqual(ready.substate, 'research_ready')
        self.assertEqual(ready.detail['min_candidates'], contract.body['completion']['min_candidates'])
        self.assertFalse(qualify.coverage(self.store, selected.id, selected.current_contract_version).programs[0].stale)
        observation = self.store.one('SELECT invalidated_at FROM observations')
        self.assertIsNone(observation[0])
        paused = goal.transition(self.store, selected.id, 'pause', 'hold', selected.row_version, 'fixture', 'pause-ready')
        paused = goal.finish_pause(self.store, paused.id)
        self.assertEqual(paused.state, 'paused')
        self.assertEqual(self.store.one('SELECT stale_for_goal,status FROM constraint_evaluations')[0:2], (0, 'met'))
        self.assertIsNone(self.store.one('SELECT invalidated_at FROM observations')[0])
        after_pause = qualify.completion_status(self.store, paused)
        self.assertFalse(after_pause.ready)
        self.assertEqual(after_pause.detail['reason'], 'no_current_evaluations')
        self.assertNotEqual(after_pause.detail['reason'], 'stale_evaluations')
        self.assertFalse(qualify.coverage(self.store, paused.id, 1).programs[0].stale)
        renewed = goal.grant_execution(self.store, paused.id, paused.current_contract_version, 'research', LIMITS,
                                       int(time.time()) + 3600, paused.row_version, 'fixture', 'renew')
        self.assertEqual(renewed.state, 'active')
        self.assertEqual(self.store.one('SELECT stale_for_goal FROM constraint_evaluations')[0], 0)
        self.assertIsNone(self.store.one('SELECT invalidated_at FROM observations')[0])
        revising = goal.transition(self.store, paused.id, 'revise', 'new version', goal.get_goal(self.store, paused.id).row_version,
                                   'fixture', 'revise-ready')
        revising = self.answer_all(revising, answers('500'))
        revising, proposed = self.propose(revising)
        approved = goal.approve_contract(self.store, revising.id, proposed.version, proposed.body_hash,
                                         revising.row_version, 'fixture', 'approve-ready-v2')
        self.assertEqual(approved.current_contract_version, 2)
        self.assertEqual(self.store.one('SELECT stale_for_goal FROM constraint_evaluations WHERE contract_version=1')[0], 1)
        self.assertIsNone(self.store.one('SELECT invalidated_at FROM observations')[0])
        self.assertTrue(qualify.coverage(self.store, approved.id, 1).programs[0].stale)
        self.assertEqual(qualify.completion_status(self.store, approved).detail['reason'], 'no_current_evaluations')

    def test_refresh_policy_permits_a_monitoring_grant_without_starting_monitoring(self):
        selected, contract = self.approved('monitor-policy')
        policy = contract.body['monitoring']['refresh_policy']
        self.assertEqual(set(policy), {'permitted_work', 'max_refreshes', 'interval_days'})
        self.assertEqual(policy['permitted_work'], ['recheck_official_sources'])
        self.assertEqual(contract.body['completion']['min_candidates'], 1)
        self.assertNotIn('enabled', contract.body['monitoring'])
        self.assertEqual(selected.state, 'approved')
        selected, research = self.granted(selected)
        self.assertEqual(selected.state, 'approved')
        self.assertEqual(research.mode, 'research')

        def stored_body():
            return json.loads(self.store.one('SELECT body_json FROM goal_contracts WHERE goal_id=? AND version=?',
                                             (selected.id, selected.current_contract_version))[0])

        def replace_policy(monitoring):
            body = stored_body()
            body['monitoring'] = monitoring
            with self.store.tx() as db:
                db.execute('UPDATE goal_contracts SET body_json=? WHERE goal_id=? AND version=?',
                           (goal.canonical(body), selected.id, selected.current_contract_version))

        current = goal.get_goal(self.store, selected.id)
        refused = (
            {'refresh_policy': None},
            {'enabled': True},
            {'refresh_policy': {'permitted_work': [], 'max_refreshes': 1, 'interval_days': 14}},
            {'enabled': True, 'refresh_policy': policy},
        )
        for index, monitoring in enumerate(refused):
            replace_policy(monitoring)
            with self.assertRaises(goal.ValidationError) as raised:
                goal.grant_execution(self.store, current.id, current.current_contract_version, 'monitoring', LIMITS,
                                     int(time.time()) + 3600, current.row_version, 'fixture', f'refused-{index}')
            self.assertEqual(raised.exception.field, 'mode')
            self.assertEqual(goal.get_goal(self.store, current.id).state, 'approved')
            self.assertEqual(self.store.one('SELECT id,state,mode FROM execution_grants WHERE state=\'active\'')[0:3],
                             (research.id, 'active', 'research'))
        replace_policy({'refresh_policy': policy})
        granted = goal.grant_execution(self.store, current.id, current.current_contract_version, 'monitoring', LIMITS,
                                       int(time.time()) + 3600, current.row_version, 'fixture', 'monitor-grant')
        self.assertEqual(granted.mode, 'monitoring')
        self.assertEqual(granted.state, 'active')
        after_grant = goal.get_goal(self.store, current.id)
        self.assertEqual(after_grant.state, 'approved')
        with self.assertRaises(goal.IllegalTransition):
            goal.transition(self.store, after_grant.id, 'monitoring', '', after_grant.row_version, 'fixture', 'monitor-too-soon')
        self.assertEqual(goal.get_goal(self.store, current.id).state, 'approved')
        self.assertEqual(self.store.one('SELECT state FROM execution_grants WHERE id=?', (research.id,))[0], 'revoked')

    def test_second_stored_goal_cannot_start_while_another_runs_and_conflict_is_typed(self):
        active, _grant = self.running('active')
        second, _contract = self.approved('second')
        second, _grant = self.granted(second)
        with self.assertRaises(goal.ValidationError):
            goal.transition(self.store, second.id, 'start', '', second.row_version, 'fixture', 'second-start')
        self.assertEqual(goal.get_goal(self.store, second.id).state, 'approved')
        self.assertEqual(goal.get_goal(self.store).id, active.id)
        self.assertEqual(self.store.one('SELECT COUNT(*) FROM strategies WHERE goal_id=?', (second.id,))[0], 0)

    def test_unconfirmed_answer_cannot_propose_and_failed_batch_rolls_back(self):
        selected = goal.create_goal(self.store, 'partial', 'Synthetic research control', 'fixture')
        question = goal._sibling('interview').question_for('degree_level')
        goal.record_answer(self.store, selected.id, 'degree_level', 'masters', False, question.visibility_default, selected.row_version, 'fixture')
        stored = self.store.one('SELECT status,provenance FROM profile_fields')
        self.assertEqual(tuple(stored), ('draft', 'user_stated'))
        current = goal.get_goal(self.store, selected.id)
        with self.assertRaises(goal.ValidationError):
            goal.propose_contract(self.store, current.id, {}, None, 'fixture', expected_row_version=current.row_version)
        self.assertEqual(self.store.one('SELECT COUNT(*) FROM goal_contracts')[0], 0)
        fresh = goal.create_goal(self.store, 'batch', 'Synthetic research control', 'fixture')
        raw = answers()
        raw['max_upfront_cash'] = {'amount': '424242', 'currency': 'INR'}
        batch = []
        interview = goal._sibling('interview')
        for key, value in raw.items():
            batch.append((key, value, True, interview.question_for(key).visibility_default))
        batch.append(('citizenship', 'Bad!!!', True, 'private'))
        with self.assertRaises(goal.ValidationError) as raised:
            goal.apply_answers(self.store, fresh.id, batch, fresh.row_version, 'fixture')
        self.assertNotIn('424242', str(raised.exception))
        self.assertNotIn(SENTINEL, str(raised.exception))
        self.assertEqual(self.store.one('SELECT COUNT(*) FROM profile_fields WHERE goal_id=?', (fresh.id,))[0], 0)
        self.assertEqual(goal.get_goal(self.store, fresh.id).row_version, fresh.row_version)

    def test_answer_and_approval_races_commit_once_and_replay_does_not_reapply(self):
        selected = goal.create_goal(self.store, 'race', 'Synthetic research control', 'fixture')
        path = self.store.path
        version = selected.row_version
        goal._sibling('interview')
        wins, conflicts = [], []
        barrier = threading.Barrier(2)

        def answer():
            local = orch.Store.connect_existing(path)
            try:
                barrier.wait(5)
                wins.append(goal.record_answer(local, selected.id, 'degree_level', 'masters', True, 'public_ok', version, 'fixture'))
            except goal.VersionConflict:
                conflicts.append('conflict')
            finally:
                local.db.close()

        threads = [threading.Thread(target=answer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(goal.get_goal(self.store, selected.id).row_version, version + 1)

        selected = self.answer_all(goal.get_goal(self.store, selected.id))
        selected, contract = self.propose(selected)
        results, errors = [], []
        barrier = threading.Barrier(2)

        def approve(key):
            local = orch.Store.connect_existing(path)
            try:
                barrier.wait(5)
                results.append(goal.approve_contract(local, selected.id, contract.version, contract.body_hash,
                                                     selected.row_version, 'fixture', key))
            except goal.VersionConflict:
                errors.append('conflict')
            finally:
                local.db.close()

        threads = [threading.Thread(target=approve, args=(f'race-approve-{index}',)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        approved = goal.get_goal(self.store, selected.id)
        self.assertEqual(approved.state, 'approved')
        self.assertEqual(approved.epoch, selected.epoch + 1)
        stopped = goal.transition(self.store, approved.id, 'stop', ' operator halt ', approved.row_version, 'fixture', 'stop-race')
        self.assertEqual(stopped.stop_reason, 'operator halt')
        key = self.store.one("SELECT idempotency_key FROM goal_events WHERE operation='approve' AND result_status=200 "
                             "AND idempotency_key LIKE 'race-approve-%'")[0]
        replay = goal.approve_contract(self.store, selected.id, contract.version, contract.body_hash, selected.row_version, 'fixture', key)
        self.assertEqual(replay.state, 'approved')
        self.assertEqual(goal.get_goal(self.store, selected.id).state, 'stopped')
        self.assertEqual(goal.get_goal(self.store, selected.id).stop_reason, 'operator halt')

    def _seed_work(self, selected, grant, reserved_amount, dispatched_amount, settled, lost):
        now = int(time.time())
        scope = f'goal:{selected.id}'
        with self.store.tx() as db:
            db.execute("INSERT INTO resource_ledger(scope_key,window_kind,window_key,counter,reserved,settled,lost) "
                       "VALUES(?,'lifetime','all','fetches',?,?,?)",
                       (scope, reserved_amount + dispatched_amount, settled, lost))
            for key, amount, state in (('reserved-op', reserved_amount, 'reserved'), ('dispatched-op', dispatched_amount, 'dispatched')):
                operation_id = db.execute(
                    'INSERT INTO resource_operations(goal_id,grant_id,operation_key,request_hash,binding_json,state,created_at,dispatched_at) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (selected.id, grant.id, key, 'hash', '{}', state, now, now if state == 'dispatched' else None),
                ).lastrowid
                task_id = db.execute(
                    "INSERT INTO tasks(kind,payload,status,next_attempt_at,created_at,goal_id,operation_key,lease_token,lease_until) "
                    "VALUES('fetch','{}','claimed',?,?,?,?,?,?)",
                    (now, now, selected.id, key, key + '-lease', now + 3600),
                ).lastrowid
                db.execute('INSERT INTO resource_reservations(grant_id,task_id,operation_key,scope_key,window_kind,window_key,counter,amount,state,created_at) '
                           "VALUES(?,?,?,?,'lifetime','all','fetches',?,'reserved',?)",
                           (grant.id, task_id, key, scope, amount, now))
                db.execute('INSERT INTO domain_slots(domain,task_id,operation_id,lease_token,lease_until) VALUES(?,?,?,?,?)',
                           (key + '.example.edu', task_id, operation_id, key + '-lease', now + 3600))

    def _seed_readiness(self, selected):
        now = int(time.time())
        interview = goal._sibling('interview')
        with self.store.tx() as db:
            db.execute("INSERT INTO domains(domain) VALUES('example.edu')")
            page_id = db.execute(
                "INSERT INTO pages(canon_url,domain,next_check_at,interval_sec,discovered_via,created_at) "
                "VALUES('https://example.edu/synthetic-program','example.edu',?,86400,'seed',?)",
                (now, now),
            ).lastrowid
            db.execute("INSERT INTO programs(key,name,degree_level,modality,field_tags_json,provenance,first_seen_at,last_seen_at) "
                       "VALUES('synthetic-program','Synthetic Program','masters','on_campus','[]','observed',?,?)",
                       (now, now))
            snapshot_id = db.execute(
                "INSERT INTO source_snapshots(page_id,content_hash,retrieved_at,text_path,visibility,provenance,applicability_json) "
                "VALUES(?,? ,?,'synthetic.txt','public','public_fetch','{}')",
                (page_id, 'a' * 64, now),
            ).lastrowid
            for field in interview.CRITICAL_SOURCE_FIELDS:
                db.execute(
                    'INSERT INTO observations(snapshot_id,page_id,program_key,field_key,value_json,subject_json,quote,quote_start,quote_end,'
                    'extractor_model,prompt_version,assurance,goal_id,contract_version,strategy_version,epoch,created_at) '
                    "VALUES(?,?,?,?,?,?,?,0,21,'synthetic','v1','reviewed_kept',?,?,?,?,?)",
                    (snapshot_id, page_id, 'synthetic-program', field, '"synthetic"', '{}', 'Synthetic public fact',
                     selected.id, selected.current_contract_version, selected.current_strategy_version, selected.epoch, now),
                )
            db.execute("INSERT INTO funding_packages(key,program_key,cycle_label,award_keys_json,components_json,compatibility_basis_ids_json,"
                       "cash_flow_json,basis_observation_ids_json,computed_at) VALUES('pkg-synthetic','synthetic-program','2031-fall','[]','{}','[]','{}','[]',?)",
                       (now,))
            db.execute('INSERT INTO constraint_evaluations(goal_id,contract_version,program_key,constraint_key,package_key,status,basis_json,reason,'
                       'evaluated_at,epoch,stale_for_goal) VALUES(?,1,?,?,?,?,?,?,?,?,0)',
                       (selected.id, 'synthetic-program', 'degree_level', 'pkg-synthetic', 'met', '{}', 'synthetic match', now, selected.epoch))
            db.execute("INSERT INTO goal_programs(goal_id,contract_version,program_key,role,visibility) VALUES(?,1,'synthetic-program','candidate','public')",
                       (selected.id,))


if __name__ == '__main__':
    unittest.main()
