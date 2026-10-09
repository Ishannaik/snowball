"""Paid fetch and egress checks. Offline: transports are fakes, providers are not called.

python -m unittest tests.test_access_fetch -v
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TOOLS = ROOT / "tools"
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _TOOLS]
if (
    getattr(sys.modules.get("queue"), "__file__", None)
    and Path(sys.modules["queue"].__file__).resolve().parent == _TOOLS
):
    del sys.modules["queue"]

import importlib.util
import json
import tempfile
import unittest


def _load(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, _TOOLS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


orch = _load("orchestrator.py", "orchestrator")
policy = _load("access_policy.py", "snowball_access_policy")
paid = _load("paid_fetch.py", "snowball_paid_fetch")
guard = _load("egress_guard.py", "snowball_egress_guard")

EXAMPLE = ROOT / "access.example.yml"
GRANTS = "https://grants.example.org/opportunities/2026"
ARTICLE = (
    "The Mozilla Foundation offers grants of up to $50,000 for open-source projects that make AI "
    "more accountable. Applications close on 2026-12-15. Individuals and organisations from any "
    "country, including India, may apply. This page is the program description and not a challenge."
)
SECRET = "zenrows-test-key-should-not-leak"
PROXY = "http://proxyuser:proxy-password-secret@proxy.example:8000"
STICKY_FAIL_CLOSED = "http://customer-sessid_oneip-abc:proxy-password-secret@proxy.example:8000"
STICKY_ZENROWS = "http://proxyuser:pass_ttl-30m_session-abc12@superproxy.zenrows.com:1337"


def _caps(daily_count: int = 10, daily_micro: int = 5_000_000) -> dict:
    return {
        "daily_micro_usd": daily_micro,
        "monthly_micro_usd": 50_000_000,
        "daily_retrieval_count": daily_count,
        "daily_solver_count": 0,
        "monthly_solver_count": 0,
        "tariffs": {"zenrows": 1000, "residential": 1000},
        "providers": {
            "zenrows": {"daily_count": daily_count, "monthly_count": 100},
            "residential": {"daily_count": daily_count, "monthly_count": 100},
        },
    }


def _public(host: str, _port: int) -> list[str]:
    if host == "grants.example.org":
        return ["93.184.216.34"]
    if host == "news.example.org":
        return ["93.184.216.34"]
    return ["203.0.113.10"]


class EgressTests(unittest.TestCase):
    def test_mixed_dns_answer_is_denied_before_any_dial(self):
        def resolver(_host: str, _port: int) -> list[str]:
            return ["93.184.216.34", "127.0.0.1"]

        with self.assertRaises(guard.EgressDenied) as caught:
            guard.validate_target("https://example.com/a", resolver=resolver)
        self.assertEqual(caught.exception.reason, "private_answer")

    def test_redirect_target_classes_are_denied(self):
        for url in (
            "http://169.254.169.254/latest",
            "https://100.64.0.1/",
            "https://metadata.google.internal/computeMetadata/v1/",
            "wss://grants.example.org/socket",
            "https://93.184.216.34/a",
            "https://example.com:8080/a",
        ):
            with self.assertRaises(guard.EgressDenied):
                guard.validate_target(url, resolver=_public)

    def test_proxy_env_and_userinfo_redaction(self):
        self.assertEqual(guard.proxy_env_names({"HTTP_PROXY": "http://user:pw@h:1", "PATH": "/bin"}), ("HTTP_PROXY",))
        scrubbed = guard.scrub_proxy_env({"http_proxy": "x", "PATH": "/bin"})
        self.assertEqual(scrubbed, {"PATH": "/bin"})
        text = guard.redact("failed http://proxyuser:proxy-password-secret@proxy.example:8000 key " + SECRET, (SECRET,))
        self.assertNotIn(SECRET, text)
        self.assertNotIn("proxy-password-secret", text)
        self.assertIn("[redacted]", text)


class PaidFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = policy.load_access_policy(EXAMPLE)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = orch.Store.open(Path(self.tmp.name) / "s.db")
        self.page_id = self._seed()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def _seed(self) -> int:
        with self.store.tx() as db:
            db.execute("INSERT INTO domains(domain) VALUES ('grants.example.org')")
            db.execute(
                "INSERT INTO pages(canon_url, raw_urls, domain, predicted_kind, next_check_at, interval_sec, "
                "discovered_via, created_at) VALUES (?,?,?,?,0,86400,'seed',0)",
                (GRANTS, json.dumps([GRANTS]), "grants.example.org", "item"),
            )
            return int(db.execute("SELECT id FROM pages").fetchone()[0])

    def _page(self, **overrides) -> paid.PageContext:
        values = {
            "page_id": self.page_id,
            "domain": "grants.example.org",
            "predicted_kind": "item",
            "yield_score": 0.5,
            "paid_failures": 0,
            "paid_attempts": 0,
        }
        values.update(overrides)
        return paid.PageContext(**values)

    def _cfg(self, **overrides) -> paid.PaidConfig:
        values = {
            "provider": "zenrows",
            "zenrows_mode": "auto",
            "zenrows_api_key": SECRET,
            "min_yield": 0.30,
        }
        values.update(overrides)
        return paid.PaidConfig(**values)

    def _go(self, transport, **kwargs):
        args = {
            "url": GRANTS,
            "private": False,
            "wall_kind": None,
            "policy": self.policy,
            "caps": _caps(),
            "store": self.store,
            "cfg": self._cfg(),
            "page": self._page(),
            "transport": transport,
            "resolver": _public,
            "environ": {},
        }
        args.update(kwargs)
        return paid.maybe_escalate(**args)

    def test_gates_make_no_provider_call(self):
        calls: list = []

        def transport(spec):
            calls.append(spec)
            raise AssertionError("transport")

        blocked = [
            self._go(transport, url="https://unknown.example/a"),
            self._go(transport, private=True),
            self._go(transport, wall_kind="login"),
            self._go(transport, wall_kind="429"),
            self._go(transport, wall_kind="robots"),
            self._go(transport, wall_kind="unsupported_challenge"),
            self._go(transport, page=self._page(predicted_kind="article")),
            self._go(transport, page=self._page(yield_score=0.1)),
            self._go(transport, cfg=self._cfg(zenrows_api_key=None)),
            self._go(transport, cfg=self._cfg(provider="")),
            self._go(transport, caps=_caps(daily_micro=0)),
            self._go(transport, url="https://docs.example.org/research/guide"),
        ]
        self.assertEqual(calls, [])
        self.assertTrue(all(item.called is False for item in blocked))
        reasons = {item.reason for item in blocked}
        self.assertIn("yield_below_0.30", reasons)
        self.assertIn("credential_missing", reasons)
        self.assertIn("ceilings_inactive", reasons)
        self.assertIn("provider_not_configured", reasons)
        self.assertEqual(self.store.one("SELECT COUNT(*) AS n FROM paid_fetches")["n"], 0)

    def test_auto_mode_keeps_body_only_inside_the_same_allowlist_entry(self):
        calls: list = []

        def transport(spec):
            calls.append(spec)
            return paid.HttpExchange(
                200,
                {"Zr-Final-Url": GRANTS, "X-Request-Id": "req1"},
                ARTICLE.encode(),
            )

        attempt = self._go(transport)
        self.assertTrue(attempt.called)
        self.assertEqual(attempt.outcome, "ok")
        self.assertIn("50,000", attempt.text)
        self.assertEqual(attempt.credit_weight, 25)
        params = calls[0]["params"]
        self.assertEqual(params["mode"], "auto")
        self.assertEqual(params["original_status"], "true")
        self.assertNotIn("js_render", params)
        self.assertNotIn("premium_proxy", params)
        self.assertNotIn("session_id", params)
        self.assertNotIn(SECRET, attempt.error)
        row = self.store.one("SELECT cost_units, reservation_state, reserved_micro_usd FROM paid_fetches")
        self.assertEqual(row["cost_units"], 25)
        self.assertEqual(row["reserved_micro_usd"], 1000)
        reservation = self.store.one("SELECT actual_micro_usd, outcome, settled FROM paid_budget_reservations")
        self.assertIsNone(reservation["actual_micro_usd"])
        self.assertEqual(reservation["outcome"], "awaiting_usage_reconciliation")
        self.assertEqual(reservation["settled"], 1)

    def test_missing_or_foreign_final_url_discards_the_body(self):
        def missing(_spec):
            return paid.HttpExchange(200, {}, ARTICLE.encode())

        attempt = self._go(missing)
        self.assertEqual(attempt.outcome, "failure")
        self.assertEqual(attempt.text, "")
        held = self.store.one("SELECT actual_micro_usd FROM paid_budget_reservations")
        self.assertIsNone(held["actual_micro_usd"])

        def other_host(_spec):
            return paid.HttpExchange(
                200,
                {"Zr-Final-Url": "https://news.example.org/stories/1"},
                ARTICLE.encode(),
            )

        second = self._go(other_host, page=self._page(paid_attempts=1))
        self.assertEqual(second.outcome, "failure")
        self.assertEqual(second.text, "")

    def test_provider_json_and_challenge_html_are_not_content(self):
        def provider_error(_spec):
            body = json.dumps({"code": "REQS001", "title": "forbidden", "detail": ARTICLE}).encode()
            return paid.HttpExchange(400, {"Zr-Final-Url": GRANTS}, body)

        attempt = self._go(provider_error)
        self.assertEqual(attempt.outcome, "failure")
        self.assertEqual(attempt.text, "")
        self.assertNotIn("Mozilla", attempt.text)

        def challenge(_spec):
            html = f"<html>Just a moment...<div class='cf-turnstile'></div>{ARTICLE}</html>"
            return paid.HttpExchange(200, {"Zr-Final-Url": GRANTS}, html.encode())

        walled = self._go(challenge, page=self._page(paid_attempts=1))
        self.assertEqual(walled.outcome, "walled")
        self.assertEqual(walled.text, "")
        self.assertEqual(walled.wall_kind, "challenge")

    def test_nonbillable_error_releases_and_timeout_holds(self):
        def nonbillable(_spec):
            return paid.HttpExchange(400, {}, b'{"code":"REQS002","title":"needs render"}')

        released = self._go(nonbillable)
        row = self.store.one(
            "SELECT actual_micro_usd, outcome FROM paid_budget_reservations WHERE id=?",
            (released.reservation_id,),
        )
        self.assertEqual(row["actual_micro_usd"], 0)
        self.assertEqual(row["outcome"], paid.CONFIRMED_NONBILLABLE)

        def timeout(_spec):
            raise paid.TransportError(f"timed out apikey={SECRET}", timeout=True)

        held = self._go(timeout, page=self._page(paid_attempts=1))
        self.assertTrue(held.called)
        self.assertEqual(held.outcome, "outage")
        self.assertNotIn(SECRET, held.error)
        reservation = self.store.one(
            "SELECT actual_micro_usd, outcome FROM paid_budget_reservations WHERE id=?",
            (held.reservation_id,),
        )
        self.assertIsNone(reservation["actual_micro_usd"])
        self.assertEqual(reservation["outcome"], "unknown")

    def test_fourth_reservation_does_not_call(self):
        calls = {"n": 0}

        def transport(_spec):
            calls["n"] += 1
            return paid.HttpExchange(200, {"Zr-Final-Url": GRANTS, "X-Request-Credits": "25"}, ARTICLE.encode())

        results = [
            self._go(transport, page=self._page(paid_attempts=0), caps=_caps(daily_count=3), cfg=self._cfg())
            for _ in range(4)
        ]
        self.assertEqual(calls["n"], 3)
        self.assertTrue(all(item.called for item in results[:3]))
        self.assertFalse(results[3].called)
        self.assertEqual(results[3].reason, "budget")

    def test_residential_redirect_to_private_does_not_dial_and_errors_are_redacted(self):
        calls: list[str] = []

        def transport(spec):
            calls.append(spec["url"])
            self.assertNotIn("http_proxy", spec["environ"])
            if spec["url"] == GRANTS:
                return paid.HttpExchange(302, {"location": "http://169.254.169.254/latest"}, b"")
            raise AssertionError(spec["url"])

        attempt = self._go(
            transport,
            cfg=self._cfg(provider="residential", residential_proxy_url=PROXY, zenrows_api_key=None),
        )
        self.assertEqual(calls, [GRANTS])
        self.assertEqual(attempt.outcome, "ssrf_blocked")
        self.assertNotIn("proxy-password-secret", attempt.error)

        def boom(_spec):
            raise RuntimeError(f"curl failed for {PROXY}")

        blown = self._go(
            boom,
            cfg=self._cfg(provider="residential", residential_proxy_url=PROXY, zenrows_api_key=None),
            page=self._page(paid_attempts=1),
        )
        self.assertNotIn("proxy-password-secret", blown.error)
        self.assertNotIn("proxyuser", blown.error)

    def test_residential_proxy_rules(self):
        calls: list = []

        def transport(spec):
            calls.append(spec["url"])
            return paid.HttpExchange(200, {}, ARTICLE.encode())

        no_auth = self._go(
            transport,
            cfg=self._cfg(
                provider="residential", residential_proxy_url="http://proxy.example:8000", zenrows_api_key=None
            ),
        )
        inherited = self._go(
            transport,
            cfg=self._cfg(provider="residential", residential_proxy_url=PROXY, zenrows_api_key=None),
            environ={"https_proxy": "http://other:1"},
        )
        unproven = self._go(
            transport,
            wall_kind="challenge_turnstile",
            cfg=self._cfg(provider="residential", residential_proxy_url=STICKY_ZENROWS, zenrows_api_key=None),
        )
        self.assertEqual(calls, [])
        self.assertEqual(
            [no_auth.reason, inherited.reason, unproven.reason],
            ["proxy_not_authenticated", "environment_proxy", "node_loss_unproven"],
        )

        allowed = self._go(
            transport,
            wall_kind="challenge_turnstile",
            cfg=self._cfg(provider="residential", residential_proxy_url=STICKY_FAIL_CLOSED, zenrows_api_key=None),
        )
        self.assertTrue(allowed.called)
        self.assertEqual(calls, [GRANTS])
        self.assertEqual(allowed.outcome, "ok")

    def test_exa_contents_ignores_snippets_and_short_bodies(self):
        self.assertIsNone(paid.read_exa_contents({"results": [{"title": "t", "text": "short"}]}))
        self.assertIsNone(paid.read_exa_contents({"snippet": ARTICLE}))
        title, text = paid.read_exa_contents({"results": [{"title": "Program", "text": ARTICLE}]})
        self.assertEqual(title, "Program")
        self.assertIn("50,000", text)


if __name__ == "__main__":
    unittest.main()
