"""Qualification behavior against a real fresh v4 store and reviewed evidence.

Synthetic institutions and amounts only. Every observation is produced through
the snapshot, quote, review, package and evaluation path.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def _load(name: str):
    canonical = "snowball_" + name
    path = TOOLS / (name + ".py")
    loaded = sys.modules.get(canonical)
    if loaded is not None and Path(loaded.__file__).resolve() == path.resolve():
        return loaded
    spec = importlib.util.spec_from_file_location(canonical, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    spec.loader.exec_module(module)
    return module


goal = _load("goal")
interview = _load("interview")
qualify = _load("qualify")
orchestrator = _load("orchestrator")


POLICY = {
    "lifetime_limits": {
        "fetches": 20, "queries": 20, "model_tokens": 40000,
        "queued_nodes": 20, "visited_nodes": 20,
    },
    "max_depth": 2,
    "max_queue": 10,
    "per_domain_inflight": 1,
    "max_hypotheses_per_wave": 2,
    "exploration_share": 0.2,
    "usage_mode": "estimated",
}
LIMITS = {
    "day": {"fetches": 5, "queries": 5, "model_tokens": 10000},
    "lifetime": POLICY["lifetime_limits"],
    "max_depth": 2,
    "max_queue": 10,
    "per_domain_inflight": 1,
    "max_hypotheses_per_wave": 2,
    "exploration_share": "0.2",
    "usage_mode": "estimated",
}


def _field(value, visibility):
    return interview.ProfileField("temporary", value, "known", "user_confirmed", visibility, 0)


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "data" / "qualify.db"
        self.store = orchestrator.Store.open(self.db_path)
        self.conn = self.store.db

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def _answer(self, selected, key, value, visibility):
        current = goal.get_goal(self.store, selected.id)
        return goal.record_answer(
            self.store, selected.id, key, value, True, visibility, current.row_version, "fixture",
        )

    def _running(self, slug, fields):
        selected = goal.create_goal(self.store, slug, "Synthetic qualification goal", "fixture")
        for key, value, visibility in fields:
            self._answer(selected, key, value, visibility)
            selected = goal.get_goal(self.store, selected.id)
        selected = goal.get_goal(self.store, selected.id)
        body = interview.build_contract(goal.fields(self.store, selected.id))
        proposed = goal.propose_contract(
            self.store, selected.id, body, selected.current_contract_version, "fixture",
            expected_row_version=selected.row_version,
        )
        selected = goal.get_goal(self.store, selected.id)
        selected = goal.approve_contract(
            self.store, selected.id, proposed.version, proposed.body_hash,
            selected.row_version, "fixture", slug + "-approve",
        )
        goal.grant_execution(
            self.store, selected.id, proposed.version, "research", LIMITS,
            int(time.time()) + 3600, selected.row_version, "fixture", slug + "-grant",
        )
        selected = goal.get_goal(self.store, selected.id)
        selected = goal.transition(
            self.store, selected.id, "start", "", selected.row_version, "fixture", slug + "-start",
        )
        return selected

    def _base_fields(self, intake="2030-fall", cash="5000", upfront="20000", conditional=False, competitive=False, regions=None, degree="masters", modality="on_campus"):
        if regions is None:
            regions = ["FI"]
        components = {
            "tuition": True,
            "mandatory_fees": True,
            "living_amount": True,
            "living_duration": True,
            "health_insurance": True,
            "visa_costs": True,
            "application_costs": True,
            "travel_costs": True,
            "deposits": True,
            "proof_of_funds_liquidity": True,
        }
        fields = [
            ("degree_level", degree, "public_ok"),
            ("modality", modality, "public_ok"),
            ("field_tags", ["hydrology"], "public_ok"),
            ("regions", regions, "public_ok"),
        ]
        if regions == []:
            fields.append(("scope_broad_acknowledged", True, "public_ok"))
        fields.extend([
            ("intake_windows", [intake], "public_ok"),
            ("required_components", components, "public_ok"),
            ("grants_only", True, "public_ok"),
            ("accept_conditional_ra_ta", conditional, "private"),
            ("accept_competitive_awards", competitive, "private"),
            ("max_net_total_cost", {"amount": cash, "currency": "EUR"}, "private"),
            ("max_upfront_cash", {"amount": upfront, "currency": "EUR"}, "private"),
            ("min_living_adequacy_rule", {
                "basis": "same_location_period",
                "count_housing_once": True,
                "count_insurance_once": True,
                "numeric_floor": None,
            }, "private"),
            ("max_work_hours_acceptable", 0, "private"),
            ("allow_loans", False, "private"),
            ("citizenship", "FI", "private"),
            ("prior_degree", {"level": "bachelors", "field": "hydrology"}, "private"),
            ("completion_min_candidates", 1, "public_ok"),
            ("execution_policy", POLICY, "public_ok"),
            ("monitoring", {
                "permitted_work": ["recheck_official_sources"],
                "max_refreshes": 1,
                "interval_days": 30,
            }, "public_ok"),
            ("consent_private_evaluation", True, "private"),
        ])
        return fields

    def _page(self, url):
        host = url.split("/")[2]
        now = int(time.time())
        with self.store.tx() as db:
            db.execute("INSERT OR IGNORE INTO domains(domain) VALUES (?)", (host,))
            db.execute(
                """INSERT INTO pages (
                       canon_url, domain, status, next_check_at, interval_sec, discovered_via, created_at
                   ) VALUES (?, ?, 'fetched', ?, 86400, 'seed', ?)""",
                (url, host, now, now),
            )
            page_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute(
                """INSERT INTO fetches (page_id, at, route, outcome, due)
                   VALUES (?, ?, 'direct', 'ok', ?)""",
                (page_id, now, now),
            )
            fetch_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        return page_id, fetch_id

    def _observe(self, selected, url, text, program, observations, cycle=None, applicability=None, original=None, span=None):
        page_id, fetch_id = self._page(url)
        snapshot_id = qualify.record_snapshot(
            self.store, page_id, text, fetch_id, public_fetch=True,
            source_cycle_label=cycle, applicability=applicability,
            original_bytes=original, text_span=span,
        )
        binding = {
            "goal_id": selected.id,
            "contract_version": selected.current_contract_version,
            "strategy_version": selected.current_strategy_version,
            "epoch": selected.epoch,
        }
        ids = qualify.record_observations(
            self.store, snapshot_id, program, observations, binding, "extractor-test", "prompt-v1",
        )
        qualify.record_reviews(self.store, ids, ["keep"] * len(ids), review_model="reviewer-test")
        return snapshot_id, ids

    def _quote(self, text, snippet):
        start = text.index(snippet)
        return start, start + len(snippet)

    def _add(self, text, observations, field, value, snippet, subject, period=None):
        start, end = self._quote(text, snippet)
        observations.append({
            "field_key": field,
            "value": value,
            "quote": snippet,
            "quote_start": start,
            "quote_end": end,
            "period": period,
            "currency": value.get("currency") if isinstance(value, dict) else None,
            "subject": subject,
        })

    def _bound(self, snippet):
        return snippet + " for 2030-fall at Otaniemi"

    def _complete_source(self, award_type="guaranteed", award_amount="12000", acceptance=None, degree="masters", modality_phrase="on campus"):
        label = "conditional ra/ta" if award_type == "conditional_ra_ta" else award_type.replace("_", " ")
        parts = [
            "Nordic Water Masters publishes the hydrology programme in FI.",
            self._bound("The degree level is " + degree),
            self._bound("The modality is " + modality_phrase),
            self._bound("The field of study is hydrology"),
            self._bound("The region is FI"),
            self._bound("The intake cycle is 2030-fall"),
            self._bound("The cycle dates are 2030-fall"),
            self._bound("The campus is Otaniemi"),
            self._bound("AWARD-A official funding source is Nordic Water Office"),
            self._bound("Eligibility text is open hydrology graduates"),
            self._bound("Duration is 24 months and 4 semesters"),
            self._bound("Tuition is 10000 EUR total on 2030-08-01"),
            self._bound("Mandatory fees are 500 EUR total on 2030-08-01"),
            self._bound("Living cost is 12000 EUR total on 2030-09-01"),
            self._bound("Health insurance is 300 EUR total on 2030-08-01"),
            self._bound("Visa cost is 200 EUR total on 2030-08-01"),
            self._bound("Application fee is 100 EUR total on 2030-07-01"),
            self._bound("Travel relocation is 400 EUR total on 2030-08-01"),
            self._bound("Deposit is 250 EUR total on 2030-07-01"),
            self._bound("Proof of funds is 1000 EUR total"),
            self._bound("Award AWARD-A is " + label),
            self._bound("AWARD-A tuition coverage is 10000 EUR total on 2030-08-01"),
            self._bound("AWARD-A mandatory fees coverage is 500 EUR total on 2030-08-01"),
            self._bound("AWARD-A living support amount is " + award_amount + " EUR total on 2030-09-01"),
            self._bound("AWARD-A health insurance coverage is 300 EUR total on 2030-08-01"),
            self._bound("AWARD-A visa coverage is 200 EUR total on 2030-08-01"),
            self._bound("AWARD-A travel coverage is 400 EUR total on 2030-08-01"),
        ]
        if acceptance:
            parts.append(self._bound(acceptance))
        return " ".join(parts)

    def _complete_observations(self, text, award_type="guaranteed", award_amount="12000", degree="masters", modality="on_campus", modality_phrase="on campus"):
        observations = []
        base = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        award = dict(base, award_key="AWARD-A")
        self._add(text, observations, "degree_level", degree, self._bound("The degree level is " + degree), base)
        self._add(text, observations, "modality", modality, self._bound("The modality is " + modality_phrase), base)
        self._add(text, observations, "field_of_study", ["hydrology"], self._bound("The field of study is hydrology"), base)
        self._add(text, observations, "region", "FI", self._bound("The region is FI"), base)
        self._add(text, observations, "intake_cycle", "2030-fall", self._bound("The intake cycle is 2030-fall"), base)
        self._add(text, observations, "cycle_dates", "2030-fall", self._bound("The cycle dates are 2030-fall"), base)
        self._add(text, observations, "campus", "Otaniemi", self._bound("The campus is Otaniemi"), base)
        self._add(text, observations, "funding_source_official", "Nordic Water Office", self._bound("AWARD-A official funding source is Nordic Water Office"), award)
        self._add(text, observations, "eligibility_text", "open hydrology graduates", self._bound("Eligibility text is open hydrology graduates"), base)
        self._add(text, observations, "program_duration", {"duration_months": 24, "semesters": 4}, self._bound("Duration is 24 months and 4 semesters"), base)
        money = [
            ("tuition", "10000", "2030-08-01", "Tuition is 10000 EUR total on 2030-08-01", base),
            ("mandatory_fees", "500", "2030-08-01", "Mandatory fees are 500 EUR total on 2030-08-01", base),
            ("living_costs", "12000", "2030-09-01", "Living cost is 12000 EUR total on 2030-09-01", base),
            ("health_insurance", "300", "2030-08-01", "Health insurance is 300 EUR total on 2030-08-01", base),
            ("visa_costs", "200", "2030-08-01", "Visa cost is 200 EUR total on 2030-08-01", base),
            ("application_fee", "100", "2030-07-01", "Application fee is 100 EUR total on 2030-07-01", base),
            ("travel_relocation", "400", "2030-08-01", "Travel relocation is 400 EUR total on 2030-08-01", base),
            ("deposit", "250", "2030-07-01", "Deposit is 250 EUR total on 2030-07-01", base),
            ("proof_of_funds", "1000", None, "Proof of funds is 1000 EUR total", base),
        ]
        for field, amount, when, snippet, subject in money:
            value = {"amount": amount, "currency": "EUR", "period": "total"}
            if when:
                value["date"] = when
            self._add(text, observations, field, value, self._bound(snippet), subject, "total")
        self._add(text, observations, "award_type", award_type, self._bound("Award AWARD-A is " + ("conditional ra/ta" if award_type == "conditional_ra_ta" else award_type.replace("_", " "))), award)
        covered = [
            ("tuition_coverage", "10000", "2030-08-01", "AWARD-A tuition coverage is 10000 EUR total on 2030-08-01"),
            ("mandatory_fees_coverage", "500", "2030-08-01", "AWARD-A mandatory fees coverage is 500 EUR total on 2030-08-01"),
            ("living_support_amount", award_amount, "2030-09-01", "AWARD-A living support amount is " + award_amount + " EUR total on 2030-09-01"),
            ("health_insurance_coverage", "300", "2030-08-01", "AWARD-A health insurance coverage is 300 EUR total on 2030-08-01"),
            ("visa_coverage", "200", "2030-08-01", "AWARD-A visa coverage is 200 EUR total on 2030-08-01"),
            ("travel_coverage", "400", "2030-08-01", "AWARD-A travel coverage is 400 EUR total on 2030-08-01"),
        ]
        for field, amount, when, snippet in covered:
            self._add(
                text, observations, field,
                {"amount": amount, "currency": "EUR", "period": "total", "date": when},
                self._bound(snippet), award, "total",
            )
        return observations

    def _ready_package(self, slug="ready-goal", **field_kwargs):
        degree = field_kwargs.get("degree", "masters")
        modality = field_kwargs.get("modality", "on_campus")
        modality_phrase = {"on_campus": "on campus", "online": "online", "hybrid": "hybrid"}[modality]
        selected = self._running(slug, self._base_fields(**field_kwargs))
        text = self._complete_source(degree=degree, modality_phrase=modality_phrase)
        program = {"key": "nordic-water", "name": "Nordic Water Masters", "official_domain": "example.edu"}
        observations = self._complete_observations(
            text, degree=degree, modality=modality, modality_phrase=modality_phrase,
        )
        self._observe(selected, "https://example.edu/" + slug, text, program, observations, cycle="2030-fall")
        packages = qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall")
        chosen = [item for item in packages if item.award_keys == ("AWARD-A",)][0]
        results = qualify.evaluate(self.store, selected, selected.current_contract_version, chosen.key, selected.epoch)
        return selected, chosen, results

    def test_positive_reviewed_package_is_ready_and_not_an_offer(self):
        selected, package, results = self._ready_package()
        self.assertTrue(all(item.status == "met" for item in results))
        self.assertEqual(Decimal(package.cash_flow["net_cost"]), Decimal("350.00"))
        self.assertEqual(Decimal(package.cash_flow["peak_own_cash"]), Decimal("12350.00"))
        self.assertNotIn("20000", json.dumps(package.components))
        self.assertNotIn("5000", json.dumps(package.cash_flow))
        status = qualify.completion_status(self.store, selected)
        self.assertIsInstance(status.detail, dict)
        self.assertTrue(status.ready)
        self.assertEqual(status.substate, "research_ready")
        self.assertTrue(status.detail["research_not_offer"])
        self.assertEqual(status.detail["eligible_count"], 1)
        self.assertFalse(status.detail["eligible_packages"][0]["award_receipt"])

    def test_code_checked_pending_and_unavailable_never_qualify(self):
        selected = self._running("pending-goal", self._base_fields())
        text = self._complete_source()
        program = {"key": "nordic-water", "name": "Nordic Water Masters"}
        page_id, fetch_id = self._page("https://example.edu/pending")
        snapshot_id = qualify.record_snapshot(self.store, page_id, text, fetch_id, public_fetch=True, source_cycle_label="2030-fall")
        binding = {
            "goal_id": selected.id,
            "contract_version": selected.current_contract_version,
            "strategy_version": selected.current_strategy_version,
            "epoch": selected.epoch,
        }
        ids = qualify.record_observations(
            self.store, snapshot_id, program, self._complete_observations(text), binding, "extractor-test", "prompt-v1",
        )
        self.assertEqual(qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall"), [])
        qualify.record_reviews(self.store, ids[:1], [None], review_model="reviewer-test")
        self.assertEqual(self.conn.execute("SELECT assurance FROM observations WHERE id=?", (ids[0],)).fetchone()[0], "review_pending")
        qualify.record_reviews(self.store, ids[1:2], None, unavailable=True)
        self.assertEqual(self.conn.execute("SELECT assurance FROM observations WHERE id=?", (ids[1],)).fetchone()[0], "review_unavailable")
        status = qualify.completion_status(self.store, selected)
        self.assertFalse(status.ready)
        self.assertIn(status.substate, ("partial_coverage", "verifier_unavailable"))

    def test_incompatible_awards_cannot_make_a_full_package(self):
        selected = self._running("stack-goal", self._base_fields(cash="1000", upfront="1000"))
        text = " ".join([
            "Nordic Water Masters publishes separate awards.",
            self._bound("The degree level is masters"),
            self._bound("The modality is on campus"),
            self._bound("The field of study is hydrology"),
            self._bound("The intake cycle is 2030-fall"),
            self._bound("Duration is 24 months and 4 semesters"),
            self._bound("Tuition is 10000 EUR per year"),
            self._bound("Living cost is 12000 EUR per year"),
            self._bound("Award AWARD-A is guaranteed"),
            self._bound("AWARD-A official funding source is Nordic Water Office"),
            self._bound("AWARD-A tuition coverage is 10000 EUR per year"),
            self._bound("Award AWARD-B is guaranteed"),
            self._bound("AWARD-B official funding source is Nordic Water Office"),
            self._bound("AWARD-B living support amount is 12000 EUR per year"),
            self._bound("AWARD-A and AWARD-B cannot be combined"),
        ])
        observations = []
        base = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        award_a = dict(base, award_key="AWARD-A")
        award_b = dict(base, award_key="AWARD-B")
        self._add(text, observations, "degree_level", "masters", self._bound("The degree level is masters"), base)
        self._add(text, observations, "modality", "on_campus", self._bound("The modality is on campus"), base)
        self._add(text, observations, "field_of_study", ["hydrology"], self._bound("The field of study is hydrology"), base)
        self._add(text, observations, "intake_cycle", "2030-fall", self._bound("The intake cycle is 2030-fall"), base)
        self._add(text, observations, "program_duration", {"duration_months": 24, "semesters": 4}, self._bound("Duration is 24 months and 4 semesters"), base)
        self._add(text, observations, "tuition", {"amount": "10000", "currency": "EUR", "period": "per_year"}, self._bound("Tuition is 10000 EUR per year"), base, "per_year")
        self._add(text, observations, "living_costs", {"amount": "12000", "currency": "EUR", "period": "per_year"}, self._bound("Living cost is 12000 EUR per year"), base, "per_year")
        self._add(text, observations, "award_type", "guaranteed", self._bound("Award AWARD-A is guaranteed"), award_a)
        self._add(text, observations, "funding_source_official", "Nordic Water Office", self._bound("AWARD-A official funding source is Nordic Water Office"), award_a)
        self._add(text, observations, "tuition_coverage", {"amount": "10000", "currency": "EUR", "period": "per_year"}, self._bound("AWARD-A tuition coverage is 10000 EUR per year"), award_a, "per_year")
        self._add(text, observations, "award_type", "guaranteed", self._bound("Award AWARD-B is guaranteed"), award_b)
        self._add(text, observations, "funding_source_official", "Nordic Water Office", self._bound("AWARD-B official funding source is Nordic Water Office"), award_b)
        self._add(text, observations, "living_support_amount", {"amount": "12000", "currency": "EUR", "period": "per_year"}, self._bound("AWARD-B living support amount is 12000 EUR per year"), award_b, "per_year")
        self._add(text, observations, "stacking_policy", {"relation": "forbidden", "award_keys": ["AWARD-A", "AWARD-B"]}, self._bound("AWARD-A and AWARD-B cannot be combined"), base)
        self._observe(selected, "https://example.edu/stack", text, {"key": "nordic-water", "name": "Nordic Water Masters"}, observations, cycle="2030-fall")
        packages = qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall")
        self.assertFalse(any(set(item.award_keys) == {"AWARD-A", "AWARD-B"} for item in packages))
        by_awards = {item.award_keys: item for item in packages}
        self.assertEqual(Decimal(by_awards[("AWARD-A",)].cash_flow["net_cost"]), Decimal("24000.00"))
        self.assertEqual(Decimal(by_awards[("AWARD-B",)].cash_flow["net_cost"]), Decimal("20000.00"))

    def test_unknown_duration_source_cycle_and_fx_stay_unknown(self):
        selected = self._running("unknown-goal", self._base_fields())
        text = "Nordic Water Masters tuition is 10 EUR. Living support amount is 20 USD."
        observations = []
        subject = {"program_key": "nordic-water", "campus": "Otaniemi"}
        self._add(text, observations, "tuition", {"amount": "10", "currency": "EUR"}, "tuition is 10 EUR", subject)
        page_id, fetch_id = self._page("https://example.edu/unknown")
        snapshot_id = qualify.record_snapshot(
            self.store, page_id, text, fetch_id, public_fetch=True, source_cycle_label="2030-fall",
            applicability={"cycle_label": "2030-fall"},
        )
        stored = self.conn.execute(
            "SELECT source_cycle_label, applicability_json FROM source_snapshots WHERE id=?",
            (snapshot_id,),
        ).fetchone()
        self.assertIsNone(stored[0])
        self.assertEqual(json.loads(stored[1]), {})
        flow = qualify.compute_cash_flow(
            [
                {"kind": "cost", "amount": "10", "currency": "EUR", "period": "per_month"},
                {"kind": "cost", "amount": "20", "currency": "USD", "period": "one_time", "date": "2030-01-01"},
            ],
            {"currency": "EUR", "fx_rates": []},
        )
        self.assertIsNone(flow["net_cost"])
        self.assertIn("duration", flow["unknown_reasons"])
        self.assertIn("fx", flow["unknown_reasons"])

    def test_deposit_credit_is_not_double_counted_and_proof_is_not_a_fee(self):
        proof = {"proof_of_funds": {"amount": "1000.00", "currency": "EUR"}}
        credited = qualify.compute_cash_flow(
            [
                {"kind": "cost", "amount": "1000.00", "currency": "EUR", "period": "total", "date": "2030-02-01", "identity": "tuition"},
                {"kind": "deposit", "amount": "250.00", "currency": "EUR", "period": "one_time", "date": "2030-01-01", "credited_to": "tuition"},
            ],
            proof,
        )
        refundable = qualify.compute_cash_flow(
            [
                {"kind": "cost", "amount": "1000.00", "currency": "EUR", "period": "total", "date": "2030-02-01", "identity": "tuition"},
                {"kind": "cost", "amount": "250.00", "currency": "EUR", "period": "one_time", "date": "2030-01-01", "identity": "deposit"},
                {"kind": "refund", "amount": "250.00", "currency": "EUR", "period": "one_time", "date": "2030-03-01"},
            ],
            proof,
        )
        self.assertEqual(credited["gross_cost"], "1000.00")
        self.assertEqual(credited["net_cost"], "1000.00")
        self.assertEqual(credited["peak_own_cash"], "1000.00")
        self.assertEqual(refundable["net_cost"], "1000.00")
        self.assertEqual(refundable["peak_own_cash"], "1250.00")
        self.assertGreater(Decimal(refundable["peak_own_cash"]), Decimal(credited["peak_own_cash"]))
        self.assertNotEqual(credited["net_cost"], "2250.00")
        self.assertFalse(credited["proof_of_funds_is_expense"])

    def test_loan_discount_and_unaccepted_assistantship_are_not_full_funding(self):
        selected = self._running("loan-goal", self._base_fields())
        for award_type, slug in (("loan", "loan"), ("discount", "discount"), ("conditional_ra_ta", "rata")):
            text = self._complete_source(award_type)
            observations = self._complete_observations(text, award_type)
            program_key = slug + "-water"
            for item in observations:
                item["subject"]["program_key"] = program_key
            self._observe(
                selected, "https://example.edu/" + slug, text,
                {"key": program_key, "name": "Nordic Water Masters"}, observations, cycle="2030-fall",
            )
            packages = qualify.derive_funding_packages(self.store, program_key, "2030-fall")
            chosen = [item for item in packages if item.award_keys == ("AWARD-A",)][0]
            evaluated = qualify.evaluate(self.store, selected, selected.current_contract_version, chosen.key, selected.epoch)
            funding = [item for item in evaluated if item.constraint_key == "funding_type"][0]
            self.assertEqual(funding.status, "unmet")
        self.assertFalse(qualify.completion_status(self.store, selected).ready)

    def test_explicit_conditional_acceptance_counts_but_stays_conditional(self):
        selected = self._running("conditional-goal", self._base_fields(conditional=True))
        acceptance = "AWARD-A remains conditional on enrollment"
        text = self._complete_source("conditional_ra_ta", acceptance=acceptance)
        observations = self._complete_observations(text, "conditional_ra_ta")
        award = {"program_key": "nordic-water", "award_key": "AWARD-A", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        self._add(
            text, observations, "acceptance_status",
            {"status": "conditional", "award_key": "AWARD-A", "condition": "enrollment"},
            self._bound(acceptance), award,
        )
        self._observe(selected, "https://example.edu/conditional", text, {"key": "nordic-water", "name": "Nordic Water Masters"}, observations, cycle="2030-fall")
        chosen = [item for item in qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall") if item.award_keys == ("AWARD-A",)][0]
        results = qualify.evaluate(self.store, selected, selected.current_contract_version, chosen.key, selected.epoch)
        funding = [item for item in results if item.constraint_key == "funding_type"][0]
        self.assertEqual(funding.status, "conditional")
        status = qualify.completion_status(self.store, selected)
        self.assertTrue(status.ready)
        self.assertEqual(status.detail["eligible_packages"][0]["outcome"], "conditional")
        self.assertFalse(status.detail["eligible_packages"][0]["award_receipt"])

    def test_missing_critical_component_and_duration_are_not_ready(self):
        selected = self._running("missing-goal", self._base_fields())
        text = " ".join([
            "Nordic Water Masters leaves the duration unpublished.",
            self._bound("Tuition is 1000 EUR total on 2030-08-01"),
            self._bound("Award AWARD-A is guaranteed"),
            self._bound("AWARD-A official funding source is Nordic Water Office"),
            self._bound("AWARD-A tuition coverage is 1000 EUR total on 2030-08-01"),
        ])
        observations = []
        base = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        award = dict(base, award_key="AWARD-A")
        self._add(text, observations, "tuition", {"amount": "1000", "currency": "EUR", "period": "total", "date": "2030-08-01"}, self._bound("Tuition is 1000 EUR total on 2030-08-01"), base, "total")
        self._add(text, observations, "award_type", "guaranteed", self._bound("Award AWARD-A is guaranteed"), award)
        self._add(text, observations, "funding_source_official", "Nordic Water Office", self._bound("AWARD-A official funding source is Nordic Water Office"), award)
        self._add(text, observations, "tuition_coverage", {"amount": "1000", "currency": "EUR", "period": "total", "date": "2030-08-01"}, self._bound("AWARD-A tuition coverage is 1000 EUR total on 2030-08-01"), award, "total")
        self._observe(selected, "https://example.edu/missing", text, {"key": "nordic-water", "name": "Nordic Water Masters"}, observations, cycle="2030-fall")
        chosen = [item for item in qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall") if item.award_keys == ("AWARD-A",)][0]
        results = qualify.evaluate(self.store, selected, selected.current_contract_version, chosen.key, selected.epoch)
        components = [item for item in results if item.constraint_key == "required_components"][0]
        self.assertEqual(components.status, "unknown")
        status = qualify.completion_status(self.store, selected)
        self.assertFalse(status.ready)
        self.assertNotEqual(status.substate, "research_ready")

    def test_scoped_contradiction_blocks_and_distinct_goals_share_a_source(self):
        first = self._running("goal-one", self._base_fields())
        program = {"key": "nordic-water", "name": "Nordic Water Masters"}
        subject = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        for url, amount in (("https://example.edu/left", "100"), ("https://example.edu/right", "200")):
            snippet = self._bound("Tuition is " + amount + " EUR total")
            text = "Nordic Water Masters states " + snippet + "."
            observations = []
            self._add(text, observations, "tuition", {"amount": amount, "currency": "EUR", "period": "total"}, snippet, subject, "total")
            self._observe(first, url, text, program, observations, cycle="2030-fall")
        qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall")
        current = goal.get_goal(self.store, first.id)
        goal.transition(self.store, first.id, "stop", "synthetic stop", current.row_version, "fixture", "goal-one-stop")
        second = self._running("goal-two", self._base_fields())
        other_snippet = self._bound("AWARD-B funding amount is 50 EUR total")
        other = "Nordic Water Masters states " + other_snippet + "."
        observations = []
        award = dict(subject, award_key="AWARD-B")
        self._add(other, observations, "funding_amount", {"amount": "50", "currency": "EUR", "period": "total"}, other_snippet, award, "total")
        self._observe(second, "https://example.edu/other", other, program, observations, cycle="2030-fall")
        contradiction = self.conn.execute(
            "SELECT field_key, winner FROM contradictions WHERE program_key='nordic-water'"
        ).fetchone()
        self.assertEqual(contradiction[0], "tuition")
        self.assertIsNone(contradiction[1])
        associations = self.conn.execute(
            "SELECT goal_id, program_key FROM goal_programs ORDER BY goal_id"
        ).fetchall()
        self.assertEqual({row[0] for row in associations}, {first.id, second.id})
        self.assertEqual({row[1] for row in associations}, {"nordic-water"})
        coverage = qualify.coverage(self.store, first.id, first.current_contract_version)
        self.assertIn("tuition", coverage.programs[0].public_contested_fields)

    def test_stale_binding_keeps_bytes_but_cannot_write_current_observations(self):
        selected = self._running("stale-goal", self._base_fields())
        text = self._complete_source()
        page_id, fetch_id = self._page("https://example.edu/stale")
        snapshot_id = qualify.record_snapshot(self.store, page_id, text, fetch_id, public_fetch=True)
        before = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        with self.assertRaises(qualify.QualificationError):
            qualify.record_observations(
                self.store, snapshot_id, {"key": "nordic-water", "name": "Nordic Water Masters"},
                self._complete_observations(text),
                {"goal_id": selected.id, "contract_version": selected.current_contract_version, "strategy_version": 99, "epoch": selected.epoch},
                "extractor-test", "prompt-v1",
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], before)
        self.assertEqual(qualify.read_snapshot(self.store, snapshot_id), text)

    def test_snapshot_hash_offset_and_original_span_are_checked(self):
        text = "Nordic Water Masters exact bytes."
        original = b"HEAD" + text.encode("utf-8") + b"TAIL"
        page_id, fetch_id = self._page("https://example.edu/bytes")
        snapshot_id = qualify.record_snapshot(
            self.store, page_id, text, fetch_id, public_fetch=True,
            original_bytes=original, text_span={"byte_start": 4, "byte_end": 4 + len(text.encode("utf-8"))},
        )
        self.assertEqual(qualify.read_snapshot(self.store, snapshot_id), text)
        row = self.conn.execute(
            "SELECT text_path, original_raw_path, content_hash FROM source_snapshots WHERE id=?",
            (snapshot_id,),
        ).fetchone()
        Path(row[0]).write_bytes(b"changed")
        with self.assertRaises(qualify.QualificationError):
            qualify.read_snapshot(self.store, snapshot_id)

    def test_target_intake_is_not_copied_from_the_request_or_fetch_date(self):
        page_id, fetch_id = self._page("https://example.edu/old-call")
        text = "Nordic Water Masters call for 2029."
        snapshot_id = qualify.record_snapshot(
            self.store, page_id, text, fetch_id, public_fetch=True,
            source_cycle_label="2030-fall", applicability={"intake": "2030-fall"},
        )
        row = self.conn.execute(
            "SELECT source_cycle_label, applicability_json, retrieved_at FROM source_snapshots WHERE id=?",
            (snapshot_id,),
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(json.loads(row[1]), {})
        self.assertNotIn("2030", row[1])

    def test_two_goal_evaluations_do_not_overwrite(self):
        first, package, first_results = self._ready_package("shared-one")
        current = goal.get_goal(self.store, first.id)
        goal.transition(self.store, first.id, "stop", "synthetic stop", current.row_version, "fixture", "shared-one-stop")
        second = self._running("shared-two", self._base_fields(cash="1"))
        with self.store.tx() as db:
            db.execute(
                """INSERT OR IGNORE INTO goal_programs (
                       goal_id, contract_version, program_key, role, visibility
                   ) VALUES (?, ?, 'nordic-water', 'candidate', 'public')""",
                (second.id, second.current_contract_version),
            )
        second_results = qualify.evaluate(self.store, second, second.current_contract_version, package.key, second.epoch)
        first_cost = [item.status for item in first_results if item.constraint_key == "net_cost"][0]
        second_cost = [item.status for item in second_results if item.constraint_key == "net_cost"][0]
        self.assertEqual(first_cost, "met")
        self.assertEqual(second_cost, "unmet")
        rows = self.conn.execute(
            """SELECT goal_id, status FROM constraint_evaluations
               WHERE package_key=? AND constraint_key='net_cost' ORDER BY goal_id""",
            (package.key,),
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(first.id, "met"), (second.id, "unmet")])

    def _approved_body(self, goal_id):
        row = self.conn.execute(
            "SELECT body_json FROM goal_contracts WHERE goal_id=? AND status='approved'",
            (goal_id,),
        ).fetchone()
        return json.loads(row[0])

    def test_completion_counts_nested_min_candidates_not_a_top_level_key(self):
        selected, _package, _results = self._ready_package("nested-min")
        body = self._approved_body(selected.id)
        self.assertEqual(body["completion"]["min_candidates"], 1)
        self.assertNotIn("min_candidates", body)
        self.assertEqual(body["completion"]["critical_fields"], list(interview.CRITICAL_SOURCE_FIELDS))
        self.assertEqual(body["objective"]["domain"], "masters_funding")
        self.assertEqual(body["funding"]["threshold_refs"], list(interview.THRESHOLD_REFS))
        self.assertNotIn("max_net_total_cost", body["funding"])
        self.assertNotIn("5000", json.dumps(body))
        status = qualify.completion_status(self.store, selected)
        self.assertIsInstance(status.detail, dict)
        self.assertTrue(status.ready)
        self.assertEqual(status.detail["min_candidates"], 1)
        body["min_candidates"] = 99
        with self.store.tx() as db:
            db.execute(
                "UPDATE goal_contracts SET body_json=? WHERE goal_id=? AND status='approved'",
                (json.dumps(body), selected.id),
            )
        self.assertTrue(qualify.completion_status(self.store, selected).ready)
        body["min_candidates"] = body["completion"].pop("min_candidates")
        with self.store.tx() as db:
            db.execute(
                "UPDATE goal_contracts SET body_json=? WHERE goal_id=? AND status='approved'",
                (json.dumps(body), selected.id),
            )
        moved = qualify.completion_status(self.store, selected)
        self.assertFalse(moved.ready)
        self.assertIsInstance(moved.detail, dict)
        self.assertEqual(moved.detail["reason"], "min_candidates_missing")

    def test_null_acceptance_stays_unknown(self):
        selected = self._running("null-accept", self._base_fields(conditional=None))
        body = self._approved_body(selected.id)
        self.assertIsNone(body["funding"]["accept_conditional_ra_ta"])
        text = self._complete_source("conditional_ra_ta")
        observations = self._complete_observations(text, "conditional_ra_ta")
        self._observe(
            selected, "https://example.edu/null-accept", text,
            {"key": "nordic-water", "name": "Nordic Water Masters"}, observations, cycle="2030-fall",
        )
        chosen = [item for item in qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall") if item.award_keys == ("AWARD-A",)][0]
        results = qualify.evaluate(self.store, selected, selected.current_contract_version, chosen.key, selected.epoch)
        funding = [item for item in results if item.constraint_key == "funding_type"][0]
        self.assertEqual(funding.status, "unknown")
        status = qualify.completion_status(self.store, selected)
        self.assertFalse(status.ready)
        self.assertIsInstance(status.detail, dict)

    def test_empty_regions_are_broad_only_when_explicitly_acknowledged(self):
        selected, _package, results = self._ready_package("broad-goal", regions=[])
        body = self._approved_body(selected.id)
        self.assertEqual(body["scope"]["regions"], [])
        self.assertIs(body["scope"]["broad_acknowledged"], True)
        region = [item for item in results if item.constraint_key == "region"][0]
        self.assertEqual(region.status, "met")
        self.assertNotIn("worldwide", region.reason)
        status = qualify.completion_status(self.store, selected)
        self.assertTrue(status.ready)
        self.assertIsInstance(status.detail, dict)
        self.assertEqual(status.substate, "research_ready")

    def test_confirmed_degree_and_modality_are_not_hardcoded(self):
        selected, _package, results = self._ready_package("enum-goal", degree="phd", modality="online")
        body = self._approved_body(selected.id)
        self.assertEqual(body["objective"]["degree_level"], "phd")
        self.assertEqual(body["objective"]["modality"], "online")
        self.assertEqual(body["objective"]["domain"], "masters_funding")
        self.assertTrue(all(item.status == "met" for item in results))
        status = qualify.completion_status(self.store, selected)
        self.assertTrue(status.ready)
        self.assertEqual(status.substate, "research_ready")

    def test_work_cap_is_read_from_the_frozen_profile(self):
        selected = self._running("hours-goal", self._base_fields())
        text = self._complete_source() + " " + self._bound("Work hours required is 10")
        observations = self._complete_observations(text)
        subject = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        self._add(text, observations, "work_hours_required", 10, self._bound("Work hours required is 10"), subject)
        self._observe(
            selected, "https://example.edu/hours", text,
            {"key": "nordic-water", "name": "Nordic Water Masters"}, observations, cycle="2030-fall",
        )
        chosen = [item for item in qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall") if item.award_keys == ("AWARD-A",)][0]
        results = qualify.evaluate(self.store, selected, selected.current_contract_version, chosen.key, selected.epoch)
        hours = [item for item in results if item.constraint_key == "work_hours"][0]
        self.assertEqual(hours.status, "unmet")
        self.assertNotIn("0", hours.reason)
        self.assertFalse(qualify.completion_status(self.store, selected).ready)

    def test_quote_offsets_are_unicode_code_points(self):
        emoji = "\U0001F393"
        self.assertEqual(len(emoji), 1)
        self.assertEqual(len(emoji.encode("utf-8")), 4)
        self.assertEqual(len(emoji.encode("utf-16-le")) // 2, 2)
        text = "Nordic Water Masters tuition marker " + emoji + " is 10 EUR total for 2030-fall at Otaniemi."
        quote = emoji + " is 10 EUR total for 2030-fall at Otaniemi"
        start = text.index(quote)
        end = start + len(quote)
        self.assertEqual(text[start:end], quote)
        byte_end = start + len(quote.encode("utf-8"))
        utf16_end = start + len(quote.encode("utf-16-le")) // 2
        self.assertNotEqual(byte_end, end)
        self.assertNotEqual(utf16_end, end)
        selected = self._running("unicode-goal", self._base_fields())
        subject = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        value = {"amount": "10", "currency": "EUR", "period": "total"}
        page_id, fetch_id = self._page("https://example.edu/unicode")
        snapshot_id = qualify.record_snapshot(
            self.store, page_id, text, fetch_id, public_fetch=True, source_cycle_label="2030-fall",
        )
        program = {"key": "nordic-water", "name": "Nordic Water Masters"}
        binding = {
            "goal_id": selected.id,
            "contract_version": selected.current_contract_version,
            "strategy_version": selected.current_strategy_version,
            "epoch": selected.epoch,
        }

        def _observation(quote_end):
            return [{
                "field_key": "tuition",
                "value": value,
                "quote": quote,
                "quote_start": start,
                "quote_end": quote_end,
                "period": "total",
                "currency": "EUR",
                "subject": subject,
            }]

        with self.assertRaises(qualify.QualificationError) as byte_error:
            qualify.record_observations(
                self.store, snapshot_id, program, _observation(byte_end), binding, "extractor-test", "prompt-v1",
            )
        self.assertIn("UTF-8", str(byte_error.exception))
        with self.assertRaises(qualify.QualificationError) as utf16_error:
            qualify.record_observations(
                self.store, snapshot_id, program, _observation(utf16_end), binding, "extractor-test", "prompt-v1",
            )
        self.assertIn("UTF-16", str(utf16_error.exception))
        ids = qualify.record_observations(
            self.store, snapshot_id, program, _observation(end), binding, "extractor-test", "prompt-v1",
        )
        qualify.record_reviews(self.store, ids, ["keep"], review_model="reviewer-test")
        stored = self.conn.execute("SELECT quote, quote_start, quote_end FROM observations WHERE id=?", (ids[0],)).fetchone()
        self.assertEqual(stored[0], quote)
        self.assertEqual(text[stored[1]:stored[2]], stored[0])

    def test_declared_cohort_ignores_retrieval_year(self):
        page_id, fetch_id = self._page("https://example.edu/cohort-2027")
        text = (
            "Announcement from 2026. Nordic Water Masters opens the Fall 2027 cohort. "
            "Tuition is 10 EUR total for Fall 2027 at Otaniemi."
        )
        rejected = qualify.record_snapshot(
            self.store, page_id, text, fetch_id, public_fetch=True,
            source_cycle_label="2026",
            applicability={"retrieved_at": "2026", "intake": "Fall 2027"},
        )
        row = self.conn.execute(
            "SELECT source_cycle_label, applicability_json, retrieved_at FROM source_snapshots WHERE id=?",
            (rejected,),
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(json.loads(row[1]), {"intake": "Fall 2027"})
        self.assertNotIn("2026", row[1])
        accepted = qualify.record_snapshot(
            self.store, page_id, text, fetch_id, public_fetch=True, source_cycle_label="Fall 2027",
        )
        stored = self.conn.execute(
            "SELECT source_cycle_label FROM source_snapshots WHERE id=?", (accepted,),
        ).fetchone()
        self.assertEqual(stored[0], "Fall 2027")
        self.assertNotEqual(stored[0], str(time.gmtime(row[2]).tm_year))
        selected = self._running("cohort-goal", self._base_fields())
        quote = "Tuition is 10 EUR total for Fall 2027 at Otaniemi"
        observations = []
        subject = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "Fall 2027"}
        self._add(text, observations, "tuition", {"amount": "10", "currency": "EUR", "period": "total"}, quote, subject, "total")
        binding = {
            "goal_id": selected.id,
            "contract_version": selected.current_contract_version,
            "strategy_version": selected.current_strategy_version,
            "epoch": selected.epoch,
        }
        ids = qualify.record_observations(
            self.store, accepted, {"key": "nordic-water", "name": "Nordic Water Masters"},
            observations, binding, "extractor-test", "prompt-v1",
        )
        qualify.record_reviews(self.store, ids, ["keep"], review_model="reviewer-test")
        stale_text = "Nordic Water Masters Fall 2025 intake has closed. Tuition is 9 EUR total for Fall 2025 at Otaniemi."
        stale_page, stale_fetch = self._page("https://example.edu/cohort-2025")
        stale_id = qualify.record_snapshot(
            self.store, stale_page, stale_text, stale_fetch, public_fetch=True, source_cycle_label="2026",
        )
        stale_row = self.conn.execute(
            "SELECT source_cycle_label FROM source_snapshots WHERE id=?", (stale_id,),
        ).fetchone()
        self.assertIsNone(stale_row[0])
        dated = qualify.record_snapshot(
            self.store, stale_page, stale_text, stale_fetch, public_fetch=True, source_cycle_label="Fall 2025",
        )
        self.assertEqual(
            self.conn.execute("SELECT source_cycle_label FROM source_snapshots WHERE id=?", (dated,)).fetchone()[0],
            "Fall 2025",
        )
        stale_observations = []
        stale_subject = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "Fall 2025"}
        stale_quote = "Tuition is 9 EUR total for Fall 2025 at Otaniemi"
        self._add(
            stale_text, stale_observations, "tuition",
            {"amount": "9", "currency": "EUR", "period": "total"}, stale_quote, stale_subject, "total",
        )
        stale_ids = qualify.record_observations(
            self.store, dated, {"key": "nordic-water", "name": "Nordic Water Masters"},
            stale_observations, binding, "extractor-test", "prompt-v1",
        )
        qualify.record_reviews(self.store, stale_ids, ["keep"], review_model="reviewer-test")
        packages = qualify.derive_funding_packages(self.store, "nordic-water", "Fall 2027")
        basis = [item for package in packages for item in package.basis_ids]
        amounts = self.conn.execute(
            "SELECT value_json FROM observations WHERE id IN (%s)" % ",".join("?" for _ in basis),
            tuple(basis),
        ).fetchall() if basis else []
        self.assertTrue(any("10" in row[0] for row in amounts))
        self.assertFalse(any("\"9" in row[0] or "9.000" in row[0] or ": \"9\"" in row[0] for row in amounts))

    def test_supersession_requires_same_field_and_same_or_newer_cycle(self):
        selected = self._running("supersede-goal", self._base_fields())
        page_id, fetch_id = self._page("https://example.edu/supersede")
        program = {"key": "nordic-water", "name": "Nordic Water Masters"}
        binding = {
            "goal_id": selected.id,
            "contract_version": selected.current_contract_version,
            "strategy_version": selected.current_strategy_version,
            "epoch": selected.epoch,
        }

        def _store(label, tuition, living=False):
            parts = [
                "Nordic Water Masters",
                "Tuition is " + tuition + " EUR total for " + label + " at Otaniemi",
            ]
            if living:
                parts.append("Living cost is 20 EUR total for " + label + " at Otaniemi")
            text = ". ".join(parts) + "."
            snapshot_id = qualify.record_snapshot(
                self.store, page_id, text, fetch_id, public_fetch=True, source_cycle_label=label,
            )
            observations = []
            subject = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": label}
            self._add(
                text, observations, "tuition",
                {"amount": tuition, "currency": "EUR", "period": "total"},
                "Tuition is " + tuition + " EUR total for " + label + " at Otaniemi",
                subject, "total",
            )
            if living:
                self._add(
                    text, observations, "living_costs",
                    {"amount": "20", "currency": "EUR", "period": "total"},
                    "Living cost is 20 EUR total for " + label + " at Otaniemi",
                    subject, "total",
                )
            ids = qualify.record_observations(
                self.store, snapshot_id, program, observations, binding, "extractor-test", "prompt-v1",
            )
            return snapshot_id, ids

        older_snapshot, older_ids = _store("Fall 2027", "10", living=True)
        qualify.record_reviews(self.store, older_ids, ["keep"] * len(older_ids), review_model="reviewer-test")
        _store("Fall 2025", "99")
        tuition_2027 = self.conn.execute(
            "SELECT invalidated_at FROM observations WHERE id=?", (older_ids[0],),
        ).fetchone()
        living_2027 = self.conn.execute(
            "SELECT invalidated_at FROM observations WHERE id=?", (older_ids[1],),
        ).fetchone()
        self.assertIsNone(tuition_2027[0])
        self.assertIsNone(living_2027[0])
        broken = [{
            "field_key": "tuition",
            "value": {"amount": "11", "currency": "EUR", "period": "total"},
            "quote": "Tuition is 11 EUR total for Fall 2029 at Otaniemi",
            "quote_start": 0,
            "quote_end": 0,
            "period": "total",
            "currency": "EUR",
            "subject": {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "Fall 2029"},
        }]
        failed_text = "Nordic Water Masters. Tuition is 11 EUR total for Fall 2029 at Otaniemi."
        failed_snapshot = qualify.record_snapshot(
            self.store, page_id, failed_text, fetch_id, public_fetch=True, source_cycle_label="Fall 2029",
        )
        with self.assertRaises(qualify.QualificationError):
            qualify.record_observations(
                self.store, failed_snapshot, program, broken, binding, "extractor-test", "prompt-v1",
            )
        self.assertIsNone(self.conn.execute(
            "SELECT invalidated_at FROM observations WHERE id=?", (older_ids[0],),
        ).fetchone()[0])
        newer_snapshot, newer_ids = _store("Fall 2028", "11")
        retired = self.conn.execute(
            "SELECT invalidated_at, invalidated_reason FROM observations WHERE id=?", (older_ids[0],),
        ).fetchone()
        self.assertIsNotNone(retired[0])
        self.assertEqual(retired[1], "source_changed")
        self.assertIsNone(self.conn.execute(
            "SELECT invalidated_at FROM observations WHERE id=?", (older_ids[1],),
        ).fetchone()[0])
        self.assertIsNone(self.conn.execute(
            "SELECT superseded_by FROM source_snapshots WHERE id=?", (older_snapshot,),
        ).fetchone()[0])
        self.assertIsNone(self.conn.execute(
            "SELECT invalidated_at FROM observations WHERE id=?", (newer_ids[0],),
        ).fetchone()[0])
        self.assertEqual(newer_snapshot, self.conn.execute(
            "SELECT superseded_by FROM source_snapshots WHERE source_cycle_label='Fall 2025'",
        ).fetchone()[0])

    def test_unknown_or_forbidden_awards_stay_separate(self):
        selected = self._running("unstacked-goal", self._base_fields())
        text = " ".join([
            "Nordic Water Masters publishes separate awards.",
            self._bound("Award AWARD-A is guaranteed"),
            self._bound("Award AWARD-B is guaranteed"),
            self._bound("AWARD-A tuition coverage is 10 EUR total"),
            self._bound("AWARD-B living support amount is 12 EUR total"),
        ])
        observations = []
        base = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        award_a = dict(base, award_key="AWARD-A")
        award_b = dict(base, award_key="AWARD-B")
        self._add(text, observations, "award_type", "guaranteed", self._bound("Award AWARD-A is guaranteed"), award_a)
        self._add(text, observations, "award_type", "guaranteed", self._bound("Award AWARD-B is guaranteed"), award_b)
        self._add(
            text, observations, "tuition_coverage",
            {"amount": "10", "currency": "EUR", "period": "total"},
            self._bound("AWARD-A tuition coverage is 10 EUR total"), award_a, "total",
        )
        self._add(
            text, observations, "living_support_amount",
            {"amount": "12", "currency": "EUR", "period": "total"},
            self._bound("AWARD-B living support amount is 12 EUR total"), award_b, "total",
        )
        self._observe(
            selected, "https://example.edu/unstacked", text,
            {"key": "nordic-water", "name": "Nordic Water Masters"}, observations, cycle="2030-fall",
        )
        packages = qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall")
        self.assertIn(("AWARD-A",), [item.award_keys for item in packages])
        self.assertIn(("AWARD-B",), [item.award_keys for item in packages])
        self.assertFalse(any(len(item.award_keys) > 1 for item in packages))

    def test_conflicting_tuition_is_not_averaged_or_summed(self):
        selected = self._running("conflict-money", self._base_fields())
        program = {"key": "nordic-water", "name": "Nordic Water Masters"}
        subject = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        for url, amount in (("https://example.edu/dept", "100"), ("https://example.edu/grad", "300")):
            snippet = self._bound("Tuition is " + amount + " EUR total")
            text = "Nordic Water Masters states " + snippet + "."
            observations = []
            self._add(
                text, observations, "tuition",
                {"amount": amount, "currency": "EUR", "period": "total"}, snippet, subject, "total",
            )
            self._observe(selected, url, text, program, observations, cycle="2030-fall")
        packages = qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall")
        flows = [item.cash_flow for item in packages if not item.award_keys]
        self.assertTrue(flows)
        self.assertIsNone(flows[0]["net_cost"])
        self.assertIsNone(flows[0]["gross_cost"])
        self.assertNotIn("200.00", json.dumps(flows[0]))
        self.assertNotIn("400.00", json.dumps(flows[0]))
        coverage = qualify.coverage(self.store, selected.id, selected.current_contract_version)
        self.assertIn("tuition", coverage.programs[0].public_contested_fields)

    def test_satellite_or_online_evidence_is_not_assigned_on_campus(self):
        selected = self._running("campus-scope", self._base_fields())
        program = {"key": "nordic-water", "name": "Nordic Water Masters"}
        subject = {"program_key": "nordic-water", "campus": "Otaniemi", "cycle_label": "2030-fall"}
        text = " ".join([
            "Nordic Water Masters publishes the hydrology programme in FI.",
            self._bound("The modality is on campus"),
            "Online tuition is 10 EUR total for 2030-fall at Otaniemi.",
        ])
        observations = []
        self._add(text, observations, "modality", "on_campus", self._bound("The modality is on campus"), subject)
        online_quote = "Online tuition is 10 EUR total for 2030-fall at Otaniemi"
        self._add(
            text, observations, "tuition",
            {"amount": "10", "currency": "EUR", "period": "total"}, online_quote, subject, "total",
        )
        _snapshot, ids = self._observe(
            selected, "https://example.edu/online-only", text, program, observations, cycle="2030-fall",
        )
        packages = qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall")
        self.assertFalse(any(ids[1] in item.basis_ids for item in packages))
        policy = " ".join([
            "Nordic Water Masters publishes one policy that applies to all campuses.",
            self._bound("The modality is on campus"),
            "Satellite campus tuition is 15 EUR total for 2030-fall at North Campus.",
        ])
        policy_observations = []
        self._add(policy, policy_observations, "modality", "on_campus", self._bound("The modality is on campus"), subject)
        satellite_quote = "Satellite campus tuition is 15 EUR total for 2030-fall at North Campus"
        satellite_subject = dict(subject, campus="North Campus")
        self._add(
            policy, policy_observations, "tuition",
            {"amount": "15", "currency": "EUR", "period": "total"}, satellite_quote, satellite_subject, "total",
        )
        _policy_snapshot, policy_ids = self._observe(
            selected, "https://example.edu/multi-campus", policy, program, policy_observations, cycle="2030-fall",
        )
        combined = qualify.derive_funding_packages(self.store, "nordic-water", "2030-fall")
        self.assertTrue(any(policy_ids[1] in item.basis_ids for item in combined))
        self.assertFalse(any(ids[1] in item.basis_ids for item in combined))


if __name__ == "__main__":
    unittest.main()
