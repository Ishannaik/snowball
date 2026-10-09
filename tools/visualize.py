#!/usr/bin/env python3
"""Snowball read-only evidence console.

  --terminal (default)  ASCII summary: frontier health, engine spend, domain yield, opportunity radar.
  --serve               Local web dashboard: opportunities with their evidence, knowledge graph, crawl status.

The database is always opened read-only (SQLite URI mode=ro plus query_only); a missing file is an error and is
never created. The server binds 127.0.0.1 unless --host says otherwise, sends no CORS headers, checks the Host
header against the bound name, and serves only its own inline-free assets. Private-topic records never leave it.
"""

from __future__ import annotations

import argparse
import http.server
import json
import re
import socket
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
IST = timezone(timedelta(hours=5, minutes=30))

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
WILDCARD_HOSTS = {"", "0.0.0.0", "::"}
# Caps keep the in-browser force layout responsive; anything cut is reported in the payload's "truncated".
GRAPH_LIMITS = {"topics": 200, "entities": 300, "sources": 300, "hubs": 80}
YIELD_SQL = "(CAST(new_items_total AS REAL) + 1.0) / (CAST(fetch_count AS REAL) + 2.0)"
_TTY_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def get_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the selected database read-only. Never creates a file."""
    p = Path(path or DB_PATH)
    if not p.is_file():
        raise FileNotFoundError(f"Database not found at {p}. Run the orchestrator first; the dashboard never creates it.")
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _public_pages(conn: sqlite3.Connection, alias: str) -> str:
    """SQL predicate that drops pages belonging to a private topic (column added by an orchestrator migration)."""
    if "private" not in _columns(conn, "topics"):
        return "1 = 1"
    return f"({alias}.topic_id IS NULL OR {alias}.topic_id NOT IN (SELECT id FROM topics WHERE private = 1))"


def _deadline_date(text: Any) -> date | None:
    if not isinstance(text, str):
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def _amount_display(amount: Any, currency: Any) -> str | None:
    if amount is None or amount == "":
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    text = f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"
    return f"{currency} {text}" if currency else text


def _facts(*pairs: tuple[str, Any]) -> list[list[str]]:
    return [[label, str(value)] for label, value in pairs if value not in (None, "")]


def _entity_changes(conn: sqlite3.Connection, tables: set[str]) -> dict[str, list[dict[str, Any]]]:
    if "entity_changes" not in tables:
        return {}
    rows = conn.execute(f"""
        SELECT ec.entity_key, ec.field, ec.old, ec.new, ec.seen_at, ec.evidence_quote, p.canon_url AS source_url
        FROM entity_changes ec LEFT JOIN pages p ON p.id = ec.page_id
        WHERE {_public_pages(conn, "p")}
        ORDER BY ec.seen_at DESC, ec.id DESC LIMIT 5000
    """).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        bucket = out.setdefault(r["entity_key"], [])
        if len(bucket) < 8:
            bucket.append({k: r[k] for k in ("field", "old", "new", "seen_at", "evidence_quote", "source_url")})
    return out


def fetch_opportunities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Public verified entities with their evidence page, source/claim counts and recent changes.

    Order is a deadline radar: upcoming deadlines soonest first, then undated, then passed (most recent first).
    """
    tables = _tables(conn)
    sources = "(SELECT COUNT(*) FROM entity_sources es WHERE es.entity_key = e.key)" if "entity_sources" in tables else "0"
    claims = "(SELECT COUNT(*) FROM claims c WHERE c.entity_key = e.key)" if "claims" in tables else "0"
    rows = conn.execute(f"""
        SELECT e.key, e.name, e.org, e.year, e.kind, e.family, e.status, e.amount, e.currency, e.deadline,
               e.eligibility, e.location, e.link, e.relevance, e.urgency, e.evidence_quote,
               e.first_seen_at, e.last_seen_at, e.last_changed_at,
               p.canon_url AS evidence_url, p.domain AS evidence_domain,
               {sources} AS source_count, {claims} AS claim_count
        FROM entities e LEFT JOIN pages p ON p.id = e.evidence_page_id
        WHERE {_public_pages(conn, "p")}
    """).fetchall()
    changes = _entity_changes(conn, tables)
    today = datetime.now(IST).date()
    keyed: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for r in rows:
        o = dict(r)
        due = _deadline_date(o["deadline"])
        o["days_left"] = (due - today).days if due else None
        o["amount_display"] = _amount_display(o["amount"], o["currency"])
        o["changes"] = changes.get(o["key"], [])
        o["change_log"] = "; ".join(f"{c['field']}: {c['old']} -> {c['new']}" for c in o["changes"]) or None
        recency = -int(o["last_changed_at"] or o["last_seen_at"] or 0)
        if due is None:
            rank = (1, recency)
        elif o["days_left"] >= 0:
            rank = (0, due.toordinal())
        else:
            rank = (2, -due.toordinal())
        keyed.append((rank, o))
    keyed.sort(key=lambda pair: pair[0])
    return [o for _, o in keyed]


def fetch_graph_data(conn: sqlite3.Connection) -> dict[str, Any]:
    """Evidence graph: family -> topic -> source page -> entity, domain -> page, hub -> listed entity."""
    tables = _tables(conn)
    pub = _public_pages(conn, "p")
    nodes: dict[str, dict[str, Any]] = {}
    links: list[dict[str, str]] = []
    seen_links: set[tuple[str, str, str]] = set()
    truncated: dict[str, int] = {}

    def add_node(nid: str, ntype: str, name: Any, **extra: Any) -> None:
        if nid not in nodes:
            nodes[nid] = {"id": nid, "type": ntype, "name": str(name) if name not in (None, "") else nid, **extra}

    def link(src: str, dst: str, label: str) -> None:
        key = (src, dst, label)
        if src in nodes and dst in nodes and src != dst and key not in seen_links:
            seen_links.add(key)
            links.append({"source": src, "target": dst, "label": label})

    def family(name: Any) -> str | None:
        if not name:
            return None
        nid = f"family:{name}"
        add_node(nid, "family", name, facts=[])
        return nid

    if "families" in tables:
        for r in conn.execute("SELECT name, focus, recheck_days FROM families ORDER BY name"):
            add_node(
                f"family:{r['name']}", "family", r["name"],
                facts=_facts(("Focus", r["focus"]), ("Recheck", f"every {r['recheck_days']} days")),
            )

    if "topics" in tables:
        where = "WHERE private = 0" if "private" in _columns(conn, "topics") else ""
        total = conn.execute(f"SELECT COUNT(*) FROM topics {where}").fetchone()[0]
        rows = conn.execute(
            f"SELECT id, key, family, title, focus, status, parent_id, depth, new_items_total, fetch_count, "
            f"discovered_via FROM topics {where} ORDER BY new_items_total DESC, id LIMIT ?",
            (GRAPH_LIMITS["topics"],),
        ).fetchall()
        if total > len(rows):
            truncated["topics"] = total
        for r in rows:
            add_node(
                f"topic:{r['id']}", "topic", r["title"] or r["key"],
                facts=_facts(
                    ("Focus", r["focus"]), ("Family", r["family"]), ("Status", r["status"]), ("Depth", r["depth"]),
                    ("Discovered via", r["discovered_via"]), ("New items", r["new_items_total"]),
                    ("Fetches", r["fetch_count"]),
                ),
            )
        for r in rows:
            tid = f"topic:{r['id']}"
            parent = f"topic:{r['parent_id']}" if r["parent_id"] is not None else None
            if parent and parent in nodes:
                link(parent, tid, "spawned")
            else:
                fam = family(r["family"])
                if fam:
                    link(fam, tid, "contains")

    total = conn.execute(f"SELECT COUNT(*) FROM entities e LEFT JOIN pages p ON p.id = e.evidence_page_id WHERE {pub}").fetchone()[0]
    entities = conn.execute(
        f"""SELECT e.key, e.name, e.org, e.kind, e.family, e.status, e.amount, e.currency, e.deadline, e.link,
                   e.relevance, e.evidence_quote, e.evidence_page_id
            FROM entities e LEFT JOIN pages p ON p.id = e.evidence_page_id
            WHERE {pub} ORDER BY e.relevance DESC, e.last_seen_at DESC LIMIT ?""",
        (GRAPH_LIMITS["entities"],),
    ).fetchall()
    if total > len(entities):
        truncated["entities"] = total
    for r in entities:
        relevance = f"{r['relevance']:.2f}" if isinstance(r["relevance"], (int, float)) else None
        add_node(
            f"entity:{r['key']}", "entity", r["name"], url=r["link"], quote=r["evidence_quote"],
            facts=_facts(
                ("Kind", r["kind"]), ("Organisation", r["org"]), ("Amount", _amount_display(r["amount"], r["currency"])),
                ("Deadline", r["deadline"]), ("Status", r["status"]), ("Relevance", relevance),
            ),
        )

    # Evidence and source pages for the entities on the map.
    pairs: list[tuple[str, int, str]] = [
        (r["key"], r["evidence_page_id"], "evidences") for r in entities if r["evidence_page_id"] is not None
    ]
    keys = [r["key"] for r in entities]
    if "entity_sources" in tables and keys:
        marks = ",".join("?" * len(keys))
        pairs += [
            (r[0], r[1], "corroborates")
            for r in conn.execute(f"SELECT entity_key, page_id FROM entity_sources WHERE entity_key IN ({marks})", keys)
        ]
    page_ids = list(dict.fromkeys(pid for _, pid, _ in pairs))
    if len(page_ids) > GRAPH_LIMITS["sources"]:
        truncated["sources"] = len(page_ids)
        page_ids = page_ids[: GRAPH_LIMITS["sources"]]

    page_rows: list[sqlite3.Row] = []
    if page_ids:
        marks = ",".join("?" * len(page_ids))
        page_rows = conn.execute(
            f"SELECT p.id, p.canon_url, p.domain, p.title, p.kind, p.status, p.topic_id, p.http_status, p.route, "
            f"p.fetched_at FROM pages p WHERE p.id IN ({marks}) AND {pub}",
            page_ids,
        ).fetchall()

    if "hubs" in tables:
        hub_sql = (
            f"SELECT p.id, p.canon_url, p.domain, p.title, p.kind, p.status, p.topic_id, p.http_status, p.route, "
            f"p.fetched_at, h.item_count, h.rows_added_total, h.last_diff_at FROM pages p "
            f"LEFT JOIN hubs h ON h.page_id = p.id WHERE p.kind = 'hub' AND {pub} "
            f"ORDER BY COALESCE(h.rows_added_total, 0) DESC, p.new_items_total DESC LIMIT ?"
        )
    else:
        hub_sql = (
            f"SELECT p.id, p.canon_url, p.domain, p.title, p.kind, p.status, p.topic_id, p.http_status, p.route, "
            f"p.fetched_at, NULL AS item_count, NULL AS rows_added_total, NULL AS last_diff_at FROM pages p "
            f"WHERE p.kind = 'hub' AND {pub} ORDER BY p.new_items_total DESC LIMIT ?"
        )
    hub_total = conn.execute(f"SELECT COUNT(*) FROM pages p WHERE p.kind = 'hub' AND {pub}").fetchone()[0]
    hub_rows = conn.execute(hub_sql, (GRAPH_LIMITS["hubs"],)).fetchall()
    if hub_total > len(hub_rows):
        truncated["hubs"] = hub_total

    for r in [*page_rows, *hub_rows]:
        extra = []
        if "item_count" in r.keys() and r["item_count"] is not None:
            extra = [("Rows listed", r["item_count"]), ("Rows added", r["rows_added_total"])]
        add_node(
            f"page:{r['id']}", "hub" if r["kind"] == "hub" else "source", r["title"] or r["canon_url"],
            url=r["canon_url"],
            facts=_facts(
                ("Domain", r["domain"]), ("Kind", r["kind"]), ("Status", r["status"]), ("HTTP", r["http_status"]),
                ("Route", r["route"]), *extra,
            ),
        )

    domains = sorted({r["domain"] for r in [*page_rows, *hub_rows] if r["domain"]})
    domain_info: dict[str, sqlite3.Row] = {}
    if domains and "domains" in tables:
        marks = ",".join("?" * len(domains))
        for r in conn.execute(
            f"SELECT domain, status, fetch_count, walled_count, {YIELD_SQL} AS yield_score FROM domains "
            f"WHERE domain IN ({marks})",
            domains,
        ):
            domain_info[r["domain"]] = r
    for d in domains:
        info = domain_info.get(d)
        add_node(
            f"domain:{d}", "domain", d,
            yield_score=info["yield_score"] if info else None,
            facts=_facts(
                ("Yield", f"{info['yield_score']:.2f}" if info else None), ("Status", info["status"] if info else None),
                ("Fetches", info["fetch_count"] if info else None), ("Walled", info["walled_count"] if info else None),
            ),
        )

    for r in [*page_rows, *hub_rows]:
        pid = f"page:{r['id']}"
        link(f"domain:{r['domain']}", pid, "hosts")
        if r["topic_id"] is not None:
            link(f"topic:{r['topic_id']}", pid, "found")
    for key, page_id, label in pairs:
        link(f"page:{page_id}", f"entity:{key}", label)

    hub_ids = [r["id"] for r in hub_rows]
    if "hub_rows" in tables and hub_ids and keys:
        marks = ",".join("?" * len(hub_ids))
        for r in conn.execute(
            f"SELECT DISTINCT hub_page_id, entity_key FROM hub_rows WHERE entity_key IS NOT NULL AND hub_page_id IN ({marks})",
            hub_ids,
        ):
            link(f"page:{r[0]}", f"entity:{r[1]}", "lists")

    # An entity with no surviving source edge still belongs on the map through its family.
    linked = {lk["target"] for lk in links} | {lk["source"] for lk in links}
    for r in entities:
        eid = f"entity:{r['key']}"
        if eid not in linked:
            fam = family(r["family"])
            if fam:
                link(fam, eid, "classifies")

    return {"nodes": list(nodes.values()), "links": links, "truncated": truncated, "limits": GRAPH_LIMITS}


def _count_by(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, int]:
    return {str(r[0]): r[1] for r in conn.execute(sql, params)}


def fetch_status_metrics(conn: sqlite3.Connection, db_path: Path | None = None) -> dict[str, Any]:
    """Crawl, spend and pipeline health. A key is None when its table does not exist in this database."""
    tables = _tables(conn)
    page_cols = _columns(conn, "pages")
    m: dict[str, Any] = {"generated_at": int(time.time()), "database": Path(db_path).name if db_path else None}

    m["pages_by_status"] = _count_by(conn, "SELECT status, COUNT(*) FROM pages GROUP BY status") if "pages" in tables else None
    m["walls"] = (
        _count_by(conn, "SELECT wall_kind, COUNT(*) FROM pages WHERE wall_kind IS NOT NULL GROUP BY wall_kind")
        if "wall_kind" in page_cols else None
    )
    m["quarantined_pages"] = (
        conn.execute("SELECT COUNT(*) FROM pages WHERE quarantined = 1").fetchone()[0] if "quarantined" in page_cols else None
    )
    m["total_entities"] = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] if "entities" in tables else 0
    m["total_claims"] = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] if "claims" in tables else 0
    m["multi_source_claims"] = (
        conn.execute("SELECT COUNT(*) FROM claims WHERE n_sources >= 2").fetchone()[0]
        if "n_sources" in _columns(conn, "claims") else None
    )

    m["top_domains"] = (
        [
            dict(r)
            for r in conn.execute(
                f"SELECT domain, {YIELD_SQL} AS yield_score, junk_count, walled_count, fetch_count, status "
                f"FROM domains ORDER BY yield_score DESC LIMIT 10"
            )
        ]
        if "domains" in tables else None
    )

    if "engine_spend" in tables:
        day = datetime.now(IST).date().isoformat()
        today = {
            r["engine"]: dict(r)
            for r in conn.execute(
                "SELECT engine, calls, tokens_in + tokens_out AS tokens FROM engine_spend WHERE day = ?", (day,)
            )
        }
        engine_state = (
            {r["engine"]: dict(r) for r in conn.execute(
                "SELECT engine, paused_until, outage_streak, failure_strikes FROM engine_state"
            )}
            if "engine_state" in tables else {}
        )
        spend = []
        for r in conn.execute("""
            SELECT engine, SUM(tokens_in + tokens_out) AS total_tokens, SUM(tokens_in) AS tokens_in,
                   SUM(tokens_out) AS tokens_out, SUM(calls) AS call_count, SUM(outages) AS outage_count,
                   SUM(failures) AS failure_count, COUNT(*) AS days, MAX(day) AS last_day
            FROM engine_spend GROUP BY engine ORDER BY total_tokens DESC
        """):
            row = dict(r)
            row["today_tokens"] = today.get(r["engine"], {}).get("tokens", 0)
            row["today_calls"] = today.get(r["engine"], {}).get("calls", 0)
            st = engine_state.get(r["engine"], {})
            row["paused_until"] = st.get("paused_until")
            row["outage_streak"] = st.get("outage_streak")
            row["failure_strikes"] = st.get("failure_strikes")
            spend.append(row)
        m["engine_spend"] = spend
        m["spend_day_ist"] = day
    else:
        m["engine_spend"] = None

    if "fetches" in tables:
        since = int(time.time()) - 86400
        m["fetch_outcomes_24h"] = _count_by(conn, "SELECT outcome, COUNT(*) FROM fetches WHERE at >= ? GROUP BY outcome", (since,))
        m["fetch_routes_24h"] = _count_by(conn, "SELECT route, COUNT(*) FROM fetches WHERE at >= ? GROUP BY route", (since,))
    else:
        m["fetch_outcomes_24h"] = m["fetch_routes_24h"] = None

    m["tasks_by_status"] = _count_by(conn, "SELECT status, COUNT(*) FROM tasks GROUP BY status") if "tasks" in tables else None
    m["recent_waves"] = (
        [
            dict(r)
            for r in conn.execute("""
                SELECT id, started_at, ended_at, status, fetches, exa_calls, tokens_in, tokens_out,
                       new_entities, changed_entities, new_claims, error IS NOT NULL AS had_error
                FROM waves ORDER BY id DESC LIMIT 8
            """)
        ]
        if "waves" in tables else None
    )

    if "wiki_pages" in tables:
        w = conn.execute("SELECT COUNT(*), COALESCE(SUM(dirty), 0), COALESCE(SUM(stale), 0) FROM wiki_pages").fetchone()
        m["wiki"] = {"pages": w[0], "dirty": w[1], "stale": w[2]}
    else:
        m["wiki"] = None

    if "paid_budget_reservations" in tables:
        now_ist = datetime.now(IST)
        summary = """
            SELECT COUNT(*) AS reservations,
                   COALESCE(SUM(CASE WHEN settled = 1 THEN COALESCE(actual_micro_usd, reserved_micro_usd) END), 0)
                       AS settled_micro_usd,
                   COALESCE(SUM(CASE WHEN settled = 0 THEN reserved_micro_usd END), 0) AS outstanding_micro_usd,
                   COALESCE(SUM(is_solver), 0) AS solver_tasks
            FROM paid_budget_reservations
        """
        m["paid"] = {
            "day": now_ist.date().isoformat(),
            "month": now_ist.strftime("%Y-%m"),
            "today": dict(conn.execute(summary + " WHERE window_day_ist = ?", (now_ist.date().isoformat(),)).fetchone()),
            "month_totals": dict(conn.execute(summary + " WHERE window_month_ist = ?", (now_ist.strftime("%Y-%m"),)).fetchone()),
            "unresolved": dict(conn.execute(summary + " WHERE settled = 0").fetchone()),
        }
    else:
        m["paid"] = None
    return m


def _tty(value: Any, limit: int | None = None) -> str:
    """Strip control characters (ANSI escapes included) from scraped text before it reaches a terminal."""
    text = _TTY_UNSAFE.sub(" ", "" if value is None else str(value)).strip()
    if limit and len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _tty_link(value: Any) -> str:
    if not value:
        return "(none recorded)"
    parsed = urlparse(str(value))
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return _tty(value)
    return "(withheld: not an http(s) link)"


def print_terminal_dashboard(conn: sqlite3.Connection) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="replace")
    metrics = fetch_status_metrics(conn)
    opps = fetch_opportunities(conn)

    print("=" * 78)
    print("   SNOWBALL AUTONOMOUS RESEARCH RADAR: TERMINAL DASHBOARD")
    print("=" * 78)

    pages = metrics["pages_by_status"] or {}
    total_pages = sum(pages.values()) or 1
    print("\n[1] FRONTIER HEALTH & CRAWL STATUS")
    print("-" * 78)
    if not pages:
        print("  (No pages recorded yet.)")
    for status, count in sorted(pages.items()):
        pct = (count / total_pages) * 100
        bars = "#" * int(pct / 4)
        print(f"  {_tty(status):<14} : {count:>5} [{bars:<25}] {pct:>5.1f}%")
    walls = metrics.get("walls") or {}
    extras = []
    if walls:
        extras.append("walled " + ", ".join(f"{_tty(k)}={v}" for k, v in sorted(walls.items())))
    if metrics.get("quarantined_pages") is not None:
        extras.append(f"quarantined={metrics['quarantined_pages']}")
    if extras:
        print("  " + " | ".join(extras))

    print("\n[2] MULTI-MODEL ENGINE SPEND & RESILIENCE (all recorded days)")
    print("-" * 78)
    spend = metrics["engine_spend"]
    if not spend:
        print("  (No model spend recorded.)")
    for eng in spend or []:
        print(
            f"  * {_tty(eng['engine']):<10}: {eng['total_tokens'] or 0:>10,} tokens | {eng['call_count'] or 0:>4} calls | "
            f"{eng['outage_count'] or 0:>3} outages | {eng['failure_count'] or 0:>3} fails | today {eng['today_tokens'] or 0:,}"
        )

    print("\n[3] TOP HUB DOMAIN YIELDS (Laplace Smoothed)")
    print("-" * 78)
    if not metrics["top_domains"]:
        print("  (No domains recorded yet.)")
    for d in metrics["top_domains"] or []:
        yd = d["yield_score"] or 0.0
        bar = "=" * int(min(30, max(0, yd * 30)))
        print(f"  {_tty(d['domain'], 28):<28} : {yd:>5.2f} [{bar:<30}]")

    print("\n[4] DISCOVERED OPPORTUNITIES & DEADLINE RADAR")
    print("-" * 78)
    if not opps:
        print("  (No public opportunities recorded yet. Run a topic crawl to populate.)")
    else:
        for idx, o in enumerate(opps[:10], 1):
            due = o["deadline"] or "none recorded"
            if o["days_left"] is not None:
                due += f" ({o['days_left']}d left)" if o["days_left"] >= 0 else f" (passed {-o['days_left']}d ago)"
            print(f"  {idx:2d}. {_tty(o['name'], 60)} ({_tty(o['org'] or 'organisation unstated', 40)})")
            print(f"      Amount: {_tty(o['amount_display'] or 'not stated', 18):<18} | Deadline: {_tty(due, 32)} | Kind: {_tty(o['kind'])}")
            if o["evidence_quote"]:
                print(f'      Evidence: "{_tty(o["evidence_quote"], 140)}"')
            print(f"      Source: {_tty_link(o['evidence_url'])} | {o['source_count']} source page(s), {o['claim_count']} claim(s)")
            if o.get("change_log"):
                print(f"      Changes: {_tty(o['change_log'], 200)}")
            print(f"      Link: {_tty_link(o['link'])}")
            print()

    print("=" * 78)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Snowball - Evidence Console</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="masthead">
  <div class="brand">
    <h1>Snowball <span>evidence console</span></h1>
    <p class="sub">Read-only view of <code id="db-name">the selected database</code> &middot; <span id="refreshed">not loaded yet</span></p>
  </div>
  <button id="refresh" class="btn" type="button">Refresh data</button>
</header>
<nav class="tabs" role="tablist" aria-label="Dashboard views">
  <button role="tab" type="button" id="tab-opps" aria-controls="panel-opps" aria-selected="true">Opportunities<span class="count" id="count-opps"></span></button>
  <button role="tab" type="button" id="tab-graph" aria-controls="panel-graph" aria-selected="false" tabindex="-1">Knowledge graph<span class="count" id="count-graph"></span></button>
  <button role="tab" type="button" id="tab-status" aria-controls="panel-status" aria-selected="false" tabindex="-1">Crawl status</button>
</nav>
<main id="main">
  <section id="panel-opps" role="tabpanel" aria-labelledby="tab-opps" tabindex="-1">
    <div class="panel-head">
      <h2>Opportunities</h2>
      <p>Every record carries the quote it was verified against and the page that quote came from. Records from private topics are not shown.</p>
    </div>
    <form class="filters" id="opp-filters" role="search" aria-label="Filter opportunities">
      <div class="field q"><label for="f-q">Search</label><input type="search" id="f-q" autocomplete="off" placeholder="Name, organisation, eligibility, quote"></div>
      <div class="field"><label for="f-kind">Kind</label><select id="f-kind"><option value="">All kinds</option></select></div>
      <div class="field"><label for="f-status">Record status</label><select id="f-status"><option value="">All statuses</option></select></div>
      <div class="field"><label for="f-window">Deadline</label>
        <select id="f-window">
          <option value="">Any deadline</option>
          <option value="open">Open or undated</option>
          <option value="week">Due within 7 days</option>
          <option value="month">Due within 30 days</option>
          <option value="undated">No dated deadline</option>
          <option value="past">Deadline passed</option>
        </select></div>
      <div class="field"><label for="f-sort">Order</label>
        <select id="f-sort">
          <option value="deadline">Deadline radar</option>
          <option value="relevance">Relevance</option>
          <option value="recent">Recently changed</option>
          <option value="sources">Most source pages</option>
        </select></div>
    </form>
    <p id="opp-summary" class="summary" aria-live="polite"></p>
    <div id="opp-list" class="cards"></div>
  </section>

  <section id="panel-graph" role="tabpanel" aria-labelledby="tab-graph" tabindex="-1" hidden>
    <div class="panel-head">
      <h2>Knowledge graph</h2>
      <p>How verified opportunities connect to the pages that evidence them, the topics that found those pages and the domains that host them. The layout is deterministic: the same data always draws the same map.</p>
    </div>
    <div class="graph-tools">
      <fieldset class="types" id="graph-types"><legend>Show</legend></fieldset>
      <div class="field"><label for="g-q">Find node</label><input type="search" id="g-q" autocomplete="off" placeholder="Name or URL"></div>
      <div class="btn-row" role="group" aria-label="Graph view controls">
        <button class="btn" id="g-zoom-in" type="button" aria-label="Zoom in">+</button>
        <button class="btn" id="g-zoom-out" type="button" aria-label="Zoom out">&minus;</button>
        <button class="btn" id="g-fit" type="button">Fit</button>
        <button class="btn" id="g-clear" type="button">Clear selection</button>
      </div>
    </div>
    <p id="graph-note" class="summary"></p>
    <div class="graph-layout">
      <div class="graph-stage" id="graph-stage">
        <svg id="graph-svg" tabindex="0" role="group" aria-roledescription="graph canvas" aria-label="Knowledge graph" aria-describedby="graph-help"></svg>
        <div id="graph-overlay" class="overlay" hidden></div>
        <p id="graph-msg" class="stage-state" aria-live="polite"></p>
      </div>
      <aside class="graph-side" aria-label="Graph details">
        <section class="side-box" id="graph-detail" aria-live="polite"></section>
        <section class="side-box">
          <h3>Nodes by connections</h3>
          <p id="graph-list-note" class="summary"></p>
          <ul id="graph-list" class="node-list"></ul>
        </section>
      </aside>
    </div>
    <p id="graph-help" class="note">Canvas: drag to pan, scroll to zoom, click a node to inspect it. With the canvas focused, arrow keys pan, + and &minus; zoom, 0 fits and Escape clears the selection. The node list and the connection buttons reach every node without a pointer.</p>
  </section>

  <section id="panel-status" role="tabpanel" aria-labelledby="tab-status" tabindex="-1" hidden>
    <div class="panel-head">
      <h2>Crawl status</h2>
      <p>Frontier health, fetch outcomes, model spend and paid-access reservations as currently recorded in the database.</p>
    </div>
    <div id="status-body"></div>
  </section>
</main>
<div id="sr-status" class="sr-only" role="status" aria-live="polite"></div>
<script src="/app.js" defer></script>
</body>
</html>
"""

DASHBOARD_CSS = r"""
:root{
  --paper:#f5f2ea;--paper-2:#ebe6d8;--card:#fbf9f4;--ink:#1c1a16;--ink-2:#45413a;--muted:#635e53;--rule:#d6cfbd;--edge:#b3aa93;
  --accent:#b23c12;--verified:#0d6457;--focus:#1d4ed8;
  --n-family:#6b4f8f;--n-topic:#2f5f8f;--n-domain:#8a6d2f;--n-source:#5c7d70;--n-hub:#b8681c;--n-entity:#0d6457;
  --serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Palatino,Charter,Georgia,serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#15140f;--paper-2:#221f18;--card:#1c1a14;--ink:#ece6d6;--ink-2:#cdc6b3;--muted:#a59e8a;--rule:#3b372c;--edge:#5a5444;
  --accent:#f0895a;--verified:#62c6b2;--focus:#9db9ff;
  --n-family:#b79dd8;--n-topic:#82b2e2;--n-domain:#d4b670;--n-source:#9fc4b5;--n-hub:#f0a65c;--n-entity:#62c6b2;
}}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
[hidden]{display:none!important}
.skip{position:absolute;left:-9999px;top:0}
.skip:focus{left:1rem;top:1rem;z-index:20;background:var(--ink);color:var(--paper);padding:.5rem .8rem}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.muted{color:var(--muted)}
code,.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
a{color:var(--verified);text-underline-offset:2px;overflow-wrap:anywhere}
a.ext::after{content:" \2197";font-size:.85em}

.masthead{display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-end;justify-content:space-between;padding:1.4rem clamp(1rem,4vw,2.5rem) 1rem;border-bottom:3px double var(--rule)}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(1.6rem,3.2vw,2.2rem);letter-spacing:-.01em;margin:0;line-height:1.1}
h1 span{font-style:italic;font-weight:400;color:var(--ink-2)}
.sub{margin:.3rem 0 0;color:var(--muted);font-size:.875rem}
.btn{font:inherit;font-size:.9rem;padding:.45rem .9rem;border:1px solid var(--ink);background:transparent;color:var(--ink);border-radius:2px;cursor:pointer;min-height:44px;min-width:44px}
.btn:hover{background:var(--ink);color:var(--paper)}
.btn:disabled{opacity:.55;cursor:progress}
.btn-row{display:flex;flex-wrap:wrap;gap:.4rem}

.tabs{display:flex;padding:0 clamp(1rem,4vw,2.5rem);border-bottom:1px solid var(--rule);overflow-x:auto}
.tabs [role=tab]{font:inherit;font-family:var(--serif);font-size:1.08rem;background:none;border:0;border-bottom:3px solid transparent;padding:.8rem 1rem .55rem;color:var(--muted);cursor:pointer;white-space:nowrap;min-height:44px}
.tabs [role=tab]:hover{color:var(--ink)}
.tabs [role=tab][aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.count{font-family:var(--mono);font-size:.75rem;color:var(--muted);margin-left:.4rem}
.count:empty{display:none}

main{padding:1.5rem clamp(1rem,4vw,2.5rem) 3rem;max-width:1440px;margin:0 auto}
[role=tabpanel]:focus{outline:none}
.panel-head h2{font-family:var(--serif);font-size:1.6rem;margin:0 0 .25rem;font-weight:600}
.panel-head p{margin:0 0 1.1rem;color:var(--ink-2);max-width:70ch}
.summary{font-family:var(--mono);font-size:.8rem;color:var(--muted);margin:.25rem 0 1rem}
.summary:empty{display:none}
.note{font-size:.82rem;color:var(--muted);margin:.75rem 0 0;max-width:80ch}

.filters{display:grid;grid-template-columns:minmax(14rem,2fr) repeat(4,minmax(9rem,1fr));gap:.75rem;align-items:end;margin-bottom:.5rem}
.field{display:flex;flex-direction:column;gap:.25rem;min-width:0}
.field label,fieldset.types legend{font-size:.85rem;color:var(--muted)}
input[type=search],select{font:inherit;font-size:.95rem;padding:.5rem .65rem;border:1px solid var(--rule);background:var(--card);color:var(--ink);border-radius:2px;min-height:44px;width:100%}
input[type=search]:focus,select:focus{border-color:var(--ink)}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,23rem),1fr));gap:1rem}
.card{background:var(--card);border:1px solid var(--rule);border-top:3px solid var(--ink-2);padding:1rem 1.15rem;display:flex;flex-direction:column;gap:.65rem;min-width:0}
.card.soon{border-top-color:var(--accent)}
.card.past{border-top-style:dashed}
.meta-line{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem .8rem;font-size:.85rem;color:var(--muted);margin:0}
.tag{border:1px solid var(--rule);padding:0 .35rem;border-radius:2px}
.card h3{font-family:var(--serif);font-size:1.25rem;line-height:1.25;margin:0;font-weight:600;overflow-wrap:anywhere}
.org{margin:-.4rem 0 0;color:var(--ink-2)}
dl.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:.2rem .9rem;margin:0;font-size:.92rem}
dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0;overflow-wrap:anywhere}
.stamp{font-family:var(--mono);font-size:.78rem;padding:0 .35rem;border:1px solid currentColor;border-radius:2px;margin-left:.45rem;white-space:nowrap}
.stamp.soon{color:var(--accent);font-weight:600}
.stamp.past{color:var(--muted)}
.evidence{margin:0;padding:.6rem .85rem;border-left:3px solid var(--verified);background:var(--paper-2)}
.evidence .label{display:block;font-size:.82rem;color:var(--verified);font-weight:700;margin-bottom:.2rem}
.evidence blockquote{margin:0;font-family:var(--serif);font-size:1rem;line-height:1.5;overflow-wrap:anywhere}
.evidence blockquote::before{content:"\201C"}
.evidence blockquote::after{content:"\201D"}
.evidence figcaption{margin-top:.4rem;font-size:.82rem;color:var(--ink-2)}
details.changes summary{cursor:pointer;font-size:.88rem;color:var(--ink-2);min-height:32px;padding:.2rem 0}
details.changes ol{margin:.3rem 0 0;padding-left:1.2rem;font-size:.86rem;display:flex;flex-direction:column;gap:.45rem}
blockquote.mini{margin:.2rem 0 0;padding-left:.6rem;border-left:2px solid var(--rule);font-family:var(--serif);color:var(--ink-2);overflow-wrap:anywhere}
.seen{margin:auto 0 0;font-size:.78rem;color:var(--muted);font-family:var(--mono)}

.state{padding:2rem 1.25rem;text-align:center;color:var(--ink-2);border:1px dashed var(--rule);background:var(--card);grid-column:1/-1}
.state h3{font-family:var(--serif);font-size:1.15rem;margin:0 0 .35rem;font-weight:600}
.state p{margin:0 auto .75rem;max-width:60ch}
.state.error{border:1px solid var(--accent);color:var(--ink)}
.state.error h3{color:var(--accent)}
.loading::before{content:"";display:inline-block;width:.85rem;height:.85rem;border:2px solid var(--rule);border-top-color:var(--ink);border-radius:50%;margin-right:.55rem;vertical-align:-1px;animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.graph-tools{display:flex;flex-wrap:wrap;gap:.8rem 1.4rem;align-items:flex-end;margin-bottom:.5rem}
fieldset.types{border:0;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:.2rem .9rem;min-width:0}
fieldset.types legend{padding:0;margin-bottom:.15rem}
.chk{display:inline-flex;align-items:center;gap:.4rem;font-size:.9rem;min-height:36px;cursor:pointer}
.chk input{width:1.05rem;height:1.05rem;accent-color:var(--ink)}
.swatch{width:.75rem;height:.75rem;border-radius:50%;display:inline-block;flex:none}
.sw-family{background:var(--n-family)}.sw-topic{background:var(--n-topic)}.sw-domain{background:var(--n-domain)}
.sw-source{background:var(--n-source)}.sw-hub{background:var(--n-hub)}.sw-entity{background:var(--n-entity)}
.graph-layout{display:grid;grid-template-columns:minmax(0,1fr) 21rem;gap:1rem}
.graph-stage{position:relative;border:1px solid var(--rule);background:var(--card);height:min(72vh,680px);min-height:360px;overflow:hidden}
#graph-svg{width:100%;height:100%;display:block;touch-action:none;cursor:grab}
#graph-svg.panning{cursor:grabbing}
.overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:1rem;background:var(--card)}
.overlay .state{max-width:34rem}
.stage-state{position:absolute;left:0;right:0;bottom:0;margin:0;padding:.45rem .75rem;font-family:var(--mono);font-size:.8rem;color:var(--ink-2);background:var(--card);border-top:1px solid var(--rule)}
.stage-state:empty{display:none}
svg .edge{stroke:var(--edge);stroke-width:1;vector-effect:non-scaling-stroke}
svg .edge.l-evidences,svg .edge.l-corroborates,svg .edge.l-lists{stroke:var(--verified);stroke-opacity:.5}
svg .node circle{stroke:var(--card);stroke-width:1.5;cursor:pointer;vector-effect:non-scaling-stroke}
svg .t-family circle{fill:var(--n-family)}svg .t-topic circle{fill:var(--n-topic)}svg .t-domain circle{fill:var(--n-domain)}
svg .t-source circle{fill:var(--n-source)}svg .t-hub circle{fill:var(--n-hub)}svg .t-entity circle{fill:var(--n-entity)}
svg .node text{font:11px var(--sans);fill:var(--ink);paint-order:stroke;stroke:var(--card);stroke-width:3px;stroke-linejoin:round;pointer-events:none;display:none}
svg .node.lbl text,svg.zoomed .node text,svg .node.sel text,svg .node.nb text,svg .node.match text{display:inline}
svg.searching .node:not(.match){opacity:.18}
svg.has-sel .node:not(.sel):not(.nb){opacity:.16}
svg.has-sel .edge:not(.hot){opacity:.1}
svg .edge.hot{stroke:var(--ink);stroke-opacity:.85;stroke-width:1.6}
svg .node.sel circle{stroke:var(--accent);stroke-width:3}
svg .node.sel text{font-weight:700}
svg .hide{display:none}
.graph-side{display:flex;flex-direction:column;gap:1rem;min-width:0}
.side-box{border:1px solid var(--rule);background:var(--card);padding:.9rem 1rem;min-width:0}
.side-box h3{font-family:var(--serif);font-size:1.12rem;margin:.1rem 0 .5rem;overflow-wrap:anywhere}
.side-box h3:focus{outline:none}
.side-box h4{font-size:.88rem;color:var(--muted);margin:1rem 0 .3rem;font-weight:600}
.side-box .evidence{margin-top:.6rem}
.side-box .summary{margin:0 0 .5rem}
.node-list,.nb-list{list-style:none;margin:0;padding:0}
.node-list{max-height:24rem;overflow:auto}
.nb-list{max-height:18rem;overflow:auto}
.node-list button,.nb-list button{font:inherit;font-size:.88rem;text-align:left;width:100%;background:none;border:0;border-bottom:1px solid var(--rule);padding:.4rem .3rem;color:var(--ink);cursor:pointer;display:flex;gap:.5rem;align-items:center;min-height:40px}
.node-list button:hover,.nb-list button:hover{background:var(--paper-2)}
.node-list button[aria-pressed=true]{background:var(--paper-2);font-weight:600}
.node-list .nm,.nb-list .nm{min-width:0;overflow-wrap:anywhere}
.node-list .deg{margin-left:auto;font-family:var(--mono);font-size:.75rem;color:var(--muted)}
.nb-list .rel{font-family:var(--mono);font-size:.72rem;color:var(--muted);white-space:nowrap}

.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,12rem),1fr));gap:.75rem;margin-bottom:1.5rem}
.tile{border:1px solid var(--rule);border-top:3px solid var(--ink-2);background:var(--card);padding:.75rem 1rem}
.tile .k{font-size:.85rem;color:var(--muted)}
.tile .v{font-family:var(--mono);font-size:1.65rem;font-variant-numeric:tabular-nums;line-height:1.25}
.tile .n{font-size:.8rem;color:var(--ink-2);overflow-wrap:anywhere}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,27rem),1fr));gap:1.25rem;align-items:start}
.tbl-wrap{overflow-x:auto;border:1px solid var(--rule);background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:.88rem}
caption{text-align:left;font-family:var(--serif);font-size:1.1rem;font-weight:600;padding:.75rem .9rem .35rem}
th,td{padding:.45rem .9rem;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}
th{font-size:.85rem;color:var(--muted);font-weight:600}
td{overflow-wrap:anywhere}
td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl-wrap .note{padding:0 .9rem .75rem;margin:.5rem 0 0}
.bar{height:.55rem;background:var(--paper-2);min-width:5rem;position:relative;margin-top:.45rem}
.bar>span{position:absolute;left:0;top:0;bottom:0;background:var(--ink-2)}
.foot{margin-top:1.25rem}

@media (max-width:960px){
  .graph-layout{grid-template-columns:1fr}
  .graph-stage{height:60vh}
  .filters{grid-template-columns:1fr 1fr}
  .filters .field.q{grid-column:1/-1}
}
@media (max-width:540px){
  .filters{grid-template-columns:1fr}
  .masthead{align-items:flex-start}
  .tabs [role=tab]{padding:.75rem .7rem .5rem;font-size:1rem}
}
@media (prefers-reduced-motion:reduce){.loading::before{animation:none}}
"""

DASHBOARD_JS = r"""'use strict';
(() => {
  const TYPES = [
    ['entity', 'Opportunities'], ['source', 'Source pages'], ['hub', 'Hub pages'],
    ['topic', 'Topics'], ['domain', 'Domains'], ['family', 'Families'],
  ];
  const TYPE_LABEL = Object.fromEntries(TYPES);
  const TAB_IDS = ['opps', 'graph', 'status'];
  const LIST_LIMIT = 150;
  const state = { opps: [], g: null, loading: false };
  const prefs = { hidden: new Set(), q: '' };
  let graphToken = 0;

  const $ = (id) => document.getElementById(id);

  // Optional children are dropped (null/undefined/booleans), but 0 and '' are kept on purpose.
  function kidNodes(kids) {
    return kids.flat(Infinity)
      .filter((c) => c !== null && c !== undefined && typeof c !== 'boolean')
      .map((c) => (c instanceof Node ? c : document.createTextNode(String(c))));
  }
  // All DOM is built here; strings always become text nodes, never markup.
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? '' : String(v));
      }
    }
    n.append(...kidNodes(kids));
    return n;
  }
  function svgEl(tag, attrs) {
    const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    return n;
  }
  const truncate = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const plural = (n, word) => `${fmtNum(n)} ${word}${Number(n) === 1 ? '' : 's'}`;
  const safeClass = (s) => String(s || '').toLowerCase().replace(/[^a-z]/g, '');
  function fmtNum(n) { return n === null || n === undefined || n === '' || isNaN(Number(n)) ? '—' : Number(n).toLocaleString(); }
  function fmtTime(sec) {
    if (!sec) return '—';
    const d = new Date(Number(sec) * 1000);
    return isNaN(d.getTime()) ? '—' : d.toLocaleString();
  }
  function fmtUsd(micro) { return micro === null || micro === undefined ? '—' : '$' + (Number(micro) / 1e6).toFixed(2); }
  function announce(msg) { $('sr-status').textContent = msg; }

  function safeUrl(u) {
    if (typeof u !== 'string' || !u) return null;
    try {
      const x = new URL(u);
      return (x.protocol === 'http:' || x.protocol === 'https:') ? x.href : null;
    } catch (_) { return null; }
  }
  function linkTo(u, label) {
    if (!u) return el('span', { class: 'muted' }, 'none recorded');
    const href = safeUrl(u);
    if (!href) return el('span', { class: 'muted' }, 'withheld: not an http(s) link');
    let text = label;
    if (!text) { try { const x = new URL(href); text = x.host + (x.pathname.length > 1 ? truncate(x.pathname, 40) : ''); } catch (_) { text = href; } }
    return el('a', { href, class: 'ext', target: '_blank', rel: 'noopener noreferrer nofollow' }, text);
  }
  function stateBox(kind, title, detail, action, actionLabel) {
    return el('div', { class: 'state ' + kind, role: kind === 'error' ? 'alert' : 'status' },
      el('h3', { class: kind === 'loading' ? 'loading' : null }, title),
      detail ? el('p', null, detail) : null,
      action ? el('button', { class: 'btn', type: 'button', onclick: action }, actionLabel || 'Try again') : null);
  }

  async function getJSON(path) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 30000);
    try {
      const r = await fetch(path, { signal: ctl.signal, cache: 'no-store', headers: { Accept: 'application/json' } });
      let body = null;
      try { body = await r.json(); } catch (_) { body = null; }
      if (!r.ok) throw new Error((body && body.message) || `Server answered HTTP ${r.status}`);
      if (body === null) throw new Error('The server returned a response that is not JSON.');
      return body;
    } catch (e) {
      if (e && e.name === 'AbortError') throw new Error('The request timed out after 30 seconds.');
      if (e instanceof TypeError) throw new Error('Could not reach the dashboard server. Is it still running?');
      throw e;
    } finally { clearTimeout(timer); }
  }

  /* ---------- tabs ---------- */
  function selectTab(name, focus) {
    if (!TAB_IDS.includes(name)) name = 'opps';
    for (const t of TAB_IDS) {
      const on = t === name;
      const tab = $('tab-' + t);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.tabIndex = on ? 0 : -1;
      $('panel-' + t).hidden = !on;
    }
    if (focus) $('tab-' + name).focus();
    if (history.replaceState) history.replaceState(null, '', '#' + name);
    if (name === 'graph' && state.g) { resizeStage(); if (state.g.needsFit) fitView(); }
  }
  function initTabs() {
    TAB_IDS.forEach((t, i) => {
      const tab = $('tab-' + t);
      tab.addEventListener('click', () => selectTab(t, false));
      tab.addEventListener('keydown', (ev) => {
        let j = null;
        if (ev.key === 'ArrowRight') j = (i + 1) % TAB_IDS.length;
        else if (ev.key === 'ArrowLeft') j = (i - 1 + TAB_IDS.length) % TAB_IDS.length;
        else if (ev.key === 'Home') j = 0;
        else if (ev.key === 'End') j = TAB_IDS.length - 1;
        if (j !== null) { ev.preventDefault(); selectTab(TAB_IDS[j], true); }
      });
    });
    selectTab(location.hash.slice(1), false);
  }

  /* ---------- refresh ---------- */
  async function refreshAll() {
    if (state.loading) return;
    state.loading = true;
    const btn = $('refresh');
    btn.disabled = true;
    btn.textContent = 'Refreshing…';
    announce('Loading dashboard data.');
    $('opp-summary').textContent = '';
    $('opp-list').replaceChildren(stateBox('loading', 'Loading opportunities…'));
    $('status-body').replaceChildren(stateBox('loading', 'Loading crawl status…'));
    setOverlay(stateBox('loading', 'Loading knowledge graph…'));
    setGraphMsg('');

    const [opps, graph, status] = await Promise.allSettled([getJSON('/api/entities'), getJSON('/api/graph'), getJSON('/api/status')]);

    if (opps.status === 'fulfilled' && Array.isArray(opps.value)) {
      state.opps = opps.value;
      populateOppFilters();
      renderOpps();
    } else {
      state.opps = [];
      $('count-opps').textContent = '';
      const msg = opps.status === 'rejected' ? opps.reason.message : 'Unexpected response shape.';
      $('opp-list').replaceChildren(stateBox('error', 'Could not load opportunities', msg, refreshAll));
    }
    if (graph.status === 'fulfilled' && graph.value && Array.isArray(graph.value.nodes)) {
      initGraph(graph.value);
    } else {
      graphToken++;
      state.g = null;
      $('count-graph').textContent = '';
      $('graph-svg').replaceChildren();
      $('graph-types').replaceChildren(el('legend', null, 'Show'));
      $('graph-list').replaceChildren();
      $('graph-list-note').textContent = '';
      $('graph-note').textContent = '';
      renderDetail(null);
      const msg = graph.status === 'rejected' ? graph.reason.message : 'Unexpected response shape.';
      setOverlay(stateBox('error', 'Could not load the knowledge graph', msg, refreshAll));
    }
    if (status.status === 'fulfilled' && status.value && typeof status.value === 'object') {
      renderStatus(status.value);
    } else {
      const msg = status.status === 'rejected' ? status.reason.message : 'Unexpected response shape.';
      $('status-body').replaceChildren(stateBox('error', 'Could not load crawl status', msg, refreshAll));
    }

    const failed = [opps, graph, status].filter((r) => r.status === 'rejected').length;
    const at = new Date().toLocaleTimeString();
    $('refreshed').textContent = failed ? `${failed} of 3 requests failed at ${at}` : `refreshed ${at}`;
    announce(failed ? `Refresh finished; ${failed} of 3 requests failed.` : 'Dashboard data refreshed.');
    btn.disabled = false;
    btn.textContent = 'Refresh data';
    state.loading = false;
  }

  /* ---------- opportunities ---------- */
  const uniq = (xs) => [...new Set(xs.filter((x) => typeof x === 'string' && x))].sort();
  function fillSelect(sel, allLabel, values) {
    const prev = sel.value;
    sel.replaceChildren(el('option', { value: '' }, allLabel), ...values.map((v) => el('option', { value: v }, v)));
    sel.value = values.includes(prev) ? prev : '';
  }
  function populateOppFilters() {
    fillSelect($('f-kind'), 'All kinds', uniq(state.opps.map((o) => o.kind)));
    fillSelect($('f-status'), 'All statuses', uniq(state.opps.map((o) => o.status)));
    $('count-opps').textContent = String(state.opps.length);
  }
  function clearOppFilters() {
    $('f-q').value = ''; $('f-kind').value = ''; $('f-status').value = ''; $('f-window').value = '';
    renderOpps();
    $('f-q').focus();
  }
  function oppMatches(o, f) {
    if (f.kind && o.kind !== f.kind) return false;
    if (f.status && o.status !== f.status) return false;
    const d = (typeof o.days_left === 'number') ? o.days_left : null;
    if (f.window === 'week' && (d === null || d < 0 || d > 7)) return false;
    if (f.window === 'month' && (d === null || d < 0 || d > 30)) return false;
    if (f.window === 'open' && d !== null && d < 0) return false;
    if (f.window === 'past' && (d === null || d >= 0)) return false;
    if (f.window === 'undated' && d !== null) return false;
    if (f.q) {
      const hay = [o.name, o.org, o.kind, o.eligibility, o.location, o.evidence_quote, o.evidence_domain]
        .filter((x) => typeof x === 'string').join(' ').toLowerCase();
      if (!hay.includes(f.q)) return false;
    }
    return true;
  }
  function sortOpps(list, by) {
    const out = list.slice(); // server order is the deadline radar
    const num = (x) => (typeof x === 'number' ? x : 0);
    if (by === 'relevance') out.sort((a, b) => num(b.relevance) - num(a.relevance));
    else if (by === 'recent') out.sort((a, b) => num(b.last_changed_at || b.first_seen_at) - num(a.last_changed_at || a.first_seen_at));
    else if (by === 'sources') out.sort((a, b) => num(b.source_count) - num(a.source_count));
    return out;
  }
  function renderOpps() {
    const list = $('opp-list');
    const summary = $('opp-summary');
    if (!state.opps.length) {
      summary.textContent = '';
      list.replaceChildren(stateBox('empty', 'No opportunities recorded yet',
        'This database holds no public verified entities. One appears here once a crawl extracts it and its evidence quote is found verbatim in the fetched page.'));
      return;
    }
    const f = { q: $('f-q').value.trim().toLowerCase(), kind: $('f-kind').value, status: $('f-status').value, window: $('f-window').value };
    const shown = sortOpps(state.opps.filter((o) => oppMatches(o, f)), $('f-sort').value);
    summary.textContent = `Showing ${fmtNum(shown.length)} of ${plural(state.opps.length, 'verified record')}`;
    if (!shown.length) {
      list.replaceChildren(stateBox('empty', 'No records match these filters', 'Clear the search or widen the deadline window.', clearOppFilters, 'Clear filters'));
      return;
    }
    list.replaceChildren(...shown.map(oppCard));
  }
  function deadlineStamp(d) {
    if (typeof d !== 'number') return null;
    if (d < 0) return el('span', { class: 'stamp past' }, `closed ${-d}d ago`);
    if (d === 0) return el('span', { class: 'stamp soon' }, 'due today');
    return el('span', { class: 'stamp' + (d <= 7 ? ' soon' : '') }, `T−${d}d`);
  }
  function oppCard(o) {
    const d = typeof o.days_left === 'number' ? o.days_left : null;
    const facts = el('dl', { class: 'facts' });
    const fact = (k, ...v) => facts.append(el('dt', null, k), el('dd', null, ...v));
    fact('Amount', o.amount_display || el('span', { class: 'muted' }, 'not stated in source'));
    fact('Deadline', o.deadline || el('span', { class: 'muted' }, 'none recorded'), deadlineStamp(d));
    if (o.location) fact('Location', o.location);
    fact('Eligibility', o.eligibility || el('span', { class: 'muted' }, 'not stated in source'));
    fact('Listing', linkTo(o.link));
    const changes = Array.isArray(o.changes) ? o.changes : [];
    return el('article', { class: 'card' + (d === null ? '' : d < 0 ? ' past' : d <= 7 ? ' soon' : '') },
      el('p', { class: 'meta-line' },
        el('span', null, o.kind || 'unclassified'),
        o.family ? el('span', null, o.family) : null,
        o.status ? el('span', { class: 'tag' }, o.status) : null,
        typeof o.relevance === 'number' ? el('span', null, `relevance ${o.relevance.toFixed(2)}`) : null),
      el('h3', null, o.name || '(unnamed record)'),
      o.org ? el('p', { class: 'org' }, o.org, o.year ? ` · ${o.year}` : '') : null,
      facts,
      el('figure', { class: 'evidence' },
        el('span', { class: 'label' }, 'Verified quote'),
        o.evidence_quote ? el('blockquote', null, o.evidence_quote) : el('p', { class: 'muted' }, 'No quote stored for this record.'),
        el('figcaption', null, 'Source: ', linkTo(o.evidence_url, o.evidence_domain || null),
          ` · ${plural(o.source_count, 'source page')} · ${plural(o.claim_count, 'claim')}`)),
      changes.length ? el('details', { class: 'changes' },
        el('summary', null, plural(changes.length, 'recorded change')),
        el('ol', null, ...changes.map((c) => el('li', null,
          el('strong', null, c.field || 'field'), ': ',
          c.old === null || c.old === undefined ? el('span', { class: 'muted' }, '(none)') : String(c.old),
          ' → ', String(c.new ?? ''),
          el('span', { class: 'muted' }, ` · ${fmtTime(c.seen_at)}`),
          c.evidence_quote ? el('blockquote', { class: 'mini' }, c.evidence_quote) : null,
          c.source_url ? el('div', null, linkTo(c.source_url)) : null)))) : null,
      el('p', { class: 'seen' }, `first seen ${fmtTime(o.first_seen_at)}` + (o.last_changed_at ? ` · changed ${fmtTime(o.last_changed_at)}` : '')));
  }
  function initOppFilters() {
    $('opp-filters').addEventListener('submit', (ev) => ev.preventDefault());
    let t = null;
    $('f-q').addEventListener('input', () => { clearTimeout(t); t = setTimeout(renderOpps, 120); });
    for (const id of ['f-kind', 'f-status', 'f-window', 'f-sort']) $(id).addEventListener('change', renderOpps);
  }

  /* ---------- knowledge graph ---------- */
  function setOverlay(node) { const o = $('graph-overlay'); if (node) { o.replaceChildren(node); o.hidden = false; } else { o.replaceChildren(); o.hidden = true; } }
  function setGraphMsg(text) { $('graph-msg').textContent = text; }

  function initGraph(data) {
    const token = ++graphToken;
    const nodes = data.nodes.filter((n) => n && typeof n.id === 'string')
      .map((n, i) => ({ ...n, i, type: safeClass(n.type), name: String(n.name ?? n.id), deg: 0, vis: true, match: false, elm: null, btn: null }));
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const edges = [];
    const adj = new Map(nodes.map((n) => [n.i, []]));
    for (const l of data.links || []) {
      const a = byId.get(l.source), b = byId.get(l.target);
      if (!a || !b || a === b) continue;
      const e = { s: a.i, t: b.i, label: String(l.label || 'related'), cls: safeClass(l.label), elm: null };
      edges.push(e);
      a.deg++; b.deg++;
      adj.get(a.i).push({ edge: e, other: b.i, out: true });
      adj.get(b.i).push({ edge: e, other: a.i, out: false });
    }
    state.g = { nodes, edges, adj, sel: null, view: null, tx: 0, ty: 0, k: 1, w: 0, h: 0, needsFit: true, dragMoved: false };
    $('count-graph').textContent = String(nodes.length);
    $('graph-svg').replaceChildren();
    $('graph-svg').classList.remove('has-sel', 'searching', 'zoomed');

    const cut = Object.entries(data.truncated || {});
    const limits = data.limits || {};
    $('graph-note').textContent = cut.length
      ? 'Capped for readability: ' + cut.map(([k, total]) => `${fmtNum(limits[k])} of ${fmtNum(total)} ${k}`).join(', ') + '. Opportunities are kept in order of relevance.'
      : `${plural(nodes.length, 'node')}, ${plural(edges.length, 'connection')}.`;

    renderTypeFilters();
    applyVisibility();
    renderNodeList();
    renderDetail(null);

    if (!nodes.length) {
      setGraphMsg('');
      setOverlay(stateBox('empty', 'The knowledge graph is empty', 'No public topics, source pages or verified entities are recorded in this database yet.'));
      return;
    }
    setOverlay(null);
    setGraphMsg(`Laying out ${plural(nodes.length, 'node')}…`);
    layout(state.g, token, () => {
      drawGraph();
      setGraphMsg('');
      fitView();
    });
  }

  // Deterministic force layout: phyllotaxis seed, pairwise repulsion, degree-weighted springs, weak gravity.
  function layout(g, token, done) {
    const nodes = g.nodes, n = nodes.length;
    const x = new Float64Array(n), y = new Float64Array(n), vx = new Float64Array(n), vy = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const r = 14 * Math.sqrt(i + 0.5), a = i * 2.399963229728653;
      x[i] = r * Math.cos(a); y[i] = r * Math.sin(a);
    }
    const ITER = 300;
    const perFrame = Math.max(1, Math.floor(1.5e6 / Math.max(1, n * n)));
    let it = 0, alpha = 1;
    function tick() {
      for (let i = 0; i < n; i++) {
        const xi = x[i], yi = y[i];
        for (let j = i + 1; j < n; j++) {
          let dx = xi - x[j], dy = yi - y[j];
          let d2 = dx * dx + dy * dy;
          if (d2 > 360000) continue;
          if (d2 < 0.01) { dx = ((i % 7) - 3) * 0.1 + 0.05; dy = ((j % 5) - 2) * 0.1 + 0.05; d2 = dx * dx + dy * dy; }
          const f = 700 * alpha / d2;
          vx[i] += dx * f; vy[i] += dy * f; vx[j] -= dx * f; vy[j] -= dy * f;
        }
      }
      for (const e of g.edges) {
        const a = e.s, b = e.t, da = nodes[a].deg, db = nodes[b].deg;
        const dx = x[b] - x[a], dy = y[b] - y[a];
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = (d - 55) / d * alpha * 0.6 / Math.min(da, db);
        const bias = da / (da + db);
        vx[b] -= dx * f * bias; vy[b] -= dy * f * bias;
        vx[a] += dx * f * (1 - bias); vy[a] += dy * f * (1 - bias);
      }
      for (let i = 0; i < n; i++) {
        vx[i] = (vx[i] - x[i] * 0.006 * alpha) * 0.6;
        vy[i] = (vy[i] - y[i] * 0.006 * alpha) * 0.6;
        const sp = Math.hypot(vx[i], vy[i]);
        if (sp > 40) { vx[i] *= 40 / sp; vy[i] *= 40 / sp; }
        x[i] += vx[i]; y[i] += vy[i];
      }
      alpha = Math.max(0.03, alpha * 0.985);
    }
    function frame() {
      if (token !== graphToken) return; // a newer refresh superseded this layout
      for (let s = 0; s < perFrame && it < ITER; s++, it++) tick();
      if (it < ITER) {
        setGraphMsg(`Laying out ${plural(n, 'node')}… ${Math.round((it / ITER) * 100)}%`);
        requestAnimationFrame(frame);
        return;
      }
      for (let i = 0; i < n; i++) { nodes[i].x = x[i]; nodes[i].y = y[i]; }
      done();
    }
    requestAnimationFrame(frame);
  }

  function drawGraph() {
    const g = state.g;
    const svg = $('graph-svg');
    const view = svgEl('g');
    const edgeLayer = svgEl('g'), nodeLayer = svgEl('g');
    for (const e of g.edges) {
      const a = g.nodes[e.s], b = g.nodes[e.t];
      e.elm = svgEl('line', { class: 'edge l-' + e.cls, x1: a.x.toFixed(1), y1: a.y.toFixed(1), x2: b.x.toFixed(1), y2: b.y.toFixed(1) });
      edgeLayer.append(e.elm);
    }
    const labelled = new Set(g.nodes.slice().sort((p, q) => q.deg - p.deg).slice(0, 24).map((n) => n.i));
    for (const n of g.nodes) {
      const r = Math.min(16, 4 + Math.sqrt(n.deg) * 1.8);
      const grp = svgEl('g', { class: 'node t-' + n.type + (labelled.has(n.i) ? ' lbl' : ''), transform: `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})` });
      const title = svgEl('title');
      title.textContent = `${TYPE_LABEL[n.type] || n.type}: ${n.name}`;
      const text = svgEl('text', { x: (r + 3).toFixed(1), y: 4 });
      text.textContent = truncate(n.name, 42);
      grp.append(title, svgEl('circle', { r: r.toFixed(1) }), text);
      grp.addEventListener('click', (ev) => { ev.stopPropagation(); if (!g.dragMoved) selectNode(n.i, false); });
      n.elm = grp;
      nodeLayer.append(grp);
    }
    view.append(edgeLayer, nodeLayer);
    svg.replaceChildren(view);
    g.view = view;
    applyVisibility();
    applyTransform();
  }

  function applyTransform() {
    const g = state.g;
    if (!g || !g.view) return;
    g.view.setAttribute('transform', `translate(${g.tx.toFixed(1)},${g.ty.toFixed(1)}) scale(${g.k.toFixed(4)})`);
    $('graph-svg').classList.toggle('zoomed', g.k >= 1.8);
  }
  function resizeStage() {
    const r = $('graph-svg').getBoundingClientRect();
    if (state.g) { state.g.w = r.width; state.g.h = r.height; }
  }
  function fitView() {
    const g = state.g;
    if (!g || !g.view) return;
    resizeStage();
    if (!g.w || !g.h) { g.needsFit = true; return; }
    const pts = g.nodes.filter((n) => n.vis);
    if (!pts.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of pts) { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); }
    const bw = Math.max(x1 - x0, 80), bh = Math.max(y1 - y0, 80);
    g.k = Math.min(3, Math.max(0.05, 0.88 * Math.min(g.w / bw, g.h / bh)));
    g.tx = g.w / 2 - ((x0 + x1) / 2) * g.k;
    g.ty = g.h / 2 - ((y0 + y1) / 2) * g.k;
    g.needsFit = false;
    applyTransform();
  }
  function zoomAt(factor, cx, cy) {
    const g = state.g;
    if (!g || !g.view) return;
    const k = Math.min(8, Math.max(0.05, g.k * factor));
    g.tx = cx - (cx - g.tx) * (k / g.k);
    g.ty = cy - (cy - g.ty) * (k / g.k);
    g.k = k;
    applyTransform();
  }
  function centerOn(n) {
    const g = state.g;
    if (!g || !g.view || typeof n.x !== 'number') return;
    resizeStage();
    if (!g.w || !g.h) return;
    g.tx = g.w / 2 - n.x * g.k;
    g.ty = g.h / 2 - n.y * g.k;
    applyTransform();
  }

  function renderTypeFilters() {
    const g = state.g;
    const counts = {};
    for (const n of g.nodes) counts[n.type] = (counts[n.type] || 0) + 1;
    const box = $('graph-types');
    box.replaceChildren(el('legend', null, 'Show'), ...TYPES.filter(([t]) => counts[t]).map(([t, label]) => {
      const cb = el('input', { type: 'checkbox' });
      cb.checked = !prefs.hidden.has(t);
      cb.addEventListener('change', () => {
        if (cb.checked) prefs.hidden.delete(t); else prefs.hidden.add(t);
        applyVisibility();
        renderNodeList();
      });
      return el('label', { class: 'chk' }, cb, el('span', { class: 'swatch sw-' + t, 'aria-hidden': 'true' }), label, el('span', { class: 'count' }, String(counts[t])));
    }));
  }
  function applyVisibility() {
    const g = state.g;
    if (!g) return;
    const q = prefs.q;
    for (const n of g.nodes) {
      n.vis = !prefs.hidden.has(n.type);
      n.match = !!q && (n.name.toLowerCase().includes(q) || (typeof n.url === 'string' && n.url.toLowerCase().includes(q)));
      if (n.elm) { n.elm.classList.toggle('hide', !n.vis); n.elm.classList.toggle('match', n.match); }
    }
    for (const e of g.edges) if (e.elm) e.elm.classList.toggle('hide', !(g.nodes[e.s].vis && g.nodes[e.t].vis));
    $('graph-svg').classList.toggle('searching', !!q);
    if (g.sel !== null && !g.nodes[g.sel].vis) selectNode(null);
  }
  function renderNodeList() {
    const g = state.g;
    if (!g) return;
    for (const n of g.nodes) n.btn = null;
    const items = g.nodes.filter((n) => n.vis && (!prefs.q || n.match))
      .sort((a, b) => b.deg - a.deg || a.name.localeCompare(b.name));
    $('graph-list-note').textContent = items.length > LIST_LIMIT
      ? `Showing the ${LIST_LIMIT} most connected of ${fmtNum(items.length)}. Search to narrow.`
      : (items.length ? plural(items.length, 'node') : 'No nodes match the current filters.');
    $('graph-list').replaceChildren(...items.slice(0, LIST_LIMIT).map((n) => {
      n.btn = el('button', { type: 'button', 'aria-pressed': g.sel === n.i ? 'true' : 'false', onclick: () => selectNode(n.i, true) },
        el('span', { class: 'swatch sw-' + n.type, 'aria-hidden': 'true' }),
        el('span', { class: 'nm' }, truncate(n.name, 70), el('span', { class: 'sr-only' }, `, ${TYPE_LABEL[n.type] || n.type}`)),
        el('span', { class: 'deg', 'aria-label': plural(n.deg, 'connection') }, String(n.deg)));
      return el('li', null, n.btn);
    }));
  }
  function selectNode(i, center) {
    const g = state.g;
    if (!g) return;
    for (const n of g.nodes) {
      if (n.elm) n.elm.classList.remove('sel', 'nb');
      if (n.btn) n.btn.setAttribute('aria-pressed', 'false');
    }
    for (const e of g.edges) if (e.elm) e.elm.classList.remove('hot');
    g.sel = (i === null || i === undefined) ? null : i;
    $('graph-svg').classList.toggle('has-sel', g.sel !== null);
    if (g.sel !== null) {
      const n = g.nodes[g.sel];
      if (n.btn) n.btn.setAttribute('aria-pressed', 'true');
      if (n.elm) { n.elm.classList.add('sel'); n.elm.parentNode.append(n.elm); }
      for (const a of g.adj.get(g.sel) || []) {
        const o = g.nodes[a.other];
        if (o.elm) o.elm.classList.add('nb');
        if (a.edge.elm) a.edge.elm.classList.add('hot');
      }
      if (center) centerOn(n);
    }
    renderDetail(g.sel === null ? null : g.nodes[g.sel]);
  }
  function renderDetail(n) {
    const box = $('graph-detail');
    const hadFocus = box.contains(document.activeElement);
    if (!n) {
      box.replaceChildren(el('h3', null, 'Selection'),
        el('p', { class: 'muted' }, 'Select a node in the canvas or the list below. Its evidence, link and connections appear here; each connection is a button, so the graph can be walked from the keyboard.'));
      return;
    }
    const g = state.g;
    const facts = el('dl', { class: 'facts' });
    for (const pair of Array.isArray(n.facts) ? n.facts : []) {
      if (Array.isArray(pair) && pair.length === 2) facts.append(el('dt', null, String(pair[0])), el('dd', null, String(pair[1])));
    }
    const conns = (g.adj.get(n.i) || []).slice().sort((a, b) => {
      const oa = g.nodes[a.other], ob = g.nodes[b.other];
      return oa.type.localeCompare(ob.type) || ob.deg - oa.deg || oa.name.localeCompare(ob.name);
    });
    const heading = el('h3', { tabindex: '-1' }, n.name);
    box.replaceChildren(...kidNodes([
      el('p', { class: 'meta-line' }, el('span', { class: 'swatch sw-' + n.type, 'aria-hidden': 'true' }), TYPE_LABEL[n.type] || n.type),
      heading,
      facts.childNodes.length ? facts : null,
      typeof n.quote === 'string' && n.quote ? el('figure', { class: 'evidence' }, el('span', { class: 'label' }, 'Verified quote'), el('blockquote', null, n.quote)) : null,
      n.url ? el('p', null, linkTo(n.url)) : null,
      el('h4', null, `Connections (${fmtNum(conns.length)})`),
      conns.length
        ? el('ul', { class: 'nb-list' }, ...conns.slice(0, 80).map((a) => {
            const o = g.nodes[a.other];
            return el('li', null, el('button', { type: 'button', onclick: () => selectNode(a.other, true) },
              el('span', { class: 'swatch sw-' + o.type, 'aria-hidden': 'true' }),
              el('span', { class: 'rel' }, a.out ? `${a.edge.label} →` : `← ${a.edge.label}`),
              el('span', { class: 'nm' }, truncate(o.name, 60), el('span', { class: 'sr-only' }, `, ${TYPE_LABEL[o.type] || o.type}`))));
          }))
        : el('p', { class: 'muted' }, 'No connections on this map.'),
      conns.length > 80 ? el('p', { class: 'summary' }, `First 80 of ${fmtNum(conns.length)} shown.`) : null]));
    if (hadFocus) heading.focus();
  }

  function initGraphInteractions() {
    const svg = $('graph-svg');
    let drag = null;
    svg.addEventListener('pointerdown', (ev) => {
      if (!state.g || !state.g.view || ev.button !== 0) return;
      drag = { x: ev.clientX, y: ev.clientY, tx: state.g.tx, ty: state.g.ty, id: ev.pointerId };
      state.g.dragMoved = false;
    });
    svg.addEventListener('pointermove', (ev) => {
      const g = state.g;
      if (!drag || !g || ev.pointerId !== drag.id) return;
      const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      if (!g.dragMoved) {
        if (Math.hypot(dx, dy) < 4) return;
        g.dragMoved = true;
        svg.setPointerCapture(ev.pointerId);
        svg.classList.add('panning');
      }
      g.tx = drag.tx + dx; g.ty = drag.ty + dy;
      applyTransform();
    });
    const end = (ev) => {
      if (!drag) return;
      drag = null;
      svg.classList.remove('panning');
      if (svg.hasPointerCapture(ev.pointerId)) svg.releasePointerCapture(ev.pointerId);
    };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);
    svg.addEventListener('click', (ev) => { if (state.g && !state.g.dragMoved && ev.target === svg) selectNode(null); });
    svg.addEventListener('wheel', (ev) => {
      if (!state.g || !state.g.view) return;
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      const dy = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
      zoomAt(Math.exp(-dy * 0.0015), ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
    svg.addEventListener('keydown', (ev) => {
      const g = state.g;
      if (!g || !g.view) return;
      resizeStage();
      const step = 60;
      switch (ev.key) {
        case 'ArrowLeft': g.tx += step; break;
        case 'ArrowRight': g.tx -= step; break;
        case 'ArrowUp': g.ty += step; break;
        case 'ArrowDown': g.ty -= step; break;
        case '+': case '=': zoomAt(1.25, g.w / 2, g.h / 2); break;
        case '-': case '_': zoomAt(0.8, g.w / 2, g.h / 2); break;
        case '0': fitView(); break;
        case 'Escape': selectNode(null); break;
        default: return;
      }
      ev.preventDefault();
      applyTransform();
    });
    $('g-zoom-in').addEventListener('click', () => { if (state.g) { resizeStage(); zoomAt(1.25, state.g.w / 2, state.g.h / 2); } });
    $('g-zoom-out').addEventListener('click', () => { if (state.g) { resizeStage(); zoomAt(0.8, state.g.w / 2, state.g.h / 2); } });
    $('g-fit').addEventListener('click', fitView);
    $('g-clear').addEventListener('click', () => selectNode(null));
    let t = null;
    $('g-q').addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => { prefs.q = $('g-q').value.trim().toLowerCase(); applyVisibility(); renderNodeList(); }, 150);
    });
    if (typeof ResizeObserver === 'function') {
      new ResizeObserver(() => { resizeStage(); if (state.g && state.g.needsFit) fitView(); }).observe($('graph-stage'));
    }
  }

  /* ---------- crawl status ---------- */
  const sumValues = (o) => (o ? Object.values(o).reduce((a, b) => a + (Number(b) || 0), 0) : 0);
  const entriesDesc = (o) => Object.entries(o || {}).sort((a, b) => b[1] - a[1]);
  function tile(label, value, note) {
    return el('div', { class: 'tile' }, el('div', { class: 'k' }, label),
      el('div', { class: 'v' }, typeof value === 'string' ? value : fmtNum(value)),
      note ? el('div', { class: 'n' }, note) : null);
  }
  function bar(pct) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    const fill = el('span');
    fill.style.width = p.toFixed(1) + '%';
    return el('div', { class: 'bar', 'aria-hidden': 'true' }, fill);
  }
  function dataTable(caption, cols, rows, emptyText) {
    const head = el('thead', null, el('tr', null, ...cols.map((c) => el('th', { scope: 'col', class: c.num ? 'num' : null }, c.label))));
    if (rows === null || rows === undefined) {
      return el('div', { class: 'tbl-wrap' }, el('table', null, el('caption', null, caption)),
        el('p', { class: 'note' }, 'Not tracked: this database has no table for it.'));
    }
    const body = el('tbody', null, ...rows.map((r) => el('tr', null, ...cols.map((c) => {
      const v = c.render ? c.render(r) : c.num ? fmtNum(r[c.key]) : (r[c.key] ?? '—');
      return el('td', { class: c.num ? 'num' : null }, v);
    }))));
    return el('div', { class: 'tbl-wrap' }, el('table', null, el('caption', null, caption), head, body),
      rows.length ? null : el('p', { class: 'note' }, emptyText || 'Nothing recorded yet.'));
  }
  function renderStatus(m) {
    $('db-name').textContent = m.database || 'the selected database';
    const pages = m.pages_by_status || {};
    const totalPages = sumValues(pages);
    const now = Date.now() / 1000;
    const paidToday = m.paid ? m.paid.today || {} : null;
    const tiles = el('div', { class: 'tiles' },
      tile('Verified entities', m.total_entities, 'count includes private topics'),
      tile('Claims', m.total_claims, m.multi_source_claims === null || m.multi_source_claims === undefined ? null : `${fmtNum(m.multi_source_claims)} backed by 2+ sources`),
      tile('Known pages', totalPages, `${fmtNum(pages.fetched || 0)} fetched · ${fmtNum(pages.classified || 0)} classified`),
      m.walls ? tile('Walled pages', sumValues(m.walls), entriesDesc(m.walls).map(([k, v]) => `${k} ${fmtNum(v)}`).join(' · ') || 'none recorded') : null,
      m.quarantined_pages === null || m.quarantined_pages === undefined ? null : tile('Quarantined', m.quarantined_pages, 'held out of extraction'),
      m.fetch_outcomes_24h ? tile('Fetches, last 24 h', sumValues(m.fetch_outcomes_24h), `${fmtNum(m.fetch_outcomes_24h.ok || 0)} usable`) : null,
      m.wiki ? tile('Wiki pages', m.wiki.pages, `${fmtNum(m.wiki.dirty)} dirty · ${fmtNum(m.wiki.stale)} stale`) : null,
      paidToday ? tile('Paid access today', fmtUsd((paidToday.settled_micro_usd || 0) + (paidToday.outstanding_micro_usd || 0)),
        `${fmtUsd(paidToday.outstanding_micro_usd)} still reserved · IST ${m.paid.day}`) : null);

    const pageRows = m.pages_by_status ? entriesDesc(pages).map(([status, count]) => ({ status, count, pct: totalPages ? (count / totalPages) * 100 : 0 })) : null;
    const kv = (o) => (o ? entriesDesc(o).map(([k, v]) => ({ k, v })) : null);
    const paidRows = m.paid ? [
      { window: `Today (IST ${m.paid.day})`, ...(m.paid.today || {}) },
      { window: `Month ${m.paid.month}`, ...(m.paid.month_totals || {}) },
      { window: 'Unresolved, any window', ...(m.paid.unresolved || {}) },
    ] : null;

    const grid = el('div', { class: 'grid2' },
      dataTable('Crawl frontier', [
        { label: 'Status', key: 'status' },
        { label: 'Pages', key: 'count', num: true },
        { label: 'Share', num: true, render: (r) => el('span', null, r.pct.toFixed(1) + '%', bar(r.pct)) },
      ], pageRows, 'No pages recorded yet.'),
      dataTable('Model engines, all recorded days', [
        { label: 'Engine', key: 'engine' },
        { label: 'Calls', key: 'call_count', num: true },
        { label: 'Tokens', key: 'total_tokens', num: true },
        { label: `Today${m.spend_day_ist ? ' (IST)' : ''}`, key: 'today_tokens', num: true },
        { label: 'Outages', key: 'outage_count', num: true },
        { label: 'Failures', key: 'failure_count', num: true },
        { label: 'State', render: (r) => (r.paused_until && r.paused_until > now ? `paused until ${fmtTime(r.paused_until)}` : 'available') },
      ], m.engine_spend, 'No model calls recorded yet.'),
      dataTable('Top domains by yield', [
        { label: 'Domain', key: 'domain' },
        { label: 'Yield', num: true, render: (r) => (typeof r.yield_score === 'number' ? r.yield_score.toFixed(2) : '—') },
        { label: 'Fetches', key: 'fetch_count', num: true },
        { label: 'Walled', key: 'walled_count', num: true },
        { label: 'Junk', key: 'junk_count', num: true },
        { label: 'State', key: 'status' },
      ], m.top_domains, 'No domains recorded yet.'),
      dataTable('Fetch outcomes, last 24 h', [{ label: 'Outcome', key: 'k' }, { label: 'Fetches', key: 'v', num: true }], kv(m.fetch_outcomes_24h), 'No fetches in the last 24 hours.'),
      dataTable('Fetch routes, last 24 h', [{ label: 'Route', key: 'k' }, { label: 'Fetches', key: 'v', num: true }], kv(m.fetch_routes_24h), 'No fetches in the last 24 hours.'),
      dataTable('Recent waves', [
        { label: 'Wave', key: 'id', num: true },
        { label: 'Started', render: (r) => fmtTime(r.started_at) },
        { label: 'Status', key: 'status' },
        { label: 'Fetches', key: 'fetches', num: true },
        { label: 'New', key: 'new_entities', num: true },
        { label: 'Changed', key: 'changed_entities', num: true },
        { label: 'Claims', key: 'new_claims', num: true },
        { label: 'Error', render: (r) => (r.had_error ? 'recorded' : '—') },
      ], m.recent_waves, 'No waves have run yet.'),
      dataTable('Task queue', [{ label: 'Status', key: 'k' }, { label: 'Tasks', key: 'v', num: true }], kv(m.tasks_by_status), 'The task queue is empty.'),
      dataTable('Paid access ledger (micro-USD reservations)', [
        { label: 'Window', key: 'window' },
        { label: 'Reservations', key: 'reservations', num: true },
        { label: 'Settled', num: true, render: (r) => fmtUsd(r.settled_micro_usd) },
        { label: 'Outstanding', num: true, render: (r) => fmtUsd(r.outstanding_micro_usd) },
        { label: 'Solver tasks', key: 'solver_tasks', num: true },
      ], paidRows, 'No paid reservations recorded.'));

    $('status-body').replaceChildren(tiles, grid,
      el('p', { class: 'note foot' }, `Snapshot generated ${fmtTime(m.generated_at)}. Yield = (new items + 1) / (fetches + 2). Outstanding reservations count against the budget until the provider settles them.`));
  }

  /* ---------- boot ---------- */
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== '/' || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const t = ev.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    const target = !$('panel-opps').hidden ? $('f-q') : !$('panel-graph').hidden ? $('g-q') : null;
    if (target) { ev.preventDefault(); target.focus(); }
  });
  document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initOppFilters();
    initGraphInteractions();
    renderDetail(null);
    $('refresh').addEventListener('click', refreshAll);
    refreshAll();
  });
})();
"""

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}

STATIC_ROUTES: dict[str, tuple[bytes, str]] = {
    "/": (DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8"),
    "/index.html": (DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8"),
    "/app.css": (DASHBOARD_CSS.encode("utf-8"), "text/css; charset=utf-8"),
    "/app.js": (DASHBOARD_JS.encode("utf-8"), "text/javascript; charset=utf-8"),
}

API_ROUTES: dict[str, Callable[[sqlite3.Connection, Path], Any]] = {
    "/api/entities": lambda conn, _path: fetch_opportunities(conn),
    "/api/graph": lambda conn, _path: fetch_graph_data(conn),
    "/api/status": fetch_status_metrics,
}


def _allowed_hosts(host: str, port: int) -> set[str] | None:
    """Host header values accepted (DNS-rebinding guard). None disables the check for wildcard binds."""
    name = host.strip().lower().strip("[]")
    if name in WILDCARD_HOSTS:
        return None
    names = {name} | (LOOPBACK_HOSTS if name in LOOPBACK_HOSTS else set())
    allowed: set[str] = set()
    for n in names:
        literal = f"[{n}]" if ":" in n else n
        allowed |= {literal, f"{literal}:{port}"}
    return allowed


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str, port: int, db_path: Path) -> None:
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.db_path = Path(db_path)
        super().__init__((host, port), DashboardHandler)
        self.allowed_hosts = _allowed_hosts(host, self.server_address[1])


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "SnowballDashboard/1"
    sys_version = ""

    def do_GET(self) -> None:
        allowed = self.server.allowed_hosts
        if allowed is not None and (self.headers.get("Host") or "").strip().lower() not in allowed:
            self._send_bytes(421, b"Unexpected Host header.\n", "text/plain; charset=utf-8")
            return
        path = urlparse(self.path).path
        static = STATIC_ROUTES.get(path)
        if static:
            self._send_bytes(200, *static)
            return
        handler = API_ROUTES.get(path)
        if handler is None:
            if path.startswith("/api/"):
                self._send_json({"error": "not_found", "message": "Unknown API route."}, 404)
            else:
                self._send_bytes(404, b"Not found.\n", "text/plain; charset=utf-8")
            return
        try:
            conn = get_db(self.server.db_path)
        except FileNotFoundError as exc:
            self._send_json({"error": "database_missing", "message": str(exc)}, 503)
            return
        except sqlite3.Error as exc:
            self._send_json({"error": "database_unavailable", "message": f"Could not open the database read-only: {exc}"}, 503)
            return
        try:
            data = handler(conn, self.server.db_path)
        except sqlite3.Error as exc:
            self._send_json({"error": "database_error", "message": f"Database query failed: {exc}"}, 500)
            return
        finally:
            conn.close()
        self._send_json(data)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: Any, status: int = 200) -> None:
        self._send_bytes(status, json.dumps(data, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        try:
            status = int(code)
        except (TypeError, ValueError):
            return
        if status >= 400:  # successful requests stay quiet
            self.log_message('"%s" %s', self.requestline, status)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("dashboard: " + (format % args) + "\n")


def serve_dashboard(port: int = 8989, host: str = "127.0.0.1", db_path: Path = DB_PATH) -> None:
    server = DashboardServer(host, port, db_path)
    bound_port = server.server_address[1]
    name = host.strip().strip("[]")
    shown = "127.0.0.1" if name in WILDCARD_HOSTS else (f"[{name}]" if ":" in name else name)
    print(f"\nSnowball evidence console (read-only): http://{shown}:{bound_port}/")
    print(f"Database: {Path(db_path).resolve()}")
    if name.lower() not in LOOPBACK_HOSTS:
        print(
            f"WARNING: bound to {host!r}, not loopback. The dashboard has no authentication; anyone who can reach "
            "this address can read it.",
            file=sys.stderr,
        )
    print("Press Ctrl+C to stop the server.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Snowball read-only evidence console (terminal or local web dashboard)")
    ap.add_argument("--serve", action="store_true", help="Serve the web dashboard")
    ap.add_argument("--terminal", action="store_true", help="Print the ASCII dashboard (the default without --serve)")
    ap.add_argument("--host", default="127.0.0.1", help="Bind address for --serve (default 127.0.0.1; non-loopback is unauthenticated)")
    ap.add_argument("--port", type=int, default=8989, help="Port for --serve (default 8989; 0 picks a free port)")
    ap.add_argument("--db", type=Path, default=None, help=f"SQLite database, opened read-only (default {DB_PATH})")
    args = ap.parse_args(argv)
    if args.serve and args.terminal:
        ap.error("choose either --serve or --terminal")
    if not 0 <= args.port <= 65535:
        ap.error("--port must be between 0 and 65535")
    db_path = args.db or DB_PATH

    try:
        conn = get_db(db_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"error: could not open {db_path} read-only: {exc}", file=sys.stderr)
        return 2

    if args.serve:
        conn.close()
        try:
            serve_dashboard(args.port, args.host, db_path)
        except OSError as exc:
            print(f"error: could not bind {args.host}:{args.port}: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        print_terminal_dashboard(conn)
    except sqlite3.Error as exc:
        print(f"error: database query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
