"""Offline tests for tools/orchestrator.py. No network, no model: engines and the router are stubbed.

python3 -m unittest tests/test_orchestrator.py -v
"""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("orchestrator", ROOT / "tools/orchestrator.py")
o = importlib.util.module_from_spec(_spec)
sys.modules["orchestrator"] = o  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(o)

PAGE = (
    "Mozilla Technology Fund 2026 is open. The Mozilla Foundation offers grants of up to $50,000 for "
    "open-source projects that make AI more accountable. Applications close on 2026-12-15. "
    "Individuals and organisations from any country, including India, may apply. "
) * 3


class Primitives(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(
            o.canonical_url("http://www.Example.com/a/index.html?utm_source=x&b=2&a=1#frag"),
            "https://example.com/a?a=1&b=2",
        )
        self.assertEqual(
            o.canonical_url("https://old.reddit.com/r/x/comments/abc123/title/"), "https://reddit.com/comments/abc123"
        )
        self.assertEqual(o.canonical_url("https://github.com/Foo/Bar/tree/main"), "https://github.com/foo/bar")
        self.assertIsNone(o.canonical_url("ftp://x.com/a"))

    def test_query_and_slug(self):
        self.assertEqual(o.norm_query("grants India"), o.norm_query("india  GRANTS!"))
        s = o.slug("Mozilla Technology Fund — 2026!")
        self.assertEqual(o.slug(s), s)
        self.assertEqual(
            o.entity_key("Mozilla Technology Fund", "Mozilla Foundation", "2026-12-15"),
            "mozilla-technology-fund|mozilla-foundation|2026",
        )

    def test_similarity_and_quote(self):
        self.assertGreaterEqual(
            o.hybrid_similarity("The fund gives $50,000 grants", "The fund gives $50,000 grants."), 0.70
        )
        ok, s, e = o.quote_ok("grants of up to $50,000 for open-source projects", PAGE)
        self.assertTrue(ok)
        self.assertFalse(o.quote_ok("grants of up to $90,000 for closed projects only", PAGE)[0])

    def test_snowflake(self):
        # 1800000000000000000 decodes to 2024-07-10; garbage ids do not decode
        self.assertEqual(o.snowflake_utc("1800000000000000000").year, 2024)
        self.assertIsNone(o.snowflake_utc("12"))
        self.assertIsNone(o.snowflake_utc("abc"))
        self.assertEqual(
            o.validate_x_posts([{"url": "https://x.com/a/status/1800000000000000000", "text": "old"}], 30), []
        )

    def test_ssrf(self):
        for host, port in (("localhost", 443), ("127.0.0.1", 443), ("example.com", 8080)):
            with self.assertRaises(o.SSRFBlocked):
                o.resolve_and_pin(host, port)

    def test_substring_guard_drops_hallucination(self):
        data = {
            "rows": [
                {
                    "name": "Mozilla Technology Fund",
                    "kind": "grant",
                    "amount": 50000,
                    "deadline": "2026-12-15",
                    "evidence_quote": "The Mozilla Foundation offers grants of up to $50,000 for open-source projects",
                },
                {
                    "name": "Imaginary Fellowship",
                    "kind": "fellowship",
                    "evidence_quote": "The Imaginary Fellowship pays $1M to anyone who asks nicely, no questions asked.",
                },
            ],
            "names": ["Mozilla Foundation", "Made Up Org"],
            "claims": [],
        }
        g = o.substring_guard(data, PAGE)
        self.assertEqual([r["name"] for r in g["rows"]], ["Mozilla Technology Fund"])
        self.assertEqual(g["names"], ["Mozilla Foundation"])
        self.assertEqual(g["dropped"], 1)

    def test_mechanical_check(self):
        row = {"amount": 50000.0, "deadline": "2026-12-15"}
        self.assertTrue(o.mechanical_check(row, PAGE)[0])
        self.assertFalse(o.mechanical_check({"amount": 75000.0}, PAGE)[0])
        self.assertTrue(o.mechanical_check({"amount": 50000.0}, "grants up to $50k each")[0])


class StubEngine:
    """Replaces Engine._call. Records every call, so a private-topic test can prove zero leakage."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, system, user, max_out):
        self.calls.append(user)
        r = self.replies.pop(0) if self.replies else '{"rows":[],"names":[],"claims":[],"verdicts":[]}'
        if isinstance(r, Exception):
            raise r
        return o.EngineReply(r, 100, 50, "stub")


EXTRACT_REPLY = json.dumps(
    {
        "rows": [
            {
                "name": "Mozilla Technology Fund",
                "org": "Mozilla Foundation",
                "kind": "grant",
                "amount": 50000,
                "currency": "USD",
                "deadline": "2026-12-15",
                "eligibility": "Individuals and organisations from any country",
                "link": None,
                "evidence_quote": "The Mozilla Foundation offers grants of up to $50,000 for open-source projects",
            },
            {
                "name": "Ghost Grant",
                "kind": "grant",
                "evidence_quote": "Ghost Grant gives everyone free money every single day forever.",
            },
        ],
        "names": ["Mozilla Foundation"],
        "claims": [
            {
                "text": "Mozilla Technology Fund applications close on 2026-12-15.",
                "evidence_quote": "Mozilla Technology Fund 2026 is open. The Mozilla Foundation offers grants",
            }
        ],
    }
)
VERIFY_KEEP = json.dumps({"verdicts": [{"id": 0, "verdict": "keep", "reason": "supported", "india_eligible": "yes"}]})


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "snowball.db"
        o.RAW_DIR = Path(self.tmp.name) / "raw"
        self.cfg = o.load_config()
        self.cfg.gemini_api_key = "test-key"
        self.store = o.Store.open(self.db)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def make(self, mode="auto", page_text=PAGE, private=False):
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode=mode)
        orch.seed_from_yaml = lambda: 0
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO families(name, recheck_days, focus) VALUES ('opportunities', 14, 'grants') "
                "ON CONFLICT DO NOTHING"
            )
            orch.locks.topic_lock(
                db,
                "open source grants",
                "grants for open-source AI accountability projects",
                "opportunities",
                "seed",
                private=private,
                key="oss-grants",
            )
        orch.router.exa_allowed = lambda: True
        orch.router.search = lambda raw, source, days, family="opportunities": (
            [o.SearchHit("https://foundation.mozilla.org/en/what-we-fund/?utm_source=x", "MTF", "", "direct")]
            if source == "exa"
            else []
        )
        orch.router.fetch = lambda url, hit=None: o.FetchResult(
            o.canonical_url(url), "direct", "ok", 200, "Mozilla Technology Fund", page_text, final_url=url
        )
        orch.local.available = lambda: (True, "ok")
        orch.grok.available = lambda: (True, "ok")
        return orch

    def test_full_dag_commits_and_rejects(self):
        orch = self.make()
        orch.gemini._call = gem = StubEngine([EXTRACT_REPLY])
        orch.grok._call = grok = StubEngine([VERIFY_KEEP])
        summary = orch.run(1, "oss-grants")
        self.assertEqual(len(gem.calls), 1)
        self.assertEqual(len(grok.calls), 1)
        self.assertIn("<untrusted_source", gem.calls[0])
        ents = self.store.all("SELECT key, amount, deadline FROM entities")
        self.assertEqual([e["key"] for e in ents], ["mozilla-technology-fund|mozilla-foundation|2026"])
        self.assertEqual(self.store.one("SELECT count(*) n FROM claims")["n"], 1)
        self.assertEqual(self.store.one("SELECT status FROM pages")["status"], "scheduled")
        self.assertEqual(summary["topics"][0]["rows_guard_dropped"], 1)  # Ghost Grant never reached the DB
        spend = {r["engine"]: r["tokens_in"] for r in self.store.all("SELECT * FROM engine_spend")}
        self.assertEqual(set(spend), {"gemini", "grok"})

    def test_grok_drop_is_final_and_logged(self):
        orch = self.make()
        orch.gemini._call = StubEngine([EXTRACT_REPLY])
        orch.grok._call = StubEngine(
            [
                json.dumps(
                    {
                        "verdicts": [
                            {
                                "id": 0,
                                "verdict": "drop",
                                "reason": "deadline unsupported",
                                "unsupported_fields": ["deadline"],
                            }
                        ]
                    }
                )
            ]
        )
        orch.run(1, "oss-grants")
        self.assertEqual(self.store.one("SELECT count(*) n FROM entities")["n"], 0)
        self.assertEqual(self.store.one("SELECT count(*) n FROM verify_log WHERE tier=3")["n"], 1)

    def test_entity_change_is_a_diff(self):
        orch = self.make()
        orch.gemini._call = StubEngine([EXTRACT_REPLY])
        orch.grok._call = StubEngine([VERIFY_KEEP])
        orch.run(1, "oss-grants")
        page2 = PAGE.replace("$50,000", "$75,000")
        orch2 = self.make(page_text=page2)
        with self.store.tx() as db:  # make the page and topic due again
            db.execute("UPDATE pages SET next_check_at=0")
            db.execute("UPDATE topics SET last_run_at=0")
        orch2.gemini._call = StubEngine([EXTRACT_REPLY.replace("50000", "75000").replace("$50,000", "$75,000")])
        orch2.grok._call = StubEngine([VERIFY_KEEP])
        orch2.run(1, "oss-grants")
        ch = self.store.all("SELECT field, old, new FROM entity_changes")
        self.assertEqual([(c["field"], c["new"]) for c in ch], [("amount", "75000.0")])
        self.assertEqual(self.store.one("SELECT status FROM entities")["status"], "changed")

    def test_private_topic_never_leaves_the_box(self):
        orch = self.make(mode="gemini-bulk", private=True)
        orch.gemini._call = gem = StubEngine([])
        orch.grok._call = grok = StubEngine([])
        orch.grok.x_search = lambda *a: self.fail("X search on a private topic")
        orch.local._call = local = StubEngine([EXTRACT_REPLY, VERIFY_KEEP])
        orch.run(1, "oss-grants")
        self.assertEqual((len(gem.calls), len(grok.calls)), (0, 0))
        self.assertEqual(len(local.calls), 2)
        self.assertEqual(self.store.one("SELECT count(*) n FROM entities")["n"], 1)
        self.assertNotIn("grok", [q["source"] for q in self.store.all("SELECT source FROM queries")])

    def test_outage_pauses_engine_refunds_and_defers(self):
        orch = self.make()
        orch.gemini._call = StubEngine([o.EngineOutage("HTTP 429", retry_after=120)])
        orch.run(1, "oss-grants")
        self.assertEqual(self.store.one("SELECT status FROM pages")["status"], "fetched")  # waits, not scheduled
        t = self.store.one("SELECT status, attempts, outage_pauses FROM tasks WHERE kind='extract'")
        self.assertEqual((t["status"], t["attempts"], t["outage_pauses"]), ("pending", 0, 1))
        st = self.store.one("SELECT paused_until FROM engine_state WHERE engine='gemini'")
        self.assertGreater(st["paused_until"], o.now() + 100)
        # next run: Gemini is paused, so auto mode extracts the leftover page locally
        orch2 = self.make()
        orch2.local._call = StubEngine([EXTRACT_REPLY])
        orch2.grok._call = StubEngine([VERIFY_KEEP])
        with self.store.tx() as db:
            db.execute("UPDATE topics SET last_run_at=0")
        orch2.run(1, "oss-grants")
        self.assertEqual(self.store.one("SELECT count(*) n FROM entities")["n"], 1)

    def test_daily_cap_cuts_cleanly(self):
        self.cfg.caps["gemini"] = 10
        orch = self.make(mode="gemini-bulk")
        orch.gemini._call = StubEngine([EXTRACT_REPLY])
        summary = orch.run(1, "oss-grants")
        self.assertEqual(self.store.one("SELECT count(*) n FROM entities")["n"], 0)
        self.assertTrue(summary["topics"][0].get("error") or summary["topics"][0].get("budget_cut"))

    def test_query_lock_skips_second_run(self):
        orch = self.make()
        orch.gemini._call = StubEngine([EXTRACT_REPLY])
        orch.grok._call = StubEngine([VERIFY_KEEP])
        orch.run(1, "oss-grants")
        n1 = self.store.one("SELECT count(*) n FROM queries")["n"]
        with self.store.tx() as db:
            db.execute("UPDATE topics SET last_run_at=0")
        orch.run(1, "oss-grants")
        seed = "SELECT count(*) n FROM queries WHERE discovered_via='seed'"
        self.assertEqual(self.store.one(seed)["n"], n1)  # the same seed words are never searched twice
        self.assertGreater(self.store.one("SELECT sum(dup_requests) n FROM queries")["n"], 0)
        # run 1 found "Mozilla Foundation" in the page; run 2 spends exactly one search on it
        nq = self.store.all("SELECT raw FROM queries WHERE discovered_via='name'")
        self.assertEqual([q["raw"] for q in nq], ["Mozilla Foundation"])
        self.assertEqual(self.store.one("SELECT status FROM names")["status"], "searched")
        with self.store.tx() as db:
            db.execute("UPDATE topics SET last_run_at=0")
        orch.run(1, "oss-grants")
        self.assertEqual(len(self.store.all("SELECT raw FROM queries WHERE discovered_via='name'")), 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM fetches")["n"], 1)  # page not due: never re-fetched


class Cli(unittest.TestCase):
    def test_status_and_dry_run_leave_db_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "s.db"

            def run(*a):
                return subprocess.run(
                    [sys.executable, str(ROOT / "tools/orchestrator.py"), "--db", str(db), *a],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

            r = run("--status")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("domain yield", r.stdout)
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            r = run("--dry-run", "--max-topics", "2")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("dry run", r.stdout)
            self.assertIn("would call: search", r.stdout)
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
