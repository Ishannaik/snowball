"""Generic migration: verified backup, restore, retention, and schema upgrade.

Covers: populated legacy rows (id and raw/wiki bytes preserved), backup tamper
and path traversal and symlink refusal, no autoapproved goals after migration,
complete rollback of an isolated corpus, and an old-schema generic upgrade
backup. All paths are synthetic fixtures under TemporaryDirectory.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


# tests may run with repo root on sys.path (real 'tools' namespace) or alone;
# guarantee a 'tools' package object either way so package-relative imports work.
if "tools" in sys.modules:
    tools_mod = sys.modules["tools"]
else:
    tools_mod = types.ModuleType("tools")
    tools_mod.__path__ = [str(ROOT / "tools")]
    sys.modules["tools"] = tools_mod


def _load_under_tools(name: str):
    """Load tools/<name>.py exactly as 'tools.<name>' and register it once."""
    if f"tools.{name}" in sys.modules:
        return sys.modules[f"tools.{name}"]
    spec = importlib.util.spec_from_file_location(f"tools.{name}", ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"tools.{name}"] = module
    setattr(tools_mod, name, module)
    spec.loader.exec_module(module)
    return module


# Single copy of the migration module: the package member that
# ` _load_store_module()` inside generic_migration itself resolves.
gmig = _load_under_tools("generic_migration")
store_mod = _load_under_tools("generic_store")


LEGACY_DDL = """
CREATE TABLE pages (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  raw_path TEXT
);
CREATE TABLE source_snapshots (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  text_path TEXT,
  original_raw_path TEXT
);
CREATE TABLE entities (
  key TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  wiki_path TEXT
);
CREATE TABLE wiki_pages (
  path TEXT PRIMARY KEY,
  body TEXT NOT NULL
);
CREATE TABLE goals (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  position INTEGER
);
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO schema_meta VALUES('schema_version', '3');
"""

# Historic OLD generic schema, captured verbatim as a data fixture: all seven
# tables existed pre-upgrade with fewer columns and no generic_schema marker.
# Do NOT substitute tools.generic_store.DDL here — once the storage owner
# lands the new schema that import would silently stop testing the upgrade.
OLD_GENERIC_DDL = """
CREATE TABLE generic_goals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  state TEXT NOT NULL,
  row_version INTEGER NOT NULL
);
CREATE TABLE generic_versions (
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  public_id TEXT NOT NULL,
  public_hash TEXT NOT NULL,
  body_json TEXT NOT NULL,
  PRIMARY KEY (goal_id, body_hash)
);
CREATE TABLE generic_grants (
  id INTEGER PRIMARY KEY,
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  limits_json TEXT NOT NULL
);
CREATE TABLE generic_snapshots (
  source_id TEXT NOT NULL,
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  PRIMARY KEY (goal_id, source_id)
);
CREATE TABLE generic_claims (
  id TEXT NOT NULL,
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  quote_hash TEXT NOT NULL,
  body_json TEXT NOT NULL,
  PRIMARY KEY (goal_id, id)
);
CREATE TABLE generic_judgments (
  id INTEGER PRIMARY KEY,
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  outcome TEXT NOT NULL
);
CREATE TABLE generic_events (
  id INTEGER PRIMARY KEY,
  goal_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  body_json TEXT NOT NULL
);
"""


class BaseFixture(unittest.TestCase):
    """Storage root with a populated legacy corpus (DB + bytes + wiki)."""

    def make_corpus(self) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp(prefix="gmig-root-"))
        self.addCleanup(_cleanup_tree, root)
        data = root / "data"
        data.mkdir()
        db_path = data / "goal.db"
        pages_dir = root / "pages"
        pages_dir.mkdir()
        (pages_dir / "raw-1.html").write_bytes(b"<html>public page one</html>\n")
        (pages_dir / "raw-2.html").write_bytes(b"<html>page two bytes</html>\n")
        (pages_dir / "text-1.txt").write_bytes(b"extracted text one\n")
        wiki = root / "wiki"
        wiki.mkdir()
        (wiki / "Entity").mkdir()
        (wiki / "Entity" / "Alpha.md").write_bytes(b"# Alpha\nwiki bytes\n")
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            conn.executescript(LEGACY_DDL)
            conn.execute("INSERT INTO pages(id,title,raw_path) VALUES(101,'Page One',?)", (os.path.join("pages", "raw-1.html"),))
            conn.execute("INSERT INTO pages(id,title,raw_path) VALUES(102,'Page Two',?)", (os.path.join("pages", "raw-2.html"),))
            conn.execute("INSERT INTO source_snapshots(id,title,text_path,original_raw_path) VALUES(500,'Snap One',?,?)",
                         (os.path.join("pages", "text-1.txt"), "pages/raw-1.html"))
            conn.execute("INSERT INTO entities(key,label,wiki_path) VALUES('alpha','Alpha',?)",
                         (os.path.join("wiki", "Entity", "Alpha.md"),))
            conn.execute("INSERT INTO entities(key,label,wiki_path) VALUES('beta','Beta','Entity/Beta.md')")
            conn.execute("INSERT INTO wiki_pages(path,body) VALUES(?,?)", (os.path.join("wiki", "Entity", "Alpha.md"), "# Alpha\n"))
            conn.execute("INSERT INTO goals(id,title,position) VALUES(7,'Legacy Goal 7',1)")
            conn.commit()
        return root, db_path

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


def _cleanup_tree(path: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(path, ignore_errors=True)


class ManifestScriptTest(BaseFixture):
    def _bundle(self, root: Path, db_path: Path, destination: Path) -> Path:
        result = gmig.backup(db_path, root, destination)
        self.assertTrue(result["quick_check"] == "ok")
        return Path(result["bundle"])

    def test_backup_bundle_contains_real_bytes(self):
        root, db_path = self.make_corpus()
        destination = root / "backups"
        bundle = self._bundle(root, db_path, destination)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], gmig.MANIFEST_VERSION)
        self.assertEqual(manifest["database_name"], "goal.db")
        self.assertEqual(manifest["schema_version"], 3)
        db_sha = self.digest(bundle / "database.db")
        self.assertEqual(db_sha, manifest["database_sha256"])
        for entry in manifest["files"]:
            relative = gmig.relative_reference(entry["path"])
            landed = bundle / "files" / relative
            self.assertTrue(landed.is_file())
            self.assertEqual(self.digest(landed), entry["sha256"])
        sizes = {Path(entry["path"]).name: entry["sha256"] for entry in manifest["files"]}
        self.assertEqual(sizes["raw-1.html"], self.digest(root / "pages" / "raw-1.html"))
        self.assertEqual(sizes["Alpha.md"], self.digest(root / "wiki" / "Entity" / "Alpha.md"))

    def test_backup_preserves_legacy_db_rows_and_user_version(self):
        root, db_path = self.make_corpus()
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA user_version=9")
        bundle = self._bundle(root, db_path, root / "backups2")
        with contextlib.closing(sqlite3.connect(bundle / "database.db")) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(conn.execute("SELECT count(*) FROM goals").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT title FROM goals WHERE id=7").fetchone()[0], "Legacy Goal 7")
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "3")

    def test_backup_missing_reference_bytes_refused(self):
        root, db_path = self.make_corpus()
        (root / "pages" / "raw-1.html").unlink()
        with self.assertRaises(RuntimeError):
            gmig.backup(db_path, root, root / "nope")

    def test_backup_tampered_bundle_fails_verification(self):
        root, db_path = self.make_corpus()
        bundle = self._bundle(root, db_path, root / "backups3")
        target = bundle / "files" / "pages" / "raw-1.html"
        target.write_bytes(b"tampered\n")
        with self.assertRaises(RuntimeError) as caught:
            gmig.restore(bundle, root / "restored-tamper")
        self.assertIn("checksum", str(caught.exception))

    def test_backup_tampered_database_fails_verification(self):
        root, db_path = self.make_corpus()
        bundle = self._bundle(root, db_path, root / "backups4")
        database = bundle / "database.db"
        payload = bytearray(database.read_bytes())
        payload[-8:] = b"\x00" * 8
        database.write_bytes(bytes(payload))
        with self.assertRaises(RuntimeError):
            gmig.restore(bundle, root / "restored-tamperdb")

    def test_manifest_traversal_reference_refused(self):
        root, db_path = self.make_corpus()
        bundle = self._bundle(root, db_path, root / "backups5")
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append({"path": "../escape.html", "sha256": "0" * 64})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            gmig.restore(bundle, root / "restored-escape")
        self.assertFalse((root / "restored-escape").exists())
        self.assertEqual(sorted(p.name for p in root.iterdir() if p.name.startswith("pages") or p.name == "wiki"),
                         ["pages", "wiki"])

    def test_manifest_absolute_reference_refused(self):
        root, db_path = self.make_corpus()
        bundle = self._bundle(root, db_path, root / "backups6")
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append({"path": str(Path(root.anchor) / "etc" / "passwd"), "sha256": "0" * 64})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            gmig.restore(bundle, root / "restored-absolute")

    def test_manifest_network_drive_reference_refused(self):
        root, db_path = self.make_corpus()
        bundle = self._bundle(root, db_path, root / "backups7")
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append({"path": "\\\\server\\share\\x.md", "sha256": "0" * 64})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            gmig.restore(bundle, root / "restored-network")

    @unittest.skipUnless(os.name == "posix", "symlink semantics require POSIX")
    def test_backup_symlinked_reference_refused(self):
        root, db_path = self.make_corpus()
        link = root / "pages" / "raw-2.html"
        link.unlink()
        link.symlink_to(root / "pages" / "raw-1.html")
        with self.assertRaises(RuntimeError) as caught:
            gmig.backup(db_path, root, root / "nope2")
        self.assertIn("reparse", str(caught.exception).lower())

    @unittest.skipUnless(os.name == "posix", "symlink semantics require POSIX")
    def test_restore_rejects_bundle_files_symlink(self):
        root, db_path = self.make_corpus()
        bundle = self._bundle(root, db_path, root / "backups8")
        files = bundle / "files"
        shutil.rmtree(files)
        os.symlink(str(root / "pages"), str(files))
        with self.assertRaises(RuntimeError):
            gmig.restore(bundle, root / "restored-symlink")

    def test_restore_isolated_root_full_corpus(self):
        root, db_path = self.make_corpus()
        destination = root.parent / f"gmig-dst-{int(time.time()*1000)}"
        self.addCleanup(_cleanup_tree, destination)
        bundle = self._bundle(root, db_path, root / "backups9")
        result = gmig.restore(bundle, destination)
        self.assertEqual(result["quick_check"], "ok")
        self.assertEqual(result["files_restored"], 4)
        restored_db = destination / "goal.db"
        self.assertTrue(restored_db.is_file())
        # Backup-API snapshot: logically identical, not promised byte-identical.
        with contextlib.closing(sqlite3.connect(restored_db)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "3")
            # Evidence paths are filesystem-backed: restore remaps them to the
            # new absolute destination root; the loop below checks both rows.
            raw_101 = conn.execute("SELECT raw_path FROM pages WHERE id=101").fetchone()[0]
            self.assertEqual(Path(os.path.abspath(raw_101)), destination / "pages" / "raw-1.html")
        self.assertEqual((destination / "pages" / "raw-1.html").read_bytes(), b"<html>public page one</html>\n")
        self.assertEqual((destination / "wiki" / "Entity" / "Alpha.md").read_bytes(), b"# Alpha\nwiki bytes\n")
        with contextlib.closing(sqlite3.connect(restored_db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM goals").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT title FROM goals WHERE id=7").fetchone()[0], "Legacy Goal 7")
            raw_paths = {row[0] for row in conn.execute("SELECT raw_path FROM pages WHERE raw_path IS NOT NULL")}
            self.assertEqual(len(raw_paths), 2)
            for raw_path in raw_paths:
                resolved = Path(os.path.abspath(raw_path))
                self.assertEqual(resolved.parent, destination / "pages")
                self.assertEqual((destination / "pages" / resolved.name).read_bytes(), db_path_read_bytes(root, resolved.name))
            # Real filesystem-backed path column: remapped into the new root.
            alpha_path = conn.execute("SELECT wiki_path FROM entities WHERE key='alpha'").fetchone()[0]
            self.assertEqual(Path(os.path.abspath(alpha_path)), destination / "wiki" / "Entity" / "Alpha.md")
            # Logical wiki-relative identifier without bundle bytes: preserved verbatim.
            beta_path = conn.execute("SELECT wiki_path FROM entities WHERE key='beta'").fetchone()[0]
            self.assertEqual(beta_path, "Entity/Beta.md")

    def test_restore_nonempty_destination_refused(self):
        root, db_path = self.make_corpus()
        destination = root.parent / f"gmig-nempty-{int(time.time()*1000)}"
        self.addCleanup(_cleanup_tree, destination)
        destination.mkdir()
        (destination / "corpus.db").write_bytes(b"existing\n")
        bundle = self._bundle(root, db_path, root / "backups10")
        with self.assertRaises(ValueError):
            gmig.restore(bundle, destination)
        self.assertTrue((destination / "corpus.db").is_file())

    def test_restore_nesting_with_bundle_refused(self):
        root, db_path = self.make_corpus()
        bundle = self._bundle(root, db_path, root / "backups11")
        with self.assertRaises(RuntimeError):
            gmig.restore(bundle, bundle / "nested")

    def test_restore_twice_second_refused(self):
        root, db_path = self.make_corpus()
        bundle = self._bundle(root, db_path, root / "backups12")
        first = root.parent / f"gmig-twice-{int(time.time()*1000)}"
        self.addCleanup(_cleanup_tree, first)
        gmig.restore(bundle, first)
        with self.assertRaises(ValueError):
            gmig.restore(bundle, first)


class RetainTest(BaseFixture):
    def _bundle(self, root: Path, db_path: Path) -> Path:
        result = gmig.backup(db_path, root, root / "data" / "backup")
        return Path(result["bundle"])

    def test_retention_keeps_newest_owned_bundles(self):
        root, db_path = self.make_corpus()
        for _ in range(4):
            time.sleep(0.01)
            self._bundle(root, db_path)
        out = gmig.retain(root, keep=2)
        self.assertEqual(len(out["kept"]), 2)
        self.assertEqual(len(out["deleted"]), 2)
        live = [p for p in (root / "data" / "backup").iterdir() if p.is_dir()]
        self.assertEqual(len(live), 2)

    def test_retention_never_touches_sources_or_foreign_names(self):
        root, db_path = self.make_corpus()
        legacy_backup = root / "data" / "backup" / "goal-v3-1700000000-deadbeef"
        legacy_backup.mkdir(parents=True)
        (legacy_backup / "keep.txt").write_text("legacy bundle stays\n", encoding="utf-8")
        data = root / "data"
        (data / "history.db").write_bytes(b"history\n")
        for _ in range(3):
            time.sleep(0.01)
            self._bundle(root, db_path)
        out = gmig.retain(root, keep=1)
        self.assertEqual(len(out["deleted"]), 2)
        self.assertTrue(legacy_backup.is_dir())
        self.assertTrue((data / "history.db").is_file())
        self.assertEqual(len([p for p in (data / "backup").iterdir() if p.is_dir()]), 2)  # legacy + 1 kept

    def test_retention_keep_zero_refused(self):
        root, db_path = self.make_corpus()
        self._bundle(root, db_path)
        with self.assertRaises(ValueError):
            gmig.retain(root, keep=0)
        self.assertEqual(len([p for p in (root / "data" / "backup").iterdir() if p.is_dir()]), 1)

    def test_retention_empty_root_ok(self):
        root, db_path = self.make_corpus()
        out = gmig.retain(root, keep=7)
        self.assertEqual(out["deleted"], [])


class MigrationTest(BaseFixture):
    """Exercises the real tools.generic_store.initialize_schema storage export.

    initialize_schema is imported lazily by generic_migration at run time, so
    these tests bind whatever the storage owner shipped when they execute —
    no schema-echo stubs. Old-schema fixtures use the explicit historic DDL
    snapshot, never the live store module.
    """

    def test_migrate_creates_empty_generic_tables_over_legacy(self):
        root, db_path = self.make_corpus()
        result = call_migrate(db_path, root)
        self.assertTrue(result["changed"])
        self.assertTrue(result["backup"]["bundle"])
        self.assertEqual(result["user_version"], 0)
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            for table in gmig.GENERIC_TABLES:
                self.assertTrue(table_exists(conn, table), table)
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM goals").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT title FROM goals WHERE id=7").fetchone()[0], "Legacy Goal 7")
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "3")
        bundle = result["backup"]["bundle"]
        with contextlib.closing(sqlite3.connect(Path(bundle) / "database.db")) as conn:
            self.assertFalse(table_exists(conn, "generic_goals"))

    def test_migrate_never_autoapproves_or_starts(self):
        root, db_path = self.make_corpus()
        call_migrate(db_path, root)
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM generic_goals").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM generic_versions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM generic_grants").fetchone()[0], 0)
            states = {row[0] for row in conn.execute("SELECT state FROM generic_goals")}
            self.assertFalse(states & {"approved", "granted", "running"})

    def test_migrate_without_root_refused(self):
        root, db_path = self.make_corpus()
        with self.assertRaises(ValueError):
            gmig.migrate(db_path)

    def test_migrate_missing_db_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                gmig.migrate(Path(directory) / "absent.db", Path(directory))

    def test_migrate_missing_reference_bytes_refused_without_tables(self):
        root, db_path = self.make_corpus()
        (root / "pages" / "raw-1.html").unlink()
        with self.assertRaises(RuntimeError):
            call_migrate(db_path, root)
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            self.assertFalse(table_exists(conn, "generic_goals"))

    def test_migrate_twice_idempotent_no_new_tables(self):
        root, db_path = self.make_corpus()
        first = call_migrate(db_path, root)
        self.assertTrue(first["changed"])
        second = gmig.migrate(db_path, root)
        self.assertFalse(second["changed"])
        self.assertIsNone(second["backup"])

    def test_migrate_user_version_not_clobbered(self):
        root, db_path = self.make_corpus()
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA user_version=11")
        result = call_migrate(db_path, root)
        self.assertEqual(result["user_version"], 11)

    def test_old_generic_db_without_marker_is_backed_up_and_upgraded(self):
        """Old generic DB: tables exist but no generic_schema marker.

        Table-name-only no-op detection would silently skip a needed upgrade;
        migrate must treat it as an upgrade candidate, back up, and initialize.
        """
        root, db_path = self.make_corpus()
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            # Historic OLD generic schema (data fixture): tables but no marker.
            conn.executescript(OLD_GENERIC_DDL)
            conn.execute("INSERT INTO generic_goals(id,title,state,row_version) VALUES('g-old','Historic Goal','draft',2)")
            conn.commit()
        result = call_migrate(db_path, root)
        self.assertTrue(result["changed"])
        self.assertIsNotNone(result["backup"])
        # Snapshot keeps the pre-upgrade world: old generic tables, no marker.
        with contextlib.closing(sqlite3.connect(Path(result["backup"]["bundle"]) / "database.db")) as conn:
            self.assertTrue(table_exists(conn, "generic_goals"))
            self.assertEqual(conn.execute("SELECT title FROM generic_goals WHERE id='g-old'").fetchone()[0], "Historic Goal")
            self.assertIsNone(conn.execute(
                "SELECT value FROM schema_meta WHERE key='generic_schema'").fetchone())
        # Live DB is upgraded: marker present, every generic table complete.
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT value FROM schema_meta WHERE key='generic_schema'").fetchone()[0],
                gmig.GENERIC_SCHEMA_VERSION)
            for table in gmig.GENERIC_TABLES:
                self.assertTrue(table_exists(conn, table), table)
            self.assertEqual(conn.execute("SELECT count(*) FROM goals").fetchone()[0], 1)

    def test_current_generic_marker_noop(self):
        """Exact current marker: initialize_schema is never invoked again."""
        root, db_path = self.make_corpus()
        real_initialize = store_mod.initialize_schema  # loud AttributeError until store lands
        calls: list[sqlite3.Connection] = []

        def recording(conn: sqlite3.Connection) -> None:
            calls.append(conn)
            real_initialize(conn)

        with mock.patch.object(store_mod, "initialize_schema", new=recording):
            call_migrate(db_path, root)
            self.assertEqual(len(calls), 1)  # first run upgrades through the real export
            calls.clear()
            second = gmig.migrate(db_path, root)
        self.assertFalse(second["changed"])
        self.assertIsNone(second["backup"])
        self.assertEqual(calls, [])

    def test_generic_snapshot_payload_survives_roundtrip_without_external_files(self):
        """Generic snapshot rows are inline DB payloads: the DB backup retains
        them inherently; path collection must not demand external files for them."""
        root, db_path = self.make_corpus()
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            conn.executescript(OLD_GENERIC_DDL)
            conn.execute("INSERT INTO generic_goals(id,title,state,row_version) VALUES('g-1','Old Generic Goal','draft',3)")
            conn.execute(
                "INSERT INTO generic_snapshots(source_id,goal_id,body_hash,text,snapshot_hash)"
                " VALUES(?,?,?,?,?)",
                ("s-9", "g-1", "ab" * 32, "inline generic snapshot payload", "cd" * 32),
            )
            conn.commit()
        backup_info = gmig.backup(db_path, root, root / "bk")
        # Only the four legacy evidence/wiki files were chased; nothing for generic rows.
        manifest = json.loads((Path(backup_info["bundle"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["files"]), 4)
        destination = root.parent / f"gmig-snap-{int(time.time()*1000)}"
        self.addCleanup(_cleanup_tree, destination)
        result = gmig.restore(Path(backup_info["bundle"]), destination)
        self.assertEqual(result["quick_check"], "ok")
        with contextlib.closing(sqlite3.connect(destination / "goal.db")) as conn:
            row = conn.execute(
                "SELECT source_id, text, snapshot_hash FROM generic_snapshots WHERE goal_id='g-1'"
            ).fetchone()
            self.assertEqual(row[0], "s-9")
            self.assertEqual(row[1], "inline generic snapshot payload")
            self.assertEqual(row[2], "cd" * 32)
            goal = conn.execute("SELECT state, row_version FROM generic_goals WHERE id='g-1'").fetchone()
            self.assertEqual(goal[0], "draft")  # migrated data is never re-approved or re-run
            self.assertEqual(goal[1], 3)
            self.assertEqual(conn.execute("SELECT count(*) FROM goals").fetchone()[0], 1)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def db_path_read_bytes(root: Path, name: str) -> bytes:
    """Original bytes of a source file, matched by filename under root/pages."""
    return (root / "pages" / name).read_bytes()


def call_migrate(db_path: Path, root: Path) -> dict:
    return gmig.migrate(db_path, root)


if __name__ == "__main__":
    unittest.main()
