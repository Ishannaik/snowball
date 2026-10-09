#!/usr/bin/env python3
"""Once-only urgent alerts for deadlines inside seven days.

``alerted_at`` is set only after a transport returns a message id. A dead
source link is replaced with the hub URL and ``entities.link_dead`` is set.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import os
import sqlite3
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"


def _sibling(name: str):
    mod_name = f"snowball_alerts_{name}"
    spec = importlib.util.spec_from_file_location(
        mod_name, Path(__file__).resolve().parent / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Python 3.13 dataclasses look up the module in sys.modules during class creation.
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _commit(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.OperationalError):
        conn.commit()


def _fetch(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description or []]
    rows: list[dict[str, Any]] = []
    for row in cur.fetchall():
        if isinstance(row, sqlite3.Row):
            rows.append({key: row[key] for key in names})
        else:
            rows.append(dict(zip(names, row, strict=False)))
    return rows


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return found is not None


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_link_dead(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "entities"):
        return
    if "link_dead" in _cols(conn, "entities"):
        return
    conn.execute("ALTER TABLE entities ADD COLUMN link_dead INTEGER NOT NULL DEFAULT 0")


def _as_of(as_of_date: str | None) -> dt.date:
    if as_of_date:
        return dt.date.fromisoformat(as_of_date[:10])
    return dt.date.today()


def _http_status(url: str, method: str, extra_headers: dict[str, str] | None = None) -> int | None:
    headers = {"User-Agent": "Snowball/1.0"}
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def _probe_link(url: str) -> bool:
    """True when the URL answers outside 404/410. 405 and 403 fall back to a ranged GET."""
    if not url or not str(url).startswith(("http://", "https://")):
        return False
    status = _http_status(url, "HEAD")
    if status in (405, 403):
        status = _http_status(url, "GET", {"Range": "bytes=0-1023"})
    if status is None or status in (404, 410):
        return False
    return 200 <= status < 400


def _hub_url(conn: sqlite3.Connection, entity: dict[str, Any]) -> str:
    if not _has_table(conn, "pages"):
        return ""
    key = entity.get("key")
    if key and _has_table(conn, "entity_sources"):
        row = conn.execute(
            """
            SELECT p.canon_url
            FROM entity_sources es
            JOIN pages p ON p.id = es.page_id
            WHERE es.entity_key = ? AND p.kind = 'hub'
            ORDER BY es.seen_at DESC
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    page_id = entity.get("evidence_page_id")
    if page_id:
        row = conn.execute("SELECT canon_url FROM pages WHERE id = ?", (page_id,)).fetchone()
        if row and row[0]:
            return str(row[0])
    link = str(entity.get("link") or "")
    return link


def prepare_urgent_alerts(
    conn: sqlite3.Connection, as_of_date: str | None = None
) -> list[dict[str, Any]]:
    """Entities with a live deadline inside 7 days, relevance >= 0.5, and no prior alert."""
    if not _has_table(conn, "entities"):
        return []
    cols = _cols(conn, "entities")
    needed = {"key", "deadline", "relevance", "alerted_at"}
    if not needed <= cols:
        return []
    today = _as_of(as_of_date)
    wanted = [
        "key",
        "name",
        "org",
        "kind",
        "deadline",
        "relevance",
        "link",
        "evidence_quote",
        "evidence_page_id",
        "alerted_at",
    ]
    pick = [col for col in wanted if col in cols]
    rows = _fetch(
        conn,
        f"""
        SELECT {", ".join(pick)}
        FROM entities
        WHERE alerted_at IS NULL
          AND relevance >= 0.5
          AND deadline IS NOT NULL
          AND TRIM(deadline) != ''
          AND evidence_quote IS NOT NULL
          AND TRIM(evidence_quote) != ''
        ORDER BY deadline, key
        """,
    )
    digest = _sibling("digest")
    alerts: list[dict[str, Any]] = []
    for row in rows:
        try:
            deadline = dt.date.fromisoformat(str(row["deadline"])[:10])
        except ValueError:
            continue
        days = (deadline - today).days
        if days < 0 or days > 7:
            continue
        item = dict(row)
        quote = str(item.get("evidence_quote") or "").strip()
        if not quote or not digest._verify_source_quote(conn, quote, item.get("evidence_page_id")):
            continue
        item["days_left"] = days
        item["hub_url"] = _hub_url(conn, item)
        alerts.append(item)
    return alerts

def _alert_text(item: dict[str, Any], link: str, link_dead: bool) -> str:
    quote = str(item.get("evidence_quote") or "").strip()
    lines = [
        f"🚨 Urgent: {item.get('name') or item.get('key')} ({item.get('org') or 'unknown'})",
        f"Deadline: {item.get('deadline')} ({item.get('days_left')}d left)",
        f"Relevance: {item.get('relevance')}",
        f"Link: {link}",
    ]
    if link_dead:
        lines.append("Source link is dead; using hub fallback.")
    if quote:
        lines.append(f'> "{quote}"')
    return "\n".join(lines)


def send_urgent_alerts(
    conn: sqlite3.Connection, channel_id: str = "", as_of_date: str | None = None
) -> dict[str, int]:
    """Deliver each prepared alert once. ``alerted_at`` sticks only after a real message id."""
    alerts = prepare_urgent_alerts(conn, as_of_date=as_of_date)
    channel = (channel_id or os.environ.get("DIGEST_CHANNEL_ID", "")).strip()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    sent = 0
    failed = 0
    dead = 0
    if not channel and not webhook:
        return {"prepared": len(alerts), "sent": 0, "failed": len(alerts), "link_dead": 0}
    digest = _sibling("digest")
    for item in alerts:
        row = conn.execute(
            "SELECT alerted_at FROM entities WHERE key = ?",
            (item["key"],),
        ).fetchone()
        if not row or row[0] is not None:
            continue

        link = str(item.get("link") or "")
        alive = _probe_link(link) if link else False
        if not alive:
            _ensure_link_dead(conn)
            conn.execute("UPDATE entities SET link_dead = 1 WHERE key = ?", (item["key"],))
            _commit(conn)
            dead += 1
            fallback = str(item.get("hub_url") or "")
            if fallback:
                link = fallback
        text = _alert_text(item, link, not alive)
        alert_nonce = f"alert_{item['key'][:15]}"[:25]
        try:
            message_id = digest._discord_post(channel, text, nonce=alert_nonce)
        except Exception:
            failed += 1
            continue
        if (
            not message_id
            or not str(message_id).strip()
            or str(message_id).strip() in ("0", "None", "null", "undefined")
        ):
            failed += 1
            continue
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        cur = conn.execute(
            "UPDATE entities SET alerted_at = ? WHERE key = ? AND alerted_at IS NULL",
            (now, item["key"]),
        )
        _commit(conn)
        if cur.rowcount:
            sent += 1
        else:
            failed += 1
    return {"prepared": len(alerts), "sent": sent, "failed": failed, "link_dead": dead}

def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball urgent deadline alerts")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--channel", type=str, default="")
    ap.add_argument("--post", action="store_true", help="Post alerts to Discord")
    ap.add_argument("--as-of", type=str, default="")
    ap.add_argument("--json", action="store_true", help="Output summary as JSON")
    args = ap.parse_args()
    if not args.db.exists():
        print(f"Database not found at {args.db}.")
        return
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if args.post:
        stats = send_urgent_alerts(conn, channel_id=args.channel, as_of_date=args.as_of or None)
    else:
        alerts = prepare_urgent_alerts(conn, as_of_date=args.as_of or None)
        stats = {"prepared": len(alerts), "sent": 0, "failed": 0, "link_dead": 0}
    conn.close()
    if args.json:
        print(json.dumps(stats))
    else:
        print(
            f"Urgent alerts prepared={stats['prepared']} sent={stats['sent']} "
            f"failed={stats['failed']} link_dead={stats['link_dead']}"
        )


if __name__ == "__main__":
    main()
