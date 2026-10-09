#!/usr/bin/env python3
"""Queue privacy: follow-ups of a private topic stay private. No network.

python -m unittest tests.test_queue_privacy -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def import_tool(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


queue = import_tool("snowball_queue_privacy", "tools/queue.py")


class PrivateFollowUps(unittest.TestCase):
    def test_suggestion_from_private_topic_stays_private(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            queue.QUEUE = root / "topics.yml"
            queue.BUDGET = root / "state" / "budget.json"
            queue.QUEUE.write_text(
                yaml.safe_dump(
                    {
                        "topics": [
                            {
                                "slug": "tmj-notes",
                                "priority": 10,
                                "status": "queued",
                                "private": True,
                                "title": "TMJ notes",
                                "focus": "forum reports only",
                            }
                        ]
                    }
                )
            )
            report = root / "report.md"
            report.write_text("- SUGGEST: tmj-follow | More forum notes\n")
            queue.done("tmj-notes", str(report))
            topics = {t["slug"]: t for t in queue.load()["topics"]}
            self.assertTrue(topics["tmj-follow"].get("private"))
            self.assertEqual(topics["tmj-follow"]["status"], "suggested")

    def test_suggestion_from_public_topic_stays_public(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            queue.QUEUE = root / "topics.yml"
            queue.BUDGET = root / "state" / "budget.json"
            queue.QUEUE.write_text(
                yaml.safe_dump(
                    {
                        "topics": [
                            {
                                "slug": "grants",
                                "priority": 10,
                                "status": "queued",
                                "title": "Grant programs",
                                "focus": "open calls",
                            }
                        ]
                    }
                )
            )
            report = root / "report.md"
            report.write_text("- SUGGEST: grant-follow | Later calls\n")
            queue.done("grants", str(report))
            topics = {t["slug"]: t for t in queue.load()["topics"]}
            self.assertFalse(topics["grant-follow"].get("private"))

    def test_private_topic_prompt_includes_anecdote_line(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            queue.QUEUE = root / "topics.yml"
            queue.QUEUE.write_text(
                yaml.safe_dump(
                    {
                        "topics": [
                            {
                                "slug": "box-notes",
                                "priority": 1,
                                "status": "queued",
                                "private": True,
                                "title": "Forum notes",
                                "focus": "what people tried",
                            }
                        ]
                    }
                )
            )
            from io import StringIO
            from contextlib import redirect_stdout

            buf = StringIO()
            with redirect_stdout(buf):
                queue.prompt("box-notes", "out.md", "20")
            text = buf.getvalue()
            self.assertIn("Anecdotal forum reports, not medical advice", text)
            self.assertNotIn("{{EXTRA}}", text)


if __name__ == "__main__":
    unittest.main()
