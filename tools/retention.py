#!/usr/bin/env python3
"""Raw, quarantine, and log retention plus full DB/source/wiki backups.

``wiki/``, the live database, and ``digests/`` are never pruned. Referenced
source bytes are pinned by exact path for every contract version. Nightly
backups keep the seven newest verified bundles under ``data/backup``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(os.path.abspath(__file__)).parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BUNDLE_DIR = re.compile(r"^goal-v\d+-\d+-[0-9a-f]{8}$")


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _sibling(name: str) -> Any:
    key = f"snowball_{name}"
    path = ROOT / "tools" / f"{name}.py"
    existing = sys.modules.get(key)
    if existing is not None and getattr(existing, "__file__", None):
        if os.path.normcase(os.path.abspath(existing.__file__)) == os.path.normcase(os.path.abspath(path)):
            return existing
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _as_ist(now_dt: dt.datetime | None) -> dt.datetime:
    if now_dt is None:
        return dt.datetime.now(IST)
    if now_dt.tzinfo is None:
        return now_dt.replace(tzinfo=IST)
    return now_dt.astimezone(IST)


def _commit(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.OperationalError):
        conn.commit()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return found is not None


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _relative_posix(root_abs: Path, path: Path) -> str:
    return Path(os.path.abspath(path)).relative_to(root_abs).as_posix()


def _regular_mtime(path: Path) -> float | None:
    migration = _sibling("migration")
    if migration.is_reparse(path):
        return None
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_mtime


def _pin_key(relative: str) -> str:
    return os.path.normcase(relative)


def pinned_paths(conn: sqlite3.Connection, root: Path) -> set[str]:
    """Exact relative paths for referenced bytes. Missing or unsafe pins raise."""
    migration = _sibling("migration")
    root_abs = migration.guard_directory(root, must_exist=True)
    pins: set[str] = set()
    for raw in migration.storage_references(conn):
        relative = migration.confine_reference(root_abs, raw)
        path = root_abs / relative
        if not migration._is_regular(path):
            raise RuntimeError("Referenced source bytes are missing or unsafe; retention left the corpus unchanged.")
        pins.add(_pin_key(relative.as_posix()))
    return pins


def _iter_files(top: Path) -> list[Path]:
    return _sibling("migration")._iter_files(top)


def _plan_raw(root_abs: Path, pins: set[str], cutoff_ts: float, today: dt.date, days: int) -> list[tuple[str, Path]]:
    migration = _sibling("migration")
    raw = root_abs / "raw"
    if not os.path.lexists(raw):
        return []
    if migration.is_reparse(raw):
        raise RuntimeError("Symlink or Windows reparse traversal refused.")
    ops: list[tuple[str, Path]] = []
    cutoff_day = today - dt.timedelta(days=days)
    with os.scandir(raw) as scan:
        children = list(scan)
    for entry in children:
        path = Path(entry.path)
        if migration.is_reparse(path):
            raise RuntimeError("Symlink or Windows reparse traversal refused.")
        if entry.is_file(follow_symlinks=False):
            relative = _relative_posix(root_abs, path)
            mtime = _regular_mtime(path)
            if _pin_key(relative) not in pins and mtime is not None and mtime < cutoff_ts:
                ops.append(("unlink", path))
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        files = _iter_files(path)
        if not _DATE_DIR.match(path.name):
            continue
        try:
            stamp = dt.date.fromisoformat(path.name)
        except ValueError:
            continue
        if stamp >= cutoff_day:
            continue
        pinned_here = False
        unpinned: list[Path] = []
        for child in files:
            relative = _relative_posix(root_abs, child)
            if _pin_key(relative) in pins:
                pinned_here = True
            else:
                unpinned.append(child)
        if not pinned_here:
            ops.append(("rmtree", path))
        else:
            ops.extend(("unlink", child) for child in unpinned)
    return ops


def _plan_aged_files(
    root_abs: Path, directory: Path, cutoff_ts: float, pins: set[str] | None = None
) -> list[tuple[str, Path]]:
    migration = _sibling("migration")
    pins = pins or set()
    if not os.path.lexists(directory):
        return []
    if migration.is_reparse(directory):
        raise RuntimeError("Symlink or Windows reparse traversal refused.")
    try:
        prefix = directory.relative_to(root_abs).as_posix()
    except ValueError as exc:
        raise RuntimeError("Retention path escapes the storage root.") from exc
    ops: list[tuple[str, Path]] = []
    for path in _iter_files(directory):
        relative = _relative_posix(root_abs, path)
        if relative != prefix and not relative.startswith(prefix + "/"):
            raise RuntimeError("Retention path escapes the storage root.")
        if _pin_key(relative) in pins:
            continue
        mtime = _regular_mtime(path)
        if mtime is not None and mtime < cutoff_ts:
            ops.append(("unlink", path))
    return ops


def _execute(ops: list[tuple[str, Path]]) -> int:
    migration = _sibling("migration")
    for kind, path in ops:
        if migration.is_reparse(path):
            raise RuntimeError("Symlink or Windows reparse traversal refused.")
        if kind == "rmtree":
            migration._iter_files(path)
    removed = 0
    for kind, path in ops:
        if migration.is_reparse(path):
            raise RuntimeError("Symlink or Windows reparse traversal refused.")
        if kind == "unlink":
            if _regular_mtime(path) is None:
                continue
            path.unlink()
            removed += 1
        elif kind == "rmtree":
            shutil.rmtree(path)
            removed += 1
    return removed


def _prune_raw(conn: sqlite3.Connection, root: Path, cutoff_ts: float, today: dt.date, days: int, pins: set[str]) -> int:
    root_abs = _sibling("migration").guard_directory(root, must_exist=True)
    return _execute(_plan_raw(root_abs, pins, cutoff_ts, today, days))


def _prune_old_files(
    root: Path, directory: Path, cutoff_ts: float, pins: set[str] | None = None
) -> int:
    root_abs = _sibling("migration").guard_directory(root, must_exist=True)
    return _execute(_plan_aged_files(root_abs, directory, cutoff_ts, pins))


def _expire_deadlines(conn: sqlite3.Connection, today: dt.date) -> int:
    if not _has_table(conn, "entities"):
        return 0
    cols = _cols(conn, "entities")
    if "deadline" not in cols or "status" not in cols:
        return 0
    cur = conn.execute(
        """
        UPDATE entities
           SET status = 'expired'
         WHERE deadline IS NOT NULL
           AND TRIM(deadline) != ''
           AND substr(deadline, 1, 10) < ?
           AND status != 'expired'
        """,
        (today.isoformat(),),
    )
    _commit(conn)
    return int(cur.rowcount if cur.rowcount is not None else 0)


def _trim_backups(backup_dir: Path, keep: int = 7) -> tuple[int, int]:
    if keep < 1:
        keep = 1
    files = sorted(path for path in backup_dir.glob("snowball-*.db") if path.is_file() and not path.is_symlink())
    removed = 0
    while len(files) > keep:
        files[0].unlink()
        removed += 1
        files = files[1:]
    return len(files), removed


def _trim_bundles(backup_dir: Path, keep: int = 7) -> tuple[int, int]:
    migration = _sibling("migration")
    if keep < 1:
        keep = 1
    bundles = []
    if not backup_dir.exists():
        return 0, 0
    for path in backup_dir.iterdir():
        if migration.is_reparse(path):
            raise RuntimeError("Symlink or Windows reparse traversal refused.")
        if path.is_dir() and _BUNDLE_DIR.match(path.name):
            bundles.append(path)
    bundles.sort(key=lambda item: item.name)
    removed = 0
    while len(bundles) > keep:
        shutil.rmtree(bundles[0])
        removed += 1
        bundles = bundles[1:]
    return len(bundles), removed


def _quick_check(conn: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    if rows == ["ok"]:
        return "ok"
    return "; ".join(rows) if rows else "ok"


def run_retention(
    conn: sqlite3.Connection,
    root_dir: Path = ROOT,
    days_raw: int = 14,
    days_quarantine: int = 30,
    days_logs: int = 30,
    now_dt: dt.datetime | None = None,
    backup_dir: Path | None = None,
    keep: int = 7,
) -> dict[str, Any]:
    """Prune unreferenced aged bytes, expire deadlines, and write a full bundle.

    The verified bundle is written before any delete or deadline update. A missing
    pin, a symlink or reparse point, or a backup failure leaves the corpus unchanged.
    """
    migration = _sibling("migration")
    root = Path(root_dir)
    root_abs = migration.guard_directory(root, must_exist=True)
    bdir = Path(backup_dir) if backup_dir else (root_abs / "data" / "backup")
    if os.path.lexists(bdir):
        if migration.is_reparse(bdir):
            raise RuntimeError("Symlink or Windows reparse traversal refused.")
        for child in Path(os.path.abspath(bdir)).iterdir():
            if migration.is_reparse(child):
                raise RuntimeError("Symlink or Windows reparse traversal refused.")
    now = _as_ist(now_dt)
    today = now.date()
    pins = pinned_paths(conn, root_abs)
    raw_cutoff = now.timestamp() - days_raw * 86400
    quarantine_cutoff = now.timestamp() - days_quarantine * 86400
    log_cutoff = now.timestamp() - days_logs * 86400
    raw_ops = _plan_raw(root_abs, pins, raw_cutoff, today, days_raw)
    quarantine_ops = _plan_aged_files(root_abs, root_abs / "quarantine", quarantine_cutoff, pins)
    quarantine_ops += _plan_aged_files(root_abs, root_abs / "raw" / "quarantine", quarantine_cutoff, pins)
    log_ops = _plan_aged_files(root_abs, root_abs / "logs", log_cutoff, pins)
    bundle = migration.create_bundle(conn, root_abs, bdir)
    raw_pruned = _execute(raw_ops)
    quarantine_pruned = _execute(quarantine_ops)
    logs_pruned = _execute(log_ops)
    expired = _expire_deadlines(conn, today)
    _commit(conn)
    backups_kept, backups_pruned = _trim_bundles(bdir, keep=keep)
    _trim_backups(bdir, keep=keep)
    quick = _quick_check(conn)
    return {
        "raw_pruned": raw_pruned,
        "quarantine_pruned": quarantine_pruned,
        "logs_pruned": logs_pruned,
        "expired": expired,
        "backup_path": str(bundle),
        "backups": backups_kept,
        "backups_pruned": backups_pruned,
        "quick_check": quick,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball retention and consistent backup bundles")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--keep", type=int, default=7, help="Number of backup bundles to retain")
    ap.add_argument("--restore", type=Path, default=None, help="Bundle directory to restore into a new isolated --root")
    ap.add_argument("--json", action="store_true", help="Output summary as JSON")
    args = ap.parse_args()

    if args.restore:
        stats = _sibling("migration").restore_bundle(args.restore, args.db, args.root)
        if args.json:
            print(json.dumps(stats))
        else:
            print(
                f"Restored bundle to {stats['restored_to']} "
                f"schema={stats['schema_version']} files={stats['files_restored']} "
                f"quick_check={stats['quick_check']}"
            )
        return

    if not args.db.exists():
        err = {"ok": False, "error": f"Database not found at {args.db}"}
        if args.json:
            print(json.dumps(err))
        else:
            print(err["error"])
        sys.exit(1)
    conn = sqlite3.connect(args.db)
    try:
        stats = run_retention(conn, root_dir=args.root, keep=max(1, args.keep))
    finally:
        conn.close()
    if args.json:
        print(json.dumps(stats))
    else:
        print(
            f"Retention raw={stats['raw_pruned']} quarantine={stats['quarantine_pruned']} "
            f"logs={stats['logs_pruned']} expired={stats['expired']} "
            f"backups={stats['backups']} quick_check={stats['quick_check']}"
        )


if __name__ == "__main__":
    main()
