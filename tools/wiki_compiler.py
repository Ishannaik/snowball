#!/usr/bin/env python3
"""Snowball incremental Markdown wiki compiler.

Entity pages are the source of truth. ``wiki_pages``, ``wiki_links``, and ``wiki_fts``
are a rebuildable cache. Code owns YAML frontmatter and the Facts, Changes, and
Sources sections. Human text outside those sections is kept verbatim.

Staleness propagates exactly one hop, and only from a page compiled because its
own facts were dirty. A stale recompile never marks its linkers.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import sqlite3
import time
import sys
from pathlib import Path
from typing import Any

import importlib.util


def _sibling(name: str):
    mod_name = f"snowball_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    mod_path = Path(__file__).resolve().parent / f"{name}.py"
    if not mod_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
WIKI_DIR = ROOT / "wiki"
BODY_SHRINK_THRESHOLD = 0.70
DIRTY_LIMIT = 200
STALE_LIMIT = 50
TIME_BUDGET_SEC = 600
CODE_OWNED = frozenset({
    "facts",
    "changes",
    "sources",
    "overview",
    "entities",
    "topics",
    "discovered entities",
    "current source evidence",
    "historical source evidence",
    "provenance",
})
SOURCE_REGION_KEYS = frozenset({
    "facts",
    "changes",
    "sources",
    "overview",
    "entities",
    "topics",
    "current_evidence",
    "historical_evidence",
    "provenance",
})
WIKI_REGIONS_DDL = """
CREATE TABLE IF NOT EXISTS wiki_regions (
  path TEXT NOT NULL,
  region_key TEXT NOT NULL,
  provenance TEXT NOT NULL CHECK (provenance IN ('source_evidence','human_note')),
  body TEXT NOT NULL,
  PRIMARY KEY (path, region_key)
)
"""
NUMBERS_RX = re.compile(r"\b\d+(?:[\.,]\d+)*\b")
_H2 = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_ANY_REGION = re.compile(
    r"<!-- snowball:([a-zA-Z0-9_-]+):start -->.*?<!-- snowball:\1:end -->\n?",
    re.S,
)
_REGION = {
    key: re.compile(
        rf"<!-- snowball:{key}:start -->.*?<!-- snowball:{key}:end -->\n?",
        re.S,
    )
    for key in ("facts", "changes", "sources", "overview", "entities", "topics")
}

WIKI_PAGE_DDL = """
CREATE TABLE IF NOT EXISTS wiki_pages (
  path TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('entity','topic','hub','index','family')),
  ref_key TEXT,
  title TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  frontmatter TEXT NOT NULL,
  compiled_at INTEGER NOT NULL,
  dirty INTEGER NOT NULL DEFAULT 0,
  stale INTEGER NOT NULL DEFAULT 0,
  stale_reason TEXT,
  stale_origin TEXT,
  stale_marked_at INTEGER
)
"""


def slugify(text: str) -> str:
    """Format string into a safe file slug."""
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", s) or "untitled"


def entity_relpath(
    name: str | None,
    org: str | None,
    year: str | None,
    key: str | None = None,
    wiki_path: str | None = None,
) -> str:
    """Stable entity filename: entities/<slug(name)>-<slug(org)>-<year>.md or wiki_path."""
    if wiki_path and str(wiki_path).strip():
        return str(wiki_path).strip()
    year_slug = slugify(str(year).strip()) if year and str(year).strip() else "undated"
    org_slug = slugify(org) if org and str(org).strip() else ""
    name_slug = slugify(name) if name and str(name).strip() else ""
    if not name_slug and key:
        name_slug = slugify(str(key))
    if not name_slug:
        name_slug = "untitled"
    if org_slug:
        return f"entities/{name_slug}-{org_slug}-{year_slug}.md"
    return f"entities/{name_slug}-{year_slug}.md"

def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and body from Markdown."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    fm_raw = parts[1]
    body = parts[2].lstrip("\n")
    fm: dict[str, Any] = {}
    for line in fm_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if v.startswith("[") and v.endswith("]"):
                items = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                fm[k] = items
            else:
                fm[k] = v
    return fm, body


def _yaml_scalar(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def format_frontmatter(fm: dict[str, Any]) -> str:
    """Format dictionary into YAML frontmatter string."""
    lines = ["---"]
    for k, v in sorted(fm.items()):
        if isinstance(v, (list, set)):
            items_str = ", ".join(_yaml_scalar(x) for x in sorted(v, key=lambda s: str(s)))
            lines.append(f"{k}: [{items_str}]")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---\n")
    return "\n".join(lines)


def _merge_frontmatter(old_fm: dict[str, Any], new_fm: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old_fm)
    merged.update(new_fm)
    for array_key in ("sources", "tags", "aliases"):
        old_arr = old_fm.get(array_key, [])
        new_arr = new_fm.get(array_key, [])
        if not isinstance(old_arr, list):
            old_arr = [old_arr] if old_arr else []
        if not isinstance(new_arr, list):
            new_arr = [new_arr] if new_arr else []
        combined = sorted({str(x) for x in [*old_arr, *new_arr] if str(x).strip()})
        if combined:
            merged[array_key] = combined
    return merged


def _split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(_H2.finditer(body))
    if not matches:
        return body, []
    preamble = body[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((match.group(1).strip(), body[match.start() : end]))
    return preamble, sections


def _extract_regions(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _ANY_REGION.finditer(body):
        key = match.group(1).lower()
        block = match.group(0)
        if not block.endswith("\n"):
            block += "\n"
        found[key] = block
    return found


def _replace_regions(old_body: str, regions: dict[str, str]) -> str:
    """Replace code-owned regions in place. Bytes outside those regions stay put."""
    body = old_body
    for key, block in regions.items():
        if key not in SOURCE_REGION_KEYS:
            continue
        repl = block if block.endswith("\n") else block + "\n"
        pattern = re.compile(
            rf"<!-- snowball:{re.escape(key)}:start -->.*?<!-- snowball:{re.escape(key)}:end -->\n?",
            re.S,
        )
        if pattern.search(body):
            body = pattern.sub(lambda _m, replacement=repl: replacement, body, count=1)
        else:
            body += repl
    return body


def _human_outside(body: str) -> str:
    """Bytes that are not inside a code-owned source region, unchanged."""

    def _keep(match: re.Match[str]) -> str:
        if match.group(1).lower() in SOURCE_REGION_KEYS:
            return ""
        return match.group(0)

    return _ANY_REGION.sub(_keep, body)


def _human_portion(body: str) -> str:
    """Text outside code-owned regions or code-owned headings."""
    stripped = _ANY_REGION.sub("", body)
    preamble, sections = _split_sections(stripped)
    parts = [preamble]
    for title, text in sections:
        if title.lower() not in CODE_OWNED:
            parts.append(text)
    return "".join(parts)


def _human_text_exact(body: str) -> str:
    preamble, sections = _split_sections(body)
    parts = [preamble]
    for title, text in sections:
        if title.lower() not in CODE_OWNED:
            parts.append(text)
    return "".join(parts)


def _splice_owned(old_body: str, new_body: str) -> str:
    """Replace code-owned headings. Every other heading is copied verbatim."""
    old_pre, old_secs = _split_sections(old_body)
    _, new_secs = _split_sections(new_body)
    new_owned: dict[str, str] = {}
    for title, text in new_secs:
        key = title.lower()
        if key in CODE_OWNED:
            new_owned[key] = text if text.endswith("\n") else text + "\n"
    if not old_secs:
        return old_body
    out: list[str] = [old_pre]
    seen: set[str] = set()
    for title, text in old_secs:
        key = title.lower()
        if key in CODE_OWNED and key in new_owned:
            out.append(new_owned[key])
            seen.add(key)
        else:
            out.append(text if text.endswith("\n") else text + "\n")
    for key, block in new_owned.items():
        if key not in seen:
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append("\n")
            out.append(block)
    return "".join(out)


def _append_previous(old_body: str, new_body: str) -> str:
    if new_body.strip() and new_body.strip() in old_body:
        return old_body
    return old_body + "\n\n## Latest Update\n" + new_body


def _shrinks(new_text: str, old_text: str) -> bool:
    if not old_text:
        return False
    raw = len(new_text) < BODY_SHRINK_THRESHOLD * len(old_text)
    stripped = len(new_text.strip()) < len(old_text.strip()) * BODY_SHRINK_THRESHOLD
    return raw or stripped


def _missing_numbers(old_text: str, new_text: str) -> bool:
    return any(num not in new_text for num in NUMBERS_RX.findall(old_text))


def _guard_previous(old_body: str, new_body: str, candidate: str, *, unstructured: bool) -> str:
    if unstructured:
        if _shrinks(new_body, old_body) or _missing_numbers(old_body, new_body):
            print(
                f"  [GUARD] Compaction loss prevented: new body ({len(new_body)}) "
                f"< 70% of old ({len(old_body)}) or numbers dropped. Retaining previous sections."
            )
            return _append_previous(old_body, new_body)
        return new_body
    old_human = _human_portion(old_body)
    human_missing = _missing_numbers(old_human, candidate)
    if _shrinks(candidate, old_body) or human_missing:
        print(
            "  [GUARD] Compaction loss prevented. Retaining previous text sections."
        )
        return _append_previous(old_body, new_body)
    return candidate


def _private_summary_merge(old_text: str, update_text: str) -> str | None:
    """Ask the attested private engine to merge a summary. Never calls a public model."""
    transport = _sibling("engine_transport")
    orch = _sibling("orchestrator")
    if transport is None or orch is None:
        return None
    try:
        cfg = orch.load_config()
        engine = transport.LocalPrivateEngine(cfg, dry=False)
        ok, _reason = engine.available()
        if not ok:
            return None
        reply = engine.interpret(
            "Merge the existing summary and the new information. Retain every number verbatim.\n\n"
            f"Existing Summary:\n{old_text}\n\nNew Information:\n{update_text}\n",
            max_out=1024,
        )
    except Exception:
        return None
    text = reply.text
    return text if isinstance(text, str) and text.strip() else None


def is_model_configured() -> bool:
    """Check if a local model is explicitly configured for summary merges."""
    if os.environ.get("NO_MODEL", "").lower() in ("1", "true", "yes"):
        return False
    return os.environ.get("USE_LOCAL_MODEL_MERGE", "").lower() in ("1", "true", "yes")


def merge_summary_local(
    old_summary: str,
    new_info: str,
    *,
    model_callable: Any = None,
) -> str:
    """Merge an existing summary with new information using the configured local model.

    Enforces the 0.70 length floor and survival of all numbers from the old summary.
    If local model is unavailable or candidate fails the guard, preserves old summary deterministically.
    Refuses to call a model when no model is configured. A missing model does not crash compile.
    """
    old_text = old_summary.strip()
    update_text = new_info.strip()
    if not old_text:
        return update_text
    if not update_text:
        return old_text

    candidate: str | None = None
    if model_callable is not None:
        try:
            candidate = str(model_callable(old_text, update_text))
        except Exception:
            candidate = None
    elif is_model_configured():
        candidate = _private_summary_merge(old_text, update_text)
    else:
        # Refuse to call a model when no model is configured
        candidate = None

    if candidate and candidate.strip():
        cand = candidate.strip()
        if not _shrinks(cand, old_text) and not _missing_numbers(old_text, cand):
            return cand

    if update_text in old_text:
        return old_text
    return f"{old_text}\n\n{update_text}"


def _extract_summary(body: str) -> str | None:
    _, sections = _split_sections(body)
    for title, text in sections:
        if title.lower() == "summary":
            lines = text.splitlines(keepends=True)
            if lines and lines[0].strip().lower().startswith("## summary"):
                return "".join(lines[1:]).strip()
            return text.strip()
    return None


def merge_page(old_content: str, new_frontmatter: dict[str, Any], new_body: str) -> str:
    """Merge frontmatter arrays, preserve human sections, enforce the 70% floor."""
    if not old_content:
        return format_frontmatter(new_frontmatter) + "\n" + new_body

    old_fm, old_body = parse_frontmatter(old_content)
    merged_fm = _merge_frontmatter(old_fm, new_frontmatter)
    if not old_body.strip():
        body = new_body
    else:
        new_regions = _extract_regions(new_body)
        old_regions = _extract_regions(old_body)
        _preamble, old_secs = _split_sections(old_body)
        old_sum = _extract_summary(old_body)
        new_sum = _extract_summary(new_body)

        if old_regions and new_regions:
            candidate = _replace_regions(old_body, new_regions)
            if _human_outside(candidate) != _human_outside(old_body):
                raise RuntimeError(
                    "wiki compile would change human text outside code-owned regions"
                )
            body = candidate
        elif old_secs and new_regions:
            human = _human_text_exact(old_body)
            blocks = "".join(
                new_regions[key] for key in new_regions if key in SOURCE_REGION_KEYS
            )
            candidate = human + blocks if human.endswith("\n") else human + "\n" + blocks
            if old_sum and new_sum and old_sum != new_sum:
                merged_sum = merge_summary_local(old_sum, new_sum)
                c_pre, c_secs = _split_sections(candidate)
                parts = [c_pre]
                for title, text in c_secs:
                    if title.lower() == "summary":
                        parts.append(f"## Summary\n\n{merged_sum}\n\n")
                    else:
                        parts.append(text)
                candidate = "".join(parts)
            body = _guard_previous(old_body, new_body, candidate, unstructured=False)
        elif old_secs:
            candidate = _splice_owned(old_body, new_body)
            if old_sum and new_sum and old_sum != new_sum:
                merged_sum = merge_summary_local(old_sum, new_sum)
                c_pre, c_secs = _split_sections(candidate)
                parts = [c_pre]
                for title, text in c_secs:
                    if title.lower() == "summary":
                        parts.append(f"## Summary\n\n{merged_sum}\n\n")
                    else:
                        parts.append(text)
                candidate = "".join(parts)
            body = _guard_previous(old_body, new_body, candidate, unstructured=False)
        else:
            body = _guard_previous(old_body, new_body, new_body, unstructured=True)
    return format_frontmatter(merged_fm) + "\n" + body


def _budget_exceeded(started: float) -> bool:
    return (time.monotonic() - started) >= TIME_BUDGET_SEC


def _atomic_replace(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    _atomic_replace(tmp, path)


def _commit(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.OperationalError):
        conn.commit()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


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


def ensure_wiki_schema(conn: sqlite3.Connection) -> None:
    """Create the rebuildable wiki cache if this database does not have it yet."""
    conn.execute(WIKI_PAGE_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS wiki_pages_dirty ON wiki_pages(dirty, stale)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wiki_links ("
        "from_path TEXT NOT NULL, to_path TEXT NOT NULL, PRIMARY KEY (from_path, to_path))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS wiki_links_to ON wiki_links(to_path)")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5("
        "title, body, path UNINDEXED, tokenize='unicode61 remove_diacritics 2')"
    )
    _ensure_region_audit(conn)


def _schema_version(conn: sqlite3.Connection) -> int | None:
    if not _has_table(conn, "schema_meta"):
        return None
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _ensure_region_audit(conn: sqlite3.Connection) -> bool:
    """Create wiki_regions except on v2/v3, where migration owns that CREATE TABLE."""
    if _has_table(conn, "wiki_regions"):
        return True
    if _schema_version(conn) in (2, 3):
        return False
    conn.execute(WIKI_REGIONS_DDL)
    return True


def _year_of(entity: dict[str, Any]) -> str:
    year = str(entity.get("year") or "").strip()
    if year:
        return year
    deadline = str(entity.get("deadline") or "")
    if len(deadline) >= 4 and deadline[:4].isdigit():
        return deadline[:4]
    return str(dt.date.today().year)


def _amount_str(entity: dict[str, Any]) -> str:
    amount = entity.get("amount")
    currency = str(entity.get("currency") or "").strip()
    if amount is None or amount == "":
        return "Variable"
    if isinstance(amount, float) and amount.is_integer():
        amount = int(amount)
    text = f"{currency} {amount}".strip()
    return text or "Variable"


def _fmt_ts(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def _json_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return []
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        return [text]
    return [str(value)]


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _load_entities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _has_table(conn, "entities"):
        return []
    wanted = [
        "key",
        "name",
        "org",
        "year",
        "kind",
        "amount",
        "currency",
        "deadline",
        "eligibility",
        "link",
        "evidence_quote",
        "first_seen_at",
        "last_seen_at",
        "last_changed_at",
        "relevance",
        "urgency",
        "status",
        "family",
        "location",
        "aliases",
        "evidence_page_id",
        "wiki_path",
        "provenance",
    ]
    have = _cols(conn, "entities")
    pick = [col for col in wanted if col in have]
    if "key" not in pick or "name" not in pick:
        raise sqlite3.OperationalError("entities.key and entities.name are required")
    return _fetch(conn, f"SELECT {', '.join(pick)} FROM entities ORDER BY key")


def _load_changes(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    if not _has_table(conn, "entity_changes"):
        return {}
    have = _cols(conn, "entity_changes")
    if "entity_key" not in have:
        return {}
    select = ["entity_key"]
    if "id" in have:
        select.append("id")
    if "field" in have:
        select.append("field")
    if "old" in have:
        select.append('"old" AS old')
    if "new" in have:
        select.append('"new" AS new')
    if "seen_at" in have:
        select.append("seen_at")
    if "evidence_quote" in have:
        select.append("evidence_quote")
    order = "seen_at, id" if "seen_at" in have and "id" in have else "entity_key"
    rows = _fetch(conn, f"SELECT {', '.join(select)} FROM entity_changes ORDER BY {order}")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("entity_key")), []).append(row)
    return grouped


def _load_claims(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    if not _has_table(conn, "claims"):
        return {}
    have = _cols(conn, "claims")
    if "text" not in have or "entity_key" not in have:
        return {}
    select = ["entity_key", "text"]
    if "hash" in have:
        select.append("hash")
    if "provenance" in have:
        select.append("provenance")
    rows = _fetch(
        conn,
        f"SELECT {', '.join(select)} FROM claims WHERE entity_key IS NOT NULL ORDER BY text",
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("text"):
            grouped.setdefault(str(row.get("entity_key")), []).append(dict(row))
    return grouped


def _lead_label(provenance: Any) -> str:
    token = str(provenance or "legacy_v3").strip().lower()
    if token in {"legacy_v3", "lead_legacy", "lead", "imported_history", "unknown", ""}:
        return "LEAD"
    if token in {"observed", "fact"}:
        return "FACT"
    return token


def _claim_records(claims: list[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in claims:
        if isinstance(item, str):
            records.append({"text": item, "provenance": "legacy_v3"})
        elif isinstance(item, dict) and item.get("text"):
            records.append(item)
    return records


def _region(key: str, inner: str) -> str:
    return f"<!-- snowball:{key}:start -->\n{inner.rstrip()}\n<!-- snowball:{key}:end -->\n"


PROVENANCE_REGION = _region(
    "provenance",
    "## Provenance\n\n"
    "- Human text outside code-owned regions is HUMAN_NOTE and is not source evidence.\n"
    "- Legacy rows are LEAD and are not reviewed.\n",
)


def _is_historical_observation(row: dict[str, Any]) -> bool:
    if row.get("invalidated_at") not in (None, ""):
        return True
    return row.get("superseded_by") not in (None, "")


def _load_observation_index(conn: sqlite3.Connection) -> dict[str, dict[Any, list[dict[str, Any]]]] | None:
    if not _has_table(conn, "observations") or not _has_table(conn, "source_snapshots"):
        return None
    obs_cols = _cols(conn, "observations")
    snap_cols = _cols(conn, "source_snapshots")
    if not {"id", "snapshot_id", "quote"}.issubset(obs_cols) or "id" not in snap_cols:
        return None
    select = ["o.id AS obs_id", "o.snapshot_id AS snapshot_id", "o.quote AS quote"]
    for column in (
        "page_id",
        "program_key",
        "field_key",
        "quote_start",
        "quote_end",
        "assurance",
        "invalidated_at",
        "invalidated_reason",
    ):
        if column in obs_cols:
            select.append(f"o.{column} AS {column}")
    for column in (
        "content_hash",
        "text_path",
        "original_raw_path",
        "visibility",
        "provenance",
        "source_cycle_label",
        "superseded_by",
        "retrieved_at",
    ):
        if column in snap_cols:
            select.append(f"s.{column} AS snap_{column}")
    rows = _fetch(
        conn,
        "SELECT "
        + ", ".join(select)
        + " FROM observations o JOIN source_snapshots s ON s.id = o.snapshot_id ORDER BY o.id",
    )
    by_page: dict[Any, list[dict[str, Any]]] = {}
    by_program: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        item = {
            "id": row.get("obs_id"),
            "snapshot_id": row.get("snapshot_id"),
            "page_id": row.get("page_id"),
            "program_key": row.get("program_key"),
            "field_key": row.get("field_key") or "unknown",
            "quote": row.get("quote") or "",
            "quote_start": row.get("quote_start"),
            "quote_end": row.get("quote_end"),
            "assurance": row.get("assurance") or "unknown",
            "invalidated_at": row.get("invalidated_at"),
            "invalidated_reason": row.get("invalidated_reason"),
            "content_hash": row.get("snap_content_hash"),
            "text_path": row.get("snap_text_path"),
            "original_raw_path": row.get("snap_original_raw_path"),
            "snapshot_provenance": row.get("snap_provenance"),
            "source_cycle_label": row.get("snap_source_cycle_label"),
            "superseded_by": row.get("snap_superseded_by"),
            "visibility": row.get("snap_visibility"),
        }
        if item["page_id"] is not None:
            by_page.setdefault(item["page_id"], []).append(item)
        if item["program_key"]:
            by_program.setdefault(str(item["program_key"]), []).append(item)
    return {"by_page": by_page, "by_program": by_program}


def _observations_for_entity(
    index: dict[str, dict[Any, list[dict[str, Any]]]] | None,
    entity: dict[str, Any],
) -> list[dict[str, Any]]:
    if not index:
        return []
    seen: set[Any] = set()
    rows: list[dict[str, Any]] = []
    page_id = entity.get("evidence_page_id")
    page_keys: list[Any] = []
    if page_id is not None:
        page_keys.append(page_id)
        try:
            page_keys.append(int(page_id))
        except (TypeError, ValueError):
            pass
    buckets = [index["by_page"].get(key, []) for key in page_keys]
    if entity.get("key"):
        buckets.append(index["by_program"].get(str(entity.get("key")), []))
    for bucket in buckets:
        for row in bucket:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(row)
    return rows


def _pin_link(label: str, raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    target = str(raw).replace("\n", "").replace("\r", "")
    return f"[{label}](<{target}>)"


def _render_observation_block(row: dict[str, Any]) -> str:
    assurance = str(row.get("assurance") or "unknown")
    lines = [
        f"- field `{row.get('field_key') or 'unknown'}` assurance `{assurance}` "
        f"snapshot `{row.get('snapshot_id')}`"
    ]
    if row.get("content_hash"):
        lines[0] += f" hash `{row['content_hash']}`"
    if row.get("source_cycle_label"):
        lines[0] += f" cycle `{row['source_cycle_label']}`"
    if row.get("invalidated_reason"):
        lines.append(f"  invalidated `{row['invalidated_reason']}`")
    if row.get("superseded_by") not in (None, ""):
        lines.append(f"  superseded_by `{row['superseded_by']}`")
    if row.get("quote_start") is not None and row.get("quote_end") is not None:
        lines.append(f"  span {row['quote_start']}:{row['quote_end']}")
    for label, key in (("raw", "original_raw_path"), ("text", "text_path")):
        pin = _pin_link(label, row.get(key))
        if pin:
            lines.append(f"  {pin}")
    lines.append("> \"" + str(row.get("quote") or "") + "\"")
    return "\n".join(lines)


def _render_evidence_regions(rows: list[dict[str, Any]]) -> str:
    current = [row for row in rows if not _is_historical_observation(row)]
    historical = [row for row in rows if _is_historical_observation(row)]
    parts = []
    for key, title, group in (
        ("current_evidence", "Current source evidence", current),
        ("historical_evidence", "Historical source evidence", historical),
    ):
        body = "\n\n".join(_render_observation_block(row) for row in group) or "_None._"
        parts.append(_region(key, f"## {title}\n\n{body}\n"))
    return "\n".join(parts) + "\n"


def _render_entity(
    entity: dict[str, Any],
    changes: list[dict[str, Any]],
    claims: list[Any],
    observations: list[dict[str, Any]] | None = None,
    *,
    evidence_enabled: bool = False,
) -> tuple[dict[str, Any], str]:
    year = _year_of(entity)
    amount = _amount_str(entity)
    deadline = entity.get("deadline") or "Rolling"
    kind = entity.get("kind") or "opportunity"
    name = str(entity.get("name") or "Untitled")
    org = str(entity.get("org") or "")
    link = str(entity.get("link") or "")
    quote = str(entity.get("evidence_quote") or "")
    tags = [slugify(str(kind))]
    tags.append("grant" if "grant" in name.lower() else "opportunity")
    fm: dict[str, Any] = {
        "key": entity.get("key") or "",
        "title": f"{name} {year}".strip(),
        "name": name,
        "org": org,
        "year": year,
        "kind": kind,
        "amount": amount,
        "deadline": str(deadline),
        "eligibility": entity.get("eligibility") or "",
        "sources": [link] if link else [],
        "tags": tags,
        "aliases": _json_list(entity.get("aliases")),
        "last_compiled": dt.date.today().isoformat(),
    }
    if entity.get("family"):
        fm["family"] = entity["family"]
    if entity.get("status"):
        fm["status"] = entity["status"]
    if entity.get("first_seen_at"):
        fm["first_seen"] = _fmt_ts(entity.get("first_seen_at"))
    if entity.get("last_changed_at"):
        fm["last_changed"] = _fmt_ts(entity.get("last_changed_at"))

    fact_rows = [
        ("Organization", org),
        ("Category", kind),
        ("Value", amount),
        ("Deadline", deadline),
        ("Eligibility", entity.get("eligibility") or ""),
        ("Link", link),
    ]
    if entity.get("location"):
        fact_rows.append(("Location", entity["location"]))
    if entity.get("status"):
        fact_rows.append(("Status", entity["status"]))
    lead = _lead_label(entity.get("provenance"))
    if lead == "LEAD":
        fact_rows.append(("Provenance", "LEAD"))
    fact_lines = ["| Field | Value |", "|---|---|"]
    fact_lines.extend(f"| {label} | {_cell(value)} |" for label, value in fact_rows)
    facts = "\n".join(fact_lines)

    if changes:
        change_lines = []
        for change in changes:
            when = _fmt_ts(change.get("seen_at"))
            prefix = f"{when} " if when else ""
            old = change.get("old")
            old_text = "∅" if old is None or old == "" else old
            change_lines.append(
                f"- {prefix}{change.get('field') or 'field'}: {old_text} → {change.get('new')}"
            )
            if change.get("evidence_quote"):
                change_lines.append(f"  > \"{change['evidence_quote']}\"")
        changes_md = "\n".join(change_lines)
    else:
        changes_md = "_No recorded changes._"

    prov_token = str(entity.get("provenance") or "legacy_v3")
    source_lines: list[str] = []
    if link:
        if lead == "LEAD":
            source_lines.append(f"- LEAD ({prov_token}): {link}")
        else:
            source_lines.append(f"1. {link}")
    if quote:
        if lead == "LEAD":
            source_lines.append(f"- LEAD ({prov_token}): {quote}")
        else:
            source_lines.append(f"> \"{quote}\"")
    for claim in _claim_records(claims):
        label = _lead_label(claim.get("provenance"))
        token = str(claim.get("provenance") or "legacy_v3")
        text = str(claim["text"])
        if label == "LEAD":
            source_lines.append(f"- LEAD ({token}): {text}")
        else:
            source_lines.append(f"- {label}: {text}")
    if not source_lines:
        source_lines.append("_No sources recorded._")
    sources = "\n".join(source_lines)

    body = (
        f"# {name}\n\n"
        + _region("facts", "## Facts\n\n" + facts + "\n")
        + "\n"
        + _region("changes", "## Changes\n\n" + changes_md + "\n")
        + "\n"
        + _region("sources", "## Sources\n\n" + sources + "\n")
    )
    if evidence_enabled:
        body += "\n" + _render_evidence_regions(observations or [])
    body += "\n" + PROVENANCE_REGION
    return fm, body


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _wiki_targets(from_rel: str, text: str) -> list[str]:
    raw_links = _MD_LINK.findall(text) + _WIKI_LINK.findall(text)
    found: list[str] = []
    seen: set[str] = set()
    base = posixpath.dirname(from_rel)
    for raw in raw_links:
        target = raw.strip().strip("<>").split("#", 1)[0].strip()
        if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target) or target.startswith("/"):
            continue
        joined = posixpath.normpath(posixpath.join(base, target))
        if joined.startswith("../") or joined == "..":
            continue
        if not joined.endswith(".md"):
            if "." in posixpath.basename(joined):
                continue
            joined += ".md"
        if joined not in seen:
            seen.add(joined)
            found.append(joined)
    return found


def _propagate_stale(conn: sqlite3.Connection, origin: str, now: int) -> None:
    """Mark direct incoming linkers stale. Callers must not use this for stale origins."""
    conn.execute(
        """
        UPDATE wiki_pages
           SET stale = 1,
               stale_reason = ?,
               stale_origin = ?,
               stale_marked_at = ?
         WHERE path IN (SELECT from_path FROM wiki_links WHERE to_path = ?)
           AND path <> ?
           AND stale = 0
           AND dirty = 0
        """,
        (f"link:{origin}", origin, now, origin, origin),
    )


def _record_file(
    conn: sqlite3.Connection,
    rel: str,
    content: str,
    *,
    kind: str,
    ref_key: str | None,
    title: str,
    propagate: bool,
) -> int:
    now = int(time.time())
    fm, body = parse_frontmatter(content)
    resolved_title = title or str(fm.get("title") or fm.get("name") or _first_heading(body) or Path(rel).stem)
    if not resolved_title:
        resolved_title = Path(rel).stem or "untitled"
    body_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    frontmatter = json.dumps(fm, sort_keys=True, ensure_ascii=False)
    stored_key = ref_key if ref_key else (str(fm["key"]) if fm.get("key") else None)
    conn.execute(
        """
        INSERT INTO wiki_pages (
            path, kind, ref_key, title, body_hash, frontmatter, compiled_at,
            dirty, stale, stale_reason, stale_origin, stale_marked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, NULL, NULL)
        ON CONFLICT(path) DO UPDATE SET
            kind = excluded.kind,
            ref_key = excluded.ref_key,
            title = excluded.title,
            body_hash = excluded.body_hash,
            frontmatter = excluded.frontmatter,
            compiled_at = excluded.compiled_at,
            dirty = 0,
            stale = 0,
            stale_reason = NULL,
            stale_origin = NULL,
            stale_marked_at = NULL
        """,
        (rel, kind, stored_key, resolved_title, body_hash, frontmatter, now),
    )
    conn.execute("DELETE FROM wiki_links WHERE from_path = ?", (rel,))
    links = _wiki_targets(rel, content)
    if links:
        conn.executemany(
            "INSERT OR IGNORE INTO wiki_links (from_path, to_path) VALUES (?, ?)",
            [(rel, dest) for dest in links],
        )
    conn.execute("DELETE FROM wiki_fts WHERE path = ?", (rel,))
    conn.execute(
        "INSERT INTO wiki_fts (title, body, path) VALUES (?, ?, ?)",
        (resolved_title, body, rel),
    )
    if propagate:
        _propagate_stale(conn, rel, now)
    _audit_page_regions(conn, rel, content)
    return len(links)


def _audit_page_regions(conn: sqlite3.Connection, rel: str, content: str) -> None:
    if not _has_table(conn, "wiki_regions"):
        return
    _fm, body = parse_frontmatter(content)
    conn.execute("DELETE FROM wiki_regions WHERE path = ?", (rel,))
    rows: list[tuple[str, str, str, str]] = []
    for key, block in _extract_regions(body).items():
        provenance = "source_evidence" if key in SOURCE_REGION_KEYS else "human_note"
        rows.append((rel, key, provenance, block))
    human = _human_outside(body)
    if human:
        rows.append((rel, "human_text", "human_note", human))
    if rows:
        conn.executemany(
            "INSERT INTO wiki_regions (path, region_key, provenance, body) VALUES (?, ?, ?, ?)",
            rows,
        )


def _touch_wiki_path(conn: sqlite3.Connection, key: str, rel: str) -> None:
    if "wiki_path" not in _cols(conn, "entities"):
        return
    conn.execute("UPDATE entities SET wiki_path = ? WHERE key = ?", (rel, key))


def _write_entity_page(
    conn: sqlite3.Connection,
    output_dir: Path,
    entity: dict[str, Any],
    rel: str,
    changes: list[dict[str, Any]],
    claims: list[str],
    *,
    propagate: bool,
) -> None:
    index = _load_observation_index(conn)
    fm, new_body = _render_entity(
        entity,
        changes,
        claims,
        _observations_for_entity(index, entity),
        evidence_enabled=index is not None,
    )
    target = output_dir / rel
    old = target.read_text(encoding="utf-8") if target.is_file() else ""
    merged = merge_page(old, fm, new_body)
    # Dirty/stale flags flip only after the temp file replaces the target.
    _atomic_write(target, merged)
    _record_file(
        conn,
        rel,
        merged,
        kind="entity",
        ref_key=str(entity.get("key") or ""),
        title=str(fm.get("title") or entity.get("name") or "untitled"),
        propagate=propagate,
    )
    if entity.get("key"):
        _touch_wiki_path(conn, str(entity["key"]), rel)
    _commit(conn)


def _load_topics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _has_table(conn, "topics"):
        return []
    cols = _cols(conn, "topics")
    pick = [c for c in ("id", "key", "family", "title", "focus") if c in cols]
    if not pick:
        return []
    return _fetch(conn, f"SELECT {', '.join(pick)} FROM topics ORDER BY key")


def _load_families(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _has_table(conn, "families"):
        return []
    cols = _cols(conn, "families")
    pick = [c for c in ("name", "recheck_days", "weight") if c in cols]
    if not pick:
        return []
    return _fetch(conn, f"SELECT {', '.join(pick)} FROM families ORDER BY name")


def _load_hubs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _has_table(conn, "hubs"):
        return []
    if _has_table(conn, "pages"):
        return _fetch(
            conn,
            """
            SELECT h.page_id AS page_id, h.item_count AS item_count,
                   p.domain AS domain, p.canon_url AS canon_url
            FROM hubs h
            JOIN pages p ON p.id = h.page_id
            ORDER BY p.domain
            """,
        )
    return _fetch(conn, "SELECT page_id, item_count FROM hubs")


def _render_topic(
    topic: dict[str, Any],
    entities: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    title = str(topic.get("title") or topic.get("key") or "Topic")
    key = str(topic.get("key") or slugify(title))
    family = str(topic.get("family") or "general")
    focus = str(topic.get("focus") or "").strip()
    fm: dict[str, Any] = {
        "key": key,
        "title": title,
        "family": family,
        "kind": "topic",
        "tags": sorted(list({slugify(family), "topic"})),
        "last_compiled": dt.date.today().isoformat(),
    }
    if focus:
        fm["focus"] = focus

    lines = [f"# {title}", ""]
    overview_text = focus or "No focus description recorded."
    lines.append(_region("overview", f"## Overview\n\n{overview_text}\n"))
    lines.append("")
    matching = [
        e
        for e in entities
        if str(e.get("family") or "").lower() == family.lower()
        or key.lower() in [slugify(str(t)).lower() for t in _json_list(e.get("tags"))]
    ]
    entity_lines = [
        "## Entities",
        "",
        "| Entity / Program | Organization | Category | Deadline | Source |",
        "|---|---|---|---|---|",
    ]
    if matching:
        for entity in matching:
            rel = entity_relpath(str(entity.get("name") or ""), entity.get("org"), _year_of(entity))
            link_path = f"../{rel}"
            entity_lines.append(
                f"| [{_cell(entity.get('name'))}]({link_path}) | {_cell(entity.get('org'))} | "
                f"{_cell(entity.get('kind'))} | {_cell(entity.get('deadline'))} | [Source]({entity.get('link') or '#'}) |"
            )
    else:
        entity_lines.append("_No tracked entities currently linked to this topic._")
    lines.append(_region("entities", "\n".join(entity_lines) + "\n"))
    lines.append(PROVENANCE_REGION)
    return fm, "\n".join(lines) + "\n"


def _write_topic_page(
    conn: sqlite3.Connection,
    output_dir: Path,
    topic: dict[str, Any],
    entities: list[dict[str, Any]],
    *,
    propagate: bool,
) -> str:
    key = str(topic.get("key") or slugify(str(topic.get("title") or "topic")))
    rel = f"topics/{slugify(key)}.md"
    fm, new_body = _render_topic(topic, entities)
    target = output_dir / rel
    old = target.read_text(encoding="utf-8") if target.is_file() else ""
    merged = merge_page(old, fm, new_body) if old else format_frontmatter(fm) + "\n" + new_body
    _atomic_write(target, merged)
    _record_file(
        conn,
        rel,
        merged,
        kind="topic",
        ref_key=key,
        title=str(fm.get("title") or key),
        propagate=propagate,
    )
    _commit(conn)
    return rel


def _render_hub(
    hub: dict[str, Any],
    entities: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    domain = str(hub.get("domain") or f"hub-{hub.get('page_id')}")
    canon_url = str(hub.get("canon_url") or f"https://{domain}")
    item_count = hub.get("item_count", 0)
    fm: dict[str, Any] = {
        "domain": domain,
        "canon_url": canon_url,
        "kind": "hub",
        "item_count": item_count,
        "tags": ["hub", slugify(domain)],
        "last_compiled": dt.date.today().isoformat(),
    }
    matching = [
        e for e in entities if domain.lower() in str(e.get("link") or "").lower()
    ]
    lines = [f"# Hub: {domain}", ""]
    overview_text = f"Crawl research hub for `{domain}` ([{canon_url}]({canon_url})).\nTracking {item_count} items."
    lines.append(_region("overview", f"## Overview\n\n{overview_text}\n"))
    lines.append("")
    entity_lines = [
        "## Discovered Entities",
        "",
        "| Entity / Program | Category | Deadline | Source |",
        "|---|---|---|---|",
    ]
    if matching:
        for entity in matching:
            rel = entity_relpath(str(entity.get("name") or ""), entity.get("org"), _year_of(entity))
            link_path = f"../{rel}"
            entity_lines.append(
                f"| [{_cell(entity.get('name'))}]({link_path}) | {_cell(entity.get('kind'))} | "
                f"{_cell(entity.get('deadline'))} | [Source]({entity.get('link') or '#'}) |"
            )
    else:
        entity_lines.append("_No tracked entities currently linked to this hub domain._")
    lines.append(_region("entities", "\n".join(entity_lines) + "\n"))
    lines.append(PROVENANCE_REGION)
    return fm, "\n".join(lines) + "\n"


def _write_hub_page(
    conn: sqlite3.Connection,
    output_dir: Path,
    hub: dict[str, Any],
    entities: list[dict[str, Any]],
    *,
    propagate: bool,
) -> str:
    domain = str(hub.get("domain") or f"hub-{hub.get('page_id')}")
    rel = f"hubs/{slugify(domain)}.md"
    fm, new_body = _render_hub(hub, entities)
    target = output_dir / rel
    old = target.read_text(encoding="utf-8") if target.is_file() else ""
    merged = merge_page(old, fm, new_body) if old else format_frontmatter(fm) + "\n" + new_body
    _atomic_write(target, merged)
    _record_file(
        conn,
        rel,
        merged,
        kind="hub",
        ref_key=domain,
        title=f"Hub: {domain}",
        propagate=propagate,
    )
    _commit(conn)
    return rel


def _index_text(
    entities: list[dict[str, Any]],
    topics: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    hubs: list[dict[str, Any]] | None = None,
) -> str:
    today = dt.date.today().isoformat()
    lines = [
        "# ❄️ Snowball Research Wiki Index",
        f"*Compiled: {today} | Knowledge Base Root*",
        "",
    ]
    if families or topics:
        lines.append("## Families & Topics")
        lines.append("")
        topics_by_fam: dict[str, list[dict[str, Any]]] = {}
        for t in topics or []:
            fam = str(t.get("family") or "general")
            topics_by_fam.setdefault(fam, []).append(t)
        all_fams = sorted(set(list(topics_by_fam.keys()) + [str(f.get("name")) for f in (families or []) if f.get("name")]))
        for fam in all_fams:
            lines.append(f"### Family: {fam.capitalize()}")
            f_topics = topics_by_fam.get(fam, [])
            if f_topics:
                for t in f_topics:
                    rel = f"topics/{slugify(str(t.get('key') or t.get('title')))}.md"
                    desc = str(t.get("focus") or "Tracked focus area")
                    lines.append(f"- [{t.get('title') or t.get('key')}]({rel}): {desc}")
            else:
                lines.append(f"- _No topics configured under {fam}._")
            lines.append("")

    if hubs:
        lines.append("## Research Hubs")
        lines.append("")
        for h in sorted(hubs, key=lambda x: str(x.get("domain") or "")):
            domain = str(h.get("domain") or f"hub-{h.get('page_id')}")
            rel = f"hubs/{slugify(domain)}.md"
            lines.append(f"- [{domain}]({rel}) ({h.get('item_count', 0)} items)")
        lines.append("")

    lines.extend([
        "## Discovered Entities & Opportunities",
        "",
        "| Entity / Program | Organization | Category | Amount / Value | Deadline | Source |",
        "|---|---|---|---|---|---|",
    ])
    ordered = sorted(entities, key=lambda e: (str(e.get("name") or "").lower(), str(e.get("key") or "")))
    for entity in ordered:
        rel = entity_relpath(str(entity.get("name") or ""), entity.get("org"), _year_of(entity))
        amount = _amount_str(entity)
        deadline = entity.get("deadline") or "Rolling"
        kind = entity.get("kind") or "Opportunity"
        link = entity.get("link") or "#"
        lines.append(
            f"| [{_cell(entity.get('name'))}]({rel}) | {_cell(entity.get('org'))} | "
            f"{_cell(kind)} | {_cell(amount)} | {_cell(deadline)} | [Link]({link}) |"
        )
    return "\n".join(lines) + "\n"

def _render_family(
    family: dict[str, Any],
    topics: list[dict[str, Any]],
    entities: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    name = str(family.get("name") or "general")
    recheck = family.get("recheck_days", 7)
    weight = family.get("weight", 1.0)
    title = f"Family: {name.capitalize()}"
    fm: dict[str, Any] = {
        "family": name,
        "title": title,
        "kind": "family",
        "recheck_days": recheck,
        "weight": weight,
        "tags": ["family", slugify(name)],
        "last_compiled": dt.date.today().isoformat(),
    }
    matching_topics = [t for t in topics if str(t.get("family") or "").lower() == name.lower()]
    matching_entities = [e for e in entities if str(e.get("family") or "").lower() == name.lower()]

    lines = [f"# {title}", ""]
    overview_text = (
        f"Research family tracking `{name}`.\n- Recheck interval: {recheck} days\n- Frontier weight: {weight}"
    )
    lines.append(_region("overview", f"## Overview\n\n{overview_text}\n"))
    lines.append("")

    topic_lines = ["## Topics", ""]
    if matching_topics:
        for t in matching_topics:
            rel = f"../topics/{slugify(str(t.get('key') or t.get('title')))}.md"
            topic_lines.append(
                f"- [{t.get('title') or t.get('key')}]({rel}): {t.get('focus') or 'No focus description'}"
            )
    else:
        topic_lines.append("_No topics configured in this family._")
    lines.append(_region("topics", "\n".join(topic_lines) + "\n"))
    lines.append("")

    entity_lines = [
        "## Entities",
        "",
        "| Entity / Program | Organization | Category | Deadline | Source |",
        "|---|---|---|---|---|",
    ]
    if matching_entities:
        for e in matching_entities:
            rel = entity_relpath(
                str(e.get("name") or ""),
                e.get("org"),
                _year_of(e),
                key=e.get("key"),
                wiki_path=e.get("wiki_path"),
            )
            entity_lines.append(
                f"| [{_cell(e.get('name'))}](../{rel}) | {_cell(e.get('org'))} | "
                f"{_cell(e.get('kind'))} | {_cell(e.get('deadline'))} | [Source]({e.get('link') or '#'}) |"
            )
    else:
        entity_lines.append("_No entities currently tracked in this family._")
    lines.append(_region("entities", "\n".join(entity_lines) + "\n"))
    lines.append(PROVENANCE_REGION)
    return fm, "\n".join(lines) + "\n"


def _write_family_page(
    conn: sqlite3.Connection,
    output_dir: Path,
    family: dict[str, Any],
    topics: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    *,
    propagate: bool,
) -> str:
    name = str(family.get("name") or "general")
    rel = f"families/{slugify(name)}.md"
    fm, new_body = _render_family(family, topics, entities)
    target = output_dir / rel
    old = target.read_text(encoding="utf-8") if target.is_file() else ""
    merged = merge_page(old, fm, new_body) if old else format_frontmatter(fm) + "\n" + new_body
    _atomic_write(target, merged)
    _record_file(
        conn,
        rel,
        merged,
        kind="family",
        ref_key=name,
        title=str(fm.get("title") or name),
        propagate=propagate,
    )
    _commit(conn)
    return rel

def _write_index(
    conn: sqlite3.Connection,
    output_dir: Path,
    entities: list[dict[str, Any]],
    *,
    propagate: bool,
    rel: str = "index.md",
    topics: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    hubs: list[dict[str, Any]] | None = None,
) -> None:
    text = _index_text(entities, topics=topics, families=families, hubs=hubs)
    canonical = output_dir / "index.md"
    old = canonical.read_text(encoding="utf-8") if canonical.is_file() else ""
    if old:
        fm, new_body = parse_frontmatter(text)
        merged = merge_page(old, fm, new_body)
    else:
        merged = text
    _atomic_write(canonical, merged)
    legacy = output_dir / "INDEX.md"
    try:
        if legacy.is_file() and not legacy.samefile(canonical):
            legacy.unlink()
    except (AttributeError, OSError):
        pass
    _record_file(
        conn,
        "index.md",
        merged,
        kind="index",
        ref_key=None,
        title="Snowball Research Wiki Index",
        propagate=propagate,
    )
    _commit(conn)
def _entity_maps(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    entities = _load_entities(conn)
    by_key = {str(entity["key"]): entity for entity in entities if entity.get("key")}
    return entities, by_key, _load_changes(conn), _load_claims(conn)


def _compile_all(conn: sqlite3.Connection, output_dir: Path, entities: list[dict[str, Any]]) -> int:
    changes = _load_changes(conn)
    claims = _load_claims(conn)
    topics = _load_topics(conn)
    families = _load_families(conn)
    hubs = _load_hubs(conn)
    written = 0
    (output_dir / "entities").mkdir(parents=True, exist_ok=True)
    if topics:
        (output_dir / "topics").mkdir(parents=True, exist_ok=True)
    if hubs:
        (output_dir / "hubs").mkdir(parents=True, exist_ok=True)
    for entity in entities:
        rel = entity_relpath(str(entity.get("name") or ""), entity.get("org"), _year_of(entity))
        key = str(entity.get("key") or "")
        _write_entity_page(
            conn,
            output_dir,
            entity,
            rel,
            changes.get(key, []),
            claims.get(key, []),
            propagate=False,
        )
        written += 1
    for topic in topics:
        _write_topic_page(conn, output_dir, topic, entities, propagate=False)
    for hub in hubs:
        _write_hub_page(conn, output_dir, hub, entities, propagate=False)
    _write_index(conn, output_dir, entities, propagate=False, topics=topics, families=families, hubs=hubs)
    return written


def _compile_cached_row(
    conn: sqlite3.Connection,
    output_dir: Path,
    row: dict[str, Any],
    by_key: dict[str, dict[str, Any]],
    changes: dict[str, list[dict[str, Any]]],
    claims: dict[str, list[str]],
    entities: list[dict[str, Any]],
    *,
    propagate: bool,
    topics: list[dict[str, Any]] | None = None,
    families: list[dict[str, Any]] | None = None,
    hubs: list[dict[str, Any]] | None = None,
) -> str:
    kind = str(row.get("kind") or "")
    rel = Path(str(row.get("path") or "")).as_posix()
    if kind == "entity":
        entity = by_key.get(str(row.get("ref_key") or ""))
        if entity is None:
            return "skipped"
        key = str(entity.get("key") or "")
        _write_entity_page(
            conn,
            output_dir,
            entity,
            rel,
            changes.get(key, []),
            claims.get(key, []),
            propagate=propagate,
        )
        return "entity"
    if kind == "topic":
        ref_key = str(row.get("ref_key") or "")
        topic_match = next((t for t in (topics or []) if str(t.get("key")) == ref_key), None)
        if topic_match:
            _write_topic_page(conn, output_dir, topic_match, entities, propagate=propagate)
            return "topic"
    if kind == "hub":
        ref_key = str(row.get("ref_key") or "")
        hub_match = next((h for h in (hubs or []) if str(h.get("domain")) == ref_key), None)
        if hub_match:
            _write_hub_page(conn, output_dir, hub_match, entities, propagate=propagate)
            return "hub"
    if kind == "family":
        ref_key = str(row.get("ref_key") or "")
        family_match = next((f for f in (families or []) if str(f.get("name")) == ref_key), None)
        if family_match:
            _write_family_page(
                conn, output_dir, family_match, topics or [], entities, propagate=propagate
            )
            return "family"
    if kind == "index":
        _write_index(
            conn,
            output_dir,
            entities,
            propagate=propagate,
            rel="index.md",
            topics=topics,
            families=families,
            hubs=hubs,
        )
        return "index"
    target = output_dir / rel
    if not target.is_file():
        return "skipped"
    text = target.read_text(encoding="utf-8")
    _atomic_write(target, text)
    _record_file(
        conn,
        rel,
        text,
        kind=kind if kind in {"topic", "hub", "index", "entity", "family"} else "topic",
        ref_key=str(row["ref_key"]) if row.get("ref_key") else None,
        title=str(row.get("title") or ""),
        propagate=propagate,
    )
    _commit(conn)
    return "other"

def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    dirty = int(conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE dirty = 1").fetchone()[0])
    stale = int(conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE stale = 1 AND dirty = 0").fetchone()[0])
    return dirty, stale


def compile_wiki(conn: sqlite3.Connection, output_dir: Path, full: bool = False) -> dict[str, int]:
    """Compile entity, topic, hub, family, and index pages.

    Bounded to DIRTY_LIMIT (200) dirty pages, then STALE_LIMIT (50) stale pages within TIME_BUDGET_SEC (600s).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "entities").mkdir(parents=True, exist_ok=True)
    (output_dir / "topics").mkdir(parents=True, exist_ok=True)
    (output_dir / "hubs").mkdir(parents=True, exist_ok=True)
    (output_dir / "families").mkdir(parents=True, exist_ok=True)
    ensure_wiki_schema(conn)

    entities, by_key, changes, claims = _entity_maps(conn)
    topics = _load_topics(conn)
    families = _load_families(conn)
    hubs = _load_hubs(conn)

    # 1. Register all known pages into wiki_pages with dirty=1 on full or initial compile
    for entity in entities:
        key = str(entity.get("key") or "")
        rel = entity.get("wiki_path")
        if not rel:
            rel = entity_relpath(
                str(entity.get("name") or ""),
                entity.get("org"),
                _year_of(entity),
                key=key,
            )
            _touch_wiki_path(conn, key, rel)
            entity["wiki_path"] = rel
        name = str(entity.get("name") or key)
        if full:
            conn.execute("UPDATE wiki_pages SET dirty = 1 WHERE path = ?", (rel,))
        conn.execute(
            """
            INSERT OR IGNORE INTO wiki_pages (
                path, kind, ref_key, title, body_hash, frontmatter, compiled_at, dirty, stale
            ) VALUES (?, 'entity', ?, ?, '', '{}', 0, 1, 0)
            """,
            (rel, key, name),
        )

    for topic in topics:
        t_key = str(topic.get("key") or slugify(str(topic.get("title") or "topic")))
        rel = f"topics/{slugify(t_key)}.md"
        t_title = str(topic.get("title") or t_key)
        if full:
            conn.execute("UPDATE wiki_pages SET dirty = 1 WHERE path = ?", (rel,))
        conn.execute(
            """
            INSERT OR IGNORE INTO wiki_pages (
                path, kind, ref_key, title, body_hash, frontmatter, compiled_at, dirty, stale
            ) VALUES (?, 'topic', ?, ?, '', '{}', 0, 1, 0)
            """,
            (rel, t_key, t_title),
        )

    for hub in hubs:
        h_domain = str(hub.get("domain") or f"hub-{hub.get('page_id')}")
        rel = f"hubs/{slugify(h_domain)}.md"
        if full:
            conn.execute("UPDATE wiki_pages SET dirty = 1 WHERE path = ?", (rel,))
        conn.execute(
            """
            INSERT OR IGNORE INTO wiki_pages (
                path, kind, ref_key, title, body_hash, frontmatter, compiled_at, dirty, stale
            ) VALUES (?, 'hub', ?, ?, '', '{}', 0, 1, 0)
            """,
            (rel, h_domain, f"Hub: {h_domain}"),
        )

    for family in families:
        f_name = str(family.get("name") or "general")
        rel = f"families/{slugify(f_name)}.md"
        if full:
            conn.execute("UPDATE wiki_pages SET dirty = 1 WHERE path = ?", (rel,))
        conn.execute(
            """
            INSERT OR IGNORE INTO wiki_pages (
                path, kind, ref_key, title, body_hash, frontmatter, compiled_at, dirty, stale
            ) VALUES (?, 'family', ?, ?, '', '{}', 0, 1, 0)
            """,
            (rel, f_name, f"Family: {f_name}"),
        )

    if full:
        conn.execute("UPDATE wiki_pages SET dirty = 1 WHERE path = 'index.md'")
    conn.execute(
        """
        INSERT OR IGNORE INTO wiki_pages (
            path, kind, ref_key, title, body_hash, frontmatter, compiled_at, dirty, stale
        ) VALUES ('index.md', 'index', NULL, 'Snowball Research Wiki Index', '', '{}', 0, 1, 0)
        """
    )
    _commit(conn)

    # 2. Bounded compile loop (200 dirty, then 50 stale, within 600 seconds)
    started = time.monotonic()
    dirty_rows = _fetch(
        conn,
        """
        SELECT path, kind, ref_key, title, dirty, stale
        FROM wiki_pages
        WHERE dirty = 1
        ORDER BY path
        LIMIT ?
        """,
        (DIRTY_LIMIT,),
    )
    stale_rows = _fetch(
        conn,
        """
        SELECT path, kind, ref_key, title, dirty, stale
        FROM wiki_pages
        WHERE stale = 1 AND dirty = 0
        ORDER BY stale_marked_at, path
        LIMIT ?
        """,
        (STALE_LIMIT,),
    )

    dirty_compiled = 0
    stale_compiled = 0
    entity_written = 0

    for row in dirty_rows:
        if _budget_exceeded(started):
            break
        outcome = _compile_cached_row(
            conn,
            output_dir,
            row,
            by_key,
            changes,
            claims,
            entities,
            propagate=True,
            topics=topics,
            families=families,
            hubs=hubs,
        )
        if outcome == "skipped":
            continue
        dirty_compiled += 1
        if outcome == "entity":
            entity_written += 1

    for row in stale_rows:
        if _budget_exceeded(started):
            break
        outcome = _compile_cached_row(
            conn,
            output_dir,
            row,
            by_key,
            changes,
            claims,
            entities,
            propagate=False,
            topics=topics,
            families=families,
            hubs=hubs,
        )
        if outcome == "skipped":
            continue
        stale_compiled += 1
        if outcome == "entity":
            entity_written += 1

    remaining_dirty, remaining_stale = _counts(conn)
    _commit(conn)
    return {
        "compiled_entities": entity_written,
        "total_entities": len(entities),
        "total_topics": len(topics),
        "total_hubs": len(hubs),
        "total_families": len(families),
        "dirty_compiled": dirty_compiled,
        "stale_compiled": stale_compiled,
        "remaining_dirty": remaining_dirty,
        "remaining_stale": remaining_stale,
    }


def _kind_for(rel: str) -> str:
    path = Path(rel)
    if path.name.lower() == "index.md":
        return "index"
    head = path.parts[0].lower() if path.parts else ""
    if head == "entities":
        return "entity"
    if head == "hubs":
        return "hub"
    if head == "families":
        return "family"
    if head in {"topics", "concepts"}:
        return "topic"
    return "topic"


def reindex_wiki(conn: sqlite3.Connection, wiki_dir: Path) -> dict[str, Any]:
    """Rebuild wiki_pages, wiki_links, and wiki_fts from Markdown on disk.

    File bytes are the source of truth, including human edits. SQLite errors propagate.
    Includes every markdown file and returns a failure if database count does not match files.
    """
    wiki_dir = Path(wiki_dir)
    ensure_wiki_schema(conn)
    _commit(conn)
    md_files = {p for p in wiki_dir.rglob("*.md") if p.is_file()}
    md_files.update(p for p in wiki_dir.rglob("*.MD") if p.is_file())
    files = sorted(md_files, key=lambda p: str(p))
    began = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        began = True
    except sqlite3.OperationalError as exc:
        if "transaction" not in str(exc).lower():
            raise
    try:
        conn.execute("DELETE FROM wiki_links")
        conn.execute("DELETE FROM wiki_pages")
        conn.execute("DELETE FROM wiki_fts")
        if _has_table(conn, "wiki_regions"):
            conn.execute("DELETE FROM wiki_regions")
        page_count = 0
        link_count = 0
        for path in files:
            rel = path.relative_to(wiki_dir).as_posix()
            content = path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(content)
            title = str(fm.get("title") or fm.get("name") or _first_heading(body) or path.stem)
            ref = fm.get("key")
            if isinstance(ref, list):
                ref = ref[0] if ref else None
            link_count += _record_file(
                conn,
                rel,
                content,
                kind=_kind_for(rel),
                ref_key=str(ref) if ref else None,
                title=title,
                propagate=False,
            )
            page_count += 1
        if began:
            conn.commit()
        else:
            _commit(conn)
    except Exception:
        if began:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.rollback()
        raise
    db_pages = int(conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0])
    fts_count = int(conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0])
    file_count = len(files)
    matched = (db_pages == file_count)
    if not matched:
        return {
            "status": "failure",
            "passed": False,
            "success": False,
            "ok": False,
            "error": f"Database wiki count ({db_pages}) does not match markdown files ({file_count})",
            "indexed_pages": page_count,
            "indexed_links": link_count,
            "pages": db_pages,
            "links": link_count,
            "fts": fts_count,
            "markdown_files": file_count,
            "wiki": db_pages,
        }
    return {
        "status": "ok",
        "passed": True,
        "success": True,
        "ok": True,
        "indexed_pages": page_count,
        "indexed_links": link_count,
        "pages": page_count,
        "links": link_count,
        "fts": fts_count,
        "markdown_files": file_count,
        "wiki": db_pages,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Markdown Wiki Compiler")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    ap.add_argument("--out", type=Path, default=WIKI_DIR, help="Output wiki directory")
    ap.add_argument("--full", action="store_true", help="Recompile every entity page")
    ap.add_argument("--reindex", action="store_true", help="Rebuild the wiki cache from Markdown")
    ap.add_argument("--json", action="store_true", help="Output stats as JSON")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"Database not found at {args.db}. Nothing to compile.")
        return

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if args.reindex:
        stats = reindex_wiki(conn, args.out)
        conn.close()
        if not stats.get("ok", True) or stats.get("status") == "failure":
            if args.json:
                print(json.dumps(stats))
            else:
                print(f"Reindex failed: {stats.get('error', 'count mismatch')}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(stats))
        else:
            print(f"\n❄️  Wiki reindexed -> {args.out}")
            print(f"  • {stats['indexed_pages']} pages")
            print(f"  • {stats['indexed_links']} links\n")
        return

    stats = compile_wiki(conn, args.out, full=args.full)
    conn.close()
    if args.json:
        print(json.dumps(stats))
    else:
        print(f"\n❄️  Wiki Compiled -> {args.out}")
        print(f"  • {stats['compiled_entities']} entity pages updated")
        print(f"  • Index generated at: {args.out / 'index.md'}\n")

if __name__ == "__main__":
    main()
