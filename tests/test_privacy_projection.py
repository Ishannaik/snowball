"""Frozen public frames stay free of private profile values on a real Store."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

COMPONENTS = {
    'tuition': True,
    'mandatory_fees': True,
    'living_amount': True,
    'living_duration': True,
    'health_insurance': True,
    'visa_costs': False,
    'application_costs': True,
    'travel_costs': False,
    'deposits': True,
    'proof_of_funds_liquidity': True,
}
POLICY = {
    'lifetime_limits': {
        'queued_nodes': 20,
        'visited_nodes': 20,
        'fetches': 20,
        'queries': 20,
        'model_tokens': 40000,
    },
    'max_depth': 2,
    'max_queue': 10,
    'per_domain_inflight': 1,
    'max_hypotheses_per_wave': 2,
    'exploration_share': 0.25,
    'usage_mode': 'estimated',
}
ANSWERS = (
    ('degree_level', 'masters'),
    ('modality', 'on_campus'),
    ('field_tags', ['synthetic-0']),
    ('regions', ['sample-region']),
    ('intake_windows', ['2028-spring']),
    ('required_components', COMPONENTS),
    ('grants_only', True),
    ('accept_conditional_ra_ta', None),
    ('accept_competitive_awards', False),
    ('max_net_total_cost', {'amount': '1500.25', 'currency': 'INR'}),
    ('max_upfront_cash', {'amount': '0', 'currency': 'INR'}),
    ('min_living_adequacy_rule', {
        'basis': 'same_location_period',
        'count_housing_once': True,
        'count_insurance_once': False,
        'numeric_floor': {'amount': '2028', 'currency': 'INR'},
    }),
    ('max_work_hours_acceptable', 10),
    ('allow_loans', False),
    ('citizenship', 'CIVIC-TOKEN-ZZ'),
    ('prior_degree', {'level': 'bachelors', 'field': 'synthetic-studies'}),
    ('academic_record', {'grade': '17.25', 'scale': '20'}),
    ('tests', [{'test': 'SYNTH', 'date': '2024-05-01', 'score': {'value': '88.5', 'scale': '120'}}]),
    ('completion_min_candidates', 4),
    ('execution_policy', POLICY),
    ('monitoring', {
        'permitted_work': ['recheck_official_sources'],
        'max_refreshes': 3,
        'interval_days': 14,
    }),
    ('consent_private_evaluation', True),
)

spec = importlib.util.spec_from_file_location('privacy_projection_loader', ROOT / 'tools' / 'goal.py')
loader = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = loader
spec.loader.exec_module(loader)
goal, interview, orch = loader._sibling('goal'), loader._sibling('interview'), loader._sibling('orchestrator')


def snapshot_of(store, goal_id):
    return {
        key: {
            'value': field.value,
            'status': field.status,
            'provenance': field.provenance,
            'visibility': field.visibility,
        }
        for key, field in goal.fields(store, goal_id).items()
    }


class PrivacyProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = orch.Store.open(Path(self.temp.name) / 'privacy.db')
        self.selected = goal.create_goal(self.store, 'frame-privacy', 'TITLETOKEN research label', 'fixture')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def answer_all(self):
        selected = self.selected
        for key, value in ANSWERS:
            selected = goal.get_goal(self.store, selected.id)
            visibility = interview.question_for(key).visibility_default
            goal.record_answer(
                self.store, selected.id, key, value, True, visibility, selected.row_version, 'fixture',
            )
        return goal.get_goal(self.store, selected.id)

    def test_imported_history_cannot_lock_a_degree(self):
        selected = self.answer_all()
        with self.store.tx() as db:
            db.execute(
                "UPDATE profile_fields SET provenance='imported_history' WHERE goal_id=? AND key='degree_level'",
                (selected.id,),
            )
        fields = goal.fields(self.store, selected.id)
        self.assertEqual(fields['degree_level'].value, 'masters')
        self.assertEqual(fields['degree_level'].provenance, 'imported_history')
        self.assertEqual(interview.next_question(fields).field_key, 'degree_level')
        with self.assertRaises(goal.ValidationError) as caught:
            goal.propose_contract(
                self.store, selected.id, {'objective': {'domain': 'masters_funding'}},
                selected.current_contract_version, 'fixture', expected_row_version=selected.row_version,
            )
        self.assertEqual(caught.exception.field, 'degree_level')
        self.assertIsNone(self.store.one('SELECT version FROM goal_contracts WHERE goal_id=?', (selected.id,)))

    def test_approved_frame_omits_private_values_and_ignores_draft_toggle(self):
        selected = self.answer_all()
        self.assertIsNone(interview.next_question(goal.fields(self.store, selected.id)))
        body = interview.build_contract(goal.fields(self.store, selected.id))
        proposed = goal.propose_contract(
            self.store, selected.id, body, selected.current_contract_version, 'fixture',
            expected_row_version=selected.row_version,
        )
        selected = goal.get_goal(self.store, selected.id)
        goal.approve_contract(
            self.store, selected.id, proposed.version, proposed.body_hash,
            selected.row_version, 'fixture', 'approve-frame',
        )
        locked = goal.load_contract(self.store, selected.id, proposed.version)
        frame_text = json.dumps(locked.frame)
        snapshot_text = json.dumps(locked.profile_snapshot)
        self.assertEqual(locked.frame['domain'], 'masters_funding')
        self.assertEqual(locked.frame['public_criteria']['degree_level'], 'masters')
        self.assertEqual(locked.frame['public_criteria']['modality'], 'on_campus')
        self.assertEqual(set(locked.frame['public_criteria']), {
            'degree_level', 'modality', 'required_components', 'grants_only',
        })
        self.assertIn('synthetic-0', locked.frame['field_tags'])
        self.assertEqual(locked.frame['intake_windows'], ['2028-spring'])
        self.assertNotIn('TITLETOKEN', frame_text)
        for private in (
            'CIVIC-TOKEN-ZZ', '17.25', '88.5', '1500.25', 'INR', 'max_upfront_cash',
            'citizenship', 'academic_record', 'consent_private_evaluation', 'allow_loans',
            locked.profile_snapshot_hash, locked.body_hash,
        ):
            self.assertNotIn(private, frame_text)
            self.assertIn('CIVIC-TOKEN-ZZ', snapshot_text)
        self.assertIn('17.25', snapshot_text)
        self.assertEqual(locked.profile_snapshot['max_upfront_cash']['value'], {'amount': '0', 'currency': 'INR'})
        self.assertEqual(locked.profile_snapshot['max_upfront_cash']['visibility'], 'private')
        compiled = interview.compile_public_frame(locked.body, locked.profile_snapshot)
        self.assertEqual(compiled, interview.PublicFrame(**locked.frame))
        self.assertNotIn('TITLETOKEN', compiled.public_objective)

        selected = goal.get_goal(self.store, selected.id)
        selected = goal.transition(
            self.store, selected.id, 'revise', 'toggle draft privacy', selected.row_version, 'fixture', 'revise-frame',
        )
        goal.record_answer(
            self.store, selected.id, 'max_upfront_cash', {'amount': '9999', 'currency': 'EUR'},
            True, 'public_ok', selected.row_version, 'fixture',
        )
        reloaded = goal.load_contract(self.store, selected.id, proposed.version)
        self.assertEqual(reloaded.frame, locked.frame)
        self.assertEqual(reloaded.profile_snapshot, locked.profile_snapshot)
        self.assertEqual(reloaded.profile_snapshot_hash, locked.profile_snapshot_hash)
        draft = goal.fields(self.store, selected.id)['max_upfront_cash']
        self.assertEqual(draft.value, {'amount': '9999', 'currency': 'EUR'})
        self.assertEqual(draft.visibility, 'public_ok')
        with self.assertRaises(goal.ValidationError):
            interview.compile_public_frame(reloaded.body, snapshot_of(self.store, selected.id))
        self.assertEqual(goal.load_contract(self.store, selected.id, proposed.version).frame, locked.frame)

    def test_declined_consent_keeps_unknown_eligibility_out_of_the_frame(self):
        selected = self.selected
        answered = (
            ('degree_level', 'masters', 'public_ok'),
            ('modality', 'on_campus', 'public_ok'),
            ('field_tags', ['synthetic-0'], 'public_ok'),
            ('regions', [], 'public_ok'),
            ('intake_windows', ['2028-spring'], 'public_ok'),
            ('scope_broad_acknowledged', True, 'public_ok'),
            ('required_components', COMPONENTS, 'public_ok'),
            ('grants_only', True, 'public_ok'),
            ('completion_min_candidates', 4, 'public_ok'),
            ('execution_policy', POLICY, 'public_ok'),
            ('monitoring', {
                'permitted_work': ['recheck_official_sources'],
                'max_refreshes': 3,
                'interval_days': 14,
            }, 'public_ok'),
            ('consent_private_evaluation', False, 'private'),
            ('accept_conditional_ra_ta', None, 'private'),
            ('accept_competitive_awards', False, 'private'),
            ('max_net_total_cost', {'amount': '1500.25', 'currency': 'INR'}, 'private'),
            ('min_living_adequacy_rule', {
                'basis': 'same_location_period',
                'count_housing_once': True,
                'count_insurance_once': False,
                'numeric_floor': {'amount': '2028', 'currency': 'INR'},
            }, 'private'),
            ('max_work_hours_acceptable', 10, 'private'),
            ('allow_loans', False, 'private'),
        )
        for key, value, visibility in answered:
            selected = goal.get_goal(self.store, selected.id)
            goal.record_answer(
                self.store, selected.id, key, value, True, visibility, selected.row_version, 'fixture',
            )
        selected = goal.get_goal(self.store, selected.id)
        parked = {
            'max_upfront_cash': None,
            'citizenship': None,
            'prior_degree': None,
            'academic_record': {'grade': '17.25', 'scale': '20'},
        }
        with self.store.tx() as db:
            for key, value in parked.items():
                provenance = 'imported_history' if key == 'academic_record' else 'user_stated'
                status = 'known' if key == 'academic_record' else 'unconfirmed'
                db.execute(
                    'INSERT INTO profile_fields(goal_id,key,value_json,status,provenance,visibility,row_version,updated_at) '
                    'VALUES(?,?,?,?,?,?,0,?)',
                    (selected.id, key, goal.canonical(value), status, provenance, 'private', 0),
                )
        fields = goal.fields(self.store, selected.id)
        self.assertIsNone(interview.next_question(fields))
        for key in ('max_upfront_cash', 'citizenship', 'prior_degree'):
            self.assertIsNone(fields[key].value)
            self.assertEqual(fields[key].status, 'unconfirmed')
            self.assertEqual(fields[key].provenance, 'user_stated')
        self.assertEqual(fields['academic_record'].provenance, 'imported_history')
        body = interview.build_contract(fields)
        self.assertEqual(fields['max_upfront_cash'].value, None)
        self.assertEqual(fields['max_upfront_cash'].provenance, 'user_stated')
        self.assertEqual(fields['academic_record'].provenance, 'imported_history')
        self.assertIsNone(body['funding']['accept_conditional_ra_ta'])
        self.assertIs(body['scope']['broad_acknowledged'], True)
        self.assertEqual(body['scope']['regions'], [])
        self.assertEqual(body['objective']['degree_level'], 'masters')
        self.assertEqual(body['objective']['modality'], 'on_campus')
        self.assertNotIn('1500.25', json.dumps(body))
        self.assertNotIn('17.25', json.dumps(body))
        proposed = goal.propose_contract(
            self.store, selected.id, body, selected.current_contract_version, 'fixture',
            expected_row_version=selected.row_version,
        )
        stored = goal.fields(self.store, selected.id)
        self.assertEqual(stored['citizenship'].status, 'unconfirmed')
        self.assertEqual(stored['citizenship'].provenance, 'user_stated')
        self.assertIsNone(stored['citizenship'].value)
        self.assertEqual(stored['academic_record'].provenance, 'imported_history')
        selected = goal.get_goal(self.store, selected.id)
        goal.approve_contract(
            self.store, selected.id, proposed.version, proposed.body_hash,
            selected.row_version, 'fixture', 'approve-public-unknown',
        )
        locked = goal.load_contract(self.store, selected.id, proposed.version)
        self.assertEqual(locked.profile_snapshot_hash, goal.digest(locked.profile_snapshot))
        self.assertEqual(locked.profile_snapshot['max_upfront_cash']['value'], None)
        self.assertEqual(locked.profile_snapshot['max_upfront_cash']['status'], 'unconfirmed')
        self.assertEqual(locked.profile_snapshot['max_upfront_cash']['provenance'], 'user_stated')
        self.assertEqual(locked.profile_snapshot['prior_degree']['value'], None)
        self.assertEqual(locked.profile_snapshot['academic_record']['provenance'], 'imported_history')
        frame_text = json.dumps(locked.frame)
        self.assertEqual(locked.frame['regions'], [])
        self.assertEqual(locked.frame['public_criteria']['degree_level'], 'masters')
        self.assertEqual(locked.frame['public_criteria']['modality'], 'on_campus')
        for private in ('1500.25', '17.25', 'INR', 'citizenship', 'max_upfront_cash', 'academic_record', 'TITLETOKEN'):
            self.assertNotIn(private, frame_text)
        qualify = goal._sibling('qualify')
        amount, state = qualify._profile_value(locked.profile_snapshot, 'max_upfront_cash')
        citizen, citizen_state = qualify._profile_value(locked.profile_snapshot, 'citizenship')
        consent, consent_state = qualify._profile_value(locked.profile_snapshot, 'consent_private_evaluation')
        self.assertIsNone(amount)
        self.assertEqual(state, 'unconfirmed')
        self.assertIsNone(citizen)
        self.assertEqual(citizen_state, 'unconfirmed')
        self.assertIs(consent, False)
        self.assertEqual(consent_state, 'known')
        status = qualify.completion_status(self.store, goal.get_goal(self.store, selected.id))
        self.assertFalse(status.ready)
        selected = goal.get_goal(self.store, selected.id)
        selected = goal.transition(
            self.store, selected.id, 'revise', 'draft stays off the frozen snapshot',
            selected.row_version, 'fixture', 'revise-unknown',
        )
        goal.record_answer(
            self.store, selected.id, 'citizenship', 'OTHER-TOKEN', True, 'private',
            selected.row_version, 'fixture',
        )
        reloaded = goal.load_contract(self.store, selected.id, proposed.version)
        self.assertEqual(reloaded.profile_snapshot_hash, locked.profile_snapshot_hash)
        self.assertEqual(reloaded.profile_snapshot, locked.profile_snapshot)
        self.assertIsNone(reloaded.profile_snapshot['citizenship']['value'])
        self.assertNotIn('OTHER-TOKEN', json.dumps(reloaded.frame))
        self.assertEqual(goal.fields(self.store, selected.id)['citizenship'].value, 'OTHER-TOKEN')
        self.assertEqual(goal.fields(self.store, selected.id)['academic_record'].provenance, 'imported_history')


if __name__ == '__main__':
    unittest.main()
