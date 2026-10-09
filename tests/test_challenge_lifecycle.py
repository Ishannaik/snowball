#!/usr/bin/env python3
"""Challenge lifecycle without a live solver, browser, or provider call.

python -m unittest tests.test_challenge_lifecycle -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
_TOOLS = ROOT / "tools"
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _TOOLS]
if (
    getattr(sys.modules.get("queue"), "__file__", None)
    and Path(sys.modules["queue"].__file__).resolve().parent == _TOOLS
):
    del sys.modules["queue"]


def _load(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, _TOOLS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


orch = _load("orchestrator.py", "orchestrator")
captcha = _load("captcha.py", "snowball_captcha")
browser = _load("browser_worker.py", "snowball_browser_worker")
fetch_types = _load("fetch_types.py", "snowball_fetch_types")
guard = _load("egress_guard.py", "snowball_egress_guard")
gateway = guard
SECRET = "captcha-client-key-should-not-leak"
SITEKEY = "0x4AAAAAAAtest-sitekey"
TOKEN = "solver-token-not-a-page"
UA = "Mozilla/5.0 test-agent"
PASSWORD = "proxy-password-secret"
PROXY = "http://customer-sessid_oneip-abc:proxy-password-secret@proxy.example:8000"
ACQ = "acq-1"
GRANTS = "https://grants.example.org/opportunities/2026"
ARTICLE = (
    "The Mozilla Foundation offers grants of up to $50,000 for open-source projects that make AI "
    "more accountable. Applications close on 2026-12-15. Individuals and organisations from any "
    "country, including India, may apply. This page is the program description and not a challenge."
)
CAPTURE = {"kind": "challenge_turnstile", "sitekey": SITEKEY}


def _caps(solver_count: int = 10) -> dict:
    return {
        "daily_micro_usd": 5_000_000,
        "monthly_micro_usd": 50_000_000,
        "daily_retrieval_count": 10,
        "daily_solver_count": solver_count,
        "monthly_solver_count": 100,
        "tariffs": {"captcha": 1000, "zenrows": 1000, "residential": 1000},
        "providers": {
            "captcha": {
                "daily_micro_usd": 5_000_000,
                "monthly_micro_usd": 50_000_000,
                "daily_count": 10,
                "monthly_count": 100,
                "daily_solver_count": solver_count,
                "monthly_solver_count": 100,
            }
        },
    }


class Script:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, spec):
        self.calls.append(spec)
        item = self.steps.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSession:
    def __init__(self, pages, capture=None, outcome=None, ips=None):
        self.pages = list(pages)
        self.capture_payload = CAPTURE if capture is None else capture
        self.outcome = {"challenge_visible": False, "text": ARTICLE, "title": "Program"} if outcome is None else outcome
        self.ips = list(ips or ["203.0.113.9"])
        self.opened: list[str] = []
        self.closed = False
        self.callbacks: list[tuple[str, str]] = []
        self.agents: list[str] = []
        self.captured = False

    def open(self, url):
        self.opened.append(url)
        return self.pages.pop(0)

    def close(self):
        self.closed = True

    def capture(self):
        self.captured = True
        return self.capture_payload

    def echo_ip(self):
        if len(self.ips) == 1:
            return self.ips[0]
        return self.ips.pop(0)

    def apply_user_agent(self, user_agent):
        self.agents.append(user_agent)

    def invoke_callback(self, token, field):
        self.callbacks.append((token, field))

    def read_outcome(self):
        return self.outcome


def _public(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def _cfg(proxy: str | None = None, key: str | None = SECRET):
    return captcha.SolverConfig(client_key=key, proxy_url=proxy)


def _challenge(kind: str = "challenge_turnstile", **overrides):
    fields = {"kind": kind, "url": GRANTS, "sitekey": SITEKEY, "session_id": ACQ}
    fields.update(overrides)
    return captcha.Challenge(**fields)


def _request(**overrides):
    fields = {
        "acquisition_id": ACQ,
        "page_id": 1,
        "url": GRANTS,
        "private": False,
        "allowed_modes": ("public", "solver"),
        "approved_resource_origins": ("https://challenges.cloudflare.com",),
        "allowed_path_prefixes": ("/opportunities/",),
        "session_id": ACQ,
        "deadline_at": 10_000.0,
    }
    fields.update(overrides)
    return browser.AccessRequest(**fields)


def _ok_page():
    return {"status": 200, "location": None, "final_url": GRANTS}


def _dump(store) -> str:
    return "\n".join(store.db.iterdump())


def _budget(store, reservation_id: int):
    return store.one("SELECT * FROM paid_budget_reservations WHERE id = ?", (reservation_id,))


def _task_row(store, acquisition: str = ACQ):
    found = store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='solver_tasks'")
    if found is None:
        return None
    return store.one("SELECT * FROM solver_tasks WHERE acquisition_id = ?", (acquisition,))


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = orch.Store.open(Path(self.tmp.name) / "s.db")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def _reserve(self, acquisition: str = ACQ, solver_count: int = 10):
        return captcha.pb.reserve(
            self.store,
            acquisition,
            "captcha",
            1000,
            is_solver=True,
            caps=_caps(solver_count),
        )


class SecretReprTests(unittest.TestCase):
    def test_repr_hides_challenge_and_token_fields(self):
        challenge = _challenge(sitekey="SITEKEYSECRET", data="DATASECRET", user_agent="UASECRET")
        self.assertNotIn("SITEKEYSECRET", repr(challenge))
        self.assertNotIn("DATASECRET", repr(challenge))
        self.assertNotIn("UASECRET", str(challenge))
        solved = captcha.SolveResult(status="ready", token="TOKENSECRET", user_agent="UASECRET", cost_micro_usd=None)
        self.assertNotIn("TOKENSECRET", repr(solved))
        self.assertNotIn("TOKENSECRET", str(solved))
        task = captcha.SolverTask("2captcha", 7, 3, "submitted", detail="TOKENSECRET proxy-password-secret")
        self.assertNotIn("TOKENSECRET", repr(task))
        self.assertNotIn("proxy-password-secret", str(task))


class SubmitTests(StoreCase):
    def test_standalone_turnstile_payload_excludes_extra_fields(self):
        script = Script([{"status": 200, "body": {"errorId": 0, "taskId": 41}}])
        reservation = self._reserve()
        challenge = _challenge(user_agent="UA-SHOULD-NOT-BE-SENT", action=None, data=None, pagedata=None)
        task = captcha.submit(challenge, _cfg(), reservation, store=self.store, transport=script, now=1000)
        self.assertEqual(task.submission_state, "submitted")
        self.assertEqual(task.task_id, 41)
        sent = script.calls[0]["body"]["task"]
        self.assertEqual(sent["type"], "TurnstileTaskProxyless")
        self.assertEqual(script.calls[0]["url"], captcha.CREATE_URL)
        self.assertEqual(script.calls[0]["body"]["clientKey"], SECRET)
        for banned in ("languagePool", "softId", "callbackUrl", "cookies", "userAgent", "proxyPassword"):
            self.assertNotIn(banned, sent)
        self.assertNotIn("UA-SHOULD-NOT-BE-SENT", json.dumps(sent))
        self.assertEqual(_budget(self.store, reservation.id)["settled"], 0)
        self.assertNotIn(SECRET, _dump(self.store))
        self.assertNotIn(SITEKEY, _dump(self.store))
        again = captcha.submit(challenge, _cfg(), reservation, store=self.store, transport=script, now=1001)
        self.assertEqual(again.submission_state, "submitted")
        self.assertEqual(len(script.calls), 1)

    def test_partial_turnstile_releases_without_transport(self):
        script = Script([])
        reservation = self._reserve()
        challenge = _challenge(action="managed", data=None, pagedata=None)
        task = captcha.submit(challenge, _cfg(PROXY), reservation, store=self.store, transport=script, now=1000)
        self.assertEqual(task.submission_state, "rejected")
        self.assertEqual(script.calls, [])
        self.assertIsNone(_task_row(self.store))
        row = _budget(self.store, reservation.id)
        self.assertEqual(row["settled"], 1)
        self.assertEqual(row["actual_micro_usd"], 0)
        self.assertEqual(row["outcome"], captcha.pb.CONFIRMED_NONBILLABLE)

    def test_turnstile_challenge_sends_proxy_but_does_not_store_it(self):
        script = Script([{"status": 200, "body": {"errorId": 0, "taskId": 42}}])
        reservation = self._reserve()
        challenge = _challenge(action="managed", data="cdata", pagedata="page-data", user_agent="UA-SECRET")
        task = captcha.submit(challenge, _cfg(PROXY), reservation, store=self.store, transport=script, now=1000)
        sent = script.calls[0]["body"]["task"]
        self.assertEqual(sent["type"], "TurnstileTask")
        self.assertEqual(sent["proxyPassword"], PASSWORD)
        self.assertNotIn(PASSWORD, repr(task))
        self.assertNotIn(PASSWORD, _dump(self.store))
        self.assertNotIn(SECRET, _dump(self.store))

    def test_recaptcha_fields_and_refused_api_domain(self):
        script = Script([{"status": 200, "body": {"errorId": 0, "taskId": 43}}])
        reservation = self._reserve()
        challenge = _challenge(
            "challenge_recaptcha",
            api_domain="www.recaptcha.net",
            invisible=True,
            data="data-s-value",
            user_agent="UA-SECRET",
        )
        captcha.submit(challenge, _cfg(), reservation, store=self.store, transport=script, now=1000)
        sent = script.calls[0]["body"]["task"]
        self.assertEqual(sent["type"], "RecaptchaV2TaskProxyless")
        self.assertEqual(sent["apiDomain"], "www.recaptcha.net")
        self.assertIs(sent["isInvisible"], True)
        self.assertEqual(sent["recaptchaDataSValue"], "data-s-value")
        for banned in ("cookies", "userAgent", "languagePool", "softId", "callbackUrl"):
            self.assertNotIn(banned, sent)

        hidden = Script([{"status": 200, "body": {"errorId": 0, "taskId": 44}}])
        other = self._reserve("acq-visible")
        plain = _challenge("challenge_recaptcha", session_id="acq-visible", invisible=False, data=None)
        captcha.submit(plain, _cfg(), other, store=self.store, transport=hidden, now=1000)
        self.assertNotIn("isInvisible", hidden.calls[0]["body"]["task"])
        self.assertNotIn("recaptchaDataSValue", hidden.calls[0]["body"]["task"])

        refused = Script([])
        bad = self._reserve("acq-evil")
        evil = _challenge("challenge_recaptcha", session_id="acq-evil", api_domain="evil.com")
        task = captcha.submit(evil, _cfg(), bad, store=self.store, transport=refused, now=1000)
        self.assertEqual(task.submission_state, "rejected")
        self.assertEqual(refused.calls, [])
        self.assertEqual(_budget(self.store, bad.id)["actual_micro_usd"], 0)

    def test_google_search_without_proxy_makes_no_request(self):
        script = Script([])
        reservation = self._reserve()
        challenge = _challenge("challenge_recaptcha", url="https://www.google.com/search?q=grants")
        task = captcha.submit(challenge, _cfg(), reservation, store=self.store, transport=script, now=1000)
        self.assertEqual(task.submission_state, "rejected")
        self.assertEqual(script.calls, [])
        self.assertEqual(_budget(self.store, reservation.id)["outcome"], captcha.pb.CONFIRMED_NONBILLABLE)

    def test_session_mismatch_holds_the_reservation(self):
        script = Script([])
        reservation = self._reserve()
        challenge = _challenge(session_id="other-session")
        task = captcha.submit(challenge, _cfg(), reservation, store=self.store, transport=script, now=1000)
        self.assertEqual(task.submission_state, "mismatch")
        self.assertEqual(script.calls, [])
        row = _budget(self.store, reservation.id)
        self.assertEqual(row["settled"], 0)
        self.assertIsNone(row["actual_micro_usd"])

    def test_slot_cooldown_then_balance_and_image_are_terminal(self):
        script = Script(
            [
                {"status": 200, "body": {"errorId": 2}},
                {"status": 200, "body": {"errorId": 0, "taskId": 50}},
            ]
        )
        reservation = self._reserve()
        challenge = _challenge()
        first = captcha.submit(challenge, _cfg(), reservation, store=self.store, transport=script, now=1000)
        self.assertEqual(first.submission_state, "not_accepted")
        skipped = captcha.submit(challenge, _cfg(), reservation, store=self.store, transport=script, now=1004)
        self.assertEqual(skipped.submission_state, "not_accepted")
        self.assertEqual(len(script.calls), 1)
        retried = captcha.submit(challenge, _cfg(), reservation, store=self.store, transport=script, now=1005)
        self.assertEqual(retried.submission_state, "submitted")
        self.assertEqual(retried.task_id, 50)
        self.assertEqual(len(script.calls), 2)

        balance = Script([{"status": 200, "body": {"errorId": 10, "errorCode": "ERROR_ZERO_BALANCE"}}])
        balance_row = self._reserve("acq-balance")
        task = captcha.submit(
            _challenge(session_id="acq-balance"),
            _cfg(),
            balance_row,
            store=self.store,
            transport=balance,
            now=1000,
        )
        self.assertEqual(task.submission_state, "insufficient_balance")
        self.assertEqual(_budget(self.store, balance_row.id)["actual_micro_usd"], 0)
        captcha.submit(
            _challenge(session_id="acq-balance"),
            _cfg(),
            balance_row,
            store=self.store,
            transport=balance,
            now=1060,
        )
        self.assertEqual(len(balance.calls), 1)

        slow = Script([{"status": 200, "body": {"errorId": 10}}])
        slow_row = self._reserve("acq-slow")
        waiting = captcha.submit(
            _challenge(session_id="acq-slow"), _cfg(), slow_row, store=self.store, transport=slow, now=2000
        )
        self.assertEqual(waiting.submission_state, "not_accepted")
        self.assertEqual(_task_row(self.store, "acq-slow")["cooldown_until"], 2060)
        self.assertEqual(_budget(self.store, slow_row.id)["settled"], 0)

        image = Script([{"status": 200, "body": {"errorId": 3}}])
        image_row = self._reserve("acq-image")
        rejected = captcha.submit(
            _challenge(session_id="acq-image"), _cfg(), image_row, store=self.store, transport=image, now=1000
        )
        self.assertEqual(rejected.submission_state, "rejected")
        self.assertEqual(_budget(self.store, image_row.id)["actual_micro_usd"], 0)

    def test_ambiguous_create_is_not_resubmitted_and_redacts_the_key(self):
        script = Script([captcha.TransportError(f"timed out key {SECRET} password {PASSWORD}", timeout=True)])
        reservation = self._reserve(solver_count=1)
        task = captcha.submit(_challenge(), _cfg(PROXY), reservation, store=self.store, transport=script, now=1000)
        self.assertEqual(task.submission_state, "unknown")
        self.assertNotIn(SECRET, task.detail)
        self.assertNotIn(PASSWORD, task.detail)
        self.assertNotIn(SECRET, repr(task))
        self.assertEqual(_budget(self.store, reservation.id)["settled"], 0)
        again = Script([{"status": 200, "body": {"errorId": 0, "taskId": 99}}])
        second = captcha.submit(_challenge(), _cfg(PROXY), reservation, store=self.store, transport=again, now=1010)
        self.assertEqual(second.submission_state, "unknown")
        self.assertEqual(again.calls, [])
        with self.assertRaises(captcha.pb.PaidBudgetExhausted):
            self._reserve("acq-next", solver_count=1)

    def test_unsolvable_create_frees_the_solver_slot(self):
        script = Script([{"status": 200, "body": {"errorId": 12}}])
        reservation = self._reserve(solver_count=1)
        task = captcha.submit(_challenge(), _cfg(), reservation, store=self.store, transport=script, now=1000)
        self.assertEqual(task.submission_state, "unsolvable")
        self.assertEqual(_budget(self.store, reservation.id)["actual_micro_usd"], 0)
        follow = self._reserve("acq-next", solver_count=1)
        self.assertGreater(follow.id, 0)

    def test_bool_error_id_stays_unknown(self):
        script = Script([{"status": 200, "body": {"errorId": True, "taskId": True}}])
        reservation = self._reserve()
        task = captcha.submit(_challenge(), _cfg(), reservation, store=self.store, transport=script, now=1000)
        self.assertEqual(task.submission_state, "unknown")
        self.assertEqual(_budget(self.store, reservation.id)["settled"], 0)

    def test_transport_rejects_a_non_provider_url_without_leaking_the_key(self):
        with self.assertRaises(captcha.TransportError) as caught:
            captcha.http_transport({"url": "https://evil.example/createTask", "body": {"clientKey": SECRET}})
        self.assertNotIn(SECRET, str(caught.exception))


class PollTests(StoreCase):
    def _submitted(self, acquisition: str = ACQ, kind: str = "challenge_turnstile"):
        script = Script([{"status": 200, "body": {"errorId": 0, "taskId": 77}}])
        reservation = self._reserve(acquisition)
        task = captcha.submit(
            _challenge(kind, session_id=acquisition),
            _cfg(),
            reservation,
            store=self.store,
            transport=script,
            now=1000,
        )
        return task, reservation

    def test_poll_interval_deadline_and_single_result_call(self):
        task, reservation = self._submitted()
        early = Script([{"status": 200, "body": {"errorId": 0, "status": "processing"}}])
        waiting = captcha.poll(task, 2000, store=self.store, transport=early, now=1004, min_interval=1)
        self.assertEqual(waiting.status, "processing")
        self.assertEqual(early.calls, [])
        first = captcha.poll(task, 2000, store=self.store, transport=early, now=1005, min_interval=1)
        self.assertEqual(first.status, "processing")
        self.assertEqual(len(early.calls), 1)
        self.assertEqual(early.calls[0]["url"], captcha.RESULT_URL)
        second = captcha.poll(task, 2000, store=self.store, transport=early, now=1006)
        self.assertEqual(second.status, "processing")
        self.assertEqual(len(early.calls), 1)

        deadline = captcha.poll(task, 1000, store=self.store, transport=early, now=1000)
        self.assertEqual(deadline.status, "deadline")
        self.assertEqual(len(early.calls), 1)
        row = _budget(self.store, reservation.id)
        self.assertEqual(row["settled"], 1)
        self.assertIsNone(row["actual_micro_usd"])
        self.assertEqual(row["outcome"], "deadline")
        self.assertEqual(_task_row(self.store)["submission_state"], "submitted")

    def test_ready_cost_string_is_not_micro_usd_and_token_is_not_stored(self):
        task, reservation = self._submitted()
        body = {
            "errorId": 0,
            "status": "ready",
            "cost": "0.00145",
            "endTime": 1005 - 299,
            "solution": {"token": TOKEN, "userAgent": UA, "cookies": {"secret": "COOKIESECRET"}},
        }
        script = Script([{"status": 200, "body": body}])
        solved = captcha.poll(task, 2000, store=self.store, transport=script, now=1005)
        self.assertEqual(solved.status, "ready")
        self.assertEqual(solved.token, TOKEN)
        self.assertEqual(solved.cost_micro_usd, 1450)
        self.assertNotIn(TOKEN, repr(solved))
        dump = _dump(self.store)
        self.assertNotIn(TOKEN, dump)
        self.assertNotIn("COOKIESECRET", dump)
        self.assertNotIn(SECRET, dump)
        self.assertEqual(_task_row(self.store)["cost_text"], "0.00145")
        self.assertEqual(_budget(self.store, reservation.id)["actual_micro_usd"], 1000)
    def test_missing_user_agent_float_and_bool_end_time_discard_the_token(self):
        task, reservation = self._submitted()
        bodies = [
            {"errorId": 0, "status": "ready", "cost": "0.00145", "endTime": 1000, "solution": {"token": TOKEN}},
            {
                "errorId": 0,
                "status": "ready",
                "cost": "0.00145",
                "endTime": 1000.5,
                "solution": {"token": TOKEN, "userAgent": UA},
            },
            {
                "errorId": 0,
                "status": "ready",
                "cost": "0.00145",
                "endTime": True,
                "solution": {"token": TOKEN, "userAgent": UA},
            },
            {"errorId": 0, "status": "ready", "cost": "0.00145", "solution": {"token": TOKEN, "userAgent": UA}},
        ]
        for body in bodies:
            script = Script([{"status": 200, "body": body}])
            solved = captcha.poll(task, 2000, store=self.store, transport=script, now=1005)
            self.assertEqual(solved.status, "failure")
            self.assertIsNone(solved.token)
            self.assertNotIn(TOKEN, _dump(self.store))
            self.assertEqual(_task_row(self.store)["cost_text"], "0.00145")
            self.assertIsNone(_budget(self.store, reservation.id)["actual_micro_usd"])
            _task_reset(self.store)

    def test_turnstile_and_recaptcha_age_limits(self):
        task, _reservation = self._submitted()
        accepted = _ready_body(1005 - 299)
        solved = captcha.poll(task, 4000, store=self.store, transport=Script([_http(accepted)]), now=1005)
        self.assertEqual(solved.status, "ready")
        _task_reset(self.store, kind="challenge_turnstile")
        expired = captcha.poll(
            task, 4000, store=self.store, transport=Script([_http(_ready_body(1005 - 301))]), now=1005
        )
        self.assertEqual(expired.status, "expired")
        self.assertIsNone(expired.token)

        recaptcha, _reservation = self._submitted("acq-recaptcha", "challenge_recaptcha")
        fresh = captcha.poll(
            recaptcha,
            4000,
            store=self.store,
            transport=Script([_http(_ready_body(2000 - 119))]),
            now=2000,
        )
        self.assertEqual(fresh.status, "ready")
        _task_reset(self.store, "acq-recaptcha", "challenge_recaptcha")
        stale = captcha.poll(
            recaptcha,
            4000,
            store=self.store,
            transport=Script([_http(_ready_body(2000 - 121))]),
            now=2000,
        )
        self.assertEqual(stale.status, "expired")
        self.assertIsNone(stale.token)

    def test_unsolvable_poll_frees_slot_and_missing_task_does_not(self):
        task, reservation = self._submitted()
        script = Script([{"status": 200, "body": {"errorId": 12, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE"}}])
        solved = captcha.poll(task, 3000, store=self.store, transport=script, now=1005)
        self.assertEqual(solved.status, "unsolvable")
        self.assertEqual(_budget(self.store, reservation.id)["actual_micro_usd"], 0)
        follow = Script([{"status": 200, "body": {"errorId": 0, "taskId": 88}}])
        captcha.submit(_challenge(), _cfg(), reservation, store=self.store, transport=follow, now=1010)
        self.assertEqual(follow.calls, [])

        missing_task, missing_reservation = self._submitted("acq-missing")
        gone = captcha.poll(
            missing_task,
            3000,
            store=self.store,
            transport=Script([{"status": 200, "body": {"errorId": 22, "errorCode": "ERROR_TASK_ABSENT"}}]),
            now=1005,
        )
        self.assertEqual(gone.status, "failed")
        self.assertIsNone(_budget(self.store, missing_reservation.id)["actual_micro_usd"])

    def test_missing_row_and_pause_hook_do_not_call_transport(self):
        script = Script([])
        slept: list[float] = []
        forged = captcha.SolverTask("2captcha", 1, 999, "submitted", acquisition_id="missing")
        solved = captcha.poll(forged, 2000, store=self.store, transport=script, now=1000, pause=1.5, sleep=slept.append)
        self.assertEqual(solved.status, "failure")
        self.assertEqual(script.calls, [])
        self.assertEqual(slept, [1.5])

    def test_solve_callback_polls_once_the_interval_has_elapsed(self):
        clock = Clock(5000)
        script = Script(
            [
                {"status": 200, "body": {"errorId": 0, "taskId": 77}},
                {"status": 200, "body": _ready_body(4990)},
            ]
        )
        solve = browser.solve_with_captcha(
            captcha,
            self.store,
            _cfg(),
            lambda _challenge_obj: self._reserve(),
            transport=script,
            sleep=clock.sleep,
            clock=clock,
        )
        solved = solve(_challenge(), 8000)
        self.assertEqual(solved.status, "ready")
        self.assertEqual(solved.token, TOKEN)
        self.assertEqual(solved.cost_micro_usd, 1450)
        self.assertEqual([call["url"] for call in script.calls], [captcha.CREATE_URL, captcha.RESULT_URL])
        self.assertEqual(script.calls[1]["body"]["clientKey"], SECRET)
        self.assertEqual(script.calls[1]["body"]["taskId"], 77)
        self.assertNotIn(TOKEN, _dump(self.store))


def _http(body: dict) -> dict:
    return {"status": 200, "body": body}


def _ready_body(end_time: int) -> dict:
    return {
        "errorId": 0,
        "status": "ready",
        "cost": "0.00145",
        "endTime": end_time,
        "solution": {"token": TOKEN, "userAgent": UA, "gRecaptchaResponse": TOKEN},
    }


def _task_reset(store, acquisition: str = ACQ, kind: str = "challenge_turnstile") -> None:
    with store.tx() as db:
        db.execute(
            """
            UPDATE solver_tasks
               SET submission_state = 'submitted', cost_text = NULL, last_poll_at = NULL, detail = NULL
             WHERE acquisition_id = ?
            """,
            (acquisition,),
        )
        db.execute(
            """
            UPDATE paid_budget_reservations
               SET settled = 0, actual_micro_usd = NULL, outcome = NULL
             WHERE id = (SELECT reservation_id FROM solver_tasks WHERE acquisition_id = ?)
            """,
            (acquisition,),
        )
        db.execute("UPDATE solver_tasks SET kind = ? WHERE acquisition_id = ?", (kind, acquisition))


class BrowserTests(unittest.TestCase):
    def test_activation_reasons(self):
        self.assertEqual(
            browser.activation_block_reason(
                fixture_mode=False, api_key=SECRET, allowed_modes=("solver",), caps=_caps()
            ),
            "fixture_off",
        )
        self.assertEqual(
            browser.activation_block_reason(fixture_mode=True, api_key="", allowed_modes=("solver",), caps=_caps()),
            "credential_missing",
        )
        self.assertEqual(
            browser.activation_block_reason(fixture_mode=True, api_key=SECRET, allowed_modes=("public",), caps=_caps()),
            "solver_not_allowed",
        )
        self.assertEqual(
            browser.activation_block_reason(
                fixture_mode=True, api_key=SECRET, allowed_modes=("solver",), caps=_caps(0)
            ),
            "ceilings_inactive",
        )
        self.assertIsNone(
            browser.activation_block_reason(
                fixture_mode=True, api_key=SECRET, allowed_modes=("public", "solver"), caps=_caps()
            )
        )

    def test_missing_browser_does_not_resolve_dns(self):
        def resolver(_host: str, _port: int) -> list[str]:
            raise AssertionError("dns")

        result = browser.acquire(
            _request(), _cfg(), 10_000, session_factory=None, solve=None, resolver=resolver, clock=lambda: 1
        )
        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.wall_kind, "browser_unavailable")
        self.assertEqual(result.text, "")

        def broken():
            raise ImportError("playwright")

        imported = browser.acquire(
            _request(), _cfg(), 10_000, session_factory=broken, resolver=resolver, clock=lambda: 1
        )
        self.assertEqual(imported.wall_kind, "browser_unavailable")
        empty = browser.acquire(
            _request(), _cfg(), 10_000, session_factory=lambda: None, resolver=resolver, clock=lambda: 1
        )
        self.assertEqual(empty.wall_kind, "browser_unavailable")

    def test_private_path_mismatch_and_deadline_skip_the_browser(self):
        def factory():
            raise AssertionError("browser")

        private = browser.acquire(_request(private=True), _cfg(), 10_000, session_factory=factory, clock=lambda: 1)
        self.assertEqual(private.wall_kind, "not_allowed")
        mismatch = browser.acquire(
            _request(session_id="other"), _cfg(), 10_000, session_factory=factory, clock=lambda: 1
        )
        self.assertEqual(mismatch.wall_kind, "mismatch")
        path = browser.acquire(
            _request(url="https://grants.example.org/secret"),
            _cfg(),
            10_000,
            session_factory=factory,
            clock=lambda: 1,
        )
        self.assertEqual(path.wall_kind, "path")
        late = browser.acquire(_request(), _cfg(), 1000, session_factory=factory, clock=lambda: 1000)
        self.assertEqual(late.outcome, "deadline")

    def test_ok_page_keeps_text_and_discards_a_visible_challenge(self):
        session = FakeSession([_ok_page()])
        seen = []

        def solve(challenge, _deadline):
            seen.append(challenge)
            return captcha.SolveResult(status="ready", token=TOKEN, user_agent=UA)

        result = browser.acquire(
            _request(),
            _cfg(),
            10_000,
            session_factory=lambda: session,
            solve=solve,
            resolver=_public,
            clock=lambda: 1,
        )
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.route, "browser")
        self.assertIn("program description", result.text)
        self.assertNotIn(TOKEN, result.text)
        self.assertNotIn("challenges.cloudflare.com", " ".join(session.opened))
        self.assertEqual(session.callbacks, [(TOKEN, "cf-turnstile-response")])
        self.assertEqual(seen[0].session_id, ACQ)
        self.assertTrue(session.closed)

        visible = FakeSession(
            [_ok_page()],
            outcome={"challenge_visible": True, "text": ARTICLE + TOKEN, "title": "Wall"},
        )
        walled = browser.acquire(
            _request(),
            _cfg(),
            10_000,
            session_factory=lambda: visible,
            solve=solve,
            resolver=_public,
            clock=lambda: 1,
        )
        self.assertEqual(walled.outcome, "walled")
        self.assertEqual(walled.wall_kind, "challenge_turnstile")
        self.assertEqual(walled.text, "")
        self.assertNotIn(TOKEN, walled.text)
        self.assertNotIn("accountable", walled.text)
        self.assertTrue(visible.closed)

    def test_capture_html_is_not_challenge_metadata(self):
        session = FakeSession(
            [_ok_page()],
            capture={
                "kind": "challenge_turnstile",
                "sitekey": SITEKEY,
                "html": "SECRET-HTML-BODY",
                "action": "managed",
            },
        )
        seen = []

        def solve(challenge, _deadline):
            seen.append(challenge)
            return captcha.SolveResult(status="failure")

        browser.acquire(
            _request(),
            _cfg(),
            10_000,
            session_factory=lambda: session,
            solve=solve,
            resolver=_public,
            clock=lambda: 1,
        )
        self.assertNotIn("SECRET-HTML-BODY", repr(seen[0]))
        self.assertNotEqual(seen[0].action, "SECRET-HTML-BODY")
        self.assertIsNone(seen[0].data)
        self.assertIsNone(seen[0].pagedata)

    def test_ip_change_and_private_redirect_do_not_return_the_page(self):
        flipped = FakeSession([_ok_page()], ips=["203.0.113.9", "198.51.100.20"])
        result = browser.acquire(
            _request(),
            _cfg(),
            10_000,
            session_factory=lambda: flipped,
            solve=lambda _challenge_obj, _deadline: captcha.SolveResult(status="ready", token=TOKEN, user_agent=UA),
            resolver=_public,
            clock=lambda: 1,
        )
        self.assertEqual(result.outcome, "walled")
        self.assertEqual(result.text, "")
        self.assertEqual(len(flipped.callbacks), 1)
        self.assertTrue(flipped.closed)

        redirected = FakeSession([{"status": 302, "location": "http://169.254.169.254/latest", "final_url": None}])

        def solve(*_args):
            raise AssertionError("solve")

        blocked = browser.acquire(
            _request(),
            _cfg(),
            10_000,
            session_factory=lambda: redirected,
            solve=solve,
            resolver=_public,
            clock=lambda: 1,
        )
        self.assertEqual(blocked.outcome, "ssrf_blocked")
        self.assertEqual(redirected.opened, [GRANTS])
        self.assertNotIn("169.254.169.254", " ".join(redirected.opened))
        self.assertFalse(redirected.captured)
        self.assertTrue(redirected.closed)

    def test_dns_rebinding_after_open_is_ssrf_blocked(self):
        calls = {"n": 0}

        def resolver(_host: str, _port: int) -> list[str]:
            calls["n"] += 1
            if calls["n"] == 1:
                return ["93.184.216.34"]
            return ["127.0.0.1"]

        session = FakeSession([_ok_page()])
        result = browser.acquire(
            _request(),
            _cfg(),
            10_000,
            session_factory=lambda: session,
            solve=lambda *_args: (_ for _ in ()).throw(AssertionError("solve")),
            resolver=resolver,
            clock=lambda: 1,
        )
        self.assertEqual(result.outcome, "ssrf_blocked")
        self.assertEqual(session.opened, [GRANTS])
        self.assertFalse(session.captured)
        self.assertTrue(session.closed)

    def test_solver_exception_does_not_leak_the_token(self):
        session = FakeSession([_ok_page()])

        def solve(_challenge_obj, _deadline):
            raise RuntimeError(TOKEN)

        result = browser.acquire(
            _request(),
            _cfg(),
            10_000,
            session_factory=lambda: session,
            solve=solve,
            resolver=_public,
            clock=lambda: 1,
        )
        self.assertEqual(result.outcome, "failure")
        self.assertNotIn(TOKEN, result.text)
        self.assertNotIn(TOKEN, repr(result))
        self.assertTrue(session.closed)


class WorkerProcessTests(unittest.TestCase):
    def test_check_egress_and_serve_do_not_read_stdin(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            blocked = browser.main(
                ["--check-egress"],
                environ={"HTTP_PROXY": "http://user:proxy-password-secret@h:9", "PATH": "x"},
            )
        self.assertEqual(blocked, 2)
        self.assertIn("environment_proxy", stderr.getvalue())
        self.assertNotIn("proxy-password-secret", stderr.getvalue())

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            clean = browser.main(["--check-egress=json"], environ={"PATH": "x"})
        self.assertEqual(clean, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"reason": None})

        class Boom:
            def read(self, *_args):
                raise AssertionError("stdin")

        saved = sys.stdin
        sys.stdin = Boom()
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                missing = browser.main(["--serve"], environ={"PATH": "x"})
            self.assertEqual(missing, 2)
            self.assertIn("fixture_missing", err.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                present = browser.main(["--serve"], environ={"SNOWBALL_CHALLENGE_FIXTURE": "1", "PATH": "x"})
            self.assertEqual(present, 2)
            self.assertIn("playwright_missing", err.getvalue())
        finally:
            sys.stdin = saved

    def test_deadline_kills_owned_children_without_inheriting_proxy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_path = Path(tmp.name) / "tree.log"
        child = textwrap.dedent(
            """\
            import os, subprocess, sys, time
            print("PROXY:" + str(os.environ.get("HTTP_PROXY")), flush=True)
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print(f"GRANDCHILD:{p.pid}", flush=True)
            p.wait()
            """
        )
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

        def _stop_unrelated() -> None:
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=5)

        self.addCleanup(_stop_unrelated)
        env = dict(os.environ)
        env["HTTP_PROXY"] = "http://user:proxy-password-secret@h:9"
        alive = browser.enforce_deadline(
            [sys.executable, "-u", "-c", child],
            timeout=8,
            grace=0.2,
            stdout_path=log_path,
            environ=env,
        )
        text = log_path.read_text(encoding="utf-8")
        grandchild = int(text.split("GRANDCHILD:", 1)[1].split()[0])
        self.assertEqual(alive, [])
        self.assertFalse(browser._pid_alive(grandchild))
        self.assertTrue(browser._pid_alive(unrelated.pid))
        self.assertIn("PROXY:None", text)
        self.assertNotIn("proxy-password-secret", text)

    def test_service_units_cap_memory_and_do_not_loop(self):
        browser_unit = (ROOT / "systemd" / "snowball-browser.service").read_text(encoding="utf-8")
        egress_unit = (ROOT / "systemd" / "snowball-egress.service").read_text(encoding="utf-8")
        self.assertIn("snowball.service", browser_unit)
        self.assertIn("MemoryMax=768M", browser_unit)
        self.assertIn("NoNewPrivileges=yes", browser_unit)
        self.assertIn("MemorySwapMax=0", browser_unit)
        self.assertIn("Type=oneshot", browser_unit)
        self.assertIn("ReadWritePaths=/tmp", browser_unit)
        self.assertIn("WorkingDirectory=/home/ubuntu/repos/snowball", browser_unit)
        self.assertNotIn("Restart=", browser_unit)
        self.assertNotIn("EnvironmentFile=", browser_unit)
        self.assertNotIn("ProtectHome=", browser_unit)
        self.assertIn("MemoryMax=128M", egress_unit)
        self.assertIn("--check-egress", egress_unit)
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("playwright", project)
        # A live Chromium RSS reading is not claimed. MemoryMax is the bound that ships.

    def test_egress_gateway_enforces_pins_and_blocks_ssrf(self):
        def fake_resolver(host: str, port: int) -> list[str]:
            if host == "grants.example.org":
                return ["203.0.113.9"]
            if host == "private.example.org":
                return ["127.0.0.1"]
            if host == "metadata.example.org":
                return ["169.254.169.254"]
            return ["192.168.1.1"]

        with gateway.EgressGateway(resolver=fake_resolver) as gw:
            self.assertGreater(gw.port, 0)
            self.assertTrue(gw.url.startswith("http://127.0.0.1:"))

            # Connect to private host -> 403 Forbidden
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", gw.port))
            s.sendall(b"CONNECT private.example.org:443 HTTP/1.1\r\n\r\n")
            resp = s.recv(1024)
            s.close()
            self.assertIn(b"403 Forbidden", resp)

            # Connect to metadata host -> 403 Forbidden
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", gw.port))
            s.sendall(b"CONNECT metadata.example.org:443 HTTP/1.1\r\n\r\n")
            resp = s.recv(1024)
            s.close()
            self.assertIn(b"403 Forbidden", resp)

            # Connect to non-allowed port -> 403 Forbidden
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", gw.port))
            s.sendall(b"CONNECT grants.example.org:22 HTTP/1.1\r\n\r\n")
            resp = s.recv(1024)
            s.close()
            self.assertIn(b"403 Forbidden", resp)

    def test_egress_gateway_blocks_upstream_proxy_override(self):
        def fake_resolver(host: str, port: int) -> list[str]:
            return ["203.0.113.9"]

        with gateway.EgressGateway(upstream_proxy="http://upstream.example:8080", resolver=fake_resolver) as gw:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", gw.port))
            s.sendall(b"CONNECT grants.example.org:443 HTTP/1.1\r\n\r\n")
            resp = s.recv(1024)
            s.close()
            self.assertIn(b"403 Forbidden", resp)
            self.assertIn(b"upstream proxy overrides destination pin", resp)

    def test_egress_gateway_enforces_approved_origins(self):
        def fake_resolver(host: str, port: int) -> list[str]:
            return ["203.0.113.9"]

        with gateway.EgressGateway(approved_origins=("https://grants.example.org",), resolver=fake_resolver) as gw:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", gw.port))
            s.sendall(b"CONNECT unauthorized.example.org:443 HTTP/1.1\r\n\r\n")
            resp = s.recv(1024)
            s.close()
            self.assertIn(b"403 Forbidden", resp)
            self.assertIn(b"origin_not_approved", resp)

            # Subdomain/prefix attack (https://grants.example.org.evil must NOT match https://grants.example.org)
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s2.connect(("127.0.0.1", gw.port))
            s2.sendall(b"CONNECT grants.example.org.evil:443 HTTP/1.1\r\n\r\n")
            resp2 = s2.recv(1024)
            s2.close()
            self.assertIn(b"403 Forbidden", resp2)
            self.assertIn(b"origin_not_approved", resp2)

    def test_namespace_isolation_detection_contract(self):
        ok, reason = guard.check_namespace_isolation()
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertIn(reason, ("network_namespace_unsupported", "network_namespace_not_active"))

    def test_worker_bounded_json_ipc_serve_loop(self):
        # Test bounded JSON IPC loop over stdin
        input_line = json.dumps({
            "request": {
                "acquisition_id": "test-acq",
                "page_id": 1,
                "url": "https://grants.example.org",
                "session_id": "test-acq",
                "allowed_path_prefixes": ["/"],
                "approved_resource_origins": ["https://grants.example.org"],
                "deadline_at": 2000.0,
                "private": False,
            },
            "deadline_at": 2000.0,
        }) + "\n"
        saved_stdin = sys.stdin
        saved_stdout = sys.stdout
        sys.stdin = io.StringIO(input_line)
        sys.stdout = io.StringIO()
        try:
            ret = browser._serve_main({"SNOWBALL_CHALLENGE_FIXTURE": "1", "PATH": os.environ.get("PATH", "")})
            self.assertEqual(ret, 0)
            out = sys.stdout.getvalue().strip()
            if out:
                parsed = json.loads(out)
                self.assertIn("outcome", parsed)
        finally:
            sys.stdin = saved_stdin
            sys.stdout = saved_stdout

    def test_worker_multi_step_ipc_protocol(self):
        lines = [
            json.dumps({"action": "close", "session_handle": "nonexistent"}) + "\n",
            json.dumps({"action": "unsupported_action_name"}) + "\n",
        ]
        saved_stdin = sys.stdin
        saved_stdout = sys.stdout
        sys.stdin = io.StringIO("".join(lines))
        sys.stdout = io.StringIO()
        try:
            ret = browser._serve_main({"SNOWBALL_CHALLENGE_FIXTURE": "1", "PATH": os.environ.get("PATH", "")})
            self.assertEqual(ret, 0)
            output_lines = [json.loads(line) for line in sys.stdout.getvalue().strip().splitlines() if line.strip()]
            self.assertEqual(output_lines[0], {"status": "closed"})
            self.assertIn("error", output_lines[1])
        finally:
            sys.stdin = saved_stdin
            sys.stdout = saved_stdout


class OrchestratorWiringTests(unittest.TestCase):
    def test_fixture_defaults_keep_solver_fail_closed(self):
        cfg = orch.Config()
        self.assertFalse(cfg.challenge_fixture)
        self.assertIsNone(cfg.captcha_api_key)
        self.assertEqual(cfg.captcha_deadline_sec, 120)
    def test_fixture_off_does_not_load_policy_or_replace_the_result(self):
        cfg = orch.Config()
        router = orch.FetchRouter(cfg, None, None, False)
        html = "<html><div class='cf-turnstile'>Just a moment...</div></html>"
        result = fetch_types.FetchResult(GRANTS, "direct", "walled", 403, "", html)
        router._try_exa = lambda _canon: None
        router._try_paid = lambda *_args, **_kwargs: None
        original = orch.access_policy.load_access_policy

        def boom(*_args, **_kwargs):
            raise AssertionError("policy loaded")

        orch.access_policy.load_access_policy = boom
        try:
            self.assertIs(router._escalate(result, GRANTS, False, None), result)
            cfg.challenge_fixture = True
            cfg.captcha_api_key = "not-a-real-key"
            self.assertIs(router._finish_escalation(result, GRANTS, False, None, html), result)
        finally:
            orch.access_policy.load_access_policy = original

    def test_solver_gate_stops_before_acquire_on_the_shipped_policy(self):
        cfg = orch.Config()
        cfg.challenge_fixture = True
        cfg.captcha_api_key = SECRET
        router = orch.FetchRouter(cfg, None, None, False)
        result = fetch_types.FetchResult(GRANTS, "direct", "walled", 403, "", "x")
        calls = []

        class Gate:
            @staticmethod
            def activation_block_reason(**kwargs):
                calls.append(kwargs)
                return browser.activation_block_reason(**kwargs)

            @staticmethod
            def acquire(*_args, **_kwargs):
                raise AssertionError("acquire")

        grants = router._run_solver(result, GRANTS, None, "challenge_turnstile", "", Gate)
        self.assertIs(grants, result)
        self.assertIn("solver", calls[0]["allowed_modes"])
        calls.clear()
        docs = "https://docs.example.org/research/paper"
        kept = router._run_solver(result, docs, None, "challenge_turnstile", "", Gate)
        self.assertIs(kept, result)
        self.assertEqual(calls, [])

    def test_browser_ok_is_the_only_replacement(self):
        router = orch.FetchRouter(orch.Config(), None, None, False)
        original = fetch_types.FetchResult(GRANTS, "direct", "walled", 403, "", "short")
        fetched = SimpleNamespace(outcome="ok", status=200, title="Program", text=ARTICLE, final_url=GRANTS)
        adopted = router._adopt_browser_result(original, fetched)
        self.assertEqual(adopted.route, "browser")
        self.assertEqual(adopted.outcome, "ok")
        self.assertIn("program description", adopted.text)
        walled = SimpleNamespace(outcome="walled", status=403, title="", text=ARTICLE, final_url=GRANTS)
        self.assertIs(router._adopt_browser_result(original, walled), original)
        short = SimpleNamespace(outcome="ok", status=200, title="", text="too short", final_url=GRANTS)
        self.assertIs(router._adopt_browser_result(original, short), original)


if __name__ == "__main__":
    unittest.main()
