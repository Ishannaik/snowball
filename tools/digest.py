#!/usr/bin/env python3
"""Snowball digest preparation, delivery, and acknowledgement.

``reported_at`` is written only by ``acknowledge_digest``, after Discord returns
a message id. A failed send leaves the rows unreported.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import ipaddress
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
CONFIG_PATH = ROOT / "config.env"
DIGEST_DIR = ROOT / "digests"
DISCORD_LIMIT = 2000
ITEM_LIMIT = 25
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class DeliveryFailure(Exception):
    """Discord did not accept the message, or the channel/token was missing."""


@dataclass
class PreparedDigest:
    delivery_key: str
    day: str
    item_refs: tuple[tuple[str, str], ...]
    text: str
    chunk_index: int


@dataclass
class DeliveryAck:
    delivery_key: str
    channel_id: str
    message_id: str
    sent_at: int


def get_channel_id() -> str:
    """Retrieve DIGEST_CHANNEL_ID from config.env or environment."""
    ch = os.environ.get("DIGEST_CHANNEL_ID", "").strip()
    if not ch and CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DIGEST_CHANNEL_ID="):
                ch = line.split("=", 1)[1].strip().strip("'\"")
    return ch

def get_discord_bot_token() -> str:
    """Safely retrieve Discord bot token from environment or configured token file.

    Does not log or expose the token.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    token_file = os.environ.get("DISCORD_TOKEN_FILE", "").strip()
    if not token_file and CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DISCORD_TOKEN_FILE="):
                token_file = line.split("=", 1)[1].strip().strip("'\"")
    if token_file:
        p = Path(token_file)
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("DISCORD_BOT_TOKEN="):
                        return line.split("=", 1)[1].strip().strip("'\"")
                    if not token and line and not line.startswith("#") and "=" not in line:
                        token = line
                if token:
                    return token
            except Exception:
                pass
    return ""

DENY_NETS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "::ffff:0:0/96",
    "64:ff9b::/96",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)
_DENY_OBJS = [ipaddress.ip_network(n) for n in DENY_NETS]


def is_ssrf_safe_url(url: str) -> bool:
    """Verify that URL uses public HTTP(S) on allowed ports 80/443 without hitting private/denied nets."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host or port not in (80, 443):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = {ipaddress.ip_address(i[4][0]) for i in infos}
        if not ips:
            return False
        for ip in ips:
            if any(ip in n for n in _DENY_OBJS):
                return False
        return True
    except Exception:
        return False


def _verify_source_quote(conn: sqlite3.Connection, quote: str, page_id: Any) -> bool:
    """Validate that evidence quote is non-empty and present in raw source text."""
    if not quote or not str(quote).strip():
        return False
    q = re.sub(r"\s+", " ", str(quote)).strip().casefold()
    if not q:
        return False
    if not page_id or not _has_table(conn, "pages"):
        return True
    page_cols = _cols(conn, "pages")
    if "raw_path" not in page_cols:
        return True
    row = conn.execute("SELECT raw_path FROM pages WHERE id = ?", (page_id,)).fetchone()
    if not row or not row[0]:
        return True
    raw_p = ROOT / str(row[0])
    if not raw_p.is_file():
        return True
    try:
        content = raw_p.read_text(encoding="utf-8", errors="replace")
        norm_content = re.sub(r"\s+", " ", content).casefold()
        return q in norm_content
    except OSError:
        return True


def _http_status(url: str, method: str, extra_headers: dict[str, str] | None = None) -> int | None:
    headers = {"User-Agent": "Snowball/1.0"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def _probe_link(url: str) -> bool:
    """True when the URL answers outside 404/410. Pinned SSRF-safe checks enforced first."""
    if not url or not str(url).startswith(("http://", "https://")):
        return False
    if not is_ssrf_safe_url(url):
        return False
    status = _http_status(url, "HEAD")
    if status in (405, 403):
        status = _http_status(url, "GET", {"Range": "bytes=0-1023"})
    if status is None or status in (404, 410):
        return False
    return 200 <= status < 400


def _hub_url(conn: sqlite3.Connection | None, row: dict[str, Any]) -> str:
    if conn is None or not _has_table(conn, "pages"):
        return ""
    key = row.get("key") or row.get("entity_key")
    if key and _has_table(conn, "entity_sources"):
        r = conn.execute(
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
        if r and r[0]:
            candidate = str(r[0])
            if _probe_link(candidate):
                return candidate
    page_id = row.get("evidence_page_id")
    if page_id and _has_table(conn, "pages"):
        r = conn.execute(
            """
            SELECT p_hub.canon_url
            FROM pages p
            JOIN pages p_hub ON p_hub.id = p.parent_page_id
            WHERE p.id = ? AND p_hub.kind = 'hub'
            LIMIT 1
            """,
            (page_id,),
        ).fetchone()
        if r and r[0]:
            candidate = str(r[0])
            if _probe_link(candidate):
                return candidate
    return ""
def compute_urgency_badge(deadline_str: str | None) -> tuple[str, int]:
    """Calculate days to deadline and format urgency badge."""
    if not deadline_str:
        return ("Rolling", 999)
    try:
        dl = dt.date.fromisoformat(deadline_str[:10])
        today = dt.date.today()
        days = (dl - today).days
        if days < 0:
            return ("Expired", -1)
        if days <= 7:
            return (f"🚨 URGENT ({days}d left)", days)
        if days <= 30:
            return (f"⏳ {days}d left", days)
        return (f"📅 {dl.isoformat()}", days)
    except Exception:
        return (deadline_str, 999)


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


def ensure_digest_schema(conn: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS digest_outbox (
          delivery_key TEXT PRIMARY KEY,
          day TEXT NOT NULL,
          chunk_index INTEGER NOT NULL,
          item_refs TEXT NOT NULL,
          text TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'prepared',
          message_id TEXT,
          sent_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_items (
          id INTEGER PRIMARY KEY,
          day TEXT NOT NULL,
          ref_kind TEXT NOT NULL,
          ref_id TEXT NOT NULL,
          rank INTEGER NOT NULL,
          reason TEXT NOT NULL,
          sent_at INTEGER,
          message_id TEXT,
          UNIQUE (day, ref_kind, ref_id)
        )
        """
    )


def _float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _select_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if _has_table(conn, "entities"):
        cols = _cols(conn, "entities")
        if "key" in cols and "reported_at" in cols:
            wanted = [
                "key",
                "name",
                "org",
                "kind",
                "amount",
                "currency",
                "deadline",
                "eligibility",
                "link",
                "evidence_quote",
                "relevance",
                "urgency",
                "evidence_page_id",
                "link_dead",
            ]
            pick = [col for col in wanted if col in cols]
            rows = _fetch(
                conn,
                f"""
                SELECT {", ".join(pick)}
                FROM entities
                WHERE (reported_at IS NULL OR reported_at = 0)
                  AND evidence_quote IS NOT NULL
                  AND TRIM(evidence_quote) != ''
                """,
            )
            for row in rows:
                ev_page = row.get("evidence_page_id")
                quote = str(row.get("evidence_quote") or "").strip()
                if not _verify_source_quote(conn, quote, ev_page):
                    continue
                relevance = _float(row.get("relevance"), 0.5)
                urgency = _float(row.get("urgency"), 0.0)
                deadline = row.get("deadline")
                due = f" due {deadline}" if deadline else ""
                kind = row.get("kind") or "opportunity"
                items.append(
                    {
                        "ref_kind": "entity",
                        "ref_id": str(row["key"]),
                        "score": relevance * (1.0 + urgency),
                        "reason": f"new: {kind}{due}",
                        "row": row,
                    }
                )
        change_cols = _cols(conn, "entity_changes")
        entity_cols = _cols(conn, "entities")
        if "id" in change_cols and "entity_key" in change_cols and "reported_at" in change_cols:
            page_expr = (
                "e.evidence_page_id AS evidence_page_id"
                if "evidence_page_id" in entity_cols
                else "NULL AS evidence_page_id"
            )
            rows = _fetch(
                conn,
                f"""
                SELECT c.id AS id, c.entity_key AS entity_key, c.field AS field,
                       c."old" AS old, c."new" AS new, c.evidence_quote AS evidence_quote,
                       e.name AS name, e.org AS org, e.link AS link, e.deadline AS deadline,
                       e.kind AS kind, e.relevance AS relevance, e.evidence_quote AS entity_quote,
                       {page_expr}
                FROM entity_changes c
                JOIN entities e ON e.key = c.entity_key
                WHERE (c.reported_at IS NULL OR c.reported_at = 0)
                  AND (
                    (c.evidence_quote IS NOT NULL AND TRIM(c.evidence_quote) != '')
                    OR (e.evidence_quote IS NOT NULL AND TRIM(e.evidence_quote) != '')
                  )
                """,
            )
            for row in rows:
                quote = str(row.get("evidence_quote") or "").strip()
                if not quote:
                    quote = str(row.get("entity_quote") or "").strip()
                if not quote or not _verify_source_quote(conn, quote, row.get("evidence_page_id")):
                    continue
                relevance = 0.5 if "relevance" not in entity_cols else _float(row.get("relevance"), 0.5)
                old = row.get("old")
                old_text = "∅" if old is None or old == "" else old
                field = row.get("field") or "field"
                items.append(
                    {
                        "ref_kind": "change",
                        "ref_id": str(row["id"]),
                        "score": relevance * 1.2,
                        "reason": f"{field}: {old_text} → {row.get('new')}",
                        "row": row,
                    }
                )
    items.sort(key=lambda item: (-float(item["score"]), str(item["ref_kind"]), str(item["ref_id"])))
    return items[:ITEM_LIMIT]


def _amount(row: dict[str, Any]) -> str:
    amount = row.get("amount")
    currency = str(row.get("currency") or "").strip()
    if amount is None or amount == "":
        return "Variable Amount"
    if isinstance(amount, float) and amount.is_integer():
        amount = int(amount)
    return f"{currency} {amount}".strip() or "Variable Amount"


def _format_item(item: dict[str, Any], rank: int, conn: sqlite3.Connection | None = None) -> str:
    row = item["row"]
    link = str(row.get("link") or "").strip()
    link_dead = bool(row.get("link_dead"))
    if not link_dead and conn and link:
        if not _probe_link(link):
            link_dead = True
            key = row.get("key") or row.get("entity_key")
            if key and _has_table(conn, "entities") and "link_dead" in _cols(conn, "entities"):
                conn.execute("UPDATE entities SET link_dead = 1 WHERE key = ?", (key,))
                _commit(conn)
    hub = _hub_url(conn, row) if conn and (link_dead or not link) else ""
    if item["ref_kind"] == "entity":
        badge, _days = compute_urgency_badge(row.get("deadline"))
        lines = [
            f"### {row.get('name') or 'Untitled'} ({row.get('org') or 'unknown'}) — {_amount(row)}",
            f"- **Rank:** {rank} — {item['reason']} — {badge}",
        ]
        if link_dead and hub:
            lines.append(f"- **Source / Apply:** [Hub: {hub}]({hub}) *(source link dead)*")
        elif link:
            lines.append(f"- **Source / Apply:** [{link}]({link})")
        quote = str(row.get("evidence_quote") or "").strip()
        if quote:
            lines.append(f'> "{quote}"')
        return "\n".join(lines)
    lines = [
        f"### Update: {row.get('name') or row.get('entity_key')} ({row.get('org') or 'unknown'})",
        f"- **Rank:** {rank}",
        f"- **Reason:** {item['reason']}",
        f"- **Change:** `{row.get('field')}` from `{row.get('old')}` → `{row.get('new')}`",
    ]
    if link_dead and hub:
        lines.append(f"- **Source / Apply:** [Hub: {hub}]({hub}) *(source link dead)*")
    elif link:
        lines.append(f"- **Source / Apply:** [{link}]({link})")
    quote = str(row.get("evidence_quote") or row.get("entity_quote") or "").strip()
    if quote:
        lines.append(f'> "{quote}"')
    return "\n".join(lines)
def _fit_block(block: str, limit: int) -> str:
    if len(block) <= limit:
        return block
    cut = block[:limit]
    newline = cut.rfind("\n")
    if newline > limit // 2:
        cut = cut[:newline]
    return cut.rstrip()


def _chunk_header(day: str, index: int) -> str:
    title = f"# ❄️ Snowball Research Digest — {day}"
    if index:
        title += f" (part {index + 1})"
    return title + "\n\n"


def _pack(
    day: str,
    blocks: list[dict[str, Any]],
    title_prefix: str = "# ❄️ Snowball Research Digest — ",
) -> list[dict[str, Any]]:
    packed: list[dict[str, Any]] = []
    if not blocks:
        return packed

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    def header_for(idx: int) -> str:
        h = f"{title_prefix}{day}"
        if idx > 0:
            h += f" (part {idx + 1})"
        return h + "\n\n"

    for block in blocks:
        # Block item if its minimal text alone exceeds limit, never split quote or item
        if len(header_for(0) + block["text"]) > DISCORD_LIMIT:
            continue
        candidate = [*current, block]
        cand_header = header_for(len(chunks))
        cand_text = cand_header + "\n\n".join(b["text"] for b in candidate)
        if current and len(cand_text) > DISCORD_LIMIT:
            chunks.append(current)
            current = [block]
        else:
            current = candidate
    if current:
        chunks.append(current)

    for index, parts in enumerate(chunks):
        header = header_for(index)
        text = header + "\n\n".join(part["text"] for part in parts)
        refs = tuple((part["ref_kind"], part["ref_id"]) for part in parts)
        packed.append({"chunk_index": index, "text": text, "parts": parts, "refs": refs})
    return packed

def _delivery_key(day: str, chunk_index: int, refs: tuple[tuple[str, str], ...]) -> str:
    payload = json.dumps(
        {"day": day, "chunk": chunk_index, "refs": list(refs)},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # Discord nonces are at most 25 characters. The key must equal the nonce so
    # reconciliation cannot match a different delivery that shares a prefix.
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:25]


def _write_digest_file(day: str, text: str, *, prefix: str = "") -> Path:
    if not _DAY.fullmatch(day):
        raise ValueError("day must be YYYY-MM-DD")
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}-{day}.md" if prefix else f"{day}.md"
    path = DIGEST_DIR / name
    path.write_text(text, encoding="utf-8")
    return path


def prepare_digest(conn: sqlite3.Connection, day: str) -> list[PreparedDigest]:
    """Rank unreported rows, chunk them under the Discord limit, and archive the day."""
    if not _DAY.fullmatch(day):
        raise ValueError("day must be YYYY-MM-DD")
    ensure_digest_schema(conn)
    selected = _select_items(conn)
    if not selected:
        _write_digest_file(
            day,
            f"# ❄️ Snowball Research Digest — {day}\n\n"
            "*No new unnotified opportunities discovered since last digest.*\n",
        )
        _commit(conn)
        return []

    blocks: list[dict[str, Any]] = []
    for rank, item in enumerate(selected, start=1):
        blocks.append(
            {
                "ref_kind": item["ref_kind"],
                "ref_id": item["ref_id"],
                "rank": rank,
                "reason": item["reason"],
                "text": _format_item(item, rank, conn=conn),
            }
        )
    packed = _pack(day, blocks)
    prepared: list[PreparedDigest] = []
    archive_parts: list[str] = []
    for chunk in packed:
        key = _delivery_key(day, chunk["chunk_index"], chunk["refs"])
        meta = [
            {
                "ref_kind": part["ref_kind"],
                "ref_id": part["ref_id"],
                "rank": part["rank"],
                "reason": part["reason"],
            }
            for part in chunk["parts"]
        ]
        conn.execute(
            """
            INSERT INTO digest_outbox (delivery_key, day, chunk_index, item_refs, text, state)
            VALUES (?, ?, ?, ?, ?, 'prepared')
            ON CONFLICT(delivery_key) DO UPDATE SET
                day = excluded.day,
                chunk_index = excluded.chunk_index,
                item_refs = excluded.item_refs,
                text = excluded.text
            WHERE digest_outbox.state != 'acked'
            """,
            (key, day, chunk["chunk_index"], json.dumps(meta, ensure_ascii=False), chunk["text"]),
        )
        prepared.append(
            PreparedDigest(
                delivery_key=key,
                day=day,
                item_refs=chunk["refs"],
                text=chunk["text"],
                chunk_index=chunk["chunk_index"],
            )
        )
        archive_parts.append(chunk["text"])
    _write_digest_file(day, "\n\n".join(archive_parts) + "\n")
    _commit(conn)
    return prepared

def prepare_weekly_digest(conn: sqlite3.Connection, day: str) -> list[PreparedDigest]:
    """Select verified new/material changes from the last 7 days and format weekly briefing."""
    if not _DAY.fullmatch(day):
        raise ValueError("day must be YYYY-MM-DD")
    ensure_digest_schema(conn)
    items: list[dict[str, Any]] = []
    if _has_table(conn, "entities"):
        cols = _cols(conn, "entities")
        pick = [
            c
            for c in (
                "key",
                "name",
                "org",
                "kind",
                "amount",
                "currency",
                "deadline",
                "eligibility",
                "link",
                "evidence_quote",
                "evidence_page_id",
                "relevance",
                "urgency",
            )
            if c in cols
        ]
        rows = _fetch(
            conn,
            f"""
            SELECT {", ".join(pick)}
            FROM entities
            WHERE evidence_quote IS NOT NULL AND TRIM(evidence_quote) != ''
            ORDER BY relevance DESC, urgency DESC
            LIMIT 25
            """,
        )
        for r in rows:
            quote = str(r.get("evidence_quote") or "").strip()
            if not quote or not _verify_source_quote(conn, quote, r.get("evidence_page_id")):
                continue
            items.append(
                {
                    "ref_kind": "entity",
                    "ref_id": str(r["key"]),
                    "reason": f"weekly: {r.get('kind') or 'opportunity'}",
                    "row": r,
                }
            )
    if _has_table(conn, "entity_changes") and _has_table(conn, "entities"):
        page_expr = (
            "e.evidence_page_id AS evidence_page_id"
            if "evidence_page_id" in _cols(conn, "entities")
            else "NULL AS evidence_page_id"
        )
        rows = _fetch(
            conn,
            f"""
            SELECT c.id AS id, c.entity_key AS entity_key, c.field AS field,
                   c."old" AS old, c."new" AS new, c.evidence_quote AS evidence_quote,
                   e.name AS name, e.org AS org, e.link AS link, e.deadline AS deadline,
                   e.kind AS kind, e.relevance AS relevance, e.evidence_quote AS entity_quote,
                   {page_expr}
            FROM entity_changes c
            JOIN entities e ON e.key = c.entity_key
            WHERE (
              (c.evidence_quote IS NOT NULL AND TRIM(c.evidence_quote) != '')
              OR (e.evidence_quote IS NOT NULL AND TRIM(e.evidence_quote) != '')
            )
            ORDER BY c.seen_at DESC
            LIMIT 15
            """,
        )
        for r in rows:
            quote = str(r.get("evidence_quote") or "").strip()
            if not quote:
                quote = str(r.get("entity_quote") or "").strip()
            if not quote or not _verify_source_quote(conn, quote, r.get("evidence_page_id")):
                continue
            old_val = r.get("old") or "∅"
            items.append(
                {
                    "ref_kind": "change",
                    "ref_id": str(r["id"]),
                    "reason": f"material change: {r.get('field')} {old_val} → {r.get('new')}",
                    "row": r,
                }
            )
    blocks = [
        {
            "ref_kind": item["ref_kind"],
            "ref_id": item["ref_id"],
            "rank": rank,
            "reason": item["reason"],
            "text": _format_item(item, rank, conn=conn),
        }
        for rank, item in enumerate(items[:ITEM_LIMIT], start=1)
    ]
    packed = _pack(day, blocks, title_prefix="# ❄️ Snowball Weekly Research Briefing — ")
    prepared: list[PreparedDigest] = []
    archive_parts: list[str] = []
    for chunk in packed:
        weekly_day = f"weekly-{day}"
        key = _delivery_key(weekly_day, chunk["chunk_index"], chunk["refs"])
        meta = [
            {
                "ref_kind": part["ref_kind"],
                "ref_id": part["ref_id"],
                "rank": part["rank"],
                "reason": part["reason"],
            }
            for part in chunk["parts"]
        ]
        conn.execute(
            """
            INSERT INTO digest_outbox (delivery_key, day, chunk_index, item_refs, text, state)
            VALUES (?, ?, ?, ?, ?, 'prepared')
            ON CONFLICT(delivery_key) DO UPDATE SET
                day = excluded.day,
                chunk_index = excluded.chunk_index,
                item_refs = excluded.item_refs,
                text = excluded.text
            WHERE digest_outbox.state != 'acked'
            """,
            (key, weekly_day, chunk["chunk_index"], json.dumps(meta, ensure_ascii=False), chunk["text"]),
        )
        prepared.append(
            PreparedDigest(
                delivery_key=key,
                day=weekly_day,
                item_refs=chunk["refs"],
                text=chunk["text"],
                chunk_index=chunk["chunk_index"],
            )
        )
        archive_parts.append(chunk["text"])
    _write_digest_file(day, "\n\n".join(archive_parts) + "\n", prefix="weekly")
    _commit(conn)
    return prepared


def _discord_post(channel_id: str, content: str, nonce: str | None = None) -> str:
    token = get_discord_bot_token()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    bridge_host = os.environ.get("DISCORD_BRIDGE_HOST", "").strip()
    body_dict: dict[str, Any] = {"content": content}
    if nonce:
        body_dict["nonce"] = nonce if len(nonce) <= 25 else nonce[:25]
        body_dict["enforce_nonce"] = True

    if not token and not webhook and bridge_host:
        script = (
            "import sys, json, os, urllib.request, urllib.error\n"
            "token = ''\n"
            "env_path = '/home/ubuntu/.claude/channels/discord/.env'\n"
            "if os.path.exists(env_path):\n"
            "    with open(env_path, 'r', encoding='utf-8') as f:\n"
            "        for line in f:\n"
            "            if line.startswith('DISCORD_BOT_TOKEN='):\n"
            "                token = line.split('=', 1)[1].strip().strip('\"\\'')\n"
            "payload = json.loads(sys.stdin.read())\n"
            "ch = payload.get('channel_id')\n"
            "method = payload.get('method', 'POST')\n"
            "headers = {'Authorization': f'Bot {token}', 'Content-Type': 'application/json', 'User-Agent': 'Snowball/1.0'}\n"
            "if method == 'GET':\n"
            "    url = f'https://discord.com/api/v10/channels/{ch}/messages?limit=25'\n"
            "    req = urllib.request.Request(url, headers=headers, method='GET')\n"
            "else:\n"
            "    url = f'https://discord.com/api/v10/channels/{ch}/messages'\n"
            "    body = {'content': payload.get('content', '')}\n"
            "    if payload.get('nonce'):\n"
            "        body['nonce'] = payload['nonce'][:25]\n"
            "        body['enforce_nonce'] = True\n"
            "    req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')\n"
            "try:\n"
            "    with urllib.request.urlopen(req, timeout=15) as resp:\n"
            "        data = json.loads(resp.read().decode('utf-8'))\n"
            "        if method == 'GET':\n"
            "            msgs = [{'id': m.get('id'), 'nonce': m.get('nonce')} for m in (data if isinstance(data, list) else [])]\n"
            "            sys.stdout.write(json.dumps({'status': 200, 'messages': msgs}))\n"
            "        else:\n"
            "            sys.stdout.write(json.dumps({'status': 200, 'id': str(data.get('id', ''))}))\n"
            "except urllib.error.HTTPError as e:\n"
            "    sys.stdout.write(json.dumps({'status': e.code, 'error': f'http {e.code}'}))\n"
            "except Exception as e:\n"
            "    sys.stdout.write(json.dumps({'status': 500, 'error': str(type(e).__name__)}))\n"
        )
        payload = json.dumps({"channel_id": channel_id, "content": content, "nonce": (nonce or "")[:25]})
        try:
            res = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", bridge_host, f"python3 -c {shlex.quote(script)}"],
                input=payload,
                capture_output=True,
                text=True,
                check=False,
                timeout=25,
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if data.get("status") == 200 and data.get("id"):
                    return str(data["id"])
        except Exception as exc:
            raise DeliveryFailure(f"remote bridge transport error: {exc}") from exc
        raise DeliveryFailure("remote bridge post failed")

    body = json.dumps(body_dict).encode("utf-8")
    if token and channel_id:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Snowball/1.0",
        }
    elif webhook:
        join = "&" if "?" in webhook else "?"
        url = f"{webhook}{join}wait=true"
        headers = {"Content-Type": "application/json", "User-Agent": "Snowball/1.0"}
    else:
        raise DeliveryFailure("missing discord token")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise DeliveryFailure(f"discord http {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise DeliveryFailure(str(exc.reason)) from exc
    if status < 200 or status >= 300:
        raise DeliveryFailure(f"discord http {status}")
    try:
        resp_payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise DeliveryFailure("discord response not json") from exc
    message_id = resp_payload.get("id")
    if (
        message_id is None
        or not str(message_id).strip()
        or str(message_id).strip() in ("0", "None", "null", "undefined")
    ):
        raise DeliveryFailure("discord response missing message id")
    return str(message_id)


def send_digest(prepared: PreparedDigest, channel_id: str, conn: sqlite3.Connection | None = None) -> DeliveryAck:
    """Post one prepared chunk. Raises DeliveryFailure without touching reported_at."""
    ch = str(channel_id or "").strip()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not ch and not webhook:
        raise DeliveryFailure("missing channel_id")
    if len(prepared.text) > DISCORD_LIMIT:
        raise DeliveryFailure("prepared digest exceeds discord message limit")

    if conn and _has_table(conn, "digest_outbox"):
        conn.execute("UPDATE digest_outbox SET state = 'sending' WHERE delivery_key = ?", (prepared.delivery_key,))
        _commit(conn)

    try:
        message_id = _discord_post(ch, prepared.text, nonce=prepared.delivery_key)
    except Exception as exc:
        if conn and _has_table(conn, "digest_outbox"):
            conn.execute("UPDATE digest_outbox SET state = 'unknown' WHERE delivery_key = ?", (prepared.delivery_key,))
            _commit(conn)
        raise DeliveryFailure(str(exc)) from exc

    if not message_id or not str(message_id).strip() or str(message_id).strip() in ("0", "None", "null", "undefined"):
        if conn and _has_table(conn, "digest_outbox"):
            conn.execute("UPDATE digest_outbox SET state = 'unknown' WHERE delivery_key = ?", (prepared.delivery_key,))
            _commit(conn)
        raise DeliveryFailure("missing message id from transport")

    return DeliveryAck(
        delivery_key=prepared.delivery_key,
        channel_id=ch,
        message_id=str(message_id),
        sent_at=int(dt.datetime.now(dt.timezone.utc).timestamp()),
    )



def reconcile_delivery(conn: sqlite3.Connection, delivery_key: str, channel_id: str = "") -> DeliveryAck | None:
    """Check if a previously attempted delivery was already acknowledged or posted."""
    ensure_digest_schema(conn)
    if not _has_table(conn, "digest_outbox"):
        return None
    row = conn.execute(
        "SELECT message_id, sent_at, state FROM digest_outbox WHERE delivery_key = ?",
        (delivery_key,),
    ).fetchone()
    if not row:
        return None
    if row[0] and row[2] == "acked":
        return DeliveryAck(
            delivery_key=delivery_key,
            channel_id=channel_id,
            message_id=str(row[0]),
            sent_at=int(row[1] or time.time()),
        )
    nonce = delivery_key if len(delivery_key) <= 25 else delivery_key[:25]
    if channel_id and row[2] in ("sending", "unknown"):
        token = get_discord_bot_token()
        bridge_host = os.environ.get("DISCORD_BRIDGE_HOST", "").strip()
        found_msg_id = None
        if token:
            try:
                url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=25"
                headers = {"Authorization": f"Bot {token}", "User-Agent": "Snowball/1.0"}
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    for m in (data if isinstance(data, list) else []):
                        if str(m.get("nonce") or "") == nonce:
                            found_msg_id = str(m["id"])
                            break
            except Exception:
                pass
        elif bridge_host:
            script = (
                "import sys, json, os, urllib.request\n"
                "env_file = '/home/ubuntu/.claude/channels/discord/.env'\n"
                "token = ''\n"
                "if os.path.exists(env_file):\n"
                "    with open(env_file, 'r', encoding='utf-8') as f:\n"
                "        for line in f:\n"
                "            if line.startswith('DISCORD_BOT_TOKEN='):\n"
                "                token = line.split('=', 1)[1].strip().strip('\"\\'')\n"
                "payload = json.loads(sys.stdin.read())\n"
                "ch = payload.get('channel_id')\n"
                "url = f'https://discord.com/api/v10/channels/{ch}/messages?limit=25'\n"
                "req = urllib.request.Request(url, headers={'Authorization': f'Bot {token}', 'User-Agent': 'Snowball/1.0'}, method='GET')\n"
                "try:\n"
                "    with urllib.request.urlopen(req, timeout=10) as resp:\n"
                "        msgs = [{'id': m.get('id'), 'nonce': m.get('nonce')} for m in json.loads(resp.read().decode('utf-8'))]\n"
                "        sys.stdout.write(json.dumps({'status': 200, 'messages': msgs}))\n"
                "except Exception:\n"
                "    sys.stdout.write(json.dumps({'status': 500}))\n"
            )
            try:
                res = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", bridge_host, f"python3 -c {shlex.quote(script)}"],
                    input=json.dumps({"channel_id": channel_id}),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
                if res.returncode == 0 and res.stdout.strip():
                    data = json.loads(res.stdout)
                    for m in data.get("messages", []):
                        if str(m.get("nonce") or "") == nonce:
                            found_msg_id = str(m["id"])
                            break
            except Exception:
                pass
        if found_msg_id:
            # Finding the Discord message is not acknowledgement. digest_items and
            # reported_at are written only by acknowledge_digest. Marking acked here
            # would hide the chunk from a later prepare update and skip those rows.
            now_ts = int(time.time())
            conn.execute(
                """
                UPDATE digest_outbox
                   SET message_id = ?, sent_at = ?
                 WHERE delivery_key = ?
                   AND state != 'acked'
                """,
                (found_msg_id, now_ts, delivery_key),
            )
            _commit(conn)
            return DeliveryAck(
                delivery_key=delivery_key,
                channel_id=channel_id,
                message_id=found_msg_id,
                sent_at=now_ts,
            )
    return None

def _outbox_meta(conn: sqlite3.Connection, delivery_key: str) -> list[dict[str, Any]]:
    if not _has_table(conn, "digest_outbox"):
        return []
    row = conn.execute(
        "SELECT item_refs FROM digest_outbox WHERE delivery_key = ?",
        (delivery_key,),
    ).fetchone()
    if not row or not row[0]:
        return []
    try:
        parsed = json.loads(row[0])
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def acknowledge_digest(conn: sqlite3.Connection, prepared: PreparedDigest, ack: DeliveryAck) -> None:
    """Persist reported_at and digest_items for one confirmed delivery. Replay-idempotent."""
    if ack.delivery_key != prepared.delivery_key:
        raise DeliveryFailure("delivery_key mismatch")
    if not ack.message_id or not str(ack.message_id).strip() or str(ack.message_id).strip() in ("0", "None", "null", "undefined"):
        raise DeliveryFailure("missing message id")
    ensure_digest_schema(conn)

    meta = _outbox_meta(conn, prepared.delivery_key)
    by_ref = {(str(item.get("ref_kind")), str(item.get("ref_id"))): item for item in meta}
    began = False
    if not conn.in_transaction:
        try:
            conn.execute("BEGIN IMMEDIATE")
            began = True
        except sqlite3.OperationalError as exc:
            if "transaction" not in str(exc).lower():
                raise
    try:
        for offset, (kind, ref_id) in enumerate(prepared.item_refs, start=1):
            info = by_ref.get((kind, ref_id), {})
            rank = int(info.get("rank") or offset)
            reason = str(info.get("reason") or kind)
            if prepared.day.startswith("weekly-"):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO weekly_items (day, ref_kind, ref_id, rank, reason, sent_at, message_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (prepared.day, kind, ref_id, rank, reason, ack.sent_at, ack.message_id),
                )
            else:
                if kind == "entity":
                    conn.execute(
                        """
                        UPDATE entities
                           SET reported_at = ?
                         WHERE key = ?
                           AND (reported_at IS NULL OR reported_at = 0)
                        """,
                        (ack.sent_at, ref_id),
                    )
                elif kind == "change":
                    conn.execute(
                        """
                        UPDATE entity_changes
                           SET reported_at = ?
                         WHERE id = ?
                           AND (reported_at IS NULL OR reported_at = 0)
                        """,
                        (ack.sent_at, ref_id),
                    )
                else:
                    raise DeliveryFailure(f"unknown ref kind {kind}")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO digest_items (day, ref_kind, ref_id, rank, reason, sent_at, message_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (prepared.day, kind, ref_id, rank, reason, ack.sent_at, ack.message_id),
                )
        conn.execute(
            """
            UPDATE digest_outbox
               SET state = 'acked', message_id = ?, sent_at = ?
             WHERE delivery_key = ?
            """,
            (ack.message_id, ack.sent_at, prepared.delivery_key),
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


def get_digest_snapshot(conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
    """Return truthful snapshot and provenance for an acknowledged digest item."""
    if not _has_table(conn, "digest_items"):
        return None
    row = conn.execute("SELECT * FROM digest_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return None
    res = dict(row)
    ref_kind = row["ref_kind"]
    ref_id = row["ref_id"]
    if ref_kind == "entity" and _has_table(conn, "entities"):
        ent = conn.execute("SELECT * FROM entities WHERE key = ?", (ref_id,)).fetchone()
        if ent:
            res["evidence_quote"] = ent["evidence_quote"]
            res["link"] = ent["link"]
            res["link_dead"] = ent["link_dead"] if "link_dead" in ent.keys() else 0
            page_id = ent["evidence_page_id"] if "evidence_page_id" in ent.keys() else None
            res["page_id"] = page_id
            if page_id and _has_table(conn, "pages"):
                p = conn.execute("SELECT canon_url, raw_path FROM pages WHERE id = ?", (page_id,)).fetchone()
                if p:
                    res["canon_url"] = p["canon_url"]
                    res["raw_path"] = p["raw_path"]
    elif ref_kind == "change" and _has_table(conn, "entity_changes"):
        ch = conn.execute("SELECT * FROM entity_changes WHERE id = ?", (ref_id,)).fetchone()
        if ch:
            res["evidence_quote"] = ch["evidence_quote"]
            res["field"] = ch["field"]
            res["old"] = ch["old"]
            res["new"] = ch["new"]
    return res


def compile_daily_digest(conn: sqlite3.Connection) -> tuple[str, int]:
    """Compatibility wrapper. Does not set reported_at."""
    day = dt.date.today().isoformat()
    prepared = prepare_digest(conn, day)
    if not prepared:
        path = DIGEST_DIR / f"{day}.md"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return text, 0
    text = "\n\n".join(part.text for part in prepared)
    return text, sum(len(part.item_refs) for part in prepared)


def save_digest_file(text: str) -> Path:
    """Save digest markdown to digests/YYYY-MM-DD.md."""
    return _write_digest_file(dt.date.today().isoformat(), text)


def post_to_discord(channel_id: str, content: str) -> bool:
    """Post digest markdown to Discord channel if token available."""
    try:
        _discord_post(channel_id, content[:DISCORD_LIMIT])
    except DeliveryFailure as exc:
        print(f"Discord post failed: {exc}")
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Daily Digest Generator")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    ap.add_argument("--channel", type=str, default="", help="Override Discord channel ID")
    ap.add_argument("--post", action="store_true", help="Post to Discord channel/webhook")
    ap.add_argument("--day", type=str, default="", help="Digest day YYYY-MM-DD")
    ap.add_argument("--weekly", action="store_true", help="Compile weekly learning summary")
    ap.add_argument("--json", action="store_true", help="Output summary as JSON")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"Database not found at {args.db}. Nothing to digest.")
        return

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    day = args.day or dt.date.today().isoformat()
    if args.weekly:
        prepared = prepare_weekly_digest(conn, day)
    else:
        prepared = prepare_digest(conn, day)
    count = sum(len(part.item_refs) for part in prepared)
    text = "\n\n".join(part.text for part in prepared)
    if not text:
        path = DIGEST_DIR / f"{day}.md"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    posted_count = 0
    if args.post:
        channel = args.channel or get_channel_id()
        if not channel:
            print("No Discord channel configured; digest file only.")
        else:
            for part in prepared:
                try:
                    ack = send_digest(part, channel)
                    acknowledge_digest(conn, part, ack)
                    posted_count += 1
                    if not args.json:
                        print(f"Posted chunk {part.chunk_index} message {ack.message_id}")
                except DeliveryFailure as exc:
                    if not args.json:
                        print(f"Delivery failed: {exc}")
                    break
    conn.close()
    if args.json:
        print(
            json.dumps(
                {
                    "day": day,
                    "items": count,
                    "chunks": len(prepared),
                    "posted": posted_count,
                    "weekly": args.weekly,
                }
            )
        )
    else:
        print(f"\n❄️  Digest compiled ({count} items) -> {DIGEST_DIR / f'{day}.md'}\n")
        print(text)


if __name__ == "__main__":
    main()
