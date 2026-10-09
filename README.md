# Snowball

Research that never repeats itself.

Snowball is a research agent that runs non-stop. You give it a few seed topics. It follows what it finds, layer after layer, and builds its own library. It never re-reads a page it has already read, and it never re-researches a fact or topic it already knows. The goal is one daily digest of only what is new.

The status today is a research-wiki loop plus a partial Fable orchestrator on our own models, through an OpenAI-compatible proxy. It can search with an Exa key pool, read Reddit through Arctic Shift, keep a markdown wiki, and guard against prompt injection. [docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md) is the current policy, including the 2026-10-08 access amendment. [data/gate-evidence.jsonl](data/gate-evidence.jsonl) is the gate ledger. Statuses are `implemented`, `fixture_verified`, `live_verified`, `soak_verified`, and `blocked`. Only a verified row counts, and only with a command, exit code, timestamps, and a redacted evidence path. The original Fable list is 44 gates. The unit suite on this tree is not those gates. An existing git tag is not those gates.

Checked on 2026-10-09, not recorded as gate evidence: two dry-run seeds printed the same JSON and created no database; `compile --full` on an isolated crawl database exited 0; the read-only evidence console served that database on 127.0.0.1 and rejected a foreign Host. `ask` imports cleanly and answers queries with verified wiki citation paths. `retention` imports cleanly with `time` and SQLite integrity verification. The complete unit suite (12 test modules, 225 tests) passes across access budgets, challenge lifecycles, full e2e, egress, knowledge ops, orchestrator, and prompt injection. Managed fetch and CAPTCHA stay inactive while ceilings are zero. Live environment gates, soak gates, and residential provider canaries require remote deployment activation.

## Layout

| Path | What it is |
|---|---|
| `loop.sh` | Non-stop runner. One topic per iteration, then a pause. `touch STOP` pauses it. |
| `run.sh` | One research run: pick a topic, collect, write, compile the wiki, commit. |
| `tools/research.py` | Collect sources (Exa, Arctic Shift, GitHub, HN), plan, write, verify citations. |
| `tools/queue.py` | Topic queue (`topics.yml` today, SQLite next). |
| `tools/orchestrator.py` | Multi-model wave runner on `data/snowball.db`: Gemini extracts, Grok 4.7 xhigh rejects, local models only for private topics. `--status`, `--dry-run`, `--mode`. Settings in `config.env`. |
| `tools/quarantine.py` | Injection scan. Scraped pages can never add topics or instructions. |
| `tools/exa_budget.py` | Exa key pool budget and live-key floor. |
| `tools/digest.py` | Digest file compiler and Discord delivery. `reported_at` is written only in `acknowledge_digest` after confirmed delivery; failed sends leave rows unreported. |
| `prompts/research.md`, `SOUL.md` | Model instructions. |
| `systemd/` | User units: `snowball-loop.service`, `snowball-digest.{service,timer}`. |
| `docs/SYSTEM_DESIGN.md` | **The definitive spec**: invariants, access amendment, architecture, 5-layer lock engine, scraper waterfall, full SQLite schema, orchestration, safety pipeline, wiki compiler, 44 Fable gates with evidence rules, failure-mode matrix. |
| `docs/DESIGN.md` | v0 crawl decision tree and locks (history; superseded by SYSTEM_DESIGN.md). |
| `docs/PLAN.md` | Feature list (P0-P2), plus the 2026-10-08 policy and gate-status record. |
| `data/gate-evidence.jsonl` | One status row per gate. A green unit run does not fill a verified row. |
| `docs/FEATURE_MATRIX.md` | Code-read comparison of 8 open-source research agents, plus what we port from each. |

## Run

```bash
cp topics.example.yml topics.yml        # seed topics
# set DIGEST_CHANNEL_ID in config.env (empty = write digests/ only)
cp systemd/snowball-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now snowball-loop snowball-digest.timer
```

Rules it keeps:
- Code generates queries. The model has no tools and does not decide novelty.
- Every kept fact has an evidence quote from a fetched page.
- Private topics stay on local models and never use a paid external fetcher or solver.
- It reads public pages. Login walls, paywalls, robots exclusions, explicit access denials, and rate-limit bans stop escalation.
- Managed fetch, residential proxy, and CAPTCHA solving for standalone Turnstile, Turnstile challenge flows, and reCAPTCHA v2 apply only to operator-approved public origins. Shipping that code is not provider activation. Ceilings stay at zero until credentials, a current tariff, and domain policy exist.
- It does not apply, purchase, post, or email on the strength of a finding.
- SSRF checks deny private, loopback, link-local, metadata, reserved, and `100.64.0.0/10` addresses. Target pages use ports 80 and 443 only.
