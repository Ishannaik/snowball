#!/usr/bin/env python3
"""Snowball terminal evidence snapshot and public-safe read models.

  --terminal (default)  ASCII summary: frontier health, engine spend, domain yield, opportunity radar.

The database is opened read-only and is never created here. Private topics, unknown provenance and goal
records stay out of these projections. The HTTP console lives in tools/console.py.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"
IST = timezone(timedelta(hours=5, minutes=30))

# Caps keep the in-browser force layout responsive; anything cut is reported in the payload's "truncated".
GRAPH_LIMITS = {"topics": 200, "entities": 300, "sources": 300, "hubs": 80}
YIELD_SQL = "(CAST(new_items_total AS REAL) + 1.0) / (CAST(fetch_count AS REAL) + 2.0)"
_TTY_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def get_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the selected database read-only. Never creates a file."""
    p = Path(path or DB_PATH)
    if not p.is_file():
        raise FileNotFoundError(f"Database not found at {p}. Run the orchestrator first; the dashboard never creates it.")
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _public_pages(conn: sqlite3.Connection, alias: str) -> str:
    """SQL predicate that drops pages belonging to a private topic (column added by an orchestrator migration)."""
    if "topics" not in _tables(conn) or "private" not in _columns(conn, "topics"):
        return "1 = 1"
    return f"({alias}.topic_id IS NULL OR {alias}.topic_id NOT IN (SELECT id FROM topics WHERE private = 1))"


def _public_provenance(conn: sqlite3.Connection, table: str, alias: str) -> str:
    """Keep only explicit public provenance. Unknown, private and missing values stay out."""
    if table not in _tables(conn) or "provenance" not in _columns(conn, table):
        return "1 = 1"
    return f"{alias}.provenance IN ('legacy_v3','public','observed','public_fetch')"


def _public_claims(conn: sqlite3.Connection) -> str:
    parts = [_public_provenance(conn, "claims", "c")]
    if "topic_id" in _columns(conn, "claims") and "topics" in _tables(conn) and "private" in _columns(conn, "topics"):
        parts.append("(c.topic_id IS NULL OR c.topic_id NOT IN (SELECT id FROM topics WHERE private = 1))")
    return " AND ".join(parts)


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
        WHERE {_public_pages(conn, "p")} AND {_public_provenance(conn, "entities", "e")}
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

    entity_scope = f"{pub} AND {_public_provenance(conn, 'entities', 'e')}"
    total = conn.execute(
        f"SELECT COUNT(*) FROM entities e LEFT JOIN pages p ON p.id = e.evidence_page_id WHERE {entity_scope}"
    ).fetchone()[0]
    entities = conn.execute(
        f"""SELECT e.key, e.name, e.org, e.kind, e.family, e.status, e.amount, e.currency, e.deadline, e.link,
                   e.relevance, e.evidence_quote, e.evidence_page_id
            FROM entities e LEFT JOIN pages p ON p.id = e.evidence_page_id
            WHERE {entity_scope} ORDER BY e.relevance DESC, e.last_seen_at DESC LIMIT ?""",
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

    pub = _public_pages(conn, "p") if "pages" in tables else "1 = 0"
    m["pages_by_status"] = (
        _count_by(conn, f"SELECT p.status, COUNT(*) FROM pages p WHERE {pub} GROUP BY p.status") if "pages" in tables else None
    )
    m["walls"] = (
        _count_by(conn, f"SELECT p.wall_kind, COUNT(*) FROM pages p WHERE p.wall_kind IS NOT NULL AND {pub} GROUP BY p.wall_kind")
        if "pages" in tables and "wall_kind" in page_cols else None
    )
    m["quarantined_pages"] = (
        conn.execute(f"SELECT COUNT(*) FROM pages p WHERE p.quarantined = 1 AND {pub}").fetchone()[0]
        if "pages" in tables and "quarantined" in page_cols else None
    )
    if "entities" in tables and "pages" in tables:
        m["total_entities"] = conn.execute(
            f"SELECT COUNT(*) FROM entities e LEFT JOIN pages p ON p.id = e.evidence_page_id "
            f"WHERE {pub} AND {_public_provenance(conn, 'entities', 'e')}"
        ).fetchone()[0]
    elif "entities" in tables:
        m["total_entities"] = conn.execute(
            f"SELECT COUNT(*) FROM entities e WHERE {_public_provenance(conn, 'entities', 'e')}"
        ).fetchone()[0]
    else:
        m["total_entities"] = 0
    claim_scope = _public_claims(conn) if "claims" in tables else "1 = 0"
    m["total_claims"] = (
        conn.execute(f"SELECT COUNT(*) FROM claims c WHERE {claim_scope}").fetchone()[0] if "claims" in tables else 0
    )
    m["multi_source_claims"] = (
        conn.execute(f"SELECT COUNT(*) FROM claims c WHERE c.n_sources >= 2 AND {claim_scope}").fetchone()[0]
        if "claims" in tables and "n_sources" in _columns(conn, "claims") else None
    )

    m["top_domains"] = (
        [
            dict(r)
            for r in conn.execute(
                f"SELECT domain, {YIELD_SQL} AS yield_score, junk_count, walled_count, fetch_count, status "
                f"FROM domains WHERE domain IN (SELECT p.domain FROM pages p WHERE {pub}) "
                f"ORDER BY yield_score DESC LIMIT 10"
            )
        ]
        if "domains" in tables and "pages" in tables else None
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

    if "fetches" in tables and "pages" in tables:
        since = int(time.time()) - 86400
        m["fetch_outcomes_24h"] = _count_by(
            conn,
            f"SELECT f.outcome, COUNT(*) FROM fetches f JOIN pages p ON p.id = f.page_id WHERE f.at >= ? AND {pub} GROUP BY f.outcome",
            (since,),
        )
        m["fetch_routes_24h"] = _count_by(
            conn,
            f"SELECT f.route, COUNT(*) FROM fetches f JOIN pages p ON p.id = f.page_id WHERE f.at >= ? AND {pub} GROUP BY f.route",
            (since,),
        )
    else:
        m["fetch_outcomes_24h"] = m["fetch_routes_24h"] = None

    if "tasks" in tables:
        task_goal = " WHERE goal_id IS NULL" if "goal_id" in _columns(conn, "tasks") else ""
        m["tasks_by_status"] = _count_by(conn, f"SELECT status, COUNT(*) FROM tasks{task_goal} GROUP BY status")
    else:
        m["tasks_by_status"] = None
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Snowball terminal evidence snapshot")
    ap.add_argument("--serve", action="store_true", help="Removed; the console server lives in tools/console.py")
    ap.add_argument("--terminal", action="store_true", help="Print the ASCII dashboard (the default)")
    ap.add_argument("--host", default="127.0.0.1", help="Ignored. The console server owns the bind address")
    ap.add_argument("--port", type=int, default=8989, help="Ignored. The console server owns the port")
    ap.add_argument("--db", type=Path, default=None, help=f"SQLite database, opened read-only (default {DB_PATH})")
    args = ap.parse_args(argv)
    if args.serve:
        print("error: the web console is served by tools/console.py, not this terminal snapshot.", file=sys.stderr)
        return 2
    db_path = args.db or DB_PATH

    try:
        conn = get_db(db_path)
    except FileNotFoundError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f'error: could not open {db_path} read-only: {exc}', file=sys.stderr)
        return 2

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
