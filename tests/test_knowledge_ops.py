#!/usr/bin/env python3
"""Behavior tests for the wiki compiler, digest delivery, alerts, retention, and feedback."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"snowball_{name}", ROOT / "tools" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod.__name__] = mod
    spec.loader.exec_module(mod)
    return mod


wiki = _load("wiki_compiler")
digest = _load("digest")
alerts = _load("alerts")
retention = _load("retention")
feedback = _load("feedback")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


WIKI_SQL = """
CREATE TABLE entities (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  org TEXT,
  year TEXT,
  kind TEXT,
  amount REAL,
  currency TEXT,
  deadline TEXT,
  eligibility TEXT,
  link TEXT,
  evidence_quote TEXT,
  first_seen_at INTEGER,
  last_seen_at INTEGER,
  last_changed_at INTEGER,
  relevance REAL,
  urgency REAL,
  status TEXT,
  family TEXT,
  location TEXT,
  aliases TEXT,
  evidence_page_id INTEGER,
  reported_at INTEGER,
  alerted_at INTEGER,
  wiki_path TEXT
);
CREATE TABLE entity_changes (
  id INTEGER PRIMARY KEY,
  entity_key TEXT,
  field TEXT,
  old TEXT,
  new TEXT,
  seen_at INTEGER,
  page_id INTEGER,
  evidence_quote TEXT,
  reported_at INTEGER
);
CREATE TABLE claims (
  hash TEXT PRIMARY KEY,
  text TEXT,
  entity_key TEXT
);
"""

DIGEST_SQL = """
CREATE TABLE entities (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  org TEXT,
  kind TEXT,
  amount REAL,
  currency TEXT,
  deadline TEXT,
  eligibility TEXT,
  link TEXT,
  evidence_quote TEXT,
  relevance REAL NOT NULL DEFAULT 0.5,
  urgency REAL NOT NULL DEFAULT 0,
  reported_at INTEGER,
  status TEXT DEFAULT 'new',
  link_dead INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE entity_changes (
  id INTEGER PRIMARY KEY,
  entity_key TEXT,
  field TEXT,
  old TEXT,
  new TEXT,
  seen_at INTEGER,
  evidence_quote TEXT,
  reported_at INTEGER
);
"""


def _add_entity(conn: sqlite3.Connection, **kwargs) -> None:
    cols = [
        "key",
        "name",
        "org",
        "year",
        "kind",
        "amount",
        "currency",
        "deadline",
        "eligibility",
        "link",
        "evidence_quote",
        "status",
        "relevance",
        "urgency",
        "family",
    ]
    values = {
        "org": "Acme",
        "year": "2026",
        "kind": "grant",
        "amount": 1000,
        "currency": "USD",
        "deadline": "2026-12-15",
        "eligibility": "Open to India",
        "link": "https://example.com/apply",
        "evidence_quote": "Awards of up to 1000.",
        "status": "new",
        "relevance": 0.8,
        "urgency": 0.2,
    }
    values.update(kwargs)
    have = {str(row[1]) for row in conn.execute("PRAGMA table_info(entities)")}
    use = [col for col in cols if col in have and col in values]
    conn.execute(
        f"INSERT INTO entities ({', '.join(use)}) VALUES ({', '.join('?' for _ in use)})",
        tuple(values[col] for col in use),
    )


class TestWikiCompiler(unittest.TestCase):
    def setUp(self):
        wiki.TIME_BUDGET_SEC = 600
        wiki.DIRTY_LIMIT = 200
        wiki.STALE_LIMIT = 50
        self.tmp = tempfile.TemporaryDirectory(prefix="snowball-wiki-")
        self.wiki_dir = Path(self.tmp.name) / "wiki"
        self.conn = _conn()
        self.conn.executescript(WIKI_SQL)

    def tearDown(self):
        wiki.TIME_BUDGET_SEC = 600
        wiki.DIRTY_LIMIT = 200
        wiki.STALE_LIMIT = 50
        self.conn.close()
        self.tmp.cleanup()

    def _page(self, key: str) -> Path:
        row = self.conn.execute("SELECT path FROM wiki_pages WHERE ref_key = ?", (key,)).fetchone()
        self.assertIsNotNone(row)
        return self.wiki_dir / row["path"]

    def test_stable_year_filenames_and_code_sections(self):
        _add_entity(
            self.conn,
            key="alpha",
            name="Alpha Grant",
            org="Acme Org",
            year="2026",
            evidence_quote="Quote kept verbatim 77.",
        )
        self.conn.execute(
            """
            INSERT INTO entity_changes (entity_key, field, old, new, seen_at, evidence_quote)
            VALUES ('alpha', 'deadline', '2026-11-01', '2026-12-15', 1760000000, 'moved the date')
            """
        )
        self.conn.execute(
            """
            INSERT INTO claims (hash, text, entity_key)
            VALUES ('c1', 'Alpha funds 12 teams.', 'alpha')
            """
        )
        self.conn.commit()
        stats = wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        self.assertEqual(stats["compiled_entities"], 1)
        path = self.wiki_dir / "entities" / "alpha-grant-acme-org-2026.md"
        self.assertTrue(path.is_file(), path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("## Facts", text)
        self.assertIn("## Changes", text)
        self.assertIn("## Sources", text)
        self.assertIn("Acme Org", text)
        self.assertIn("Quote kept verbatim 77.", text)
        self.assertIn("Alpha funds 12 teams.", text)
        self.assertIn("https://example.com/apply", text)
        self.assertIn("deadline: 2026-11-01 → 2026-12-15", text)
        idx_path = self.wiki_dir / "index.md" if (self.wiki_dir / "index.md").is_file() else self.wiki_dir / "INDEX.md"
        self.assertIn("Alpha Grant", idx_path.read_text(encoding="utf-8"))

    def test_human_text_survives_recompile(self):
        _add_entity(self.conn, key="alpha", name="Alpha Grant", org="Acme Org", amount=1111)
        self.conn.commit()
        wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        path = self.wiki_dir / "entities" / "alpha-grant-acme-org-2026.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n## Notes\nHuman kept this paragraph about 4242.\n",
            encoding="utf-8",
        )
        self.conn.execute("UPDATE entities SET amount = 2222 WHERE key = 'alpha'")
        self.conn.commit()
        wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        text = path.read_text(encoding="utf-8")
        self.assertIn("## Notes\nHuman kept this paragraph about 4242.", text)
        self.assertIn("2222", text)
        self.assertIn("## Facts", text)
        self.assertIn("## Changes", text)
        self.assertIn("## Sources", text)
        self.assertNotIn("1111", text)

    def test_summary_floor_and_number_survival(self):
        old = "Plan 2024 budget 1500\n" * 30
        new = "updated section\n" * 30
        merged = wiki.merge_page(old, {"tags": ["grant"]}, new)
        self.assertIn("Plan 2024 budget 1500", merged)
        self.assertIn("1500", merged)
        self.assertIn("2024", merged)
        self.assertIn("updated section", merged)

        structured_old = (
            "# Alpha\n\n"
            "## Summary\n\n"
            "The cohort size is 50000 with 12 seats.\n\n"
            "## Facts\n\n"
            "| Amount | 1 |\n"
        )
        structured_new = (
            "# Alpha\n\n"
            "## Summary\n\n"
            "shrunk\n\n"
            "## Facts\n\n"
            "| Amount | 2 |\n\n"
            "## Changes\n\n"
            "_No recorded changes._\n\n"
            "## Sources\n\n"
            "https://example.com/apply\n"
        )
        merged_structured = wiki.merge_page(structured_old, {"title": "Alpha"}, structured_new)
        self.assertIn("The cohort size is 50000 with 12 seats.", merged_structured)
        self.assertIn("50000", merged_structured)
        self.assertIn("12", merged_structured)
        self.assertIn("| Amount | 2 |", merged_structured)

        huge = "Detailed background overview.\n" * 40
        short = wiki.merge_page(huge, {"tags": ["new"]}, "Truncated summary.")
        self.assertIn("Detailed background overview", short)
        self.assertIn("Truncated summary", short)

    def test_dirty_stale_and_time_budgets(self):
        for i in range(210):
            _add_entity(
                self.conn,
                key=f"e{i:03d}",
                name=f"Program {i:03d}",
                org="Org",
                evidence_quote=f"Evidence {i}.",
            )
        self.conn.commit()
        wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        total = self.conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        self.assertGreater(total, 200)
        self.conn.execute("UPDATE wiki_pages SET dirty = 1, stale = 0")
        self.conn.commit()
        stats = wiki.compile_wiki(self.conn, self.wiki_dir, full=False)
        remaining_dirty = self.conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE dirty = 1").fetchone()[0]
        self.assertEqual(stats["dirty_compiled"], 200)
        self.assertEqual(remaining_dirty, total - 200)

        self.conn.execute("UPDATE wiki_pages SET dirty = 0, stale = 1, stale_origin = 'seed'")
        self.conn.commit()
        stats = wiki.compile_wiki(self.conn, self.wiki_dir, full=False)
        remaining_stale = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE stale = 1 AND dirty = 0"
        ).fetchone()[0]
        self.assertEqual(stats["stale_compiled"], 50)
        self.assertEqual(remaining_stale, total - 50)

        self.conn.execute("UPDATE wiki_pages SET dirty = 1, stale = 0")
        self.conn.commit()
        wiki.TIME_BUDGET_SEC = 0
        try:
            stopped = wiki.compile_wiki(self.conn, self.wiki_dir, full=False)
        finally:
            wiki.TIME_BUDGET_SEC = 600
        self.assertEqual(stopped["dirty_compiled"], 0)
        self.assertEqual(stopped["stale_compiled"], 0)
        still_dirty = self.conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE dirty = 1").fetchone()[0]
        self.assertEqual(still_dirty, total)

    def test_stale_depth_is_exactly_one(self):
        for key, name in (("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")):
            _add_entity(self.conn, key=key, name=name, org="Acme", evidence_quote=f"{name} evidence.")
        self.conn.commit()
        wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        paths = {
            key: self.conn.execute("SELECT path FROM wiki_pages WHERE ref_key = ?", (key,)).fetchone()["path"]
            for key in ("a", "b", "c")
        }
        self.conn.execute(
            "INSERT INTO wiki_links (from_path, to_path) VALUES (?, ?), (?, ?)",
            (paths["b"], paths["a"], paths["c"], paths["b"]),
        )
        self.conn.execute("UPDATE wiki_pages SET dirty = 1 WHERE ref_key = 'a'")
        self.conn.commit()
        wiki.compile_wiki(self.conn, self.wiki_dir, full=False)
        b = self.conn.execute(
            "SELECT stale, stale_origin FROM wiki_pages WHERE ref_key = 'b'"
        ).fetchone()
        c = self.conn.execute("SELECT stale FROM wiki_pages WHERE ref_key = 'c'").fetchone()
        self.assertEqual(b["stale"], 1)
        self.assertEqual(b["stale_origin"], paths["a"])
        self.assertEqual(c["stale"], 0)
        wiki.compile_wiki(self.conn, self.wiki_dir, full=False)
        c_after = self.conn.execute("SELECT stale FROM wiki_pages WHERE ref_key = 'c'").fetchone()
        b_after = self.conn.execute("SELECT stale, dirty FROM wiki_pages WHERE ref_key = 'b'").fetchone()
        self.assertEqual(c_after["stale"], 0)
        self.assertEqual(b_after["stale"], 0)
        self.assertEqual(b_after["dirty"], 0)

    def test_atomic_write_before_clearing_dirty(self):
        _add_entity(self.conn, key="alpha", name="Alpha Grant", org="Acme Org", amount=1111)
        self.conn.commit()
        wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        path = self.wiki_dir / "entities" / "alpha-grant-acme-org-2026.md"
        before = path.read_text(encoding="utf-8")
        self.conn.execute("UPDATE entities SET amount = 9999 WHERE key = 'alpha'")
        self.conn.execute("UPDATE wiki_pages SET dirty = 1 WHERE ref_key = 'alpha'")
        self.conn.commit()

        def boom(src, dst):
            raise OSError("replace failed")

        with patch.object(wiki, "_atomic_replace", side_effect=boom), self.assertRaises(OSError):
            wiki.compile_wiki(self.conn, self.wiki_dir, full=False)
        dirty = self.conn.execute("SELECT dirty FROM wiki_pages WHERE ref_key = 'alpha'").fetchone()
        self.assertEqual(dirty["dirty"], 1)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertNotIn("9999", path.read_text(encoding="utf-8"))

    def test_reindex_rebuilds_cache_from_files(self):
        _add_entity(self.conn, key="alpha", name="Alpha Grant", org="Acme Org")
        _add_entity(self.conn, key="beta", name="Beta Grant", org="Acme Org", evidence_quote="Beta quote.")
        self.conn.commit()
        wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        alpha = self.wiki_dir / "entities" / "alpha-grant-acme-org-2026.md"
        original = alpha.read_text(encoding="utf-8")
        alpha.write_text(original + "\n## Notes\nUniqueHumanNoteZZZ stays.\n", encoding="utf-8")
        note = alpha.read_text(encoding="utf-8")
        self.conn.execute(
            """
            INSERT INTO wiki_pages (
                path, kind, ref_key, title, body_hash, frontmatter, compiled_at, dirty
            ) VALUES ('garbage.md', 'topic', 'nope', 'Garbage', 'x', '{}', 1, 1)
            """
        )
        self.conn.commit()
        stats = wiki.reindex_wiki(self.conn, self.wiki_dir)
        files = sorted(path for path in self.wiki_dir.rglob("*.md") if path.is_file())
        self.assertEqual(stats["indexed_pages"], len(files))
        self.assertEqual(stats["pages"], len(files))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0],
            len(files),
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0],
            len(files),
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM wiki_links").fetchone()[0],
            stats["indexed_links"],
        )
        self.assertGreater(stats["indexed_links"], 0)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM wiki_pages WHERE path = 'garbage.md'").fetchone()
        )
        self.assertEqual(alpha.read_text(encoding="utf-8"), note)
        hit = self.conn.execute(
            "SELECT path FROM wiki_fts WHERE wiki_fts MATCH 'UniqueHumanNoteZZZ'"
        ).fetchone()
        self.assertIsNotNone(hit)
        self.assertIn("alpha-grant", hit["path"])

    def test_topics_hubs_and_family_index_generation(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS topics (
              id INTEGER PRIMARY KEY,
              key TEXT NOT NULL UNIQUE,
              family TEXT NOT NULL,
              title TEXT NOT NULL,
              focus TEXT
            );
            CREATE TABLE IF NOT EXISTS families (
              name TEXT PRIMARY KEY,
              recheck_days INTEGER NOT NULL DEFAULT 7,
              weight REAL NOT NULL DEFAULT 1.0
            );
            CREATE TABLE IF NOT EXISTS pages (
              id INTEGER PRIMARY KEY,
              canon_url TEXT NOT NULL,
              domain TEXT NOT NULL,
              kind TEXT
            );
            CREATE TABLE IF NOT EXISTS hubs (
              page_id INTEGER PRIMARY KEY,
              item_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self.conn.execute("INSERT INTO families VALUES ('opportunities', 7, 1.0)")
        self.conn.execute(
            "INSERT INTO topics VALUES (1, 'ai-grants', 'opportunities', 'AI Grants', 'Grants funding open AI research')"
        )
        self.conn.execute(
            "INSERT INTO pages VALUES (10, 'https://hub.example/grants', 'hub.example', 'hub')"
        )
        self.conn.execute("INSERT INTO hubs VALUES (10, 5)")
        _add_entity(
            self.conn,
            key="grant1",
            name="Grant One",
            org="Research Org",
            family="opportunities",
            link="https://hub.example/grants/g1",
            evidence_quote="Funding for teams.",
        )
        self.conn.commit()

        stats = wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        self.assertGreaterEqual(stats["compiled_entities"], 1)
        self.assertEqual(stats["total_topics"], 1)
        self.assertEqual(stats["total_hubs"], 1)

        topic_file = self.wiki_dir / "topics" / "ai-grants.md"
        self.assertTrue(topic_file.is_file(), topic_file)
        topic_content = topic_file.read_text(encoding="utf-8")
        self.assertIn("# AI Grants", topic_content)
        self.assertIn("## Overview", topic_content)
        self.assertIn("Grants funding open AI research", topic_content)
        self.assertIn("## Entities", topic_content)
        self.assertIn("Grant One", topic_content)

        hub_file = self.wiki_dir / "hubs" / "hubexample.md"
        self.assertTrue(hub_file.is_file(), hub_file)
        hub_content = hub_file.read_text(encoding="utf-8")
        self.assertIn("# Hub: hub.example", hub_content)
        self.assertIn("## Overview", hub_content)
        self.assertIn("## Discovered Entities", hub_content)
        self.assertIn("Grant One", hub_content)

        index_file = self.wiki_dir / "index.md"
        if not index_file.is_file():
            index_file = self.wiki_dir / "INDEX.md"
        self.assertTrue(index_file.is_file(), index_file)
        index_content = index_file.read_text(encoding="utf-8")
        self.assertIn("## Families & Topics", index_content)
        self.assertIn("Family: Opportunities", index_content)
        self.assertIn("[AI Grants](topics/ai-grants.md)", index_content)
        self.assertIn("## Research Hubs", index_content)
        self.assertIn("hub.example", index_content)
        self.assertIn("Grant One", index_content)

        family_file = self.wiki_dir / "families" / "opportunities.md"
        self.assertTrue(family_file.is_file(), family_file)
        family_content = family_file.read_text(encoding="utf-8")
        self.assertIn("# Family: Opportunities", family_content)
        self.assertIn("## Overview", family_content)
        self.assertIn("## Topics", family_content)
        self.assertIn("AI Grants", family_content)
        self.assertIn("Grant One", family_content)

        # Human-owned notes in family page survive recompile
        family_file.write_text(family_content + "\n## Notes\nHumanFamilyAnalysisPreservedZZZ\n", encoding="utf-8")
        wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        recompiled_family = family_file.read_text(encoding="utf-8")
        self.assertIn("HumanFamilyAnalysisPreservedZZZ", recompiled_family)
        self.assertIn("## Overview", recompiled_family)

        # Human-owned notes in topic page survive recompile
        topic_file.write_text(topic_content + "\n## Notes\nHumanTopicAnalysisPreservedZZZ\n", encoding="utf-8")
        wiki.compile_wiki(self.conn, self.wiki_dir, full=True)
        recompiled_topic = topic_file.read_text(encoding="utf-8")
        self.assertIn("HumanTopicAnalysisPreservedZZZ", recompiled_topic)
        self.assertIn("## Overview", recompiled_topic)

    def test_merge_summary_local_guard_and_survival(self):
        old_summary = "Cohort launched in 2024 with 50000 prize budget across 12 teams."
        update = "Applications for 2026 cohort are now open."

        # Candidate that drops number 50000 is rejected
        lossy_model = lambda old, new: "New cohort opened in 2026 with 12 teams."
        cand = wiki.merge_summary_local(old_summary, update, model_callable=lossy_model)
        self.assertIn("50000", cand)
        self.assertIn("2024", cand)

        # Candidate that shrinks below 70% is rejected
        shrunk_model = lambda old, new: "Short 2024 50000 12."
        cand_shrunk = wiki.merge_summary_local(old_summary, update, model_callable=shrunk_model)
        self.assertIn("Cohort launched in 2024", cand_shrunk)

        # Candidate meeting 70% length floor and number survival is accepted
        valid_model = lambda old, new: (
            "Cohort launched in 2024 with 50000 prize budget across 12 teams. "
            "Applications for 2026 cohort are now actively accepting submissions."
        )
        cand_valid = wiki.merge_summary_local(old_summary, update, model_callable=valid_model)
        self.assertIn("actively accepting submissions", cand_valid)
        self.assertIn("50000", cand_valid)

        # Exception in model gracefully falls back to deterministic preservation
        def failing_model(old, new):
            raise RuntimeError("LLM tunnel timeout")

        cand_fail = wiki.merge_summary_local(old_summary, update, model_callable=failing_model)
        self.assertIn("Cohort launched in 2024 with 50000", cand_fail)
        self.assertIn("Applications for 2026", cand_fail)


class _DiscordResponse:
    status = 200

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="snowball-digest-")
        self.conn = _conn()
        self.conn.executescript(DIGEST_SQL)
        self._env = {
            key: os.environ.get(key) for key in ("DISCORD_BOT_TOKEN", "DISCORD_WEBHOOK_URL")
        }
        os.environ["DISCORD_BOT_TOKEN"] = "test-token"
        os.environ["DISCORD_WEBHOOK_URL"] = ""
        self._digest_dir = digest.DIGEST_DIR
        digest.DIGEST_DIR = Path(self.tmp.name) / "digests"

    def tearDown(self):
        digest.DIGEST_DIR = self._digest_dir
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.conn.close()
        self.tmp.cleanup()

    def test_prepare_chunks_and_ack_is_only_reported_writer(self):
        quote_a = "A" * 900
        quote_b = "B" * 900
        _add_entity(
            self.conn,
            key="low",
            name="Low Grant",
            relevance=0.1,
            urgency=0,
            evidence_quote="low quote enough",
        )
        _add_entity(
            self.conn,
            key="high",
            name="High Grant",
            relevance=0.4,
            urgency=0,
            evidence_quote=quote_a,
        )
        self.conn.execute(
            """
            INSERT INTO entities (
                key, name, org, kind, amount, currency, deadline, eligibility, link,
                evidence_quote, relevance, urgency, reported_at
            ) VALUES (
                'changed', 'Changed Grant', 'Acme', 'grant', 5, 'USD', '2026-12-01',
                'Open', 'https://example.com/c', 'entity already sent', 1.0, 0, 50
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO entity_changes (entity_key, field, old, new, seen_at, evidence_quote, reported_at)
            VALUES ('changed', 'deadline', '2026-11-01', '2026-12-01', 1, ?, NULL)
            """,
            (quote_b,),
        )
        self.conn.execute(
            """
            INSERT INTO entities (
                key, name, org, kind, relevance, urgency, evidence_quote
            ) VALUES ('empty', 'Empty Quote', 'Acme', 'grant', 1, 1, '   ')
            """
        )
        self.conn.commit()
        prepared = digest.prepare_digest(self.conn, "2026-10-08")
        self.assertTrue(prepared)
        self.assertTrue(all(len(part.text) <= 2000 for part in prepared))
        joined_refs = [ref for part in prepared for ref in part.item_refs]
        self.assertEqual(joined_refs[0], ("change", "1"))
        self.assertNotIn(("entity", "empty"), joined_refs)
        self.assertTrue(any(quote_a in part.text for part in prepared))
        self.assertTrue(any(quote_b in part.text for part in prepared))
        self.assertEqual(
            sum(1 for part in prepared if quote_a in part.text),
            1,
        )
        self.assertIsNone(
            self.conn.execute("SELECT reported_at FROM entities WHERE key = 'high'").fetchone()["reported_at"]
        )
        self.assertTrue((digest.DIGEST_DIR / "2026-10-08.md").is_file())
        again = digest.prepare_digest(self.conn, "2026-10-08")
        self.assertEqual(again[0].delivery_key, prepared[0].delivery_key)
        self.assertEqual(len(prepared[0].delivery_key), 25)
        other = digest._delivery_key("2026-10-08", 1, prepared[0].item_refs)
        self.assertNotEqual(other[:25], prepared[0].delivery_key)

        with self.assertRaises(digest.DeliveryFailure):
            digest.send_digest(prepared[0], "")
        self.assertIsNone(
            self.conn.execute("SELECT reported_at FROM entities WHERE key = 'high'").fetchone()["reported_at"]
        )

        def fail_post(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 500, "nope", hdrs=None, fp=io.BytesIO(b""))

        with (
            patch.object(urllib.request, "urlopen", side_effect=fail_post),
            self.assertRaises(digest.DeliveryFailure),
        ):
            digest.send_digest(prepared[0], "channel-1")
        self.assertIsNone(
            self.conn.execute("SELECT reported_at FROM entity_changes WHERE id = 1").fetchone()["reported_at"]
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM digest_items").fetchone()[0], 0)

        def ok_post(request, timeout=0):
            return _DiscordResponse(b'{"id":"msg-42"}')

        first_ack = None
        with patch.object(urllib.request, "urlopen", side_effect=ok_post):
            for part in prepared:
                ack = digest.send_digest(part, "channel-1")
                self.assertIsInstance(ack, digest.DeliveryAck)
                self.assertEqual(ack.message_id, "msg-42")
                self.assertEqual(ack.delivery_key, part.delivery_key)
                if first_ack is None:
                    first_ack = ack
                digest.acknowledge_digest(self.conn, part, ack)
        self.assertEqual(
            self.conn.execute("SELECT message_id FROM digest_items WHERE ref_id = 'high'").fetchone()["message_id"],
            "msg-42",
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT reported_at FROM entities WHERE key = 'high'").fetchone()["reported_at"]
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT reported_at FROM entity_changes WHERE id = 1").fetchone()["reported_at"]
        )
        digest.acknowledge_digest(self.conn, prepared[0], first_ack)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM digest_items WHERE ref_kind = ? AND ref_id = ?",
                prepared[0].item_refs[0],
            ).fetchone()[0],
            1,
        )
        third = digest.prepare_digest(self.conn, "2026-10-08")
        third_refs = {ref for part in third for ref in part.item_refs}
        self.assertNotIn(("entity", "high"), third_refs)
        self.assertNotIn(("change", "1"), third_refs)

    def test_rank_cap_excludes_empty_evidence(self):
        for i in range(30):
            _add_entity(
                self.conn,
                key=f"k{i:02d}",
                name=f"Name {i:02d}",
                relevance=(i + 1) / 100,
                urgency=0,
                evidence_quote=f"quote {i}",
            )
        _add_entity(
            self.conn,
            key="blank",
            name="Blank",
            relevance=1,
            urgency=3,
            evidence_quote="",
        )
        self.conn.commit()
        prepared = digest.prepare_digest(self.conn, "2026-10-08")
        refs = [ref for part in prepared for ref in part.item_refs]
        self.assertEqual(len(refs), 25)
        self.assertIn(("entity", "k29"), refs)
        self.assertNotIn(("entity", "k00"), refs)
        self.assertNotIn(("entity", "blank"), refs)
        self.assertIsNone(
            self.conn.execute("SELECT reported_at FROM entities WHERE key = 'k29'").fetchone()["reported_at"]
        )

    def test_safe_item_bound_splitting_never_slices_items(self):
        quote = "Q" * 450
        for i in range(4):
            _add_entity(
                self.conn,
                key=f"item_{i}",
                name=f"Bounded Item {i}",
                evidence_quote=quote,
                relevance=0.8 - i * 0.1,
            )
        self.conn.commit()
        prepared = digest.prepare_digest(self.conn, "2026-10-08")
        self.assertEqual(len(prepared), 2)
        self.assertEqual(len(prepared[0].item_refs), 3)
        self.assertEqual(len(prepared[1].item_refs), 1)
        for part in prepared:
            self.assertLessEqual(len(part.text), digest.DISCORD_LIMIT)
            self.assertIn("### Bounded Item", part.text)
        self.assertIn("Bounded Item 0", prepared[0].text)
        self.assertIn("Bounded Item 1", prepared[0].text)
        self.assertIn("Bounded Item 2", prepared[0].text)
        self.assertIn("Bounded Item 3", prepared[1].text)
        self.assertNotIn("Bounded Item 3", prepared[0].text)

    def test_dead_link_probed_and_hub_fallback_used(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
              id INTEGER PRIMARY KEY,
              canon_url TEXT NOT NULL,
              domain TEXT NOT NULL,
              kind TEXT
            );
            CREATE TABLE IF NOT EXISTS entity_sources (
              entity_key TEXT NOT NULL,
              page_id INTEGER NOT NULL,
              seen_at INTEGER NOT NULL
            );
            """
        )
        self.conn.execute(
            "INSERT INTO pages (id, canon_url, domain, kind) VALUES (1, 'https://hub.example/grants', 'hub.example', 'hub')"
        )
        _add_entity(
            self.conn,
            key="dead_e",
            name="Dead Link Entity",
            link="https://broken.example/dead",
            evidence_quote="Valid verified quote.",
            relevance=0.9,
        )
        self.conn.execute("INSERT INTO entity_sources VALUES ('dead_e', 1, 100)")
        self.conn.execute("UPDATE entities SET link_dead = 1 WHERE key = 'dead_e'")
        self.conn.commit()

        def mock_probe(url):
            return "hub.example" in url

        with patch.object(digest, "_probe_link", side_effect=mock_probe):
            prepared = digest.prepare_digest(self.conn, "2026-10-08")
        self.assertEqual(len(prepared), 1)
        self.assertIn("[Hub: https://hub.example/grants](https://hub.example/grants) *(source link dead)*", prepared[0].text)

    def test_discord_reconciliation_and_safe_token(self):
        self.assertIsNone(digest.reconcile_delivery(self.conn, "nonexistent"))
        self.conn.execute(
            """
            INSERT INTO digest_outbox (delivery_key, day, chunk_index, item_refs, text, state, message_id, sent_at)
            VALUES ('deliv-1', '2026-10-08', 0, '[]', 'test', 'acked', 'msg-999', 1760000000)
            """
        )
        self.conn.commit()
        ack = digest.reconcile_delivery(self.conn, "deliv-1", channel_id="ch-test")
        self.assertIsNotNone(ack)
        self.assertEqual(ack.message_id, "msg-999")
        self.assertEqual(ack.delivery_key, "deliv-1")

        token_file = Path(self.tmp.name) / "discord.token"
        token_file.write_text("DISCORD_BOT_TOKEN=token-from-file-xyz\n", encoding="utf-8")
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "", "DISCORD_TOKEN_FILE": str(token_file)}):
            loaded = digest.get_discord_bot_token()
            self.assertEqual(loaded, "token-from-file-xyz")

    def test_acknowledge_replay_does_not_raise(self):
        _add_entity(self.conn, key="once", name="Once Grant", evidence_quote="verified once quote")
        prepared = digest.prepare_digest(self.conn, "2026-10-08")
        self.assertEqual(len(prepared), 1)
        ack = digest.DeliveryAck(
            delivery_key=prepared[0].delivery_key,
            channel_id="ch",
            message_id="msg-replay",
            sent_at=1760000001,
        )
        digest.acknowledge_digest(self.conn, prepared[0], ack)
        digest.acknowledge_digest(self.conn, prepared[0], ack)
        n = self.conn.execute("SELECT COUNT(*) FROM digest_items").fetchone()[0]
        self.assertEqual(n, 1)
        state = self.conn.execute(
            "SELECT state FROM digest_outbox WHERE delivery_key = ?",
            (prepared[0].delivery_key,),
        ).fetchone()[0]
        self.assertEqual(state, "acked")

    def test_reconcile_found_message_does_not_ack_outbox(self):
        digest.ensure_digest_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO digest_outbox (delivery_key, day, chunk_index, item_refs, text, state)
            VALUES ('deliv-unknown', '2026-10-08', 0, '[]', 'hello', 'unknown')
            """
        )
        self.conn.commit()

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self):
                return b'[{"id":"msg-found","nonce":"deliv-unknown"}]'

        with patch.object(digest.urllib.request, "urlopen", return_value=Resp()):
            ack = digest.reconcile_delivery(self.conn, "deliv-unknown", channel_id="123")
        self.assertIsNotNone(ack)
        self.assertEqual(ack.message_id, "msg-found")
        state = self.conn.execute(
            "SELECT state FROM digest_outbox WHERE delivery_key = 'deliv-unknown'"
        ).fetchone()[0]
        self.assertEqual(state, "unknown")
        items = self.conn.execute("SELECT COUNT(*) FROM digest_items").fetchone()[0]
        self.assertEqual(items, 0)

    def test_weekly_learning_summary_and_separate_acknowledgement(self):
        _add_entity(
            self.conn,
            key="wk_ent",
            name="Weekly Opportunity",
            evidence_quote="Weekly verified evidence.",
            relevance=0.85,
        )
        self.conn.execute(
            """
            INSERT INTO entity_changes (entity_key, field, old, new, seen_at, evidence_quote)
            VALUES ('wk_ent', 'amount', '500', '1500', 1760000000, 'amount increased')
            """
        )
        self.conn.commit()
        prepared = digest.prepare_weekly_digest(self.conn, "2026-10-08")
        self.assertGreaterEqual(len(prepared), 1)
        self.assertIn("# ❄️ Snowball Weekly Research Briefing — 2026-10-08", prepared[0].text)
        self.assertIn("Weekly Opportunity", prepared[0].text)

        ack = digest.DeliveryAck(
            delivery_key=prepared[0].delivery_key,
            channel_id="wk-chan",
            message_id="wk-msg-42",
            sent_at=1760000000,
        )
        digest.acknowledge_digest(self.conn, prepared[0], ack)

        row = self.conn.execute(
            "SELECT message_id FROM weekly_items WHERE ref_id = 'wk_ent'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["message_id"], "wk-msg-42")
        self.assertIsNone(
            self.conn.execute("SELECT reported_at FROM entities WHERE key = 'wk_ent'").fetchone()["reported_at"]
        )


class TestAlerts(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        self.conn.executescript(
            """
            CREATE TABLE domains (domain TEXT PRIMARY KEY, feedback_score REAL NOT NULL DEFAULT 0);
            CREATE TABLE pages (
              id INTEGER PRIMARY KEY,
              canon_url TEXT NOT NULL,
              domain TEXT,
              kind TEXT
            );
            CREATE TABLE entities (
              key TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              org TEXT,
              kind TEXT,
              deadline TEXT,
              relevance REAL,
              link TEXT,
              evidence_quote TEXT,
              evidence_page_id INTEGER,
              alerted_at INTEGER,
              status TEXT DEFAULT 'new',
              eligibility TEXT
            );
            CREATE TABLE entity_sources (
              entity_key TEXT,
              page_id INTEGER,
              seen_at INTEGER
            );
            """
        )
        self.conn.execute(
            "INSERT INTO pages (id, canon_url, domain, kind) VALUES (1, 'https://hub.example/grants', 'hub.example', 'hub')"
        )
        self._env = {
            key: os.environ.get(key) for key in ("DISCORD_BOT_TOKEN", "DISCORD_WEBHOOK_URL", "DIGEST_CHANNEL_ID")
        }
        os.environ["DISCORD_BOT_TOKEN"] = "test-token"
        os.environ["DISCORD_WEBHOOK_URL"] = ""
        os.environ["DIGEST_CHANNEL_ID"] = ""

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.conn.close()

    def _entity(self, key: str, deadline: str, relevance: float, alerted_at=None, link="https://item.example/a"):
        self.conn.execute(
            """
            INSERT INTO entities (
                key, name, org, kind, deadline, relevance, link, evidence_quote,
                evidence_page_id, alerted_at, eligibility
            ) VALUES (?, ?, 'Org', 'grant', ?, ?, ?, 'Award text.', 1, ?, 'Open')
            """,
            (key, key, deadline, relevance, link, alerted_at),
        )
        self.conn.execute(
            "INSERT INTO entity_sources (entity_key, page_id, seen_at) VALUES (?, 1, 1)",
            (key,),
        )

    def test_deadline_and_relevance_window(self):
        self._entity("in7", "2026-10-08", 0.5)
        self._entity("today", "2026-10-01", 0.9)
        self._entity("late", "2026-10-09", 0.9)
        self._entity("past", "2026-09-30", 0.9)
        self._entity("weak", "2026-10-03", 0.49)
        self._entity("sent", "2026-10-02", 0.9, alerted_at=10)
        self.conn.commit()
        found = {row["key"] for row in alerts.prepare_urgent_alerts(self.conn, as_of_date="2026-10-01")}
        self.assertEqual(found, {"in7", "today"})

    def test_confirmed_delivery_is_once_only_and_dead_link_uses_hub(self):
        self._entity("soon", "2026-10-05", 0.8, link="https://item.example/dead")
        self.conn.commit()
        posted: list[bytes] = []

        def ok_post(request, timeout=0):
            posted.append(request.data)
            return _DiscordResponse(b'{"id":"alert-7"}')

        with (
            patch.object(alerts, "_probe_link", return_value=True),
            patch.object(urllib.request, "urlopen", side_effect=ok_post),
        ):
            first = alerts.send_urgent_alerts(self.conn, channel_id="chan", as_of_date="2026-10-01")
        self.assertEqual(first["sent"], 1)
        self.assertIsNotNone(
            self.conn.execute("SELECT alerted_at FROM entities WHERE key = 'soon'").fetchone()["alerted_at"]
        )
        with (
            patch.object(alerts, "_probe_link", return_value=True),
            patch.object(urllib.request, "urlopen", side_effect=ok_post),
        ):
            second = alerts.send_urgent_alerts(self.conn, channel_id="chan", as_of_date="2026-10-01")
        self.assertEqual(second["sent"], 0)
        self.assertEqual(second["prepared"], 0)

        self.conn.execute("UPDATE entities SET alerted_at = NULL WHERE key = 'soon'")
        self.conn.commit()
        posted.clear()
        with (
            patch.object(alerts, "_probe_link", return_value=False),
            patch.object(urllib.request, "urlopen", side_effect=ok_post),
        ):
            dead = alerts.send_urgent_alerts(self.conn, channel_id="chan", as_of_date="2026-10-01")
        self.assertEqual(dead["sent"], 1)
        self.assertEqual(dead["link_dead"], 1)
        self.assertEqual(
            self.conn.execute("SELECT link_dead FROM entities WHERE key = 'soon'").fetchone()["link_dead"],
            1,
        )
        body = posted[0].decode("utf-8")
        self.assertIn("https://hub.example/grants", body)

    def test_alerts_cli_post_flag_and_json(self):
        self._entity("urgent1", "2026-10-05", 0.8)
        self.conn.commit()
        res = alerts.send_urgent_alerts(self.conn, channel_id="", as_of_date="2026-10-01")
        self.assertEqual(res["prepared"], 1)
        self.assertEqual(res["sent"], 0)
        self.assertIsNone(
            self.conn.execute("SELECT alerted_at FROM entities WHERE key = 'urgent1'").fetchone()["alerted_at"]
        )


class TestRetention(unittest.TestCase):
    def test_prune_backup_expiry_and_quick_check(self):
        tmp = tempfile.TemporaryDirectory(prefix="snowball-retain-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        now = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
        (root / "raw" / "2026-09-23").mkdir(parents=True)
        (root / "raw" / "2026-09-23" / "old.txt").write_text("old", encoding="utf-8")
        (root / "raw" / "2026-09-24").mkdir(parents=True)
        (root / "raw" / "2026-09-24" / "edge.txt").write_text("edge", encoding="utf-8")
        (root / "raw" / "2026-10-01").mkdir(parents=True)
        (root / "raw" / "2026-10-01" / "new.txt").write_text("new", encoding="utf-8")
        (root / "quarantine").mkdir()
        (root / "logs").mkdir()
        (root / "wiki").mkdir()
        (root / "digests").mkdir()
        (root / "data").mkdir()
        old_file = root / "quarantine" / "old.bin"
        new_file = root / "quarantine" / "new.bin"
        old_log = root / "logs" / "old.log"
        new_log = root / "logs" / "new.log"
        for path in (old_file, new_file, old_log, new_log):
            path.write_text("x", encoding="utf-8")
        old_ts = (now - dt.timedelta(days=31)).timestamp()
        edge_ts = (now - dt.timedelta(days=30)).timestamp()
        os.utime(old_file, (old_ts, old_ts))
        os.utime(old_log, (old_ts, old_ts))
        os.utime(new_file, (edge_ts, edge_ts))
        os.utime(new_log, (edge_ts, edge_ts))
        wiki_page = root / "wiki" / "keep.md"
        digest_page = root / "digests" / "2026-01-01.md"
        wiki_page.write_text("keep wiki", encoding="utf-8")
        digest_page.write_text("keep digest", encoding="utf-8")
        os.utime(wiki_page, (old_ts, old_ts))
        os.utime(digest_page, (old_ts, old_ts))
        db_path = root / "data" / "snowball.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE entities (key TEXT PRIMARY KEY, deadline TEXT, status TEXT)")
        conn.execute("INSERT INTO entities VALUES ('old', '2020-01-01', 'new')")
        conn.execute("INSERT INTO entities VALUES ('live', '2026-12-01', 'new')")
        conn.execute("INSERT INTO entities VALUES ('done', '2019-01-01', 'expired')")
        conn.commit()
        backup_dir = root / "data" / "backup"
        backup_dir.mkdir()
        for day in range(1, 9):
            (backup_dir / f"snowball-2026-01-{day:02d}.db").write_bytes(b"old-backup")
        stats = retention.run_retention(conn, root_dir=root, now_dt=now)
        self.assertFalse((root / "raw" / "2026-09-23").exists())
        self.assertTrue((root / "raw" / "2026-09-24" / "edge.txt").is_file())
        self.assertTrue((root / "raw" / "2026-10-01" / "new.txt").is_file())
        self.assertFalse(old_file.exists())
        self.assertFalse(old_log.exists())
        self.assertTrue(new_file.is_file())
        self.assertTrue(new_log.is_file())
        self.assertEqual(wiki_page.read_text(encoding="utf-8"), "keep wiki")
        self.assertEqual(digest_page.read_text(encoding="utf-8"), "keep digest")
        self.assertTrue(db_path.is_file())
        backups = sorted(path.name for path in backup_dir.glob("snowball-*.db"))
        self.assertEqual(len(backups), 7)
        self.assertIn("snowball-2026-10-08.db", backups)
        self.assertNotIn("snowball-2026-01-01.db", backups)
        self.assertEqual(stats["quick_check"], "ok")
        self.assertEqual(stats["backups"], 7)
        self.assertEqual(
            conn.execute("SELECT status FROM entities WHERE key = 'old'").fetchone()[0],
            "expired",
        )
        self.assertEqual(
            conn.execute("SELECT status FROM entities WHERE key = 'live'").fetchone()[0],
            "new",
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 3)
        conn.close()

    def test_restore_backup_and_eight_copies(self):
        tmp = tempfile.TemporaryDirectory(prefix="snowball-backup-test-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        backup_dir = root / "backups"
        backup_dir.mkdir(parents=True)
        for i in range(1, 9):
            (backup_dir / f"snowball-2026-01-{i:02d}.db").write_text("backup-data", encoding="utf-8")
        kept, pruned = retention._trim_backups(backup_dir, keep=7)
        self.assertEqual(kept, 7)
        self.assertEqual(pruned, 1)
        remaining = sorted(p.name for p in backup_dir.glob("snowball-*.db"))
        self.assertEqual(len(remaining), 7)
        self.assertNotIn("snowball-2026-01-01.db", remaining)
        self.assertIn("snowball-2026-01-08.db", remaining)

        db_path = root / "source.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE entities (key TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO entities VALUES ('e1', 'Entity 1')")
        conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, canon_url TEXT)")
        conn.execute("INSERT INTO pages VALUES (1, 'https://example.com')")
        conn.commit()
        conn.close()

        restored_db = root / "restored" / "snowball.db"
        stats = retention.restore_backup(db_path, restored_db)
        self.assertTrue(restored_db.is_file())
        self.assertEqual(stats["quick_check"], "ok")
        self.assertEqual(stats["entities"], 1)
        self.assertEqual(stats["pages"], 1)
        with self.assertRaises(ValueError):
            retention.restore_backup(db_path, db_path)


class TestFeedback(unittest.TestCase):
    def test_downvote_is_idempotent_and_does_not_touch_entities(self):
        conn = _conn()
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE domains (
              domain TEXT PRIMARY KEY,
              feedback_score REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE pages (
              id INTEGER PRIMARY KEY,
              canon_url TEXT,
              domain TEXT
            );
            CREATE TABLE entities (
              key TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              eligibility TEXT,
              link TEXT,
              evidence_quote TEXT,
              evidence_page_id INTEGER,
              status TEXT
            );
            CREATE TABLE claims (hash TEXT PRIMARY KEY, text TEXT, entity_key TEXT);
            CREATE TABLE digest_items (
              id INTEGER PRIMARY KEY,
              day TEXT NOT NULL,
              ref_kind TEXT NOT NULL,
              ref_id TEXT NOT NULL,
              rank INTEGER NOT NULL,
              reason TEXT NOT NULL,
              sent_at INTEGER,
              message_id TEXT
            );
            """
        )
        conn.execute("INSERT INTO domains VALUES ('grants.example', 2.0)")
        conn.execute("INSERT INTO pages VALUES (1, 'https://grants.example/a', 'grants.example')")
        conn.execute(
            """
            INSERT INTO entities VALUES (
                'alpha', 'Alpha Grant', 'Open to India', 'https://grants.example/a',
                'Award of 5000', 1, 'new'
            )
            """
        )
        conn.execute("INSERT INTO claims VALUES ('h', 'Award of 5000', 'alpha')")
        conn.execute(
            """
            INSERT INTO digest_items (day, ref_kind, ref_id, rank, reason, sent_at, message_id)
            VALUES ('2026-10-08', 'entity', 'alpha', 1, 'new: grant', 1, 'm')
            """
        )
        conn.commit()
        before = conn.execute(
            "SELECT key, name, eligibility, evidence_quote, status FROM entities"
        ).fetchall()
        claims = conn.execute("SELECT hash, text FROM claims").fetchall()
        self.assertTrue(feedback.apply_feedback(conn, 1, "👎"))
        self.assertEqual(
            conn.execute("SELECT feedback_score FROM domains WHERE domain = 'grants.example'").fetchone()[0],
            1.0,
        )
        self.assertTrue(feedback.apply_feedback(conn, 1, "down"))
        self.assertEqual(
            conn.execute("SELECT feedback_score FROM domains WHERE domain = 'grants.example'").fetchone()[0],
            1.0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM feedback WHERE digest_item_id = 1 AND reaction = 'down'").fetchone()[0],
            1,
        )
        self.assertTrue(feedback.apply_feedback(conn, 1, "👍"))
        self.assertEqual(
            conn.execute("SELECT feedback_score FROM domains WHERE domain = 'grants.example'").fetchone()[0],
            3.0,
        )
        after = conn.execute(
            "SELECT key, name, eligibility, evidence_quote, status FROM entities"
        ).fetchall()
        self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before])
        self.assertEqual(
            [tuple(row) for row in conn.execute("SELECT hash, text FROM claims")],
            [tuple(row) for row in claims],
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)
        self.assertFalse(feedback.apply_feedback(conn, 99, "down"))
        self.assertFalse(feedback.apply_feedback(conn, 1, "shrug"))

    def test_reaction_reversal_removal_and_deduplication(self):
        conn = _conn()
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE domains (domain TEXT PRIMARY KEY, feedback_score REAL NOT NULL DEFAULT 0);
            CREATE TABLE pages (id INTEGER PRIMARY KEY, canon_url TEXT, domain TEXT);
            CREATE TABLE entities (key TEXT PRIMARY KEY, name TEXT, link TEXT);
            CREATE TABLE digest_items (
              id INTEGER PRIMARY KEY, day TEXT NOT NULL, ref_kind TEXT NOT NULL,
              ref_id TEXT NOT NULL, rank INTEGER NOT NULL, reason TEXT NOT NULL,
              sent_at INTEGER, message_id TEXT
            );
            """
        )
        conn.execute("INSERT INTO domains VALUES ('example.com', 0.0)")
        conn.execute("INSERT INTO pages VALUES (1, 'https://example.com/apply', 'example.com')")
        conn.execute("INSERT INTO entities VALUES ('alpha', 'Alpha', 'https://example.com/apply')")
        conn.execute("INSERT INTO digest_items VALUES (1, '2026-10-08', 'entity', 'alpha', 1, 'reason', 1, 'msg-123')")
        conn.commit()

        self.assertTrue(feedback.apply_feedback(conn, 1, "up"))
        score = conn.execute("SELECT feedback_score FROM domains WHERE domain = 'example.com'").fetchone()[0]
        self.assertEqual(score, 1.0)

        self.assertTrue(feedback.apply_feedback(conn, 1, "down", action="reverse"))
        score = conn.execute("SELECT feedback_score FROM domains WHERE domain = 'example.com'").fetchone()[0]
        self.assertEqual(score, -1.0)

        self.assertTrue(feedback.apply_feedback(conn, 1, "down", action="remove"))
        score = conn.execute("SELECT feedback_score FROM domains WHERE domain = 'example.com'").fetchone()[0]
        self.assertEqual(score, 0.0)

        ev = {"event_id": "evt-unique-1", "message_id": "msg-123", "reaction": "up"}
        res1 = feedback.apply_reaction_event(conn, ev)
        self.assertTrue(res1["ok"])
        score1 = conn.execute("SELECT feedback_score FROM domains WHERE domain = 'example.com'").fetchone()[0]
        self.assertEqual(score1, 1.0)

        res2 = feedback.apply_reaction_event(conn, ev)
        self.assertTrue(res2["ok"])
        score2 = conn.execute("SELECT feedback_score FROM domains WHERE domain = 'example.com'").fetchone()[0]
        self.assertEqual(score2, 1.0)

        # Ambiguous message with multiple items requires exact item_id
        conn.execute("INSERT INTO digest_items VALUES (2, '2026-10-08', 'entity', 'beta', 2, 'reason2', 1, 'msg-123')")
        conn.commit()
        ambig_ev = {"event_id": "evt-ambig-1", "message_id": "msg-123", "reaction": "up"}
        res_ambig = feedback.apply_reaction_event(conn, ambig_ev)
        self.assertFalse(res_ambig["ok"])
        self.assertEqual(res_ambig["status"], "ambiguous")

        # Providing exact item_id succeeds
        exact_ev = {"event_id": "evt-exact-1", "message_id": "msg-123", "item_id": 2, "reaction": "up"}
        res_exact = feedback.apply_reaction_event(conn, exact_ev)
        self.assertTrue(res_exact["ok"])

    def test_add_opposite_reaction_switches_score(self):
        conn = _conn()
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE domains (domain TEXT PRIMARY KEY, feedback_score REAL NOT NULL DEFAULT 0);
            CREATE TABLE entities (
              key TEXT PRIMARY KEY, name TEXT, link TEXT, evidence_page_id INTEGER
            );
            CREATE TABLE digest_items (
              id INTEGER PRIMARY KEY, day TEXT, ref_kind TEXT, ref_id TEXT,
              rank INTEGER, reason TEXT, sent_at INTEGER, message_id TEXT
            );
            INSERT INTO domains VALUES ('example.com', 0);
            INSERT INTO entities VALUES ('alpha', 'Alpha', 'https://example.com/a', NULL);
            INSERT INTO digest_items VALUES (1, '2026-10-08', 'entity', 'alpha', 1, 'r', 1, 'm');
            """
        )
        conn.commit()
        self.assertTrue(feedback.apply_feedback(conn, 1, "down"))
        self.assertTrue(feedback.apply_feedback(conn, 1, "up"))
        score = conn.execute("SELECT feedback_score FROM domains WHERE domain = 'example.com'").fetchone()[0]
        self.assertEqual(score, 1.0)
        reactions = {
            row[0]
            for row in conn.execute("SELECT reaction FROM feedback WHERE digest_item_id = 1")
        }
        self.assertEqual(reactions, {"up"})


if __name__ == "__main__":
    unittest.main()
