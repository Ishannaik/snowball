"""Generic CLI subprocess lifecycle on the real Store: no mocks, no tolerant scaffolds."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "tools" / "generic_cli.py"
SNOWBALL = ROOT / "snowball.py"


def run_cli(db: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), "--db", str(db), *args],
        capture_output=True, text=True, check=check, timeout=120, cwd=str(ROOT),
    )


def run_json(db: Path, *args: str) -> dict:
    completed = run_cli(db, *args)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"non-JSON stdout for {args}: {completed.stdout!r} err={completed.stderr!r}") from exc


def _write(directory: Path, name: str, payload) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def approved_payload(goal_id: str) -> dict:
    """Complete generic contract: public objective, private answers explicit unknown."""
    return {
        "schema": "generic-goal-1", "goal_id": goal_id, "revision": 1, "parent_body_hash": None,
        "public_id": "ab" * 16,
        "questions": [
            {"id": "objective", "prompt": "Objective", "input_type": "text", "required": True, "label": []},
            {"id": "deliverable", "prompt": "Deliverable", "input_type": "text", "required": True, "label": []},
            {"id": "limits", "prompt": "Execution limits", "input_type": "text", "required": True, "label": []},
            {"id": "disclosure", "prompt": "Disclosure choices", "input_type": "text", "required": True, "label": ["private"]},
            {"id": "success", "prompt": "Success expectation", "input_type": "text", "required": True, "label": ["private"]},
        ],
        "answers": [
            {"question_id": "objective", "value": "Public objective", "state": "confirmed", "origin": "user_stated", "label": []},
            {"question_id": "deliverable", "value": "A source-backed note", "state": "confirmed", "origin": "user_stated", "label": []},
            {"question_id": "limits", "value": "bounded", "state": "confirmed", "origin": "user_stated", "label": []},
            {"question_id": "disclosure", "value": None, "state": "unknown", "origin": "user_stated", "label": ["private"]},
            {"question_id": "success", "value": None, "state": "unknown", "origin": "user_stated", "label": ["private"]},
        ],
        "rubric": {"criteria": [{"id": "supported", "description": "The requested note is source-supported.", "required": True, "expected_evidence": "kept claims", "label": [], "qualifications": []}]},
        "public_criterion_ids": ["supported"],
        "public_brief": {"text": "Public objective", "derived_from": ["objective"], "label": []},
        "recipients": {
            **{r: {"kind": "model", "attested": False, "clearance": []} for r in ("planner", "research", "review", "synthesis", "judge")},
            "fetch": {"kind": "fetch", "attested": False, "clearance": []},
            "search": {"kind": "search", "attested": False, "clearance": []},
        },
        "seeds": [{"id": "source", "value": "https://example.org/source", "label": []}],
        "ceilings": {"expansion_tasks": 4, "sources": 2, "source_bytes": 10000, "model_calls": 20, "tool_calls": 8, "model_tokens": 100000, "elapsed_sec": 120},
        "concurrency": 2, "stopping_policy": {"on_exhaustion": "partial", "stall_checkpoints": 2}, "monitoring": None,
    }


def body_hash_of(payload: dict) -> str:
    """Exact reviewed hash: computed the way the contract module hashes approvals."""
    from tools import generic_contract as contract
    return contract.build_version(payload)["body_hash"]


class GenericCliLifecycleTest(unittest.TestCase):
    """init → answer → consent → propose → approve(hash CAS) → grant → start → run/pause → revise."""

    def test_full_real_store_lifecycle_via_subprocess(self):
        from tools.generic_runtime import Service
        from tools import generic_contract as contract
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "goal.db"
            payload = approved_payload("goal-1")
            payload_path = _write(root, "payload.json", payload)
            expected_hash = body_hash_of(payload)

            created = run_json(db, "init", "--goal-id", "goal-1", "--title", "Queue comparison")
            self.assertEqual(created["goal"]["state"], "draft")

            # Answers: public objective inherits [] labels; private ones explicit unknown.
            for question_id, value in (("objective", "Compare queue semantics"), ("deliverable", "A source-backed note"),
                                       ("limits", "bounded"), ("disclosure", None), ("success", None)):
                answered = run_json(db, "answer", "--goal-id", "goal-1",
                                    "--question-id", question_id,
                                    "--value", _write(root, f"{question_id}.json", value),
                                    "--state", "confirmed" if value is not None else "unknown")
                self.assertIn("draft", answered)
            view = run_json(db, "status", "--goal-id", "goal-1")
            stored_labels = {a["question_id"]: a["label"] for a in (view.get("draft") or {}).get("answers", [])}
            self.assertEqual(stored_labels.get("objective"), [], "public answer must not silently become private")
            self.assertEqual(stored_labels.get("disclosure"), ["private"])

            consent = run_json(db, "consent", "--goal-id", "goal-1", "--recipient", "interview",
                               "--refs", _write(root, "refs.json", ["objective"]),
                               "--recipients", _write(root, "recipients.json", {"interview": {"kind": "model", "attested": True, "clearance": ["private"]}}),
                               "--calls", "3", "--tokens", "5000")
            self.assertIn("draft", consent)

            # Options-only proposal: no model env, no interview call, explicit reviewed artifacts.
            options = _write(root, "options.json", {
                "rubric": payload["rubric"],
                "brief": payload["public_brief"],
                "ceilings": payload["ceilings"],
                "concurrency": payload["concurrency"],
                "stopping_policy": payload["stopping_policy"],
                "seeds": payload["seeds"],
                "recipients": payload["recipients"],
            })
            proposal = run_json(db, "propose", "--goal-id", "goal-1", "--options", str(options))
            self.assertIn("payload", proposal)
            self.assertIn("version", proposal)

            # CAS uses the actually observed row_version, never an assumed default.
            current_version = proposal["goal"]["row_version"]
            approved = run_json(db, "approve", "--goal-id", "goal-1", "--payload", str(payload_path),
                                "--row-version", str(current_version), "--body-hash", expected_hash)
            self.assertEqual(approved["goal"]["state"], "approved")

            granted = run_json(db, "grant", "--goal-id", "goal-1")
            grant_id = granted["grant_id"]
            self.assertTrue(grant_id)

            started = run_json(db, "start", "--goal-id", "goal-1", "--grant-id", str(grant_id),
                               "--row-version", str(granted["goal"]["row_version"]))
            self.assertEqual(started["goal"]["state"], "running")

            paused = run_json(db, "pause", "--goal-id", "goal-1", "--row-version", str(started["goal"]["row_version"]))
            self.assertEqual(paused["goal"]["state"], "paused")

            revised = run_json(db, "revise", "--goal-id", "goal-1", "--row-version", str(paused["goal"]["row_version"]))
            self.assertEqual(revised["goal"]["state"], "revising")

    def test_approve_rejects_wrong_body_hash_without_state_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "goal.db"
            payload_path = _write(root, "payload.json", approved_payload("goal-1"))
            run_json(db, "init", "--goal-id", "goal-1", "--title", "Wrong hash")
            created = run_json(db, "status", "--goal-id", "goal-1")
            wrong_hash = "f" * 64
            self.assertNotEqual(body_hash_of(approved_payload("goal-1")), wrong_hash)
            result = run_cli(db, "approve", "--goal-id", "goal-1", "--payload", str(payload_path),
                             "--row-version", str(created["goal"]["row_version"]), "--body-hash", wrong_hash, check=False)
            self.assertNotEqual(result.returncode, 0)
            # Structured error, not a traceback and not silent acceptance.
            payload = json.loads(result.stderr)
            self.assertIn("error", payload)
            listed = run_json(db, "list")
            self.assertEqual(len(listed["goals"]), 1)

    def test_stale_row_version_is_a_structured_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "goal.db"
            payload_path = _write(root, "payload.json", approved_payload("goal-1"))
            run_json(db, "init", "--goal-id", "goal-1", "--title", "CAS probe")
            expected_hash = body_hash_of(approved_payload("goal-1"))
            # First approval succeeds at the observed version and advances it.
            observed = run_json(db, "status", "--goal-id", "goal-1")["goal"]["row_version"]
            first = run_json(db, "approve", "--goal-id", "goal-1", "--payload", str(payload_path),
                             "--row-version", str(observed), "--body-hash", expected_hash)
            self.assertEqual(first["goal"]["state"], "approved")
            # Re-approving at the PRIOR observed version is a genuine stale CAS conflict.
            result = run_cli(db, "approve", "--goal-id", "goal-1", "--payload", str(payload_path),
                             "--row-version", str(observed), "--body-hash", expected_hash, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stderr).get("field"), "row_version")

    def test_consent_without_any_recipient_map_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "goal.db"
            run_json(db, "init", "--goal-id", "goal-1", "--title", "No recipients")
            result = run_cli(db, "consent", "--goal-id", "goal-1", "--recipient", "interview",
                             "--refs", _write(root, "refs.json", ["objective"]),
                             "--calls", "2", "--tokens", "1000", check=False)
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stderr)
            self.assertEqual(payload.get("field"), "recipients")


class GenericCliErrorsTest(unittest.TestCase):
    def test_unknown_goal_structured_error_names_goal_id(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "goal.db"
            run_json(db, "init", "--goal-id", "present", "--title", "Anchor")
            result = run_cli(db, "status", "--goal-id", "absent", check=False)
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stderr)
            self.assertIn("error", payload)
            self.assertEqual(payload.get("field"), "goal_id")

    def test_readonly_list_never_creates_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "absent.db"
            self.assertEqual(run_json(db, "list")["goals"], [])
            self.assertFalse(db.exists())

    def test_run_waves_must_be_finite_and_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "goal.db"
            for waves in ("0", "1001"):
                with self.subTest(waves=waves):
                    result = run_cli(db, "run", "--goal-id", "any", "--max-waves", waves, check=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(json.loads(result.stderr).get("field"), "max-waves")

    def test_forwarded_commands_reach_generic_cli(self):
        for name in ("goal", "console", "run", "migrate", "serve", "status", "supervise", "retention", "ask"):
            with self.subTest(route=name):
                result = subprocess.run(
                    [sys.executable, str(SNOWBALL), name, "--db", "nonexistent-probe.db", "--help"],
                    capture_output=True, text=True, timeout=60, cwd=str(ROOT),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--db", result.stdout)
        try:
            (ROOT / "nonexistent-probe.db").unlink()
        except OSError:
            pass

    def test_generic_alias_is_gone(self):
        result = subprocess.run(
            [sys.executable, str(SNOWBALL), "generic", "--db", "x.db", "list"],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        )
        self.assertNotEqual(result.returncode, 0)


class GenericCliServeTest(unittest.TestCase):
    def test_noninteractive_serve_keeps_token_off_stdout(self):
        import contextlib
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("generic_cli_serve", CLI)
        cli = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cli
        spec.loader.exec_module(cli)
        api = cli._load("generic_api")

        holder = {}

        def capture(db_path, token, origin, port=0):
            holder["token"] = token

            class Server:
                server_address = ("127.0.0.1", 47777)

                def serve_forever(self, *a, **k):
                    raise KeyboardInterrupt()

                def server_close(self):
                    pass

            return Server()

        original = api.serve
        api.serve = capture
        try:
            with tempfile.TemporaryDirectory() as directory:
                db = Path(directory) / "g.db"
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    cli.main(["--db", str(db), "serve", "--port", "0", "--origin", "http://127.0.0.1"])
                token = holder.get("token")
                self.assertTrue(token)
                self.assertNotIn(token, out.getvalue())
                self.assertNotIn(token, err.getvalue())
                secret = ROOT / "state" / "bootstrap-token"
                self.assertTrue(secret.exists())
                self.assertIn(token, secret.read_text(encoding="utf-8"))
                try:
                    secret.unlink()
                except OSError:
                    pass
                try:
                    (ROOT / "state").rmdir()
                except OSError:
                    pass
        finally:
            api.serve = original


class GenericCliMigrateRestoreTest(unittest.TestCase):
    def test_migrate_requires_explicit_storage_root_and_backs_up(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "old.db"
            db.write_bytes(b"")
            # Explicit migration is allowed on a blank file: verified backup, then an
            # initialized empty generic schema (no goals, nothing approved or started).
            result = run_cli(db, "migrate", "--root", str(Path(directory)), check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            body = json.loads(result.stdout)
            self.assertTrue(body["changed"])
            self.assertTrue(db.stat().st_size > 0)
            # The verified bundle exists and the schema holds every generic table.
            self.assertTrue(Path(body["backup"]["bundle"]).is_dir())
            self.assertEqual(len(body["generic_tables"]), 7)
            # Fresh-process readonly verification (exercises the ro-open WAL path):
            # empty goal list, no implicit approval or start.
            listed = run_json(db, "list")
            self.assertEqual(listed["goals"], [])

    def test_migrate_requires_root(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "old.db"
            db.write_bytes(b"")
            result = subprocess.run(
                [sys.executable, str(CLI), "--db", str(db), "migrate"],
                capture_output=True, text=True, check=False, timeout=60, cwd=str(ROOT),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--root", result.stderr)

    def test_restore_reports_blocked_for_missing_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "g.db"
            result = run_cli(db, "restore", "--bundle", str(Path(directory) / "nope"),
                             "--destination", str(Path(directory) / "dest"), check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stderr).get("error"), "restore_blocked")


if __name__ == "__main__":
    unittest.main()
