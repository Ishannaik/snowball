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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("orchestrator", ROOT / "tools/orchestrator.py")
o = importlib.util.module_from_spec(_spec)
sys.modules["orchestrator"] = o  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(o)

_ft_spec = importlib.util.spec_from_file_location("fetch_types", ROOT / "tools/fetch_types.py")
ft = importlib.util.module_from_spec(_ft_spec)
sys.modules["fetch_types"] = ft
_ft_spec.loader.exec_module(ft)
FetchResult = ft.FetchResult
SearchHit = ft.SearchHit

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
        self.assertFalse(o.status_id_in_window("https://x.com/a/status/1800000000000000000", 30))
        fresh_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        fresh = str((fresh_ms - o.SNOWFLAKE_EPOCH_MS) << 22)
        fresh_url = f"https://x.com/a/status/{fresh}"
        self.assertTrue(o.status_id_in_window(fresh_url, 30))
        self.assertEqual(o.validate_x_posts([{"url": fresh_url, "text": "model wrote this post"}], 30), [])

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
            [SearchHit("https://foundation.mozilla.org/en/what-we-fund/?utm_source=x", "MTF", "", "direct")]
            if source == "exa"
            else []
        )
        orch.router.fetch = lambda url, hit=None, private=False, **_ignored: FetchResult(
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

    def test_entity_lock_omitted_link_uses_fetched_page_canon(self):
        orch = self.make()
        orch.gemini._call = StubEngine([EXTRACT_REPLY])  # row link is None
        orch.grok._call = StubEngine([VERIFY_KEEP])
        orch.run(1, "oss-grants")
        page = self.store.one("SELECT id, canon_url FROM pages")
        ent = self.store.one("SELECT evidence_page_id, link FROM entities")
        self.assertEqual(ent["evidence_page_id"], page["id"])
        self.assertEqual(ent["link"], page["canon_url"])


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

    def test_run_once_researches_topic_from_explicit_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            topics = Path(td) / "topics.yml"
            topics.write_text(
                "seeds:\n"
                "  - id: funded-on-campus-masters\n"
                "    family: opportunities\n"
                "    focus: funded on-campus computer science masters\n"
                "    regions: [global]\n"
                "    audiences: [students]\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "snowball.py"), "run-once",
                    "--topics", str(topics), "--topic", "funded-on-campus-masters",
                    "--db", str(Path(td) / "research.db"), "--dry-run", "--json",
                ],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wave = json.loads(result.stdout)
            self.assertEqual(
                [topic["topic"] for topic in wave["topics"]],
                ["funded-on-campus-masters"],
            )


class RuntimeBehaviorGates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "snowball.db"
        self.cfg = o.load_config()
        self.store = o.Store.open(self.db)
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO families(name, recheck_days, weight, relevance_floor, focus) VALUES "
                "('opportunities', 14, 1.0, 0.08, 'non-dilutive grants, scholarships, fellowships and credit programs') "
                "ON CONFLICT DO NOTHING"
            )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_hub_rows_diff_20_to_22_queues_two_and_halves_interval(self):
        """T5: 20->22 rows queues exactly two new pages and halves interval. Unchanged hub doubles interval and uses zero extraction tokens."""
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            _, tid = orch.locks.topic_lock(
                db, "Test Grants Hub", "grants and awards directory", "opportunities", "seed", key="test-grants"
            )
            pid = orch.locks.enqueue_url(db, "https://example.com/directory", tid, "seed", 14)
            db.execute("UPDATE pages SET status='fetched', kind='hub' WHERE id=?", (pid,))

        topic = self.store.one("SELECT * FROM topics WHERE id=?", (tid,))
        fam = self.store.one("SELECT * FROM families WHERE name='opportunities'")
        run = o.TopicRun(topic, fam, False)
        page_row = self.store.one("SELECT * FROM pages WHERE id=?", (pid,))

        # 1. First fetch with 20 rows
        links_20 = [(f"https://example.com/grant/{i}", f"Grant Program {i}") for i in range(1, 21)]
        res_20 = FetchResult("https://example.com/directory", "direct", "ok", 200, "Active Grants Directory", "List of grants.\n" * 20, links=links_20)
        orch.record_fetch(run, page_row, res_20)

        initial_hub_rows = self.store.one("SELECT count(*) n FROM hub_rows WHERE hub_page_id=?", (pid,))["n"]
        self.assertEqual(initial_hub_rows, 20)

        # 2. Second fetch with 22 rows (20 existing + 2 new)
        links_22 = links_20 + [
            ("https://example.com/grant/21", "Grant Program 21"),
            ("https://example.com/grant/22", "Grant Program 22"),
        ]
        res_22 = FetchResult("https://example.com/directory", "direct", "ok", 200, "Active Grants Directory", "List of grants.\n" * 22, links=links_22)
        page_row = self.store.one("SELECT * FROM pages WHERE id=?", (pid,))
        orch.record_fetch(run, page_row, res_22)

        # Check: 20 -> 22 rows queues exactly two new pages
        queued_pages = self.store.all("SELECT canon_url FROM pages WHERE discovered_via='hub' ORDER BY canon_url")
        new_hub_urls = [p["canon_url"] for p in queued_pages]
        expected_two_urls = ["https://example.com/grant/21", "https://example.com/grant/22"]
        self.assertEqual(new_hub_urls, expected_two_urls)

        # Check: halves interval
        page_after_diff = self.store.one("SELECT interval_sec FROM pages WHERE id=?", (pid,))
        self.assertEqual(page_after_diff["interval_sec"], 7 * 86400)

        # 3. Third fetch with unchanged rows (22 rows again)
        tokens_before_unchanged_hub = orch.budget.tokens_today("local")
        res_unchanged = FetchResult("https://example.com/directory", "direct", "ok", 200, "Active Grants Directory", "List of grants.\n" * 22, links=links_22)
        page_row = self.store.one("SELECT * FROM pages WHERE id=?", (pid,))
        orch.record_fetch(run, page_row, res_unchanged)

        # Check: unchanged hub doubles interval and uses zero extraction tokens
        page_after_unchanged = self.store.one("SELECT interval_sec FROM pages WHERE id=?", (pid,))
        self.assertEqual(page_after_unchanged["interval_sec"], 14 * 86400)
        tokens_after_unchanged_hub = orch.budget.tokens_today("local")
        self.assertEqual(tokens_after_unchanged_hub, tokens_before_unchanged_hub)

    def test_five_paraphrases_yield_one_claim_five_sources_four_variants(self):
        """T6: Verify five paraphrases/five independent domains yield one claim, five sources, and four variants.
        Exclude reprints from independent counts; preserve numeric differences as material facts."""
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            _, tid = orch.locks.topic_lock(db, "Mozilla Tech Fund", "grants", "opportunities", "seed", key="moz-fund")

        paraphrases = [
            ("Mozilla Technology Fund offers grants of up to $50,000 for open-source AI projects in 2026.", "domain1.org", False),
            ("The Mozilla Technology Fund provides up to $50,000 in grant funding for open-source AI builders.", "domain2.com", False),
            ("In 2026, Mozilla Technology Fund awards grants reaching $50,000 to open-source developers.", "domain3.net", False),
            ("Open-source developers can receive grants up to $50,000 from Mozilla Technology Fund.", "domain4.io", False),
            ("Grants up to $50,000 are awarded by Mozilla Technology Fund for open-source accountability projects.", "domain5.ai", False),
        ]

        with self.store.tx() as db:
            for text, dom, reprint in paraphrases:
                pid = orch.locks.enqueue_url(db, f"https://{dom}/page", tid, "seed", 14)
                orch.locks.claim_lock(db, text, pid, tid, dom, (0, len(text)), reprint)

        claims = self.store.all("SELECT * FROM claims")
        self.assertEqual(len(claims), 1, "Five paraphrases must merge into exactly one canonical claim")
        self.assertEqual(claims[0]["n_sources"], 5, "Five independent domains must yield 5 sources")

        variants = self.store.all("SELECT * FROM claim_variants WHERE claim_hash=?", (claims[0]["hash"],))
        self.assertEqual(len(variants), 4, "Four subsequent paraphrases must produce exactly four variants")

        # Reprint domain does NOT increment n_sources
        with self.store.tx() as db:
            reprint_pid = orch.locks.enqueue_url(db, "https://syndicated-news.com/copy", tid, "seed", 14)
            orch.locks.claim_lock(db, paraphrases[0][0], reprint_pid, tid, "syndicated-news.com", (0, len(paraphrases[0][0])), reprint=True)
        claim_after_reprint = self.store.one("SELECT n_sources FROM claims WHERE hash=?", (claims[0]["hash"],))
        self.assertEqual(claim_after_reprint["n_sources"], 5, "Reprint must not increment independent source count")

        # Claim with a DIFFERENT number ($90,000 instead of $50,000) is a material fact and must NOT merge
        diff_number_claim = "Mozilla Technology Fund offers grants of up to $90,000 for open-source AI projects in 2026."
        with self.store.tx() as db:
            diff_pid = orch.locks.enqueue_url(db, "https://different-fact.org/story", tid, "seed", 14)
            verdict = orch.locks.claim_lock(db, diff_number_claim, diff_pid, tid, "different-fact.org", (0, len(diff_number_claim)), False)
        self.assertEqual(verdict, "new", "Claim with different numbers must not merge")
        self.assertEqual(self.store.one("SELECT count(*) n FROM claims")["n"], 2)

    def test_private_topic_does_not_reuse_public_key(self):
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            verdict, public_id = orch.locks.topic_lock(
                db, "Forum Notes", "what people tried", "opportunities", "seed", private=False, key="forum-notes"
            )
            self.assertEqual(verdict, "new")
            verdict2, private_id = orch.locks.topic_lock(
                db, "Forum Notes", "what people tried", "opportunities", "seed", private=True, key="forum-notes"
            )
        self.assertEqual(verdict2, "new")
        self.assertNotEqual(public_id, private_id)
        pub = self.store.one("SELECT private, key FROM topics WHERE id=?", (public_id,))
        priv = self.store.one("SELECT private, key FROM topics WHERE id=?", (private_id,))
        self.assertEqual(pub["private"], 0)
        self.assertEqual(priv["private"], 1)
        self.assertEqual(priv["key"], "forum-notes-private")

    def test_30_labelled_topic_pairs_benchmark(self):
        """T6: Benchmark 30 labelled topic pairs at existing 0.75/0.30 thresholds; meet >=95% new/not-new and >=80% same/deepen agreement."""
        from collections import Counter
        fixture_path = ROOT / "tests/topic_pairs_30.jsonl"
        self.assertTrue(fixture_path.exists(), "tests/topic_pairs_30.jsonl must exist")

        pairs = []
        with open(fixture_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    pairs.append(json.loads(line))
        self.assertEqual(len(pairs), 30)

        # Build IDF dictionary
        all_terms = []
        for p in pairs:
            all_terms.append(o.topic_terms(p["topic_a"]["title"], p["topic_a"]["focus"]))
            all_terms.append(o.topic_terms(p["topic_b"]["title"], p["topic_b"]["focus"]))

        df = Counter()
        for t in all_terms:
            for term in set(t.keys()):
                df[term] += 1
        n_docs = len(all_terms)

        correct_new_not_new = 0
        correct_exact_label = 0

        for p in pairs:
            ta = o.topic_terms(p["topic_a"]["title"], p["topic_a"]["focus"])
            tb = o.topic_terms(p["topic_b"]["title"], p["topic_b"]["focus"])
            score = o.tfidf_cosine(ta, tb, df, n_docs)

            pred_new = "new" if score < 0.30 else "not-new"
            actual_new = "new" if p["label"] == "new" else "not-new"
            if pred_new == actual_new:
                correct_new_not_new += 1

            pred_label = "same" if score >= 0.75 else ("deepen" if score >= 0.30 else "new")
            if pred_label == p["label"]:
                correct_exact_label += 1

        new_agreement = (correct_new_not_new / len(pairs)) * 100.0
        exact_agreement = (correct_exact_label / len(pairs)) * 100.0

        self.assertGreaterEqual(new_agreement, 95.0, f"New/Not-new agreement {new_agreement}% must meet >= 95%")
        self.assertGreaterEqual(exact_agreement, 80.0, f"Same/Deepen exact agreement {exact_agreement}% must meet >= 80%")

    def test_drift_exclusion_drops_off_topic_page_and_prevents_reentry(self):
        """T6: Enforce family relevance floor before frontier expansion. An off-topic page becomes dropped and cannot re-enter."""
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            _, tid = orch.locks.topic_lock(db, "Developer Grants", "software startup grants", "opportunities", "seed", key="dev-grants")
            pid = orch.locks.enqueue_url(db, "https://offtopic-baking.com/recipe", tid, "seed", 14)

        topic = self.store.one("SELECT * FROM topics WHERE id=?", (tid,))
        fam = self.store.one("SELECT * FROM families WHERE name='opportunities'")
        run = o.TopicRun(topic, fam, False)
        page_row = self.store.one("SELECT * FROM pages WHERE id=?", (pid,))

        off_topic_text = (
            "Traditional sourdough baking requires 80% hydration and active wild yeast levain. "
            "Mix the flour and water, allow autolyse for 60 minutes, then fold gently every half hour. "
            "Bake in a Dutch oven at 230 degrees Celsius for optimal crust and crumb texture."
        ) * 5
        off_topic_res = FetchResult("https://offtopic-baking.com/recipe", "direct", "ok", 200, "Sourdough Bread Recipe", off_topic_text, links=[("https://offtopic-baking.com/croissants", "Croissants")])

        orch.record_fetch(run, page_row, off_topic_res)

        # Page must be marked 'dropped'
        page_after = self.store.one("SELECT status, relevance FROM pages WHERE id=?", (pid,))
        self.assertEqual(page_after["status"], "dropped")
        self.assertLess(page_after["relevance"], 0.08)

        # Cannot re-enter through a different discovered link in the same window
        with self.store.tx() as db:
            reentered_id = orch.locks.enqueue_url(db, "https://offtopic-baking.com/recipe", tid, "hub", 14)
            self.assertIsNone(reentered_id, "Dropped off-topic page must not re-enter the frontier")

    def test_task_heartbeat_and_reaper_invalidation(self):
        """T7: Heartbeat active tasks every 60s, reap after 600s, increment occurrence sequence at reclamation,
        and verify that stale commits affect zero rows."""
        orch = o.SnowballOrchestrator(self.cfg, self.store, mode="local")
        with self.store.tx() as db:
            tid = orch.new_task(db, "fetch", {"url": "https://test.com"}, 1)

        seq = orch.claim_task(tid)
        self.assertIsNotNone(seq)
        self.assertTrue(orch.heartbeat(tid, seq))

        # Simulate staleness (> 600s)
        with self.store.tx() as db:
            db.execute("UPDATE tasks SET heartbeat_at=strftime('%s','now') - 700 WHERE id=?", (tid,))

        reaped = orch.reap()
        self.assertEqual(reaped, 1)

        # Sequence must be invalidated on reclamation
        task_reaped = self.store.one("SELECT status, occurrence_seq, worker_id FROM tasks WHERE id=?", (tid,))
        self.assertEqual(task_reaped["status"], "pending")
        self.assertEqual(task_reaped["occurrence_seq"], seq + 1)
        self.assertIsNone(task_reaped["worker_id"])

        # Stale commit with old sequence must fail and write zero rows
        self.assertFalse(orch.commit_with_reclaimed_occurrence(seq, task_id=tid))
        self.assertFalse(orch.finish_task(tid, seq, "done", result={"status": "late"}))

        # Check due=0 fetch count stays zero
        self.assertEqual(self.store.one("SELECT count(*) n FROM fetches WHERE due=0")["n"], 0)

    def test_10000_deterministic_seeded_slug_property_cases(self):
        """T5: 10,000 deterministic seeded slug-property cases verifying fixed point slug(slug(x)) == slug(x)."""
        import random
        rng = random.Random(42)
        words = ["Grants", "AI", "Startup", "2026", "Fellowship", "Scholarship", "Österreich", "Tech", "Fund", "Gov.in", "Web3", "Open-Source"]
        for _ in range(10000):
            sample = " ".join(rng.choices(words, k=rng.randint(1, 5)))
            punct = rng.choice(["!", "@", "#", "$", "%", "—", "_", "/", ""])
            raw = f"{sample} {punct} {rng.randint(0, 9999)}"
            s = o.slug(raw)
            self.assertEqual(o.slug(s), s, f"slug must be idempotent fixed-point for {raw!r}")
            self.assertLessEqual(len(s), 80)
            self.assertFalse(s.startswith("-"))
            self.assertFalse(s.endswith("-"))

    def test_classifier_accuracy_on_50_labelled_pages(self):
        """T5: Run classifier on 50 labelled pages meeting >= 85% accuracy."""
        fixture_path = ROOT / "tests/pages_labelled_50.jsonl"
        self.assertTrue(fixture_path.exists())
        total, correct = 0, 0
        with open(fixture_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                actual = item["label"]
                pred = o.classify_page(item.get("title", ""), item.get("text", "") or item.get("html", ""), item.get("url", ""))
                total += 1
                if pred == actual:
                    correct += 1
        accuracy = (correct / total) * 100.0 if total else 0.0
        self.assertGreaterEqual(accuracy, 85.0, f"Classifier accuracy {accuracy}% must meet >= 85%")

    def test_validate_extraction_on_20_pipeline_fixtures(self):
        """T5: Extraction validation on 20 real pipeline fixtures."""
        fixture_path = ROOT / "tests/items_20.jsonl"
        self.assertTrue(fixture_path.exists())
        with open(fixture_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                res = o.validate_extraction({"rows": item.get("entities", [])}, item.get("text", ""), fallback_url=item.get("url", ""))
                self.assertEqual(len(res["rows"]), len(item.get("entities", [])))
                for row in res["rows"]:
                    self.assertTrue(row["name"])
                    self.assertTrue(row["evidence_quote"])
                    self.assertTrue(row["link"])

    def test_provider_diagnostics_detects_missing_credentials(self):
        """T1: Provider diagnostics report authentic availability checks without fake key-presence success."""
        cfg = o.Config()
        cfg.gemini_transport = "api"
        cfg.gemini_api_key = None
        diag = o.diagnose_providers(cfg, live=False)
        self.assertFalse(diag["all_ok"])
        self.assertEqual(diag["gemini"]["status"], "blocked")
        self.assertIn("not configured", diag["gemini"]["error"])
        self.assertIn("exa", diag)
        self.assertIn("grok", diag)
        self.assertIn("local", diag)

    def test_provider_diagnostics_ssh_omp_transport(self):
        """T1: Provider diagnostics supports explicit ssh-omp transport with ssh host check."""
        cfg = o.Config()
        cfg.gemini_transport = "ssh-omp"
        cfg.ssh_model_host = "nonexistent-oracle-host.test"
        cfg.ssh_timeout_sec = 2
        diag = o.diagnose_providers(cfg, live=False)
        self.assertEqual(diag["gemini"]["transport"], "ssh-omp")
        # Should fail reachability on nonexistent host without throwing uncaught exceptions
        self.assertIn(diag["gemini"]["status"], {"error", "blocked"})

    def test_reserve_rejects_when_daily_cap_would_be_exceeded(self):
        """A reservation that does not fit the remaining daily cap writes nothing."""
        store = o.Store.in_memory()
        cfg = o.Config()
        cfg.caps = {"gemini": 100, "grok": 100, "local": 100}
        cfg.llm_tokens_per_day = 100
        budget = o.TokenBudgetMiddleware(store, cfg)
        tb = budget.task("local", max_in=10_000, max_out=10_000)
        budget.reserve(tb, 40, 40)
        self.assertEqual(budget.tokens_today("local"), 80)
        with self.assertRaises(o.BudgetExhausted):
            budget.reserve(tb, 30, 0)
        self.assertEqual(budget.tokens_today("local"), 80)
        self.assertEqual(sum(budget.spent()), 80)

    def test_reconcile_cannot_drive_spend_negative(self):
        """A second release cannot reopen budget by pushing stored totals below zero."""
        store = o.Store.in_memory()
        cfg = o.Config()
        cfg.caps = {"gemini": 100, "grok": 100, "local": 100}
        cfg.llm_tokens_per_day = 100
        budget = o.TokenBudgetMiddleware(store, cfg)
        tb = budget.task("local", 1000, 1000)
        budget.reserve(tb, 40, 10)
        budget.reconcile(tb, 40, 10, 0, 0)
        budget.reconcile(tb, 40, 10, 0, 0)
        self.assertEqual(budget.tokens_today("local"), 0)
        self.assertEqual(sum(budget.spent()), 0)
        self.assertEqual(tb.used_in, 0)
        self.assertEqual(tb.used_out, 0)
        budget.reserve(tb, 100, 0)
        self.assertEqual(budget.tokens_today("local"), 100)
if __name__ == "__main__":
    unittest.main()
