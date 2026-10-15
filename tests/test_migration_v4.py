"""A representative legacy corpus must survive an explicit, reversible v4 cutover."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('snowball_migration_fixture_goal', ROOT / 'tools' / 'goal.py')
loader = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = loader
spec.loader.exec_module(loader)
orch, migration = loader._sibling('orchestrator'), loader._sibling('migration')

SOURCE = 'Historical Example Scholarship: tuition USD 1000 per year. Human source text.'
WIKI = """# Historical source

Human note: keep this edit.

## Facts

<!-- snowball:facts:start -->
Code-owned fact: synthetic tuition note.
<!-- snowball:facts:end -->

## Notes

Human region stays verbatim.
"""
GOAL_DDL = loader._sibling('goal_schema').DDL


def _link_dir(src: Path, dst: Path) -> None:
    src.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=True)
    except OSError:
        subprocess.check_call(['cmd', '/c', 'mklink', '/J', str(dst), str(src)])


def legacy_fixture(root: Path, version: int = 3) -> Path:
    (root / 'data').mkdir(parents=True)
    (root / 'raw' / '2025-01-01').mkdir(parents=True)
    (root / 'wiki' / 'entities').mkdir(parents=True)
    raw = root / 'raw' / '2025-01-01' / 'source.txt'
    raw.write_text(SOURCE, encoding='utf-8')
    (root / 'wiki' / 'entities' / 'historical.md').write_text(WIKI, encoding='utf-8')
    path = root / 'data' / 'legacy.db'
    db = sqlite3.connect(path, isolation_level=None)
    ddl = orch.DDL.replace("('schema_version', '4')", f"('schema_version', '{version}')")
    ddl = ddl.replace(",'gap','hypothesis'", '').replace(",'evaluate'", '')
    extension = orch.ORCH_DDL.replace(",'review','plan'", '')
    if version < 3:
        ddl = ddl.replace(",'deadline'", '').replace(",'2captcha'", '')
    db.executescript(ddl)
    db.executescript(extension)
    outcome = 'deadline' if version >= 3 else 'ok'
    provider = '2captcha' if version >= 3 else 'zenrows'
    db.execute('INSERT INTO families(name,recheck_days,focus) VALUES(?,?,?)', ('highered', 30, 'Public degree funding'))
    db.execute(
        "INSERT INTO topics(id,key,family,title,focus,discovered_via,created_at) "
        "VALUES(7,'historical','highered','Historical source','Degree funding','seed',100)"
    )
    db.execute("INSERT INTO domains(domain) VALUES('example.edu')")
    db.execute(
        'INSERT INTO pages(id,canon_url,domain,topic_id,title,raw_path,status,next_check_at,interval_sec,discovered_via,created_at) '
        "VALUES(11,'https://example.edu/funding','example.edu',7,'Historical source',?,'fetched',100,86400,'seed',100)",
        (str(raw),),
    )
    db.execute(
        'INSERT INTO entities(key,name,norm_name,kind,family,evidence_quote,evidence_page_id,first_seen_at,last_seen_at,wiki_path) '
        "VALUES('historical','Historical Example Scholarship','historical example scholarship','scholarship','highered',?,11,100,100,'wiki/entities/historical.md')",
        (SOURCE,),
    )
    db.execute("INSERT INTO entity_sources(entity_key,page_id,seen_at) VALUES('historical',11,100)")
    db.execute(
        "INSERT INTO claims(hash,text,norm_text,topic_id,entity_key,first_seen_at,last_seen_at) "
        "VALUES('claim1',?,'historical funding',7,'historical',100,100)",
        (SOURCE,),
    )
    db.execute(
        'INSERT INTO claim_sources(claim_hash,page_id,quote_start,quote_end) VALUES(?,?,?,?)',
        ('claim1', 11, 0, len(SOURCE)),
    )
    db.execute(
        "INSERT INTO queries(id,query_key,norm,raw,source,window_key,topic_id,discovered_via,scheduled_at) "
        "VALUES(13,'q1','degree funding','degree funding','exa',0,7,'seed',100)"
    )
    db.execute(
        "INSERT INTO tasks(id,kind,payload,next_attempt_at,created_at) VALUES(17,'fetch','{\"page_id\":11}',100,100)"
    )
    db.execute(
        'INSERT INTO fetches(id,page_id,at,route,http_status,bytes,ms,outcome,pinned_ip,due) '
        "VALUES(19,11,100,'direct',200,42,5,?,'203.0.113.10',100)",
        (outcome,),
    )
    db.execute(
        'INSERT INTO paid_fetches(id,page_id,provider,cost_units,at,outcome) VALUES(23,11,?,0,100,\'ok\')',
        (provider,),
    )
    db.execute(
        "INSERT INTO engine_spend(day,engine,calls,tokens_in,tokens_out) VALUES('2026-01-02','grok',2,11,3)"
    )
    db.close()
    return path


def _goal_ddl() -> str:
    return GOAL_DDL


class ExplicitMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = legacy_fixture(self.root)
        self.text = SOURCE

    def tearDown(self):
        self.temp.cleanup()

    def test_read_or_open_never_upgrades_legacy_and_missing_read_never_creates(self):
        for opening in (lambda: orch.Store.connect_existing(self.db, readonly=True), lambda: orch.Store.open(self.db)):
            with self.assertRaises(migration.MigrationRequired):
                opening()
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(migration.schema_version(db), 3)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='goals'").fetchone())
            with self.assertRaises(migration.MigrationRequired):
                migration.initialize(db, orch.DDL, orch.ORCH_DDL, _goal_ddl())
            self.assertEqual(migration.schema_version(db), 3)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='goals'").fetchone())
        missing = self.root / 'missing.db'
        with self.assertRaises(sqlite3.OperationalError):
            orch.Store.connect_existing(missing, readonly=True)
        self.assertFalse(missing.exists())
        empty = self.root / 'empty.db'
        sqlite3.connect(empty).close()
        with self.assertRaises(migration.MigrationRequired):
            orch.Store.open(empty)
        with self.assertRaises(migration.MigrationRequired):
            with contextlib.closing(sqlite3.connect(empty)) as db:
                migration.migrate(db, self.root, orch.DDL, orch.ORCH_DDL, _goal_ddl())
        with contextlib.closing(sqlite3.connect(empty)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])

    def test_refuses_nonempty_schema0_malformed_and_unknown_without_writing(self):
        path = self.root / 'schema0.db'
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE pages(id INTEGER PRIMARY KEY, raw_path TEXT)')
            db.execute("INSERT INTO pages VALUES (1, 'raw/orphan.txt')")
            db.commit()
            before = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1").fetchall()
            self.assertEqual(migration.schema_version(db), 0)
            with self.assertRaises(migration.MigrationRequired):
                migration.initialize(db, orch.DDL, orch.ORCH_DDL, _goal_ddl())
            with self.assertRaises(migration.MigrationRequired):
                migration.migrate(db, self.root, orch.DDL, orch.ORCH_DDL, _goal_ddl())
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1").fetchall(), before)
        marked = self.root / 'marked.db'
        with contextlib.closing(sqlite3.connect(marked)) as db:
            db.execute('CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute("INSERT INTO schema_meta VALUES ('schema_version', '0')")
            db.execute('CREATE TABLE pages(id INTEGER PRIMARY KEY)')
            db.commit()
            with self.assertRaises(migration.MigrationRequired):
                migration.migrate(db, self.root, orch.DDL, orch.ORCH_DDL, _goal_ddl())
            db.execute("UPDATE schema_meta SET value='04'")
            with self.assertRaises(migration.MigrationRequired):
                migration.schema_version(db)
            db.execute("UPDATE schema_meta SET value='1'")
            self.assertEqual(migration.schema_version(db), 1)
            with self.assertRaises(migration.MigrationRequired):
                migration.migrate(db, self.root, orch.DDL, orch.ORCH_DDL, _goal_ddl())
            db.execute("UPDATE schema_meta SET value='4'")
            self.assertEqual(migration.schema_version(db), 4)
            with self.assertRaises(migration.MigrationRequired):
                migration.assert_current(db)
            with self.assertRaises(migration.MigrationRequired):
                migration.migrate(db, self.root, orch.DDL, orch.ORCH_DDL, _goal_ddl())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='goals'").fetchone())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='source_snapshots'").fetchone())
        with contextlib.closing(sqlite3.connect(marked.as_uri() + '?mode=ro', uri=True)) as readonly:
            with self.assertRaises(migration.MigrationRequired):
                migration.assert_current(readonly)

    def test_cutover_preserves_ids_sources_fts_human_edits_and_round_trip_rollback(self):
        wiki = self.root / 'wiki' / 'entities' / 'historical.md'
        before = wiki.read_bytes()
        result = orch.Store.migrate(self.db)
        self.assertEqual(result['schema_version'], 4)
        self.assertEqual(wiki.read_bytes(), before)
        bundle = Path(result['bundle'])
        manifest = migration.verify_bundle(bundle)
        self.assertEqual(manifest['schema_version'], 3)
        self.assertEqual(
            {entry['path'] for entry in manifest['files']},
            {'raw/2025-01-01/source.txt', 'wiki/entities/historical.md'},
        )
        store = orch.Store.connect_existing(self.db)
        try:
            self.assertEqual(store.one('SELECT id FROM pages')[0], 11)
            self.assertEqual(store.one('SELECT id FROM queries')[0], 13)
            self.assertEqual(store.one('SELECT id FROM tasks')[0], 17)
            self.assertEqual(tuple(store.one('SELECT id,outcome FROM fetches')), (19, 'deadline'))
            self.assertEqual(tuple(store.one('SELECT id,provider FROM paid_fetches')), (23, '2captcha'))
            self.assertEqual(store.one("SELECT calls FROM engine_spend WHERE engine='grok'")[0], 2)
            self.assertIn("'deadline'", store.one("SELECT sql FROM sqlite_master WHERE name='fetches'")[0])
            self.assertIn("'review'", store.one("SELECT sql FROM sqlite_master WHERE name='engine_spend'")[0])
            self.assertEqual(store.one('SELECT provenance FROM entities')[0], 'legacy_v3')
            self.assertEqual(store.one('SELECT provenance FROM claims')[0], 'legacy_v3')
            self.assertEqual(store.one("SELECT COUNT(*) FROM claims_fts WHERE claims_fts MATCH 'funding'")[0], 1)
            self.assertEqual(store.one('SELECT COUNT(*) FROM funding_packages')[0], 0)
            self.assertEqual(store.one('SELECT COUNT(*) FROM constraint_evaluations')[0], 0)
            self.assertEqual(store.one('PRAGMA quick_check')[0], 'ok')
            self.assertEqual(store.all('PRAGMA foreign_key_check'), [])
            self.assertIn('private', {row[1] for row in store.db.execute('PRAGMA table_info(topics)')})
            self.assertEqual(Path(store.one('SELECT raw_path FROM pages')[0]).read_text(encoding='utf-8'), self.text)
        finally:
            store.db.close()
        target = self.root / 'isolated-rollback'
        restored_db = target / 'data' / 'restored.db'
        restored = migration.restore_bundle(bundle, restored_db, target)
        self.assertEqual(restored['schema_version'], 3)
        restored_wiki = target / 'wiki' / 'entities' / 'historical.md'
        self.assertEqual(restored_wiki.read_bytes(), before)
        self.assertIn('Code-owned fact: synthetic tuition note.', restored_wiki.read_text(encoding='utf-8'))
        self.assertIn('Human region stays verbatim.', restored_wiki.read_text(encoding='utf-8'))
        with contextlib.closing(sqlite3.connect(restored_db)) as db:
            raw = Path(db.execute('SELECT raw_path FROM pages WHERE id=11').fetchone()[0])
            self.assertTrue(raw.is_relative_to(target))
            self.assertEqual(raw.read_text(encoding='utf-8'), self.text)
            self.assertEqual(migration.schema_version(db), 3)
            self.assertEqual(db.execute('SELECT id,outcome FROM fetches').fetchone(), (19, 'deadline'))
            self.assertEqual(db.execute('SELECT quote_start,quote_end FROM claim_sources').fetchone(), (0, len(self.text)))
        self.assertIn('Human note: keep this edit.', restored_wiki.read_text(encoding='utf-8'))
        backup_before = len(list((self.root / 'data' / 'backup').iterdir()))
        self.assertFalse(orch.Store.migrate(self.db)['changed'])
        self.assertEqual(len(list((self.root / 'data' / 'backup').iterdir())), backup_before)

    def test_v2_populated_corpus_preserves_rows_regions_and_rolls_back(self):
        root = self.root / 'v2-corpus'
        db_path = legacy_fixture(root, 2)
        wiki = root / 'wiki' / 'entities' / 'historical.md'
        before = wiki.read_bytes()
        with contextlib.closing(sqlite3.connect(db_path)) as db:
            fetches_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='fetches'").fetchone()[0]
            paid_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='paid_fetches'").fetchone()[0]
            self.assertNotIn("'deadline'", fetches_sql)
            self.assertNotIn("'2captcha'", paid_sql)
            self.assertEqual(migration.schema_version(db), 2)
            with self.assertRaises(migration.MigrationRequired):
                migration.assert_current(db)
            with self.assertRaises(migration.MigrationRequired):
                migration.initialize(db, orch.DDL, orch.ORCH_DDL, _goal_ddl())
            self.assertEqual(migration.schema_version(db), 2)
        result = orch.Store.migrate(db_path)
        self.assertEqual(result['schema_version'], 4)
        self.assertEqual(wiki.read_bytes(), before)
        bundle = Path(result['bundle'])
        manifest = migration.verify_bundle(bundle)
        self.assertEqual(manifest['schema_version'], 2)
        self.assertEqual(
            {entry['path'] for entry in manifest['files']},
            {'raw/2025-01-01/source.txt', 'wiki/entities/historical.md'},
        )
        store = orch.Store.connect_existing(db_path)
        try:
            self.assertEqual(tuple(store.one('SELECT id,outcome FROM fetches')), (19, 'ok'))
            self.assertEqual(tuple(store.one('SELECT id,provider FROM paid_fetches')), (23, 'zenrows'))
            self.assertEqual(store.one('SELECT id FROM pages')[0], 11)
            self.assertEqual(store.one('SELECT id FROM queries')[0], 13)
            self.assertEqual(store.one('SELECT id FROM tasks')[0], 17)
            self.assertEqual(store.one("SELECT calls FROM engine_spend WHERE engine='grok'")[0], 2)
            self.assertIn("'deadline'", store.one("SELECT sql FROM sqlite_master WHERE name='fetches'")[0])
            self.assertIn("'2captcha'", store.one("SELECT sql FROM sqlite_master WHERE name='paid_fetches'")[0])
            self.assertIn("'review'", store.one("SELECT sql FROM sqlite_master WHERE name='engine_spend'")[0])
            self.assertEqual(store.one('SELECT provenance FROM entities')[0], 'legacy_v3')
            self.assertEqual(store.one("SELECT COUNT(*) FROM claims_fts WHERE claims_fts MATCH 'funding'")[0], 1)
            self.assertEqual(store.one('PRAGMA quick_check')[0], 'ok')
            self.assertEqual(store.all('PRAGMA foreign_key_check'), [])
        finally:
            store.db.close()
        target = root / 'isolated-rollback'
        restored_db = target / 'data' / 'restored.db'
        restored = migration.restore_bundle(bundle, restored_db, target)
        self.assertEqual(restored['schema_version'], 2)
        self.assertEqual((target / 'wiki' / 'entities' / 'historical.md').read_bytes(), before)
        with contextlib.closing(sqlite3.connect(restored_db)) as db:
            self.assertEqual(migration.schema_version(db), 2)
            raw = Path(db.execute('SELECT raw_path FROM pages WHERE id=11').fetchone()[0])
            self.assertTrue(raw.is_relative_to(target))
            self.assertEqual(raw.read_text(encoding='utf-8'), SOURCE)
            self.assertEqual(db.execute('SELECT id,outcome FROM fetches').fetchone(), (19, 'ok'))
            self.assertEqual(db.execute('SELECT quote_start,quote_end FROM claim_sources').fetchone(), (0, len(SOURCE)))
        backup_before = len(list((root / 'data' / 'backup').iterdir()))
        self.assertFalse(orch.Store.migrate(db_path)['changed'])
        self.assertEqual(len(list((root / 'data' / 'backup').iterdir())), backup_before)

    def test_schema_failure_does_not_advance_version_or_destroy_corpus(self):
        db = sqlite3.connect(self.db, isolation_level=None)
        try:
            with self.assertRaises((ValueError, sqlite3.OperationalError)):
                migration.migrate(
                    db, self.root, orch.DDL, orch.ORCH_DDL, _goal_ddl() + '\nCREATE TABLE invalid ('
                )
            self.assertEqual(migration.schema_version(db), 3)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='goals'").fetchone())
            self.assertEqual(db.execute('SELECT id FROM pages').fetchone()[0], 11)
            self.assertEqual(db.execute('SELECT evidence_quote FROM entities').fetchone()[0], self.text)
        finally:
            db.close()

    def test_missing_or_modified_bytes_fail_closed_without_schema_changes(self):
        (self.root / 'raw' / '2025-01-01' / 'source.txt').unlink()
        with self.assertRaises(RuntimeError):
            orch.Store.migrate(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(migration.schema_version(db), 3)
        (self.root / 'raw' / '2025-01-01' / 'source.txt').write_text(self.text, encoding='utf-8')
        result = orch.Store.migrate(self.db)
        bundle = Path(result['bundle'])
        original_db = (bundle / 'database.db').read_bytes()
        data = bytearray(original_db)
        data[0] ^= 0xFF
        (bundle / 'database.db').write_bytes(data)
        rejected = self.root / 'rejected-db'
        with self.assertRaises(RuntimeError):
            migration.restore_bundle(bundle, rejected / 'data' / 'restored.db', rejected)
        self.assertFalse(rejected.exists())
        (bundle / 'database.db').write_bytes(original_db)
        (bundle / 'files' / 'wiki' / 'entities' / 'historical.md').write_text('Corrupted wiki', encoding='utf-8')
        rejected_wiki = self.root / 'rejected-wiki'
        with self.assertRaises(RuntimeError):
            migration.restore_bundle(bundle, rejected_wiki / 'data' / 'restored.db', rejected_wiki)
        self.assertFalse(rejected_wiki.exists())
        self.assertIn('Human note: keep this edit.', (self.root / 'wiki' / 'entities' / 'historical.md').read_text(encoding='utf-8'))
        (bundle / 'files' / 'raw' / '2025-01-01' / 'source.txt').write_text('Corrupted copy', encoding='utf-8')
        target = self.root / 'rejected' / 'restored.db'
        with self.assertRaises(RuntimeError):
            migration.restore_bundle(bundle, target, target.parent)
        self.assertFalse(target.exists())

    def test_restore_refuses_existing_corpus_without_overwriting_human_edits(self):
        bundle = Path(orch.Store.migrate(self.db)['bundle'])
        target = self.root / 'live-corpus'
        (target / 'wiki' / 'entities').mkdir(parents=True)
        note = target / 'wiki' / 'entities' / 'historical.md'
        note.write_text('New human edit must survive.', encoding='utf-8')
        with self.assertRaises((ValueError, RuntimeError)):
            migration.restore_bundle(bundle, target / 'data' / 'restored.db', target)
        self.assertEqual(note.read_text(encoding='utf-8'), 'New human edit must survive.')
        self.assertFalse((target / 'data' / 'restored.db').exists())

    def test_restore_rejects_checksummed_database_with_relative_path_escape(self):
        bundle = Path(orch.Store.migrate(self.db)['bundle'])
        with contextlib.closing(sqlite3.connect(bundle / 'database.db')) as db:
            db.execute("UPDATE pages SET raw_path='../outside-source.txt'")
            db.commit()
        manifest_path = bundle / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['database_sha256'] = hashlib.sha256((bundle / 'database.db').read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        target = self.root / 'unsafe-restoration'
        with self.assertRaises((ValueError, RuntimeError)):
            migration.restore_bundle(bundle, target / 'data' / 'restored.db', target)
        self.assertFalse(target.exists())

    def test_manifest_lexical_escape_does_not_write_outside(self):
        bundle = Path(orch.Store.migrate(self.db)['bundle'])
        sentinel = self.root / 'sentinel.txt'
        sentinel.write_text('stay', encoding='utf-8')
        manifest_path = bundle / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files'].append({'path': '../sentinel.txt', 'sha256': 'abc'})
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        target = self.root / 'escaped'
        with self.assertRaises((ValueError, RuntimeError)):
            migration.restore_bundle(bundle, target / 'data' / 'restored.db', target)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'stay')
        self.assertFalse(target.exists())

    def test_reparse_and_ancestor_links_are_refused_before_resolve(self):
        outside = self.root / 'outside'
        secret = outside / 'secret.txt'
        secret.parent.mkdir()
        secret.write_text('do-not-touch', encoding='utf-8')
        linked = self.root / 'raw' / 'linked'
        _link_dir(outside, linked)
        self.assertTrue(migration.is_reparse(linked))
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE pages SET raw_path=?', (str(linked / 'secret.txt'),))
            db.commit()
        with self.assertRaises(RuntimeError):
            orch.Store.migrate(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(migration.schema_version(db), 3)
        self.assertEqual(secret.read_text(encoding='utf-8'), 'do-not-touch')
        self.assertFalse((self.root / 'data' / 'backup').exists())

        clean = self.root / 'clean'
        clean_db = legacy_fixture(clean, 3)

        def fail_resolve(*_args, **_kwargs):
            raise AssertionError('resolve')

        with patch.object(migration.Path, 'resolve', fail_resolve):
            with contextlib.closing(sqlite3.connect(clean_db, isolation_level=None)) as db:
                result = migration.migrate(db, clean, orch.DDL, orch.ORCH_DDL, _goal_ddl())
            bundle = Path(result['bundle'])
            real_parent = self.root / 'real-parent'
            real_parent.mkdir()
            link_parent = self.root / 'link-parent'
            _link_dir(real_parent, link_parent)
            target = link_parent / 'corpus'
            with self.assertRaises((ValueError, RuntimeError)):
                migration.restore_bundle(bundle, target / 'data' / 'restored.db', target)
            self.assertEqual(list(real_parent.iterdir()), [])
            live = self.root / 'live'
            note = live / 'wiki' / 'entities' / 'historical.md'
            note.parent.mkdir(parents=True)
            note.write_text('keep live', encoding='utf-8')
            live_link = self.root / 'live-link'
            _link_dir(live, live_link)
            with self.assertRaises((ValueError, RuntimeError)):
                migration.restore_bundle(bundle, live_link / 'data' / 'restored.db', live_link)
            self.assertEqual(note.read_text(encoding='utf-8'), 'keep live')
            raw_dir = bundle / 'files' / 'raw'
            moved = bundle / 'files' / 'raw-real'
            os.rename(raw_dir, moved)
            _link_dir(outside, raw_dir)
            escaped = self.root / 'bundle-link-escape'
            with self.assertRaises((ValueError, RuntimeError)):
                migration.restore_bundle(bundle, escaped / 'data' / 'restored.db', escaped)
            self.assertFalse(escaped.exists())
            self.assertEqual(secret.read_text(encoding='utf-8'), 'do-not-touch')

    def test_copy_failure_does_not_publish_partial_restored_corpus(self):
        bundle = Path(orch.Store.migrate(self.db)['bundle'])
        target = self.root / 'failed-restoration'
        copy = migration.shutil.copy2

        def copy_until_wiki(source, destination, *args, **kwargs):
            if Path(source).suffix == '.md':
                raise OSError('Synthetic disk failure after source copy.')
            return copy(source, destination, *args, **kwargs)

        with patch.object(migration.shutil, 'copy2', side_effect=copy_until_wiki):
            with self.assertRaises(OSError):
                migration.restore_bundle(bundle, target / 'data' / 'restored.db', target)
        self.assertFalse(target.exists())
        self.assertEqual((self.root / 'raw' / '2025-01-01' / 'source.txt').read_text(encoding='utf-8'), self.text)
        migration.verify_bundle(bundle)


if __name__ == '__main__':
    unittest.main()
