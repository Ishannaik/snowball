# Snowball design

## Goal

A research agent that runs 24/7 and grows its own knowledge base. It starts from a few seed topics. It finds new topics in the pages it reads, so the model never invents them from memory. It never repeats work, and it reports only what is new.

## Why not a fixed query list

When an LLM is asked to invent queries, it collapses onto the same few ideas. A hand-written list goes stale and cannot cover topics nobody knew existed. So:

- **Code** builds the seed queries (topic family x region x audience x source x time window). They are used on day 1 only.
- **The web** supplies everything after that. The model only extracts names and links from pages it fetched; it never invents what to look at.
- **Code** decides what counts as new. The model never judges novelty.

## Crawl decision tree (per URL)

0. **Pick next.** Take the highest score from the frontier: novelty x expected value x re-check due. If the daily Exa or token budget is spent, or the per-domain rate limit is hit, sleep.
1. **Seen?** If the canonical URL is known and no re-check is due, skip it. If a re-check is due, go to step 6.
2. **Allowed?** Reddit goes through Arctic Shift, X through grok, GitHub through the API. A login wall, a captcha or bot challenge, or a robots.txt disallow means mark and skip. Never fight a wall.
3. **Fetch, cheapest first.** RSS, sitemap or API first, then a plain fetch, then Exa contents. After 3 failures, mark the URL dead.
4. **Injection scan.** If the page is suspicious, quarantine it. A quarantined page never adds links or topics.
5. **Classify** (code heuristics first, the LLM only when unsure):
   - **5a Hub** (lists many similar items): save it as a hub, push its item links at high priority, and set a re-check timer.
   - **5b Item** (a single thing: an opportunity, a tool, a claim): extract a row, then match it on entity name + org + year.
     - New: save it, and alert if it is relevant and urgent.
     - Known but changed: report the change.
     - Known and the same: nothing.
   - **5c Article / news** (mentions things): extract names. Each unknown name gets one search, and the results go to the frontier.
   - **5d Junk** (off-topic, SEO spam, a copied article): the domain loses score. A domain that hits N junk pages gets blocked.
6. **Re-check = diff, not redo.** If the page is unchanged, double the interval. If it changed, send only the new rows to 5b and halve the interval.
7. **Yield.** Each hub, domain and query scores new useful items / fetches. High-yield sources get checked more often. Low-yield ones go dormant.
8. **Frontier dry?** Try, in order: Exa find-similar on the best hubs, then backlinks to the best hubs, then the name snowball.
9. **Prune.** There is no fixed depth limit. A branch keeps going while it keeps paying, and is cut after 3 dry hops. An off-topic score below the threshold means drop it (the drift guard).

## No-repeat locks

| Lock | Key |
|---|---|
| Query | normalized query + time window |
| Page | canonical URL + content hash (plus MinHash for reprints) |
| Thing | entity name + org + year |
| Claim | claim hash + near-duplicate match |
| Topic | embedding similarity (merge at >= 0.85, "deepen" at 0.7-0.85) |

Re-check timers per family: news 1 day, opportunities 14 days, competitors 30 days. A re-check looks for changes only. It does not research the page again.

## Roadmap

**First cut (~8 h):**
1. SQLite store (topics, pages, entities, claims, runs).
2. Frontier with the 5 locks and yield scoring.
3. Item extractor (name, amount, deadline, eligibility, link).
4. Digest of new and changed items only.
5. Fix the three known v0 bugs: the topic that is retried forever, the compile timeout and the empty digest channel.

**Then port** (see FEATURE_MATRIX.md section 3):
- From last30days: URL normalisation and near-duplicate removal, the delta store, and grok X with date validation.
- From hyperresearch: the citation quote check, the independence score, and resume with a spend cap.
- From research-queue: the dead-link check and the 429-pause retry.
- From nashsu/llm_wiki: page merge and duplicate pages.
- From deer-flow: the hard token budget.

## Risks

- Topic drift with no human approval. Mitigations: family weights, the off-topic threshold and yield pruning.
- Weak local models make false merges, so the deterministic gates run before the LLM.
- No embeddings endpoint has been verified on our proxy yet.
- Disk on Oracle is 97% full. `raw/` and `logs/` need retention.
