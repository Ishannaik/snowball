"""Explicit migration to the generic goal schema, with a verified rollback bundle.

Backs up the complete existing database plus every DB-referenced source/wiki
byte, verifies checksums and integrity, then calls
``tools.generic_store.initialize_schema(conn)``. Old rows, historical tables
and ``PRAGMA user_version`` are preserved; generic tables start empty and
nothing is autoapproved or started. Path checks are lexical and use lstat;
symlink and Windows reparse traversal are refused before any resolution.

CLI-usable signature: every exported function takes plain ``Path``/``int``
arguments and returns a JSON-serializable dict.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import migration as legacy  # noqa: E402  (lexical guards, reused after review)
from tools.migration import MigrationRequired, guard_directory, is_reparse  # noqa: E402

__all__ = ['migrate', 'backup', 'restore', 'retain', 'MigrationRequired']

MANIFEST_VERSION = 2
GENERIC_SCHEMA_VERSION = '1'
_CHUNK = 1 << 20
_WIKI_SUFFIXES = ('.md',)
GENERIC_TABLES = (
    'generic_goals', 'generic_versions', 'generic_grants', 'generic_snapshots',
    'generic_claims', 'generic_judgments', 'generic_events',
)
_OWNED_BACKUP = re.compile(r'^generic-v(\d+)-(\d{10,})-([0-9a-f]{8})$')
# (table, primary key, filesystem path column) remapped on restore.
_PATH_COLUMNS = (
    ('pages', 'id', ('raw_path',)),
    ('source_snapshots', 'id', ('text_path', 'original_raw_path')),
    ('entities', 'key', ('wiki_path',)),
    ('wiki_pages', None, ('path',)),
    ('wiki_regions', None, ('path',)),
)


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def _is_regular(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not is_reparse(path)


def _mkdir_confined(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if is_reparse(current):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        if _lexists(current):
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                raise RuntimeError('Directory path is not a directory.')
        else:
            os.mkdir(current)
    return absolute


def _read_nofollow(path: Path) -> bytes:
    if is_reparse(path):
        raise RuntimeError('Symlink or Windows reparse traversal refused.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as handle:
        return handle.read()


def _hash_file(path: Path) -> str:
    """Streaming sha256 without following links."""
    if is_reparse(path):
        raise RuntimeError('Symlink or Windows reparse traversal refused.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, flags), 'rb') as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _iter_files(top: Path) -> list[Path]:
    """Files under a real directory; a symlink or junction raises before descent."""
    files: list[Path] = []

    def walk(directory: Path) -> None:
        if is_reparse(directory):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        with os.scandir(directory) as scan:
            for entry in scan:
                path = Path(entry.path)
                if is_reparse(path):
                    raise RuntimeError('Symlink or Windows reparse traversal refused.')
                if entry.is_dir(follow_symlinks=False):
                    walk(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)

    walk(top)
    return files


def relative_reference(raw: str, source_root: str | None = None) -> Path:
    """Confine a stored reference lexically before any filesystem resolution.

    Returns a strictly relative path. Absolute references must sit inside
    ``source_root``; network paths, drives, traversal, NUL and unsafe
    components are refused.
    """
    if not isinstance(raw, str) or '\x00' in raw or raw.strip() == '':
        raise RuntimeError('Empty or invalid bundle reference refused.')
    windows = PureWindowsPath(raw)
    root_text = source_root or ''
    root_windows = bool(PureWindowsPath(root_text).drive or root_text.startswith('\\\\'))
    if (windows.drive or raw.startswith('\\\\') or raw.startswith('//')) and not root_windows:
        raise RuntimeError('Absolute or network bundle reference refused.')
    old = windows if (root_windows or windows.drive) else PurePosixPath(raw.replace('\\', '/'))
    if old.anchor:
        if source_root is None:
            raise RuntimeError('Absolute bundle reference refused.')
        root = PureWindowsPath(source_root) if root_windows else PurePosixPath(source_root)
        try:
            old = old.relative_to(root)
        except ValueError as exc:
            raise RuntimeError('Bundle reference escapes its storage root.') from exc
    if not old.parts or any(part in ('', '.', '..') for part in old.parts):
        raise RuntimeError('Empty or traversing bundle reference refused.')
    for part in old.parts:
        if ':' in part or part.endswith(' ') or part.endswith('.'):
            raise RuntimeError('Unsafe path component refused.')
    return Path(*old.parts)


def _join_nofollow(root: Path, relative: Path) -> Path:
    if is_reparse(root):
        raise RuntimeError('Symlink or Windows reparse traversal refused.')
    current = root
    for part in relative.parts:
        current = current / part
        if is_reparse(current):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
    if not _is_regular(current):
        raise RuntimeError('Bundle source/wiki checksum mismatch.')
    return current


def _connect_read_db(db_path: Path) -> sqlite3.Connection:
    text = os.fspath(db_path)
    if '\x00' in text:
        raise RuntimeError('NUL in database path refused.')
    absolute = Path(os.path.abspath(text))
    if not _lexists(absolute):
        raise RuntimeError('Database does not exist; migration refused.')
    if is_reparse(absolute) or not _is_regular(absolute):
        raise RuntimeError('Database path is not a regular file.')
    return sqlite3.connect(absolute)


def _generic_tables(conn: sqlite3.Connection) -> list[str]:
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    return [table for table in GENERIC_TABLES if table in names]


def _generic_marker(conn: sqlite3.Connection) -> str | None:
    """Value of schema_meta['generic_schema'] (str) or None when absent."""
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='generic_schema'").fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _marker_current(conn: sqlite3.Connection) -> bool:
    """Exact current generic schema: every table present and matching marker.

    Table names alone are not enough: old generic DBs carry all seven tables
    with fewer columns. Only the storage owner's ``generic_schema`` marker,
    written by ``initialize_schema``, proves the column set is current.
    """
    return len(_generic_tables(conn)) == len(GENERIC_TABLES) and _generic_marker(conn) == GENERIC_SCHEMA_VERSION


def _snapshot_database(conn: sqlite3.Connection, database: Path) -> int:
    """Byte-faithful DB snapshot via the online backup API; returns kept user_version."""
    with contextlib.closing(sqlite3.connect(database)) as dest:
        conn.backup(dest)
        user_version = dest.execute('PRAGMA user_version').fetchone()[0]
        if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or dest.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Backup database integrity verification failed.')
    return int(user_version)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _add_path(conn: sqlite3.Connection, sql: str, params: tuple, refs: list[str]) -> None:
    """Verbatim root-relative references from evidence path columns.

    Missing bytes refuse the backup; these are DB-referenced evidence.
    """
    for row in conn.execute(sql, params):
        for value in row:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                refs.append(text)


def _add_wiki_identifier(conn: sqlite3.Connection, sql: str, refs: list[str]) -> None:
    """Wiki path columns: wiki-relative logical identifiers, optional bytes.

    A value is evidence only when the file ``wiki/<value>`` actually exists;
    otherwise it is a logical identifier preserved verbatim (never remapped,
    never a failure). Stored lexical bug workaround: values that already carry
    a ``wiki/`` prefix are interpreted relative to the storage root.
    """
    for row in conn.execute(sql):
        raw = row[0]
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or '\x00' in text:
            continue
        refs.append(text)


def _collect_references(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """Returns (evidence_refs, wiki_refs)."""
    evidence: list[str] = []
    wiki: list[str] = []
    if _table_exists(conn, 'pages') and 'raw_path' in _columns(conn, 'pages'):
        _add_path(conn, 'SELECT raw_path FROM pages WHERE raw_path IS NOT NULL', (), evidence)
        if _table_exists(conn, 'claim_sources') and 'page_id' in _columns(conn, 'claim_sources'):
            _add_path(conn, '''SELECT p.raw_path FROM claim_sources c
                               JOIN pages p ON p.id = c.page_id WHERE p.raw_path IS NOT NULL''', (), evidence)
        if _table_exists(conn, 'entities') and 'evidence_page_id' in _columns(conn, 'entities'):
            _add_path(conn, '''SELECT p.raw_path FROM entities e
                               JOIN pages p ON p.id = e.evidence_page_id WHERE p.raw_path IS NOT NULL''', (), evidence)
        for table in ('entity_changes', 'entity_sources'):
            if _table_exists(conn, table) and 'page_id' in _columns(conn, table):
                _add_path(conn, f'''SELECT p.raw_path FROM {table} t
                                    JOIN pages p ON p.id = t.page_id WHERE p.raw_path IS NOT NULL''', (), evidence)
    if _table_exists(conn, 'source_snapshots') and 'text_path' in _columns(conn, 'source_snapshots'):
        original = 'original_raw_path' in _columns(conn, 'source_snapshots')
        _add_path(
            conn,
            'SELECT text_path, original_raw_path FROM source_snapshots' if original
            else 'SELECT text_path FROM source_snapshots',
            (), evidence)
        if _table_exists(conn, 'observations') and 'snapshot_id' in _columns(conn, 'observations'):
            _add_path(
                conn,
                '''SELECT s.text_path, s.original_raw_path
                   FROM observations o JOIN source_snapshots s ON s.id = o.snapshot_id''' if original
                else '''SELECT s.text_path
                        FROM observations o JOIN source_snapshots s ON s.id = o.snapshot_id''',
                (), evidence)
    if _table_exists(conn, 'entities') and 'wiki_path' in _columns(conn, 'entities'):
        _add_wiki_identifier(conn, 'SELECT wiki_path FROM entities WHERE wiki_path IS NOT NULL', wiki)
    for table in ('wiki_pages', 'wiki_regions'):
        if _table_exists(conn, table) and 'path' in _columns(conn, table):
            _add_wiki_identifier(conn, f'SELECT path FROM {table}', wiki)
    return evidence, wiki


def _resolve_evidence(root_abs: Path, refs: list[str], entries: dict[str, tuple[Path, Path]]) -> None:
    for raw in refs:
        relative = relative_reference(raw, os.fspath(root_abs) if PureWindowsPath(raw).drive or raw.startswith(('/', '\\')) else None)
        current = root_abs
        for part in relative.parts:
            current = current / part
            if is_reparse(current):
                raise RuntimeError('Symlink or Windows reparse traversal refused.')
        if not _is_regular(current):
            raise RuntimeError('Referenced source bytes are missing or unsafe; backup refused.')
        entries[relative.as_posix()] = (current, relative)


def _resolve_wiki(root_abs: Path, refs: list[str], entries: dict[str, tuple[Path, Path]]) -> None:
    """Wiki path columns carry wiki-relative logical identifiers.

    A value maps to bundled bytes only when the file actually exists on disk:
    anchored values are root-relative file paths; bare values live under
    ``wiki/``. Values whose bytes are absent stay verbatim in the database
    (logical identifiers) and never fail the backup.
    """
    wiki_root = root_abs / 'wiki'
    for raw in refs:
        anchored = bool(PureWindowsPath(raw).drive or raw.startswith(('/', '\\')))
        if anchored:
            relative = relative_reference(raw, os.fspath(root_abs))
        else:
            text = raw.replace('\\', '/')
            relative = Path('wiki') / relative_reference(text[5:] if text.lower().startswith('wiki/') else text)
        candidate = root_abs / relative
        current = root_abs
        for part in relative.parts:
            current = current / part
            if is_reparse(current):
                raise RuntimeError('Symlink or Windows reparse traversal refused.')
        if _is_regular(candidate):
            entries[relative.as_posix()] = (candidate, relative)
        # else: logical identifier — preserved in the DB, not bundled.


def _collect_entries(conn: sqlite3.Connection, root_abs: Path) -> list[tuple[Path, Path]]:
    """Confined (absolute, relative) pairs the bundle must carry.

    Evidence path columns (pages.raw_path, source_snapshots.text_path and
    linked originals, plus joins) must exist on disk or backup refuses. Wiki
    path columns are identifiers: they are bundled only when the file exists
    under ``wiki/`` and otherwise left untouched as logical identifiers.
    Every real ``.md`` file under ``wiki/`` is included regardless.
    """
    entries: dict[str, tuple[Path, Path]] = {}
    evidence, wiki_refs = _collect_references(conn)
    _resolve_evidence(root_abs, evidence, entries)
    _resolve_wiki(root_abs, wiki_refs, entries)
    wiki = root_abs / 'wiki'
    if _lexists(wiki):
        if is_reparse(wiki):
            raise RuntimeError('Wiki root is a symlink or reparse point.')
        for path in _iter_files(wiki):
            if path.suffix.lower() not in _WIKI_SUFFIXES:
                continue
            relative = Path(os.path.abspath(path)).relative_to(root_abs)
            if relative.as_posix() not in entries:
                entries[relative.as_posix()] = (path, relative)
    return [entries[key] for key in sorted(entries)]


def _create_bundle(conn: sqlite3.Connection, root_abs: Path, backup_dir: Path,
                   db_name: str) -> tuple[Path, dict[str, Any], str]:
    entries = _collect_entries(conn, root_abs)
    source_user_version = conn.execute('PRAGMA user_version').fetchone()[0]
    schema_version = legacy.schema_version(conn)
    db_bytes = conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]
    total = db_bytes + sum(os.lstat(path).st_size for path, _ in entries)
    base = guard_directory(backup_dir, must_exist=False)
    usage_at = base
    while not _lexists(usage_at) and usage_at.parent != usage_at:
        usage_at = usage_at.parent
    guard_directory(usage_at, must_exist=True)
    if shutil.disk_usage(usage_at).free < total * 2 + (1 << 20):
        raise RuntimeError('Insufficient disk for a verified backup bundle.')
    base = _mkdir_confined(base)
    bundle = base / f'generic-v{schema_version}-{int(time.time())}-{uuid.uuid4().hex[:8]}'
    os.mkdir(bundle, 0o700)
    try:
        database = bundle / 'database.db'
        user_version = _snapshot_database(conn, database)
        if user_version != int(source_user_version):
            raise RuntimeError('Backup snapshot lost PRAGMA user_version; refused.')
        manifest: dict[str, Any] = {
            'version': MANIFEST_VERSION,
            'schema_version': schema_version,
            'user_version': user_version,
            'storage_root': os.fspath(root_abs),
            'database': 'database.db',
            'database_name': db_name,
            'database_sha256': _hash_file(database),
            'files': [],
        }
        files_root = _mkdir_confined(bundle / 'files')
        for path, relative in entries:
            destination_parent = _mkdir_confined(bundle / 'files' / relative.parent) if len(relative.parts) > 1 else files_root
            destination = destination_parent / relative.parts[-1]
            landed = Path(os.path.abspath(destination)).relative_to(files_root)
            if landed.as_posix() != relative.as_posix():
                raise RuntimeError('Bundle destination escapes the manifest tree.')
            shutil.copy2(path, destination)
            if _hash_file(destination) != _hash_file(path):
                raise RuntimeError('Bundle copy checksum mismatch.')
            manifest['files'].append({'path': relative.as_posix(), 'sha256': _hash_file(path)})
        (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        _verify_bundle(bundle)
        return bundle, manifest, manifest['database_sha256']
    except BaseException:
        if _lexists(bundle) and not is_reparse(bundle):
            shutil.rmtree(bundle)
        raise


def _verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle_abs = guard_directory(bundle, must_exist=True)
    manifest_path = bundle_abs / 'manifest.json'
    if not _is_regular(manifest_path):
        raise RuntimeError('Unsupported backup manifest.')
    manifest = json.loads(_read_nofollow(manifest_path).decode('utf-8'))
    if manifest.get('version') != MANIFEST_VERSION or manifest.get('database') != 'database.db':
        raise RuntimeError('Unsupported backup manifest.')
    database_name = manifest.get('database_name')
    if (not isinstance(database_name, str) or not database_name or database_name in ('.', '..')
            or PurePosixPath(database_name.replace('\\', '/')).name != database_name
            or ':' in database_name or database_name.endswith(('.', ' '))):
        raise RuntimeError('Unsupported backup database name.')
    if not isinstance(manifest.get('files'), list):
        raise RuntimeError('Unsupported backup manifest.')
    database = bundle_abs / 'database.db'
    if not _is_regular(database) or _hash_file(database) != manifest.get('database_sha256'):
        raise RuntimeError('Backup database checksum mismatch.')
    with contextlib.closing(sqlite3.connect(database)) as check:
        if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or check.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Backup database integrity verification failed.')
    files_root = bundle_abs / 'files'
    if _lexists(files_root) and is_reparse(files_root):
        raise RuntimeError('Symlink or Windows reparse traversal refused.')
    seen: set[str] = set()
    for entry in manifest['files']:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise RuntimeError('Unsupported backup manifest.')
        relative = relative_reference(entry['path'])
        if relative.as_posix() in seen:
            raise RuntimeError('Duplicate bundle path.')
        seen.add(relative.as_posix())
        path = _join_nofollow(files_root, relative)
        if _hash_file(path) != entry.get('sha256'):
            raise RuntimeError('Backup source/wiki checksum mismatch.')
    return manifest


def _remap_value(raw: Any, entry_set: set[str], storage_root: str, destination: Path) -> Any:
    """Rewrite filesystem path values to the new storage root.

    Relative values that are not bundle entries stay untouched: logical
    wiki-relative identifiers are preserved, only real path columns backed by
    bundle bytes are remapped.
    """
    if raw is None:
        return None
    text = str(raw)
    if text == '':
        return raw
    windows = PureWindowsPath(text)
    anchored = bool(windows.drive or text.startswith(('\\', '/')))
    if anchored:
        relative = relative_reference(text, storage_root)
        inbundle = relative.as_posix() in entry_set
        if not inbundle:
            raise RuntimeError('Referenced source/wiki bytes are absent from the backup manifest.')
        return os.fspath(destination / relative)
    relative = relative_reference(text)  # refuses traversal; raises on unsafe parts
    if relative.as_posix() in entry_set:
        return os.fspath(destination / relative)
    return raw


def _remap_database(staged_db: Path, manifest: dict[str, Any], destination: Path) -> None:
    entry_set = {relative_reference(entry['path']).as_posix() for entry in manifest['files']}
    storage_root = manifest.get('storage_root') or ''
    with contextlib.closing(sqlite3.connect(staged_db)) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, primary, columns in _PATH_COLUMNS:
            if table not in tables:
                continue
            names = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            if primary is not None and primary not in names:
                continue  # unexpected legacy shape; paths stay verbatim rather than mistrust the key
            for column in columns:
                if column not in names:
                    continue
                key_sql = f'{primary}, ' if primary else ''
                rows = conn.execute(f'SELECT {key_sql}{column} FROM {table} WHERE {column} IS NOT NULL').fetchall()
                for row in rows:
                    current = row[-1]
                    key = row[0] if primary else current
                    updated = _remap_value(current, entry_set, storage_root, destination)
                    if updated != current:
                        if primary:
                            conn.execute(f'UPDATE {table} SET {column}=? WHERE {primary}=?', (updated, key))
                        else:
                            conn.execute(f'UPDATE {table} SET {column}=? WHERE {column}=?', (updated, current))
        conn.commit()
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Restored database integrity failed.')


def _load_store_module() -> Any:
    from tools import generic_store
    return generic_store


def migrate(db_path: Path, root: Path | None = None, backup_dir: Path | None = None) -> dict[str, Any]:
    """Back up DB + referenced bytes, verify, then initialize the generic schema.

    Legacy rows, historical tables and ``PRAGMA user_version`` are preserved;
    generic tables are created empty (no autoapproved goals, nothing run).
    """
    db_conn = _connect_read_db(db_path)
    try:
        if _marker_current(db_conn):
            return {'changed': False, 'backup': None, 'user_version': db_conn.execute('PRAGMA user_version').fetchone()[0], 'quick_check': 'ok'}
        if root is None:
            raise ValueError('migrate requires the storage root for the backup bundle.')
        root_abs = guard_directory(root, must_exist=True)
        base = backup_dir if backup_dir is not None else root_abs / 'data' / 'backup'
        bundle, manifest, database_sha256 = _create_bundle(
            db_conn, root_abs, base, Path(os.path.abspath(os.fspath(db_path))).name)
        # Backup is verified and durable; only now touch the live schema.
        generic_store = _load_store_module()
        generic_store.initialize_schema(db_conn)
        created = _generic_tables(db_conn)
        if len(created) != len(GENERIC_TABLES):
            raise RuntimeError('initialize_schema did not produce the complete generic schema.')
        if _generic_marker(db_conn) != GENERIC_SCHEMA_VERSION:
            raise RuntimeError('initialize_schema did not record the generic schema marker; refused.')
        user_version = db_conn.execute('PRAGMA user_version').fetchone()[0]
        if user_version != manifest['user_version']:
            raise RuntimeError('Legacy PRAGMA user_version was clobbered during migration.')
        if db_conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or db_conn.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Post-migration database integrity failed.')
        return {
            'changed': True,
            'schema_version': manifest['schema_version'],
            'user_version': user_version,
            'backup': {
                'bundle': str(bundle),
                'database_sha256': database_sha256,
                'files': len(manifest['files']),
            },
            'generic_tables': list(created),
            'quick_check': 'ok',
        }
    finally:
        db_conn.close()


def backup(db_path: Path, root: Path, destination: Path) -> dict[str, Any]:
    """Verified rollback bundle: full DB snapshot plus referenced source/wiki bytes."""
    db_conn = _connect_read_db(db_path)
    try:
        root_abs = guard_directory(root, must_exist=True)
        bundle, manifest, database_sha256 = _create_bundle(
            db_conn, root_abs, destination, Path(os.path.abspath(os.fspath(db_path))).name)
        return {
            'bundle': str(bundle),
            'schema_version': manifest['schema_version'],
            'user_version': manifest['user_version'],
            'files': len(manifest['files']),
            'database_sha256': database_sha256,
            'quick_check': 'ok',
        }
    finally:
        db_conn.close()


def restore(bundle: Path, destination: Path) -> dict[str, Any]:
    """Restore a verified bundle into a new isolated root, staged and atomic."""
    bundle_abs = guard_directory(bundle, must_exist=True)
    manifest = _verify_bundle(bundle_abs)
    destination_abs = guard_directory(destination, must_exist=False)
    if destination_abs.is_relative_to(bundle_abs) or bundle_abs.is_relative_to(destination_abs):
        raise RuntimeError('Restore destination may not nest with the bundle.')
    if _lexists(destination_abs):
        if is_reparse(destination_abs):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        if not stat.S_ISDIR(os.lstat(destination_abs).st_mode):
            raise RuntimeError('Restore destination is not a directory.')
        if any(os.scandir(destination_abs)):
            raise ValueError('Restore requires a new isolated storage root; existing corpus refused.')
        os.rmdir(destination_abs)
    parent = _mkdir_confined(guard_directory(destination_abs.parent, must_exist=False))
    db_name = manifest['database_name']
    entries = [relative_reference(entry['path']) for entry in manifest['files']]
    if Path(db_name).as_posix() in {entry.as_posix() for entry in entries}:
        raise RuntimeError('Restored database conflicts with a corpus file.')
    staged_root = Path(tempfile.mkdtemp(prefix=f'.{destination_abs.name}-restore-', dir=parent))
    try:
        if is_reparse(staged_root):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        staged_db = staged_root / db_name
        shutil.copy2(bundle_abs / 'database.db', staged_db)
        _remap_database(staged_db, manifest, destination_abs)
        for relative, entry in zip(entries, manifest['files'], strict=True):
            destination_parent = _mkdir_confined(staged_root / relative.parent) if len(relative.parts) > 1 else staged_root
            target = destination_parent / relative.parts[-1]
            source = _join_nofollow(bundle_abs / 'files', relative)
            shutil.copy2(source, target)
            landed = Path(os.path.abspath(target)).relative_to(staged_root)
            if landed.as_posix() != relative.as_posix():
                raise RuntimeError('Restore destination escapes the new storage root.')
            if _hash_file(target) != entry.get('sha256'):
                raise RuntimeError('Backup source/wiki checksum mismatch.')
        if _lexists(destination_abs):
            raise ValueError('Restore root appeared while staging; existing corpus refused.')
        os.rename(staged_root, destination_abs)
    finally:
        if _lexists(staged_root) and not is_reparse(staged_root):
            shutil.rmtree(staged_root)
    return {
        'restored_to': str(destination_abs / db_name),
        'schema_version': manifest['schema_version'],
        'user_version': manifest['user_version'],
        'files_restored': len(manifest['files']),
        'quick_check': 'ok',
    }


def retain(root: Path, keep: int = 7) -> dict[str, Any]:
    """Delete oldest owned generic backup bundles; nothing else is ever touched.

    Only directories whose exact name matches this module's bundle pattern are
    candidates, anywhere under ``root`` (default bundles live in
    ``root/data/backup``; explicit destinations may sit elsewhere). Sources,
    wiki, history, and foreign/legacy backup names are never read or removed.
    """
    if keep < 1:
        raise ValueError('keep must be at least 1.')
    root_abs = guard_directory(root, must_exist=True)
    owned: list[tuple[int, str, Path]] = []

    def walk(directory: Path, depth: int) -> None:
        if is_reparse(directory):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        with os.scandir(directory) as scan:
            for entry in scan:
                path = Path(entry.path)
                if is_reparse(path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    match = _OWNED_BACKUP.match(entry.name)
                    if match is not None:
                        owned.append((int(match.group(2)), entry.name, path))
                        continue  # never descend into a candidate bundle
                    if depth < 6:
                        walk(path, depth + 1)

    walk(root_abs, 0)
    owned.sort(key=lambda item: (item[0], item[1]), reverse=True)
    kept = [name for _ts, name, _path in owned[:keep]]
    deleted: list[str] = []
    for _ts, name, path in owned[keep:]:
        shutil.rmtree(path)
        deleted.append(name)
    return {'root': str(root_abs), 'kept': kept, 'deleted': deleted, 'refused': []}
