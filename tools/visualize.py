#!/usr/bin/env python3
"""Snowball Visualization & Interactive Dashboard Module.
Provides:
  1. Interactive Web Dashboard (port 8989): Knowledge Graph, Opportunity Feed, Frontier Metrics.
  2. Terminal Visualizer (--terminal): ASCII Graph Trees, Yield Charts, and Opportunity Summaries.
"""

from __future__ import annotations

import argparse
import http.server
import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "snowball.db"


def get_db(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    if not p.exists():
        raise FileNotFoundError(f"Database not found at {p}. Run orchestrator.py first.")
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_graph_data(conn: sqlite3.Connection) -> dict[str, Any]:
    nodes = []
    links = []
    seen_nodes = set()

    cur = conn.cursor()
    # 1. Topics
    cur.execute("SELECT id, family, focus FROM topics")
    for r in cur.fetchall():
        nid = f"topic:{r['id']}"
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "name": str(r["id"]),
                    "type": "topic",
                    "details": r["focus"],
                }
            )

    # 2. Domains
    cur.execute(
        "SELECT domain, (CAST(new_items_total AS REAL) + 1.0) / (CAST(fetch_count AS REAL) + 2.0) AS yield_score, junk_count FROM domains"
    )
    for r in cur.fetchall():
        nid = f"domain:{r['domain']}"
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "name": r["domain"],
                    "type": "domain",
                    "yield": r["yield_score"],
                }
            )
    # 3. Entities
    cur.execute("SELECT key, name, org, kind, amount, currency, deadline, link FROM entities")
    for r in cur.fetchall():
        nid = f"entity:{r['key']}"
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            amt = f"{r['currency']} {r['amount']}" if r["amount"] else "Variable"
            nodes.append(
                {
                    "id": nid,
                    "name": r["name"],
                    "org": r["org"],
                    "type": "entity",
                    "kind": r["kind"],
                    "amount": amt,
                    "deadline": r["deadline"],
                    "link": r["link"],
                }
            )

    # 4. Hubs
    cur.execute("SELECT canon_url, domain, title FROM pages WHERE kind = 'hub' LIMIT 50")
    for r in cur.fetchall():
        nid = f"hub:{r['canon_url']}"
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "name": r["title"] or r["canon_url"],
                    "type": "hub",
                    "url": r["canon_url"],
                }
            )
        dom_id = f"domain:{r['domain']}"
        if dom_id in seen_nodes:
            links.append({"source": dom_id, "target": nid, "label": "hosts"})

    # 5. Entity sources
    cur.execute("SELECT es.entity_key, p.canon_url FROM entity_sources es JOIN pages p ON es.page_id = p.id LIMIT 100")
    for r in cur.fetchall():
        e_id = f"entity:{r['entity_key']}"
        h_id = f"hub:{r['canon_url']}"
        if e_id in seen_nodes and h_id in seen_nodes:
            links.append({"source": h_id, "target": e_id, "label": "yielded"})
    return {"nodes": nodes, "links": links}


def fetch_opportunities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("""
        SELECT e.key, e.name, e.org, e.year, e.kind, e.amount, e.currency,
               e.deadline, e.eligibility, e.link, e.first_seen_at, e.last_changed_at,
               GROUP_CONCAT(ec.field || ': ' || ec.old || ' -> ' || ec.new, '; ') AS change_log
        FROM entities e
        LEFT JOIN entity_changes ec ON e.key = ec.entity_key
        GROUP BY e.key
        ORDER BY e.deadline ASC, e.last_changed_at DESC
    """)
    return [dict(r) for r in cur.fetchall()]


def fetch_status_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    cur = conn.cursor()
    metrics: dict[str, Any] = {}

    cur.execute("SELECT status, count(*) FROM pages GROUP BY status")
    metrics["pages_by_status"] = dict(cur.fetchall())

    cur.execute("SELECT count(*) FROM entities")
    metrics["total_entities"] = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM claims")
    metrics["total_claims"] = cur.fetchone()[0]

    cur.execute(
        "SELECT domain, (CAST(new_items_total AS REAL) + 1.0) / (CAST(fetch_count AS REAL) + 2.0) AS yield_score, junk_count FROM domains ORDER BY yield_score DESC LIMIT 10"
    )
    metrics["top_domains"] = [dict(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT engine, (tokens_in + tokens_out) AS total_tokens, tokens_in, tokens_out, calls AS call_count, outages AS outage_count, failures AS failure_count FROM engine_spend"
    )
    metrics["engine_spend"] = [dict(r) for r in cur.fetchall()]

    return metrics


def print_terminal_dashboard(conn: sqlite3.Connection) -> None:
    metrics = fetch_status_metrics(conn)
    opps = fetch_opportunities(conn)

    print("=" * 78)
    print("   SNOWBALL AUTONOMOUS RESEARCH RADAR: TERMINAL DASHBOARD")
    print("=" * 78)

    pages = metrics["pages_by_status"]
    total_pages = sum(pages.values()) or 1
    print("\n[1] FRONTIER HEALTH & CRAWL STATUS")
    print("-" * 78)
    for status, count in sorted(pages.items()):
        pct = (count / total_pages) * 100
        bars = "#" * int(pct / 4)
        print(f"  {status:<14} : {count:>5} [{bars:<25}] {pct:>5.1f}%")

    print("\n[2] MULTI-MODEL ENGINE SPEND & RESILIENCE")
    print("-" * 78)
    for eng in metrics["engine_spend"]:
        name = eng["engine"]
        tokens = eng["total_tokens"]
        calls = eng["call_count"]
        outages = eng["outage_count"]
        fails = eng["failure_count"]
        print(f"  * {name:<10}: {tokens:>8,} tokens | {calls:>3} calls | {outages:>2} outages | {fails:>2} fails")

    print("\n[3] TOP HUB DOMAIN YIELDS (Laplace Smoothed)")
    print("-" * 78)
    for d in metrics["top_domains"]:
        yd = d["yield_score"] or 0.0
        bar = "=" * int(min(30, max(0, yd * 30)))
        print(f"  {d['domain']:<28} : {yd:>5.2f} [{bar:<30}]")

    print("\n[4] DISCOVERED OPPORTUNITIES & DEADLINE RADAR")
    print("-" * 78)
    if not opps:
        print("  (No opportunities parsed yet. Run topic crawl to populate.)")
    else:
        for idx, o in enumerate(opps[:10], 1):
            name = o["name"]
            org = o["org"]
            amt = o["amount"] or "N/A"
            dl = o["deadline"] or "No deadline"
            print(f"  {idx:2d}. {name} ({org})")
            print(f"      Amount: {amt:<18} | Deadline: {dl:<12} | Kind: {o['kind']}")
            if o.get("change_log"):
                print(f"      Changes: {o['change_log']}")
            print(f"      Link: {o['link']}")
            print()

    print("=" * 78)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <title>Snowball Research Radar</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
    <style>
        #cy { width: 100%; height: 580px; background-color: #0b0f19; border-radius: 0.75rem; }
    </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen flex flex-col font-sans">
    <header class="border-b border-slate-800 bg-slate-800/80 px-6 py-4 flex items-center justify-between">
        <div class="flex items-center space-x-3">
            <span class="text-2xl">❄</span>
            <div>
                <h1 class="text-lg font-bold text-white">Snowball Research Radar</h1>
                <p class="text-xs text-slate-400">Autonomous 24/7 Deep Research & Knowledge Graph</p>
            </div>
        </div>
        <div class="flex items-center space-x-4">
            <nav class="flex space-x-2 bg-slate-950 p-1 rounded-lg border border-slate-800">
                <button onclick="switchTab('radar')" id="tab-btn-radar" class="px-3 py-1.5 text-xs font-medium rounded-md bg-indigo-600 text-white">Opportunity Feed</button>
                <button onclick="switchTab('graph')" id="tab-btn-graph" class="px-3 py-1.5 text-xs font-medium rounded-md text-slate-400 hover:text-white">Knowledge Graph</button>
                <button onclick="switchTab('metrics')" id="tab-btn-metrics" class="px-3 py-1.5 text-xs font-medium rounded-md text-slate-400 hover:text-white">Frontier Health</button>
            </nav>
            <button onclick="refreshData()" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-xs text-slate-200 border border-slate-700 rounded-md transition">
                Refresh
            </button>
        </div>
    </header>

    <main class="flex-1 p-6 max-w-7xl mx-auto w-full space-y-6">
        <section id="tab-radar" class="space-y-4">
            <div class="flex justify-between items-center">
                <div>
                    <h2 class="text-base font-semibold text-white">Discovered Opportunities</h2>
                    <p class="text-xs text-slate-400">Grants, scholarships, credits, and hackathons</p>
                </div>
                <input type="text" id="search-input" onkeyup="filterCards()" placeholder="Search opportunities..."
                       class="bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none w-64">
            </div>
            <div id="opportunities-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"></div>
        </section>

        <section id="tab-graph" class="hidden space-y-4">
            <div>
                <h2 class="text-base font-semibold text-white">Knowledge Graph</h2>
                <p class="text-xs text-slate-400">Interactive force-directed graph of Topics, Hubs, and Entities</p>
            </div>
            <div id="cy" class="border border-slate-800 shadow-xl"></div>
        </section>

        <section id="tab-metrics" class="hidden space-y-6">
            <div>
                <h2 class="text-base font-semibold text-white">Frontier Health & Token Spend</h2>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4" id="stats-cards"></div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div class="bg-slate-800 border border-slate-700 rounded-xl p-5">
                    <h3 class="text-sm font-semibold text-white mb-3">Engine Spend</h3>
                    <div id="engine-spend-table" class="space-y-3"></div>
                </div>
                <div class="bg-slate-800 border border-slate-700 rounded-xl p-5">
                    <h3 class="text-sm font-semibold text-white mb-3">Domain Yield</h3>
                    <div id="domain-yield-table" class="space-y-3"></div>
                </div>
            </div>
        </section>
    </main>

    <script>
        let cyInstance = null;
        let allOpps = [];

        function switchTab(tab) {
            ['radar', 'graph', 'metrics'].forEach(t => {
                document.getElementById('tab-' + t).classList.toggle('hidden', t !== tab);
                const btn = document.getElementById('tab-btn-' + t);
                btn.className = (t === tab) ? 'px-3 py-1.5 text-xs font-medium rounded-md bg-indigo-600 text-white' : 'px-3 py-1.5 text-xs font-medium rounded-md text-slate-400 hover:text-white';
            });
            if (tab === 'graph' && cyInstance) {
                setTimeout(() => cyInstance.resize(), 100);
            }
        }

        async function refreshData() {
            const [oppsRes, graphRes, metricsRes] = await Promise.all([
                fetch('/api/entities'),
                fetch('/api/graph'),
                fetch('/api/status')
            ]);
            allOpps = await oppsRes.json();
            renderOpportunities(allOpps);
            renderGraph(await graphRes.json());
            renderMetrics(await metricsRes.json());
        }

        function renderOpportunities(opps) {
            const container = document.getElementById('opportunities-container');
            if (!opps || opps.length === 0) {
                container.innerHTML = '<div class="col-span-full py-12 text-center text-slate-500 text-xs">No opportunities recorded yet.</div>';
                return;
            }
            container.innerHTML = opps.map(o => `
                <div class="bg-slate-800 border border-slate-700 rounded-xl p-5 flex flex-col justify-between space-y-3 shadow-lg">
                    <div>
                        <div class="flex justify-between text-xs mb-1">
                            <span class="text-indigo-400 font-semibold">${o.kind || 'Opportunity'}</span>
                            <span class="text-slate-400">${o.org || ''}</span>
                        </div>
                        <h3 class="text-sm font-bold text-white">${o.name}</h3>
                        <p class="text-xs text-slate-300 mt-2">${o.eligibility || 'Eligibility unstated'}</p>
                    </div>
                    <div class="pt-3 border-t border-slate-700 flex justify-between text-xs">
                        <span class="text-emerald-400 font-bold">${o.amount || 'Variable'}</span>
                        <span class="text-slate-300">${o.deadline || 'Rolling'}</span>
                    </div>
                    ${o.link ? `<a href="${o.link}" target="_blank" class="block text-center py-2 bg-slate-900 hover:bg-slate-700 text-white rounded-lg text-xs font-medium border border-slate-700 transition">View Source ↗</a>` : ''}
                </div>
            `).join('');
        }

        function filterCards() {
            const q = document.getElementById('search-input').value.toLowerCase();
            renderOpportunities(allOpps.filter(o =>
                (o.name && o.name.toLowerCase().includes(q)) ||
                (o.org && o.org.toLowerCase().includes(q))
            ));
        }

        function renderGraph(data) {
            const elements = [];
            data.nodes.forEach(n => {
                let color = '#6366f1';
                if (n.type === 'entity') color = '#10b981';
                if (n.type === 'hub') color = '#f59e0b';
                if (n.type === 'domain') color = '#0284c7';
                elements.push({ data: { id: n.id, label: n.name, color: color } });
            });
            data.links.forEach((l, i) => {
                elements.push({ data: { id: 'e-' + i, source: l.source, target: l.target } });
            });

            cyInstance = cytoscape({
                container: document.getElementById('cy'),
                elements: elements,
                style: [
                    {
                        selector: 'node',
                        style: {
                            'background-color': 'data(color)',
                            'label': 'data(label)',
                            'color': '#cbd5e1',
                            'font-size': '10px',
                            'width': 18,
                            'height': 18
                        }
                    },
                    {
                        selector: 'edge',
                        style: { 'width': 1, 'line-color': '#334155', 'curve-style': 'bezier' }
                    }
                ],
                layout: { name: 'cose', animate: false, padding: 30 }
            });
        }

        function renderMetrics(m) {
            const stats = document.getElementById('stats-cards');
            stats.innerHTML = `
                <div class="bg-slate-800 border border-slate-700 rounded-xl p-4">
                    <span class="text-xs text-slate-400">Verified Entities</span>
                    <div class="text-2xl font-bold text-emerald-400 mt-1">${m.total_entities || 0}</div>
                </div>
                <div class="bg-slate-800 border border-slate-700 rounded-xl p-4">
                    <span class="text-xs text-slate-400">Pages Fetched</span>
                    <div class="text-2xl font-bold text-sky-400 mt-1">${(m.pages_by_status && m.pages_by_status.fetched) || 0}</div>
                </div>
                <div class="bg-slate-800 border border-slate-700 rounded-xl p-4">
                    <span class="text-xs text-slate-400">Claims Verified</span>
                    <div class="text-2xl font-bold text-indigo-400 mt-1">${m.total_claims || 0}</div>
                </div>
            `;

            const spendTable = document.getElementById('engine-spend-table');
            if (m.engine_spend && m.engine_spend.length > 0) {
                spendTable.innerHTML = m.engine_spend.map(e => `
                    <div class="flex justify-between text-xs py-2 border-b border-slate-700">
                        <span class="text-slate-200">${e.engine}</span>
                        <span class="text-slate-100 font-semibold">${(e.total_tokens || 0).toLocaleString()} toks</span>
                    </div>
                `).join('');
            } else {
                spendTable.innerHTML = '<p class="text-xs text-slate-500">No spend recorded today.</p>';
            }

            const yieldTable = document.getElementById('domain-yield-table');
            if (m.top_domains && m.top_domains.length > 0) {
                yieldTable.innerHTML = m.top_domains.map(d => `
                    <div class="flex justify-between text-xs py-2 border-b border-slate-700">
                        <span class="text-slate-300">${d.domain}</span>
                        <span class="font-mono font-bold text-emerald-400">${(d.yield_score || 0).toFixed(2)}</span>
                    </div>
                `).join('');
            } else {
                yieldTable.innerHTML = '<p class="text-xs text-slate-500">No domain yield yet.</p>';
            }
        }

        window.addEventListener('DOMContentLoaded', refreshData);
    </script>
</body>
</html>
"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
            return

        if parsed.path == "/api/graph":
            conn = get_db()
            data = fetch_graph_data(conn)
            conn.close()
            self._send_json(data)
            return

        if parsed.path == "/api/entities":
            conn = get_db()
            data = fetch_opportunities(conn)
            conn.close()
            self._send_json(data)
            return

        if parsed.path == "/api/status":
            conn = get_db()
            data = fetch_status_metrics(conn)
            conn.close()
            self._send_json(data)
            return

        self.send_response(404)
        self.end_headers()

    def _send_json(self, data: Any) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        pass


def serve_dashboard(port: int = 8989) -> None:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"\nSnowball Interactive Dashboard live at: http://localhost:{port}")
    print("Press Ctrl+C to stop the server.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Visualization & Interactive Dashboard")
    ap.add_argument("--serve", action="store_true", help="Launch web dashboard server on port 8989")
    ap.add_argument("--port", type=int, default=8989, help="Server port (default: 8989)")
    ap.add_argument("--terminal", action="store_true", help="Render ASCII terminal visualization")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="Path to snowball.db")
    args = ap.parse_args()

    conn = get_db(args.db)
    if args.serve:
        conn.close()
        serve_dashboard(args.port)
    else:
        print_terminal_dashboard(conn)
        conn.close()


if __name__ == "__main__":
    main()
