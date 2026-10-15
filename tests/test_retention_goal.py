"""Retention pins full historical paths and backs up the whole corpus."""
from __future__ import annotations

import contextlib
import datetime as dt
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


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'snowball_{name}', ROOT / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


migration = _load('migration')
retention = _load('retention')

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
WIKI = """# Note

Human note: keep this edit.

## Facts

<!-- snowball:facts:start -->
Code-owned fact: synthetic tuition note.
<!-- snowball:facts:end -->

## Notes

Human region stays verbatim.
"""
HISTORICAL = 'Historical contract version one keeps this sentence.'
CURRENT = 'Current contract version two keeps this sentence.'
PINNED_SAME = 'Pinned full path, not the basename.'
QUOTED = 'Quoted sentence for the claim.'
HUMAN_RAW = 'Human note inside raw.'


def _link_dir(src: Path, dst: Path) -> None:
    src.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=True)
    except OSError:
        subprocess.check_call(['cmd', '/c', 'mklink', '/J', str(dst), str(src)])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def build_corpus(root: Path) -> Path:
    historical = root / 'raw' / '2020-01-01' / 'historical.txt'
    original = root / 'raw' / '2020-01-01' / 'historical.bin'
    current = root / 'raw' / '2024-01-01' / 'current.txt'
    pinned_same = root / 'raw' / '2020-01-01' / 'same.txt'
    loose = root / 'raw' / '2020-01-01' / 'loose.txt'
    human_raw = root / 'raw' / '2020-01-01' / 'human-note.md'
    collision = root / 'raw' / '2019-01-01' / 'same.txt'
    quoted = root / 'raw' / '2018-06-01' / 'quoted.txt'
    wiki = root / 'wiki' / 'entities' / 'note.md'
    loose_old = root / 'raw' / 'loose-old.txt'
    loose_new = root / 'raw' / 'loose-new.txt'
    for path, text in (
        (historical, HISTORICAL),
        (original, 'original-bytes'),
        (current, CURRENT),
        (pinned_same, PINNED_SAME),
        (loose, 'loose unreferenced'),
        (human_raw, HUMAN_RAW),
        (collision, 'basename collision'),
        (quoted, QUOTED),
        (wiki, WIKI),
        (loose_old, 'old loose'),
        (loose_new, 'new loose'),
    ):
        _write(path, text)
    old_ts = (NOW - dt.timedelta(days=40)).timestamp()
    os.utime(loose_old, (old_ts, old_ts))
    db_path = root / 'data' / 'corpus.db'
    db_path.parent.mkdir(parents=True)
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE pages (id INTEGER PRIMARY KEY, raw_path TEXT);
        CREATE TABLE source_snapshots (
          id INTEGER PRIMARY KEY, page_id INTEGER, text_path TEXT NOT NULL, original_raw_path TEXT);
        CREATE TABLE observations (
          id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, page_id INTEGER NOT NULL,
          contract_version INTEGER, quote TEXT NOT NULL, quote_start INTEGER NOT NULL, quote_end INTEGER NOT NULL);
        CREATE TABLE funding_packages (
          key TEXT PRIMARY KEY, basis_observation_ids_json TEXT, compatibility_basis_ids_json TEXT,
          components_json TEXT);
        CREATE TABLE constraint_evaluations (
          goal_id INTEGER, contract_version INTEGER, package_key TEXT, basis_json TEXT);
        CREATE TABLE contradictions (id INTEGER PRIMARY KEY, observation_a INTEGER, observation_b INTEGER);
        CREATE TABLE model_calls (id INTEGER PRIMARY KEY, source_snapshot_ids_json TEXT);
        CREATE TABLE claim_sources (claim_hash TEXT, page_id INTEGER, quote_start INTEGER, quote_end INTEGER);
        CREATE TABLE entities (key TEXT PRIMARY KEY, evidence_page_id INTEGER, wiki_path TEXT);
        CREATE TABLE wiki_regions (path TEXT, region_key TEXT, provenance TEXT, body TEXT);
        """
    )
    db.execute('INSERT INTO pages VALUES (1, ?)', (str(historical),))
    db.execute('INSERT INTO pages VALUES (2, ?)', ('raw/2024-01-01/current.txt',))
    db.execute('INSERT INTO pages VALUES (3, ?)', ('raw/2020-01-01/same.txt',))
    db.execute('INSERT INTO pages VALUES (7, ?)', ('raw/2018-06-01/quoted.txt',))
    db.execute(
        'INSERT INTO source_snapshots VALUES (1, 1, ?, ?)',
        (str(historical), 'raw/2020-01-01/historical.bin'),
    )
    db.execute('INSERT INTO source_snapshots VALUES (2, 2, ?, NULL)', ('raw/2024-01-01/current.txt',))
    db.execute('INSERT INTO source_snapshots VALUES (3, 3, ?, NULL)', ('raw/2020-01-01/same.txt',))
    db.execute(
        'INSERT INTO observations VALUES (1, 1, 1, 1, ?, 0, ?)',
        (HISTORICAL, len(HISTORICAL)),
    )
    db.execute(
        'INSERT INTO observations VALUES (2, 2, 2, 2, ?, 0, ?)',
        (CURRENT, len(CURRENT)),
    )
    db.execute(
        "INSERT INTO funding_packages VALUES ('pkg', ?, '[]', ?)",
        (json.dumps([1]), json.dumps({'backing_ids': [1, 2]})),
    )
    db.execute(
        "INSERT INTO constraint_evaluations VALUES (1, 1, 'pkg', ?)",
        (json.dumps({'observation_ids': [1], 'snapshot_ids': [1, 3], 'profile_keys': []}),),
    )
    db.execute('INSERT INTO contradictions VALUES (1, 1, 2)')
    db.execute('INSERT INTO model_calls VALUES (1, ?)', (json.dumps([1, 2]),))
    db.execute('INSERT INTO claim_sources VALUES (?, 7, 0, ?)', ('claim', len(QUOTED)))
    db.execute(
        "INSERT INTO entities VALUES ('historical', 7, 'wiki/entities/note.md')"
    )
    db.execute(
        "INSERT INTO wiki_regions VALUES ('raw/2020-01-01/human-note.md', 'note', 'human_note', ?)",
        (HUMAN_RAW,),
    )
    db.commit()
    db.close()
    return db_path


class GoalRetentionTests(unittest.TestCase):
    def test_pins_exact_paths_across_contract_versions_and_restores_bundle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db_path = build_corpus(root)
        conn = sqlite3.connect(db_path)
        self.addCleanup(conn.close)
        stats = retention.run_retention(conn, root_dir=root, now_dt=NOW)
        self.assertFalse((root / 'raw' / '2019-01-01').exists())
        self.assertFalse((root / 'raw' / '2020-01-01' / 'loose.txt').exists())
        self.assertFalse((root / 'raw' / 'loose-old.txt').exists())
        self.assertEqual((root / 'raw' / 'loose-new.txt').read_text(encoding='utf-8'), 'new loose')
        self.assertTrue((root / 'raw' / '2020-01-01').is_dir())
        self.assertEqual((root / 'raw' / '2020-01-01' / 'historical.txt').read_text(encoding='utf-8'), HISTORICAL)
        self.assertEqual((root / 'raw' / '2020-01-01' / 'historical.bin').read_text(encoding='utf-8'), 'original-bytes')
        self.assertEqual((root / 'raw' / '2020-01-01' / 'same.txt').read_text(encoding='utf-8'), PINNED_SAME)
        self.assertEqual((root / 'raw' / '2020-01-01' / 'human-note.md').read_text(encoding='utf-8'), HUMAN_RAW)
        self.assertEqual((root / 'raw' / '2024-01-01' / 'current.txt').read_text(encoding='utf-8'), CURRENT)
        self.assertEqual((root / 'raw' / '2018-06-01' / 'quoted.txt').read_text(encoding='utf-8'), QUOTED)
        wiki = (root / 'wiki' / 'entities' / 'note.md').read_text(encoding='utf-8')
        self.assertIn('Human note: keep this edit.', wiki)
        self.assertIn('Code-owned fact: synthetic tuition note.', wiki)
        self.assertIn('Human region stays verbatim.', wiki)
        bundle = Path(stats['backup_path'])
        self.assertTrue(bundle.is_dir())
        self.assertTrue((bundle / 'manifest.json').is_file())
        self.assertTrue((bundle / 'database.db').is_file())
        manifest = migration.verify_bundle(bundle)
        bundled = {entry['path'] for entry in manifest['files']}
        self.assertIn('raw/2020-01-01/historical.txt', bundled)
        self.assertIn('raw/2020-01-01/historical.bin', bundled)
        self.assertIn('raw/2020-01-01/same.txt', bundled)
        self.assertIn('raw/2024-01-01/current.txt', bundled)
        self.assertIn('raw/2018-06-01/quoted.txt', bundled)
        self.assertIn('raw/2020-01-01/human-note.md', bundled)
        self.assertIn('wiki/entities/note.md', bundled)
        self.assertNotIn('raw/2019-01-01/same.txt', bundled)
        self.assertNotIn('raw/2020-01-01/loose.txt', bundled)
        self.assertNotIn('raw/loose-old.txt', bundled)
        self.assertEqual(stats['quick_check'], 'ok')
        self.assertFalse(hasattr(retention, 'restore_backup'))
        restored_root = root / 'isolated'
        restored = migration.restore_bundle(bundle, restored_root / 'data' / 'corpus.db', restored_root)
        self.assertEqual(restored['quick_check'], 'ok')
        self.assertEqual(
            (restored_root / 'raw' / '2020-01-01' / 'historical.txt').read_text(encoding='utf-8'),
            HISTORICAL,
        )
        self.assertEqual(
            (restored_root / 'raw' / '2020-01-01' / 'historical.bin').read_text(encoding='utf-8'),
            'original-bytes',
        )
        restored_wiki = (restored_root / 'wiki' / 'entities' / 'note.md').read_text(encoding='utf-8')
        self.assertIn('Code-owned fact: synthetic tuition note.', restored_wiki)
        self.assertIn('Human region stays verbatim.', restored_wiki)
        with contextlib.closing(sqlite3.connect(restored_root / 'data' / 'corpus.db')) as db:
            raw = Path(db.execute('SELECT raw_path FROM pages WHERE id=7').fetchone()[0])
            self.assertTrue(raw.is_relative_to(restored_root))
            start, end = db.execute('SELECT quote_start,quote_end FROM claim_sources').fetchone()
            self.assertEqual(raw.read_text(encoding='utf-8')[start:end], QUOTED)
            historical = Path(db.execute('SELECT text_path FROM source_snapshots WHERE id=1').fetchone()[0])
            quote_end = db.execute('SELECT quote_end FROM observations WHERE contract_version=1').fetchone()[0]
            self.assertEqual(historical.read_text(encoding='utf-8')[:quote_end], HISTORICAL)
            current = Path(db.execute('SELECT text_path FROM source_snapshots WHERE id=2').fetchone()[0])
            self.assertEqual(current.read_text(encoding='utf-8'), CURRENT)

    def test_pinned_logs_and_quarantine_survive_and_backup_failure_does_not_prune(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db_path = build_corpus(root)
        pinned_log = root / 'logs' / 'pinned.log'
        pinned_quarantine = root / 'quarantine' / 'pinned.bin'
        _write(pinned_log, 'keep-log')
        _write(pinned_quarantine, 'keep-quarantine')
        old_ts = (NOW - dt.timedelta(days=40)).timestamp()
        os.utime(pinned_log, (old_ts, old_ts))
        os.utime(pinned_quarantine, (old_ts, old_ts))
        conn = sqlite3.connect(db_path)
        try:
            conn.execute('INSERT INTO pages VALUES (9, ?)', ('logs/pinned.log',))
            conn.execute('INSERT INTO pages VALUES (10, ?)', ('quarantine/pinned.bin',))
            conn.commit()
            with patch.object(migration, 'create_bundle', side_effect=RuntimeError('disk')):
                with self.assertRaises(RuntimeError):
                    retention.run_retention(conn, root_dir=root, now_dt=NOW)
            self.assertTrue((root / 'raw' / 'loose-old.txt').is_file())
            self.assertTrue((root / 'raw' / '2019-01-01' / 'same.txt').is_file())
            self.assertEqual(pinned_log.read_text(encoding='utf-8'), 'keep-log')
            self.assertEqual(pinned_quarantine.read_text(encoding='utf-8'), 'keep-quarantine')
            self.assertEqual(
                (root / 'raw' / '2020-01-01' / 'historical.txt').read_text(encoding='utf-8'),
                HISTORICAL,
            )
            stats = retention.run_retention(conn, root_dir=root, now_dt=NOW)
            self.assertEqual(pinned_log.read_text(encoding='utf-8'), 'keep-log')
            self.assertEqual(pinned_quarantine.read_text(encoding='utf-8'), 'keep-quarantine')
            self.assertFalse((root / 'raw' / 'loose-old.txt').exists())
            bundled = {entry['path'] for entry in migration.verify_bundle(Path(stats['backup_path']))['files']}
            self.assertIn('logs/pinned.log', bundled)
            self.assertIn('quarantine/pinned.bin', bundled)
        finally:
            conn.close()

    def test_missing_pin_and_reparse_leave_the_corpus_untouched(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db_path = build_corpus(root)
        historical = root / 'raw' / '2020-01-01' / 'historical.txt'
        historical.unlink()
        conn = sqlite3.connect(db_path)
        try:
            with self.assertRaises(RuntimeError):
                retention.run_retention(conn, root_dir=root, now_dt=NOW)
            self.assertTrue((root / 'raw' / 'loose-old.txt').is_file())
            self.assertTrue((root / 'raw' / '2019-01-01' / 'same.txt').is_file())
            self.assertTrue((root / 'raw' / '2020-01-01' / 'loose.txt').is_file())
            self.assertIn('Human note: keep this edit.', (root / 'wiki' / 'entities' / 'note.md').read_text(encoding='utf-8'))
        finally:
            conn.close()

        root2 = root / 'reparse'
        db2 = build_corpus(root2)
        outside = root2 / 'outside'
        secret = outside / 'secret.txt'
        _write(secret, 'do-not-touch')
        _link_dir(outside, root2 / 'raw' / '1999-01-01')
        self.assertTrue(migration.is_reparse(root2 / 'raw' / '1999-01-01'))
        conn2 = sqlite3.connect(db2)
        try:
            with self.assertRaises(RuntimeError):
                retention.run_retention(conn2, root_dir=root2, now_dt=NOW)
            self.assertEqual(secret.read_text(encoding='utf-8'), 'do-not-touch')
            self.assertTrue((root2 / 'raw' / 'loose-old.txt').is_file())
            self.assertTrue((root2 / 'raw' / '2019-01-01' / 'same.txt').is_file())
            self.assertTrue((root2 / 'wiki' / 'entities' / 'note.md').is_file())
        finally:
            conn2.close()

    def test_bundle_trim_keeps_seven_newest(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        backup = Path(tmp.name)
        for index in range(1, 9):
            (backup / f'goal-v3-{1_000_000_000 + index:010d}-{index:08x}').mkdir()
        kept, pruned = retention._trim_bundles(backup, keep=7)
        self.assertEqual((kept, pruned), (7, 1))
        names = sorted(path.name for path in backup.iterdir())
        self.assertEqual(len(names), 7)
        self.assertNotIn('goal-v3-1000000001-00000001', names)
        self.assertIn('goal-v3-1000000008-00000008', names)


if __name__ == '__main__':
    unittest.main()
