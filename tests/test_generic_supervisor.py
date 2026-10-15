"""Supervisor never manufactures execution authorization."""
from __future__ import annotations
import sys
import tempfile
import threading
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.generic_supervisor import supervise
from tools.generic_runtime import Service

class SupervisorTest(unittest.TestCase):
    def test_missing_database_is_idle_and_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite"
            result = supervise(path, poll_sec=0.01, max_cycles=1)
            self.assertEqual(result["outcome"], "no_runnable_goal")
            self.assertFalse(path.exists())

    def test_draft_is_never_started_by_supervision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "goals.sqlite"
            service = Service(path)
            service.create("Compare queue formats", "draft-goal")
            result = supervise(path, poll_sec=0.01, max_cycles=1)
            self.assertEqual(result["outcome"], "no_runnable_goal")
            self.assertEqual(service.view("draft-goal")["goal"]["state"], "draft")

    def test_operator_stop_event_does_not_rewrite_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "goals.sqlite"
            service = Service(path)
            service.create("Compare archive formats", "draft-goal")
            before = service.view("draft-goal")["goal"]
            event = threading.Event()
            event.set()
            result = supervise(path, stop_event=event)
            self.assertEqual(result["outcome"], "user_stopped")
            self.assertEqual(service.view("draft-goal")["goal"], before)

if __name__ == "__main__":
    unittest.main()
