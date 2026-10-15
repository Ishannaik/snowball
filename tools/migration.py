"""Explicit schema migration with a verified DB/source/wiki rollback bundle.

Path checks are lexical and use lstat. They refuse symlink and Windows reparse
traversal before any resolve, including bundle creation, manifests, and restore.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

VERSION = 4
BINDING_COLUMNS = {
    'tasks': {'goal_id': 'INTEGER REFERENCES goals(id)', 'contract_version': 'INTEGER',
              'strategy_version': 'INTEGER', 'epoch': 'INTEGER', 'grant_id': 'INTEGER',
              'lease_token': 'TEXT', 'lease_until': 'INTEGER', 'operation_key': 'TEXT'},
    'queries': {'program_key': 'TEXT', 'field_key': 'TEXT'},
    'entities': {'provenance': "TEXT NOT NULL DEFAULT 'legacy_v3'"},
    'claims': {'provenance': "TEXT NOT NULL DEFAULT 'legacy_v3'"},
    'source_snapshots': {'text_format': "TEXT NOT NULL DEFAULT 'utf8_text'",
                         'encoding': "TEXT NOT NULL DEFAULT 'utf-8'",
                         'offset_kind': "TEXT NOT NULL DEFAULT 'unicode_codepoints'"},
}
_CURRENT_TABLES = ('goals', 'source_snapshots', 'observations')
_REPARSE_ATTRIBUTE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
_OBSERVATION_ID_KEYS = {
    'observation_id', 'observation_ids', 'backing_ids', 'duration_observation_ids', 'basis_ids',
}
_SNAPSHOT_ID_KEYS = {'snapshot_id', 'snapshot_ids'}


class MigrationRequired(RuntimeError):
    pass


def is_reparse(path: Path) -> bool:
    """True for a symlink or Windows junction/mount without following it."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, 'st_reparse_tag', 0):
        return True
    attributes = getattr(info, 'st_file_attributes', 0) or 0
    return bool(attributes & _REPARSE_ATTRIBUTE)


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def guard_directory(path: Path, *, must_exist: bool = False) -> Path:
    """Absolute directory path whose existing ancestors are real directories.

    ``..`` is collapsed lexically by abspath. Symlink and reparse components are
    refused before any resolve.
    """
    text = os.fspath(path)
    if '\x00' in text:
        raise RuntimeError('NUL in path refused.')
    absolute = Path(os.path.abspath(text))
    if is_reparse(absolute):
        raise RuntimeError('Symlink or Windows reparse traversal refused.')
    current = Path(absolute.anchor)
    missing = False
    for part in absolute.parts[1:]:
        current = current / part
        if is_reparse(current):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        if not _lexists(current):
            missing = True
            break
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError('Directory path is not a directory.')
    if must_exist and (missing or not _lexists(absolute)):
        raise RuntimeError('Directory does not exist.')
    if _lexists(absolute):
        info = os.lstat(absolute)
        if is_reparse(absolute) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
    return absolute


def _mkdir_confined(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if is_reparse(current):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        if _lexists(current):
            info = os.lstat(current)
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError('Directory path is not a directory.')
        else:
            os.mkdir(current)
    return absolute


def _read_nofollow(path: Path) -> bytes:
    if is_reparse(path):
        raise RuntimeError('Symlink or Windows reparse traversal refused.')
    flags = os.O_RDONLY
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    if nofollow:
        flags |= nofollow
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as handle:
        return handle.read()


def _is_regular(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not is_reparse(path)


def _relative_reference(raw: str, source_root: str | None = None) -> Path:
    """Confine portable references lexically before any filesystem resolution."""
    if not isinstance(raw, str) or '\x00' in raw or raw.strip() == '':
        raise RuntimeError('Empty or invalid rollback reference refused.')
    windows = PureWindowsPath(raw)
    root_text = source_root or ''
    root_windows = bool(PureWindowsPath(root_text).drive or root_text.startswith('\\\\'))
    if windows.drive and not root_windows:
        raise RuntimeError('Foreign drive or network path in rollback reference.')
    if raw.startswith('\\\\') or raw.startswith('//'):
        if source_root is None:
            raise RuntimeError('Network rollback path refused.')
    old = windows if root_windows else PurePosixPath(raw.replace('\\', '/'))
    if old.anchor:
        if source_root is None:
            raise RuntimeError('Absolute or drive-relative rollback path refused.')
        root = PureWindowsPath(source_root) if root_windows else PurePosixPath(source_root)
        try:
            old = old.relative_to(root)
        except ValueError as exc:
            raise RuntimeError('Rollback reference escapes its original storage root.') from exc
    if not old.parts or any(part in ('', '.', '..') for part in old.parts):
        raise RuntimeError('Empty or traversing rollback reference refused.')
    for part in old.parts:
        if ':' in part or part.endswith(' ') or part.endswith('.'):
            raise RuntimeError('Unsafe path component refused.')
    return Path(*old.parts)


def confine_reference(root: Path, raw: str) -> Path:
    """Relative path under ``root``; reparse components are refused before resolve."""
    root_abs = guard_directory(root, must_exist=True)
    relative = _relative_reference(str(raw), os.fspath(root_abs))
    current = root_abs
    for part in relative.parts:
        current = current / part
        if is_reparse(current):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
    return relative


def _join_nofollow(root: Path, relative: Path) -> Path:
    if is_reparse(root):
        raise RuntimeError('Symlink or Windows reparse traversal refused.')
    current = root
    for part in relative.parts:
        current = current / part
        if is_reparse(current):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
    if not _is_regular(current):
        raise RuntimeError('Rollback source/wiki checksum mismatch.')
    return current


def _iter_files(top: Path) -> list[Path]:
    """Files under a real directory. A symlink or junction raises before descent."""
    files: list[Path] = []

    def walk(directory: Path) -> None:
        if is_reparse(directory):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        try:
            scan = os.scandir(directory)
        except OSError as exc:
            raise RuntimeError('Cannot read directory without following links.') from exc
        with scan:
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


def _read_version(conn: sqlite3.Connection) -> tuple[int | None, str]:
    """Return ``(version, 'ok'|'absent'|'malformed')`` without writing."""
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None, 'absent'
    if row is None:
        return None, 'absent'
    value = row[0]
    if isinstance(value, bool) or value is None:
        return None, 'malformed'
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None, 'malformed'
    if text != '0' and (not text.isdigit() or text.startswith('0')):
        return None, 'malformed'
    if not text.isdigit():
        return None, 'malformed'
    return int(text), 'ok'


def schema_version(conn: sqlite3.Connection) -> int:
    version, kind = _read_version(conn)
    if kind == 'malformed':
        raise MigrationRequired('Malformed schema_version refused; read will not guess or upgrade.')
    if kind == 'absent':
        return 0
    return int(version or 0)


def _nonempty(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _current_shape(conn: sqlite3.Connection) -> bool:
    return all(_table_exists(conn, table) for table in _CURRENT_TABLES)


def assert_current(conn: sqlite3.Connection) -> None:
    """Read-only current-schema check. Never creates tables or starts a migration."""
    version, kind = _read_version(conn)
    if kind != 'ok' or version != VERSION or not _current_shape(conn):
        raise MigrationRequired('Schema migration required; run snowball migrate explicitly with services stopped.')


def execute_script(conn: sqlite3.Connection, script: str) -> None:
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete schema statement.')


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def add_columns(conn: sqlite3.Connection) -> None:
    for table, declarations in BINDING_COLUMNS.items():
        columns = _columns(conn, table)
        for name, declaration in declarations.items():
            if name not in columns:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    if 'private' not in _columns(conn, 'topics'):
        conn.execute('ALTER TABLE topics ADD COLUMN private INTEGER NOT NULL DEFAULT 0')


def initialize(conn: sqlite3.Connection, ddl: str, extension: str, goal_ddl: str) -> None:
    version, kind = _read_version(conn)
    if kind == 'malformed':
        raise MigrationRequired('Malformed schema cannot be initialized or upgraded implicitly.')
    if kind == 'ok' and version == VERSION:
        assert_current(conn)
        return
    if kind == 'ok' or _nonempty(conn):
        raise MigrationRequired('Refusing nonempty or non-fresh schema; initialization does not upgrade.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        execute_script(conn, ddl)
        execute_script(conn, extension)
        execute_script(conn, goal_ddl)
        add_columns(conn)
        conn.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
        conn.execute("INSERT INTO schema_meta VALUES('orchestrator_version','2')")
        if conn.execute('PRAGMA foreign_key_check').fetchone() or conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Fresh schema integrity verification failed.')
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise


def _json_ids(raw: object, keys: set[str]) -> list[int]:
    if not isinstance(raw, str) or raw.strip() == '':
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    found: list[int] = []

    def walk(node: object, key: str | None) -> None:
        if isinstance(node, dict):
            for child_key, child in node.items():
                walk(child, str(child_key))
        elif isinstance(node, list):
            if key in keys:
                for item in node:
                    if isinstance(item, int) and not isinstance(item, bool):
                        found.append(item)
            else:
                for item in node:
                    walk(item, key)
        elif isinstance(node, int) and not isinstance(node, bool) and key in keys:
            found.append(node)

    if isinstance(data, list) and keys == _SNAPSHOT_ID_KEYS and all(
        isinstance(item, int) and not isinstance(item, bool) for item in data
    ):
        return [int(item) for item in data]
    if isinstance(data, list) and keys == _OBSERVATION_ID_KEYS and all(
        isinstance(item, int) and not isinstance(item, bool) for item in data
    ):
        return [int(item) for item in data]
    walk(data, None)
    return found


def _add_ref(refs: list[str], value: object) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text:
        refs.append(text)


def _select_snapshot_paths(conn: sqlite3.Connection, snapshot_ids: set[int], refs: list[str]) -> None:
    if not snapshot_ids or not _table_exists(conn, 'source_snapshots'):
        return
    columns = _columns(conn, 'source_snapshots')
    if 'text_path' not in columns:
        return
    original = 'original_raw_path' in columns
    pending = list(snapshot_ids)
    while pending:
        group = pending[:400]
        del pending[:400]
        marks = ','.join('?' for _ in group)
        sql = (
            f'SELECT text_path, original_raw_path FROM source_snapshots WHERE id IN ({marks})'
            if original else
            f'SELECT text_path, NULL FROM source_snapshots WHERE id IN ({marks})'
        )
        for text, raw in conn.execute(sql, group):
            _add_ref(refs, text)
            _add_ref(refs, raw)


def storage_references(conn: sqlite3.Connection) -> list[str]:
    """Every historical and current byte path the corpus must keep.

    Observations are not filtered by contract version. Packages, evaluations,
    contradictions, model-call snapshot ids, quote pages, and human paths are
    included by full path.
    """
    refs: list[str] = []
    pages = _columns(conn, 'pages') if _table_exists(conn, 'pages') else set()
    snapshots = _columns(conn, 'source_snapshots') if _table_exists(conn, 'source_snapshots') else set()
    if 'raw_path' in pages:
        for (raw,) in conn.execute('SELECT raw_path FROM pages WHERE raw_path IS NOT NULL'):
            _add_ref(refs, raw)
    if 'text_path' in snapshots:
        original = 'original_raw_path' in snapshots
        sql = (
            'SELECT text_path, original_raw_path FROM source_snapshots'
            if original else
            'SELECT text_path, NULL FROM source_snapshots'
        )
        for text, raw in conn.execute(sql):
            _add_ref(refs, text)
            _add_ref(refs, raw)
    observations = _columns(conn, 'observations') if _table_exists(conn, 'observations') else set()
    if 'snapshot_id' in observations and 'text_path' in snapshots:
        original = 'original_raw_path' in snapshots
        sql = (
            '''SELECT s.text_path, s.original_raw_path
               FROM observations o JOIN source_snapshots s ON s.id = o.snapshot_id'''
            if original else
            '''SELECT s.text_path, NULL
               FROM observations o JOIN source_snapshots s ON s.id = o.snapshot_id'''
        )
        for text, raw in conn.execute(sql):
            _add_ref(refs, text)
            _add_ref(refs, raw)
    snapshot_ids: set[int] = set()
    observation_ids: set[int] = set()
    if _table_exists(conn, 'funding_packages'):
        package_columns = _columns(conn, 'funding_packages')
        for name in ('basis_observation_ids_json', 'compatibility_basis_ids_json', 'components_json'):
            if name not in package_columns:
                continue
            for (raw,) in conn.execute(f'SELECT {name} FROM funding_packages'):
                observation_ids.update(_json_ids(raw, _OBSERVATION_ID_KEYS))
                snapshot_ids.update(_json_ids(raw, _SNAPSHOT_ID_KEYS))
    if _table_exists(conn, 'constraint_evaluations') and 'basis_json' in _columns(conn, 'constraint_evaluations'):
        for (raw,) in conn.execute('SELECT basis_json FROM constraint_evaluations'):
            observation_ids.update(_json_ids(raw, _OBSERVATION_ID_KEYS))
            snapshot_ids.update(_json_ids(raw, _SNAPSHOT_ID_KEYS))
    if _table_exists(conn, 'model_calls') and 'source_snapshot_ids_json' in _columns(conn, 'model_calls'):
        for (raw,) in conn.execute('SELECT source_snapshot_ids_json FROM model_calls'):
            snapshot_ids.update(_json_ids(raw, _SNAPSHOT_ID_KEYS))
    if _table_exists(conn, 'contradictions') and 'snapshot_id' in observations:
        for left, right in conn.execute('SELECT observation_a, observation_b FROM contradictions'):
            if isinstance(left, int):
                observation_ids.add(left)
            if isinstance(right, int):
                observation_ids.add(right)
    if observation_ids and 'snapshot_id' in observations:
        pending = list(observation_ids)
        while pending:
            group = pending[:400]
            del pending[:400]
            marks = ','.join('?' for _ in group)
            for (snapshot_id,) in conn.execute(
                f'SELECT snapshot_id FROM observations WHERE id IN ({marks})', group
            ):
                if isinstance(snapshot_id, int):
                    snapshot_ids.add(snapshot_id)
    _select_snapshot_paths(conn, snapshot_ids, refs)
    if 'raw_path' in pages and _table_exists(conn, 'claim_sources'):
        for (raw,) in conn.execute(
            '''SELECT p.raw_path FROM claim_sources c
               JOIN pages p ON p.id = c.page_id WHERE p.raw_path IS NOT NULL'''
        ):
            _add_ref(refs, raw)
    if 'raw_path' in pages and _table_exists(conn, 'entities') and 'evidence_page_id' in _columns(conn, 'entities'):
        for (raw,) in conn.execute(
            '''SELECT p.raw_path FROM entities e
               JOIN pages p ON p.id = e.evidence_page_id WHERE p.raw_path IS NOT NULL'''
        ):
            _add_ref(refs, raw)
    if _table_exists(conn, 'entities') and 'wiki_path' in _columns(conn, 'entities'):
        for (raw,) in conn.execute('SELECT wiki_path FROM entities WHERE wiki_path IS NOT NULL'):
            _add_ref(refs, raw)
    for table, column in (
        ('entity_changes', 'page_id'),
        ('entity_sources', 'page_id'),
    ):
        if 'raw_path' in pages and _table_exists(conn, table) and column in _columns(conn, table):
            for (raw,) in conn.execute(
                f'''SELECT p.raw_path FROM {table} t
                    JOIN pages p ON p.id = t.{column} WHERE p.raw_path IS NOT NULL'''
            ):
                _add_ref(refs, raw)
    if _table_exists(conn, 'wiki_pages') and 'path' in _columns(conn, 'wiki_pages'):
        for (raw,) in conn.execute('SELECT path FROM wiki_pages'):
            _add_ref(refs, raw)
    if _table_exists(conn, 'wiki_regions') and 'path' in _columns(conn, 'wiki_regions'):
        for (raw,) in conn.execute('SELECT path FROM wiki_regions'):
            _add_ref(refs, raw)
    return refs


def referenced_paths(conn: sqlite3.Connection, root: Path) -> set[Path]:
    root_abs = guard_directory(root, must_exist=True)
    paths: set[Path] = set()
    for raw in storage_references(conn):
        relative = confine_reference(root_abs, raw)
        paths.add(root_abs / relative)
    return paths


def _bundle_entries(conn: sqlite3.Connection, root_abs: Path) -> list[tuple[Path, Path]]:
    entries: dict[str, tuple[Path, Path]] = {}
    for raw in storage_references(conn):
        relative = confine_reference(root_abs, raw)
        path = root_abs / relative
        if not _is_regular(path):
            raise RuntimeError('Referenced source/wiki bytes are missing or unsafe; migration refused.')
        entries[relative.as_posix()] = (path, relative)
    wiki = root_abs / 'wiki'
    if _lexists(wiki):
        if is_reparse(wiki):
            raise RuntimeError('Wiki root is a symlink or reparse point.')
        for path in _iter_files(wiki):
            if path.suffix != '.md':
                continue
            if not _is_regular(path):
                raise RuntimeError('Wiki symlink or reparse traversal refused.')
            relative = Path(os.path.abspath(path)).relative_to(root_abs)
            if any(part in ('', '.', '..') for part in relative.parts):
                raise RuntimeError('Wiki path escapes the storage root.')
            entries[relative.as_posix()] = (path, relative)
    return [entries[key] for key in sorted(entries)]


def _vacuum_into(conn: sqlite3.Connection, database: Path) -> None:
    quoted = Path(os.path.abspath(database)).as_posix().replace("'", "''")
    previous = conn.isolation_level
    try:
        if conn.in_transaction:
            conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.isolation_level = None
    try:
        conn.execute(f"VACUUM INTO '{quoted}'")
    finally:
        conn.isolation_level = previous


def create_bundle(conn: sqlite3.Connection, root: Path, backup_dir: Path | None = None) -> Path:
    root_abs = guard_directory(root, must_exist=True)
    entries = _bundle_entries(conn, root_abs)
    total = conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]
    for path, _relative in entries:
        total += os.lstat(path).st_size
    base = guard_directory(backup_dir or (root_abs / 'data' / 'backup'), must_exist=False)
    usage_at = base if _lexists(base) else base.parent
    usage_at = guard_directory(usage_at, must_exist=True)
    if shutil.disk_usage(usage_at).free < total * 2 + 1024 * 1024:
        raise RuntimeError('Insufficient disk for a verified rollback bundle.')
    base = _mkdir_confined(base)
    version, kind = _read_version(conn)
    if kind == 'malformed':
        raise MigrationRequired('Malformed schema_version refused; no bundle started.')
    bundle = base / f'goal-v{0 if version is None else version}-{int(time.time())}-{uuid.uuid4().hex[:8]}'
    os.mkdir(bundle, 0o700)
    try:
        database = bundle / 'database.db'
        _vacuum_into(conn, database)
        manifest: dict[str, Any] = {
            'version': 1,
            'schema_version': 0 if version is None else version,
            'storage_root': os.fspath(root_abs),
            'database': 'database.db',
            'files': [],
        }
        for path, relative in entries:
            parent = relative.parent
            destination_parent = _mkdir_confined(bundle / 'files' / parent) if parent.parts and parent != Path('.') else _mkdir_confined(bundle / 'files')
            destination = destination_parent / relative.name
            landed = Path(os.path.abspath(destination)).relative_to(Path(os.path.abspath(bundle / 'files')))
            if landed.as_posix() != relative.as_posix():
                raise RuntimeError('Bundle destination escapes the manifest tree.')
            shutil.copy2(path, destination)
            digest = hashlib.sha256(_read_nofollow(destination)).hexdigest()
            if digest != hashlib.sha256(_read_nofollow(path)).hexdigest():
                raise RuntimeError('Bundle copy checksum mismatch.')
            manifest['files'].append({'path': relative.as_posix(), 'sha256': digest})
        manifest['database_sha256'] = hashlib.sha256(_read_nofollow(database)).hexdigest()
        (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        verify_bundle(bundle)
        return bundle
    except BaseException:
        if _lexists(bundle) and not is_reparse(bundle):
            shutil.rmtree(bundle)
        raise


def verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle_abs = guard_directory(bundle, must_exist=True)
    manifest_path = bundle_abs / 'manifest.json'
    if not _is_regular(manifest_path):
        raise RuntimeError('Unsupported rollback manifest.')
    manifest = json.loads(_read_nofollow(manifest_path).decode('utf-8'))
    if manifest.get('version') != 1 or manifest.get('database') != 'database.db' or not isinstance(manifest.get('files'), list):
        raise RuntimeError('Unsupported rollback manifest.')
    database = bundle_abs / 'database.db'
    if not _is_regular(database) or hashlib.sha256(_read_nofollow(database)).hexdigest() != manifest.get('database_sha256'):
        raise RuntimeError('Rollback database checksum mismatch.')
    with contextlib.closing(sqlite3.connect(Path(os.path.abspath(database)).as_uri() + '?mode=ro', uri=True)) as check:
        if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or check.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Rollback database integrity verification failed.')
    seen = set()
    files_root = bundle_abs / 'files'
    if _lexists(files_root) and is_reparse(files_root):
        raise RuntimeError('Symlink or Windows reparse traversal refused.')
    for entry in manifest['files']:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise RuntimeError('Unsupported rollback manifest.')
        relative = _relative_reference(entry['path'])
        if relative.as_posix() in seen:
            raise RuntimeError('Duplicate rollback path.')
        seen.add(relative.as_posix())
        path = _join_nofollow(files_root, relative)
        if hashlib.sha256(_read_nofollow(path)).hexdigest() != entry.get('sha256'):
            raise RuntimeError('Rollback source/wiki checksum mismatch.')
    return manifest


def restore_bundle(bundle: Path, target_db: Path, target_root: Path) -> dict[str, Any]:
    bundle_abs = guard_directory(bundle, must_exist=True)
    manifest = verify_bundle(bundle_abs)
    target_abs = Path(os.path.abspath(target_root))
    if _lexists(target_abs):
        raise ValueError('Restore requires a new isolated storage root; existing corpus refused.')
    parent = _mkdir_confined(guard_directory(target_abs.parent, must_exist=False))
    db_abs = Path(os.path.abspath(target_db))
    try:
        db_relative = db_abs.relative_to(target_abs)
    except ValueError as exc:
        raise ValueError('Restored database must be inside the new storage root.') from exc
    if not db_relative.parts or any(part in ('', '.', '..') for part in db_relative.parts):
        raise ValueError('Restored database requires a filename.')
    entries = [_relative_reference(entry['path']) for entry in manifest['files']]
    entry_set = set(entries)
    if db_relative in entry_set:
        raise ValueError('Restored database conflicts with a corpus file.')
    staged_root = Path(tempfile.mkdtemp(prefix=f'.{target_abs.name}-restore-', dir=parent))
    try:
        if is_reparse(staged_root):
            raise RuntimeError('Symlink or Windows reparse traversal refused.')
        staged_db_parent = _mkdir_confined(staged_root / db_relative.parent) if db_relative.parent.parts else staged_root
        staged_db = staged_db_parent / db_relative.name
        shutil.copy2(bundle_abs / 'database.db', staged_db)

        def remap(raw: object) -> object:
            if raw is None or raw == '':
                return raw
            relative = _relative_reference(str(raw), manifest.get('storage_root'))
            if relative not in entry_set:
                raise RuntimeError('Referenced source bytes are absent from the rollback manifest.')
            return str(target_abs / relative)

        with contextlib.closing(sqlite3.connect(staged_db)) as connection:
            if _table_exists(connection, 'pages'):
                for page_id, raw in connection.execute('SELECT id,raw_path FROM pages WHERE raw_path IS NOT NULL').fetchall():
                    connection.execute('UPDATE pages SET raw_path=? WHERE id=?', (remap(raw), page_id))
            if _table_exists(connection, 'source_snapshots'):
                columns = _columns(connection, 'source_snapshots')
                if 'text_path' in columns:
                    original_sql = 'original_raw_path' if 'original_raw_path' in columns else 'NULL'
                    for snapshot_id, text, original in connection.execute(
                        f'SELECT id,text_path,{original_sql} FROM source_snapshots'
                    ).fetchall():
                        if 'original_raw_path' in columns:
                            connection.execute(
                                'UPDATE source_snapshots SET text_path=?,original_raw_path=? WHERE id=?',
                                (remap(text), remap(original), snapshot_id),
                            )
                        else:
                            connection.execute(
                                'UPDATE source_snapshots SET text_path=? WHERE id=?',
                                (remap(text), snapshot_id),
                            )
            if _table_exists(connection, 'entities') and 'wiki_path' in _columns(connection, 'entities'):
                for key, raw in connection.execute('SELECT key,wiki_path FROM entities WHERE wiki_path IS NOT NULL').fetchall():
                    connection.execute('UPDATE entities SET wiki_path=? WHERE key=?', (remap(raw), key))
            for table in ('wiki_pages', 'wiki_regions'):
                if _table_exists(connection, table) and 'path' in _columns(connection, table):
                    for raw in connection.execute(f'SELECT path FROM {table}').fetchall():
                        connection.execute(f'UPDATE {table} SET path=? WHERE path=?', (remap(raw[0]), raw[0]))
            connection.commit()
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('Restored database integrity failed.')
        for relative, entry in zip(entries, manifest['files'], strict=True):
            destination_parent = _mkdir_confined(staged_root / relative.parent) if relative.parent.parts else staged_root
            destination = destination_parent / relative.name
            source = _join_nofollow(bundle_abs / 'files', relative)
            shutil.copy2(source, destination)
            landed = Path(os.path.abspath(destination)).relative_to(staged_root)
            if landed.as_posix() != relative.as_posix():
                raise RuntimeError('Restore destination escapes the new storage root.')
            if hashlib.sha256(_read_nofollow(destination)).hexdigest() != entry.get('sha256'):
                raise RuntimeError('Rollback source/wiki checksum mismatch.')
        if _lexists(target_abs):
            raise ValueError('Restore root appeared while staging; existing corpus refused.')
        os.rename(staged_root, target_abs)
    finally:
        if _lexists(staged_root) and not is_reparse(staged_root):
            shutil.rmtree(staged_root)
    return {
        'restored_to': str(db_abs),
        'schema_version': manifest['schema_version'],
        'files_restored': len(manifest['files']),
        'quick_check': 'ok',
    }


def _definition(script: str, table: str) -> str:
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip().startswith(f'CREATE TABLE {table} '):
                return statement.strip()
            statement = ''
    raise ValueError('Missing base table declaration.')


def _rebuild(conn: sqlite3.Connection, table: str, definition: str) -> None:
    indexes = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
        (table,),
    )]
    old_columns = _columns(conn, table)
    conn.execute(definition.replace(f'CREATE TABLE {table} ', f'CREATE TABLE {table}_v4 ', 1))
    columns = sorted(old_columns & _columns(conn, f'{table}_v4'))
    conn.execute(f"INSERT INTO {table}_v4 ({','.join(columns)}) SELECT {','.join(columns)} FROM {table}")
    conn.execute(f'DROP TABLE {table}')
    conn.execute(f'ALTER TABLE {table}_v4 RENAME TO {table}')
    for statement in indexes:
        conn.execute(statement)


def migrate(conn: sqlite3.Connection, root: Path, ddl: str, extension: str, goal_ddl: str,
            backup_dir: Path | None = None) -> dict[str, Any]:
    version, kind = _read_version(conn)
    if kind == 'malformed':
        raise MigrationRequired('Malformed schema_version refused; no migration started.')
    if kind == 'absent' or version in (None, 0):
        raise MigrationRequired('Unknown or nonempty schema 0 refused; a read does not initialize or upgrade.')
    if version == VERSION:
        assert_current(conn)
        return {'schema_version': VERSION, 'changed': False, 'bundle': None}
    if version not in (2, 3):
        raise MigrationRequired('Only existing schema v2/v3 may be upgraded; unknown schema refused.')
    bundle = create_bundle(conn, root, backup_dir)
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.execute('PRAGMA legacy_alter_table=ON')
    try:
        conn.execute('BEGIN IMMEDIATE')
        if version == 2:
            for table in ('fetches', 'paid_fetches'):
                current = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                if (table == 'fetches' and "'deadline'" not in current) or (table == 'paid_fetches' and "'2captcha'" not in current):
                    _rebuild(conn, table, _definition(ddl, table))
        for table in ('queries', 'pages', 'tasks'):
            _rebuild(conn, table, _definition(ddl, table))
        if _table_exists(conn, 'engine_spend'):
            _rebuild(conn, 'engine_spend', _definition(extension.replace('CREATE TABLE IF NOT EXISTS', 'CREATE TABLE'), 'engine_spend'))
        execute_script(conn, extension)
        execute_script(conn, goal_ddl)
        add_columns(conn)
        if conn.execute('PRAGMA foreign_key_check').fetchone() or conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Migration integrity verification failed; no version advanced.')
        conn.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
        conn.execute("INSERT INTO schema_meta VALUES('orchestrator_version','2') ON CONFLICT(key) DO UPDATE SET value='2'")
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.execute('PRAGMA legacy_alter_table=OFF')
        conn.execute('PRAGMA foreign_keys=ON')
    return {'schema_version': VERSION, 'changed': True, 'bundle': str(bundle), 'quick_check': 'ok'}
