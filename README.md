# Snowball

Snowball collects public sources for configured research topics, extracts entities and claims with verbatim evidence, and exposes the retained evidence in a read-only dashboard. SQLite stores the frontier and facts; a compiled Markdown wiki supports local answers and digests. Deduplication avoids redundant work within this database; it does not automatically import or reconcile older Hermes research.

```
                           +------------------------+
                           |       topics.yml       |
                           +------------------------+
                                       |
                                       v
+------------------+       +------------------------+       +--------------------+
| Local Knowledge  | <---> |  snowball orchestrator | <---> | Web & API Fetchers |
| (SQLite + Wiki)  |       |   (auto / local / LLM) |       | (Exa, Direct HTTP) |
+------------------+       +------------------------+       +--------------------+
         |                             |
         v                             v
+------------------+       +------------------------+
|  snowball serve  |       |    snowball digest     |
| (Evidence Web UI)|       | (Discord / Markdown)   |
+------------------+       +------------------------+
```

---

## Core Invariants

- **Code owns queries and novelty**: The LLM has no tools and does not decide what to search. Queries are generated deterministically from seed definitions and pending entity names.
- **Verbatim evidence required**: Every retained fact and entity change requires an exact quotation from an acquired page.
- **Strict privacy boundaries**: Any topic flagged `private: true` runs exclusively against local models (`OPENAI_BASE_URL`) and never touches external APIs (Gemini, Grok, Exa, or paid fetchers).
- **Public-origin compliance**: The engine reads public pages only. Login walls, paywalls, `robots.txt` exclusions, explicit access denials, and HTTP 429 rate limits stop crawl escalation immediately without evasion.
- **SSRF and network isolation**: Loopback, link-local, cloud metadata, reserved, and `100.64.0.0/10` address ranges are blocked via pre-request IP resolution and direct socket pinning. Outbound traffic is restricted to ports 80 and 443.
- **Quarantine against prompt injection**: Scraped pages are treated as untrusted text. Flagged injection attempts are written to `QUARANTINE.md` and prohibited from contributing new topics or instructions.

---

## Multi-Model Architecture

Snowball orchestrates multi-model workflows defined in `config.env`:

| Mode | Extraction Engine | Verification Engine | Use Case |
|---|---|---|---|
| `auto` (default) | Gemini when available; otherwise configured OpenAI-compatible endpoint | Grok when available | Public research with local fallback. |
| `gemini-bulk` | Gemini | Grok when available | Public extraction; missing Gemini access is an error rather than a fallback. |
| `grok-xhigh` | Grok through `omp --no-tools` | Grok when available | Public research through the configured Grok provider. |
| `local` | Configured OpenAI-compatible endpoint | Same adapter | Local deployment only if that endpoint actually runs a local model. |
 
`config.env` contains model-name defaults, not a verified provider catalog. Select IDs supported by your deployment. Gemini supports `GEMINI_TRANSPORT=api`, `omp`, or `ssh-omp`; API transport requires `GEMINI_API_KEY`. Private topics are routed to `OPENAI_BASE_URL`, so that endpoint **must be local and trusted** before adding any private data. A cloud proxy on loopback does not make the model private.

---

## Installation & Setup

### Prerequisites

- Python `>= 3.10`
- Optional: Node.js (for legacy wiki utilities) and Playwright (for optional browser acquisition)

### Dependencies

```bash
# Clone and create an isolated Python environment
git clone https://github.com/Ishannaik/snowball.git
cd snowball
python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install pyyaml curl_cffi

# Optional: browser runtime for dynamic pages
pip install "playwright>=1.49"
playwright install chromium
```

### Configuration

Research configuration precedence is `config.env`, then `.env`, then `SNOWBALL_ENV_FILE`, then the process environment. Keep secrets outside Git. Delivery commands also need their credentials in the process environment (or a supported token file); do not assume every command loads the research `.env`.

```bash
# Topic definitions:
# - topics.example.yml: General startup grants, fellowships, and tech alpha
# - topics.highered.yml: Funding-first on-campus 2027 CS/AI master's & direct PhDs
cp topics.example.yml topics.yml
# Optional: configure secrets in .env
cat << 'EOF' > .env
GEMINI_API_KEY=your_gemini_key
DISCORD_WEBHOOK_URL=your_webhook_url
DIGEST_CHANNEL_ID=your_channel_id
EOF
```

Paid fetch routes and CAPTCHA solvers default to disabled with zero ceilings (`MAX_PAID_FETCHES_PER_DAY=0`, `MAX_CAPTCHA_SOLVES_PER_DAY=0`). Shipping solver or proxy code does not activate spending; ceilings remain zero until credentials, current tariffs, and origin policy in `access.yml` are explicitly configured.

---

## CLI Reference

The unified CLI entry point is `snowball.py`, accessible via `./snowball` (Linux/macOS), `snowball.bat` (Windows), or `python snowball.py`:

### Research & Supervision

```bash
# Deterministically seed topics from topics.yml into the SQLite database
python snowball.py seed [--topics topics.yml] [--dry-run] [--db data/snowball.db]
# Or seed higher-ed funding-first topics:
# python snowball.py seed --topics topics.highered.yml

# Run a single batch of research topics
python snowball.py run [--topic <slug>] [--topics topics.yml] [--max-topics 2] [--mode auto] [--dry-run]

# Run one topic wave and report the supervisor outcome
python snowball.py run-once [--topic <slug>] [--dry-run]

# Start the continuous supervisor loop (watches for STOP file to pause cleanly)
python snowball.py supervise [--max-topics 1]

# Inspect frontier health, spend counters, and domain yield metrics
python snowball.py status [--json]
```

### Knowledge Base & Answering

```bash
# Answer questions from compiled Markdown wiki and SQLite facts with verified file citations
python snowball.py ask "Which fellowship grants close this quarter?" [--wiki wiki] [--limit 12]

# Synthesize answer deterministically without LLM calls
python snowball.py ask "What programs support early stage founders?" --no-model

# Incrementally compile authoritative Markdown wiki pages in wiki/ from database entities
python snowball.py compile [--full] [--out wiki]

# Rebuild SQLite FTS5 search indexes from base tables and Markdown wiki
python snowball.py reindex [--wiki wiki]
```

### Evidence Dashboard (Frontend)

Snowball includes an embedded read-only evidence console:

```bash
# Launch read-only HTTP dashboard (default: http://127.0.0.1:8989)
python snowball.py serve [--host 127.0.0.1] [--port 8989] [--db data/snowball.db]

# Print terminal ASCII summary of frontier health and domain yields
python snowball.py visualize --terminal
```

### Delivery & Feedback

```bash
# Compile daily or weekly digest (writes to digests/YYYY-MM-DD.md)
python snowball.py digest [--day YYYY-MM-DD] [--weekly]

# Post digest to Discord (reported_at is updated only upon confirmed Discord delivery receipt)
python snowball.py digest --post [--channel <id>]

# Check and dispatch urgent opportunity/deadline notifications
python snowball.py alerts [--post] [--channel <id>]

# Record user feedback reaction on a delivered digest item
python snowball.py feedback --item-id <id> --reaction up
```

### Maintenance, Retention & Backups

```bash
# Prune expired raw HTML and logs, enforce 5 GB free disk floor, and produce atomic backup
python snowball.py retention [--keep 7] [--root .] [--db data/snowball.db]

# Restore database from a consistent snapshot
python snowball.py retention --restore data/backups/backup-2026-10-08.db --db data/snowball.db
```

### Diagnostics & Evaluation

```bash
# Run subsystem readiness diagnostics (topics, extraction, digest ack, retention, SSRF guard)
python snowball.py diagnostics [--live] [--json]

# Score production classifier and extraction against benchmark fixtures
python snowball.py eval classify tests/pages_labelled_50.jsonl
python snowball.py eval extract tests/items_20.jsonl
python snowball.py eval topics tests/topic_pairs_30.jsonl
python snowball.py eval digest --since 7d
```

---

## Data Behavior & Storage Isolation

- **Default storage**: Runtime state is stored at `data/snowball.db` and `wiki/`; these paths are ignored by Git.
- **Test isolation**: Offline test fixtures use temporary databases rather than populate the live dashboard. The synthetic Mozilla and hackathon records are not research findings.
- **Empty dashboard on a fresh install**: Run `seed` to create topics, then an actual research wave to acquire evidence. `serve` reads the selected database and does not seed or fetch anything.
- **Inspecting specific runs**: Pass an explicit database to inspect a retained test run, historical crawl, or backup:
  ```bash
  python snowball.py serve --db /path/to/test-run.db
  ```

---

## Repository Layout

| Path | Purpose |
|---|---|
| `snowball.py` | Unified CLI entry point (`./snowball`, `snowball.bat`). |
| `config.env` | System defaults, model routing, token budgets, and scraper ceilings. |
| `topics.example.yml` | Topic definition schema, recheck intervals, and seed definitions. |
| `topics.highered.yml` | Tracked research config: funding-first on-campus 2027 CS/AI master's & direct PhDs. |
| `tools/orchestrator.py` | Multi-model wave runner, frontier queue, model adapter, and supervisor loop. |
| `tools/visualize.py` | Read-only evidence web dashboard and terminal ASCII visualizer. |
| `tools/ask.py` | Knowledge base answering CLI with verified file citation paths. |
| `tools/wiki_compiler.py` | Incremental Markdown wiki compiler with length and entity protection. |
| `tools/digest.py` | Digest preparation and Discord delivery with confirmed receipt acknowledgement. |
| `tools/alerts.py` | Opportunity and deadline alert dispatcher. |
| `tools/retention.py` | Raw HTML/log expiry, 5 GB disk floor enforcement, and SQLite backup/restore. |
| `tools/eval_gates.py` | Subsystem diagnostics, gate evidence tracking, and evaluation benchmarks. |
| `tools/quarantine.py` | Prompt injection detection, heuristic scanning, and quarantine manager. |
| `tools/egress_guard.py` | SSRF protection, loopback/private IP blocking, and DNS socket pinning. |
| `data/gate-evidence.jsonl` | Machine-readable ledger tracking gate verification statuses. |
| `docs/SYSTEM_DESIGN.md` | Authoritative architecture specification and gate requirements. |
| `systemd/` | Production systemd service and timer unit templates. |

---

## Gate Ledger & Verification Status

Snowball's formal specification ([docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md)) defines 44 Fable gates (M0–M4) plus 15 access policy gates (A01–A15). Progress is tracked in `data/gate-evidence.jsonl`:

- **Strict Evidence Standard**: Passing unit tests or git tags does not constitute gate verification. A gate is verified only when an exact verification command, exit code, execution timestamps, and an artifact evidence path are recorded.
- **Live evidence remains separate**: The deployment smoke checks do not establish multi-day soak, frontier repetition, residential egress, or benchmark gates. Inspect the runtime ledger rather than infer gate completion from the test suite; ignored runtime evidence is not shipped in a clone.
- **Subsystem Diagnostics**: Run `python snowball.py diagnostics` to evaluate local readiness across database schemas, disk retention floors, SSRF pinning, and gate ledger state.

---

## Oracle Deployment

The Oracle checkout uses branch `feat/fable-completion-access` at `/home/ubuntu/repos/snowball`. The dashboard reads `data/highered.db`, separate from the older default database. Public higher-ed topics use `config.oracle.env`: Gemini extraction and Claude reject-only verification through OMP's existing `agy-pool` provider at `http://127.0.0.1:8088/v1`. OAuth credentials stay in the proxy's account store; none are included here. The verifier retains the legacy `grok` adapter/accounting label even when its configured model is Claude.

The dashboard URL is **https://snowball.ishannaik.com/**. You must be on the permitted tailnet. Its DNS-only A record points to `100.68.165.73`; Caddy listens there on 443 and forwards to `127.0.0.1:8989`. The [site block](systemd/snowball.caddy) uses the existing Cloudflare DNS-01 certificate configuration and rewrites the upstream Host header. Do not bind the dashboard to a public/wildcard address to bypass its DNS-rebinding guard.

### Reproduce the runtime setup

Install dependencies using the setup commands above. Run these commands from the Oracle checkout as `ubuntu`; the OMP `agy-pool` provider and Exa rotator must already exist:

```bash
SNOWBALL_ENV_FILE="$PWD/config.oracle.env" .venv/bin/python snowball.py seed \
  --topics topics.highered.yml --db data/highered.db

SNOWBALL_ENV_FILE="$PWD/config.oracle.env" .venv/bin/python snowball.py run-once \
  --topics topics.highered.yml --db data/highered.db \
  --topic highered-gulf-ms-2027 --mode gemini-bulk --json

sudo install -m 644 systemd/snowball-dashboard.service /etc/systemd/system/
sudo install -m 644 systemd/snowball-loop.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/snowball-loop.service.d
sudo install -m 644 systemd/snowball-loop.service.d/oracle.conf \
  /etc/systemd/system/snowball-loop.service.d/oracle.conf
sudo systemctl daemon-reload
sudo systemctl enable --now snowball-dashboard.service snowball-loop.service
```

Add `systemd/snowball.caddy` to the existing Caddyfile, validate it with the existing certificate environment, then reload Caddy. Preserve the other sites and global settings. The Caddy Cloudflare token belongs in the existing protected server environment, not this repository.

### Operate and inspect

```bash
sudo systemctl status snowball-loop.service snowball-dashboard.service
sudo journalctl -u snowball-loop.service -n 50 --no-pager
.venv/bin/python snowball.py status --db data/highered.db --json
curl --fail https://snowball.ishannaik.com/api/status
# Pause the managed worker; leave the read-only dashboard online.
sudo systemctl stop snowball-loop.service
# Resume:
sudo systemctl start snowball-loop.service
```

The base worker unit uses `Restart=always`; a `STOP` file stops a wave but systemd will restart the process while the file remains. Use `systemctl stop` to pause the managed service without restart churn.

Daily research caps are 800,000 estimated tokens overall, 600,000 extraction and 200,000 verification. The OMP transport estimates usage from text; these are not a billing ledger or provider quota guarantee. Each wave selects one topic and up to four pages. The two-query setting limits newly generated query seeds; pending query replay can add more searches in that wave. Paid scraping and solver ceilings remain zero; no Discord delivery channel is enabled by this deployment.

### Verification

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Offline tests do not exercise live model credentials or populate the production database. A meaningful deployment check requires a completed acquisition/extraction wave, persisted evidence, and a browser check of the actual dashboard. `--topics` applies to seed, batch, one-wave, and supervisor commands; the regression suite covers selection from an explicit topic file.
