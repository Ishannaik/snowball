# Snowball: full plan

Written 2026-10-04. The crawl logic itself is in [DESIGN.md](DESIGN.md), and the outside projects we borrow from are in [FEATURE_MATRIX.md](FEATURE_MATRIX.md).

The sections below are the legacy lead-crawler feature list, amended on 2026-10-08 for public-page access. Where they say the agent grows without an approval queue, that sentence belongs to that crawler.

The goal-contract harness is specified in [superpowers/specs/2026-10-09-snowball-goal-harness-design.md](superpowers/specs/2026-10-09-snowball-goal-harness-design.md) and tasked in [superpowers/plans/2026-10-09-snowball-goal-harness-plan.md](superpowers/plans/2026-10-09-snowball-goal-harness-plan.md). Those approved files stay unchanged. Operator install, explicit corpus migration, full-bundle rollback, interview, hash approval, a separate grant, console sign-in, and the Oracle cutover are in [../README.md](../README.md). Architecture additions are [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) sections 1.6, 5.5, 6.10, and 7.5. A v2 or v3 pre-change bundle is named `goal-v{2|3}-{unix}-{8 hex}` under the bundle root. Exit 2 JSON does not print that path. A failure inside `create_bundle` deletes the incomplete directory, so its absence means the bundle was removed, not that migrate never started. A failure in the schema transaction after `create_bundle` returns leaves that verified directory in place and does not advance `schema_version`.

The frozen profile snapshot keeps unknown and unconfirmed private provenance. Required public scope and finite limits are confirmed. Explicit bounded public research can proceed while private eligibility stays unknown. Consent false does not invent cash or a nationality, and it does not schedule private evaluation. Unknown private criteria block a positive ready result. This pointer does not record harness scenarios G01–G30 as passed, does not record a production deployment, and does not enable `snowball-loop.service`. Design scenarios O01–O03 stay not exercised until a goal, a grant, and a bounded live smoke are approved. Snowball does not install a periodic Gemini hook. A public-only Gemini pass in the build workflow is outside this runtime.

## Current policy and gate status (2026-10-08)

[SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) is the current policy. This file keeps the original feature list. Section 1's non-goals and row B3 are the amended access text. Invariant I4 in SYSTEM_DESIGN is the access rule. Row I4 in the ops table below is retention. Those are different I4s.

Access amendment: configured managed fetch, residential proxy, and a CAPTCHA provider may read operator-approved public pages. Escalation is domain-scoped, cost-capped, and time-bounded. Login, paywall, robots exclusions, explicit access denials, and rate-limit bans do not escalate. Public-page access does not authorize login bypass, paywall bypass, purchase or application actions, credential use on the target, or identity rotation through a ban. Private topics stay on local models. Evidence quotes, tool-free models, and code-generated queries stay in force.

Feature implementation is the code path. Provider activation is credentials, a current tariff, nonzero ceilings, and an `access.yml` allowlist. Shipped secret-free ceilings stay at zero. A missing key is `blocked`, not a pass. Spend is integer micro-USD. Proxy traffic is integer bytes. Provider-specific units are not added to either. The old ZenRows 1/5/10/25 credit table is not a current tariff.

Gate completion is a verified row in `data/gate-evidence.jsonl` (`fixture_verified`, `live_verified`, or `soak_verified`) with `gate_id`, `command`, `exit_code`, `started_at`, `finished_at`, and a redacted `evidence_path`. `implemented` and `blocked` are not verified. The original Fable list is 44 gates (M0.01, M1.01–M1.12, M2.01–M2.12, M3.01–M3.11, M4.01–M4.08). Access gates A01–A15 are additional. The baseline unit discovery on this worktree is 60 tests. That run is not the ledger. An existing git tag is not the ledger.

Evaluator and production paths differ. T0 does not implement the missing runtime.

- Classifier: `tools/eval_gates.py` `classify_heuristic` is not the fetch pipeline. Owners T5 and T10. Regression `classifier-not-production` blocks M2.01.
- Extractor: `eval_extract` scores quotes already on fixture rows. A row that runs the extractor still calls `extract_page_entities(source_text=, url=)`, and that call raises `TypeError`. The public engine entry is `GeminiBulkExtractor.extract(frame, program, field_keys, source, binding, operation_key, max_out)` with `interview.PublicFrame` and `prompts.PublicSourceSnapshot`. `program` is `program_key`, `name`, `institution`, `official_domain`, `degree_level`, `modality`, and `field_tags`, and `field_keys` stays separate. Extract and review binding is exactly `goal_id`, `contract_version`, `strategy_version`, `epoch`, and `task_id`. Review does not take `program` or `field_keys`. Owners T5 and T10. Regression `extractor-not-production` blocks M2.02. This note does not record M2.02 as passed.
- Digest audit: `eval_digest` accepts quote length over 10 characters and sets the pass flag on 0 quotes. The observed result is 0/0 with the pass flag set. Owner T10. Regressions `digest-eval-zero-over-zero` and `digest-quote-length` block M4.08.
- Delivery: resolved in code (`tools/digest.py`). `compile_daily_digest` does not set `reported_at`; `reported_at` is written only by `acknowledge_digest` after confirmed Discord delivery.
- Reindex: an empty `reindex_fts` does not prove M2.12. Owner T8. Regression `empty-reindex`.
- Slug: six examples are not the 10,000-string property gate. Owner T5. Regression `slug-sample-not-property` blocks M1.10.

Plan baseline observed 2026-10-08, not a deployment measurement:

- A live isolated cloud-credit run fetched two official pages with HTTP 200. Google yielded one verified entity and three committed claims. AWS extraction received a model 503 and stayed pending. The entity link was null. X search cleanup failed on Windows at `os.killpg`.
- Oracle and goku had no installed Snowball user units. The configured adapter on 8812 was unreachable. Historical free-memory and free-disk figures in section 3 are not deployment evidence.
- The digest unit command passed an unsupported `--daily`. In current code, `compile_daily_digest` leaves rows unreported; `reported_at` requires delivery acknowledgement.
- `tests/test_fable_gates.py` does not execute the 44 gates. Paid fetching, hub diffs, refill, drift enforcement, incremental dirty/stale compilation, retention, feedback, and urgent alerts still need their owners.
- The inherited `tools/forum.py` Camoufox path is outside the active orchestrator. It is not a second access policy and it is not the Turnstile or reCAPTCHA solver. It stays until owned challenge fixtures pass. T0 does not switch that runtime on.

## 1. What we want

A research agent that runs 24/7 and never stops digging.
- It starts from a few seed topics, follows what it finds layer after layer, and builds its own library.
- It never re-reads a page and never re-researches what it already knows.
- It tells you only what is new.

**Done looks like this:** every morning one Discord digest, "N new findings, M worth your time". Every item in it has a link and a reason. Asking it "what do we know about X" answers from the library in seconds.

**Non-goals:**
- **Authorized public access only.** Login, paywall, robots, explicit denial, and rate-limit bans stop escalation. Supported public challenges on an `access.yml` origin may use one bounded managed, residential, or solver step after provider activation. That is not a bypass, and the code path is not activation.
- **No restricted sources.** It does not crawl leak, warez or credential sites.
- **No acting on findings.** It does not apply for anything or post anything.
- **No approval queue in the legacy crawler.** That crawler grew from seeds and the web. Human feedback was optional and only adjusted scores. The goal harness supersedes this bullet for acquisition: a wave runs only under an approved contract and a separate execution grant.

## 2. Features

P0 is the first cut, P1 comes next, P2 is later. Each feature has a test that proves it works.

### A. Frontier and scheduling
| # | Feature | Pri | Test |
|---|---|---|---|
| A1 | SQLite frontier of URLs, queries and topics, each with a score = novelty x value x due | P0 | Pops highest score first; restart keeps the queue |
| A2 | Seed grid generated by code from `topics.yml` (family x region x audience x source x window) | P0 | Same seeds give the same grid; zero duplicate queries |
| A3 | Re-check timers per family (alpha 1 d, opportunities 14 d, competitors 30 d), adaptive: unchanged doubles, changed halves | P0 | A page is re-fetched only when due |
| A4 | Budgets: Exa calls/day, LLM tokens/day, fetches per domain per hour; sleeps instead of failing | P0 | Hitting a cap pauses; next day resumes |
| A5 | No fixed depth. A branch is cut after 3 dry hops | P0 | Synthetic graph: a paying branch reaches depth 50, a dry one stops at 3 |
| A6 | Off-topic drift guard: relevance to the seed families below a threshold means drop | P1 | Off-topic pages never enter the frontier |

### B. Fetching and sources
| # | Feature | Pri | Test |
|---|---|---|---|
| B1 | Router: Reddit to Arctic Shift, GitHub to the API, HN to Algolia, X to grok, everything else to fetch | P0 | Each source type hits its route |
| B2 | Cheapest first: RSS/sitemap/API, then plain GET, then Exa contents | P0 | Logs show the route used per fetch |
| B3 | Walls: login, paywall, robots, or an explicit denial stops escalation. A supported public challenge on an approved origin may take one bounded chain after provider activation. HTTP 429 cools down and does not rotate identity. | P0 | Excluded domain makes no paid call. Approved challenge is one chain, then no re-acquisition for 24 h. |
| B4 | 3 strikes then dead; 429 pauses the domain and does not count as a strike | P0 | Rate limit does not kill a good domain |
| B5 | Exa key pool via the rotator (:8791), with a live-key floor | P0 (exists) | Pool exhaustion pauses search, fetch continues |
| B6 | grok X search with post-date validation; kill the process after output | P1 | Fabricated or out-of-window posts rejected |
| B7 | Exa find-similar and backlinks for hub discovery | P1 | Dry frontier refills from best hubs |

### C. Safety
| # | Feature | Pri | Test |
|---|---|---|---|
| C1 | Injection scan on every page; a quarantined page adds no links or topics | P0 (exists) | `tests/injection_test.sh` passes |
| C2 | The model has no tools. Code fetches and writes; the model only reads text and returns JSON | P0 (exists) | No shell or network call from model output |
| C3 | Citation check: every claim's URL must be a page we fetched | P0 (exists) | Uncited or unfetched lines are dropped |
| C4 | Quote check: a quoted text must appear verbatim in the source | P1 | Fake quote is caught |
| C5 | Dead-link check on cited URLs | P1 | 404 citation is flagged |

### D. Understanding pages
| # | Feature | Pri | Test |
|---|---|---|---|
| D1 | Classify each page as hub / item / article / junk (code heuristics first, LLM only when unsure) | P0 | Labelled set of 50 pages, at least 85% correct |
| D2 | Item extractor gives JSON rows: name, org, kind, amount, deadline, eligibility, link, evidence quote | P0 | Rows validate against the schema; evidence quote is found in the page |
| D3 | Name extractor on articles gives unknown names, then one search each | P0 | Names come only from page text, never from model memory |
| D4 | Junk scoring per domain; N junk pages block the domain | P1 | SEO farm blocked after N pages |

### E. No-repeat (the core)
| # | Feature | Pri | Test |
|---|---|---|---|
| E1 | Page lock: canonical URL (strip utm, www, m., trailing slash) + content hash | P0 | Same article via 3 URLs is fetched once |
| E2 | Query lock: normalized query + window | P0 | Same query never runs twice in its window |
| E3 | Entity lock: name + org + year. Known entity gets a field diff only | P0 | Re-seen grant with a moved deadline reports "deadline moved", nothing else |
| E4 | Claim lock: claim hash + near-duplicate (trigram/Jaccard 0.7) with a "seen in N sources" count | P1 | Same fact from 5 sites is one claim with N=5 |
| E5 | Topic lock: embedding similarity; merge at >= 0.85, deepen at 0.7-0.85 | P1 | "free GPU credits" and "cloud GPU credit programs" merge |
| E6 | Reprint detection (MinHash) so copied articles do not count as new sources | P2 | Syndicated copy is linked to the original |

### F. Library
| # | Feature | Pri | Test |
|---|---|---|---|
| F1 | Markdown wiki: one page per entity/topic, with backlinks and sources | P0 (exists, fix compile) | Compile finishes inside its budget |
| F2 | Library-first: before searching, answer from the library; search only gaps or stale facts | P1 | A second run on the same topic makes far fewer Exa calls |
| F3 | Page merge, duplicate pages, stale-page flags | P2 | Two pages on one entity merge |
| F4 | `snowball ask "<question>"` answers from the library with citations | P1 | Answer in under 30 s, every line cited |

### G. Learning
| # | Feature | Pri | Test |
|---|---|---|---|
| G1 | Yield score per hub, domain and query = new useful items / fetches | P0 | High-yield hub re-checked more often |
| G2 | Optional feedback: 👍/👎 reactions on digest items adjust scores. Never a gate | P2 | 👎 on a domain lowers its score |

### H. Delivery
| # | Feature | Pri | Test |
|---|---|---|---|
| H1 | Daily digest to a Discord channel: new and changed items only, ranked, with link, reason and deadline | P0 | Digest never repeats yesterday's items |
| H2 | Urgent alert: a relevant item with a deadline under 7 days goes out immediately | P1 | Fires once per item |
| H3 | Weekly "what the library learned" summary | P2 | |

### I. Ops
| # | Feature | Pri | Test |
|---|---|---|---|
| I1 | systemd user service, Restart=always, MemoryMax, STOP file | P0 (exists) | |
| I2 | Resume: a crash mid-run resumes from the last step, not from scratch | P1 | Kill -9 mid-run, restart, no duplicate work |
| I3 | `snowball status`: frontier size, fetches/day, new items/day, repeat rate, budget left | P0 | |
| I4 | Retention: raw pages and logs pruned after N days; the DB and wiki are kept | P0 | Disk use flat after a week |
| I5 | Fix the v0 bugs: a topic retried forever (cap retries), the compile timeout, the empty digest channel | P0 | |

## 3. Architecture

```
seeds (topics.yml) -> grid.py --------------------+
                                                  v
           +------------------- frontier (SQLite) <------------------+
           |                          |                              |
           v                          v                              |
   scheduler (score, due,       fetch router (B1-B4)                 |
   budgets, prune)                    |                              |
                                      v                              |
                              safety (C1) -> classify (D1)           |
                                      |                              |
               +----------------------+--------------+------------+  |
               v                      v              v            v  |
          hub: links             item: row      article: names  junk |
               |                      |              |               |
               +---> locks E1-E5 <----+--------------+               |
                          |  new / changed only                      |
                          v                                          |
                library (wiki + DB) -> digest (H1) / alerts (H2)     |
                          |                                          |
                          +-- yield (G1) ----------------------------+
```

**Data model (one SQLite file, `data/snowball.db`):**
- `topics(id, family, focus, parent, embedding, score, status, created)`
- `queries(norm, window, last_run, yield)`
- `pages(canon_url, domain, content_hash, kind, status, fetched_at, next_check, interval, hops_dry, depth, yield)`
- `entities(key, name, org, year, kind, amount, deadline, eligibility, link, first_seen, last_changed)`
- `entity_changes(key, field, old, new, seen_at)`
- `claims(hash, text, n_sources, first_seen)` plus `claim_sources(hash, canon_url)`
- `domains(domain, junk, yield, walled, blocked)`
- `runs(id, step, started, ended, tokens, exa_calls)`

**Where it runs:** production state belongs on goku. Oracle supplies model and Exa services and does not hold Snowball state. The 2026-10-04 note "4 ARM cores, 11 GB free RAM, 98 GB free disk" and "Oracle's disk is 97% full" is a historical measurement, not deployment evidence. Measure the host again at deployment.
- **LLM:** the qwen proxy over the tailnet. The proxy currently listens only on Oracle's localhost, so goku needs a key-gated tailnet listener.
- **Embeddings:** a small CPU model on goku, e.g. bge-small via ONNX, about 150 MB RAM. We have not confirmed that the proxy serves embeddings; `/v1/embeddings` did not answer on Oracle.

## 4. Milestones

| M | What | Est. | Done when |
|---|---|---|---|
| M0 | Repo, design, plan | done | This file |
| M1 | DB + frontier + grid + fetch router + page/query locks + walls + budgets + status (A1-A5, B1-B5, E1-E2, I3-I5) | 8 h | Runs 24 h on goku; zero duplicate fetches in the log; disk flat |
| M2 | Classify + item/name extraction + entity lock + digest (D1-D3, E3, H1, G1) | 8 h | First digest with real new items; a moved deadline shows as a change |
| M3 | Claim + topic locks, library-first, `ask`, urgent alerts, grok X, find-similar (E4-E5, F2, F4, H2, B6-B7, A6) | 12 h | Repeat rate < 5%; Exa calls per new item falling week over week |
| M4 | Quality ports: quote check, dead links, resume, merge, reprints, feedback (C4-C5, I2, F3, E6, G2) | 12 h | Every digest line verified against its source |

Total is about 40 h of agent time over roughly a week. Each milestone ships and runs before the next one starts.

## 5. How we know it works (metrics, in `snowball status`)

- **Repeat rate:** re-fetched pages + re-reported items, divided by the total. Target < 5%.
- **Yield:** new useful items per 100 fetches, tracked weekly.
- **Cost per useful item:** Exa calls + tokens per new item.
- **Digest precision:** the share of items worth your time (from optional reactions).
- **Frontier health:** size, depth distribution and dry-branch cuts per day.

## 6. Open decisions

1. **Discord channel ID** for the digest. Until we have one, digests are written to `digests/` only.
2. **Expose the proxy to goku** over the tailnet (key-gated), or run Snowball on Oracle with a strict disk cap. Recommendation: goku.
3. **Daily budgets** to start with: Exa 300 calls/day and 2M LLM tokens/day, then tune from M1 data.

## 7. Risks

- **Drift** without approvals. Mitigations: the family relevance threshold, yield pruning and the depth-of-dryness cut.
- **Weak local models.** They produce false merges and false items, so the deterministic locks run first and the model second, and every item needs an evidence quote found in the page.
- **Arctic Shift** timeouts on busy subreddits. Backoff exists; there is no fallback by design (no IP-burning scraping).
- **Exa pool exhaustion.** Library-first search (F2) is the main saver.
- **grok `-p` hangs** after it answers. It must be killed after output.



## 8. Competitor Hardening & Battle-Tested Mitigations
Synthesized from forensic audits of 8 open-source agents (see [COMPETITOR_DEEP_DIVE.md](COMPETITOR_DEEP_DIVE.md)):
1. **SSRF Guard (`safe_url_fetcher`):** Pin resolved DNS IPs before HTTP connection; reject RFC 1918 / loopback / cloud metadata (`169.254.169.254`).
2. **Reddit Shreddit Endpoints:** Query `/svc/shreddit/search/` server components instead of deprecated `.rss` endpoints.
3. **Snowflake Post Validation:** Extract millisecond timestamps `(id >> 22) + 1288834974657` from X URLs to reject hallucinated posts.
4. **Pure-Python BM25 Filter:** Adopt GPTR PR #2161/2162 pattern to bypass heavy PyTorch/FAISS dependencies on ARM.
5. **SQLite `BEGIN IMMEDIATE` Locking:** Enforce write serialization in WAL mode to eliminate multi-worker mutex deadlocks.
6. **TokenBudgetMiddleware Tool Stripping:** Strip tool calling capability when budget reaches 100%, forcing synthesis and graceful exit (DeerFlow).
7. **Attempt-Refunding Outage Handling:** Treat HTTP 429 and transient gateway 503s as pauses rather than incrementing task failure strikes (Research-Queue).
8. **Page Merge 70% Length Floor:** Enforce `BODY_SHRINK_THRESHOLD = 0.70` and deterministic YAML array unions (`sources`, `tags`, `aliases`) to prevent compaction loss (Nashsu `page-merge.ts`).
9. **Sibling-Section Fencing:** Inject sibling subagent assignment titles into prompts to prevent duplicate research sections (GPTR PR #2158).
10. **Bounded Staleness Propagation:** Limit wiki graph dirty propagation to depth D <= 1 to avoid cascading recompilation storms (LucasAstorian).
