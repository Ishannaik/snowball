# Snowball System Design Specification

**Version:** 1.1 (access amendment and gate-evidence policy), plus the 2026-10-10 goal-harness amendment in sections 1.6, 5.5, 6.10, and 7.5
**Date:** 2026-10-08; goal-harness amendment 2026-10-10
**Repo:** `github.com/Ishannaik/snowball` (private), checkout at `~/repos/snowball`
**Supersedes:** `DESIGN.md` (crawl tree, v0). `PLAN.md` remains the legacy feature list and points at the goal-harness plan. Harness scenarios G01–G30 are not ledger rows in this amendment.
**Gate status:** section 9.1 and `data/gate-evidence.jsonl`. A verified ledger row is the only completion record for a gate.
**Inputs:** the v0 fork, the 8-agent competitor audit in `COMPETITOR_DEEP_DIVE.md` and `FEATURE_MATRIX.md`, and the host facts measured on 2026-10-06 (goku: aarch64, 4 cores, 11.8 GB available RAM, 84 GB free disk, SQLite 3.45.1; oracle: SQLite 3.45.1, curl_cffi 0.15.0 with `CurlOpt.RESOLVE`, default impersonation profile `chrome146`). Those free-memory and free-disk figures are historical measurements, not deployment evidence. Measure the host again at deployment.

---

## Table of contents

1. [Executive summary and foundational invariants](#1-executive-summary-and-foundational-invariants)
   - [1.6 Goal control plane](#16-goal-control-plane)
2. [End-to-end system architecture](#2-end-to-end-system-architecture)
3. [The 5-layer deterministic no-repeat lock engine](#3-the-5-layer-deterministic-no-repeat-lock-engine)
4. [The 3-tier scraper waterfall and pluggable anti-ban design](#4-the-3-tier-scraper-waterfall-and-pluggable-anti-ban-design)
5. [SQLite schema](#5-sqlite-schema)
   - [5.5 Schema version 4](#55-schema-version-4)
6. [Multi-subagent orchestration topology](#6-multi-subagent-orchestration-topology)
   - [6.10 Goal gate and outcomes](#610-goal-gate-and-outcomes)
7. [Safety, egress and anti-hallucination pipeline](#7-safety-egress-and-anti-hallucination-pipeline)
8. [Knowledge base and incremental wiki compiler](#8-knowledge-base-and-incremental-wiki-compiler)
9. [Milestone roadmap M0 to M4](#9-milestone-roadmap-m0-to-m4)
10. [Cross-cutting failure modes and mitigations matrix](#10-cross-cutting-failure-modes-and-mitigations-matrix)
- [Appendix A: configuration reference](#appendix-a-configuration-reference)
- [Appendix B: CLI reference](#appendix-b-cli-reference)
- [Appendix C: constants table](#appendix-c-constants-table)

---

## 1. Executive summary and foundational invariants

### 1.1 What Snowball is

Snowball is a research agent that runs 24 hours a day on the `goku` VPS. It builds and maintains its own knowledge base: a Markdown wiki that is the source of truth, plus a SQLite database that holds the crawl frontier, the no-repeat locks and a rebuildable full-text index. It looks for grants, hackathons, startup funding, free GPU and cloud credits, competitor moves and early "alpha" about AI tools. Nobody approves its work. Once a day it posts a Discord digest that contains only what is new or changed since the last digest. Managed fetching, residential proxies, and CAPTCHA solving are outside the default loop. Section 4 states when an operator may activate them for authorized public pages. Writing that code is feature implementation. Calling a provider is a separate activation step.

Historical deployment picture, measured for the 2026-10-08 writeup. The goku-only 24-hour loop, the "nobody approves its work" sentence, and the claim that Oracle holds no Snowball state describe that lead crawler. The current operator layout, including the Oracle checkout and `data/highered.db`, is in README Oracle Deployment. That section keeps `snowball-loop.service` disabled and does not record a goal-harness rollout. Section 1.6 is the control plane now being implemented.

### 1.2 The problem it solves

Two approaches to "what should the agent research next" both fail:

1. **Ask the LLM to invent search queries.** Measured on our own models, about 90% of generated queries collapse onto the same handful of ideas. A model samples from the centre of its training distribution. The topics worth finding are, by definition, the ones that are not in that centre.
2. **Hand-write a query list.** It is stale the day it is written. It cannot name a program nobody on the team has heard of.

### 1.3 The Snowball crawl thesis

The web already contains the index we want. Directory pages, listing pages and "awesome" lists (we call them **hubs**) enumerate the entities we care about. Articles name entities we have not seen. Pages that link to a hub are often hubs themselves. So:

- **Code** builds the day-one seed grid: `family x region x audience x source x time window`.
- **The web** supplies every topic after that. The crawler finds hubs, extracts **entity names** from pages, schedules exactly one targeted search per **unknown** name, extracts who links to the hubs to find more hubs, and watches known hubs for **row additions** over time.
- **The LLM** reads text we fetched and returns structured JSON. It never invents a query, never proposes a topic from memory, and never decides whether something is new.
- **Deterministic code** decides novelty through five lock layers (query, page, entity, claim, topic).

The crawl snowballs: every page fetched produces names, links and hub candidates, and the locks make sure each of them costs work exactly once.

### 1.4 Foundational invariants

These hold at every milestone. A change that breaks one is a design change, not a bug fix.

| # | Invariant | Enforced by |
|---|---|---|
| I1 | **Code seeds, the web supplies facts.** A model may propose a search hypothesis or a fetch of a URL already seen for this goal. Code validates public scope and provenance, deduplicates, checks access policy, and reserves a finite budget. `hypothesis` and code-generated `gap` are scheduling provenance, never evidence. Only a source-backed reviewed observation supports current qualification. The legacy lead rule remains: after day one, lead-crawler queries use `discovered_via` of `hub`, `name`, `backlink`, or `similar`. | Section 1.6, planner validation, section 5 |
| I2 | **The LLM never judges novelty.** New / changed / same is decided by the lock engine before any model sees the item. | Section 3 pipeline order |
| I3 | **The model has no tools.** Text in, JSON out. Code fetches, code writes. Nothing a page says can execute. | `tools/prompts.py` and `tools/engine_transport.py` for harness calls; `tools/llm.py` on the historical lead path; section 6.5 |
| I4 | **Authorized public access only.** Configured managed fetch, a residential proxy, and a CAPTCHA provider may retrieve operator-approved public research pages. Escalation is domain-scoped, cost-capped, and time-bounded. A login wall, paywall, robots exclusion, explicit access denial, or rate-limit ban is terminal or a cooldown, not a reason to rotate identity or keep paying. Feature implementation does not activate a provider: paid execution stays off until credentials, a current tariff, nonzero ceilings, and an `access.yml` allowlist all exist. Shipped secret-free configuration keeps paid ceilings at zero. A missing prerequisite is `blocked`. | Section 4 |
| I5 | **Every extracted fact carries an evidence quote found verbatim in the fetched page.** A fact without a passing quote is dropped, not softened. | Section 7.4 |
| I6 | **Re-check means diff, not redo.** A due page is re-fetched, hashed, and only its new rows enter the pipeline. | Section 3.2, 4.6 |
| I7 | **SQLite holds the frontier, locks, goals, snapshots, and evaluations. Markdown holds human-edited regions and rendered views.** `wiki/` can rebuild `wiki_*` tables and FTS5. SQL goals, snapshots, and evaluations cannot be recreated from summaries. The frontier and lock tables stay backed up with the v4 corpus bundle (section 5.5). | Section 8.1, section 5.5 |
| I8 | **Budgets sleep, they never fail.** Exa reserve, token budget, per-domain rate, paid fetch cap and memory cap all pause work and resume when the window opens. | Section 6.4 |
| I9 | **Zero PyTorch, zero FAISS, no headless browser on the default fetch path.** Pure Python, SQLite FTS5, BM25 and TF-IDF do the ranking. A separate one-context challenge worker is outside that path and stays off until provider activation. | Section 7.3, 3.5, 4.1 |
| I10 | **Only new or changed reaches a human.** A digest line is an `entities` row with `reported_at IS NULL`, or an `entity_changes` row with `reported_at IS NULL`, and nothing else. | Section 8.6 |

### 1.5 Non-goals

- Public pages only. Managed fetch, residential proxy, and CAPTCHA solving apply only to exact origins and path prefixes in `access.yml`, and only for supported public challenges: standalone Turnstile, Turnstile challenge flows, and reCAPTCHA v2. Any other challenge family is `unsupported_challenge`. A solver token is not proof of access.
- No login bypass, no paywall bypass, no purchase or application actions, no use of operator credentials on target sites, and no circumvention of an explicit access denial. Robots exclusions are final. HTTP 429 is a cooldown, not paid identity rotation.
- No crawling of leak, warez or credential sites.
- No acting on findings: Snowball never applies, posts, purchases, or emails.
- No per-page approval queue. Domain policy is operator configuration, not a queue. Human feedback is optional and only adjusts scores.
- Private topics stay on local models. They never use an external paid fetcher, a solver, or a public model endpoint.
- No treating a green unit suite, a fixture file, an empty result, a missing provider key, or an existing git tag as completion of the Fable gates. Section 9.1 is the status rule. Harness scenarios G01–G30 are a separate matrix in the goal-harness spec. This amendment does not record them as passed.

### 1.6 Goal control plane

Sections 1.1 through 6.9 describe the legacy lead crawler: topic seeds, no contract, and a model that does not propose queries. The harness above that crawler admits a wave only when `runnable_goal` finds one goal in `running` or `monitoring`, an approved current contract, and an active execution grant for that contract version and mode, inside its time window, with ledger headroom.

Goal states are `draft`, `needs_information`, `contract_proposed`, `approved`, `running`, `pausing`, `paused`, `revising`, `research_ready`, `monitoring`, and `stopped`. Approval stores the hash you reviewed, covering the contract body and the frozen profile snapshot. A later draft edit does not change that version. `grant` is a separate write from `start`. Replacing a grant keeps settled and lost lifetime usage.

Interview functions in `tools/interview.py` are `build_contract`, `next_question`, `validate_answer`, `question_for`, and `compile_public_frame`. Questions are code-owned. Status `known` and provenance `user_confirmed` are written only from an interview answer with confirm true. A declined confirmation stores `draft` / `user_stated`. `imported_history` and `system_default` stay unconfirmed. Propose stores the full profile snapshot, and `body_hash` is SHA-256 of canonical `{"body", "profile_snapshot"}`. That snapshot keeps unknown and unconfirmed private rows with their real status, provenance, and visibility. The body requires confirmed public material and an explicit `consent_private_evaluation`. Empty `scope.regions` stays unconfirmed until `scope_broad_acknowledged` is explicitly true. Empty is never worldwide, and the interview does not fill a default nationality or cash amount. Consent false allows bounded public research while private eligibility stays missing or unconfirmed. `next_question` does not then require `max_upfront_cash`. Consent true asks for those private fields and still does not store an unanswered limit as zero. `compile_public_frame` projects only whitelisted confirmed `public_ok` values and copies degree level and modality from those answers. Private amounts, citizenship, credentials, and hashes stay out of the frame. Unknown private criteria stay `unknown` and block `CompletionStatus.ready`. Private evaluation is not scheduled until consent is confirmed true.

I1 for this harness: code seeds facts and the web supplies quotes. A model may propose a search hypothesis or a fetch of a URL this goal has already seen. Code checks public scope, provenance, access policy, and a finite reservation. `hypothesis` and code-generated `gap` schedule work. They are not evidence. Only a source-backed reviewed observation supports current qualification. The lead-crawler query classes `hub`, `name`, `backlink`, and `similar` remain the legacy rule.

Four prompt layers:

| Layer | Owner | Cloud call |
|---|---|---|
| System policy | `tools/prompts.py`, static | Every public call |
| Goal contract | `goal_contracts` body and frozen profile snapshot | Never. The public frame is a separate projection and has its own hash. |
| Strategy | `strategies` | The plan call receives a compact public coverage summary only. |
| Compiled task | `compile_extract_fields`, `compile_review_observations`, `compile_plan_hypotheses` | System policy, task section, public frame, fenced public source |

The public source type is `prompts.PublicSourceSnapshot(id, url, title, text, content_hash, source_cycle_label=None, applicability={}, visibility='public', provenance='public_fetch')`. `validate_public_request` checks provenance and visibility before a public prompt is compiled. Page id and owner-private binding metadata stay outside that dataclass. `interview.PublicFrame` is the frame type. `prompts.PublicFrame` resolves to it. A raw page string is not an extract entry point. `tools/eval_gates.py` `eval_extract` still calls `extract_page_entities(source_text=, url=)`, and that call raises `TypeError`.


Public engines live in `tools/engine_transport.py`: `GeminiBulkExtractor`, `ReviewEngine`, `LocalPrivateEngine`, and `EngineReply`. Each constructor is `(cfg, dry=False)` and holds `cfg` and `dry` only. `available()` returns `(ok, reason)`. `diagnostics()` returns the role status. `dry=True` makes no network call.

Engine methods:

- `GeminiBulkExtractor.extract(frame, program, field_keys, source, binding, operation_key, max_out)`
- `ReviewEngine.review(frame, observations, source, binding, operation_key, max_out)`
- `ReviewEngine.plan(frame, public_summary, exploration_budget, binding, operation_key, max_out)` with `exploration_budget` an int
- `LocalPrivateEngine.private_eval(task, evidence, binding, operation_key, max_out)` for a bound harness task

`source` on extract and review is `prompts.PublicSourceSnapshot`. Extract `program` is only `program_key`, `name`, `institution`, `official_domain`, `degree_level`, `modality`, and `field_tags`. `field_keys` is a separate list and must not be a key inside `program`. Review takes `observations` and that snapshot. It does not take `program` or `field_keys`. Extract and review binding is exactly `goal_id`, `contract_version`, `strategy_version`, `epoch`, and `task_id`, each an int. The orchestrator strips every other key before those calls.

`planner.planner_proposals` calls `plan` with that integer budget. `EngineReply` carries `text`, `tokens_in`, `tokens_out`, `model`, `requested_model`, `reported_model`, `transport`, `usage_kind`, and `model_call` (prompt version, frame hash, source ids, usage split). Direct Gemini replies with parsed provider usage are `actual`. Oracle OMP and AGY replies are `estimated` unless `adapter_conformance` attests that exact URL. Unparsed usage is `unknown`. Those labels are not a provider invoice cap. There is no `GrokAdversarialVerifier` class and no `LocalOrnithFallback` class. Wiki ask looks for `LocalPrivateEngine.interpret(prompt, max_out=1024)` and does not send that prompt through `private_eval`. The class currently defines `private_eval` only, so ask returns `private_engine_unavailable` until `interpret` exists. Neither path uses a public model.

| Role | Class | Oracle model id |
|---|---|---|
| extract | `GeminiBulkExtractor` | `agy-pool/gemini-3.8-flash` |
| review | `ReviewEngine` | `agy-pool/claude-sonnet-4-6` |
| plan | `ReviewEngine` | `agy-pool/claude-sonnet-4-6` |
| `private_eval` | `LocalPrivateEngine` | `private_model_id` on an attested `private_model_endpoint`. Leave the endpoint unset until isolation is attested |

`http://127.0.0.1:8088/v1` is the AGY cloud proxy. Loopback does not make that process local inference. `GROK_X_*` remains X post acquisition only.

Frontier admission is `planner.gap_proposals(store, goal, coverage)`, `planner.validate_proposals(store, goal, proposals)`, `planner.planner_proposals(engine, frame, coverage, budget)`, and `planner.schedule(store, goal, validated, wave_id)`. `schedule` reserves through `ledger.reserve_batch`. It does not call `admit_node`, and it does not import a `snowball_locks` module. Extract and review binding is exactly `goal_id`, `contract_version`, `strategy_version`, `epoch`, and `task_id`. Ledger `reserve_batch` requires `goal_id`, `contract_version`, `strategy_version`, and `epoch`. A node reservation also carries `node_kind`, `node_key`, `phase`, and `depth`. A domain reservation also carries `domain`, `task_id`, `lease_token`, and `lease_until`. Model-token reservations may add `role` and `shared_day_limits`. Those ledger fields do not go to the public engine. `binding_valid(db, goal_id, contract_version, strategy_version, epoch)` takes the open connection. It is true for `running` or `monitoring` when the epoch matches, the named contract is the current approved version, and that strategy row exists. An older strategy on the same epoch stays valid. `reserve_batch(store, grant_id, operation_key, amounts, binding)` is the only atomic reserve and calls `binding_valid`. `mark_dispatched(store, operation_key, goal_id)` is a separate fenced step and succeeds once. The orchestrator calls both for extract, review, and plan token amounts on that same ledger. `LockEngine` in `tools/orchestrator.py` still owns `query_lock(db, raw, source, recheck_days, topic_id, via)` and `enqueue_url(db, raw, topic_id, via, recheck_days)`.

`goal.validate_limits` accepts grant `exploration_share` as an integer or a canonical decimal string inside the approved policy ceiling. It rejects a boolean and a JSON float such as `0.3`. The policy ceiling itself may be a finite float in `0..1`. `exploration_budget` on `plan` is a separate nonnegative integer. `ledger.reserve_batch` accepts a stored share that is an integer, a finite float from 0 to 1, or a canonical decimal string, and it rejects `usage_mode` `hard`. `goal grant` calls `validate_limits` before insert, so the grant file uses the integer or the string. The orchestrator does not rewrite the share.

`completion_status` returns `CompletionStatus`. `ready` is true only for the positive predicate: enough current matching packages, reviewed support, unknown never counted as met, and conditional staying conditional. A private criterion left unknown, including when consent is false, stays `unknown` (`consent_required` on the private matchers) and keeps the package out of the ready count. `detail` is a dict of reasons and package outcomes, not a status token. The substates this function returns are only `partial_coverage`, `verifier_unavailable` (the reviewer is unavailable; this is not the `review_pending` assurance), `no_match`, and `research_ready`. The worker writes the `research_ready` goal state only when that predicate passes. You do not set it from the CLI. Qualification does not start monitoring and does not record admission or a funding award. Assurance levels are in section 7.5. Rows with provenance `legacy_v3` stay historical leads.

`snowball.py serve` execs `tools/console.py` with `--db`, `--host`, and `--port`. `snowball.py console` forwards the remainder to `tools/console.py`. `tools/visualize.py` is the terminal view. It is not the console server. Console read routes do not migrate. `status` and `--dry-run` call `Store.connect_existing` and do not call `migrate`. `status` opens the selected database read-only. `--dry-run` copies it in memory and writes nothing back to that file.

This section is the implementation contract. It is not evidence that G01–G30 passed, it does not record a production deployment, and it does not enable `snowball-loop.service`. Snowball does not install a periodic Gemini hook. A public-only Gemini pass in the build workflow is outside this runtime. Design scenarios O01–O03 stay not exercised until you approve a goal, a grant, and a bounded live smoke.

---

## 2. End-to-end system architecture

### 2.1 Component diagram

```
                              goku (aarch64, 4 cores, 11 GB RAM free, 84 GB disk free)
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │  systemd --user                                                                          │
 │   snowball-supervisor.service  (Restart=always, MemoryMax=3G, STOP file pauses)          │
 │   snowball-digest.timer        (daily 07:30 IST)                                         │
 │   snowball-retention.timer     (daily 03:10 IST)                                         │
 │   snowball-llm-tunnel.service  (ssh -N -L 8812:127.0.0.1:8812 -L 8791:127.0.0.1:8791)    │
 │                                                                                          │
 │  topics.yml ──► seeder.py ──► queries(seed grid) ─┐                                      │
 │                                                   ▼                                      │
 │   ┌────────────────────────── snowball.db (SQLite, WAL) ───────────────────────────┐     │
 │   │ frontier: queries, pages, hubs, names, tasks      locks: pages, entities,       │     │
 │   │ domains, budgets, fetches, paid_fetches           claims(+fts), topics(+fts)    │     │
 │   │ wiki index (rebuildable): wiki_pages, wiki_links, wiki_fts                      │     │
 │   └──────────────────────────────▲──────────────────────────────────────────────────┘     │
 │                                  │ BEGIN IMMEDIATE, occurrence_seq claims                 │
 │   ┌──────────────────────────────┴───────────────────────────────────────────────────┐    │
 │   │ supervisor.py  (asyncio, one process)                                            │    │
 │   │   scheduler ─► wave builder ─► pools:                                            │    │
 │   │     Collector x6   fetch waterfall  ──► safety gate (SSRF, size, injection)      │    │
 │   │     Extractor x1/2 classify + extract JSON (LLM over tunnel)                     │    │
 │   │     Verifier  x4   quote check, number check, dead link, lock engine, writer     │    │
 │   │   reaper (orphaned tasks) · budget guard · memory guard · token budget           │    │
 │   └───────────────┬──────────────────────────────────────────────┬───────────────────┘    │
 │                   ▼                                              ▼                        │
 │   raw/YYYY-MM-DD/<page_id>.txt   (14-day retention)      wiki/  (Markdown truth, git)    │
 │                                                           compiler.py (incremental)       │
 │   digest.py ──► Discord REST (new + changed only) ◄── digest_items                        │
 └───────────────────────────────┬──────────────────────────────────────────────────────────┘
                                 │ Tailscale (100.65.187.17 ◄──► 100.68.165.73), SSH tunnel
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │  oracle ("personal-use", root disk 91-97% full: NO Snowball state lives here)            │
 │   127.0.0.1:8812  llm_adapter (raises max_tokens to 16384) ─► 127.0.0.1:8800 qwen proxy  │
 │        models: ornith-1.5-35b-a3b-abliterated (1 slot), qwen3.8-27b-abliterated          │
 │   127.0.0.1:8810  gemma shim (public topics, small prompts only)                         │
 │   127.0.0.1:8791  Exa key rotator (/search, /contents), live-key floor RESERVE=3         │
 │   grok CLI (X search; writes stdout then never exits: launch, poll, kill)                │
 └──────────────────────────────────────────────────────────────────────────────────────────┘

 External, read-only:  Arctic Shift (Reddit) · Algolia HN · GitHub API · RSS/sitemaps ·
                       direct HTTPS via curl_cffi · optional ZenRows / residential proxy (opt-in)
```

The diagram is the historical goku lead-crawler layout measured on 2026-10-06: host size, the SSH tunnel, Ornith, Qwen, Gemma, and the line that Oracle holds no Snowball state. Those host figures, the 2,000,000 token guard in section 6.4, and a later 32,000,000 process default are historical ceilings. They are not the Oracle estimated caps in the README, and they are not a hard provider-token limit. Current engines and model ids are section 1.6. Operator steps are README Oracle Deployment. Managed fetch, residential proxy, and the challenge worker are specified in section 4. Writing that code is feature implementation. Provider activation is separate: credentials, a current tariff, nonzero ceilings, and an `access.yml` allowlist. Do not swap in a paid GPU alias to force a smoke pass.

Private topics stay on local models. They never use an external paid fetcher, a solver, or a public model endpoint.

### 2.2 Subsystem boundaries

| Subsystem | Module | Owns | Never does |
|---|---|---|---|
| Seeder | `snowball/seeder.py` | Day-one query grid from `topics.yml` | Run after day one unless `topics.yml` changes (then only the delta) |
| Scheduler | `snowball/scheduler.py` | Frontier scoring, pop, due timers, budget and memory guards | Fetch or call the LLM |
| Collector | `snowball/fetch/*.py` | Waterfall fetch, route selection, wall detection, strike/outage logic, raw storage | Parse semantics, write to wiki |
| Safety gate | `snowball/safety/*.py` | SSRF pinning, body limits, injection quarantine, secret scan | Decide relevance |
| Classifier | `snowball/classify.py` | hub / item / article / junk, heuristics first | Extract rows |
| Extractor | `snowball/extract.py` | LLM JSON extraction of rows and names, schema validation, substring guard | Decide new / changed |
| Lock engine | `snowball/locks/*.py` | The five locks, canonicalisation, hashing, similarity | Call the LLM |
| Verifier | `snowball/verify.py` | 3-tier quote check, number check, dead-link check | Rewrite claims |
| Writer | `snowball/writer.py` | Upserts to `entities`, `claims`, `entity_changes`, `names`, `pages` | Format Markdown |
| Compiler | `snowball/compiler.py` | Incremental Markdown pages, frontmatter union, 70% floor, D<=1 staleness, FTS5 reindex | Fetch anything |
| Delivery | `snowball/digest.py`, `snowball/alerts.py` | Discord digest and urgent alerts, `reported_at` bookkeeping | Select what is "interesting" by LLM |
| Supervisor | `snowball/supervisor.py` | Process lifecycle, pools, waves, reaper, status | Business logic |

Access policy (`access.yml`, module planned as `tools/access_policy.py` under task T2) decides which origins may leave the free fetch route. The modules above are the free-path architecture. Paid and browser routes stay inactive until provider activation (tasks T2–T4). T0 records that policy and does not add those modules.

### 2.3 Data flow, one page

```
 1. scheduler.pop()          highest score, due, budget OK, domain not paused   → tasks(kind=fetch)
 2. collector.fetch()        router → tier 1 → tier 1b → tier 2 (opt-in) → walled → raw/…txt, fetches row
 3. safety.gate()            SSRF pin before connect; 5 MB cap; injection scan → pages.quarantined
 4. locks.page()             canon_url, content_hash, MinHash → skip / alias / reprint / proceed
 5. classify()               heuristics → hub | item | article | junk | unsure → LLM only if unsure
 6a. hub                     link extraction → hub_rows diff → new rows → pages (priority high), hubs.next_check
 6b. item / article          bm25.filter(chunks) → extract.rows_and_names (LLM JSON, substring guard)
 7. verify()                 Tier 1 numbers/URLs → Tier 2 verbatim quote → (Tier 3 reject-only) → drop failures
 8. locks.entity/claim()     key + fuzzy → new / changed(field diff) / same
 9. writer()                 upsert rows, entity_changes, names(status=pending) → one query per unknown name
10. compiler.mark_dirty()    entity/topic page dirty; linkers stale at depth 1
11. yield.update()           hub, domain, query yield counters; next_check adaptive
12. digest (daily)           reported_at IS NULL → rank → Discord → set reported_at on HTTP 2xx
```

### 2.4 Process and file layout

```
~/repos/snowball/
  snowball/              Python package (stdlib + curl_cffi + trafilatura + pyyaml + aiohttp)
  data/snowball.db       authoritative DB (WAL). Retention writes the section 5.5 bundle. A dated `snowball-YYYY-MM-DD.db` file is historical
  raw/YYYY-MM-DD/        extracted text per fetch, 14-day retention
  wiki/                  Markdown truth, git-committed after each wave
    entities/<slug>.md   one per entity key
    topics/<slug>.md     one per topic
    hubs/<slug>.md       one per hub page (what it lists, how often it changes)
    index.md             generated
  digests/YYYY-MM-DD.md  always written, posted to Discord when DIGEST_CHANNEL_ID is set
  logs/                  JSON lines, 30-day retention
  config.env             non-secret settings (Appendix A)
  .env                   secrets (QWEN_API_KEY, optional ZENROWS_API_KEY, RESIDENTIAL_PROXY_URL)
  topics.yml             seed families and seeds
  access.yml             operator allowlist: exact origins, path prefixes, modes. Absent file means free fetch only.
  STOP                   touch to pause the supervisor between waves
```

This tree is the historical goku checkout. The Oracle console checkout is `/home/ubuntu/repos/snowball` with database `data/highered.db`. A v4 cutover bundle is `database.db`, `manifest.json`, and `files/` with SHA-256 of referenced raw bytes and human-edited wiki markdown (section 5.5). A dated DB-only `snowball-YYYY-MM-DD.db` file is historical and is not that bundle. `STOP` still pauses the supervisor between waves and does not change goal state (section 6.10).

### 2.5 Network

- All LLM and Exa traffic leaves goku through one SSH tunnel to oracle, kept alive by `snowball-llm-tunnel.service` (`ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 127.0.0.1:8812:127.0.0.1:8812 -L 127.0.0.1:8791:127.0.0.1:8791 oracle-a1`, `Restart=always`). Nothing on oracle changes, and the proxy keeps listening on localhost only. Re-check that mapping at deployment. A historical listener address is not proof the tunnel is up.
- `100.64.0.0/10` (the Tailscale CGNAT range) is in the SSRF deny list, so a crawled link can never reach a tailnet peer. The same deny list covers the browser egress gateway when that worker is activated. Local DNS pinning does not cover a remote provider's own resolver.
- Outbound crawl traffic uses goku's own IP. There is no proxy unless provider activation for an approved public origin is in effect (section 4.5).

The three bullets above are the historical goku tunnel. They say Oracle does not change and that Snowball state stays off Oracle. The current console is tailnet-only at `https://snowball.ishannaik.com/`. Caddy binds `100.68.165.73:443` and forwards to `127.0.0.1:8989`, rewriting `Host` to `127.0.0.1:8989`. The browser `Origin` remains `https://snowball.ishannaik.com`. `http://127.0.0.1:8088/v1` is the AGY cloud proxy, not local inference. Operator steps are in README Oracle Deployment.

---

## 3. The 5-layer deterministic no-repeat lock engine

The engine runs **before** any LLM call on the way in (page, query) and **before** any write on the way out (entity, claim, topic). Every layer has a deterministic key, a fuzzy fallback that is also deterministic, a threshold, and a fixed action. The LLM is never consulted for sameness.

### 3.1 Layer 1: query lock

**Key:** `query_key = sha1(norm_query + "|" + source + "|" + window_key)`

```python
import hashlib, re, unicodedata


def norm_query(q: str) -> str:
    q = unicodedata.normalize("NFKC", q).casefold()
    q = re.sub(r"[^\w\s\-]", " ", q)  # keep word chars, spaces, hyphens
    toks = sorted(set(q.split()))  # order-free: "grants india" == "india grants"
    return " ".join(toks)


def window_key(family_recheck_days: int, now: int) -> int:
    return now // (family_recheck_days * 86400)


def query_key(raw: str, source: str, recheck_days: int, now: int) -> str:
    return hashlib.sha1(f"{norm_query(raw)}|{source}|{window_key(recheck_days, now)}".encode()).hexdigest()
```

**Action:**

| Condition | Action |
|---|---|
| `query_key` exists | Skip. Increment `queries.dup_requests` for the yield report. |
| `norm_query + source` exists in an older window | Allowed, this is a scheduled re-run. Insert new row with new `window_key`. |
| Not found | Insert `status=pending`. |

Sources are `exa`, `arctic`, `hn`, `github`, `grok`. The same words against two sources are two queries.

### 3.2 Layer 2: page lock

Three keys, checked in order.

**Key 2a: canonical URL.** Port of `last30days/lib/fusion.py::_normalize_url`, extended.

```python
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING_EXACT = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "si",
    "ref",
    "ref_src",
    "source",
    "spm",
    "trk",
    "_hsenc",
    "_hsmi",
    "oly_enc_id",
    "oly_anon_id",
    "vero_id",
    "wickedid",
    "feature",
    "utm",
}
HOST_PREFIXES = ("www.", "m.", "mobile.", "old.", "amp.", "new.")
INDEX_FILES = ("/index.html", "/index.htm", "/index.php", "/default.aspx")


def canonical_url(raw: str) -> str | None:
    u = urlsplit(raw.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.encode("idna").decode().lower().rstrip(".")
    for p in HOST_PREFIXES:
        if host.startswith(p) and host.count(".") >= 2:
            host = host[len(p) :]
            break
    path = re.sub(r"/{2,}", "/", u.path or "/")
    for f in INDEX_FILES:
        if path.endswith(f):
            path = path[: -len(f)] or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    q = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_EXACT
    ]
    q.sort()
    return urlunsplit(("https", host, path, urlencode(q), ""))  # scheme and fragment collapse
```

Edge rules:
- The original `raw_url` is kept in `pages.raw_urls` (JSON array) and is what the fetcher requests; `canon_url` is only the key.
- A redirect chain re-canonicalises the final URL. If the final `canon_url` already exists, the fetched page is written as an alias (`pages.alias_of`) and no new row is created.
- Reddit, HN and GitHub routes canonicalise to their API identity (`https://reddit.com/comments/<id>`, `https://news.ycombinator.com/item?id=<id>`, `https://github.com/<owner>/<repo>`).

**Key 2b: content hash.**

```python
def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}))
    return re.sub(r"\s+", " ", s).strip()


def content_hash(main_text: str) -> str:
    return hashlib.sha256(norm_text(main_text).casefold()[:200_000].encode()).hexdigest()
```

`main_text` is the article body from `trafilatura.extract(html, include_links=False, include_comments=False)`, falling back to a stdlib `HTMLParser` tag stripper when trafilatura returns `None`. Boilerplate removal is what makes the hash stable across a page's own re-renders.

**Key 2c: MinHash reprint signature.** Port of `hyperresearch/core/independence.py`.

```python
P = (1 << 61) - 1
SEEDS = [(a, b) for a, b in zip(range(1, 129), range(1000, 1128))]  # fixed, 128 permutations
BANDS, ROWS = 16, 8


def shingles(text: str, k: int = 3) -> set[int]:
    toks = norm_text(text).casefold().split()
    return {
        int.from_bytes(hashlib.blake2b(" ".join(toks[i : i + k]).encode(), digest_size=8).digest(), "big")
        for i in range(max(0, len(toks) - k + 1))
    }


def minhash(text: str) -> list[int]:
    sh = shingles(text)
    if not sh:
        return [0] * 128
    return [min(((a * s + b) % P) & 0xFFFFFFFF for s in sh) for a, b in SEEDS]


def band_keys(sig: list[int]) -> list[tuple[int, str]]:
    return [
        (b, hashlib.sha1(bytes(str(sig[b * ROWS : (b + 1) * ROWS]), "ascii")).hexdigest()[:16]) for b in range(BANDS)
    ]


def est_jaccard(a: list[int], b: list[int]) -> float:
    return sum(x == y for x, y in zip(a, b)) / 128
```

Candidates come from `minhash_bands` (any shared band bucket). A candidate with `est_jaccard >= 0.80` is a reprint.

**Action table:**

| Check | Result | Action |
|---|---|---|
| `canon_url` known, `next_check_at > now` | seen, not due | Skip. `pages.dup_requests += 1`. |
| `canon_url` known, due | re-check | Fetch, compute `content_hash`. Same hash: `interval_sec = min(interval_sec*2, 90d)`, no further work. Different: `interval_sec = max(interval_sec/2, 6h)`, diff hub rows, proceed only with new rows (I6). |
| `canon_url` new, `content_hash` exists on another page | exact copy | Insert as `status=alias`, `alias_of=<page_id>`. No classification, no extraction. |
| `canon_url` new, MinHash est. Jaccard >= 0.80 | reprint | Insert as `status=reprint`, `reprint_of=<page_id>`. Links are still extracted (a reprint can point to new hubs). Claims from it do **not** increment `n_sources` (not an independent source). |
| None of the above | new | Proceed to classification. |

### 3.3 Layer 3: entity lock

**Key:** `entity_key = slug(norm_name) + "|" + (slug(norm_org) or "-") + "|" + (year or "-")`

```python
LEGAL_SUFFIX = {
    "inc",
    "inc.",
    "llc",
    "ltd",
    "ltd.",
    "limited",
    "corp",
    "corp.",
    "corporation",
    "co",
    "co.",
    "gmbh",
    "pvt",
    "plc",
    "sa",
    "ag",
}
NAME_STOP = {"the", "a", "an", "of", "for", "and", "&"}
YEAR_RX = re.compile(r"\b(20[2-3]\d)\b")


def norm_name(s: str) -> tuple[str, str | None]:
    s = unicodedata.normalize("NFKC", s).casefold()
    year = YEAR_RX.search(s)
    s = YEAR_RX.sub(" ", s)
    toks = [t for t in re.sub(r"[^\w\s]", " ", s).split() if t not in NAME_STOP and t not in LEGAL_SUFFIX]
    return " ".join(toks), (year.group(1) if year else None)


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.casefold()).strip("-")
    return s[:80].rstrip("-")  # fixed point: slug(slug(x)) == slug(x), tested in M1


def entity_key(name: str, org: str | None, deadline_iso: str | None) -> str:
    n, y_in_name = norm_name(name)
    o, _ = norm_name(org) if org else ("", None)
    year = (deadline_iso or "")[:4] or y_in_name or "-"
    return f"{slug(n)}|{slug(o) or '-'}|{year}"
```

Year comes from the deadline when there is one, so a yearly program ("X Fellowship 2026") is one entity per cycle and a rolling program with no deadline is one entity across time.

**Fuzzy fallback (deterministic, no LLM):** before inserting a new key, the writer runs two checks against existing entities:

1. **Link match.** If `canonical_url(link)` equals an existing entity's `link` and that link is not a hub page, it is the same entity. Record the new name in `aliases`.
2. **Name match within org.** For existing entities with the same `slug(norm_org)` (or either org null) and the same `year`, compute character-trigram Jaccard on `norm_name`. If `>= 0.85`, it is the same entity. Record alias.

**Field diff (known entity):**

```python
MATERIAL = ("deadline", "amount", "currency", "status", "eligibility", "link", "location")


def diff(old: dict, new: dict) -> list[tuple[str, str, str]]:
    out = []
    for f in MATERIAL:
        a, b = old.get(f), new.get(f)
        if f == "eligibility":
            changed = a and b and hybrid_similarity(a, b) < 0.70 or bool(a) != bool(b)
        elif f == "link":
            changed = canonical_url(a or "") != canonical_url(b or "")
        else:
            changed = (a or None) != (b or None)
        if changed and b is not None:  # a disappearance is not a change; a page can omit a field
            out.append((f, str(a), str(b)))
    return out
```

**Action table:**

| Case | Action |
|---|---|
| New key, no fuzzy match | Insert `entities` row (`status=new`, `reported_at=NULL`). Add `entity_sources`. |
| Known, `diff()` empty | `last_seen_at = now`, add `entity_sources`. Nothing else. Not reported. |
| Known, `diff()` non-empty | Insert one `entity_changes` row per field (`reported_at=NULL`), update the entity, `last_changed_at = now`. Only the changed fields reach the digest ("deadline moved 2026-11-01 → 2026-12-15"). |
| Deadline passed | Nightly job sets `status=expired`. An expired entity re-seen with a future deadline gets a new key (new year) and is a new entity. |

### 3.4 Layer 4: claim lock

A claim is one declarative sentence with a source quote. Port of `last30days/lib/dedupe.py::hybrid_similarity` plus `hyperresearch/core/claims.py::_claim_hash`.

**Exact key:** `claim_hash = sha256(norm_text(text).casefold())`

**Near-duplicate:**

```python
def char_trigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", " ", norm_text(s).casefold())
    return {s[i : i + 3] for i in range(len(s) - 2)}


def tokens(s: str) -> set[str]:
    return set(re.sub(r"[^\w\s]", " ", norm_text(s).casefold()).split())


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def hybrid_similarity(a: str, b: str) -> float:
    return 0.5 * jaccard(char_trigrams(a), char_trigrams(b)) + 0.5 * jaccard(tokens(a), tokens(b))
```

Candidate retrieval is not O(n²). The six rarest tokens of the new claim (lowest document frequency in `claims_fts`) are OR-ed into one FTS5 `MATCH`, top 50 by `bm25()`, and `hybrid_similarity` runs only against those.

**Threshold:** duplicate if `hybrid_similarity >= 0.70`.

**Action table:**

| Case | Action |
|---|---|
| Exact hash exists | `n_sources += 1` if the page is not a reprint and the domain is not already in `claim_sources` for this claim. `last_seen_at = now`. |
| Near-duplicate >= 0.70 | Same as exact, against the matched claim. The new wording is stored in `claim_variants` for the compiler. |
| New | Insert `claims` (`n_sources = 1`). |

The digest never shows claims directly. A claim with `n_sources >= 2` raises the confidence of its entity page; `n_sources` prints as "seen in N independent sources".

### 3.5 Layer 5: topic lock

Topics are the agent's own research threads (family + title + focus). The original design assumed an embeddings endpoint. None is verified on the proxy, and invariant I9 forbids a local Torch model, so the topic lock uses **TF-IDF cosine over stemmed unigrams**, pure Python, with FTS5 candidate retrieval. Embeddings remain an optional upgrade behind `EMBEDDINGS_URL` and must beat this baseline on the M3 labelled set before they are enabled.

Calibration (2026-10-06, 8 labelled pairs against the 10-topic seed corpus, four variants tested): bigrams lowered every same-topic score (0.28 to 0.65) and were dropped; suffix stemming raised them (0.64 to 0.88) and was kept. The measured scores were 0.64 to 0.88 for same-topic pairs, 0.58 to 0.65 for deepen pairs, and below 0.08 for unrelated pairs. The new-versus-related boundary is therefore sharp, and the same-versus-deepen boundary is soft. The thresholds below are set so that a misjudged pair costs one deepen run (cheap, and the query lock still dedupes its searches) rather than a duplicate topic.

```python
import math
from collections import Counter


def stem(t: str) -> str:
    for suf in ("ings", "ing", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def topic_terms(title: str, focus: str) -> Counter:
    toks = [
        stem(t) for t in re.sub(r"[^\w\s]", " ", norm_text(f"{title} {focus}").casefold()).split() if t not in NAME_STOP
    ]
    return Counter(toks)  # unigrams only: bigrams measured worse


def tfidf_cosine(a: Counter, b: Counter, df: dict[str, int], n_docs: int) -> float:
    def w(c: Counter) -> dict[str, float]:
        return {t: (1 + math.log(f)) * math.log((n_docs + 1) / (df.get(t, 0) + 1)) for t, f in c.items()}

    wa, wb = w(a), w(b)
    dot = sum(wa[t] * wb[t] for t in wa.keys() & wb.keys())
    na, nb = math.sqrt(sum(v * v for v in wa.values())), math.sqrt(sum(v * v for v in wb.values()))
    return dot / (na * nb) if na and nb else 0.0
```

`df` is the `topic_terms_df` table, maintained on topic insert. Candidates: `topics_fts MATCH` on the ten highest-IDF terms, top 30.

**Thresholds and actions:**

| Cosine | Verdict | Action |
|---|---|---|
| `>= 0.75` | same topic | Merge: the candidate's focus text is appended to `topics.focus_variants`, no new topic. |
| `0.30 – 0.75` | deepen | Append the focus text to `focus_variants` **and** insert a `tasks(kind=deepen, topic_id=<existing>)` that re-runs the existing topic with the new angle. No new topic. |
| `< 0.30` | new | Insert `topics` row with `parent_id`, `depth = parent.depth + 1`. |

Worked example: "free GPU credits" against "cloud GPU credit programs" measured 0.64, so it is a deepen of the existing credits topic, not a second topic. "startup grants" against "hackathons" measured 0.08, a new topic.

Where do topic candidates come from? Only from `names` the crawler searched (a name that yields a hub becomes a topic candidate) and from hub titles. Never from the LLM (I1).

### 3.6 Lock ordering and short-circuits

```
inbound:  query lock → (fetch) → page lock 2a → page lock 2b → page lock 2c → classify
outbound: verify → entity lock (link match → key → fuzzy) → claim lock → topic lock → write
```

A skip at any inbound layer costs zero LLM tokens. A drop at verify costs one extraction call and is recorded in `pages.noise_count`, which feeds the domain junk score.

### 3.7 Lock engine metrics

`snowball status` prints per day: `dup_requests` (pages + queries), `alias`, `reprint`, `entity_same`, `entity_changed`, `entity_new`, `claim_dup`, `claim_new`, `topic_merge`, `topic_deepen`, `topic_new`. **Repeat rate** = `(refetched_not_due + reported_twice) / total_fetches`, target `< 5%`.

---

## 4. The 3-tier scraper waterfall and pluggable anti-ban design

### 4.1 Why not active evasion

| Option | Problem on our hardware | Decision |
|---|---|---|
| Headless Chromium with stealth as the default fetcher | 400–900 MB RSS per tab on aarch64; goku's supervisor is capped at 3 GB; one leaked browser froze oracle for 50 minutes on 2026-09-28 | Not the default fetcher and not installed for ordinary fetches. An optional one-context worker (768 MB, inside `MemoryMax=3G`) is only for an authorized supported challenge and stays off until provider activation. |
| Datacenter IP plus retries | Oracle and Hetzner ASNs trigger Cloudflare Turnstile regardless of client fingerprint | Detect terminal walls. Do not rotate identity through a ban or a 429. |
| Residential proxies | Historical rough cost band, not a current tariff. Spend is integer micro-USD and traffic is integer bytes. | Allowed only as a configured route for approved public origins, with a byte ceiling and a micro-USD reservation. Code for the route is not activation. |
| Unbounded captcha solving or identity rotation | Breaks the access-denial rules in I4 | Never an evasion path. After provider activation, one named solver task may handle standalone Turnstile, a Turnstile challenge flow, or reCAPTCHA v2 on an approved public page. Other families are `unsupported_challenge`. A token is not access. |

The target data (grant pages, hackathon listings, credit programs) is meant to be read. Where a site walls us, a different hub lists the same entity.

### 4.2 Router (tier 0)

Before any HTTP request the router picks the cheapest known-good route by host pattern:

| Host pattern | Route | Notes |
|---|---|---|
| `reddit.com`, `redd.it` | Arctic Shift API (`arctic-shift.photon-reddit.com/api/posts/search`, `/comments/tree`) | Existing code. Backoff on 422/timeouts. No fallback to reddit.com HTML by design. |
| `news.ycombinator.com` | Algolia HN API (`hn.algolia.com/api/v1/search`, `/items/<id>`) | Existing code |
| `github.com`, `api.github.com` | GitHub REST (`/search/issues`, `/repos/…/readme`) with `GH_TOKEN` if present | Existing code |
| `x.com`, `twitter.com` | `grok` CLI on oracle, background + poll + kill, Snowflake validation (4.7) | Only route that can search X |
| any, when `pages.kind='hub'` and a feed is known | RSS/Atom (`<link rel="alternate" type="application/rss+xml">`) or `sitemap.xml` | Row diff from feed items, cheapest re-check |
| everything else | Tier 1 direct | |

### 4.3 Tier 1: free direct fetch with curl_cffi

```python
from curl_cffi import requests as cffi, CurlOpt

IMPERSONATE = "chrome"  # resolves to the newest profile in the installed curl_cffi (chrome146 on 0.15.0)
UA_SUFFIX = " SnowballBot/1.0 (+https://github.com/Ishannaik/snowball; research crawler)"


def fetch_direct(raw_url: str, pinned_ip: str, host: str, port: int) -> cffi.Response:
    s = cffi.Session(impersonate=IMPERSONATE)
    s.curl.setopt(CurlOpt.RESOLVE, [f"{host}:{port}:{pinned_ip}"])  # DNS pinning (7.1)
    s.curl.setopt(CurlOpt.MAXFILESIZE_LARGE, 5 * 1024 * 1024)
    return s.get(
        raw_url, allow_redirects=False, timeout=(10, 30), headers={"Accept-Language": "en-US,en;q=0.9"}, stream=True
    )
```

- `impersonate="chrome"` gives a Chrome TLS, JA4 and HTTP/2 fingerprint for about 30 MB RSS per process, which is the entire anti-ban budget at tier 1.
- Redirects are **manual** (section 7.1): each hop is re-canonicalised, re-resolved and re-pinned, maximum 5 hops, `Authorization` and cookies dropped on cross-origin hops.
- Streaming with a 5 MB hard cap. A body over the cap is truncated at the cap and marked `pages.truncated = 1`.
- `robots.txt` is fetched once per domain per 7 days and cached in `domains.robots_txt`. A disallow for `*` or for `SnowballBot` on the path marks the page `walled` with `wall_kind = robots`.
- Per-domain token bucket: `rate_per_min` default 6 (one request per 10 seconds), burst 3. `Retry-After` is honoured.

**Wall detection (status and body):**

```python
WALL_STATUS = {401, 403, 407, 451}
CHALLENGE_RX = re.compile(
    r"(cf-chl|__cf_chl_|challenge-platform|Just a moment\.\.\.|Attention Required!|cf-turnstile|"
    r"Please verify you are a human|hcaptcha\.com|recaptcha/api|g-recaptcha|px-captcha|perimeterx|"
    r"datadome|Access Denied.{0,40}akamai|Checking your browser|enable JavaScript and cookies to continue)",
    re.I,
)
LOGIN_RX = re.compile(r'<input[^>]+type="password"', re.I)


def classify_response(status: int, body: str, final_url: str) -> str:
    if status in WALL_STATUS:
        return "walled"
    if status == 200 and len(body) < 4000 and CHALLENGE_RX.search(body):
        return "walled"  # challenge disguised as 200
    if status == 200 and CHALLENGE_RX.search(body[:20000]):
        return "walled"
    if status == 200 and LOGIN_RX.search(body) and len(norm_text(strip_tags(body))) < 600:
        return "walled"
    if status in (429, 500, 502, 503, 504):
        return "outage"
    if status in (404, 410):
        return "failure"
    if status >= 400:
        return "failure"
    if len(norm_text(strip_tags(body))) < 200:
        return "failure"  # empty DOM
    return "ok"
```

### 4.4 Tier 1b: Exa contents

When tier 1 returns a public challenge that policy may still read, and the Exa reserve allows (`exa_budget.use_exa()` is true, meaning more than `RESERVE=3` live keys in the rotator), the collector may call the rotator's `/contents` endpoint once for that URL. HTTP 429, a login wall, a paywall, a robots exclusion, or an explicit access denial does not call Exa, ZenRows, a residential proxy, or a solver. Exa results use the same safety gate and hash as a direct fetch. `fetches.route = 'exa_contents'`. Local DNS pinning does not cover Exa's resolver. Exa contents is not provider activation for ZenRows, residential proxy, or a solver.

### 4.5 Tier 2: pluggable paid offload (opt-in, budget-guarded)

Tier 2 code, once it exists, is feature implementation. Provider activation requires all of: a credential, a current per-mode tariff, a nonzero shipped ceiling, and an `access.yml` entry for that exact origin and path prefix. A key alone does not activate the route. Shipped secret-free configuration keeps paid ceilings at zero. A missing prerequisite records the gate as `blocked` and does not count as a pass. Private topics never reach tier 2. A page reaches an activated tier 2 route only if **all** of these hold:

```python
def paid_fetch_allowed(page, domain, budget) -> bool:
    return (
        CONFIG.paid_provider is not None
        and budget.paid_fetches_today < CONFIG.MAX_PAID_FETCHES_PER_DAY  # shipped 0; canary 100 after activation
        and yield_score(page) >= CONFIG.PAID_FETCH_MIN_YIELD_SCORE  # default 0.30
        and page.predicted_kind in ("hub", "item")  # never pay for articles
        and domain.paid_failures < 3  # provider also walled → stop paying
        and page.wall_kind != "robots"  # a robots disallow is final, no paying around it
        and page.wall_kind not in ("login", "paywall", "access_denied", "ban")
        and ceilings_activated  # shipped ceilings are zero; zero is not activation
    )


def yield_score(page) -> float:
    # Laplace-smoothed new-useful-items per fetch of the best known evidence for this page
    src = page.parent_hub or page.domain
    return (src.new_items_total + 1) / (src.fetch_count + 2)
```

The policy chooses managed fetching or residential fetching before spending. It does not try every provider in sequence. Each acquisition has at most two billed retrieval operations, one solver task, and one browser context. A managed API call is one operation. A residential acquisition is one operation with a reserved byte ceiling. HTTP 429 never enters this tier.

Providers:

| Provider | How | Cost accounting |
|---|---|---|
| ZenRows | `GET https://api.zenrows.com/v1/` with the public target `url`, `original_status=true`; `js_render` and `premium_proxy` only for the mode the policy already chose | Reserve integer micro-USD before the call. Settle the confirmed actual micro-USD. Keep provider-specific units in their own column. Do not add those units to proxy bytes or solver charges. The old 1/5/10/25 credit table is not a current tariff. |
| Residential proxy | One explicit curl_cffi proxy URL. The process refuses to run while proxy environment variables are set; `trust_env` is not used because the fetched client page does not document it | Integer bytes, ceiling `MAX_PROXY_BYTES_PER_ACQUISITION` (20971520). Micro-USD is reserved separately from bytes. |
| CAPTCHA provider | One task per acquisition, and only for an authorized supported challenge after managed or residential fetch still shows that challenge | Integer micro-USD plus the provider task id. A ready token is not a successful fetch. |

`mode=auto` sends `original_status=true` and no `js_render` or `premium_proxy`. Its provider-unit ceiling is 25 credits. Those credits are not converted to micro-USD. The response body is kept only when `Zr-Final-Url` is present and still inside the same allowlist entry. A missing `X-Request-Credits` header leaves the reserved micro-USD in place. A ZenRows `session_id` is not a proxy a browser or solver can dial. Every paid fetch writes a `paid_fetches` row before the request (so a crash still counts it) and updates `outcome` after. An unknown submission keeps its worst-case reservation. `snowball status` shows reservations and settlements in micro-USD, and provider-specific units separately. When the day cap is hit, or the ceiling is still the shipped zero, tier 2 is skipped and the page goes to tier 3 with an explicit budget reason. Do not log API keys, proxy credentials, or challenge tokens.

The challenge path is fail-closed. `SNOWBALL_CHALLENGE_FIXTURE` defaults off, and `CAPTCHA_API_KEY` is read from the process environment, not from `config.env`. While the fixture is off, the orchestrator does not load access policy for a solve and does not call 2Captcha or Playwright. A missing browser returns `browser_unavailable` before any DNS lookup. One `solver_tasks` row per acquisition stores the provider task id and a redacted error; the client key, proxy password, cookies, and solver token are not stored. Polling waits at least five seconds, stops at the deadline, and does not create a second task after an ambiguous or unaccepted submission. A ready token is applied only in the same browser context. The fetch stays walled while the challenge is still visible. A token is not access. Camoufox in `tools/forum.py` is the inherited reader, not this solver, and it is not a second access policy. Owned Turnstile and reCAPTCHA fixtures are still required before that reader is removed. No live 2Captcha task, Playwright launch, or browser memory measurement is claimed here.

### 4.6 Tier 3: walled, mark and move on

```sql
UPDATE pages SET status='walled', wall_kind=:kind, strikes=0,
       next_check_at = strftime('%s','now') + CASE WHEN walled_count = 0 THEN 30*86400 ELSE 90*86400 END,
       walled_count = walled_count + 1
 WHERE id=:page_id;
UPDATE domains SET walled_count = walled_count + 1 WHERE domain=:domain;
```

A generic 401, 403, 407, or 451 without a recognized authorized public challenge is an access denial: stop escalation. A recognized supported challenge may itself arrive as HTTP 403; classify the body and the domain policy before deciding whether solving is permitted. HTTP 429 always cools the domain down and refunds the failure strike. It never starts paid identity rotation.

A terminal wall is re-checked after 30 days, then every 90 days, because walls come and go. A domain with `walled_count >= 10` and zero successful fetches is `status=blocked` and its pending pages are dropped from the frontier. No header tricks and no proxy rotation through a ban. The default fetcher has no browser. The optional challenge worker runs only after provider activation, one context per acquisition, and never for login, paywall, robots, explicit denial, or a 429.

### 4.7 Outage versus failure (port of research-queue)

```python
OUTAGE = {"outage"}  # 429, 5xx, connect timeout, reset, DNS SERVFAIL
FAILURE = {"failure"}  # 404, 410, other 4xx, empty DOM, parse error


def on_result(page, domain, kind, retry_after: int | None):
    if kind == "outage":
        n = domain.outage_streak + 1
        pause = retry_after or min(900 * 2 ** (n - 1), 86400)  # 15 min, 30, 60 … cap 24 h
        domain.paused_until = now() + pause
        domain.outage_streak = n
        page.next_attempt_at = domain.paused_until  # attempt refunded: strikes unchanged
    elif kind == "failure":
        page.strikes += 1
        if page.strikes >= 3:
            page.status = "dead"
        else:
            page.next_attempt_at = now() + 3600 * page.strikes
    else:
        domain.outage_streak = 0
```

A rate limit never kills a good domain, and a structural 404 never pauses a whole domain.

### 4.8 grok X route

`grok -p` writes its full answer to stdout and then never exits. The collector launches an owned process, polls the log until the JSON array appears or the poll deadline passes, then stops that process tree. Snowflake decoding (section 7.6) is only a negative filter. The CLI answer is model text, not a documented X Search post body, so it is not ingested as a post. A citation URL is stored as a URL, not as the post text.

### 4.9 Fetch record

Every attempt writes one `fetches` row: `page_id, at, route, http_status, bytes, ms, outcome, pinned_ip`. The M1 "zero duplicate fetches" gate and the "logs show the route" test both read this table.

---

## 5. SQLite schema

One file, `data/snowball.db`. Connection setup for every process:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 268435456;
```

Every multi-statement write runs inside `BEGIN IMMEDIATE … COMMIT` through one helper that retries `SQLITE_BUSY` up to 5 times with 50 ms × 2ⁿ sleeps. Upserts use `INSERT … ON CONFLICT DO UPDATE`, never `INSERT OR REPLACE` (which deletes the row and cascades foreign keys).

### 5.1 DDL

```sql
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('schema_version', '1');

-- Families set the re-check timer, the frontier weight and the drift floor.
CREATE TABLE families (
  name            TEXT PRIMARY KEY,                 -- opportunities | competitors | alpha
  recheck_days    INTEGER NOT NULL CHECK (recheck_days > 0),
  weight          REAL    NOT NULL DEFAULT 1.0,
  relevance_floor REAL    NOT NULL DEFAULT 0.08,    -- TF-IDF cosine against family focus; below = drift, drop
  focus           TEXT    NOT NULL
);

CREATE TABLE topics (
  id              INTEGER PRIMARY KEY,
  key             TEXT NOT NULL UNIQUE,             -- slug(norm title)
  family          TEXT NOT NULL REFERENCES families(name),
  title           TEXT NOT NULL,
  focus           TEXT NOT NULL,
  focus_variants  TEXT NOT NULL DEFAULT '[]',       -- JSON array, merged topics' focus text
  parent_id       INTEGER REFERENCES topics(id),
  depth           INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','dormant','dropped')),
  discovered_via  TEXT NOT NULL CHECK (discovered_via IN ('seed','hub','name','backlink','similar')),
  hops_dry        INTEGER NOT NULL DEFAULT 0,
  run_count       INTEGER NOT NULL DEFAULT 0,
  new_items_total INTEGER NOT NULL DEFAULT 0,
  fetch_count     INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER NOT NULL,
  last_run_at     INTEGER
);
CREATE VIRTUAL TABLE topics_fts USING fts5(title, focus, content='topics', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER topics_ai AFTER INSERT ON topics BEGIN
  INSERT INTO topics_fts(rowid, title, focus) VALUES (new.id, new.title, new.focus); END;
CREATE TRIGGER topics_ad AFTER DELETE ON topics BEGIN
  INSERT INTO topics_fts(topics_fts, rowid, title, focus) VALUES ('delete', old.id, old.title, old.focus); END;
CREATE TRIGGER topics_au AFTER UPDATE OF title, focus ON topics BEGIN
  INSERT INTO topics_fts(topics_fts, rowid, title, focus) VALUES ('delete', old.id, old.title, old.focus);
  INSERT INTO topics_fts(rowid, title, focus) VALUES (new.id, new.title, new.focus); END;
CREATE TABLE topic_terms_df (term TEXT PRIMARY KEY, df INTEGER NOT NULL);

CREATE TABLE queries (
  id              INTEGER PRIMARY KEY,
  query_key       TEXT NOT NULL UNIQUE,             -- sha1(norm|source|window)
  norm            TEXT NOT NULL,
  raw             TEXT NOT NULL,
  source          TEXT NOT NULL CHECK (source IN ('exa','arctic','hn','github','grok')),
  window_key      INTEGER NOT NULL,
  topic_id        INTEGER REFERENCES topics(id),
  name_id         INTEGER REFERENCES names(id),
  discovered_via  TEXT NOT NULL CHECK (discovered_via IN ('seed','hub','name','backlink','similar')),
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
  priority        REAL NOT NULL DEFAULT 0.5,
  scheduled_at    INTEGER NOT NULL,
  ran_at          INTEGER,
  result_count    INTEGER,
  new_pages       INTEGER NOT NULL DEFAULT 0,
  dup_requests    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX queries_norm_source ON queries(norm, source, window_key);
CREATE INDEX queries_pending ON queries(status, priority DESC) WHERE status='pending';

CREATE TABLE domains (
  domain          TEXT PRIMARY KEY,
  status          TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','paused','blocked')),
  junk_count      INTEGER NOT NULL DEFAULT 0,
  walled_count    INTEGER NOT NULL DEFAULT 0,
  outage_streak   INTEGER NOT NULL DEFAULT 0,
  paused_until    INTEGER,
  rate_per_min    INTEGER NOT NULL DEFAULT 6,
  fetch_count     INTEGER NOT NULL DEFAULT 0,
  new_items_total INTEGER NOT NULL DEFAULT 0,
  paid_failures   INTEGER NOT NULL DEFAULT 0,
  robots_txt      TEXT,
  robots_fetched_at INTEGER,
  feedback_score  REAL NOT NULL DEFAULT 0.0        -- optional 👍/👎 adjustments, never a gate
);

CREATE TABLE pages (
  id              INTEGER PRIMARY KEY,
  canon_url       TEXT NOT NULL UNIQUE,
  raw_urls        TEXT NOT NULL DEFAULT '[]',       -- JSON array of URLs seen for this canon
  domain          TEXT NOT NULL REFERENCES domains(domain),
  content_hash    TEXT,
  minhash         BLOB,                             -- 128 x uint32 little-endian = 512 bytes
  alias_of        INTEGER REFERENCES pages(id),
  reprint_of      INTEGER REFERENCES pages(id),
  kind            TEXT CHECK (kind IN ('hub','item','article','junk','unsure')),
  predicted_kind  TEXT,                             -- from the URL/anchor before fetch, used by tier-2 gate
  status          TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','claimed','fetched','classified','scheduled','walled','dead',
                                    'quarantined','alias','reprint','dropped')),
  wall_kind       TEXT CHECK (wall_kind IN ('http','challenge','login','robots','budget')),
  walled_count    INTEGER NOT NULL DEFAULT 0,
  http_status     INTEGER,
  route           TEXT,
  title           TEXT,
  text_len        INTEGER,
  truncated       INTEGER NOT NULL DEFAULT 0,
  raw_path        TEXT,
  fetched_at      INTEGER,
  fetch_count     INTEGER NOT NULL DEFAULT 0,
  next_check_at   INTEGER NOT NULL,
  next_attempt_at INTEGER,
  interval_sec    INTEGER NOT NULL,
  strikes         INTEGER NOT NULL DEFAULT 0,
  depth           INTEGER NOT NULL DEFAULT 0,
  hops_dry        INTEGER NOT NULL DEFAULT 0,
  parent_page_id  INTEGER REFERENCES pages(id),
  topic_id        INTEGER REFERENCES topics(id),
  discovered_via  TEXT NOT NULL CHECK (discovered_via IN ('seed','hub','name','backlink','similar','redirect','feed')),
  novelty         REAL NOT NULL DEFAULT 1.0,
  value           REAL NOT NULL DEFAULT 0.5,
  relevance       REAL,
  new_items_total INTEGER NOT NULL DEFAULT 0,
  noise_count     INTEGER NOT NULL DEFAULT 0,       -- extractions dropped at verify
  quarantined     INTEGER NOT NULL DEFAULT 0,
  quarantine_reasons TEXT,
  dup_requests    INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER NOT NULL
);
CREATE INDEX pages_frontier ON pages(status, next_check_at) WHERE status IN ('queued','scheduled');
CREATE INDEX pages_domain   ON pages(domain, status);
CREATE INDEX pages_hash     ON pages(content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX pages_parent   ON pages(parent_page_id);

CREATE TABLE minhash_bands (band INTEGER NOT NULL, bucket TEXT NOT NULL, page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                            PRIMARY KEY (band, bucket, page_id));

CREATE TABLE page_links (
  from_page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  to_canon_url    TEXT NOT NULL,
  anchor          TEXT,
  position        INTEGER NOT NULL,
  PRIMARY KEY (from_page_id, to_canon_url)
);
CREATE INDEX page_links_to ON page_links(to_canon_url);      -- backlink discovery: who links to this hub

CREATE TABLE hubs (
  page_id         INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
  item_count      INTEGER NOT NULL DEFAULT 0,
  rows_added_total INTEGER NOT NULL DEFAULT 0,
  last_diff_at    INTEGER,
  feed_url        TEXT,
  link_template   TEXT                              -- dominant path template, e.g. /grants/{slug}
);
CREATE TABLE hub_rows (
  hub_page_id     INTEGER NOT NULL REFERENCES hubs(page_id) ON DELETE CASCADE,
  row_hash        TEXT NOT NULL,                    -- sha1(canon link | norm anchor)
  to_canon_url    TEXT NOT NULL,
  anchor          TEXT,
  first_seen_at   INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL,
  entity_key      TEXT REFERENCES entities(key),
  PRIMARY KEY (hub_page_id, row_hash)
);

CREATE TABLE fetches (
  id              INTEGER PRIMARY KEY,
  page_id         INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  at              INTEGER NOT NULL,
  route           TEXT NOT NULL CHECK (route IN ('direct','exa_contents','zenrows','resi_proxy','arctic','hn','github','grok','feed')),
  http_status     INTEGER,
  bytes           INTEGER,
  ms              INTEGER,
  outcome         TEXT NOT NULL CHECK (outcome IN ('ok','walled','outage','failure','ssrf_blocked','oversize')),
  pinned_ip       TEXT,
  due             INTEGER NOT NULL                  -- 1 if the page was due or new; 0 would be a repeat (M1 gate)
);
CREATE INDEX fetches_page_at ON fetches(page_id, at);

CREATE TABLE paid_fetches (
  id              INTEGER PRIMARY KEY,
  page_id         INTEGER NOT NULL REFERENCES pages(id),
  provider        TEXT NOT NULL CHECK (provider IN ('zenrows','resi_proxy')),
  cost_units      INTEGER NOT NULL,
  at              INTEGER NOT NULL,
  outcome         TEXT                               -- NULL until the request returns (crash still counts)
);
CREATE INDEX paid_fetches_day ON paid_fetches(at);

CREATE TABLE names (                                 -- unknown entity names awaiting exactly one search
  id              INTEGER PRIMARY KEY,
  norm_name       TEXT NOT NULL UNIQUE,
  raw             TEXT NOT NULL,
  first_page_id   INTEGER NOT NULL REFERENCES pages(id),
  topic_id        INTEGER REFERENCES topics(id),
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','searched','resolved','dropped')),
  searched_at     INTEGER,
  entity_key      TEXT REFERENCES entities(key),
  created_at      INTEGER NOT NULL
);

CREATE TABLE entities (
  key             TEXT PRIMARY KEY,                  -- slug(name)|slug(org)|year
  name            TEXT NOT NULL,
  norm_name       TEXT NOT NULL,
  org             TEXT,
  norm_org        TEXT,
  year            TEXT,
  aliases         TEXT NOT NULL DEFAULT '[]',        -- JSON array, sorted set
  kind            TEXT NOT NULL CHECK (kind IN ('grant','hackathon','fellowship','scholarship','credits','accelerator','competitor','tool','model','other')),
  family          TEXT NOT NULL REFERENCES families(name),
  amount          REAL,
  currency        TEXT,
  deadline        TEXT,                              -- ISO date
  eligibility     TEXT,
  location        TEXT,
  link            TEXT,
  status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','known','changed','expired')),
  relevance       REAL NOT NULL DEFAULT 0.5,
  urgency         REAL NOT NULL DEFAULT 0.0,
  evidence_quote  TEXT NOT NULL,
  evidence_page_id INTEGER NOT NULL REFERENCES pages(id),
  first_seen_at   INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL,
  last_changed_at INTEGER,
  reported_at     INTEGER,
  alerted_at      INTEGER,
  wiki_path       TEXT
);
CREATE INDEX entities_org_year ON entities(norm_org, year);
CREATE INDEX entities_link     ON entities(link);
CREATE INDEX entities_unreported ON entities(reported_at) WHERE reported_at IS NULL;
CREATE INDEX entities_deadline ON entities(deadline) WHERE deadline IS NOT NULL;

CREATE TABLE entity_sources (
  entity_key      TEXT NOT NULL REFERENCES entities(key) ON DELETE CASCADE,
  page_id         INTEGER NOT NULL REFERENCES pages(id),
  seen_at         INTEGER NOT NULL,
  PRIMARY KEY (entity_key, page_id)
);

CREATE TABLE entity_changes (
  id              INTEGER PRIMARY KEY,
  entity_key      TEXT NOT NULL REFERENCES entities(key) ON DELETE CASCADE,
  field           TEXT NOT NULL,
  old             TEXT,
  new             TEXT NOT NULL,
  seen_at         INTEGER NOT NULL,
  page_id         INTEGER NOT NULL REFERENCES pages(id),
  evidence_quote  TEXT NOT NULL,
  reported_at     INTEGER
);
CREATE INDEX entity_changes_unreported ON entity_changes(reported_at) WHERE reported_at IS NULL;

CREATE TABLE claims (
  hash            TEXT PRIMARY KEY,
  text            TEXT NOT NULL,
  norm_text       TEXT NOT NULL,
  n_sources       INTEGER NOT NULL DEFAULT 1,
  topic_id        INTEGER REFERENCES topics(id),
  entity_key      TEXT REFERENCES entities(key) ON DELETE SET NULL,
  first_seen_at   INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL
);
CREATE VIRTUAL TABLE claims_fts USING fts5(norm_text, content='claims', content_rowid='rowid', tokenize='unicode61');
CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN INSERT INTO claims_fts(rowid, norm_text) VALUES (new.rowid, new.norm_text); END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN INSERT INTO claims_fts(claims_fts, rowid, norm_text) VALUES ('delete', old.rowid, old.norm_text); END;
CREATE TABLE claim_sources (
  claim_hash      TEXT NOT NULL REFERENCES claims(hash) ON DELETE CASCADE,
  page_id         INTEGER NOT NULL REFERENCES pages(id),
  quote_start     INTEGER NOT NULL,
  quote_end       INTEGER NOT NULL,
  PRIMARY KEY (claim_hash, page_id)
);
CREATE TABLE claim_variants (claim_hash TEXT NOT NULL REFERENCES claims(hash) ON DELETE CASCADE, text TEXT NOT NULL, PRIMARY KEY (claim_hash, text));

CREATE TABLE tasks (                                 -- unified work queue, section 6
  id              INTEGER PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('fetch','search','extract','verify','compile','deepen','digest')),
  payload         TEXT NOT NULL,                     -- JSON
  priority        REAL NOT NULL DEFAULT 0.5,
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','done','failed','paused')),
  worker_id       TEXT,
  occurrence_seq  INTEGER NOT NULL DEFAULT 0,        -- monotonic claim counter (deer-flow)
  claimed_at      INTEGER,
  heartbeat_at    INTEGER,
  attempts        INTEGER NOT NULL DEFAULT 0,
  max_attempts    INTEGER NOT NULL DEFAULT 3,
  next_attempt_at INTEGER NOT NULL,
  outage_pauses   INTEGER NOT NULL DEFAULT 0,
  wave_id         INTEGER REFERENCES waves(id),
  result          TEXT,
  error           TEXT,
  created_at      INTEGER NOT NULL
);
CREATE INDEX tasks_pending ON tasks(kind, status, next_attempt_at, priority DESC) WHERE status='pending';
CREATE INDEX tasks_claimed ON tasks(heartbeat_at) WHERE status='claimed';

CREATE TABLE waves (
  id              INTEGER PRIMARY KEY,
  started_at      INTEGER NOT NULL,
  ended_at        INTEGER,
  status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','aborted','budget_cut')),
  fetches         INTEGER NOT NULL DEFAULT 0,
  exa_calls       INTEGER NOT NULL DEFAULT 0,
  tokens_in       INTEGER NOT NULL DEFAULT 0,
  tokens_out      INTEGER NOT NULL DEFAULT 0,
  new_entities    INTEGER NOT NULL DEFAULT 0,
  changed_entities INTEGER NOT NULL DEFAULT 0,
  new_claims      INTEGER NOT NULL DEFAULT 0,
  error           TEXT
);

CREATE TABLE budgets (
  day             TEXT PRIMARY KEY,                  -- YYYY-MM-DD, IST
  exa_calls       INTEGER NOT NULL DEFAULT 0,
  llm_tokens_in   INTEGER NOT NULL DEFAULT 0,
  llm_tokens_out  INTEGER NOT NULL DEFAULT 0,
  fetches         INTEGER NOT NULL DEFAULT 0,
  paid_fetches    INTEGER NOT NULL DEFAULT 0,
  paid_cost_units INTEGER NOT NULL DEFAULT 0
);

-- Wiki index: rebuildable from wiki/*.md by `snowball reindex` (invariant I7)
CREATE TABLE wiki_pages (
  path            TEXT PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('entity','topic','hub','index')),
  ref_key         TEXT,                              -- entities.key or topics.key or pages.canon_url
  title           TEXT NOT NULL,
  body_hash       TEXT NOT NULL,
  frontmatter     TEXT NOT NULL,                     -- JSON
  compiled_at     INTEGER NOT NULL,
  dirty           INTEGER NOT NULL DEFAULT 0,
  stale           INTEGER NOT NULL DEFAULT 0,
  stale_reason    TEXT,
  stale_origin    TEXT,                              -- path of the page whose fact change marked us
  stale_marked_at INTEGER
);
CREATE INDEX wiki_pages_dirty ON wiki_pages(dirty, stale);
CREATE TABLE wiki_links (from_path TEXT NOT NULL, to_path TEXT NOT NULL, PRIMARY KEY (from_path, to_path));
CREATE INDEX wiki_links_to ON wiki_links(to_path);
CREATE VIRTUAL TABLE wiki_fts USING fts5(title, body, path UNINDEXED, tokenize='unicode61 remove_diacritics 2');

CREATE TABLE digest_items (
  id              INTEGER PRIMARY KEY,
  day             TEXT NOT NULL,
  ref_kind        TEXT NOT NULL CHECK (ref_kind IN ('entity','change')),
  ref_id          TEXT NOT NULL,                     -- entities.key or entity_changes.id
  rank            INTEGER NOT NULL,
  reason          TEXT NOT NULL,
  sent_at         INTEGER,
  message_id      TEXT,
  UNIQUE (ref_kind, ref_id)                          -- an item is reported exactly once, ever
);
CREATE TABLE feedback (digest_item_id INTEGER NOT NULL REFERENCES digest_items(id), reaction TEXT NOT NULL CHECK (reaction IN ('up','down')), at INTEGER NOT NULL);
```

### 5.2 Frontier state machine (`pages.status`)

```
                 ┌──────────────── alias (content_hash seen elsewhere) ────────────┐
                 │                                                                 │
 queued ──claim──► claimed ──fetch ok──► fetched ──hash/minhash──► classified ──► scheduled ◄──┐
   ▲                 │                     │                            │            │          │
   │                 │ outage              │ reprint ──► reprint         │ junk ──► dropped      │ due:
   │                 ├─ refund, retry at   │ injection ──► quarantined   │ hub/item/article ─────┘ re-fetch,
   │                 │  domain.paused_until│                             │                        diff only
   │                 │ failure x3 ──► dead │                             │
   │                 │ wall ──► walled ────┼──── 30 d / 90 d re-check ───┘
   └── seed / hub row / name search / backlink / find-similar inserts
```

| From | To | Trigger | Side effects |
|---|---|---|---|
| (none) | queued | hub row, name search result, backlink, find-similar, seed | `novelty`, `value`, `next_check_at = now`, `interval_sec = family.recheck_days*86400` |
| queued / scheduled | claimed | `scheduler.pop()` | `tasks` row with `occurrence_seq` |
| claimed | fetched | tier 1/1b/2 returned `ok` | `fetches` row, `raw_path`, `content_hash`, `minhash`, `fetch_count += 1` |
| claimed | walled | `classify_response == walled` after waterfall | `wall_kind`, `walled_count`, 30/90-day `next_check_at` |
| claimed | queued | outage | `next_attempt_at = domain.paused_until`, strikes unchanged |
| claimed | dead | third failure | removed from frontier; `page_links` kept |
| fetched | alias / reprint | hash or MinHash match | no classification |
| fetched | quarantined | injection scan hit | no links, no names, no topics emitted |
| fetched | classified | heuristics or LLM | `kind` |
| classified | dropped | `kind = junk` or `relevance < family.relevance_floor` | `domains.junk_count += 1`; 5 junk pages → domain blocked |
| classified | scheduled | hub / item / article processed | `next_check_at = now + interval_sec` |
| scheduled | claimed | `next_check_at <= now` | re-check, diff only |

### 5.3 Frontier score (the pop query)

```sql
-- novelty: unfetched pages decay with domain saturation; due re-checks use the hub's change rate
-- value:   family weight x kind prior x parent yield (Laplace)
-- due:     0 until due, then 1 + min(1, overdue_days)
WITH cand AS (
  SELECT p.id, p.domain,
         CASE WHEN p.fetch_count = 0
              THEN MAX(0.3, 1.0 - 0.02 * d.fetch_count) * (CASE WHEN d.junk_count > 0 THEN 0.5 ELSE 1.0 END)
              ELSE (COALESCE(h.rows_added_total, 0) + 1.0) / (p.fetch_count + 2.0) END                      AS novelty,
         f.weight
           * CASE COALESCE(p.kind, p.predicted_kind, 'unsure') WHEN 'hub' THEN 1.0 WHEN 'item' THEN 0.9
                                                                WHEN 'article' THEN 0.5 ELSE 0.6 END
           * (COALESCE(pp.new_items_total, 0) + 1.0) / (COALESCE(pp.fetch_count, 0) + 2.0)
           * (1.0 + 0.2 * d.feedback_score)                                                                   AS value,
         1.0 + MIN(1.0, (strftime('%s','now') - p.next_check_at) / 86400.0)                                  AS due
    FROM pages p
    JOIN domains d ON d.domain = p.domain
    LEFT JOIN topics t ON t.id = p.topic_id
    JOIN families f ON f.name = COALESCE(t.family, 'opportunities')
    LEFT JOIN hubs h ON h.page_id = p.id
    LEFT JOIN pages pp ON pp.id = p.parent_page_id
   WHERE p.status IN ('queued','scheduled')
     AND p.next_check_at <= strftime('%s','now')
     AND (p.next_attempt_at IS NULL OR p.next_attempt_at <= strftime('%s','now'))
     AND d.status = 'ok'
     AND (d.paused_until IS NULL OR d.paused_until <= strftime('%s','now'))
)
SELECT id, domain, novelty * value * due AS score FROM cand ORDER BY score DESC LIMIT :n;
```

`novelty` and `value` are also materialised into `pages.novelty` and `pages.value` after each fetch so `snowball status` can show the distribution without re-running the join.

### 5.4 Backups and integrity

- Historical: nightly `VACUUM INTO 'data/backup/snowball-<date>.db'`, keep 7. That dated DB-only file is no longer written.
- Current retention writes the section 5.5 bundle. It does not write a new `snowball-YYYY-MM-DD.db`. It still deletes surplus existing `snowball-*.db` files down to `--keep` (default 7). Those old files are not the v4 rollback bundle.
- `PRAGMA quick_check` weekly; a failure stops the supervisor and posts to Discord.
- `wiki/` is a git repository committed after each wave (`git add wiki digests && git commit -qm "wave <id>: +N entities, M changes"`), so the truth layer has history independent of the DB.

The dated `VACUUM INTO` file was a database snapshot. It did not preserve `raw/` or human-edited wiki pages. The operator rollback bundle does (section 5.5).

### 5.5 Schema version 4

The amendment list called this "section 5.2". The existing section 5.2 is the frontier state machine, so the schema contract is recorded here.

Current schema version is 4. `python snowball.py migrate --db PATH --bundle-root DIR` execs `tools/migration_cli.py`, which calls `Store.migrate` and prints JSON. `--json` only changes compact versus indented JSON. It does not call `status` or `serve`. Exit 2 prints only `{"error":"migration_blocked","kind","detail"}` and does not advance `schema_version`. `status`, `serve`, and `goal show` refuse an old schema and do not migrate.

The database parent directory is named `data`. The storage root is the parent of `data`. Stop other Snowball processes first.

A missing path does not call `migration.migrate`. `Store.open` calls `init_schema`, which calls `migration.initialize` and creates a fresh v4 database. The JSON is `schema_version` 4, `changed` true, `bundle` null, and `fresh` true.

An existing v2 or v3 file calls `migration.migrate`. The JSON is `schema_version` 4, `changed` true, `bundle` set to the directory, and `quick_check` `"ok"`. `create_bundle` returns only after `database.db`, `manifest.json`, and `files/` verify. That directory name is `goal-v{2|3}-{unix}-{8 hex}`. The schema transaction starts only after `create_bundle` returns. The live file becomes version 4 only after `foreign_key_check` and `quick_check` pass in the transaction. Foreign keys are off only for the rebuild transaction and are turned back on afterwards. `migration.migrate` does not upgrade a missing file.

Both failures below print only `{"error":"migration_blocked","kind","detail"}` and exit 2. That JSON does not include the bundle path.

A failure inside `create_bundle`, before it returns, deletes the incomplete directory. Its absence means the bundle was removed. It does not mean migrate never started.

A failure after `create_bundle` returns, during the schema transaction, rolls that transaction back. `schema_version` stays unchanged. The verified `goal-v{2|3}-{unix}-{8 hex}` directory stays under the bundle root. The operator finds it there by that name, because the error JSON does not print the path. Keep it. Do not start `serve` on a migrate that did not finish.

Malformed `schema_version`, an absent version, schema 0, and any version other than 2, 3, or 4 raise `MigrationRequired` before a bundle and before table creation. A v4 file missing `goals`, `source_snapshots`, or `observations` raises `MigrationRequired` and does not create those tables. A second run on a complete v4 database returns `schema_version` 4, `changed` false, and `bundle` null, with no `quick_check` field and no second bundle.

Rollback is `python snowball.py retention --restore data/backup/goal-v3-UNIX-HEX --root restore-root --db restore-root/data/highered.db`. Replace the bundle directory with the `goal-v*` directory left under the backup root. `--restore` is that verified bundle, not a dated SQLite snapshot. `--db` must sit inside a new `--root` that does not exist yet. That calls `restore_bundle(bundle, target_db, target_root)`. Restore checks checksums, stages the tree, and publishes it by rename. It refuses an existing root, a symlink or Windows reparse point, and any path that escapes the new root or the original storage root. Failure leaves the target absent and the live corpus unchanged. Retention does not write a new `snowball-YYYY-MM-DD.db`. `_trim_backups` still deletes surplus pre-existing `snowball-*.db` files down to `--keep` (default 7). Those old files are not the v4 rollback bundle.

`goal new` on a missing path creates a fresh v4 database. On an existing v2 or v3 file it refuses and does not upgrade that file.

Migration adds the goal tables in `tools/goal_schema.py`: `goals`, `goal_contracts`, `profile_fields`, `interview_sessions`, `interview_turns`, `execution_grants`, `strategies`, `goal_events`, `frontier_decisions`, `resource_ledger`, `resource_reservations`, `resource_operations`, `programs`, `source_snapshots`, `observations`, `funding_packages`, `constraint_evaluations`, `contradictions`, `fx_rates`, `goal_topics`, `goal_pages`, `goal_queries`, `goal_programs`, `goal_node_identities`, `domain_slots`, `model_calls`, `console_tokens`, `console_sessions`, `console_rates`, `goal_runtime`, `program_field_cooldowns`, and `wiki_regions`. Existing `entities` and `claims` gain provenance defaulting to `legacy_v3`. Those rows stay leads. Nothing in this migration deletes evidence.

`model_calls` records requested and reported model, transport, and `usage_kind` of actual, estimated, or unknown. Historical grok spend stays unknown usage. It is not rewritten as Claude.

---

## 6. Multi-subagent orchestration topology

### 6.1 Shape

One Python process (`supervisor.py`, asyncio) owns three worker pools. "Subagent" here means a pool worker, not a separate LLM agent with its own goals. Workers are stateless; all state is in `tasks` and the lock tables, so a kill at any instant loses at most one in-flight task, which the reaper re-queues.

| Pool | Concurrency | Bound by | Work |
|---|---|---|---|
| Collector | 6 | network, per-domain token buckets | `fetch`, `search` tasks: waterfall, safety gate, hash, raw write |
| Extractor | 1 on Ornith (single T4 slot, `ornith.lock`), up to 2 on Qwen 27B | the LLM | `extract` tasks: classify-if-unsure, BM25 filter, JSON extraction |
| Verifier | 4 | CPU | `verify` tasks: quote check, number check, dead-link HEAD, lock engine, writer, dirty marking |
| Compiler | 1, runs between waves | CPU + 1 LLM call per Summary merge | `compile` tasks: incremental Markdown, FTS5 |

Model routing: Ornith 35B for extraction (strongest at JSON on long context, one slot), Qwen 27B for classification-when-unsure and Summary merges (parallel), Gemma shim only for public, small (< 40k chars) classification prompts when both others are busy. All calls stream (a quiet non-streamed request got a Cloudflare 524 through the tunnel on 2026-10-02).

### 6.2 Waves (DAG)

A wave is one scheduling round. The supervisor builds it, fans out, joins, compiles, commits.

```
wave:
  pop up to 24 due pages + up to 12 pending searches (score order, max 4 per domain)
  ├─ Collector tasks (fan-out 6)            ─┐
  │    search → result URLs → page lock → queued pages (same wave if budget allows)
  │    fetch  → ok → classify heuristics → hub: link diff (no LLM) | item/article: extract task
  ├─ Extractor tasks (depend on their fetch) ─┤  each carries sibling fencing (6.6)
  ├─ Verifier tasks  (depend on their extract) ┘  write + yield + dirty marks
  └─ Compiler (after all verifiers)            incremental pages, FTS5, git commit
  record waves row; sleep WAVE_PAUSE_SEC (default 120) or 1800 if the frontier was dry
```

Dependencies are expressed as `tasks.payload.after = [task_id…]`; a task is pop-able only when every `after` id is `done`. A failed upstream marks downstream `failed` with `error = 'upstream'`.

### 6.3 Claiming with monotonic `occurrence_seq` (port of deer-flow)

```sql
BEGIN IMMEDIATE;
UPDATE tasks
   SET status='claimed', worker_id=:w, occurrence_seq=occurrence_seq+1,
       claimed_at=strftime('%s','now'), heartbeat_at=strftime('%s','now'), attempts=attempts+1
 WHERE id = (SELECT id FROM tasks
              WHERE kind=:kind AND status='pending' AND next_attempt_at <= strftime('%s','now')
              ORDER BY priority DESC, id LIMIT 1)
RETURNING id, payload, occurrence_seq;
COMMIT;
```

Workers heartbeat every 60 s (`UPDATE tasks SET heartbeat_at=now WHERE id=? AND occurrence_seq=?`). The reaper runs every 120 s:

```sql
UPDATE tasks SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
       error = 'reaped: heartbeat stale', worker_id = NULL,
       next_attempt_at = strftime('%s','now') + 60
 WHERE status='claimed' AND heartbeat_at < strftime('%s','now') - 600;
```

A result commit is `UPDATE tasks SET status='done', result=? WHERE id=? AND occurrence_seq=?`. If a reaped worker wakes late and tries to commit, the sequence no longer matches, zero rows update, and it discards its work. No wall-clock comparisons anywhere in the claim path.

Outage pauses increment `outage_pauses` and push `next_attempt_at` without touching `attempts` (the refund rule from section 4.7).

### 6.4 Guards checked before every wave

| Guard | Check | Action when tripped |
|---|---|---|
| STOP file | `STOP` exists | sleep 60, re-check |
| Exa reserve | `exa_budget.status().use_exa` false (≤ 3 live keys) | wave runs with `search` tasks skipped and tier 1b disabled; fetch continues |
| LLM tokens | `budgets.llm_tokens_in + out >= LLM_TOKENS_PER_DAY` (default 2,000,000) | no `extract` tasks; collectors and hub diffs continue; `compile` runs without Summary merges |
| Paid fetches | `budgets.paid_fetches >= MAX_PAID_FETCHES_PER_DAY` | tier 2 disabled until midnight IST |
| Memory | process RSS > 2.2 GB (`MemoryMax=3G`) | collector concurrency drops to 2 until RSS < 1.6 GB; raw text buffers flushed |
| Disk | free < 5 GB on `/` | retention runs immediately; if still < 5 GB, supervisor pauses and alerts |
| Tunnel | `GET 127.0.0.1:8812/v1/models` fails | extractor pool paused, collectors continue; alert after 15 min |

### 6.5 Token budget middleware (port of deer-flow)

Each `extract` and `compile` task has a token budget (`EXTRACT_TOKEN_BUDGET` 12,000 in + 4,000 out; `SUMMARY_MERGE_BUDGET` 8,000 + 2,000). The adapter's `MIN_TOKENS=16384` governs the ceiling; the middleware governs what we ask for:

- At **80%** of the input budget, the BM25 filter's chunk limit drops from 25 to 10 and the prompt switches to the "synthesize-only" template: extract rows from the chunks given, do not list further names.
- At **100%**, no further LLM call is made for that task; whatever JSON has been validated is committed and the task is `done` with `result.partial = true`. A partial result is still a result (a deer-flow fallback report, not a crash).
- The model never sees tool definitions because it has no tools (I3). The "tool stripping" idea maps to stripping the **name-extraction** instruction, which is the only thing that can widen the crawl.

### 6.6 Sibling-section fencing (port of gpt-researcher PR #2158)

When two or more `extract` tasks in one wave share a `topic_id`, each prompt carries:

```
Other pages being processed for this topic in this round (do not re-extract entities whose names appear here):
- <title of sibling page 1> (<domain>)
- <title of sibling page 2> (<domain>)
Known entities for this topic (do not re-emit, they are already recorded): <up to 40 entity names>
```

The entity lock would catch the duplicates anyway; the fence saves the tokens.

### 6.7 Dry-branch pruning

There is no fixed depth limit. A page whose processing yields zero new entities, zero new claims and zero new hub rows increments `hops_dry` on its topic and on itself; a child inherits `hops_dry` from its parent at insertion. At `hops_dry >= 3` the branch is cut: the page is `scheduled` with `interval_sec = 90d` and its children are not queued. A paying hop resets `hops_dry` to 0.

### 6.8 Frontier refill when dry

When the pop returns fewer than 4 due pages, in order: (1) Exa `findSimilar` on the 5 highest-yield hubs (one call each, through the rotator, only if the reserve allows); (2) backlink discovery: `SELECT from_page_id FROM page_links WHERE to_canon_url IN (<top hubs>)` for pages not yet classified as hubs; (3) the name snowball: pending `names` ordered by the yield of the page that produced them. If all three return nothing, sleep 1800 s.

### 6.9 Supervisor loop

Historical lead-crawler loop. The harness supervisor is section 6.10. This listing does not admit work and it does not enable `snowball-loop.service`.

```python
async def main():
    while True:
        if STOP.exists():
            await asyncio.sleep(60)
            continue
        guards = check_guards()
        if guards.pause:
            await asyncio.sleep(guards.sleep_for)
            continue
        wave = open_wave()
        pops = scheduler.pop(pages=24, searches=0 if not guards.exa else 12, max_per_domain=4)
        if len(pops.pages) < 4:
            refill_frontier(guards)
        enqueue_tasks(wave, pops)
        await asyncio.gather(collectors.run(wave), extractors.run(wave, llm=guards.llm), verifiers.run(wave))
        compiler.run_incremental(wave, llm=guards.llm, time_cap_sec=600)
        git_commit(wave)
        close_wave(wave)
        await asyncio.sleep(1800 if pops.dry else WAVE_PAUSE_SEC)
```

### 6.10 Goal gate and outcomes

`SnowballOrchestrator.supervise(max_topics=1, interval_sec=60, slug_filter=None)` calls `goal_runtime.supervise(cfg, topics_file, dry, **kwargs)`. `dry` is the orchestrator dry flag. `kwargs` carries `max_topics`, `interval_sec`, and `slug_filter`, and `db_path` only when `store.path` is set. The call passes no `stop_file` and no other keyword. `python snowball.py run`, `run-once`, and `supervise` accept `--topic` (a slug), `--max-topics`, `--dry-run` or `--dryrun`, `--mode` `auto` or `gemini-bulk`, `--topics`, `--db`, and `--json`. `seed` accepts `--db`, `--json`, `--topics`, and `--dry-run` or `--dryrun`. It does not accept `--topic`, `--max-topics`, or `--mode`. `status` accepts `--db` and `--json` and does not take `--topic`. There is no `--interval` flag. The worker flag `--supervise` forwards `--max-topics` and `--topic` as `slug_filter`. `interval_sec` stays the method default 60. A read of an existing database does not migrate. Each cycle calls `run`, then reads status. `run` checks `runnable_goal` before seeding, before `select_topics`, and before a manual `--topic` override. The `CAST(strftime('%s','now') AS INTEGER)` comparison inside `select_topics` stays byte for byte. `--topic` may override a topic cooldown. It does not override the goal, the grant, privacy, or page-access checks.

`status` and `run-once --dry-run` do not migrate and do not write the selected database. `--dry-run` copies the database in memory and makes no network or model calls. With no runnable goal the outcome is `no-runnable-goal` plus a reason: `no_goal`, `not_running`, `no_grant`, `grant_expired`, `grant_exhausted`, `goal_paused`, `research_ready`, `mode_mismatch`, or `no_contract`.

Idle sleeps when the supervisor is actually running: `no-runnable-goal` 300 seconds, `goal-paused` 60 seconds, `research_ready` 1800 seconds, `monitoring` until the next due snapshot and at most 1800 seconds. A `STOP` file or `KeyboardInterrupt` returns without writing the goal row. On Oracle the loop unit uses `Restart=always`, so a `STOP` file does not keep a managed process down. Leave `snowball-loop.service` disabled until you approve the actual goal, the actual grant, and a bounded live smoke. This amendment does not enable or start that unit.

Frontier admission is the planner path in section 1.6. `schedule` does not call `admit_node`. A late result may keep raw bytes and conservative usage. It cannot qualify the current goal when `binding_valid` fails for the contract version or epoch. A grant file stores `exploration_share` as an integer or a canonical decimal string. JSON `0.3` fails `goal grant` before reserve. The orchestrator leaves the stored grant unchanged. Estimated OMP usage is not a hard billing cap. `usage_mode` `hard` is rejected.

`status` reports the goal slug and state, contract version, grant expiry, headroom per counter, substate, reason, and review and private-endpoint availability by role and model id, plus the existing frontier counters. Token figures are labelled actual, estimated, or unknown. Historical grok rows stay unknown. Estimated OMP usage is not a hard billing cap. `usage_mode` `hard` is rejected.

`completion_status` returns only `partial_coverage`, `verifier_unavailable`, `no_match`, and `research_ready`. Those names come from the spec 7.5 outcome table. Unknown private criteria, including a declined private-evaluation consent, stay unknown and keep a package out of `research_ready`. `partial_coverage` means the wave finished while some shortlisted programs still have unknown critical fields and no eligible node remains for them today. An empty queue or an exhausted grant is that outcome, not readiness. `verifier_unavailable` means the reviewer is unavailable. The substate is not "pending review". Observations from that outage stay `review_pending` and are not qualified. `no_match` means every candidate has at least one hard constraint unmet at `reviewed_kept`. Zero matches means no matches among the candidates checked. `detail` holds reasons and package outcomes. Qualification does not record admission or a funding award, and it does not start monitoring.

The last strategy outcome while `running` is a separate list: `progressing`, `exhausted`, `partial_coverage`, `outage`, `verifier_unavailable`, `empty_frontier`, `blocked`, `no_match`, `user_stopped`, and `stalled`. `stalled` is an unchanged work and coverage fingerprint. The model does not judge novelty. `research_ready` and `monitoring` in the spec outcome table are goal states. Monitoring starts only from an explicit monitoring grant and a transition to `monitoring`. The worker then refreshes due snapshots for shortlisted programs and otherwise idles.

---

## 7. Safety, egress and anti-hallucination pipeline

### 7.1 SSRF guard with DNS pinning (port of hyperresearch `safe_http.py`)

```python
import ipaddress, socket

DENY = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT incl. Tailscale 100.x: no tailnet peers
        "127.0.0.0/8",
        "169.254.0.0/16",  # loopback, link-local incl. cloud metadata 169.254.169.254
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
]
ALLOWED_PORTS = {80, 443}


class SSRFBlocked(Exception): ...


def resolve_and_pin(host: str, port: int) -> str:
    if port not in ALLOWED_PORTS:
        raise SSRFBlocked(f"port {port}")
    try:
        ipaddress.ip_address(host)  # a bare IP literal is never allowed
        raise SSRFBlocked("ip literal")
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    ips = {ipaddress.ip_address(i[4][0]) for i in infos}
    if not ips:
        raise SSRFBlocked("no A/AAAA")
    for ip in ips:  # ALL answers must be public: a mixed answer is a rebinding attempt
        if any(ip in n for n in DENY):
            raise SSRFBlocked(f"{ip} denied")
    return str(sorted(ips, key=lambda i: (i.version, str(i)))[0])
```

The pinned IP is handed to curl via `CURLOPT_RESOLVE` (section 4.3), so the connection goes to the address we checked and not to whatever a second DNS lookup returns (TOCTOU rebinding). Redirects are followed manually: for each `3xx`, build the absolute URL, run `canonical_url`, `resolve_and_pin` again, drop `Authorization` and `Cookie` if the origin changed, stop after 5 hops (`RecursionError`-style hard stop recorded as `failure`). API routes (Arctic Shift, Algolia, GitHub, rotator) have a fixed host allowlist and skip the resolver only because their hosts are constants.

This pin applies to local direct fetches. A remote managed provider resolves the target itself. Do not treat local pinning as coverage for that hop. Paid remote targets are exact-origin and path allowlisted, carry no operator credentials, and stay disabled when the backend cannot show a private-destination and redirect restriction. The browser worker, when activated, has no direct egress. It connects only to an enforcing gateway that re-checks this deny list, allows target ports 80 and 443 only, and re-validates redirect hops. An upstream proxy that overrides destination enforcement is not eligible. URL checks inside the page are not a substitute for that gateway.

Every block writes `fetches.outcome = 'ssrf_blocked'`. A domain with 3 blocks is `blocked`.

### 7.2 Injection quarantine

The existing `quarantine.py` patterns (ignore-previous, fake-system, fake-access, run-this-command, curl-pipe-shell, secret-path, secret-request, base64-blob, delimiter-spoof) run over the extracted text of every fetched page before classification. Additions:

- Every page is wrapped for the model as `<untrusted_source id="<16 hex nonce>">…</untrusted_source>`; the nonce is per call, and a page body containing the literal `untrusted_source` is a `delimiter-spoof` hit (hyperresearch PR #51).
- A quarantined page: `status = quarantined`, emits **no** links, names, topics or claims. Its text is kept in `raw/` for 14 days for review and the path is appended to `QUARANTINE.md`.
- Model output is scanned with the same secret regexes used in `tests/injection_test.sh` (`sk-qwen-`, `AQ\.[A-Za-z0-9_-]{20}`, base64 ≥ 120 chars). A hit drops the whole output and marks the page `noise_count += 3`.

### 7.3 Pure-Python BM25 context filter (port of gpt-researcher PR #2161 / #2162)

GPT-researcher's 28-run benchmark found BM25 context precision 0.51 against embeddings' 0.46, at 0.02 s versus 1.04 s and 0 MB of Torch. Snowball uses the same relative-threshold pruning before every extraction call:

```python
class BM25:
    def __init__(self, docs: list[list[str]], k1=1.5, b=0.75):
        self.k1, self.b, self.docs = k1, b, docs
        self.avgdl = sum(map(len, docs)) / max(1, len(docs))
        self.df = Counter(t for d in docs for t in set(d))
        self.N = len(docs)

    def idf(self, t):
        return math.log(1 + (self.N - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))

    def score(self, q: list[str], i: int) -> float:
        d, tf = self.docs[i], Counter(self.docs[i])
        return sum(
            self.idf(t) * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * (1 - self.b + self.b * len(d) / self.avgdl))
            for t in q
            if t in tf
        )


def filter_chunks(text: str, query: str, chunk_chars=800, keep_ratio=0.5, top=25) -> list[str]:
    chunks = [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]
    toks = [re.sub(r"[^\w\s]", " ", c.casefold()).split() for c in chunks]
    bm = BM25(toks)
    q = re.sub(r"[^\w\s]", " ", query.casefold()).split()
    scored = sorted(((bm.score(q, i), i) for i in range(len(chunks))), reverse=True)
    if not scored or scored[0][0] <= 0:
        return chunks[:top]
    mx = scored[0][0]
    keep = sorted(i for s, i in scored[:top] if s >= keep_ratio * mx)
    return [chunks[i] for i in keep]  # original order preserved
```

The query is the topic's `focus` plus the family's `focus` plus any entity names already attached to the page. The same `BM25` class with the FTS5 `bm25()` function behind it serves `snowball ask`.

### 7.4 Extraction contract and the substring guard

The extractor returns JSON validated against this schema (stdlib `json` plus a hand-written validator, no pydantic):

```json
{
  "rows": [{
    "name": "string, required, must appear verbatim (casefold) in the provided chunks",
    "org": "string|null", "kind": "grant|hackathon|fellowship|scholarship|credits|accelerator|competitor|tool|model|other",
    "amount": "number|null", "currency": "ISO 4217|null", "deadline": "YYYY-MM-DD|null",
    "eligibility": "string|null", "location": "string|null", "link": "absolute URL|null",
    "evidence_quote": "string, required, 40-400 chars, verbatim from the chunks"
  }],
  "names": ["strings that appear verbatim in the chunks and are not in the known-entities list"],
  "claims": [{"text": "one sentence", "evidence_quote": "verbatim, 40-400 chars"}]
}
```

Code enforces, before anything else: every `name`, every `names[]` entry and every `evidence_quote` must be a case-insensitive substring of the concatenated chunks after `norm_text`. Anything that fails is removed. This is the mechanism behind I1 and D3: a name the model remembered from training cannot survive because it is not in the text. One repair round-trip is allowed for malformed JSON ("return only the JSON object"); a second failure fails the task.

### 7.5 Three-tier quote and number verification (port of hyperresearch `citecheck.py`)

Runs in the Verifier pool, per row and per claim.

**Tier 1, mechanical triage (zero cost):**
- Every number of 2+ digits in `text`/`amount`/`deadline` must appear in the source page text (after removing thousands separators and currency symbols). `"$50,000"` passes if `50000` or `50,000` is in the page.
- `link`, if present, must canonicalise to a URL in `pages` that we fetched (`verify_urls` from v0, now against the DB). An unfetched link is kept but flagged `link_unverified = 1` and triggers a `fetch` task; the row itself still needs the quote to pass.
- Dates must parse and fall within 2020 to now + 3 years.

**Tier 2, deterministic verbatim check:**

```python
def quote_ok(quote: str, page_text: str) -> tuple[bool, int, int]:
    q, t = norm_text(quote).casefold(), norm_text(page_text).casefold()
    i = t.find(q)
    if i >= 0:
        return True, i, i + len(q)
    # tolerate boilerplate stripping: best token window with >= 0.90 token Jaccard and length within 20%
    qt, tt = q.split(), t.split()
    if len(qt) < 6:
        return False, -1, -1
    best = (0.0, -1)
    for start in range(0, max(1, len(tt) - len(qt) + 1)):
        win = tt[start : start + len(qt)]
        j = jaccard(set(qt), set(win))
        if j > best[0]:
            best = (j, start)
    if best[0] >= 0.90:
        s = len(" ".join(tt[: best[1]])) + (1 if best[1] else 0)
        return True, s, s + len(" ".join(tt[best[1] : best[1] + len(qt)]))
    return False, -1, -1
```

A failing quote drops the row or claim. The page's `noise_count` increments; 5 noise drops on one page mark it `junk`.

**Tier 3, historical lead crawler:** a second model (then described as Qwen 27B) could reject a digest row and could not promote a row that tier 2 had rejected. The old fail-closed sentence said a timeout or a malformed answer still left the item in the digest because tier 2 had passed. That sentence is withdrawn for goal qualification.

**Assurance levels (replaces that fail-closed digest rule).** Review uses `ReviewEngine` and `agy-pool/claude-sonnet-4-6` on Oracle, over observations, not as a funding guarantee. A missing reviewer, a timeout, or a bad reply does not qualify the field and does not publish it as ready.

| Assurance | When it is stored | Qualifies a critical field |
|---|---|---|
| `code_checked` | Quote span and number checks passed. No review keep yet. | No |
| `reviewed_kept` | Reviewer returned `keep` for that observation. | Only together with the constraint rule below |
| `reviewed_dropped` | Reviewer returned `drop`, with a reason. | No |
| `review_pending` | Verdict missing, reply malformed, or reviewer outage. | No |
| `review_unavailable` | No reviewer is configured. | No |

Per-constraint status, evaluated in code against the frozen profile snapshot:

| Status | Rule |
|---|---|
| `met` | Applicable `reviewed_kept` evidence, bound to the program, award, campus, unit, and intake, satisfies the frozen rule. |
| `unmet` | Supported evidence violates the rule. Missing information is not `unmet`. |
| `unknown` | Missing applicability, period, review, or an unconfirmed material profile. Unknown is never stored as `met`. |
| `conditional` | A documented contingency. Explicit acceptance may count the package toward the shortlist and the cell stays conditional. |

Unresolved conflicts between official sources stay `unknown`. The console may show that cell as contested. `contested` is not a stored `constraint_evaluations.status` value.

A mechanical quote check alone never qualifies a critical field. Loans do not satisfy a grant component. `research_ready` is only the positive completion predicate in section 1.6: enough current matching packages, reviewed support, unknown never counted as met, and conditional staying conditional. Reviewer keep is not an admission and not a funding award. Qualification does not start monitoring.

**Deterministic sampling (hyperresearch PR #141):** when a page yields more than 20 claims, the verifier checks all rows and a deterministic subset of claims chosen by `sha1(claim_hash) % 3 == 0` plus every claim with a number. No RNG, so re-runs verify the same subset.

### 7.6 Snowflake post-date validation (port of last30days `grok_x.py`)

```python
SNOWFLAKE_EPOCH_MS = 1288834974657


def snowflake_utc(post_id: str) -> datetime | None:
    try:
        v = int(post_id)
    except ValueError:
        return None
    if v < (1 << 22):
        return None
    return datetime.fromtimestamp(((v >> 22) + SNOWFLAKE_EPOCH_MS) / 1000, tz=timezone.utc)
```

A status id that does not decode, or whose timestamp is outside `[window_start - 1 day, now + 5 min]`, is dropped. A decodable in-window id is still not a post. X Search does not document a response field that carries the fetched post body, and text emitted by the grok CLI is model prose. `validate_x_posts` therefore returns no quotable items. Matching a citation URL records that URL only. x.com cannot be fetched as a page from these boxes: it serves its SPA shell with HTTP 200 for any path.

### 7.7 Dead-link check (port of research-queue)

Before a digest is sent, every `link` in it gets an async `HEAD`; on 405 or 403 a `GET` with `Range: bytes=0-1023`. 404 or 410 marks `entities.link_dead = 1` and the digest line prints the hub page it was found on instead of the dead link. The check uses the same SSRF pin and the same per-domain token bucket.

### 7.8 Secrets and egress hygiene

- The crawler process has `QWEN_API_KEY` and the optional paid keys in its environment and nothing else. `OPENROUTER_API_KEY` is never loaded (standing rule).
- Outbound hosts for API routes are constants; everything else passes `resolve_and_pin`.
- `User-Agent` names the bot and the repo. Per-domain rate is 6 per minute by default and `Retry-After` is honoured.

---

## 8. Knowledge base and incremental wiki compiler

### 8.1 Two layers, one truth

- `wiki/**/*.md` is the source of truth. It is a git repo. A human can edit it; the next `reindex` picks the edit up.
- `wiki_pages`, `wiki_links`, `wiki_fts` are a cache. `snowball reindex` drops and rebuilds them from the files in under 60 s at 10,000 pages (FTS5 insert speed on goku measured in M2).
- The frontier and lock tables are **not** derivable from the wiki and are backed up nightly (section 5.4). Goals, snapshots, and evaluations are also not derivable from Markdown. The v4 bundle in section 5.5 is what preserves them with the raw bytes and human edits. Human-edited wiki regions stay labelled `human_note` and are not source evidence.

The v0 npm `llm-wiki-compiler` is retired at M2 and replaced by `snowball/compiler.py`.

### 8.2 Page layout

Entity page `wiki/entities/<slug(key)>.md`:

```markdown
---
key: mozilla-technology-fund|mozilla|2026
title: Mozilla Technology Fund 2026
kind: grant
family: opportunities
org: Mozilla
year: "2026"
aliases: ["MTF 2026", "Mozilla Tech Fund"]
sources: ["https://foundation.mozilla.org/…", "https://grants-list.example/…"]
tags: ["open-source", "india-eligible"]
first_seen: 2026-10-07
last_changed: 2026-10-07
confidence: 2            # n_sources of the strongest claim
status: new
---
# Mozilla Technology Fund 2026

## Facts
| Field | Value | Evidence |
|---|---|---|
| Amount | USD 50,000 | "awards of up to $50,000" ([src 1]) |
| Deadline | 2026-12-15 | "applications close December 15, 2026" ([src 1]) |
| Eligibility | … | "…" ([src 2]) |

## Summary
<LLM-owned prose, 60-250 words, merged under the 70% floor>

## Changes
- 2026-10-07 deadline: 2026-11-01 → 2026-12-15 ([src 2])

## Sources
1. https://foundation.mozilla.org/… (fetched 2026-10-07)
2. …
```

Only `## Summary` is written by the LLM. Frontmatter, Facts, Changes and Sources are written by code from the DB, so the deterministic parts can never be lost to a bad merge.

Topic pages (`wiki/topics/`) list the entities and hubs under a topic plus a Summary. Hub pages (`wiki/hubs/`) record what the hub lists, its link template, change rate and the last diff. `wiki/index.md` is generated: families → topics → entity counts.

### 8.3 Frontmatter union (port of nashsu `page-merge.ts`)

Array fields `aliases`, `sources`, `tags` are merged by code as **sorted set union** of old and new, never taken from LLM output. Scalar fields come from the DB row. The merge is a pure function and idempotent: `merge(merge(a,b),b) == merge(a,b)`.

### 8.4 Summary merge with the 70% floor

```python
BODY_SHRINK_THRESHOLD = 0.70


def merge_summary(old: str, addition: str, llm) -> str:
    if not old:
        return addition
    proposed = llm.merge(old, addition)  # Qwen 27B, SUMMARY_MERGE_BUDGET
    floor = BODY_SHRINK_THRESHOLD * max(len(old), len(addition))
    if proposed is None or len(proposed) < floor:
        return old.rstrip() + "\n\n" + addition.strip()  # reject compaction loss: union fallback
    for num in NUMBERS_RX.findall(old):  # numbers in the old summary must survive
        if num not in proposed:
            return old.rstrip() + "\n\n" + addition.strip()
    return proposed
```

### 8.5 Dirty marking and bounded staleness (D ≤ 1, port of lucasastorian `propagate_staleness`)

Two flags, two meanings:

- `dirty = 1`: this page's own facts changed (new entity, `entity_changes`, new claim with `n_sources` crossing 2). It will be recompiled in the next compiler run.
- `stale = 1`: a page this page links to had a fact change. It gets a one-line "Related: X changed on <date>" note and a low-priority recompile.

```sql
-- called once per fact change on page :origin
UPDATE wiki_pages SET dirty = 1 WHERE path = :origin;
UPDATE wiki_pages SET stale = 1, stale_reason = :reason, stale_origin = :origin, stale_marked_at = strftime('%s','now')
 WHERE path IN (SELECT from_path FROM wiki_links WHERE to_path = :origin)
   AND path <> :origin AND stale = 0;
```

A recompile that was triggered by `stale` **never** calls the propagation statement, and a recompile triggered by `dirty` propagates only one hop. Therefore a change at page A marks its direct linkers and stops; a cascade through B, C, D is impossible by construction. The compiler processes `dirty` pages first (up to 200 per run), then `stale` pages (up to 50), inside a 600 s time cap; what is left waits for the next wave.

### 8.6 Digest and alerts

```sql
-- new entities
SELECT 'entity', key, relevance * (1 + urgency) AS rank, 'new: ' || kind || COALESCE(' due ' || deadline, '') AS reason
  FROM entities WHERE reported_at IS NULL AND status IN ('new') AND evidence_quote <> ''
UNION ALL
-- material changes
SELECT 'change', id, e.relevance * 1.2, field || ': ' || COALESCE(old,'∅') || ' → ' || new
  FROM entity_changes c JOIN entities e ON e.key = c.entity_key WHERE c.reported_at IS NULL
ORDER BY rank DESC LIMIT 25;
```

`urgency = max(0, 1 - days_to_deadline / 30)`. `relevance` is the TF-IDF cosine between the entity's page text and its family focus, clamped to [0,1]. Every line has a link (or hub fallback if dead), the reason and the deadline. After Discord returns a message object with an `id`, `reported_at` is set on each row inside one transaction; a failed POST leaves them unreported for tomorrow. The `UNIQUE (ref_kind, ref_id)` on `digest_items` makes double-reporting a constraint error.

Urgent alerts: `deadline` within 7 days, `relevance >= 0.5`, `alerted_at IS NULL` → one Discord message immediately after the wave, then `alerted_at = now`. Fires once per entity.

Digest is always written to `digests/YYYY-MM-DD.md`; Discord posting is skipped when `DIGEST_CHANNEL_ID` is empty. Sends go over REST with the bridge bot token, never the MCP. File-only mode does not set `reported_at` and does not count as Discord acknowledgement.

Current defect, recorded as regression `digest-premature-acknowledgement` for task T9: `tools/digest.py` `compile_daily_digest` sets `entities.reported_at` and `entity_changes.reported_at` before `post_to_discord`. The 2026-10-08 plan baseline also records a digest unit command that passed an unsupported `--daily` argument. T0 does not change that runtime. A compile-time timestamp is not a message id.

Current defect, recorded as regressions `digest-eval-zero-over-zero` and `digest-quote-length` for task T10: `tools/eval_gates.py` `eval_digest` sets its pass flag when the quote count is zero, and otherwise treats a quote longer than 10 characters as verified. The observed result is 0/0 with the pass flag set. That flag is not gate M4.08. T0 does not replace the evaluator.

### 8.7 `snowball ask`

`snowball ask "<question>"`: FTS5 `MATCH` over `wiki_fts` (query sanitised: quotes balanced, operators escaped, hyperresearch PR #52), top 12 by `bm25(wiki_fts)`, page bodies filtered with section 7.3 to 25 chunks. The historical design named one Qwen 27B call. The current command retrieves those chunks and, unless `--no-model` is set, calls `LocalPrivateEngine.interpret(prompt, max_out=1024)` on an attested local daemon. That method is not defined yet, so ask returns `private_engine_unavailable` instead of using AGY, Gemini, or a retrieval downgrade. Sentences without a citation are dropped by code. This is not a periodic Gemini hook.

### 8.8 Retention

| Path | Policy |
|---|---|
| `raw/` | delete files older than 14 days; quarantined pages kept 30 days |
| `logs/` | 30 days |
| `data/backup/` | current v4 bundle; surplus historical `snowball-*.db` files pruned to 7 |
| `wiki/`, `data/snowball.db`, `digests/` | forever |

`snowball-retention.timer` runs retention. That writes the section 5.5 bundle and does not write a new dated `VACUUM INTO` `snowball-YYYY-MM-DD.db`. Deadline expiry and `PRAGMA quick_check` stay on that path. Surplus existing `snowball-*.db` files are deleted down to 7.

---

## 9. Milestone roadmap M0 to M4

This section lists 44 Fable acceptance gates: M0.01, M1.01–M1.12, M2.01–M2.12, M3.01–M3.11, and M4.01–M4.08. Access gates A01–A15 are additional and are not part of that 44. Each gate is a command with an expected result. The original schedule estimated about 44 hours. That estimate is not a test count and it is not a completion claim. A green unit suite and any existing git tag are not this list.

### 9.1 Gate evidence

Current status lives in `data/gate-evidence.jsonl`, one object per gate. The fields are `gate_id`, `status`, `command`, `exit_code`, `started_at`, `finished_at`, and redacted `evidence_path`. Status values are `implemented`, `fixture_verified`, `live_verified`, `soak_verified`, and `blocked`.

`implemented` means the spec or the code contains the behavior. It is not verification. `blocked` means the gate has a known gap, a false pass, or a missing live prerequisite. `fixture_verified`, `live_verified`, and `soak_verified` are the only verified states. A verified row requires exit code 0, both timestamps, and a non-empty redacted evidence file. Fixture evidence does not satisfy a gate whose minimum status is `live_verified` or `soak_verified`. A soak gate stays open until the evidence window covers the required duration. `tools/eval_gates.py` `record_gate_evidence` enforces that rule.

The baseline unit discovery for this worktree is 60 tests. That run is not the ledger. The status snapshot shipped with this amendment marks M0.01 `implemented` and every other gate `blocked`, with null exit code, timestamps, and evidence path, because the gate commands were not executed here.

Evaluator code and production code are different paths. T0 does not implement T5, T9, or T10.

| Defect | Owner | Ledger |
|---|---|---|
| `classify_heuristic` in `tools/eval_gates.py` scores the labelled file. The fetch pipeline does not call it. | T5, T10 | `classifier-not-production` blocks M2.01 |
| `eval_extract` checks quotes already stored on fixture rows. It does not run production extraction or `quote_ok`. | T5, T10 | `extractor-not-production` blocks M2.02 |
| `eval_digest` treats a quote longer than 10 characters as verified, and it sets the pass flag when the quote count is zero. The observed result is 0/0 with the pass flag set. | T10 | `digest-eval-zero-over-zero` and `digest-quote-length` block M4.08 |
| `tools/digest.py` `compile_daily_digest` sets `reported_at` before `post_to_discord`. A digest file is not a Discord message id. | T9 | `digest-premature-acknowledgement` blocks M2.07, M2.08, M3.09, M4.01, and M4.08 |
| `reindex_fts` can return counts for an empty database. That does not prove `wiki_fts` count equals the Markdown file count. | T8 | `empty-reindex` blocks M2.12 |
| Six hand-picked strings are not the slug property gate of 10,000 seeded strings. | T5 | `slug-sample-not-property` blocks M1.10 |

### M0: repo, design, plan

| ID | Gate | Command | Pass |
|---|---|---|---|
| M0.01 | Spec documents | read this section, `docs/PLAN.md`, and `README.md` | The access amendment is present, and none of those files treats a unit suite or a git tag as completion of the 44 gates. Ledger status is `implemented` until a command records evidence. |

### M1: store, frontier, waterfall, page and query locks (about 10 h)

Builds: section 5 DDL and connection helper; seeder grid; scheduler pop; tiers 0, 1, 1b, 3 (tier 2 stays inactive until provider activation: credentials, a current tariff, `access.yml`, and nonzero ceilings; a missing key is `blocked`); outage/failure split; SSRF guard; injection gate; page lock 2a/2b/2c; query lock; `fetches` logging; `snowball status`; retention; SSH tunnel unit; `pip install curl_cffi trafilatura` on goku (aarch64 wheels checked again at deployment).

| ID | Gate | Command | Pass |
|---|---|---|---|
| M1.01 | Seed determinism | `snowball seed --dry-run \| sha1sum` twice | identical hashes, `SELECT COUNT(*) - COUNT(DISTINCT query_key) FROM queries` = 0 |
| M1.02 | 24 h soak | `systemctl --user status snowball-supervisor` after 24 h | active, zero restarts from crash in `journalctl`. Minimum ledger status `soak_verified`. |
| M1.03 | Zero duplicate fetches | `SELECT COUNT(*) FROM fetches WHERE due = 0` | 0. Minimum ledger status `live_verified`. |
| M1.04 | Same article, 3 URLs | feed `?utm_source=x`, `www.`, trailing slash variants | 1 `pages` row, `raw_urls` length 3, 1 logical acquisition |
| M1.05 | Route logging | `SELECT route, COUNT(*) FROM fetches GROUP BY 1` | every row has a route; reddit rows are `arctic` only. Minimum ledger status `live_verified`. |
| M1.06 | Wall handling | excluded domain, and one approved supported challenge | Excluded domain stops with no paid call. An approved challenge gets one bounded chain and is not re-acquired for 24 h. A generic 401/403/407/451 without a recognized authorized challenge is an access denial. HTTP 429 cools down and does not rotate identity. Minimum ledger status `live_verified`. |
| M1.07 | Rate limit refund | mock 429 then 200 | `strikes` unchanged, `paused_until` set, page fetched after pause, no paid evasion |
| M1.08 | 3 strikes | mock 404 ×3 | `status='dead'` |
| M1.09 | SSRF | queue `http://169.254.169.254/`, `http://100.65.187.17/`, a hostname resolving to 127.0.0.1, including browser and redirect routes | `fetches.outcome='ssrf_blocked'` and no destination socket. Minimum ledger status `live_verified`. |
| M1.10 | Slug fixed point | property test, 10,000 seeded random strings | `slug(slug(s)) == slug(s)`. Six hand-picked strings do not satisfy this row. |
| M1.11 | Disk flat | `du -sh raw logs` on day 1 and day 8 | day-8 size within 20% of day-7 size. Minimum ledger status `soak_verified`. Historical free-disk figures do not satisfy this row. |
| M1.12 | Budget sleep | set `LLM_TOKENS_PER_DAY=1` | supervisor logs `budget: llm` and keeps fetching; resumes next day. Minimum ledger status `live_verified`. |

### M2: classify, extract, entity lock, compiler, digest (about 10 h)

Builds: heuristics + LLM classify; BM25 filter; extraction schema and substring guard; Tier 1 + Tier 2 verification; entity lock with field diff; hub row diff; names queue → one search each; Python compiler with frontmatter union, 70% floor, D ≤ 1; digest and `reported_at`; yield counters; retire npm compiler.

| ID | Gate | Command | Pass |
|---|---|---|---|
| M2.01 | Classifier accuracy | `snowball eval classify tests/pages_labelled_50.jsonl` | Production classifier ≥ 85% (≥ 43 of 50). `classify_heuristic` does not satisfy this row. Owners T5 and T10. |
| M2.02 | Schema and quotes | `snowball eval extract tests/items_20.jsonl` | Production extraction: 100% of emitted rows validate and 100% of `evidence_quote` pass Tier 2. A scan of quotes already stored on the fixture does not satisfy this row. Owners T5 and T10. |
| M2.03 | Names from text only | inject a page with no proper nouns | `names` gains 0 rows; a planted known-from-training name absent from the text never appears |
| M2.04 | Deadline move | re-fetch a fixture with a changed deadline | exactly 1 `entity_changes` row (`field='deadline'`), 0 new entities, digest shows one "deadline moved" line |
| M2.05 | Hub diff | fixture hub with 20 rows, then 22 | 2 `hub_rows` added, 2 pages queued, `interval_sec` halved |
| M2.06 | Hub unchanged | re-fetch same hub | 0 new rows, `interval_sec` doubled, no LLM call (token counters unchanged) |
| M2.07 | First real digest | morning after enable | `digests/<date>.md` has ≥ 1 entity with link, reason, and evidence, and the send response stored a Discord message id. Setting `reported_at` while compiling the file does not satisfy this row. Minimum ledger status `live_verified`. Owner T9. |
| M2.08 | No repeat in digest | run digest twice | second run posts nothing; `digest_items` UNIQUE never violated. A compile-time acknowledgement does not satisfy this row. Owner T9. |
| M2.09 | 70% floor | mock LLM returning a 200-char summary for a 1,000-char page | file contains old + addition, not the 200 chars |
| M2.10 | D ≤ 1 | graph A→B→C→D fixture, change A | B stale, C and D not stale; recompiling B marks nothing |
| M2.11 | Compile time cap | 500 dirty pages | compiler returns in ≤ 600 s and leaves the rest dirty |
| M2.12 | Reindex | `snowball reindex` | `wiki_pages`, `wiki_links`, and `wiki_fts` rebuilt; `wiki_fts` count equals the Markdown file count. An empty-database reindex does not satisfy this row. Owner T8. |

### M3: claim and topic locks, library-first, ask, alerts, grok X, refill (about 12 h)

Builds: claim lock with FTS5 candidates; topic lock (TF-IDF); drift guard; library-first search skip; `snowball ask`; urgent alerts; grok route with Snowflake validation and kill-after-output; Exa findSimilar and backlink refill; Tier 3 reject-only check.

| ID | Gate | Command | Pass |
|---|---|---|---|
| M3.01 | Claim dedupe | same fact phrased 5 ways from 5 domains | 1 `claims` row, `n_sources = 5`, 4 `claim_variants` |
| M3.02 | Reprint not a source | syndicated copy fixture | `status='reprint'`, `n_sources` unchanged |
| M3.03 | Topic not duplicated | "free GPU credits" vs "cloud GPU credit programs" | cosine ≥ 0.30 (measured 0.64), `topics` count unchanged, 1 deepen task or 1 focus variant |
| M3.04 | Topic boundaries | labelled 30 pairs (10 same, 10 deepen, 10 new) | new-vs-not-new ≥ 95% agreement; same-vs-deepen ≥ 80% agreement |
| M3.05 | Drift guard | queue an off-topic finance page | `status='dropped'`, never in frontier again |
| M3.06 | Repeat rate | `snowball status` after 7 days | `< 5%`. Minimum ledger status `soak_verified`. |
| M3.07 | Library-first | run the same topic twice | second run's `exa_calls` ≤ 30% of the first. Minimum ledger status `live_verified`. |
| M3.08 | Ask | `snowball ask "which grants close in December?"` | answer in < 30 s, every sentence cites a retrieved `[path]`. Minimum ledger status `live_verified`. Owner T10. |
| M3.09 | Urgent alert | entity with deadline in 5 days | exactly 1 alert message, `alerted_at` set, no second alert. A badge string does not satisfy this row. Owner T9. |
| M3.10 | grok | X query fixture with 1 fabricated ID | fabricated item dropped; owned grok PID tree gone after task. Minimum ledger status `live_verified`. Owner T1. |
| M3.11 | Refill | empty frontier fixture with 2 hubs | findSimilar + backlinks queue ≥ 1 page, else sleep 1800 logged |

### M4: quality ports and resilience (about 12 h)

Builds: dead-link check; `occurrence_seq` reaper hardening; tier 2 ZenRows and residential proxy under the section 4.5 activation rule; feedback reactions; weekly summary; `quick_check`; backups. A present API key is not activation and is not a pass.

| ID | Gate | Command | Pass |
|---|---|---|---|
| M4.01 | Dead link | digest with a 404 link | line shows a verified hub fallback, `link_dead = 1`. Owner T9. |
| M4.02 | Kill -9 resume | `kill -9` mid-wave, restart | reaper re-queues ≤ 1 task per owned worker; `SELECT COUNT(*) FROM fetches WHERE due = 0` still 0; no duplicate `entities`. Minimum ledger status `live_verified`. |
| M4.03 | Stale worker | hold a claimed task past 600 s, then commit | commit updates 0 rows; task was re-run once |
| M4.04 | Paid cap | `MAX_PAID_FETCHES_PER_DAY=3` on an activated route | 4th blocked page goes `walled` with `wall_kind='budget'`; reservations count. Shipped ceiling 0 and a missing key stay `blocked`. |
| M4.05 | Paid yield gate | page with `yield_score 0.1` | tier 2 skipped, logged `paid: yield below 0.30` |
| M4.06 | Feedback | 👎 reaction on a digest line | `domains.feedback_score` decreases; frontier `value` for that domain lower next wave; no page dropped by feedback alone |
| M4.07 | Backup | `ls data/backup` after 8 nights | exactly 7 restorable files. Minimum ledger status `soak_verified`. |
| M4.08 | Every digest line verified | `snowball eval digest --since 7d` | Nonempty delivered sample. Every line has a Tier-2 quote found in the recorded source and a live link or a verified hub fallback. Zero quotes are not a pass. Quote length is not the check. Minimum ledger status `live_verified`. Owners T9, T10, and T13. |

The hour estimate above is about 44 hours of agent time across M1–M4. Each milestone is a reviewable change. Greptile and CodeRabbit run before merge when those integrations exist. A missing review integration is a blocked review, not an approval. The 44-hour estimate and the 44 gate IDs are different counts. Neither count is a number of passing tests.

---

## 10. Cross-cutting failure modes and mitigations matrix

| # | Failure mode | Where it bit (audit evidence) | Snowball mitigation | Section |
|---|---|---|---|---|
| 1 | **Scraping blocks and bot bans** | Datacenter IP 403s on Reddit and X; challenge pages returned as HTTP 200; deprecated `.rss` endpoints (last30days) | Free routes stay Arctic Shift, Algolia, GitHub API, and grok for X. Tier 1 uses curl_cffi and marks terminal walls. After provider activation, an approved public origin may use one managed or residential retrieval and, for a supported challenge, one solver task. Login, paywall, robots, explicit denial, and HTTP 429 do not escalate. Code for those routes is not activation. | 4.2–4.6 |
| 2 | **Egress SSRF and metadata exposure** | Agents following discovered links to `127.0.0.1`, RFC 1918, `169.254.169.254`; redirects bypassing checks (hyperresearch PR #53, #127, #130) | DNS pre-resolution with every answer checked, pinned via `CURLOPT_RESOLVE`, manual redirects re-validated, 5-hop cap, ports 80/443 only, Tailscale range `100.64.0.0/10` denied. The same deny list applies to the browser egress gateway. Remote provider fetches are allowlisted and are not described as locally pinned. | 7.1 |
| 3 | **Citation and quote hallucination** | Small models paraphrase quotes; LLM cite-checkers agree with themselves (hyperresearch `citecheck.py`) | Substring guard on names and quotes, Tier 1 numbers, Tier 2 verbatim with 0.90 window fallback, Tier 3 reject-only; drop, never soften | 7.4–7.5 |
| 4 | **SQLite concurrency deadlocks** | Multi-worker writes in WAL without write-lock sequencing (hyperresearch `sync.py`, LDR connection lifecycle lock) | One process, `BEGIN IMMEDIATE` everywhere, `busy_timeout=5000`, retry wrapper, UPSERT not `INSERT OR REPLACE` | 5, 6.3 |
| 5 | **Heavy ML dependency crashes** | PyTorch + FAISS, `faiss-cpu 1.14` SIGILL on ARM (LDR) | Zero Torch/FAISS. FTS5, BM25, TF-IDF, MinHash in pure Python; embeddings only as an optional endpoint behind a benchmark gate | 3.5, 7.3 |
| 6 | **Recursive context explosions and token exhaustion** | Unbounded follow-up trees; `GraphRecursionError` losing all findings (deer-flow PR #838, TokenBudgetMiddleware) | No depth limit but 3-dry-hop cut; per-task token budget with 80% synthesize-only and 100% commit-partial; adapter `MIN_TOKENS=16384` | 6.5, 6.7 |
| 7 | **Scheduler orphans and split-brain** | Crashed workers' tasks never reclaimed; late workers overwriting completed tasks (deer-flow `occurrence_seq`) | Monotonic `occurrence_seq` on claim, heartbeat, 600 s reaper, sequence-checked commit | 6.3 |
| 8 | **Outage counted as failure** | 429 burning retry attempts and killing good sources (research-queue) | Outage class pauses the domain with exponential backoff and refunds the attempt; failure class strikes; 3 strikes dead | 4.7 |
| 9 | **Compaction loss on page merge** | LLM merges summarising away 30–60% of a page; dropped frontmatter arrays (nashsu `page-merge.ts`) | Code owns frontmatter, Facts, Changes, Sources; LLM owns only Summary; `BODY_SHRINK_THRESHOLD=0.70` plus number-survival check; deterministic set union | 8.3–8.4 |
| 10 | **Staleness cascades** | One update marking a whole vault stale recursively (lucasastorian `propagate_staleness`) | `dirty` vs `stale`, propagation only from fact changes, exactly one hop, stale recompiles never propagate | 8.5 |
| 11 | **Duplicate work across parallel workers** | Parallel subagents re-scraping one URL (LDR PR #6759/#6760); sibling writers duplicating sections (GPTR PR #2158) | Page lock before fetch is the single-flight cache; `max 4 per domain` per wave; sibling fencing in extraction prompts | 3.2, 6.6 |
| 12 | **Hallucinated post dates from X** | grok inventing recent dates for old or nonexistent posts (last30days `grok_x.py`) | Snowflake decode is a negative filter only. Model text is not ingested. A decodable in-window id is not a retrieved post | 7.6 |
| 13 | **Orphaned rows and slug collisions** | Note rows written before their parent, slugs that re-slug differently (hyperresearch PR #86, #126) | Foreign keys ON, parent-first writes in one transaction, `slug()` fixed-point property test, 80-char truncation | 3.3, 5 |
| 14 | **Credential leak on redirect** | `Authorization` forwarded cross-origin on 302 (last30days PR #1069) | Manual redirects drop auth and cookies when origin changes | 7.1 |
| 15 | **FTS5 query syntax injection** | User or page text breaking `MATCH` (hyperresearch PR #52) | Query sanitiser: balanced quotes, escaped operators, tokens only | 8.7 |
| 16 | **Empty report saved as success** | Search outage producing an empty but "successful" run (research-queue `MIN_REPORT_CHARS`) | A fetch with < 200 chars of text is a `failure`; a wave with 0 fetches ok and ≥ 10 outages is `status='aborted'` and does not advance any `next_check_at` | 4.3, 6.2 |
| 17 | **Prompt injection from crawled pages** | Pages instructing the agent to add topics or run commands | No tools; quarantine regexes; nonce-wrapped untrusted blocks; quarantined pages emit nothing; output secret scan | 7.2 |
| 18 | **Topic drift without approval** | Autonomous resolution PR abandoned for runaway drift (nashsu) | Family relevance floor, yield pruning, dry-hop cut, topics only from names and hubs | 3.5, 6.7 |
| 19 | **Memory and disk exhaustion on the host** | Leaked browsers and local model tests froze oracle (2026-09-28); oracle disk at 91–97% | No default browser and no saved browser profile. The optional challenge worker is one context at 768 MB, inside `MemoryMax=3G`, and off until provider activation. Soft memory guard 2200 MB. State on goku. Free-disk floor 5 GB. Historical free-RAM and disk-percent figures are not deployment evidence. | 2.4, 6.4, 8.8 |
| 20 | **Silent delivery failure** | Discord MCP "not allowlisted" dropping sends | REST with the bridge bot token; `reported_at` set only on HTTP 2xx with a message id | 8.6 |

---

## Appendix A: configuration reference

`config.env` (non-secret, committed):

| Key | Default | Meaning |
|---|---|---|
| `DIGEST_CHANNEL_ID` | empty | Discord channel; empty writes `digests/` only |
| `WAVE_PAUSE_SEC` | 120 | pause between waves; 1800 when the frontier is dry |
| `COLLECTOR_CONCURRENCY` | 6 | drops to 2 under the memory guard |
| `VERIFIER_CONCURRENCY` | 4 | |
| `LLM_TOKENS_PER_DAY` | 2000000 | in + out |
| `EXTRACT_TOKEN_BUDGET_IN` / `_OUT` | 12000 / 4000 | per extraction task |
| `SUMMARY_MERGE_BUDGET_IN` / `_OUT` | 8000 / 2000 | per Summary merge |
| `ACCESS_MODE` | off until activation | `auto` implements routes only after domain policy and nonzero ceilings exist |
| `MAX_PAID_FETCHES_PER_DAY` | 0 shipped | tier 2 cap. 100 is the proposed canary after activation, not permission to spend |
| `MAX_CAPTCHA_SOLVES_PER_DAY` | 0 shipped | submitted tasks, including unresolved ones. Canary 10 after activation |
| `PAID_DAILY_BUDGET_MICRO_USD` | 0 shipped | combined daily ceiling in integer micro-USD. Canary 5000000 (USD 5) after activation |
| `PAID_MONTHLY_BUDGET_MICRO_USD` | 0 shipped | combined calendar-month ceiling, IST. Canary 50000000 (USD 50) after activation |
| `PAID_FETCH_MIN_YIELD_SCORE` | 0.30 | tier 2 gate |
| `MAX_PAID_RETRIEVALS_PER_ACQUISITION` | 2 | billed retrieval operations, not a provider shopping loop |
| `MAX_SOLVES_PER_ACQUISITION` | 1 | solver tasks per acquisition |
| `MAX_PROXY_BYTES_PER_ACQUISITION` | 20971520 | integer bytes, including browser subresources |
| `BROWSER_CONCURRENCY` | 1 | isolated challenge worker, off until activation |
| `BROWSER_WORKER_MEMORY_MB` | 768 | inside `MemoryMax=3G` |
| `ACCESS_DEADLINE_SEC` | 240 | whole escalation |
| `CAPTCHA_DEADLINE_SEC` | 120 | inside the access deadline |
| `CAPTCHA_POLL_SEC` | 5 | minimum provider poll interval |
| `SESSION_TTL_SEC` | 600 | cookies and tokens die with the acquisition |
| `DOMAIN_RATE_PER_MIN` | 6 | token bucket, burst 3 |
| `MAX_BODY_BYTES` | 5242880 | 5 MB |
| `RAW_RETENTION_DAYS` / `LOG_RETENTION_DAYS` | 14 / 30 | |
| `MEMORY_SOFT_CAP_MB` | 2200 | |
| `DISK_MIN_FREE_GB` | 5 | |
| `EMBEDDINGS_URL` | empty | optional; must pass the M3 topic benchmark before use |

`LLM_TOKENS_PER_DAY` 2000000 in the table above is the historical lead-crawler design default. A 32,000,000 figure in older local configuration is also historical. Oracle estimated ceilings are in the README (800000 total, 600000 extract, 200000 review) and are not a hard provider-token cap.

Harness keys. Put them in the environment file you pass as `SNOWBALL_ENV_FILE`. On Oracle that file is `config.oracle.env`. They are not activated by this document.

| Key | Value | Meaning |
|---|---|---|
| `GEMINI_MODEL` | `agy-pool/gemini-3.8-flash` on Oracle | Extract model id |
| `GEMINI_TRANSPORT` | `omp` on Oracle | Extract transport through the AGY proxy |
| `REVIEW_MODEL` | `agy-pool/claude-sonnet-4-6` on Oracle | Review and plan model id |
| `PLAN_TOKENS_PER_WAVE` | unset until you accept it | Per-wave plan accounting. Design recommendation 8000. Estimated, and it consumes the shared total. |
| `PRIVATE_MODEL_ENDPOINT` | unset | `private_model_endpoint`. Set only after you attest an isolated daemon. Loopback is not that attestation. |
| `PRIVATE_MODEL_ID` | unset | `private_model_id`. Leave unset until isolation is attested. |
| `PRIVATE_MODEL_ATTESTED` | unset | `private_model_attested`. Only boolean true counts. |
| `PRIVATE_MODEL_ISOLATION` | unset | `private_model_isolation`. 20–500 characters naming egress or isolation. |
| `CONSOLE_ALLOWED_ORIGINS` | `https://snowball.ishannaik.com` on Oracle | Browser Origin allowlist. Independent of Caddy's Host rewrite. |
| `CONSOLE_COOKIE_SECURE` | `1` behind that HTTPS origin | `Secure` flag on the session cookie |

`REVIEW_*` and `PLAN_*` are the review and plan settings. `GROK_X_*` stays for public X acquisition only. The SSH host key is `SSH_MODEL_HOST` only. Removed names are not aliases: `PRIVATE_MODEL`, `GROK_VERIFIER_MODEL`, `GROK_TOKENS_PER_DAY`, `GEMINI_SSH_HOST`, and `INDIA_ELIGIBILITY_CHECK`.

`.env` (secrets, never committed): `QWEN_API_KEY` (required for model calls), `GH_TOKEN` (optional), `ZENROWS_API_KEY`, `RESIDENTIAL_PROXY_URL`, and `CAPTCHA_API_KEY` (optional; each is inert while ceilings are zero or `access.yml` lacks that origin). The Discord bot token is read from the bridge's own env file path, never copied. Never log API-key query parameters, proxy credentials, challenge tokens, or cookies.

`topics.yml`: `families` with `recheck_days`, `weight`, `relevance_floor`, `focus`; `seeds` with `id`, `family`, `focus`, optional `regions`, `audiences`. The seeder expands `family x region x audience x source x window` into `queries` with `discovered_via='seed'`.

## Appendix B: CLI reference

| Command | Does |
|---|---|
| `snowball seed [--topics FILE] [--dry-run or --dryrun] [--db PATH] [--json]` | Expand the topic file into the seed grid. Does not accept `--topic`, `--max-topics`, or `--mode`. Does not by itself authorize a wave. |
| `snowball run [--topic SLUG] [--max-topics N] [--mode auto or gemini-bulk] [--topics FILE] [--dry-run or --dryrun] [--db PATH] [--json]` | One batch. `--topic` is a slug. |
| `snowball run-once` (same flags as `run`) | One wave, then exit. |
| `snowball supervise` (same flags as `run`) | Persistent loop. `--supervise` forwards `--max-topics` and `--topic` as `slug_filter`. `interval_sec` stays 60. There is no `--interval` flag and no `stop_file` argument on that call. |
| `snowball status [--db PATH] [--json]` | Read-only frontier and harness status. Does not take `--topic`, `--topics`, `--dry-run`, `--max-topics`, or `--mode`. |
| `snowball ask "<q>"` | library answer with citations |
| `snowball digest [--date]` | build and (if configured) post the digest |
| `snowball compile [--full]` | incremental compile, or full rebuild of `wiki/` from the DB |
| `snowball reindex` | rebuild `wiki_*` tables from `wiki/*.md`. The current `tools/eval_gates.py` `reindex_fts` path is not M2.12. Owner T8. |
| `snowball retention` | prune raw/logs, backup, expire deadlines, quick_check |
| `snowball eval classify\|extract\|digest …` | evaluator commands. Current classify, extract, and digest implementations are not production-gate proofs. See section 9.1. Owners T5 and T10. |
| `python tools/eval_gates.py evidence-check` | validate `data/gate-evidence.jsonl`. A successful check reports row counts. It does not run gates and it does not mark them verified. |
| `snowball migrate --db PATH --bundle-root DIR` | Explicit v4 upgrade. Writes the corpus bundle first. `status` and `serve` do not migrate. |
| `snowball goal new\|show\|interview\|propose\|diff\|approve\|grant\|start\|pause\|resume\|stop\|revise\|cancel-revision\|replan\|adopt-topics` | Local goal control. `propose` requires `--from-fields`. `approve` requires `--version` and `--hash`. `grant` requires `--contract`, `--limits`, and `--expires`. `grant` does not start acquisition. |
| `snowball console token --ttl 900 --db PATH` | Prints a one-use bootstrap token. Exec target is `tools/console.py`. |
| `snowball serve --host 127.0.0.1 --port 8989 --db PATH` | `snowball.py` execs `tools/console.py` with `--db`, `--host`, and `--port`. `visualize` is the terminal view. |
| `snowball run-once --dry-run` | In-memory copy, no external calls, goal gate included. |
| `snowball status --json --db PATH` | Read-only. Adds goal, grant, headroom, substate, and role availability when the harness status fields are present. Does not take `--topic`. |

## Appendix C: constants table

| Constant | Value | Used in |
|---|---|---|
| Claim near-duplicate threshold | hybrid ≥ 0.70 | 3.4 |
| MinHash permutations / bands / rows | 128 / 16 / 8 | 3.2 |
| Reprint threshold | est. Jaccard ≥ 0.80 | 3.2 |
| Entity fuzzy name threshold | trigram Jaccard ≥ 0.85 (same org and year) | 3.3 |
| Topic merge / deepen / new | stemmed-unigram TF-IDF cosine ≥ 0.75 / 0.30–0.75 / < 0.30 | 3.5 |
| Quote window fallback | token Jaccard ≥ 0.90, ≥ 6 tokens | 7.5 |
| BM25 | k1 1.5, b 0.75, keep ≥ 0.5 × max, top 25, 800-char chunks | 7.3 |
| `BODY_SHRINK_THRESHOLD` | 0.70 | 8.4 |
| Staleness depth | 1 | 8.5 |
| Strikes to dead | 3 | 4.7 |
| Dry hops to cut | 3 | 6.7 |
| Junk pages to block a domain | 5 | 5.2 |
| Noise drops to mark a page junk | 5 | 7.5 |
| Outage backoff | 15 min × 2ⁿ, cap 24 h | 4.7 |
| Walled re-check | 30 d, then 90 d | 4.6 |
| Adaptive interval bounds | 6 h to 90 d | 3.2 |
| Family re-check | alpha 1 d, opportunities 14 d, competitors 30 d | 5.1 |
| Heartbeat / reap | 60 s / 600 s | 6.3 |
| Exa reserve | 3 live keys | 6.4 |
| Snowflake epoch | 1288834974657 ms | 7.6 |
