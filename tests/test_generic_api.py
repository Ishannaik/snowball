"""The generic console API: real sessions, CAS, idempotency and honest failures.

These tests exercise observable HTTP behavior. Routes that reach the durable
store use the real generic_runtime.Service; the two API-layer suites
(idempotency recording, run-thread gate) inject their own Service-contract
fakes through serve()'s runtime_factory so storage-seam drift cannot mask
regressions in the API's own guarantees.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = load("generic_api")


class RecordingRuntime:
    """Minimum Service contract surface the API uses (create/view/list).

    In-contract stand-in for the API's own persistence tests. State persists in
    a class-level registry keyed by database path because the handler builds a
    fresh instance per request.
    """

    REGISTRY: dict[str, dict[str, dict]] = {}

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.books = RecordingRuntime.REGISTRY.setdefault(str(db_path), {})

    def create(self, title, goal_id=None):
        goal_id = goal_id or "goal-generated-" + title[:6].replace(" ", "-").lower()
        if goal_id in self.books:
            err = ValueError("duplicate goal id")
            err.field = "goal_id"
            raise err
        self.books[goal_id] = {"id": goal_id, "title": title, "state": "draft", "row_version": 0, "epoch": 0}
        return {"goal": dict(self.books[goal_id]), "draft": {"questions": [], "answers": []}}

    def view(self, goal_id):
        if goal_id not in self.books:
            err = ValueError("missing goal")
            err.field = "goal_id"
            raise err
        return {"goal": dict(self.books[goal_id]), "draft": {}}

    def list(self):
        return [dict(g) for g in self.books.values()]

    def answer(self, goal_id, question_id, answer, row_version=None):
        view = self.view(goal_id)
        updated = dict(view["goal"])
        updated["row_version"] = (row_version or 0) + 1
        self.books[goal_id] = updated
        return {**view, "goal": updated, "draft": {"questions": [{"id": question_id}], "answers": [{"question_id": question_id}]}}


class BlockingRunRuntime(RecordingRuntime):
    """A running goal whose run() blocks until released: exercises the gate."""

    RUN_STARTED = None
    RUN_RELEASE = None

    def view(self, goal_id):
        base = super().view(goal_id)
        base["goal"]["state"] = "running"
        return base

    def run(self, goal_id, max_waves=5):
        BlockingRunRuntime.RUN_STARTED.set()
        BlockingRunRuntime.RUN_RELEASE.wait(timeout=30)
        return self.view(goal_id)


class Console:
    """A live API server with one authenticated browser session."""

    def __init__(self, directory: Path, runtime_factory=None):
        self.db = directory / "console.db"
        self.server = api.serve(
            self.db, "boot-token-123", "http://127.0.0.1", port=0,
            runtime_factory=runtime_factory or self.real_runtime,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.cookie = None
        self.csrf = None

    def real_runtime(self, db_path):
        return load("generic_runtime").Service(db_path)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # raw request without session handling
    def request(self, method: str, path: str, payload=None, headers=None):
        data = None
        hdrs = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req) as response:
                body = response.read()
                return response.status, (json.loads(body) if body else {}), dict(response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return exc.code, (json.loads(body) if body else {}), dict(exc.headers)

    def authenticate(self, token: str = "boot-token-123"):
        status, payload, headers = self.request("POST", "/api/session", {"token": token})
        assert status == 200, (status, payload)
        self.csrf = payload["csrf"]
        self.cookie = headers["Set-Cookie"].split(";")[0]
        return self

    def authed(self, extra=None):
        headers = {"Cookie": self.cookie, "X-CSRF-Token": self.csrf, "Origin": "http://127.0.0.1"}
        headers.update(extra or {})
        return headers

    def anon_headers(self) -> dict:
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    # convenience wrappers
    def get(self, path: str):
        return self.request("GET", path, headers=self.anon_headers())

    def get_raw(self, path: str):
        req = urllib.request.Request(self.base + path, headers=self.anon_headers())
        with urllib.request.urlopen(req) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)

    def post(self, path: str, payload=None, extra=None):
        return self.request("POST", path, payload if payload is not None else {}, headers=self.authed(extra))

    def create_goal(self, title: str, goal_id=None, extra=None):
        payload = {"title": title}
        if goal_id:
            payload["id"] = goal_id
        return self.post("/api/goals", payload, extra=extra)


class ApiSecurityTest(unittest.TestCase):
    """Unauthenticated shell and session guards against the real default wiring."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.console = Console(Path(self.directory.name))
        self.addCleanup(self.console.stop)
        self.addCleanup(self.directory.cleanup)

    def test_root_serves_login_shell_without_leaking_state_or_scripts(self):
        # Seed a real goal directly in the store (fixture) so a leak would show.
        store_module = load("generic_store")
        store = store_module.open_store(self.console.db)
        try:
            store.create_goal("leak-check", "Sensitive queue comparison")
        finally:
            store.close()
        status, html, headers = self.console.get_raw("/")
        self.assertEqual(status, 200)
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertNotIn("Sensitive queue comparison", html)
        self.assertNotIn("leak-check", html)
        # The app script loads as a separate asset, not inline in the shell.
        self.assertNotIn("<script>", html)
        self.assertIn("/assets/app.js", html)

    def test_asset_routes_serve_app_and_stylesheet(self):
        status, js, headers = self.console.get_raw("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        status, css, headers = self.console.get_raw("/assets/app.css")
        self.assertEqual(status, 200)
        self.assertIn("css", headers.get("Content-Type", ""))
        self.assertIn("@media", css)

    def test_api_requires_session_and_returns_structured_errors(self):
        status, payload, _ = self.console.request("GET", "/api/goals")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "session_required")

    def test_bootstrap_token_is_one_use(self):
        status, _, _ = self.console.request("POST", "/api/session", {"token": "boot-token-123"})
        self.assertEqual(status, 200)
        status, payload, _ = self.console.request("POST", "/api/session", {"token": "boot-token-123"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "invalid_token")
        status, payload, _ = self.console.request("POST", "/api/session", {"token": "never-issued"})
        self.assertEqual(status, 401)

    def test_writes_need_csrf_and_matching_origin(self):
        self.console.authenticate()
        status, payload, _ = self.console.request(
            "POST", "/api/goals", {"title": "No csrf"},
            headers={"Cookie": self.console.cookie, "Origin": "http://127.0.0.1", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "csrf")
        status, payload, _ = self.console.request(
            "POST", "/api/goals", {"title": "Evil origin"},
            headers=self.console.authed({"Origin": "http://evil.example"}),
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "origin")

    def test_host_header_mismatch_is_refused_for_reads(self):
        self.console.authenticate()
        status, payload, _ = self.console.request(
            "GET", "/api/goals", headers={"Cookie": self.console.cookie, "Host": "evil.example:1"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "host")

    def test_logout_kills_the_session(self):
        self.console.authenticate()
        status, payload, _ = self.console.post("/api/logout")
        self.assertEqual(status, 200)
        status, payload, _ = self.console.request("GET", "/api/goals", headers=self.console.anon_headers())
        self.assertEqual(status, 401)

    def test_unknown_routes_and_unknown_actions_are_structured(self):
        self.console.authenticate()
        status, payload, _ = self.console.get("/api/definitely-not-here")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        status, payload, _ = self.console.post("/api/goals/goal-x/not-an-action", {})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "unknown_action")


class ApiLifecycleTest(unittest.TestCase):
    """Real generic_runtime.Service against the real store.

    The service owns its storeAdapter; these cover the consumer-visible API
    contract (CAS conflicts as structured 409s, approval hash requirement,
    honest 404s). If the storage seam is unfinished these fail on purpose.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.console = Console(Path(self.directory.name))
        self.console.authenticate()
        self.addCleanup(self.console.stop)
        self.addCleanup(self.directory.cleanup)

    def test_goal_lifecycle_through_real_runtime_service(self):
        status, created, _ = self.console.create_goal("Queue comparison", "life-1")
        self.assertEqual(status, 200)
        self.assertEqual(created["goal"]["state"], "draft")
        answer = {"value": "Compare queue disciplines", "state": "confirmed", "origin": "user_stated"}
        status, view, _ = self.console.post(
            "/api/goals/life-1/answer",
            {"question_id": "objective", "answer": answer, "row_version": 0},
        )
        self.assertEqual(status, 200)
        answered = {a["question_id"] for a in view["draft"]["answers"]}
        self.assertIn("objective", answered)
        # stale row_version is a structured 409, not a crash
        status, payload, _ = self.console.post(
            "/api/goals/life-1/answer",
            {"question_id": "objective", "answer": {"value": "Rewritten", "state": "confirmed", "origin": "user_stated"}, "row_version": 0},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "stale_row_version")
        # approval without the reviewed hash is refused before any state change
        status, payload, _ = self.console.post("/api/goals/life-1/approve", {"payload": {}, "row_version": 1})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "body_hash")
        # grant before approval refuses with a state error
        status, payload, _ = self.console.post("/api/goals/life-1/grant", {})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "invalid_state")
        # unknown goal is a clean 404
        status, payload, _ = self.console.get("/api/goals/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "goal_not_found")


class RunGateTest(unittest.TestCase):
    """Run-thread gating with an in-contract blocking Service (not the store)."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.console = Console(Path(self.directory.name), runtime_factory=RecordingRuntime)
        self.console.authenticate()
        self.addCleanup(self.console.stop)
        self.addCleanup(self.directory.cleanup)
        RecordingRuntime.REGISTRY.pop(str(self.console.db), None)

    def test_run_on_draft_goal_is_refused(self):
        status, created, _ = self.console.create_goal("Run gate goal", "run-1")
        self.assertEqual(status, 200)
        status, payload, _ = self.console.post("/api/goals/run-1/run", {})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "not_running")

    def test_second_run_while_first_active_is_refused(self):
        import time

        import threading

        RunGateTest.RUN_STARTED = threading.Event()
        RunGateTest.RUN_RELEASE = threading.Event()
        BlockingRunRuntime.RUN_STARTED = RunGateTest.RUN_STARTED
        BlockingRunRuntime.RUN_RELEASE = RunGateTest.RUN_RELEASE
        gate_dir = tempfile.TemporaryDirectory()
        try:
            gate_console = Console(Path(gate_dir.name), runtime_factory=BlockingRunRuntime)
            gate_console.authenticate()
            status, created, _ = gate_console.create_goal("Blocking run goal", "block-run")
            self.assertEqual(status, 200)
            first = gate_console.post("/api/goals/block-run/run", {})
            self.assertEqual(first[0], 202, first)
            self.assertTrue(RunGateTest.RUN_STARTED.wait(timeout=10))
            second = gate_console.post("/api/goals/block-run/run", {})
            self.assertEqual(second[0], 409, second)
            self.assertEqual(second[1]["error"]["code"], "run_in_progress")
            gate_console.stop()
        finally:
            RunGateTest.RUN_RELEASE.set()
            time.sleep(0.2)  # let the run thread drain before the dir disappears
            gate_dir.cleanup()


class ApiIdempotencyTest(unittest.TestCase):
    """Idempotency persistence with the in-contract recording Service."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.console = Console(Path(self.directory.name), runtime_factory=RecordingRuntime)
        self.console.authenticate()
        self.addCleanup(self.console.stop)
        self.addCleanup(self.directory.cleanup)
        RecordingRuntime.REGISTRY.pop(str(self.console.db), None)

    def test_create_replay_returns_recorded_response_with_replay_marker(self):
        payload = {"title": "Replayed goal"}
        first = self.console.post("/api/goals", payload, extra={"Idempotency-Key": "idem-1"})
        self.assertEqual(first[0], 200)
        second = self.console.post("/api/goals", payload, extra={"Idempotency-Key": "idem-1"})
        self.assertEqual(second[0], 200)
        self.assertEqual(second[2].get("X-Idempotent-Replay"), "1")
        self.assertEqual(second[1], first[1])
        status, listing, _ = self.console.get("/api/goals")
        titles = [g["title"] for g in listing["goals"]]
        self.assertEqual(titles.count("Replayed goal"), 1)

    def test_same_key_different_body_is_a_conflict(self):
        self.console.post("/api/goals", {"title": "Original"}, extra={"Idempotency-Key": "idem-2"})
        status, payload, _ = self.console.post("/api/goals", {"title": "Altered"}, extra={"Idempotency-Key": "idem-2"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "idempotency_mismatch")

    def test_actor_scoping_between_consoles(self):
        # The same key on a different console (own auth DB/actor) is fresh.
        self.console.post("/api/goals", {"title": "Actor one"}, extra={"Idempotency-Key": "shared-key"})
        other_directory = tempfile.TemporaryDirectory()
        try:
            other = Console(Path(other_directory.name), runtime_factory=RecordingRuntime)
            other.authenticate()
            status, payload, _ = other.post("/api/goals", {"title": "Actor one"}, extra={"Idempotency-Key": "shared-key"})
            self.assertEqual(status, 200)
            other.stop()
        finally:
            other_directory.cleanup()

    def test_action_idempotency_replays_recorded_success(self):
        status, created, _ = self.console.create_goal("Action idem goal", "idem-goal")
        self.assertEqual(status, 200)
        answer = {"value": "Publish a comparison", "state": "confirmed", "origin": "user_stated"}
        body = {"question_id": "objective", "answer": answer, "row_version": 0}
        first = self.console.post("/api/goals/idem-goal/answer", body, extra={"Idempotency-Key": "answer-1"})
        self.assertEqual(first[0], 200)
        second = self.console.post("/api/goals/idem-goal/answer", body, extra={"Idempotency-Key": "answer-1"})
        self.assertEqual(second[0], 200)
        self.assertEqual(second[2].get("X-Idempotent-Replay"), "1")
        # row_version did not advance twice
        status, view, _ = self.console.get("/api/goals/idem-goal")
        self.assertEqual(view["goal"]["row_version"], first[1]["goal"]["row_version"])


if __name__ == "__main__":
    unittest.main()
