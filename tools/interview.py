"""Code-owned interview, typed answers, and the immutable public research frame.

Public material criteria enter a contract only from user_confirmed or known
answers. imported_history and system_default are never treated as confirmed.
Declined private evaluation leaves personal eligibility unknown in the frozen
snapshot: those rows are not confirmed, not replaced with a nationality or
zero, and not copied into the public frame. Unconfirmed conditional
preferences stay null. The frame is built from whitelisted public_ok values;
private amounts, credentials, citizenship, titles, and hashes are never
copied, and public text is not scanned for private numbers.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import importlib.util
import json
import math
import re
import sys
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = 'masters_funding'

CRITICAL_SOURCE_FIELDS = (
    'degree_level',
    'modality',
    'field_of_study',
    'intake_cycle',
    'tuition',
    'mandatory_fees',
    'living_support_amount',
    'living_support_duration',
    'health_insurance',
    'award_type',
    'funding_source_official',
    'eligibility_text',
    'cycle_dates',
)
THRESHOLD_REFS = (
    'max_net_total_cost',
    'max_upfront_cash',
    'min_living_adequacy_rule',
    'max_work_hours_acceptable',
)
PRIVATE_ELIGIBILITY_FIELDS = frozenset({
    'accept_conditional_ra_ta',
    'accept_competitive_awards',
    *THRESHOLD_REFS,
    'allow_loans',
    'citizenship',
    'prior_degree',
    'academic_record',
    'tests',
})
PRIVATE_REQUIRED_FIELDS = (
    'accept_conditional_ra_ta',
    'accept_competitive_awards',
    *THRESHOLD_REFS,
    'allow_loans',
    'citizenship',
    'prior_degree',
)
COMPONENT_KEYS = (
    'tuition',
    'mandatory_fees',
    'living_amount',
    'living_duration',
    'health_insurance',
    'visa_costs',
    'application_costs',
    'travel_costs',
    'deposits',
    'proof_of_funds_liquidity',
)
POLICY_COUNTERS = (
    'queued_nodes',
    'visited_nodes',
    'fetches',
    'queries',
    'model_tokens',
)
POLICY_STRUCTURAL = (
    'max_depth',
    'max_queue',
    'per_domain_inflight',
    'max_hypotheses_per_wave',
)
DEGREES = ('masters', 'phd', 'other')
MODALITIES = ('on_campus', 'online', 'hybrid')
PRIOR_LEVELS = ('bachelors', 'masters', 'doctoral', 'other')
WORK_TOKENS = (
    'recheck_official_sources',
    'refresh_cycle_dates',
    'refresh_public_funding_facts',
)
CRITERIA_KEYS = ('degree_level', 'modality', 'required_components', 'grants_only')
_ENTRY_KEYS = ('value', 'status', 'provenance', 'visibility')
_WORLDWIDE = frozenset({'worldwide', 'world', 'global', 'earth', 'anywhere', 'any', 'all', '*'})
_INTAKE = re.compile(r'^(?:19|20|21)[0-9]{2}-(fall|spring|summer|winter)$')
_DATE = re.compile(r'^(?:19|20|21)[0-9]{2}-[0-9]{2}-[0-9]{2}$')
_AMOUNT = re.compile(r'(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?')
_MONEY_TEXT = re.compile(rf'^({_AMOUNT.pattern})\s+([A-Z]{{3}})$')
_TRUE = frozenset({'true', 'yes', 'y'})
_FALSE = frozenset({'false', 'no', 'n'})
_UNCONFIRMED_PROVENANCE = frozenset({'imported_history', 'system_default'})


def _sibling(name: str) -> Any:
    key = f'snowball_{name}'
    path = ROOT / 'tools' / f'{name}.py'
    existing = sys.modules.get(key)
    if existing is not None and Path(getattr(existing, '__file__', '')).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _fail(field: str, message: str) -> None:
    raise _sibling('goal').ValidationError(field, message)


@dataclasses.dataclass(frozen=True)
class Question:
    field_key: str
    text: str
    answer_type: str
    required: bool
    visibility_default: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'depends_on', tuple(self.depends_on))


@dataclasses.dataclass(frozen=True)
class PublicFrame:
    domain: str
    public_objective: str
    public_criteria: dict[str, Any]
    field_tags: list[str]
    regions: list[str]
    intake_windows: list[str]
    required_source_fields: list[str]
    frame_version: int = 1

    def __post_init__(self) -> None:
        if type(self.frame_version) is not int or self.frame_version != 1:
            _fail('frame_version', 'Unsupported public frame version.')
        if self.domain != DOMAIN:
            _fail('domain', 'Only the masters_funding profile is supported.')
        if not isinstance(self.public_objective, str) or not self.public_objective.strip():
            _fail('public_objective', 'Public objective must be generated text.')
        if not isinstance(self.public_criteria, dict) or set(self.public_criteria) != set(CRITERIA_KEYS):
            _fail('public_criteria', 'Public criteria contain an unsupported field.')
        object.__setattr__(self, 'field_tags', _text_list(self.field_tags, 'field_tags'))
        object.__setattr__(self, 'regions', _text_list(self.regions, 'regions'))
        object.__setattr__(self, 'intake_windows', _text_list(self.intake_windows, 'intake_windows'))
        object.__setattr__(self, 'required_source_fields', _text_list(self.required_source_fields, 'required_source_fields'))
        object.__setattr__(self, 'public_criteria', copy.deepcopy(dict(self.public_criteria)))


def _text_list(value: Any, field: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        _fail(field, 'Expected a list of text values.')
    if not all(isinstance(item, str) for item in value):
        _fail(field, 'Expected a list of text values.')
    return list(value)


QUESTIONS: list[Question] = [
    Question(
        'degree_level',
        'What degree level should this goal pursue? Answer masters, phd, or other. '
        'The profile name does not choose a degree for you.',
        'degree_level', True, 'public_ok', (),
    ),
    Question(
        'modality',
        'What study modality should this goal pursue? Answer on_campus, online, or hybrid.',
        'modality', True, 'public_ok', ('degree_level',),
    ),
    Question(
        'field_tags',
        'Which field tags should public search use? Answer a JSON list of short labels. '
        'No field list is assumed.',
        'string_list', True, 'public_ok', ('modality',),
    ),
    Question(
        'regions',
        'Which regions should public search use? Answer a JSON list of region labels. '
        'An empty list does not mean worldwide.',
        'region_list', True, 'public_ok', ('field_tags',),
    ),
    Question(
        'intake_windows',
        'Which intake windows should public search use? Answer a JSON list of YYYY-season '
        'values, with season fall, spring, summer, or winter. No year is assumed.',
        'intake_list', True, 'public_ok', ('regions',),
    ),
    Question(
        'scope_broad_acknowledged',
        'Regions are empty. Do you explicitly acknowledge a bounded broad public-research '
        'scope while personal eligibility stays unknown? Answer yes or no. '
        'No empty region list is broadened automatically.',
        'bool', True, 'public_ok', ('regions',),
    ),
    Question(
        'required_components',
        'Which funding components are required? Answer a JSON object with an explicit true '
        'or false for tuition, mandatory_fees, living_amount, living_duration, '
        'health_insurance, visa_costs, application_costs, travel_costs, deposits, and '
        'proof_of_funds_liquidity. None of these is preselected, and they are not all '
        'additive expenses.',
        'components', True, 'public_ok', ('intake_windows',),
    ),
    Question(
        'grants_only',
        'Must every required component be satisfied without loans? Answer yes or no.',
        'bool', True, 'public_ok', ('required_components',),
    ),
    Question(
        'completion_min_candidates',
        'How many matching opportunities are required before a positive shortlist? Answer a '
        'positive whole number. Rejected programs do not count, and no minimum is assumed.',
        'positive_int', True, 'public_ok', ('grants_only',),
    ),
    Question(
        'execution_policy',
        'What finite execution policy should be approved? Answer JSON with lifetime_limits '
        'for queued_nodes, visited_nodes, fetches, queries, and model_tokens; max_depth; '
        'max_queue; per_domain_inflight; max_hypotheses_per_wave; exploration_share from '
        '0 to 1; and usage_mode estimated or hard. Every ceiling is a finite integer. '
        'Hard mode does not claim an attested transport.',
        'execution_policy', True, 'public_ok', ('completion_min_candidates',),
    ),
    Question(
        'monitoring',
        'What refresh work may a future monitoring grant perform? Answer JSON with '
        'permitted_work drawn from recheck_official_sources, refresh_cycle_dates, and '
        'refresh_public_funding_facts, plus max_refreshes and interval_days. This describes '
        'permitted work and does not start monitoring.',
        'monitoring', True, 'public_ok', ('execution_policy',),
    ),
    Question(
        'consent_private_evaluation',
        'Do you consent to private eligibility evaluation on this machine? Answer yes or no. '
        'No leaves personal eligibility unknown and does not schedule private evaluation. '
        'Yes does not guess missing thresholds, citizenship, or cash, and unknown thresholds '
        'cannot become ready.',
        'bool', True, 'private', ('monitoring',),
    ),
    Question(
        'accept_conditional_ra_ta',
        'Should conditional research or teaching appointments be considered? Answer yes, no, '
        'or null when that preference is still unknown. Null stays unknown.',
        'optional_bool', True, 'private', ('consent_private_evaluation',),
    ),
    Question(
        'accept_competitive_awards',
        'Should competitive awards be considered? Answer yes, no, or null when that '
        'preference is still unknown. Null stays unknown.',
        'optional_bool', True, 'private', ('accept_conditional_ra_ta',),
    ),
    Question(
        'max_net_total_cost',
        'What is the maximum acceptable net total cost? Answer an amount and ISO currency, '
        'for example 1200 INR, or {"amount","currency"}. Leave the question unanswered when '
        'the limit is unknown; unknown is not stored as zero.',
        'money', True, 'private', ('accept_competitive_awards',),
    ),
    Question(
        'max_upfront_cash',
        'What is the maximum upfront cash you can provide? Answer an amount and ISO currency. '
        'Leave the question unanswered when the limit is unknown; unknown is not stored as zero.',
        'money', True, 'private', ('max_net_total_cost',),
    ),
    Question(
        'min_living_adequacy_rule',
        'What living-support rule should private evaluation use? Answer JSON with basis '
        'same_location_period, explicit booleans count_housing_once and count_insurance_once, '
        'and numeric_floor set to null or a money object. Null means there is no numeric floor.',
        'living_rule', True, 'private', ('max_upfront_cash',),
    ),
    Question(
        'max_work_hours_acceptable',
        'What is the maximum acceptable work requirement per week, in hours? Answer a whole '
        'number from 0 through 168.',
        'hours', True, 'private', ('min_living_adequacy_rule',),
    ),
    Question(
        'allow_loans',
        'May loan awards be considered at all? Answer yes or no. A loan is not grant funding.',
        'bool', True, 'private', ('max_work_hours_acceptable',),
    ),
    Question(
        'citizenship',
        'What citizenship should private eligibility use? Answer one short plain label. '
        'No nationality is assumed.',
        'citizenship', True, 'private', ('allow_loans',),
    ),
    Question(
        'prior_degree',
        'What prior degree should private eligibility use? Answer JSON with level '
        '(bachelors, masters, doctoral, or other) and field.',
        'prior_degree', True, 'private', ('citizenship',),
    ),
    Question(
        'academic_record',
        'Provide an academic record only when you want one stored. Answer JSON with grade '
        'and scale as decimal strings. The scale is kept as given and is not converted.',
        'academic_record', False, 'private', ('prior_degree',),
    ),
    Question(
        'tests',
        'Provide test records only when you want them stored. Answer a JSON list of objects '
        'with test, date as YYYY-MM-DD, and score {value, scale}. No score is guessed.',
        'tests', False, 'private', ('prior_degree',),
    ),
]


def _validate_bank() -> None:
    seen: list[str] = []
    implemented = {
        'degree_level', 'modality', 'string_list', 'region_list', 'intake_list', 'bool',
        'optional_bool', 'components', 'money', 'living_rule', 'hours', 'citizenship',
        'prior_degree', 'academic_record', 'tests', 'positive_int', 'execution_policy',
        'monitoring',
    }
    for question in QUESTIONS:
        if question.field_key in seen or question.answer_type not in implemented:
            raise RuntimeError('The interview bank has a duplicate or unknown question.')
        if question.visibility_default not in ('private', 'public_ok') or type(question.required) is not bool:
            raise RuntimeError('The interview bank has an invalid question policy.')
        if any(dep not in seen for dep in question.depends_on):
            raise RuntimeError('Interview dependencies must refer to earlier questions.')
        seen.append(question.field_key)


_validate_bank()


def question_for(key: str) -> Question:
    if not isinstance(key, str):
        _fail('field_key', 'Unknown interview field.')
    for question in QUESTIONS:
        if question.field_key == key:
            return question
    _fail('field_key', 'Unknown interview field.')


def _freeze_entry(entry: Any) -> dict[str, Any]:
    if isinstance(entry, Mapping):
        if any(name not in entry for name in _ENTRY_KEYS):
            _fail('fields', 'A profile entry needs value, status, provenance, and visibility.')
        value, status, provenance, visibility = (entry[name] for name in _ENTRY_KEYS)
    elif all(hasattr(entry, name) for name in _ENTRY_KEYS):
        value, status, provenance, visibility = (getattr(entry, name) for name in _ENTRY_KEYS)
    else:
        _fail('fields', 'A profile entry needs value, status, provenance, and visibility.')
    if not isinstance(status, str) or not isinstance(provenance, str) or not isinstance(visibility, str):
        _fail('fields', 'Status, provenance, and visibility must be text.')
    return {
        'value': copy.deepcopy(value),
        'status': status,
        'provenance': provenance,
        'visibility': visibility,
    }


def _freeze_snapshot(fields: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(fields, Mapping):
        _fail('fields', 'Interview fields must be a mapping.')
    frozen: dict[str, dict[str, Any]] = {}
    for key, entry in fields.items():
        if not isinstance(key, str):
            _fail('fields', 'Profile keys must be text.')
        frozen[key] = _freeze_entry(entry)
    return frozen


def _is_confirmed(entry: Mapping[str, Any]) -> bool:
    if entry['provenance'] in _UNCONFIRMED_PROVENANCE:
        return False
    return entry['status'] == 'known' and entry['provenance'] == 'user_confirmed'


def _empty_regions(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and not isinstance(value, str) and len(value) == 0


def _confirmed_map(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        key: entry for key, entry in snapshot.items()
        if any(question.field_key == key for question in QUESTIONS) and _is_confirmed(entry)
    }


def _private_evaluation_enabled(confirmed: Mapping[str, Any]) -> bool:
    entry = confirmed.get('consent_private_evaluation')
    return entry is not None and entry['value'] is True


def _applicable(question: Question, snapshot: Mapping[str, Any], confirmed: Mapping[str, Any]) -> bool:
    if question.field_key in PRIVATE_ELIGIBILITY_FIELDS and not _private_evaluation_enabled(confirmed):
        return False
    if any(dep not in confirmed for dep in question.depends_on):
        return False
    if question.field_key == 'scope_broad_acknowledged':
        return _empty_regions(confirmed['regions']['value'])
    if not question.required:
        return question.field_key in snapshot
    return True


def _requirement_met(question: Question, entry: Mapping[str, Any]) -> bool:
    if question.field_key == 'scope_broad_acknowledged':
        return entry['value'] is True
    return True


def next_question(fields: Mapping[str, Any]) -> Question | None:
    snapshot = _freeze_snapshot(fields)
    confirmed = _confirmed_map(snapshot)
    for question in QUESTIONS:
        if not _applicable(question, snapshot, confirmed):
            continue
        entry = confirmed.get(question.field_key)
        if entry is None or not _requirement_met(question, entry):
            return question
    return None


def _decode(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if text == '':
        return text
    if text[0] in '{["' or text in ('true', 'false', 'null') or re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _bounded_text(raw: Any, field: str, limit: int = 80) -> str:
    if not isinstance(raw, str):
        _fail(field, 'Expected a short plain text value.')
    text = raw.strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        _fail(field, 'Expected a short plain text value.')
    return text


def _label(raw: Any, field: str) -> str:
    text = _bounded_text(raw, field)
    if not all(char.isalnum() or char in ' ._+-/&' for char in text):
        _fail(field, 'Expected a short plain label.')
    return text


def _enum(raw: Any, field: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(raw, str) or raw.strip().lower() not in allowed:
        _fail(field, 'Answer with one of the allowed values.')
    return raw.strip().lower()


def _explicit_bool(raw: Any, field: str, allow_none: bool = False) -> bool | None:
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in {'null', 'unknown'}):
        if allow_none:
            return None
        _fail(field, 'A boolean requires an explicit yes/no or true/false answer.')
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
    _fail(field, 'A boolean requires an explicit yes/no or true/false answer.')
    return None


def _amount_text(raw: Any, field: str) -> str:
    if isinstance(raw, bool) or isinstance(raw, float):
        _fail(field, 'Money needs a finite decimal amount and an ISO currency.')
    if isinstance(raw, int):
        text = str(raw)
    elif isinstance(raw, Decimal):
        if not raw.is_finite():
            _fail(field, 'Money needs a finite decimal amount and an ISO currency.')
        text = format(raw, 'f')
    elif isinstance(raw, str):
        text = raw.strip()
        if 'e' in text.lower():
            _fail(field, 'Money needs a finite decimal amount and an ISO currency.')
    else:
        _fail(field, 'Money needs a finite decimal amount and an ISO currency.')
    if not isinstance(text, str) or not re.fullmatch(_AMOUNT, text):
        _fail(field, 'Money needs a finite decimal amount and an ISO currency.')
    try:
        amount = Decimal(text)
    except InvalidOperation:
        _fail(field, 'Money needs a finite decimal amount and an ISO currency.')
    if not amount.is_finite() or amount < 0:
        _fail(field, 'Money needs a finite nonnegative amount and an ISO currency.')
    rendered = format(amount, 'f')
    if '.' in rendered:
        rendered = rendered.rstrip('0').rstrip('.')
    return rendered


def _currency(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not re.fullmatch(r'[A-Z]{3}', raw):
        _fail(field, 'Money needs an ISO currency code.')
    return raw


def _money(raw: Any, field: str) -> dict[str, str]:
    decoded = raw
    if isinstance(raw, str):
        match = _MONEY_TEXT.fullmatch(raw.strip())
        if match:
            decoded = {'amount': match.group(1), 'currency': match.group(2)}
    if not isinstance(decoded, dict) or set(decoded) != {'amount', 'currency'}:
        _fail(field, 'Money needs a finite nonnegative amount and an ISO currency.')
    return {'amount': _amount_text(decoded['amount'], field), 'currency': _currency(decoded['currency'], field)}


def _whole(raw: Any, field: str, low: int, high: int) -> int:
    if type(raw) is not int or raw < low or raw > high:
        _fail(field, 'Expected a whole number inside the finite bounds.')
    return raw


def _sequence(raw: Any, field: str) -> list[Any]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        _fail(field, 'Expected a list.')
    return list(raw)


def _unique_labels(raw: Any, field: str, low: int, high: int) -> list[str]:
    items = _sequence(raw, field)
    if not low <= len(items) <= high:
        _fail(field, 'The list length is outside the supported bounds.')
    labels = [_label(item, field) for item in items]
    if len(set(labels)) != len(labels):
        _fail(field, 'Duplicate list values are not supported.')
    return labels


def _regions(raw: Any, field: str) -> list[str]:
    labels = _unique_labels(raw, field, 0, 32)
    if any(label.casefold() in _WORLDWIDE for label in labels):
        _fail(field, 'Empty regions need an explicit broad-scope acknowledgment; worldwide is not a region.')
    return labels


def _intakes(raw: Any, field: str) -> list[str]:
    items = _sequence(raw, field)
    if not 1 <= len(items) <= 16:
        _fail(field, 'At least one intake window is required.')
    windows: list[str] = []
    for item in items:
        if not isinstance(item, str) or _INTAKE.fullmatch(item.strip().lower()) is None:
            _fail(field, 'Intake windows use YYYY-season, such as 2028-fall.')
        windows.append(item.strip().lower())
    if len(set(windows)) != len(windows):
        _fail(field, 'Duplicate intake windows are not supported.')
    return windows


def _components(raw: Any, field: str) -> dict[str, bool]:
    if not isinstance(raw, dict) or set(raw) != set(COMPONENT_KEYS):
        _fail(field, 'Required components need an explicit boolean for every component.')
    parsed = {key: raw[key] for key in COMPONENT_KEYS}
    if any(type(value) is not bool for value in parsed.values()):
        _fail(field, 'Required components need an explicit boolean for every component.')
    return parsed


def _living_rule(raw: Any, field: str) -> dict[str, Any]:
    keys = {'basis', 'count_housing_once', 'count_insurance_once', 'numeric_floor'}
    if not isinstance(raw, dict) or set(raw) != keys:
        _fail(field, 'Living adequacy needs a basis, two explicit counting flags, and a numeric floor or null.')
    if raw['basis'] != 'same_location_period':
        _fail(field, 'Living adequacy compares support with costs for the same location and period.')
    if type(raw['count_housing_once']) is not bool or type(raw['count_insurance_once']) is not bool:
        _fail(field, 'Housing and insurance counting flags must be explicit booleans.')
    floor = raw['numeric_floor']
    return {
        'basis': 'same_location_period',
        'count_housing_once': raw['count_housing_once'],
        'count_insurance_once': raw['count_insurance_once'],
        'numeric_floor': None if floor is None else _money(floor, field),
    }


def _citizenship(raw: Any, field: str) -> str:
    text = _label(raw, field)
    if text.casefold() in _WORLDWIDE:
        _fail(field, 'Citizenship must be one short plain label.')
    return text


def _prior_degree(raw: Any, field: str) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {'level', 'field'}:
        _fail(field, 'Prior degree needs a level and a field.')
    return {'level': _enum(raw['level'], field, PRIOR_LEVELS), 'field': _label(raw['field'], field)}


def _academic(raw: Any, field: str) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {'grade', 'scale'}:
        _fail(field, 'An academic record needs the original grade and scale, without a conversion.')
    grade = _amount_text(raw['grade'], field)
    scale = _amount_text(raw['scale'], field)
    if Decimal(scale) <= 0 or Decimal(grade) > Decimal(scale):
        _fail(field, 'The grade must sit on the supplied scale.')
    return {'grade': grade, 'scale': scale}


def _calendar_date(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or _DATE.fullmatch(raw) is None:
        _fail(field, 'Test dates use YYYY-MM-DD.')
    try:
        dt.date.fromisoformat(raw)
    except ValueError:
        _fail(field, 'Test dates use a real YYYY-MM-DD calendar date.')
    return raw


def _tests(raw: Any, field: str) -> list[dict[str, Any]]:
    items = _sequence(raw, field)
    if not 1 <= len(items) <= 20:
        _fail(field, 'Supplied test records need at least one complete result.')
    parsed = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {'test', 'date', 'score'}:
            _fail(field, 'Each test record needs test, date, and score.')
        score = item['score']
        if not isinstance(score, dict) or set(score) != {'value', 'scale'}:
            _fail(field, 'Each test score needs a value and a scale. No score is guessed.')
        value = _amount_text(score['value'], field)
        scale = _amount_text(score['scale'], field)
        if Decimal(scale) <= 0:
            _fail(field, 'A test scale must be positive.')
        parsed.append({
            'test': _label(item['test'], field),
            'date': _calendar_date(item['date'], field),
            'score': {'value': value, 'scale': scale},
        })
    return parsed


def _canonical_decimal(text: str) -> bool:
    """Digits with an optional fraction, no exponent, and no leading zeros."""
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


def _share(raw: Any, field: str) -> int | str:
    """Return an integer or canonical decimal string. Floats are normalized."""
    message = 'Exploration share must be an integer or canonical decimal string from 0 to 1.'
    amount: Decimal | None = None
    if isinstance(raw, bool):
        amount = None
    elif isinstance(raw, int):
        amount = Decimal(raw)
    elif isinstance(raw, float) and math.isfinite(raw):
        amount = Decimal(str(raw))
    elif isinstance(raw, Decimal) and raw.is_finite():
        amount = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if _canonical_decimal(text):
            try:
                parsed = Decimal(text)
            except InvalidOperation:
                parsed = None
            if parsed is not None and parsed.is_finite():
                amount = parsed
    if amount is None or amount < 0 or amount > 1:
        _fail(field, message)
    if amount == 0:
        return 0
    rendered = format(amount, 'f')
    if '.' in rendered:
        rendered = rendered.rstrip('0').rstrip('.')
    if '.' not in rendered:
        return int(rendered)
    return rendered


def _execution_policy(raw: Any, field: str) -> dict[str, Any]:
    keys = {'lifetime_limits', *POLICY_STRUCTURAL, 'exploration_share', 'usage_mode'}
    if not isinstance(raw, dict) or set(raw) != keys:
        _fail(field, 'Execution policy needs finite lifetime ceilings and structural limits.')
    limits = raw['lifetime_limits']
    if not isinstance(limits, dict) or set(limits) != set(POLICY_COUNTERS):
        _fail(field, 'Lifetime limits need queued_nodes, visited_nodes, fetches, queries, and model_tokens.')
    ceilings = {counter: limits[counter] for counter in POLICY_COUNTERS}
    structural_caps = {
        'max_depth': 64,
        'max_queue': 1_000_000,
        'per_domain_inflight': 10_000,
        'max_hypotheses_per_wave': 10_000,
    }
    if any(type(value) is not int or value < 0 or value > 1_000_000_000 for value in ceilings.values()):
        _fail(field, 'Lifetime ceilings must be finite nonnegative integers.')
    structural = {}
    for name, cap in structural_caps.items():
        structural[name] = _whole(raw[name], field, 0, cap)
    if raw['usage_mode'] not in ('estimated', 'hard'):
        _fail(field, 'Usage mode must be estimated or hard.')
    return {
        'lifetime_limits': ceilings,
        **structural,
        'exploration_share': _share(raw['exploration_share'], field),
        'usage_mode': raw['usage_mode'],
    }


def _monitoring(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {'permitted_work', 'max_refreshes', 'interval_days'}:
        _fail(field, 'Monitoring needs permitted refresh work and finite refresh ceilings.')
    work = _sequence(raw['permitted_work'], field)
    if not work or len(work) > len(WORK_TOKENS) or any(item not in WORK_TOKENS for item in work) or len(set(work)) != len(work):
        _fail(field, 'Permitted refresh work must use the supported actions.')
    return {
        'permitted_work': work,
        'max_refreshes': _whole(raw['max_refreshes'], field, 1, 100_000),
        'interval_days': _whole(raw['interval_days'], field, 1, 3660),
    }


def validate_answer(question: Question, raw: Any) -> Any:
    if not isinstance(question, Question):
        _fail('field_key', 'Unknown interview field.')
    value = _decode(raw)
    field = question.field_key
    kind = question.answer_type
    if kind == 'degree_level':
        return _enum(value, field, DEGREES)
    if kind == 'modality':
        return _enum(value, field, MODALITIES)
    if kind == 'string_list':
        labels = _unique_labels(value, field, 1, 32)
        return labels
    if kind == 'region_list':
        return _regions(value, field)
    if kind == 'intake_list':
        return _intakes(value, field)
    if kind == 'bool':
        parsed = _explicit_bool(value, field, allow_none=False)
        if type(parsed) is not bool:
            _fail(field, 'A boolean requires an explicit yes/no or true/false answer.')
        return parsed
    if kind == 'optional_bool':
        return _explicit_bool(value, field, allow_none=True)
    if kind == 'components':
        return _components(value, field)
    if kind == 'money':
        return _money(value, field)
    if kind == 'living_rule':
        return _living_rule(value, field)
    if kind == 'hours':
        return _whole(value, field, 0, 168)
    if kind == 'citizenship':
        return _citizenship(value, field)
    if kind == 'prior_degree':
        return _prior_degree(value, field)
    if kind == 'academic_record':
        return _academic(value, field)
    if kind == 'tests':
        return _tests(value, field)
    if kind == 'positive_int':
        return _whole(value, field, 1, 100_000)
    if kind == 'execution_policy':
        return _execution_policy(value, field)
    if kind == 'monitoring':
        return _monitoring(value, field)
    _fail(field, 'Unsupported answer type.')
    return None


def _require_visibility(question: Question, visibility: str) -> None:
    if visibility != question.visibility_default:
        if question.visibility_default == 'public_ok':
            _fail(question.field_key, 'A required public field is hidden, so it cannot enter the public frame.')
        _fail(question.field_key, 'This field must stay private.')


def _deferred_private(question: Question, entry: Mapping[str, Any], enabled: bool) -> bool:
    return question.field_key in PRIVATE_ELIGIBILITY_FIELDS and not enabled and not _is_confirmed(entry)


def _canonical_snapshot(fields: Any) -> dict[str, Any]:
    snapshot = _freeze_snapshot(fields)
    missing = next_question(snapshot)
    if missing is not None:
        _fail(missing.field_key, 'Confirm required interview fields before building a contract.')
    for key in snapshot:
        question_for(key)
    enabled = _private_evaluation_enabled(_confirmed_map(snapshot))
    canonical: dict[str, Any] = {}
    for question in QUESTIONS:
        entry = snapshot.get(question.field_key)
        if entry is None:
            continue
        if _deferred_private(question, entry, enabled):
            continue
        if not _is_confirmed(entry):
            _fail(question.field_key, 'Imported, default, and unconfirmed values cannot build a contract.')
        _require_visibility(question, entry['visibility'])
        canonical[question.field_key] = validate_answer(question, copy.deepcopy(entry['value']))
    return canonical


def _public_objective(degree: str, modality: str, tags: list[str], regions: list[str],
                      intakes: list[str]) -> str:
    scope = ', '.join(regions) if regions else 'explicitly acknowledged broad scope'
    return (
        f'{DOMAIN}: {degree} {modality} programs in {", ".join(tags)}; '
        f'scope {scope}; intakes {", ".join(intakes)}'
    )


def build_contract(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Build the private contract body from confirmed answers without changing them."""
    values = _canonical_snapshot(fields)
    regions = values['regions']
    if regions == []:
        if values.get('scope_broad_acknowledged') is not True:
            _fail('scope_broad_acknowledged', 'Empty regions require an explicit confirmed bounded broad-scope acknowledgment.')
        broad = True
    else:
        broad = values.get('scope_broad_acknowledged') is True
    for key in ('degree_level', 'modality', 'field_tags', 'intake_windows', 'required_components',
                'grants_only', 'completion_min_candidates', 'execution_policy', 'monitoring',
                'consent_private_evaluation'):
        if key not in values:
            _fail(key, 'Confirm required interview fields before building a contract.')
    if values['consent_private_evaluation'] is True:
        for key in PRIVATE_REQUIRED_FIELDS:
            if key not in values:
                _fail(key, 'Confirm required interview fields before building a contract.')
    elif values['consent_private_evaluation'] is not False:
        _fail('consent_private_evaluation', 'Private evaluation requires an explicit yes or no.')
    return {
        'objective': {
            'domain': DOMAIN,
            'degree_level': values['degree_level'],
            'modality': values['modality'],
            'field_tags': list(values['field_tags']),
        },
        'scope': {
            'regions': list(regions),
            'intake_windows': list(values['intake_windows']),
            'broad_acknowledged': broad,
        },
        'funding': {
            'required_components': values['required_components'],
            'grants_only': values['grants_only'],
            'accept_conditional_ra_ta': values.get('accept_conditional_ra_ta'),
            'accept_competitive_awards': values.get('accept_competitive_awards'),
            'threshold_refs': list(THRESHOLD_REFS),
        },
        'completion': {
            'min_candidates': values['completion_min_candidates'],
            'critical_fields': list(CRITICAL_SOURCE_FIELDS),
        },
        'monitoring': {'refresh_policy': values['monitoring']},
        'execution_policy': values['execution_policy'],
    }


def compile_public_frame(contract_body: Any, approved_snapshot: Any) -> PublicFrame:
    """Project a frozen snapshot into a public frame. The snapshot is not modified."""
    if not isinstance(contract_body, dict) or not isinstance(contract_body.get('objective'), dict):
        _fail('body', 'Contract must be an object.')
    if contract_body['objective'].get('domain') != DOMAIN:
        _fail('domain', 'Only the masters_funding profile is supported.')
    frozen = _freeze_snapshot(approved_snapshot)
    enabled = _private_evaluation_enabled(_confirmed_map(frozen))
    for key, entry in frozen.items():
        question = question_for(key)
        if _deferred_private(question, entry, enabled):
            continue
        if not _is_confirmed(entry):
            _fail(key, 'Approved public projection requires user-confirmed fields.')
        _require_visibility(question, entry['visibility'])
    expected = build_contract(frozen)
    if contract_body != expected:
        _fail('body', 'Contract criteria must match the confirmed interview fields.')
    objective = expected['objective']
    scope = expected['scope']
    funding = expected['funding']
    if objective.get('domain') != DOMAIN:
        _fail('domain', 'Only the masters_funding profile is supported.')
    regions = list(scope['regions'])
    if regions == [] and scope['broad_acknowledged'] is not True:
        _fail('scope_broad_acknowledged', 'Empty regions require an explicit confirmed bounded broad-scope acknowledgment.')
    degree = objective['degree_level']
    modality = objective['modality']
    tags = list(objective['field_tags'])
    intakes = list(scope['intake_windows'])
    criteria = {
        'degree_level': degree,
        'modality': modality,
        'required_components': copy.deepcopy(funding['required_components']),
        'grants_only': funding['grants_only'],
    }
    return PublicFrame(
        domain=DOMAIN,
        public_objective=_public_objective(degree, modality, tags, regions, intakes),
        public_criteria=criteria,
        field_tags=tags,
        regions=regions,
        intake_windows=intakes,
        required_source_fields=list(CRITICAL_SOURCE_FIELDS),
        frame_version=1,
    )
