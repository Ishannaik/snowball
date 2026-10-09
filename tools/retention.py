#!/usr/bin/env python3
"""Raw, quarantine, and log retention plus SQLite backups.

``wiki/``, ``data/snowball.db``, and ``digests/`` are never pruned. Nightly
backups keep the seven newest ``data/backup/snowball-YYYY-MM-DD.db`` files.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


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


def _within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _prune_raw(conn: sqlite3.Connection, root: Path, cutoff_ts: float, today: dt.date, days: int) -> int:
    raw = root / "raw"
    if not raw.is_dir():
        return 0

    protected_names: set[str] = set()
    if _has_table(conn, "entities") and _has_table(conn, "pages"):
        cur = conn.execute(
            """
            SELECT p.raw_path
            FROM entities e
            JOIN pages p ON p.id = e.evidence_page_id
            WHERE e.reported_at IS NULL OR e.reported_at >= ?
            """,
            (int(cutoff_ts),),
        )
        for r in cur.fetchall():
            if r[0]:
                protected_names.add(Path(str(r[0])).name)

    if _has_table(conn, "entity_changes") and _has_table(conn, "pages"):
        cur = conn.execute(
            """
            SELECT p.raw_path
            FROM entity_changes c
            JOIN pages p ON p.id = c.page_id
            WHERE c.reported_at IS NULL OR c.reported_at >= ?
            """,
            (int(cutoff_ts),),
        )
        for r in cur.fetchall():
            if r[0]:
                protected_names.add(Path(str(r[0])).name)

    removed = 0
    for child in list(raw.iterdir()):
        if child.is_file():
            if child.name in protected_names:
                continue
            try:
                if child.stat().st_mtime < cutoff_ts and _within(root, child):
                    child.unlink()
                    removed += 1
            except OSError:
                pass
        elif child.is_dir() and _DATE_DIR.match(child.name):
            try:
                stamp = dt.date.fromisoformat(child.name)
                if stamp < (today - dt.timedelta(days=days)) and _within(root, child):
                    shutil.rmtree(child)
                    removed += 1
            except (ValueError, OSError):
                pass
    return removed

def _prune_old_files(root: Path, directory: Path, cutoff_ts: float) -> int:
    if not directory.is_dir() or not _within(root, directory):
        return 0
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(directory):
        for name in filenames:
            path = Path(dirpath) / name
            if not _within(root, path):
                continue
            try:
                if path.stat().st_mtime < cutoff_ts:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
    return removed


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


def _backup(conn: sqlite3.Connection, backup_dir: Path, day: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"snowball-{day}.db"
    temp_dest = backup_dir / f"snowball-{day}.tmp-{os.getpid()}-{int(time.time())}.db"
    if temp_dest.exists():
        temp_dest.unlink()
    _commit(conn)
    previous = conn.isolation_level
    sql_path = temp_dest.resolve().as_posix().replace("'", "''")
    conn.isolation_level = None
    try:
        conn.execute(f"VACUUM INTO '{sql_path}'")
    finally:
        conn.isolation_level = previous

    chk_conn = sqlite3.connect(str(temp_dest))
    chk = _quick_check(chk_conn)
    chk_conn.close()
    if chk != "ok":
        temp_dest.unlink(missing_ok=True)
        raise RuntimeError(f"Backup verification failed: {chk}")

    os.replace(temp_dest, dest)
    return dest


def _trim_backups(backup_dir: Path, keep: int = 7) -> tuple[int, int]:
    if keep < 1:
        keep = 1
    files = sorted(path for path in backup_dir.glob("snowball-*.db") if path.is_file())
    removed = 0
    while len(files) > keep:
        files[0].unlink()
        removed += 1
        files = files[1:]
    return len(files), removed

def _quick_check(conn: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    if rows == ["ok"]:
        return "ok"
    return "; ".join(rows) if rows else "ok"


def restore_backup(backup_path: Path, target_db_path: Path) -> dict[str, Any]:
    """Restore a SQLite backup to a target path, verify integrity, and query state counts."""
    src = Path(backup_path).resolve()
    dst = Path(target_db_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Backup file not found at {src}")
    if src == dst:
        raise ValueError(f"Source and target database cannot be the same path: {src}")

    src_conn = sqlite3.connect(str(src))
    src_chk = _quick_check(src_conn)
    src_conn.close()
    if src_chk != "ok":
        raise RuntimeError(f"Source backup corrupted: {src_chk}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.parent / f"{dst.name}.tmp-restore-{os.getpid()}"
    if tmp_dst.exists():
        tmp_dst.unlink()

    shutil.copy2(src, tmp_dst)
    conn = sqlite3.connect(str(tmp_dst))
    conn.row_factory = sqlite3.Row
    quick = _quick_check(conn)
    if quick != "ok":
        conn.close()
        tmp_dst.unlink(missing_ok=True)
        raise RuntimeError(f"Restored database integrity check failed: {quick}")

    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        conn.close()
        tmp_dst.unlink(missing_ok=True)
        raise RuntimeError(f"Foreign key integrity check failed: {len(fk_errors)} errors")

    entities = int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]) if _has_table(conn, "entities") else 0
    sources = int(conn.execute("SELECT COUNT(*) FROM entity_sources").fetchone()[0]) if _has_table(conn, "entity_sources") else 0
    pages = int(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]) if _has_table(conn, "pages") else 0
    claims = int(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]) if _has_table(conn, "claims") else 0
    conn.close()

    os.replace(tmp_dst, dst)
    return {
        "restored_from": str(src),
        "restored_to": str(dst),
        "quick_check": quick,
        "entities": entities,
        "sources": sources,
        "pages": pages,
        "claims": claims,
    }

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
    """Prune aged raw/quarantine/log files, expire deadlines, backup, and quick_check.

    ``wiki/``, the live database, and ``digests/`` are left in place.
    """
    root = Path(root_dir)
    now = _as_ist(now_dt)
    today = now.date()
    cutoff_14d = now.timestamp() - days_raw * 86400
    raw_pruned = _prune_raw(conn, root, cutoff_14d, today, days_raw)
    quarantine_pruned = _prune_old_files(
        root, root / "quarantine", now.timestamp() - days_quarantine * 86400
    ) + _prune_old_files(
        root, root / "raw" / "quarantine", now.timestamp() - days_quarantine * 86400
    )
    logs_pruned = _prune_old_files(root, root / "logs", now.timestamp() - days_logs * 86400)
    expired = _expire_deadlines(conn, today)
    bdir = Path(backup_dir) if backup_dir else (root / "data" / "backup")
    backup_path = _backup(conn, bdir, today.isoformat())
    backups_kept, backups_pruned = _trim_backups(bdir, keep=keep)
    quick = _quick_check(conn)
    return {
        "raw_pruned": raw_pruned,
        "quarantine_pruned": quarantine_pruned,
        "logs_pruned": logs_pruned,
        "expired": expired,
        "backup_path": str(backup_path),
        "backups": backups_kept,
        "backups_pruned": backups_pruned,
        "quick_check": quick,
    }


def main() -> None:
    import json

    ap = argparse.ArgumentParser(description="Snowball retention, backup, and expiry")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--keep", type=int, default=7, help="Number of backups to retain")
    ap.add_argument("--restore", type=Path, default=None, help="Backup file to restore into --db")
    ap.add_argument("--json", action="store_true", help="Output summary as JSON")
    args = ap.parse_args()

    if args.restore:
        stats = restore_backup(args.restore, args.db)
        if args.json:
            print(json.dumps(stats))
        else:
            print(
                f"Restored from {stats['restored_from']} to {stats['restored_to']} "
                f"quick_check={stats['quick_check']} entities={stats['entities']} pages={stats['pages']}"
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
    stats = run_retention(conn, root_dir=args.root, keep=max(1, args.keep))
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
