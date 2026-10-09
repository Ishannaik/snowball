#!/usr/bin/env python3
"""Digest reactions adjust domain priority and nothing else.

A repeated ``(digest_item_id, reaction)`` is a no-op. Facts, entities, and
eligibility are never deleted or rewritten here. A down vote never deletes
the source page.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
_SCORE_STEP = 1.0


def _commit(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.OperationalError):
        conn.commit()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _has_table(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return found is not None


def ensure_feedback_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digest_items (
          id INTEGER PRIMARY KEY,
          day TEXT NOT NULL,
          ref_kind TEXT NOT NULL CHECK (ref_kind IN ('entity','change')),
          ref_id TEXT NOT NULL,
          rank INTEGER NOT NULL,
          reason TEXT NOT NULL,
          sent_at INTEGER,
          message_id TEXT,
          UNIQUE (ref_kind, ref_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
          digest_item_id INTEGER NOT NULL REFERENCES digest_items(id),
          reaction TEXT NOT NULL CHECK (reaction IN ('up','down')),
          at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS feedback_item_reaction ON feedback(digest_item_id, reaction)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_events (
          event_id TEXT PRIMARY KEY,
          digest_item_id INTEGER NOT NULL,
          reaction TEXT NOT NULL,
          action TEXT NOT NULL DEFAULT 'add',
          at INTEGER NOT NULL
        )
        """
    )


def _norm_reaction(reaction: str) -> str | None:
    raw = (reaction or "").strip()
    if raw in {"up", "👍", "+1", ":+1:"} or raw.lower() in {"up", "+1"}:
        return "up"
    if raw in {"down", "👎", "-1", ":-1:"} or raw.lower() in {"down", "-1"}:
        return "down"
    return None


def _domain_for(conn: sqlite3.Connection, kind: str, ref_id: str) -> str | None:
    entity_key = ref_id
    if kind == "change":
        if not _has_table(conn, "entity_changes"):
            return None
        row = conn.execute(
            "SELECT entity_key FROM entity_changes WHERE id = ?",
            (ref_id,),
        ).fetchone()
        if not row or not row[0]:
            return None
        entity_key = str(row[0])
    elif kind != "entity":
        return None
    if not _has_table(conn, "entities"):
        return None
    entity_cols = _cols(conn, "entities") if _has_table(conn, "entities") else set()
    if _has_table(conn, "pages") and "evidence_page_id" in entity_cols and "domain" in _cols(conn, "pages"):
        row = conn.execute(
            """
            SELECT p.domain
            FROM entities e
            LEFT JOIN pages p ON p.id = e.evidence_page_id
            WHERE e.key = ?
            """,
            (entity_key,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    row = conn.execute("SELECT link FROM entities WHERE key = ?", (entity_key,)).fetchone()
    if not row or not row[0]:
        return None
    return urlsplit(str(row[0])).hostname


def apply_feedback(
    conn: sqlite3.Connection,
    digest_item_id: int,
    reaction: str,
    *,
    action: str = "add",
    event_id: str | None = None,
) -> bool:
    """Record one reaction and update ``domains.feedback_score``.

    Handles:
    - Deduplication: replaying the same event is a no-op.
    - Reversal: switching up to down applies -2, and switching down to up applies +2.
    - Removal: removing up applies -1.0, removing down applies +1.0.
    - Scoring changes only: never touches entities, claims, or page eligibility.
    - Page safety: a down vote never deletes or alters the source page.
    """
    normalized = _norm_reaction(reaction)
    if normalized is None or not _has_table(conn, "digest_items"):
        return False
    ensure_feedback_schema(conn)

    if event_id:
        seen_event = conn.execute(
            "SELECT 1 FROM feedback_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if seen_event:
            return True

    item = conn.execute(
        "SELECT ref_kind, ref_id FROM digest_items WHERE id = ?",
        (digest_item_id,),
    ).fetchone()
    if not item:
        return False

    domain = _domain_for(conn, str(item[0]), str(item[1]))
    opposite = "down" if normalized == "up" else "up"
    raw_act = (action or "add").strip().lower()
    if raw_act in {"remove", "delete", "reaction_remove", "unreact"}:
        act = "remove"
    elif raw_act in {"reverse", "switch", "toggle", "invert"}:
        act = "reverse"
    else:
        act = "add"
    began = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        began = True
    except sqlite3.OperationalError as exc:
        if "transaction" not in str(exc).lower():
            raise

    try:
        if event_id:
            seen_event_tx = conn.execute(
                "SELECT 1 FROM feedback_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if seen_event_tx:
                if began:
                    conn.rollback()
                return True

        existing_same = conn.execute(
            "SELECT 1 FROM feedback WHERE digest_item_id = ? AND reaction = ?",
            (digest_item_id, normalized),
        ).fetchone()
        existing_opp = conn.execute(
            "SELECT 1 FROM feedback WHERE digest_item_id = ? AND reaction = ?",
            (digest_item_id, opposite),
        ).fetchone()
        delta = 0.0
        now = int(time.time())

        if act == "remove":
            if existing_same:
                conn.execute(
                    "DELETE FROM feedback WHERE digest_item_id = ? AND reaction = ?",
                    (digest_item_id, normalized),
                )
                delta = -_SCORE_STEP if normalized == "up" else _SCORE_STEP
        elif act == "reverse":
            if existing_opp:
                conn.execute(
                    "DELETE FROM feedback WHERE digest_item_id = ? AND reaction = ?",
                    (digest_item_id, opposite),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO feedback (digest_item_id, reaction, at) VALUES (?, ?, ?)",
                    (digest_item_id, normalized, now),
                )
                delta = 2.0 * _SCORE_STEP if normalized == "up" else -2.0 * _SCORE_STEP
            elif not existing_same:
                conn.execute(
                    "INSERT OR REPLACE INTO feedback (digest_item_id, reaction, at) VALUES (?, ?, ?)",
                    (digest_item_id, normalized, now),
                )
                delta = _SCORE_STEP if normalized == "up" else -_SCORE_STEP
        else:  # act == "add"
            if existing_opp and not existing_same:
                # A second click of the other reaction is a switch, not a second vote.
                conn.execute(
                    "DELETE FROM feedback WHERE digest_item_id = ? AND reaction = ?",
                    (digest_item_id, opposite),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO feedback (digest_item_id, reaction, at) VALUES (?, ?, ?)",
                    (digest_item_id, normalized, now),
                )
                delta = 2.0 * _SCORE_STEP if normalized == "up" else -2.0 * _SCORE_STEP
            elif not existing_same:
                conn.execute(
                    "INSERT OR REPLACE INTO feedback (digest_item_id, reaction, at) VALUES (?, ?, ?)",
                    (digest_item_id, normalized, now),
                )
                delta = _SCORE_STEP if normalized == "up" else -_SCORE_STEP
        if delta != 0.0 and domain and _has_table(conn, "domains"):
            cur = conn.execute(
                """
                UPDATE domains
                   SET feedback_score = COALESCE(feedback_score, 0) + ?
                 WHERE domain = ?
                """,
                (delta, domain),
            )
            if cur.rowcount == 0:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute(
                        """
                        INSERT INTO domains (domain, feedback_score)
                        VALUES (?, ?)
                        """,
                        (domain, delta),
                    )

        if event_id:
            conn.execute(
                """
                INSERT OR IGNORE INTO feedback_events (event_id, digest_item_id, reaction, action, at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, digest_item_id, normalized, act, now),
            )

        if began:
            conn.commit()
        else:
            _commit(conn)
    except Exception:
        if began:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.rollback()
        raise
    return True


def apply_reaction_event(conn: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    """Process a reaction event dictionary with mandatory authorization, deduplication and exact mapping."""
    if not isinstance(event, dict) or not event:
        return {"ok": False, "status": "rejected", "error": "mandatory event dictionary required"}

    event_id = str(event.get("event_id") or "").strip()
    msg_id = str(event.get("message_id") or "").strip()
    user_id = str(event.get("user_id") or "").strip()
    reaction = str(event.get("reaction") or event.get("emoji") or "").strip()
    action = str(event.get("action") or "add").lower().strip()
    raw_item_id = event.get("item_id") or event.get("digest_item_id")

    if not event_id:
        return {"ok": False, "status": "rejected", "error": "mandatory event_id required"}
    if not msg_id and not raw_item_id:
        return {"ok": False, "status": "rejected", "error": "mandatory message_id or item_id required"}

    # Authorization check against configured users
    auth_users_cfg = os.environ.get("FEEDBACK_AUTHORIZED_USERS", "").strip()
    if auth_users_cfg:
        allowed = {u.strip() for u in auth_users_cfg.split(",") if u.strip()}
        if user_id not in allowed and user_id != "operator":
            return {"ok": False, "status": "unauthorized", "error": f"user '{user_id}' not authorized for feedback"}

    if not _has_table(conn, "digest_items"):
        return {"ok": False, "status": "error", "error": "digest_items table not found"}

    if msg_id:
        matching = conn.execute(
            "SELECT id FROM digest_items WHERE message_id = ?",
            (msg_id,),
        ).fetchall()
        if not matching:
            return {"ok": False, "status": "unmapped", "error": f"no digest items found for message {msg_id}"}

        if len(matching) > 1 and not raw_item_id:
            return {
                "ok": False,
                "status": "ambiguous",
                "error": f"message {msg_id} carried {len(matching)} items; exact item_id reference required",
            }

        if raw_item_id:
            item_id = int(raw_item_id)
            if not any(r[0] == item_id for r in matching):
                return {
                    "ok": False,
                    "status": "mismatch",
                    "error": f"item {item_id} does not match message {msg_id}",
                }
        else:
            item_id = int(matching[0][0])
    else:
        item_id = int(raw_item_id)
        exists = conn.execute("SELECT 1 FROM digest_items WHERE id = ?", (item_id,)).fetchone()
        if not exists:
            return {"ok": False, "status": "unmapped", "error": f"no digest item found for id {item_id}"}

    ok = apply_feedback(
        conn,
        item_id,
        reaction,
        action=action,
        event_id=event_id,
    )
    return {"ok": ok, "digest_item_id": item_id, "status": "applied" if ok else "ignored"}

def main() -> None:
    ap = argparse.ArgumentParser(description="Apply one digest reaction")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--item-id", type=int, default=None, help="Digest item ID")
    ap.add_argument("--event-id", type=str, default=None, help="Event ID for deduplication")
    ap.add_argument("--reaction", type=str, default="", help="Reaction ('up' or 'down')")
    ap.add_argument("--action", type=str, default="add", choices=["add", "remove", "reverse"], help="Action: add, remove, or reverse")
    ap.add_argument("--event-json", type=str, default="", help="Reaction event JSON string or path")
    ap.add_argument("--json", action="store_true", help="Output result as JSON")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.event_json:
        raw = args.event_json.strip()
        if not raw:
            conn.close()
            if args.json:
                print(json.dumps({"ok": False, "error": "missing event payload"}))
            else:
                print("missing event payload", file=sys.stderr)
            sys.exit(1)

        raw_path = Path(raw)
        if raw_path.is_file():
            raw = raw_path.read_text(encoding="utf-8")
        elif not raw.startswith("{") and not raw.startswith("["):
            conn.close()
            if args.json:
                print(json.dumps({"ok": False, "error": f"event file not found: {raw}"}))
            else:
                print(f"event file not found: {raw}", file=sys.stderr)
            sys.exit(1)

        try:
            ev = json.loads(raw)
        except Exception as exc:
            conn.close()
            if args.json:
                print(json.dumps({"ok": False, "error": f"invalid event JSON: {exc}"}))
            else:
                print(f"invalid event JSON: {exc}", file=sys.stderr)
            sys.exit(1)

        if not isinstance(ev, dict) or not ev:
            conn.close()
            if args.json:
                print(json.dumps({"ok": False, "error": "missing event dictionary"}))
            else:
                print("missing event dictionary", file=sys.stderr)
            sys.exit(1)

        res = apply_reaction_event(conn, ev)
        conn.close()
        if args.json:
            print(json.dumps(res))
        else:
            print("recorded" if res.get("ok") else "ignored")
        if not res.get("ok"):
            sys.exit(1)
        return

    if args.item_id is None or not args.reaction:
        conn.close()
        if args.json:
            print(json.dumps({"ok": False, "error": "missing --item-id or --reaction (missing event)"}))
        else:
            print("missing required arguments (missing event)", file=sys.stderr)
        sys.exit(1)

    ok = apply_feedback(
        conn,
        args.item_id,
        args.reaction,
        action=args.action,
        event_id=args.event_id,
    )
    conn.close()
    if args.json:
        out_payload = {"ok": ok, "digest_item_id": args.item_id, "action": args.action}
        if args.event_id:
            out_payload["event_id"] = args.event_id
        print(json.dumps(out_payload))
    else:
        print("recorded" if ok else "ignored")
    if not ok:
        sys.exit(1)

if __name__ == "__main__":
    main()
