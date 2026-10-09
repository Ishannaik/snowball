"""Access policy and paid-budget reservations. Offline; no network and no provider calls.

python -m unittest tests.test_access_budget -v
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
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


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
pb = _load("paid_budget.py", "snowball_paid_budget")

EXAMPLE = ROOT / "access.example.yml"
GRANTS = "https://grants.example.org/opportunities/2026"
NEWS = "https://news.example.org/stories/1"
DOCS = "https://docs.example.org/research/guide"


def _previous_day(day: str) -> tuple[str, str]:
    year, month, dom = (int(part) for part in day.split("-"))
    previous = datetime(year, month, dom) - timedelta(days=1)
    return previous.date().isoformat(), previous.strftime("%Y-%m")


def _shift(store, reservation_id: int, day: str, month: str) -> None:
    with store.tx() as db:
        db.execute(
            "UPDATE paid_budget_reservations SET window_day_ist = ?, window_month_ist = ? WHERE id = ?",
            (day, month, reservation_id),
        )


class AccessPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = policy.load_access_policy(EXAMPLE)

    def test_unknown_domain_is_public_only(self):
        decision = self.policy.decide("https://example.com/docs?next=../admin")
        self.assertEqual(decision.allowed_modes, ("public",))
        self.assertIsNone(decision.denial_reason)
        self.assertEqual(decision.approved_resource_origins, ())
        self.assertEqual(decision.allowed_path_prefixes, ())
        self.assertNotIn("paid", decision.allowed_modes)
        self.assertNotIn("solver", decision.allowed_modes)

        http_origin = self.policy.decide("http://grants.example.org/opportunities/a")
        self.assertEqual(http_origin.allowed_modes, ("public",))
        self.assertIsNone(http_origin.denial_reason)

        odd_port = self.policy.decide("https://grants.example.org:80/opportunities/a")
        self.assertEqual(odd_port.allowed_modes, ("public",))
        self.assertNotIn("paid", odd_port.allowed_modes)

    def test_allowlisted_exact_origin_and_path(self):
        decision = self.policy.decide("HTTPS://Grants.Example.ORG/opportunities/a?utm_source=x")
        self.assertEqual(decision.allowed_modes, ("public", "paid"))
        self.assertNotIn("solver", decision.allowed_modes)
        self.assertIsNone(decision.denial_reason)
        self.assertEqual(
            decision.approved_resource_origins,
            ("https://challenges.cloudflare.com", "https://www.google.com/recaptcha"),
        )
        self.assertEqual(decision.allowed_path_prefixes, ("/opportunities/", "/programs/"))

        explicit = self.policy.decide("https://grants.example.org:443/programs/list")
        self.assertIn("paid", explicit.allowed_modes)

        news = self.policy.decide(NEWS)
        self.assertEqual(news.allowed_modes, ("public", "paid"))
        self.assertNotIn("solver", news.allowed_modes)

        docs = self.policy.decide(DOCS)
        self.assertEqual(docs.allowed_modes, ("public",))
        self.assertIsNone(docs.denial_reason)
        self.assertNotIn("paid", docs.allowed_modes)

        prefix_boundary = self.policy.decide("https://grants.example.org/opportunities-extra")
        self.assertEqual(prefix_boundary.allowed_modes, ("public",))
        self.assertEqual(prefix_boundary.denial_reason, "path_not_allowlisted")
        self.assertEqual(prefix_boundary.approved_resource_origins, ())
        self.assertIn("/opportunities/", prefix_boundary.allowed_path_prefixes)

        bare = self.policy.decide("https://grants.example.org/opportunities")
        self.assertIn("paid", bare.allowed_modes)

    def test_private_strips_paid_and_solver(self):
        decision = self.policy.decide(GRANTS, private=True)
        self.assertEqual(decision.allowed_modes, ("public",))
        self.assertEqual(decision.denial_reason, "private_topic")
        self.assertNotIn("paid", decision.allowed_modes)
        self.assertNotIn("solver", decision.allowed_modes)
        self.assertEqual(decision.approved_resource_origins, ())

        unknown = self.policy.decide("https://example.com/a", private=True)
        self.assertEqual(unknown.allowed_modes, ("public",))
        self.assertEqual(unknown.denial_reason, "private_topic")

        walled = self.policy.decide(GRANTS, private=True, wall_kind="login")
        self.assertEqual(walled.allowed_modes, ("public",))
        self.assertEqual(walled.denial_reason, "private_topic")
        self.assertNotIn("paid", walled.allowed_modes)

    def test_ssrf_path_and_port_denials(self):
        denied = [
            "http://127.0.0.1/",
            "http://127.0.0.2/latest",
            "HTTP://127.0.0.1/",
            "http://2130706433/",
            "http://127.1/",
            "http://0x7f000001/",
            "http://0177.0.0.1/",
            "http://localhost/admin",
            "http://localhost./",
            "http://foo.localhost/",
            "http://printer.local/",
            "http://[::1]/",
            "http://[::]/",
            "http://[fe80::1]/",
            "http://[fc00::1]/",
            "http://[ff02::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:169.254.169.254]/",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.0.1/",
            "http://169.254.169.254/latest/meta-data",
            "http://169.254.1.1/",
            "http://100.64.0.1/",
            "http://0.0.0.0/",
            "http://224.0.0.1/",
            "http://240.0.0.1/",
            "http://192.0.2.1/",
            "http://198.51.100.1/",
            "http://203.0.113.1/",
            "http://198.18.0.1/",
            "https://metadata.google.internal/",
            "https://example.com:8080/",
            "https://example.com:22/",
            "https://grants.example.org:8443/opportunities/",
            "file:///etc/passwd",
            "gopher://example.com/",
            "javascript:alert(1)",
            "https://user:pass@grants.example.org/opportunities/",
            "http://example.com@127.0.0.1/",
            "https://grants.example.org/opportunities/../../etc/passwd",
            "https://grants.example.org/%2e%2e/opportunities",
            "https://grants.example.org/opportunities/%2e%2e/secret",
            "https://example.com/a\\..\\b",
        ]
        for url in denied:
            decision = self.policy.decide(url, private=True, wall_kind="challenge_turnstile")
            self.assertEqual(decision.allowed_modes, (), url)
            self.assertEqual(decision.denial_reason, "ssrf_or_path_escape", url)
            self.assertEqual(decision.approved_resource_origins, (), url)

    def test_special_use_cidr_boundaries(self):
        denied = [
            "http://10.0.0.0/",
            "http://10.255.255.255/",
            "http://172.16.0.0/",
            "http://172.31.255.255/",
            "http://192.168.0.0/",
            "http://192.168.255.255/",
            "http://100.64.0.0/",
            "http://100.127.255.255/",
            "http://169.254.0.1/",
            "http://169.254.255.255/",
            "http://127.255.255.255/",
            "http://255.255.255.255/",
        ]
        allowed = [
            "http://9.255.255.255/",
            "http://11.0.0.0/",
            "http://172.15.255.255/",
            "http://172.32.0.0/",
            "http://192.167.0.1/",
            "http://192.169.0.1/",
            "http://100.63.255.255/",
            "http://100.128.0.1/",
            "http://169.253.255.255/",
            "http://169.255.0.1/",
            "http://126.255.255.255/",
            "http://128.0.0.1/",
        ]
        for url in denied:
            decision = self.policy.decide(url)
            self.assertEqual(decision.denial_reason, "ssrf_or_path_escape", url)
            self.assertEqual(decision.allowed_modes, (), url)
        for url in allowed:
            decision = self.policy.decide(url)
            self.assertEqual(decision.allowed_modes, ("public",), url)
            self.assertIsNone(decision.denial_reason, url)
            self.assertNotIn("paid", decision.allowed_modes, url)
            self.assertNotIn("solver", decision.allowed_modes, url)

    def test_terminal_walls_never_escalate(self):
        cases = [
            ("login", "login"),
            ("paywall", "paywall"),
            ("generic_401", "generic_401"),
            ("generic_403", "generic_403"),
            ("generic_407", "generic_407"),
            ("generic_451", "generic_451"),
            ("429", "429"),
            ("ban", "ban"),
            ("robots", "robots"),
            ("LOGIN", "login"),
            (401, "generic_401"),
            (403, "generic_403"),
            (407, "generic_407"),
            (429, "429"),
            (451, "generic_451"),
            ("401", "generic_401"),
            ("403", "generic_403"),
            ("407", "generic_407"),
            ("451", "generic_451"),
        ]
        required = {
            "login",
            "paywall",
            "robots",
            "ban",
            "429",
            "generic_401",
            "generic_403",
            "generic_407",
            "generic_451",
        }
        self.assertTrue(required <= set(self.policy.terminal_walls))
        for wall, reason in cases:
            decision = self.policy.decide(GRANTS, wall_kind=wall)
            self.assertEqual(decision.allowed_modes, ("public",), wall)
            self.assertEqual(decision.denial_reason, reason, wall)
            self.assertNotIn("paid", decision.allowed_modes, wall)
            self.assertNotIn("solver", decision.allowed_modes, wall)
            self.assertEqual(decision.approved_resource_origins, (), wall)
        unknown = self.policy.decide("https://example.com/a", wall_kind="robots")
        self.assertEqual(unknown.allowed_modes, ("public",))
        self.assertEqual(unknown.denial_reason, "robots")

    def test_authorized_challenge_can_use_solver(self):
        for wall in ("challenge_turnstile", "challenge_recaptcha"):
            decision = self.policy.decide(GRANTS, wall_kind=wall)
            self.assertEqual(decision.allowed_modes, ("public", "paid", "solver"), wall)
            self.assertIsNone(decision.denial_reason, wall)
            self.assertIn("https://challenges.cloudflare.com", decision.approved_resource_origins)

        blocked = self.policy.decide(GRANTS, wall_kind=403)
        self.assertEqual(blocked.allowed_modes, ("public",))
        self.assertEqual(blocked.denial_reason, "generic_403")
        self.assertNotIn("solver", blocked.allowed_modes)
        self.assertNotIn("paid", blocked.allowed_modes)

        news = self.policy.decide(NEWS, wall_kind="challenge_turnstile")
        self.assertEqual(news.allowed_modes, ("public",))
        self.assertEqual(news.denial_reason, "challenge_not_authorized")
        self.assertNotIn("paid", news.allowed_modes)
        self.assertNotIn("solver", news.allowed_modes)

        unknown = self.policy.decide("https://example.com/a", wall_kind="challenge_turnstile")
        self.assertEqual(unknown.allowed_modes, ("public",))
        self.assertEqual(unknown.denial_reason, "challenge_not_authorized")

        off_path = self.policy.decide("https://grants.example.org/admin", wall_kind="challenge_turnstile")
        self.assertEqual(off_path.denial_reason, "path_not_allowlisted")
        self.assertNotIn("solver", off_path.allowed_modes)
        self.assertNotIn("paid", off_path.allowed_modes)

        private = self.policy.decide(GRANTS, private=True, wall_kind="challenge_turnstile")
        self.assertEqual(private.allowed_modes, ("public",))
        self.assertEqual(private.denial_reason, "private_topic")
        self.assertNotIn("solver", private.allowed_modes)

    def test_policy_reload_drops_sessions(self):
        first = policy.load_access_policy(EXAMPLE)
        second = policy.load_access_policy(EXAMPLE)
        self.assertIs(first, second)
        self.assertFalse(first.stale)
        self.assertTrue(first.fingerprint)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "access.yml"
            target.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            old = policy.load_access_policy(target)
            old.bind_session("browser-1", {"scoped": True})
            self.assertIsNotNone(old.session("browser-1"))
            target.write_text(
                target.read_text(encoding="utf-8").replace("grants.example.org", "grants.example.net"),
                encoding="utf-8",
            )
            updated = policy.load_access_policy(target)
            self.assertTrue(old.stale)
            self.assertIsNone(old.session("browser-1"))
            with self.assertRaises(policy.AccessPolicyStale):
                old.bind_session("browser-2", object())
            self.assertFalse(updated.stale)
            self.assertIn("paid", updated.decide("https://grants.example.net/opportunities/a").allowed_modes)
            self.assertNotIn("paid", updated.decide("https://grants.example.org/opportunities/a").allowed_modes)

    def test_example_ceilings_are_zero_and_secret_free(self):
        text = EXAMPLE.read_text(encoding="utf-8").lower()
        for banned in ("api_key", "apikey", "secret:", "password", "bearer ", "sk-"):
            self.assertNotIn(banned, text)
        caps = self.policy.caps()
        for name in (
            "daily_micro_usd",
            "monthly_micro_usd",
            "daily_retrieval_count",
            "daily_solver_count",
            "monthly_solver_count",
        ):
            self.assertEqual(caps[name], 0, name)
        self.assertEqual(caps["tariffs"], {"zenrows": 0, "residential": 0, "captcha": 0})
        for provider_caps in caps["providers"].values():
            for value in provider_caps.values():
                self.assertEqual(value, 0)

    def test_config_cannot_widen_unknown_or_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            widened = Path(tmp) / "wide.yml"
            widened.write_text(
                _minimal_policy().replace("modes: [public]", "modes: [public, paid]", 1),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                policy.load_access_policy(widened)
            remapped = Path(tmp) / "remap.yml"
            remapped.write_text(
                _minimal_policy() + '\nterminal_statuses:\n  "403": challenge_turnstile\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                policy.load_access_policy(remapped)
            duplicate = Path(tmp) / "dup.yml"
            duplicate.write_text(_minimal_policy() + _minimal_policy().split("domains:", 1)[1], encoding="utf-8")
            with self.assertRaises(ValueError):
                policy.load_access_policy(duplicate)
        with self.assertRaises(FileNotFoundError):
            policy.load_access_policy(ROOT / "missing-access-policy.yml")


class PaidBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "budget.db"
        self.store = orch.Store.open(self.path)
        pb.ensure_schema(self.store)
        pb.ensure_schema(self.store)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_fourth_reservation_fails_at_count_cap(self):
        caps = {"daily_retrieval_count": 3, "tariffs": {"zenrows": 100000}}

        def next_acquisition():
            next_acquisition.n += 1
            return f"acq-{next_acquisition.n}"

        next_acquisition.n = 0
        reserve = pb.reserve
        PaidBudgetExhausted = pb.PaidBudgetExhausted
        for _ in range(3):
            reservation = reserve(self.store, next_acquisition(), "zenrows", 100000, caps=caps)
            self.assertFalse(reservation.settled)
            self.assertIsNone(reservation.actual_micro_usd)
            self.assertEqual(reservation.reserved_micro_usd, 100000)
        with self.assertRaises(PaidBudgetExhausted):
            reserve(self.store, next_acquisition(), "zenrows", 100000, caps=caps)
        self.assertEqual(self.store.one("SELECT COUNT(*) AS n FROM paid_budget_reservations")["n"], 3)

    def test_concurrent_reservations_cannot_overspend(self):
        caps = {
            "daily_micro_usd": 300000,
            "monthly_micro_usd": 300000,
            "daily_retrieval_count": 100,
            "tariffs": {"zenrows": 100000},
        }
        stores = []
        for _ in range(8):
            extra = orch.Store.open(self.path)
            pb.ensure_schema(extra)
            stores.append(extra)
        barrier = threading.Barrier(8)
        ok: list[int] = []
        denied: list[int] = []
        recorded = threading.Lock()

        def worker(index: int, store) -> None:
            try:
                barrier.wait(15)
                try:
                    reservation = pb.reserve(store, f"acq-{index}", "zenrows", 100000, caps=caps)
                    with recorded:
                        ok.append(reservation.reserved_micro_usd)
                except pb.PaidBudgetExhausted:
                    with recorded:
                        denied.append(index)
            finally:
                store.db.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, index, store) for index, store in enumerate(stores)]
            for future in futures:
                future.result(timeout=60)
        self.assertEqual(len(ok), 3)
        self.assertEqual(len(denied), 5)
        spent = self.store.one("SELECT COALESCE(SUM(reserved_micro_usd), 0) AS n FROM paid_budget_reservations")["n"]
        self.assertEqual(spent, 300000)

    def test_unknown_and_crash_do_not_reopen_budget(self):
        caps = {"daily_micro_usd": 100000, "tariffs": {"zenrows": 100000}}
        first = pb.reserve(self.store, "acq-1", "zenrows", 100000, caps=caps)
        with self.assertRaises(pb.PaidBudgetExhausted):
            pb.reserve(self.store, "acq-2", "zenrows", 100000, caps=caps)
        pb.settle(self.store, first.id, None, "unknown")
        row = self.store.one(
            "SELECT settled, actual_micro_usd, reserved_micro_usd, outcome FROM paid_budget_reservations WHERE id = ?",
            (first.id,),
        )
        self.assertEqual(row["settled"], 1)
        self.assertIsNone(row["actual_micro_usd"])
        self.assertEqual(row["reserved_micro_usd"], 100000)
        self.assertEqual(row["outcome"], "unknown")
        with self.assertRaises(pb.PaidBudgetExhausted):
            pb.reserve(self.store, "acq-3", "zenrows", 100000, caps=caps)
        previous_day, previous_month = _previous_day(first.window_day_ist)
        _shift(self.store, first.id, previous_day, previous_month)
        with self.assertRaises(pb.PaidBudgetExhausted):
            pb.reserve(self.store, "acq-4", "zenrows", 100000, caps=caps)
        pb.settle(self.store, first.id, None, pb.CONFIRMED_NONBILLABLE)
        released = self.store.one("SELECT actual_micro_usd FROM paid_budget_reservations WHERE id = ?", (first.id,))
        self.assertEqual(released["actual_micro_usd"], 0)
        follow = pb.reserve(self.store, "acq-5", "zenrows", 100000, caps=caps)
        self.assertEqual(follow.reserved_micro_usd, 100000)

    def test_unconfirmed_zero_keeps_worst_case(self):
        caps = {"daily_micro_usd": 100000, "tariffs": {"zenrows": 100000}}
        reservation = pb.reserve(self.store, "acq-1", "zenrows", 100000, caps=caps)
        pb.settle(self.store, reservation.id, 0, "provider_error")
        row = self.store.one("SELECT actual_micro_usd FROM paid_budget_reservations WHERE id = ?", (reservation.id,))
        self.assertIsNone(row["actual_micro_usd"])
        with self.assertRaises(pb.PaidBudgetExhausted):
            pb.reserve(self.store, "acq-2", "zenrows", 100000, caps=caps)
        pb.settle(self.store, reservation.id, 0, pb.CONFIRMED_NONBILLABLE)
        pb.reserve(self.store, "acq-3", "zenrows", 100000, caps=caps)

    def test_rollover_keeps_unsettled_liability(self):
        caps = {"monthly_micro_usd": 100000, "tariffs": {"zenrows": 100000}}
        settled = pb.reserve(self.store, "acq-settled", "zenrows", 100000, caps=caps)
        pb.settle(self.store, settled.id, 100000, "billed")
        old_day, old_month = _previous_day(f"{settled.window_month_ist}-01")
        _shift(self.store, settled.id, old_day, old_month)
        current = pb.reserve(self.store, "acq-current", "zenrows", 100000, caps=caps)
        _shift(self.store, current.id, old_day, old_month)
        with self.assertRaises(pb.PaidBudgetExhausted):
            pb.reserve(self.store, "acq-blocked", "zenrows", 100000, caps=caps)

        day_caps = {"daily_micro_usd": 100000, "tariffs": {"zenrows": 100000}}
        day_store = orch.Store.open(self.path.parent / "day.db")
        try:
            done = pb.reserve(day_store, "day-settled", "zenrows", 100000, caps=day_caps)
            pb.settle(day_store, done.id, 100000, "billed")
            previous_day, previous_month = _previous_day(done.window_day_ist)
            _shift(day_store, done.id, previous_day, previous_month)
            reopened = pb.reserve(day_store, "day-new", "zenrows", 100000, caps=day_caps)
            _shift(day_store, reopened.id, previous_day, previous_month)
            with self.assertRaises(pb.PaidBudgetExhausted):
                pb.reserve(day_store, "day-blocked", "zenrows", 100000, caps=day_caps)
        finally:
            day_store.db.close()

    def test_ist_window_boundaries(self):
        before = datetime(2026, 10, 7, 18, 29, tzinfo=timezone.utc)
        after = datetime(2026, 10, 7, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(pb.ist_windows(before), ("2026-10-07", "2026-10"))
        self.assertEqual(pb.ist_windows(after), ("2026-10-08", "2026-10"))
        month_before = datetime(2026, 9, 30, 18, 29, tzinfo=timezone.utc)
        month_after = datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(pb.ist_windows(month_before), ("2026-09-30", "2026-09"))
        self.assertEqual(pb.ist_windows(month_after), ("2026-10-01", "2026-10"))

        reservation = pb.reserve(
            self.store,
            "acq-window",
            "zenrows",
            1000,
            caps={"tariffs": {"zenrows": 1000}},
        )
        created = datetime.strptime(reservation.created_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertEqual((reservation.window_day_ist, reservation.window_month_ist), pb.ist_windows(created))
        self.assertRegex(reservation.created_at_utc, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_settle_exact_and_clamped(self):
        caps = {"daily_micro_usd": 180000, "tariffs": {"zenrows": 1000}}
        first = pb.reserve(self.store, "acq-1", "zenrows", 100000, caps=caps)
        pb.settle(self.store, first.id, 40000, "billed", task_ref="task-9")
        row = self.store.one(
            "SELECT actual_micro_usd, task_ref, reserved_micro_usd FROM paid_budget_reservations WHERE id = ?",
            (first.id,),
        )
        self.assertEqual(row["actual_micro_usd"], 40000)
        self.assertEqual(row["task_ref"], "task-9")
        self.assertEqual(row["reserved_micro_usd"], 100000)
        second = pb.reserve(self.store, "acq-2", "zenrows", 100000, caps=caps)
        self.assertEqual(second.reserved_micro_usd, 100000)

        other = orch.Store.open(self.path.parent / "clamp.db")
        try:
            reserved = pb.reserve(other, "acq-c", "zenrows", 100000, caps=caps)
            pb.settle(other, reserved.id, 250000, "billed")
            stored = other.one("SELECT actual_micro_usd FROM paid_budget_reservations WHERE id = ?", (reserved.id,))
            self.assertEqual(stored["actual_micro_usd"], 100000)
            follow = pb.reserve(other, "acq-d", "zenrows", 80000, caps=caps)
            self.assertEqual(follow.reserved_micro_usd, 80000)
        finally:
            other.db.close()

    def test_solver_and_provider_caps(self):
        solver_caps = {"daily_solver_count": 3, "tariffs": {"captcha": 50, "zenrows": 50}}
        for index in range(3):
            pb.reserve(self.store, f"solve-{index}", "captcha", 50, is_solver=True, caps=solver_caps)
        with self.assertRaises(pb.PaidBudgetExhausted):
            pb.reserve(self.store, "solve-extra", "captcha", 50, is_solver=True, caps=solver_caps)
        pb.reserve(self.store, "fetch-ok", "zenrows", 50, is_solver=False, caps=solver_caps)

        provider_caps = {
            "tariffs": {"zenrows": 10, "residential": 10},
            "providers": {"zenrows": {"daily_count": 1}},
        }
        fresh = orch.Store.open(self.path.parent / "provider.db")
        try:
            pb.reserve(fresh, "z1", "zenrows", 10, caps=provider_caps)
            with self.assertRaises(pb.PaidBudgetExhausted):
                pb.reserve(fresh, "z2", "zenrows", 10, caps=provider_caps)
            pb.reserve(fresh, "r1", "residential", 10, caps=provider_caps)
        finally:
            fresh.db.close()

    def test_missing_tariff_disables_route(self):
        with self.assertRaises(pb.PaidBudgetExhausted):
            pb.reserve(self.store, "acq", "zenrows", 100000, caps=None)
        disabled = [
            {},
            {"daily_retrieval_count": 3},
            {"daily_retrieval_count": 3, "tariffs": {}},
            {"daily_retrieval_count": 3, "tariffs": {"zenrows": 0}},
            {"daily_micro_usd": 0, "tariffs": {"zenrows": 100000}},
            {"tariffs": {"zenrows": 1000}},
        ]
        for caps in disabled:
            with self.assertRaises(pb.PaidBudgetExhausted):
                pb.reserve(
                    self.store,
                    "acq",
                    "zenrows",
                    999 if caps.get("tariffs", {}).get("zenrows") == 1000 else 100000,
                    caps=caps,
                )
        with self.assertRaises(pb.PaidBudgetExhausted) as caught:
            pb.reserve(
                self.store,
                "acq-tariff",
                "zenrows",
                100000,
                caps={"daily_micro_usd": 10**9, "tariffs": {"zenrows": 0}},
            )
        self.assertIn("tariff", str(caught.exception))
        with self.assertRaises(ValueError):
            pb.reserve(self.store, "acq", "zenrows", 1.5, caps={"tariffs": {"zenrows": 1}})
        with self.assertRaises(LookupError):
            pb.settle(self.store, 999999, 0, pb.CONFIRMED_NONBILLABLE)

        example = policy.load_access_policy(EXAMPLE)
        with self.assertRaises(pb.PaidBudgetExhausted) as from_example:
            pb.reserve(self.store, "example", "zenrows", 1, caps=example.caps())
        self.assertIn("tariff", str(from_example.exception))


def _minimal_policy() -> str:
    return """
version: 1
unknown_domains:
  modes: [public]
budgets:
  daily_micro_usd: 0
  monthly_micro_usd: 0
  daily_retrieval_count: 0
  daily_solver_count: 0
  monthly_solver_count: 0
  tariffs: {}
  providers: {}
domains:
  - origin: https://grants.example.org
    path_prefixes: [/opportunities/]
    modes: [public]
    zenrows: false
"""


if __name__ == "__main__":
    unittest.main()
