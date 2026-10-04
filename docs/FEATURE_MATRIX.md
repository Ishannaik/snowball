# Deep research Ralph loop: feature matrix and fork decision

Checked 2026-10-04 11:10-11:30 IST. Shallow clones; code read with grep/sed by 4 subagents plus me.
"README only" = claim found only in docs. Paths are relative to each repo root.
Raw per-repo notes with more file:function evidence: `notes_*.md` next to this file.

## 1. Matrix

| | research-wiki (ours) | hyperresearch | local-deep-research (LDR) | gpt-researcher | deer-flow | research-queue | nashsu/llm_wiki | lucasastorian/llmwiki | last30days-skill |
|---|---|---|---|---|---|---|---|---|---|
| Licence / lang | none (private) / Bash+Py stdlib + npm llm-wiki-compiler | MIT / Py | MIT / Py | Apache-2.0 / Py | MIT / Py+TS | MIT / Py (1 file, 724 lines) | NOASSERTION / TS+Rust (Tauri) | Apache-2.0 / Py+TS | MIT / Py stdlib |
| Last push / maintainers | 2026-10-04 / our bot | 2026-10-01 / 1 main | 2026-10-04 / 1 dominant, very active | 2026-10-01 / 2 core | 2026-10-04 / ByteDance team | 2026-09-05 / solo, 2 commits | 2026-09-28 / 1 (764 of ~830 commits) | 2026-10-02 / solo | 2026-10-01 / 2 core |
| Any OpenAI-compat base_url | yes, `tools/llm.py chat()` | **no** (embeddings only, hardcoded api.openai.com) | yes, `custom_openai_endpoint.py` | yes, `OPENAI_BASE_URL` in `llm_provider/generic/base.py` | yes, config `langchain_openai:ChatOpenAI base_url` | yes, LiteLLM reviewer | yes, `custom` apiMode in `llm-providers.ts` | **no LLM at all** | yes via `openrouter` client + `OPENROUTER_BASE_URL` (`lib/providers.py`) |
| Anthropic-compat | no | no | yes, `custom_anthropic_endpoint.py` | langchain_anthropic, no base_url | langchain_anthropic | no | yes (`custom` messages mode) | n/a | no |
| Needs Claude Code / Codex | no | **yes**, pipeline is 17 skill prompts (`core/platforms.py`, `core/codex.py`) | no | no | no (optional OAuth providers) | no | no (optional CLI transports) | **yes**, needs an MCP agent (README pushes Claude Routine) | no for collection; final prose is handed to host agent (SKILL.md) |
| Exa | yes, rotator :8791 `research.py exa_search()` | yes, `web/` exa-py | yes, `engines/search_engine_exa.py` | yes, `retrievers/exa/exa.py` | yes, `community/exa/tools.py` | no (SearXNG hardcoded, 1-line change) | no | no | yes, `lib/grounding.py:91` (hardcoded api.exa.ai) |
| Other search | none | tavily, parallel, serply, crawl4ai | 30 engines incl. tavily, brave, serper, searxng | tavily (default), brave, serper, searx, ~15 more | ddg (default), tavily, brave, serper, searxng, ~10 more | via LDR | tavily, serpapi, brave, searxng | none | brave, serper, parallel, perplexity, brightdata |
| Add custom provider | edit one function, <1 h | Protocol + elif in `web/base.py:get_provider`, 1-2 h | subclass `BaseSearchEngine` + registry + settings JSON, 2-4 h | class with `search()` + entry point `gpt_researcher.retrievers`, 1-2 h | LangChain `@tool` + config path, ~1 h | n/a | Rust fn + match arm + UI, 2-3 h | n/a | per-source tuning in `pipeline.py`, 3-5 h |
| Reddit | yes, Arctic Shift (`reddit_posts`, `reddit_thread`) | URL detection only | no (only via searxng/exa) | no | no | no | no | no | yes, keyless site scrape + Arctic Shift scores (`reddit_keyless.py`, `reddit_arctic.py`) |
| X | no | no | no | paid getxapi/xquik | no | no | no | no | yes, incl. **grok CLI backend with snowflake-date validation** (`lib/grok_x.py`) |
| HN / GitHub / arXiv | yes / yes (gh issues+discussions) / no | no / no / via OpenAlex | no / yes / yes | no / no / yes | no / yes / yes (skill scripts) | via LDR | no | no / no / arXiv PDF URL only | yes (Algolia) / yes / only with external binary |
| Persistent KB | markdown wiki (llm-wiki-compiler) + raw reports | markdown notes + rebuildable SQLite FTS5 (`claims`, `sources` tables) | SQLCipher DB + FAISS vectors | **no** (JSON report store) | **no** research KB (DeerMem = user prefs) | markdown + LDR collection + Trilium | markdown vault + optional LanceDB | markdown + SQLite FTS5 | SQLite FTS5 findings store (`store.py`), opt-in |
| Source dedupe | exact URL, per run only (`collect()` seen set) | exact URL at fetch (`existing_live_note_for_url`); DOI (`scholar/dedup.py`); canonical URL + MinHash syndication (`core/independence.py`) | SHA256 of normalized URL, UNIQUE (`download_service._normalize_url`), doc hash, DOI column | exact URL, per run | no | no | SHA-256 content cache (`ingest-cache.ts`) | SHA-256 + mtime (`watcher.py`) | normalized URL (`fusion._normalize_url`) + trigram/token Jaccard 0.7 (`dedupe.py`), cross-run upsert |
| Claim dedupe | no (prompt in SOUL.md) | exact sha256 of text (`core/claims._claim_hash`) | no | no | no (memory facts: token Jaccard) | no | no; page merge by LLM with 70% length guard (`page-merge.ts`) | no | no |
| Topic / question dedupe | exact slug string (`queue.done()`) | no | no | no | no | no | page-level embedding + LLM dup groups (`dedup_embedding.ts`, `dedup.ts`) | no | exact `normalized_name` UNIQUE (`store.py discovery_topics`) |
| Check KB before searching | **no** | prompt only; code refuses re-fetch of known URL | partial: history collection exists, must pick it as engine | no (can pass `visited_urls`) | partial: memory in prompt | indirect | no (index read for linking only) | prompt only | no (store read after run for delta) |
| Follow-up topics / frontier | yes: `SUGGEST:` lines, 2/run, 5/day; `make_deepen()` re-runs "Gaps" | within one run only | within run (sub-questions); news topic tags | within run (`deep_research.py` breadth 3 depth 2) | no | no (human adds Vikunja tasks) | gaps found (`graph-insights.ts`), user must click | no | `--discover` proposes; nothing auto-runs |
| Unattended / schedule | **yes**, systemd `Restart=always`, STOP file, flock | no | APScheduler, but needs in-memory user password, dies after 48 h idle or restart | no | yes, `app/scheduler/service.py` cron + DB leases (off by default) | yes, 60 s poll daemon | folder import only, app must be open | no | `watchlist.py run-all` from your own cron |
| Citation / fact check | `verify_urls()` drops lines citing uncollected URLs; `quarantine.py` injection scan | **strong**: verbatim quote check, retraction sweep, numeric checks (`core/citecheck.py`) | LLM fact-check, off by default | curator off by default | `receipt_verification.py`: citations must match real tool calls | HEAD/GET every cited URL + cross-model review (`check_citations`, `llm_review`) | `[n]` index validation | structural lint, staleness propagation (`mcp/tools/lint.py`) | no |
| Resume after crash | topic stays queued, reruns whole | yes, run manifest + `hpr run resume` (`core/runs.py`) | queue reclaimed; running job rerun from scratch | no | yes, LangGraph checkpointer SQLite | per-task retry state in `.state.json` | ingest queue persisted | n/a | no |
| Cost / budget caps | 40 runs/day, 5 suggestions/day, Exa live-key floor | estimated spend cap blocks run (`run init --budget`) | tracking only | tracking only | `token_budget` hard cut (off by default) | attempts + 429 backoff, no $ | no | no | `PaidSourceBudget` per command, 240 s enrich budget |
| Delivery | **Discord** bot REST (`digest.py`) | no | Apprise: Discord, webhook, email | no | Discord bot interactive only; scheduled push not implemented (`base.py` raises) | Vikunja, Trilium | no | no | Slack / generic webhook (wrong shape for Discord, 10-line fix) |
| Headless | CLI | CLI + MCP | Python API + FastAPI (needs login); CLI removed | CLI, lib, FastAPI | CLI, embedded client, HTTP | daemon | **no** (desktop GUI) | CLI + MCP stdio | CLI |
| Weight | no Docker, no DB, 3G cap | SQLite; crawl4ai pulls playwright | **heavy**: torch, faiss, playwright, weasyprint | LangChain + unstructured, no torch | **heavy**: README says 4 vCPU / 8 GB, nginx, node, Docker sandboxes | trivial, but needs LDR+SearXNG+Vikunja | Rust + webview + LanceDB | light local mode | stdlib only |

## 2. Fork pick: our own research-wiki (copy to a new repo, keep the live one untouched)

**Reasons.**
1. It is the only candidate that already runs forever on our stack: OpenAI-compatible proxy, Exa rotator, Arctic Shift, Discord, systemd, no Docker. It has 31 unattended commits today.
2. Every external candidate fails at least one hard constraint:
   - hyperresearch and lucasastorian need Claude Code or Codex.
   - nashsu/llm_wiki is a desktop GUI.
   - LDR's scheduler dies on restart (in-memory password) and pulls torch.
   - deer-flow is a general agent harness sized for 8 GB, with no research KB and no Discord push.
   - gpt-researcher and last30days have no loop and no KB.
3. Its gaps are exactly the parts no candidate does well anyway: KB-first search, semantic topic dedupe, claim dedupe. Forking a big repo would not save that work.

**Runner-up if Ishan insists on an external base:** last30days-skill. It is stdlib Python, runs on ARM, already has Exa, Arctic Shift, HN, GitHub, a grok X backend, a findings store with cross-run URL dedupe, and a `discovery_topics` table. Its blocker: it is a 30-day social scanner whose final write-up is done by a host agent, so the report writer, wiki and frontier would still be ours. Estimate 30-40 h to reach parity with our loop.

**What changes in the fork (research-wiki-v2), rough effort:**

| Change | Effort |
|---|---|
| Move topic queue from `topics.yml` to SQLite (topics, sources, claims, runs) | 4 h |
| Remove the 2/run, 5/day suggestion cap; replace with dedupe gate + priority score | 2 h |
| KB-first step: before `plan()`, search wiki (FTS5 + embeddings) and pass "known facts + gaps" into the plan prompt; skip searches whose answer is fresh | 6 h |
| Persistent cross-run source dedupe (normalized URL + content hash) | 2 h |
| Seed topics: scholarships, grants, startup grants, hackathons, free credits, competitors, alpha news | 1 h |
| X via grok (port, see section 3) | 3 h |
| **Total core** | **~18 h** |

## 3. Port list

| Feature | From | File | Effort |
|---|---|---|---|
| URL normalisation (www/m/utm/trailing slash) + duplicate collapse | last30days | `skills/last30days/scripts/lib/fusion.py` `_normalize_url`, `collapse_duplicate_urls` | 1 h |
| Near-duplicate text gate (trigram + token Jaccard 0.7) for sources and claims | last30days | `lib/dedupe.py` `hybrid_similarity`, `dedupe_items` | 1 h |
| Findings store with sightings and "what is new since last run" | last30days | `scripts/store.py` `store_findings`, `compute_topic_delta`, `discovery_topics` | 4 h |
| X search through grok CLI with snowflake date validation against fabricated posts | last30days | `lib/grok_x.py` `_validate_items` | 3 h (must add our kill-after-output handling, grok -p never exits) |
| Syndication / independence score (canonical URL + MinHash) | hyperresearch | `src/hyperresearch/core/independence.py`, `core/similarity.py` | 2 h |
| Verbatim quote check for citations | hyperresearch | `core/citecheck.py` | 3 h |
| Claim hashing table | hyperresearch | `core/claims.py` `_claim_hash` | 1 h |
| Run manifest with resume + spend cap | hyperresearch | `core/runs.py` | 3 h |
| Dead-link check on cited URLs + cross-model reviewer | research-queue | `daemon.py` `check_citations`, `verify_report`, `llm_review` | 2 h |
| Outage vs failure retry split (429 pauses queue, does not burn an attempt) | research-queue | `daemon.py` `.state.json` retry logic | 1 h |
| LLM page merge with union of frontmatter arrays + length guard | nashsu/llm_wiki | `src/lib/page-merge.ts` (TS, rewrite in Python) | 3 h |
| Embedding pre-filter + LLM duplicate-page groups | nashsu/llm_wiki | `src/lib/dedup_embedding.ts`, `dedup.ts` | 4 h |
| Semantic lint prompt (contradiction, stale, missing page) | nashsu/llm_wiki | `src/lib/lint.ts` | 2 h |
| Structural lint + staleness propagation | lucasastorian | `mcp/tools/lint.py`, `mcp/vaultfs/sqlite.py` `propagate_staleness` | 2 h |
| Follow-up question generator (breadth/depth tree) | gpt-researcher | `gpt_researcher/skills/deep_research.py` `process_research_results` | 2 h |
| Hard token budget cut-off | deer-flow | `backend/packages/harness/deerflow/agents/middlewares/token_budget_middleware.py` | 1 h |
| **Total ports** | | | **~35 h** |

Skip: LDR (nothing we cannot get lighter elsewhere; Apprise is optional since we already post to Discord), deer-flow as a runtime.

## 4. Features none of them have (we build)

1. **Endless frontier with semantic topic dedupe.** Embed every candidate topic (title + focus) and every done topic. Reject at cosine >= ~0.85; merge into the existing topic as a "deepen" item at 0.7-0.85. No candidate does topic-level semantic dedupe (last30days is exact-name only). 6 h. Needs an embedding endpoint on our proxy, or a small CPU model. Not verified that our proxy serves embeddings.
2. **Frontier scoring without human approval.** Priority = novelty (1 - max similarity to KB) x expected value (deadline soon, money amount, topic family weight) x freshness decay. Families such as "grants" re-open on a schedule because deadlines roll. 4 h.
3. **KB-first gate.** Answer the plan's sub-questions from the wiki first. Only search the ones the wiki cannot answer or whose facts are older than a TTL per family (alpha news 1 day, grants 14 days, competitors 30 days). Nobody does this in code; hyperresearch and lucasastorian only ask the agent to in a prompt. 6 h (counted in section 2).
4. **Claim-level dedupe across runs** with near-duplicate matching and a "seen in N sources" count, so the Discord digest shows only genuinely new facts. 4 h.
5. **Opportunity extraction for the target topics**: structured rows (name, amount, eligibility for an Indian individual, deadline, link) with deadline-based alerts to Discord. 5 h.

Grand total: core 18 h + ports 35 h + new 19 h, so **about 70 h**. A useful first cut (core + URL/claim dedupe + semantic topic gate + grok X) is about 30 h.

## 5. Risks

1. **Topic drift and runaway growth.** An unapproved frontier will wander, for example from grants to generic finance. Mitigate with family weights, a depth limit from the seed topics, and a daily new-topic cap that is raised slowly instead of removed.
2. **Weak local models.** Dedupe, merge and fact-check all lean on the LLM. Qwen 27B / Ornith / DeepSeek Flash will produce false merges. Keep the deterministic gates (hash, Jaccard, URL) first and the LLM second.
3. **Embeddings are a new dependency.** No verified embedding endpoint on our proxy. A CPU model on goku costs RAM; check `free -m` first.
4. **grok -p hangs after output** (known). last30days uses a subprocess with timeouts; verify it reaps the process on our boxes before trusting it.
5. **Exa pool exhaustion.** The KB-first gate is the main saver. Keep the existing live-key floor in `exa_budget.py`.
6. **Arctic Shift fragility.** It 422s and times out on busy subs (already handled with backoff). There is no fallback, and keyless Reddit scraping would burn our IPs.
7. **llm-wiki-compiler is a third-party npm dep** (atomicstrata) that we did not audit here. Moving page merges into our own code (port from nashsu) removes it, at the cost of the port hours above.
8. **Prompt injection from scraped pages** grows with an unattended frontier. Keep `quarantine.py` and `neutralize()` in the path, and never let a scraped page add topics directly; topics should come only from the model's SUGGEST lines after the injection scan.
9. **Disk.** Oracle was at 97% during this audit. The KB and raw folders grow forever; add retention for `raw/` and `logs/`.
