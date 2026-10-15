"""Interview answers, contract shape, and public-frame projection."""
from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import sys
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
MONITORING = {
    'permitted_work': ['recheck_official_sources', 'refresh_cycle_dates'],
    'max_refreshes': 4,
    'interval_days': 30,
}
LIVING = {
    'basis': 'same_location_period',
    'count_housing_once': True,
    'count_insurance_once': False,
    'numeric_floor': {'amount': '2028', 'currency': 'INR'},
}
PUBLIC_KEYS = (
    'degree_level',
    'modality',
    'field_tags',
    'regions',
    'intake_windows',
    'required_components',
    'grants_only',
    'completion_min_candidates',
    'execution_policy',
    'monitoring',
)


def load_interview():
    path = ROOT / 'tools' / 'interview.py'
    key = 'snowball_interview'
    existing = sys.modules.get(key)
    if existing is not None and Path(getattr(existing, '__file__', '')).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


class InterviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interview = load_interview()
        cls.goal = cls.interview._sibling('goal')
        cls.ValidationError = cls.goal.ValidationError

    def raw_answers(self, records=False):
        answers = {
            'degree_level': 'masters',
            'modality': 'on_campus',
            'field_tags': ['synthetic-0'],
            'regions': ['sample-region'],
            'intake_windows': ['2028-spring'],
            'required_components': COMPONENTS,
            'grants_only': True,
            'accept_conditional_ra_ta': None,
            'accept_competitive_awards': False,
            'max_net_total_cost': {'amount': '1500.25', 'currency': 'INR'},
            'max_upfront_cash': {'amount': '0', 'currency': 'INR'},
            'min_living_adequacy_rule': LIVING,
            'max_work_hours_acceptable': 10,
            'allow_loans': False,
            'citizenship': 'CIVIC-TOKEN-ZZ',
            'prior_degree': {'level': 'bachelors', 'field': 'synthetic-studies'},
            'completion_min_candidates': 4,
            'execution_policy': POLICY,
            'monitoring': MONITORING,
            'consent_private_evaluation': True,
        }
        if records:
            answers['academic_record'] = {'grade': '17.25', 'scale': '20'}
            answers['tests'] = [{
                'test': 'SYNTH',
                'date': '2024-05-01',
                'score': {'value': '88.5', 'scale': '120'},
            }]
        return answers

    def entries(self, records=False, **replace):
        built = {}
        for key, value in self.raw_answers(records).items():
            question = self.interview.question_for(key)
            built[key] = {
                'value': self.interview.validate_answer(question, copy.deepcopy(value)),
                'status': 'known',
                'provenance': 'user_confirmed',
                'visibility': question.visibility_default,
            }
        for key, entry in replace.items():
            current = built.get(key, {'value': None, 'status': 'known', 'provenance': 'user_confirmed', 'visibility': 'private'})
            current.update(entry)
            built[key] = current
        return built

    def contract_and_frame(self, entries):
        body = self.interview.build_contract(entries)
        frame = self.interview.compile_public_frame(body, entries)
        return body, frame

    def unconfirmed(self, key):
        return {
            'value': None,
            'status': 'unconfirmed',
            'provenance': 'user_stated',
            'visibility': 'private',
        }

    def test_dependencies_skip_private_facts_until_consent(self):
        public = {key: self.entries()[key] for key in PUBLIC_KEYS}
        self.assertEqual(self.interview.next_question(public).field_key, 'consent_private_evaluation')
        declined = dict(public)
        declined['consent_private_evaluation'] = {
            'value': False,
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'private',
        }
        for key in ('citizenship', 'prior_degree', 'max_upfront_cash'):
            declined[key] = self.unconfirmed(key)
        self.assertIsNone(self.interview.next_question(declined))
        with self.assertRaises(self.ValidationError) as caught:
            self.interview.question_for('not-a-field')
        self.assertEqual(caught.exception.field, 'field_key')

    def test_unknown_and_imports_stay_questions(self):
        self.assertEqual(self.interview.next_question({}).field_key, 'degree_level')
        imported = {
            'degree_level': {
                'value': 'masters',
                'status': 'known',
                'provenance': 'imported_history',
                'visibility': 'public_ok',
            }
        }
        original = copy.deepcopy(imported)
        self.assertEqual(self.interview.next_question(imported).field_key, 'degree_level')
        with self.assertRaises(self.ValidationError) as caught:
            self.interview.build_contract(imported)
        self.assertEqual(caught.exception.field, 'degree_level')
        self.assertEqual(imported, original)
        system_default = self.entries()
        system_default['modality']['provenance'] = 'system_default'
        system_default['modality']['value'] = 'on_campus'
        self.assertEqual(self.interview.next_question(system_default).field_key, 'modality')
        reversed_fields = {}
        for key in reversed(list(self.entries())):
            reversed_fields[key] = copy.deepcopy(self.entries()[key])
        reversed_fields['degree_level']['provenance'] = 'user_stated'
        reversed_fields['degree_level']['status'] = 'draft'
        self.assertEqual(self.interview.next_question(reversed_fields).field_key, 'degree_level')

    def test_optional_credentials_and_broad_scope_dependencies_stay_stable(self):
        base = self.entries()
        self.assertIsNone(self.interview.next_question(base))
        self.assertNotIn('academic_record', base)
        self.assertNotIn('tests', base)
        base['academic_record'] = {
            'value': {'grade': '17.25', 'scale': '20'},
            'status': 'known',
            'provenance': 'imported_history',
            'visibility': 'private',
        }
        self.assertEqual(self.interview.next_question(base).field_key, 'academic_record')
        broad = self.entries()
        broad['regions']['value'] = []
        broad['scope_broad_acknowledged'] = {
            'value': False,
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'public_ok',
        }
        self.assertEqual(self.interview.next_question(broad).field_key, 'scope_broad_acknowledged')
        broad['scope_broad_acknowledged']['value'] = True
        self.assertIsNone(self.interview.next_question(broad))
        deferred = self.entries()
        deferred['consent_private_evaluation']['value'] = False
        deferred['academic_record'] = {
            'value': {'grade': '17.25', 'scale': '20'},
            'status': 'known',
            'provenance': 'imported_history',
            'visibility': 'private',
        }
        deferred['tests'] = {
            'value': [{'test': 'SYNTH', 'date': '2024-05-01', 'score': {'value': '88.5', 'scale': '120'}}],
            'status': 'known',
            'provenance': 'imported_history',
            'visibility': 'private',
        }
        self.assertIsNone(self.interview.next_question(deferred))
        before_deferred = copy.deepcopy(deferred)
        deferred_body, deferred_frame = self.contract_and_frame(deferred)
        self.assertEqual(deferred, before_deferred)
        self.assertEqual(deferred['academic_record']['provenance'], 'imported_history')
        self.assertNotEqual(deferred['academic_record']['provenance'], 'user_confirmed')
        projected = json.dumps(dataclasses.asdict(deferred_frame))
        self.assertNotIn('17.25', projected)
        self.assertNotIn('88.5', projected)
        self.assertNotIn('academic_record', projected)
        self.assertIs(deferred_body['funding']['grants_only'], True)
        missing_ack = self.entries()
        missing_ack['regions']['value'] = []
        self.assertEqual(self.interview.next_question(missing_ack).field_key, 'scope_broad_acknowledged')
        partial = {}
        for key in ('degree_level', 'modality', 'field_tags', 'regions'):
            partial[key] = self.entries()[key]
        self.assertEqual(self.interview.next_question(partial).field_key, 'intake_windows')

    def test_typed_answers_reject_guesses(self):
        money = self.interview.question_for('max_upfront_cash')
        self.assertEqual(self.interview.validate_answer(money, '1200 INR'), {'amount': '1200', 'currency': 'INR'})
        self.assertEqual(
            self.interview.validate_answer(money, '1200.50 INR'),
            {'amount': '1200.5', 'currency': 'INR'},
        )
        self.assertEqual(
            self.interview.validate_answer(money, {'amount': 0, 'currency': 'INR'}),
            {'amount': '0', 'currency': 'INR'},
        )
        for raw in (None, -1, 1.5, True, '1200', {'amount': '10', 'currency': 'usd'},
                    {'amount': 'nan', 'currency': 'INR'}, {'amount': 'inf', 'currency': 'INR'},
                    {'amount': '-1', 'currency': 'INR'}, {'amount': 1.0, 'currency': 'INR'}):
            with self.assertRaises(self.ValidationError):
                self.interview.validate_answer(money, raw)
        grants = self.interview.question_for('grants_only')
        self.assertIs(self.interview.validate_answer(grants, 'yes'), True)
        self.assertIs(self.interview.validate_answer(grants, 'NO'), False)
        for raw in (None, 0, 1, 'maybe', 'unknown'):
            with self.assertRaises(self.ValidationError):
                self.interview.validate_answer(grants, raw)
        optional = self.interview.question_for('accept_conditional_ra_ta')
        self.assertIsNone(self.interview.validate_answer(optional, None))
        self.assertIsNone(self.interview.validate_answer(optional, 'unknown'))
        self.assertIs(self.interview.validate_answer(optional, False), False)
        hours = self.interview.question_for('max_work_hours_acceptable')
        self.assertEqual(self.interview.validate_answer(hours, 0), 0)
        self.assertEqual(self.interview.validate_answer(hours, 168), 168)
        for raw in (-1, 169, True, 1.5, None):
            with self.assertRaises(self.ValidationError):
                self.interview.validate_answer(hours, raw)
        minimum = self.interview.question_for('completion_min_candidates')
        self.assertEqual(self.interview.validate_answer(minimum, 4), 4)
        for raw in (0, True, None):
            with self.assertRaises(self.ValidationError):
                self.interview.validate_answer(minimum, raw)
        regions = self.interview.question_for('regions')
        self.assertEqual(self.interview.validate_answer(regions, []), [])
        with self.assertRaises(self.ValidationError):
            self.interview.validate_answer(regions, ['worldwide'])
        with self.assertRaises(self.ValidationError):
            self.interview.validate_answer(self.interview.question_for('intake_windows'), [])
        academic = self.interview.question_for('academic_record')
        self.assertEqual(
            self.interview.validate_answer(academic, {'grade': '17.250', 'scale': '20'}),
            {'grade': '17.25', 'scale': '20'},
        )
        with self.assertRaises(self.ValidationError):
            self.interview.validate_answer(academic, {'grade': '17.25', 'scale': '20', 'percent': '86.25'})
        tests = self.interview.question_for('tests')
        with self.assertRaises(self.ValidationError):
            self.interview.validate_answer(tests, [{'test': 'SYNTH', 'date': '2024-05-01'}])
        with self.assertRaises(self.ValidationError):
            self.interview.validate_answer(tests, [{'test': 'SYNTH', 'date': '2024-02-31', 'score': {'value': '1', 'scale': '2'}}])
        policy = copy.deepcopy(POLICY)
        policy['usage_mode'] = 'hard'
        self.assertEqual(self.interview.validate_answer(self.interview.question_for('execution_policy'), policy)['usage_mode'], 'hard')
        broken = copy.deepcopy(POLICY)
        del broken['lifetime_limits']['model_tokens']
        with self.assertRaises(self.ValidationError):
            self.interview.validate_answer(self.interview.question_for('execution_policy'), broken)
        floated = copy.deepcopy(POLICY)
        floated['max_depth'] = 2.0
        with self.assertRaises(self.ValidationError):
            self.interview.validate_answer(self.interview.question_for('execution_policy'), floated)
        parsed_policy = self.interview.validate_answer(self.interview.question_for('execution_policy'), POLICY)
        self.assertEqual(parsed_policy['exploration_share'], '0.25')
        self.assertIsInstance(parsed_policy['exploration_share'], str)
        self.assertEqual(
            self.interview.validate_answer(self.interview.question_for('execution_policy'), parsed_policy)['exploration_share'],
            '0.25',
        )
        expected_whole = {0: 0, 1: 1, 0.0: 0, '1.0': 1}
        for whole, expected in expected_whole.items():
            integral = copy.deepcopy(POLICY)
            integral['exploration_share'] = whole
            self.assertIs(
                self.interview.validate_answer(self.interview.question_for('execution_policy'), integral)['exploration_share'],
                expected,
            )
        padded = copy.deepcopy(POLICY)
        padded['exploration_share'] = '0.250'
        self.assertEqual(
            self.interview.validate_answer(self.interview.question_for('execution_policy'), padded)['exploration_share'],
            '0.25',
        )
        for bad in (True, 1.5, '1e-1', '00.2', None):
            rejected = copy.deepcopy(POLICY)
            rejected['exploration_share'] = bad
            with self.assertRaises(self.ValidationError) as caught:
                self.interview.validate_answer(self.interview.question_for('execution_policy'), rejected)
            self.assertEqual(caught.exception.field, 'execution_policy')
            shown = bad if isinstance(bad, str) else str(bad)
            self.assertNotIn(shown, caught.exception.message)
        with self.assertRaises(self.ValidationError):
            self.interview.validate_answer(self.interview.question_for('monitoring'), {'enabled': True})

    def test_confirmed_answers_build_exact_contract_and_frame(self):
        entries = self.entries(records=True)
        before = copy.deepcopy(entries)
        body, frame = self.contract_and_frame(entries)
        self.assertEqual(entries, before)
        self.assertEqual(set(body), {'objective', 'scope', 'funding', 'completion', 'monitoring', 'execution_policy'})
        self.assertEqual(body['objective'], {
            'domain': 'masters_funding',
            'degree_level': 'masters',
            'modality': 'on_campus',
            'field_tags': ['synthetic-0'],
        })
        self.assertEqual(body['scope'], {
            'regions': ['sample-region'],
            'intake_windows': ['2028-spring'],
            'broad_acknowledged': False,
        })
        self.assertEqual(body['funding']['threshold_refs'], [
            'max_net_total_cost', 'max_upfront_cash', 'min_living_adequacy_rule', 'max_work_hours_acceptable',
        ])
        self.assertIsNone(body['funding']['accept_conditional_ra_ta'])
        self.assertIs(body['funding']['accept_competitive_awards'], False)
        self.assertIs(body['funding']['grants_only'], True)
        self.assertIs(body['funding']['required_components']['visa_costs'], False)
        self.assertEqual(body['completion']['min_candidates'], 4)
        self.assertEqual(body['completion']['critical_fields'], frame.required_source_fields)
        for name in ('degree_level', 'modality', 'tuition', 'eligibility_text', 'cycle_dates'):
            self.assertIn(name, body['completion']['critical_fields'])
        self.assertEqual(set(body['monitoring']), {'refresh_policy'})
        self.assertNotIn('enabled', body['monitoring']['refresh_policy'])
        self.assertEqual(body['execution_policy']['lifetime_limits']['queued_nodes'], 20)
        self.assertEqual(body['execution_policy']['usage_mode'], 'estimated')
        self.assertEqual(body['execution_policy']['exploration_share'], '0.25')
        self.assertNotIsInstance(body['execution_policy']['exploration_share'], float)
        body_text = json.dumps(body)
        for private in ('INR', '1500.25', 'CIVIC-TOKEN-ZZ', '17.25', '88.5', 'academic_record', 'citizenship'):
            self.assertNotIn(private, body_text)
        self.assertEqual(frame.domain, 'masters_funding')
        self.assertEqual(frame.frame_version, 1)
        self.assertEqual(set(frame.public_criteria), {
            'degree_level', 'modality', 'required_components', 'grants_only',
        })
        self.assertEqual(frame.public_criteria['degree_level'], 'masters')
        self.assertEqual(frame.public_criteria['modality'], 'on_campus')
        self.assertIs(frame.public_criteria['required_components']['visa_costs'], False)
        self.assertNotIn('CIVIC-TOKEN-ZZ', frame.public_objective)
        self.assertNotIn('1500.25', frame.public_objective)
        self.assertEqual(frame.field_tags, ['synthetic-0'])
        self.assertEqual(frame.regions, ['sample-region'])
        self.assertEqual(frame.intake_windows, ['2028-spring'])
        self.assertEqual(set(dataclasses.asdict(frame)), {
            'domain', 'public_objective', 'public_criteria', 'field_tags', 'regions',
            'intake_windows', 'required_source_fields', 'frame_version',
        })
        self.assertEqual(self.interview.PublicFrame(**dataclasses.asdict(frame)), frame)
        profile = {
            key: self.goal.ProfileField(key, entry['value'], entry['status'], entry['provenance'], entry['visibility'])
            for key, entry in entries.items()
        }
        self.assertEqual(self.interview.compile_public_frame(body, profile), frame)

    def test_degree_and_modality_come_from_confirmed_answers(self):
        entries = self.entries()
        entries['degree_level']['value'] = 'phd'
        entries['modality']['value'] = 'online'
        body, frame = self.contract_and_frame(entries)
        self.assertEqual(body['objective']['degree_level'], 'phd')
        self.assertEqual(body['objective']['modality'], 'online')
        self.assertEqual(frame.public_criteria['degree_level'], 'phd')
        self.assertEqual(frame.public_criteria['modality'], 'online')
        self.assertEqual(frame.domain, 'masters_funding')
        self.assertNotIn('CIVIC-TOKEN-ZZ', frame.public_objective)

    def test_empty_regions_require_explicit_broad_acknowledgment(self):
        denied = self.entries()
        denied['regions']['value'] = []
        denied['scope_broad_acknowledged'] = {
            'value': False,
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'public_ok',
        }
        with self.assertRaises(self.ValidationError) as caught:
            self.interview.build_contract(denied)
        self.assertEqual(caught.exception.field, 'scope_broad_acknowledged')
        acknowledged = self.entries()
        acknowledged['regions']['value'] = []
        acknowledged['scope_broad_acknowledged'] = {
            'value': True,
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'public_ok',
        }
        body, frame = self.contract_and_frame(acknowledged)
        self.assertEqual(body['scope']['regions'], [])
        self.assertIs(body['scope']['broad_acknowledged'], True)
        self.assertEqual(frame.regions, [])
        self.assertNotIn('worldwide', json.dumps(dataclasses.asdict(frame)))

    def test_private_values_and_live_draft_do_not_change_the_frame(self):
        quiet = self.entries()
        loud = self.entries(records=True)
        loud['max_upfront_cash']['value'] = {'amount': '9999', 'currency': 'EUR'}
        loud['citizenship']['value'] = 'OTHER-TOKEN-YY'
        quiet_body, quiet_frame = self.contract_and_frame(quiet)
        loud_body, loud_frame = self.contract_and_frame(loud)
        self.assertEqual(quiet_frame, loud_frame)
        self.assertEqual(quiet_body['objective'], loud_body['objective'])
        blob = json.dumps(dataclasses.asdict(loud_frame))
        for private in ('EUR', '9999', 'CIVIC-TOKEN-ZZ', 'OTHER-TOKEN-YY', '17.25', '88.5', 'INR', '1500.25'):
            self.assertNotIn(private, blob)
        self.assertIn('synthetic-0', blob)
        self.assertIn('2028-spring', blob)
        before = copy.deepcopy(loud)
        frame = self.interview.compile_public_frame(loud_body, loud)
        self.assertEqual(loud, before)
        loud['required_components']['value']['tuition'] = False
        loud['degree_level']['visibility'] = 'private'
        loud['degree_level']['value'] = 'phd'
        loud['max_net_total_cost']['value']['amount'] = '1'
        self.assertIs(frame.public_criteria['required_components']['tuition'], True)
        self.assertEqual(frame.public_criteria['degree_level'], 'masters')
        self.assertNotIn('phd', frame.public_objective)
        frozen = copy.deepcopy(before)
        self.assertEqual(self.interview.compile_public_frame(loud_body, frozen), frame)
        with self.assertRaises(self.ValidationError) as caught:
            self.interview.compile_public_frame(loud_body, loud)
        self.assertEqual(caught.exception.field, 'degree_level')
        self.assertNotIn('CIVIC-TOKEN-ZZ', caught.exception.message)
        tampered = copy.deepcopy(quiet_body)
        tampered['objective']['domain'] = 'phd_funding'
        with self.assertRaises(self.ValidationError) as caught:
            self.interview.compile_public_frame(tampered, quiet)
        self.assertEqual(caught.exception.field, 'domain')
        hidden = self.entries()
        hidden['max_upfront_cash']['visibility'] = 'public_ok'
        with self.assertRaises(self.ValidationError) as caught:
            self.interview.build_contract(hidden)
        self.assertEqual(caught.exception.field, 'max_upfront_cash')
        self.assertNotIn('1500.25', caught.exception.message)

    def test_declined_private_evaluation_freezes_unknown_personal_facts(self):
        red = self.entries()
        red['regions']['value'] = []
        red['scope_broad_acknowledged'] = {
            'value': True,
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'public_ok',
        }
        red['consent_private_evaluation']['value'] = False
        for key in ('citizenship', 'prior_degree', 'max_upfront_cash'):
            red[key] = self.unconfirmed(key)
        before = copy.deepcopy(red)
        self.assertIsNone(self.interview.next_question(red))
        body, frame = self.contract_and_frame(red)
        self.assertEqual(red, before)
        for key in ('citizenship', 'prior_degree', 'max_upfront_cash'):
            self.assertIsNone(red[key]['value'])
            self.assertEqual(red[key]['status'], 'unconfirmed')
            self.assertEqual(red[key]['provenance'], 'user_stated')
        self.assertNotEqual(red['max_upfront_cash']['value'], {'amount': '0', 'currency': 'INR'})
        self.assertEqual(body['scope']['regions'], [])
        self.assertIs(body['scope']['broad_acknowledged'], True)
        self.assertIs(body['execution_policy']['lifetime_limits']['fetches'], 20)
        self.assertEqual(frame.regions, [])
        self.assertEqual(frame.public_criteria['degree_level'], 'masters')
        self.assertEqual(frame.public_criteria['modality'], 'on_campus')
        projected = json.dumps(dataclasses.asdict(frame))
        for private in ('CIVIC-TOKEN-ZZ', 'citizenship', 'max_upfront_cash', 'prior_degree', '1500.25', 'INR'):
            self.assertNotIn(private, projected)
        self.assertNotIn('0', json.dumps(body['funding']))

        minimal = {key: self.entries()[key] for key in PUBLIC_KEYS}
        minimal['regions'] = {
            'value': [],
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'public_ok',
        }
        minimal['scope_broad_acknowledged'] = {
            'value': True,
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'public_ok',
        }
        minimal['consent_private_evaluation'] = {
            'value': False,
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'private',
        }
        for key in ('citizenship', 'prior_degree', 'max_upfront_cash'):
            minimal[key] = self.unconfirmed(key)
        minimal_body, minimal_frame = self.contract_and_frame(minimal)
        self.assertIsNone(minimal_body['funding']['accept_conditional_ra_ta'])
        self.assertIsNone(minimal_body['funding']['accept_competitive_awards'])
        self.assertEqual(minimal_body['funding']['threshold_refs'], [
            'max_net_total_cost', 'max_upfront_cash', 'min_living_adequacy_rule', 'max_work_hours_acceptable',
        ])
        self.assertNotIn('citizenship', json.dumps(dataclasses.asdict(minimal_frame)))
        self.assertIsNone(minimal['citizenship']['value'])

        enabled = self.entries()
        enabled['max_upfront_cash'] = self.unconfirmed('max_upfront_cash')
        self.assertEqual(self.interview.next_question(enabled).field_key, 'max_upfront_cash')
        with self.assertRaises(self.ValidationError) as blocked:
            self.interview.build_contract(enabled)
        self.assertEqual(blocked.exception.field, 'max_upfront_cash')
        self.assertIsNone(enabled['max_upfront_cash']['value'])
        self.assertNotIn('0', blocked.exception.message)
        enabled['max_upfront_cash'] = {
            'value': {'amount': 'nan', 'currency': 'INR'},
            'status': 'known',
            'provenance': 'user_confirmed',
            'visibility': 'private',
        }
        with self.assertRaises(self.ValidationError) as invalid:
            self.interview.build_contract(enabled)
        self.assertEqual(invalid.exception.field, 'max_upfront_cash')
        self.assertNotIn('nan', invalid.exception.message)


if __name__ == '__main__':
    unittest.main()
