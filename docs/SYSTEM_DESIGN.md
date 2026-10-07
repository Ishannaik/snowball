# Snowball System Design Specification

**Version:** 1.0 (definitive)
**Date:** 2026-10-06
**Repo:** `github.com/Ishannaik/snowball` (private), checkout at `~/repos/snowball`
**Supersedes:** `DESIGN.md` (crawl tree, v0), `PLAN.md` (feature list), both kept as history.
**Inputs:** the v0 fork, the 8-agent competitor audit in `COMPETITOR_DEEP_DIVE.md` and `FEATURE_MATRIX.md`, and the host facts measured on 2026-10-06 (goku: aarch64, 4 cores, 11.8 GB available RAM, 84 GB free disk, SQLite 3.45.1; oracle: SQLite 3.45.1, curl_cffi 0.15.0 with `CurlOpt.RESOLVE`, default impersonation profile `chrome146`).

---

## Table of contents

1. [Executive summary and foundational invariants](#1-executive-summary-and-foundational-invariants)
2. [End-to-end system architecture](#2-end-to-end-system-architecture)
3. [The 5-layer deterministic no-repeat lock engine](#3-the-5-layer-deterministic-no-repeat-lock-engine)
4. [The 3-tier scraper waterfall and pluggable anti-ban design](#4-the-3-tier-scraper-waterfall-and-pluggable-anti-ban-design)
5. [SQLite schema](#5-sqlite-schema)
6. [Multi-subagent orchestration topology](#6-multi-subagent-orchestration-topology)
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

Snowball is a research agent that runs 24 hours a day on the `goku` VPS. It builds and maintains its own knowledge base: a Markdown wiki that is the source of truth, plus a SQLite database that holds the crawl frontier, the no-repeat locks and a rebuildable full-text index. It looks for grants, hackathons, startup funding, free GPU and cloud credits, competitor moves and early "alpha" about AI tools. Nobody approves its work. Once a day it posts a Discord digest that contains only what is new or changed since the last digest.

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
| I1 | **Code seeds, the web supplies.** After day one, every query in `queries` has a `discovered_via` of `hub`, `name`, `backlink` or `similar`, never `llm`. | `queries.discovered_via` CHECK constraint, section 5 |
| I2 | **The LLM never judges novelty.** New / changed / same is decided by the lock engine before any model sees the item. | Section 3 pipeline order |
| I3 | **The model has no tools.** Text in, JSON out. Code fetches, code writes. Nothing a page says can execute. | `tools/llm.py`, section 6.5 |
| I4 | **Never fight a wall.** A login wall, captcha, Cloudflare challenge or robots disallow marks the page `walled` and the crawler moves on. No stealth browsers, no captcha solving. Paid offload is opt-in and budget-capped. | Section 4 |
| I5 | **Every extracted fact carries an evidence quote found verbatim in the fetched page.** A fact without a passing quote is dropped, not softened. | Section 7.4 |
| I6 | **Re-check means diff, not redo.** A due page is re-fetched, hashed, and only its new rows enter the pipeline. | Section 3.2, 4.6 |
| I7 | **Markdown is truth, SQLite indexes are rebuildable.** `wiki/` can rebuild `wiki_*` tables and FTS5 at any time. The frontier and lock tables are authoritative and backed up. | Section 8.1 |
| I8 | **Budgets sleep, they never fail.** Exa reserve, token budget, per-domain rate, paid fetch cap and memory cap all pause work and resume when the window opens. | Section 6.4 |
| I9 | **Zero PyTorch, zero FAISS, zero headless browser by default.** Pure Python, SQLite FTS5, BM25 and TF-IDF do the ranking. | Section 7.3, 3.5 |
| I10 | **Only new or changed reaches a human.** A digest line is an `entities` row with `reported_at IS NULL`, or an `entity_changes` row with `reported_at IS NULL`, and nothing else. | Section 8.6 |

### 1.5 Non-goals

- No solving captchas, no bypassing bot walls, no logins, no form fills.
- No crawling of leak, warez or credential sites.
- No acting on findings: Snowball never applies, posts or emails.
- No approval queue. Human feedback is optional and only adjusts scores.

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
  data/snowball.db       authoritative DB (WAL). Nightly `VACUUM INTO data/backup/snowball-YYYY-MM-DD.db`, keep 7
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
  STOP                   touch to pause the supervisor between waves
```

### 2.5 Network

- All LLM and Exa traffic leaves goku through one SSH tunnel to oracle, kept alive by `snowball-llm-tunnel.service` (`ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 127.0.0.1:8812:127.0.0.1:8812 -L 127.0.0.1:8791:127.0.0.1:8791 oracle-a1`, `Restart=always`). Nothing on oracle changes, and the proxy keeps listening on localhost only.
- `100.64.0.0/10` (the Tailscale CGNAT range) is in the SSRF deny list, so a crawled link can never reach a tailnet peer.
- Outbound crawl traffic uses goku's own IP. There is no proxy unless the operator sets one (section 4.4).

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
| Headless Chromium with stealth | 400–900 MB RSS per tab on aarch64; goku's supervisor is capped at 3 GB; one leaked browser froze oracle for 50 minutes on 2026-09-28 | Not used, not installed |
| Datacenter IP plus retries | Oracle and Hetzner ASNs trigger Cloudflare Turnstile regardless of client fingerprint | Detect and mark `walled`, never retry with tricks |
| Residential proxies | $3–8 per GB, $150–400 per month at our volume, for data that is published openly | Opt-in only, capped per day |
| Captcha solving | Breaks I4 and most sites' terms | Never |

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

When tier 1 returns `walled` or `outage` and the Exa reserve allows (`exa_budget.use_exa()` is true, meaning more than `RESERVE=3` live keys in the rotator), the collector calls the rotator's `/contents` endpoint for the URL. Exa fetches from its own infrastructure, so a Cloudflare wall against our ASN often does not apply. One call per URL per attempt; the result goes through the same safety gate and hash as a direct fetch. `fetches.route = 'exa_contents'`.

### 4.5 Tier 2: pluggable paid offload (opt-in, budget-guarded)

Disabled unless the operator sets `ZENROWS_API_KEY` or `RESIDENTIAL_PROXY_URL` in `.env`. When enabled, a page reaches tier 2 only if **all** of these hold:

```python
def paid_fetch_allowed(page, domain, budget) -> bool:
    return (
        CONFIG.paid_provider is not None
        and budget.paid_fetches_today < CONFIG.MAX_PAID_FETCHES_PER_DAY  # default 100
        and yield_score(page) >= CONFIG.PAID_FETCH_MIN_YIELD_SCORE  # default 0.30
        and page.predicted_kind in ("hub", "item")  # never pay for articles
        and domain.paid_failures < 3  # provider also walled → stop paying
        and page.wall_kind != "robots"  # a robots disallow is final, no paying around it
    )


def yield_score(page) -> float:
    # Laplace-smoothed new-useful-items per fetch of the best known evidence for this page
    src = page.parent_hub or page.domain
    return (src.new_items_total + 1) / (src.fetch_count + 2)
```

Providers:

| Provider | How | Cost accounting |
|---|---|---|
| ZenRows | `GET https://api.zenrows.com/v1/?apikey=…&url=…&js_render=false` first; `js_render=true&premium_proxy=true` only on a second attempt for a hub with `yield_score >= 0.5` | 1 credit basic, 5 js_render, 10 premium, 25 both; written to `paid_fetches.cost_units` |
| Residential proxy | curl_cffi `proxies={"https": RESIDENTIAL_PROXY_URL}` with the same impersonation | `paid_fetches.cost_units = ceil(bytes / 1 MB)` |

Every paid fetch writes a `paid_fetches` row before the request (so a crash still counts it) and updates `outcome` after. `snowball status` shows `paid_fetches_today / MAX` and credits consumed. When the day cap is hit, tier 2 is skipped silently and the page goes to tier 3.

### 4.6 Tier 3: walled, mark and move on

```sql
UPDATE pages SET status='walled', wall_kind=:kind, strikes=0,
       next_check_at = strftime('%s','now') + CASE WHEN walled_count = 0 THEN 30*86400 ELSE 90*86400 END,
       walled_count = walled_count + 1
 WHERE id=:page_id;
UPDATE domains SET walled_count = walled_count + 1 WHERE domain=:domain;
```

A walled page is re-checked after 30 days, then every 90 days, because walls come and go. A domain with `walled_count >= 10` and zero successful fetches is `status=blocked` and its pending pages are dropped from the frontier. No retries with different headers, no proxy rotation, no browser.

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

`grok -p` writes its full answer to stdout and then never exits. The collector launches it with `nohup … > /tmp/grok_<task>.log &`, captures `$!`, polls the log until the closing JSON bracket appears or 180 seconds pass, then `kill`s the PID. Every returned post is validated by Snowflake decoding (section 7.6). Posts outside the requested window, or whose ID fails to decode, are dropped before the safety gate.

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

- Nightly `VACUUM INTO 'data/backup/snowball-<date>.db'`, keep 7, from `snowball-retention.timer`.
- `PRAGMA quick_check` weekly; a failure stops the supervisor and posts to Discord.
- `wiki/` is a git repository committed after each wave (`git add wiki digests && git commit -qm "wave <id>: +N entities, M changes"`), so the truth layer has history independent of the DB.

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

**Tier 3, cross-model reject-only check (digest-bound items only):** for each entity about to enter a digest, Qwen 27B is asked, with the source quote and the row, "Does the quote support every field in the row? Answer yes or no and name the unsupported field." LLM judges have agreement bias, so this tier can only **reject** (set `status = known`, `reported_at = -1`, log the field); it can never promote something Tier 2 rejected. Fail-closed: a timeout or malformed answer leaves the item in the digest (Tier 2 already passed) and logs the miss.

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

Every X item from grok must have a status URL with a decodable ID whose timestamp lies in `[window_start - 1 day, now + 5 min]`. A claimed `created_at` more than 48 hours from the decoded time is a hallucination; the item is dropped and the task's `noise_count` increments. x.com cannot be verified from these boxes (it serves its SPA shell with HTTP 200 for any path), so the Snowflake check is the only verification and failing it is final.

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
- The frontier and lock tables are **not** derivable from the wiki and are backed up nightly (section 5.4).

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

Digest is always written to `digests/YYYY-MM-DD.md`; Discord posting is skipped when `DIGEST_CHANNEL_ID` is empty. Sends go over REST with the bridge bot token, never the MCP.

### 8.7 `snowball ask`

`snowball ask "<question>"`: FTS5 `MATCH` over `wiki_fts` (query sanitised: quotes balanced, operators escaped, hyperresearch PR #52), top 12 by `bm25(wiki_fts)`, page bodies filtered with section 7.3 to 25 chunks, one Qwen 27B call with the instruction to answer only from the chunks and cite `[path]` after each sentence. Sentences without a citation are dropped by code. Target under 30 s.

### 8.8 Retention

| Path | Policy |
|---|---|
| `raw/` | delete files older than 14 days; quarantined pages kept 30 days |
| `logs/` | 30 days |
| `data/backup/` | 7 nightly copies |
| `wiki/`, `data/snowball.db`, `digests/` | forever |

`snowball-retention.timer` also runs `VACUUM INTO`, the deadline-expiry job and `PRAGMA quick_check` weekly.

---

## 9. Milestone roadmap M0 to M4

Each milestone ships and runs on goku before the next starts. Every gate is a command with an expected result.

### M0: repo, design, plan (done)

Gate: this document and its predecessors are in `docs/`.

### M1: store, frontier, waterfall, page and query locks (about 10 h)

Builds: section 5 DDL and connection helper; seeder grid; scheduler pop; tiers 0, 1, 1b, 3 (tier 2 as code path behind config, untested until a key exists); outage/failure split; SSRF guard; injection gate; page lock 2a/2b/2c; query lock; `fetches` logging; `snowball status`; retention; SSH tunnel unit; `pip install curl_cffi trafilatura` on goku (aarch64 wheels verified at install).

| Gate | Command | Pass |
|---|---|---|
| Seed determinism | `snowball seed --dry-run \| sha1sum` twice | identical hashes, `SELECT COUNT(*) - COUNT(DISTINCT query_key) FROM queries` = 0 |
| 24 h soak | `systemctl --user status snowball-supervisor` after 24 h | active, zero restarts from crash in `journalctl` |
| Zero duplicate fetches | `SELECT COUNT(*) FROM fetches WHERE due = 0` | 0 |
| Same article, 3 URLs | feed `?utm_source=x`, `www.`, trailing slash variants | 1 `pages` row, `raw_urls` length 3, 1 `fetches` row |
| Route logging | `SELECT route, COUNT(*) FROM fetches GROUP BY 1` | every row has a route; reddit rows are `arctic` only |
| Wall handling | fetch a known Turnstile page | `status='walled'`, `wall_kind='challenge'`, exactly 1 `fetches` row after 24 h |
| Rate limit refund | mock 429 then 200 | `strikes` unchanged, `paused_until` set, page fetched after pause |
| 3 strikes | mock 404 ×3 | `status='dead'` |
| SSRF | queue `http://169.254.169.254/`, `http://100.65.187.17/`, a hostname resolving to 127.0.0.1 | 3 `fetches.outcome='ssrf_blocked'`, no socket opened (strace or mock) |
| Slug fixed point | property test, 10,000 random strings | `slug(slug(s)) == slug(s)` |
| Disk flat | `du -sh raw logs` on day 1 and day 8 | day-8 size within 20% of day-7 size |
| Budget sleep | set `LLM_TOKENS_PER_DAY=1` | supervisor logs `budget: llm` and keeps fetching; resumes next day |

### M2: classify, extract, entity lock, compiler, digest (about 10 h)

Builds: heuristics + LLM classify; BM25 filter; extraction schema and substring guard; Tier 1 + Tier 2 verification; entity lock with field diff; hub row diff; names queue → one search each; Python compiler with frontmatter union, 70% floor, D ≤ 1; digest and `reported_at`; yield counters; retire npm compiler.

| Gate | Command | Pass |
|---|---|---|
| Classifier accuracy | `snowball eval classify tests/pages_labelled_50.jsonl` | ≥ 85% (≥ 43 of 50) |
| Schema and quotes | `snowball eval extract tests/items_20.jsonl` | 100% of emitted rows validate; 100% of `evidence_quote` pass Tier 2 |
| Names from text only | inject a page with no proper nouns | `names` gains 0 rows; a planted known-from-training name absent from the text never appears |
| Deadline move | re-fetch a fixture with a changed deadline | exactly 1 `entity_changes` row (`field='deadline'`), 0 new entities, digest shows one "deadline moved" line |
| Hub diff | fixture hub with 20 rows, then 22 | 2 `hub_rows` added, 2 pages queued, `interval_sec` halved |
| Hub unchanged | re-fetch same hub | 0 new rows, `interval_sec` doubled, no LLM call (token counters unchanged) |
| First real digest | morning after enable | `digests/<date>.md` has ≥ 1 entity with link, reason, evidence; Discord message id stored |
| No repeat in digest | run digest twice | second run posts nothing; `digest_items` UNIQUE never violated |
| 70% floor | mock LLM returning a 200-char summary for a 1,000-char page | file contains old + addition, not the 200 chars |
| D ≤ 1 | graph A→B→C→D fixture, change A | B stale, C and D not stale; recompiling B marks nothing |
| Compile time cap | 500 dirty pages | compiler returns in ≤ 600 s and leaves the rest dirty |
| Reindex | `rm data/snowball.db-wiki-tables; snowball reindex` | `wiki_fts` count equals file count |

### M3: claim and topic locks, library-first, ask, alerts, grok X, refill (about 12 h)

Builds: claim lock with FTS5 candidates; topic lock (TF-IDF); drift guard; library-first search skip; `snowball ask`; urgent alerts; grok route with Snowflake validation and kill-after-output; Exa findSimilar and backlink refill; Tier 3 reject-only check.

| Gate | Command | Pass |
|---|---|---|
| Claim dedupe | same fact phrased 5 ways from 5 domains | 1 `claims` row, `n_sources = 5`, 4 `claim_variants` |
| Reprint not a source | syndicated copy fixture | `status='reprint'`, `n_sources` unchanged |
| Topic not duplicated | "free GPU credits" vs "cloud GPU credit programs" | cosine ≥ 0.30 (measured 0.64), `topics` count unchanged, 1 deepen task or 1 focus variant |
| Topic boundaries | labelled 30 pairs (10 same, 10 deepen, 10 new) | new-vs-not-new ≥ 95% agreement; same-vs-deepen ≥ 80% agreement |
| Drift guard | queue an off-topic finance page | `status='dropped'`, never in frontier again |
| Repeat rate | `snowball status` after 7 days | `< 5%` |
| Library-first | run the same topic twice | second run's `exa_calls` ≤ 30% of the first |
| Ask | `snowball ask "which grants close in December?"` | answer in < 30 s, every sentence cites a `[path]` |
| Urgent alert | entity with deadline in 5 days | exactly 1 alert message, `alerted_at` set, no second alert |
| grok | X query fixture with 1 fabricated ID | fabricated item dropped; grok PID gone after task |
| Refill | empty frontier fixture with 2 hubs | findSimilar + backlinks queue ≥ 1 page, else sleep 1800 logged |

### M4: quality ports and resilience (about 12 h)

Builds: dead-link check; `occurrence_seq` reaper hardening; tier 2 ZenRows with budget guard (tested with a real key, 5 calls); residential proxy path; feedback reactions; weekly summary; `quick_check`; backups.

| Gate | Command | Pass |
|---|---|---|
| Dead link | digest with a 404 link | line shows hub fallback, `link_dead = 1` |
| Kill -9 resume | `kill -9` mid-wave, restart | reaper re-queues ≤ 1 task per worker; `SELECT COUNT(*) FROM fetches WHERE due = 0` still 0; no duplicate `entities` |
| Stale worker | hold a claimed task past 600 s, then commit | commit updates 0 rows; task was re-run once |
| Paid cap | `MAX_PAID_FETCHES_PER_DAY=3` | 4th blocked page goes `walled` with `wall_kind='budget'`, `paid_fetches` has 3 rows with outcome |
| Paid yield gate | page with `yield_score 0.1` | tier 2 skipped, logged `paid: yield below 0.30` |
| Feedback | 👎 reaction on a digest line | `domains.feedback_score` decreases; frontier `value` for that domain lower next wave; no page dropped by feedback alone |
| Backup | `ls data/backup` after 8 nights | exactly 7 files |
| Every digest line verified | `snowball eval digest --since 7d` | 100% of lines have a Tier-2-passing quote and a live link or hub fallback |

Total about 44 hours of agent time. Each milestone is a PR; Greptile and CodeRabbit run before merge.

---

## 10. Cross-cutting failure modes and mitigations matrix

| # | Failure mode | Where it bit (audit evidence) | Snowball mitigation | Section |
|---|---|---|---|---|
| 1 | **Scraping blocks and bot bans** | Datacenter IP 403s on Reddit and X; challenge pages returned as HTTP 200; deprecated `.rss` endpoints (last30days) | Route Reddit to Arctic Shift, HN to Algolia, GitHub to API, X to grok; curl_cffi Chrome fingerprint at tier 1; body-signature wall detection; mark `walled` and move on; opt-in paid tier under a daily cap | 4.2–4.6 |
| 2 | **Egress SSRF and metadata exposure** | Agents following discovered links to `127.0.0.1`, RFC 1918, `169.254.169.254`; redirects bypassing checks (hyperresearch PR #53, #127, #130) | DNS pre-resolution with every answer checked, pinned via `CURLOPT_RESOLVE`, manual redirects re-validated, 5-hop cap, ports 80/443 only, Tailscale range denied | 7.1 |
| 3 | **Citation and quote hallucination** | Small models paraphrase quotes; LLM cite-checkers agree with themselves (hyperresearch `citecheck.py`) | Substring guard on names and quotes, Tier 1 numbers, Tier 2 verbatim with 0.90 window fallback, Tier 3 reject-only; drop, never soften | 7.4–7.5 |
| 4 | **SQLite concurrency deadlocks** | Multi-worker writes in WAL without write-lock sequencing (hyperresearch `sync.py`, LDR connection lifecycle lock) | One process, `BEGIN IMMEDIATE` everywhere, `busy_timeout=5000`, retry wrapper, UPSERT not `INSERT OR REPLACE` | 5, 6.3 |
| 5 | **Heavy ML dependency crashes** | PyTorch + FAISS, `faiss-cpu 1.14` SIGILL on ARM (LDR) | Zero Torch/FAISS. FTS5, BM25, TF-IDF, MinHash in pure Python; embeddings only as an optional endpoint behind a benchmark gate | 3.5, 7.3 |
| 6 | **Recursive context explosions and token exhaustion** | Unbounded follow-up trees; `GraphRecursionError` losing all findings (deer-flow PR #838, TokenBudgetMiddleware) | No depth limit but 3-dry-hop cut; per-task token budget with 80% synthesize-only and 100% commit-partial; adapter `MIN_TOKENS=16384` | 6.5, 6.7 |
| 7 | **Scheduler orphans and split-brain** | Crashed workers' tasks never reclaimed; late workers overwriting completed tasks (deer-flow `occurrence_seq`) | Monotonic `occurrence_seq` on claim, heartbeat, 600 s reaper, sequence-checked commit | 6.3 |
| 8 | **Outage counted as failure** | 429 burning retry attempts and killing good sources (research-queue) | Outage class pauses the domain with exponential backoff and refunds the attempt; failure class strikes; 3 strikes dead | 4.7 |
| 9 | **Compaction loss on page merge** | LLM merges summarising away 30–60% of a page; dropped frontmatter arrays (nashsu `page-merge.ts`) | Code owns frontmatter, Facts, Changes, Sources; LLM owns only Summary; `BODY_SHRINK_THRESHOLD=0.70` plus number-survival check; deterministic set union | 8.3–8.4 |
| 10 | **Staleness cascades** | One update marking a whole vault stale recursively (lucasastorian `propagate_staleness`) | `dirty` vs `stale`, propagation only from fact changes, exactly one hop, stale recompiles never propagate | 8.5 |
| 11 | **Duplicate work across parallel workers** | Parallel subagents re-scraping one URL (LDR PR #6759/#6760); sibling writers duplicating sections (GPTR PR #2158) | Page lock before fetch is the single-flight cache; `max 4 per domain` per wave; sibling fencing in extraction prompts | 3.2, 6.6 |
| 12 | **Hallucinated post dates from X** | grok inventing recent dates for old or nonexistent posts (last30days `grok_x.py`) | Snowflake decode `(id >> 22) + 1288834974657`, window check, 48 h mismatch rule, final because x.com cannot be verified from here | 7.6 |
| 13 | **Orphaned rows and slug collisions** | Note rows written before their parent, slugs that re-slug differently (hyperresearch PR #86, #126) | Foreign keys ON, parent-first writes in one transaction, `slug()` fixed-point property test, 80-char truncation | 3.3, 5 |
| 14 | **Credential leak on redirect** | `Authorization` forwarded cross-origin on 302 (last30days PR #1069) | Manual redirects drop auth and cookies when origin changes | 7.1 |
| 15 | **FTS5 query syntax injection** | User or page text breaking `MATCH` (hyperresearch PR #52) | Query sanitiser: balanced quotes, escaped operators, tokens only | 8.7 |
| 16 | **Empty report saved as success** | Search outage producing an empty but "successful" run (research-queue `MIN_REPORT_CHARS`) | A fetch with < 200 chars of text is a `failure`; a wave with 0 fetches ok and ≥ 10 outages is `status='aborted'` and does not advance any `next_check_at` | 4.3, 6.2 |
| 17 | **Prompt injection from crawled pages** | Pages instructing the agent to add topics or run commands | No tools; quarantine regexes; nonce-wrapped untrusted blocks; quarantined pages emit nothing; output secret scan | 7.2 |
| 18 | **Topic drift without approval** | Autonomous resolution PR abandoned for runaway drift (nashsu) | Family relevance floor, yield pruning, dry-hop cut, topics only from names and hubs | 3.5, 6.7 |
| 19 | **Memory and disk exhaustion on the host** | Leaked browsers and local model tests froze oracle (2026-09-28); oracle disk at 91–97% | No browsers; `MemoryMax=3G` with a 2.2 GB soft guard; all state on goku; retention and 5 GB disk guard | 2.4, 6.4, 8.8 |
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
| `MAX_PAID_FETCHES_PER_DAY` | 100 | tier 2 cap |
| `PAID_FETCH_MIN_YIELD_SCORE` | 0.30 | tier 2 gate |
| `DOMAIN_RATE_PER_MIN` | 6 | token bucket, burst 3 |
| `MAX_BODY_BYTES` | 5242880 | 5 MB |
| `RAW_RETENTION_DAYS` / `LOG_RETENTION_DAYS` | 14 / 30 | |
| `MEMORY_SOFT_CAP_MB` | 2200 | |
| `DISK_MIN_FREE_GB` | 5 | |
| `EMBEDDINGS_URL` | empty | optional; must pass the M3 topic benchmark before use |

`.env` (secrets, never committed): `QWEN_API_KEY` (required), `GH_TOKEN` (optional), `ZENROWS_API_KEY` (optional, enables tier 2), `RESIDENTIAL_PROXY_URL` (optional, enables tier 2). The Discord bot token is read from the bridge's own env file path, never copied.

`topics.yml`: `families` with `recheck_days`, `weight`, `relevance_floor`, `focus`; `seeds` with `id`, `family`, `focus`, optional `regions`, `audiences`. The seeder expands `family x region x audience x source x window` into `queries` with `discovered_via='seed'`.

## Appendix B: CLI reference

| Command | Does |
|---|---|
| `snowball seed [--dry-run]` | expand `topics.yml` into the seed grid; idempotent |
| `snowball run-once` | one wave, then exit (used by tests) |
| `snowball status` | frontier size by status, fetches/day by route, new/changed entities/day, repeat rate, budgets left, paid fetches used, dry cuts, memory |
| `snowball ask "<q>"` | library answer with citations |
| `snowball digest [--date]` | build and (if configured) post the digest |
| `snowball compile [--full]` | incremental compile, or full rebuild of `wiki/` from the DB |
| `snowball reindex` | rebuild `wiki_*` tables from `wiki/*.md` |
| `snowball retention` | prune raw/logs, backup, expire deadlines, quick_check |
| `snowball eval classify|extract|digest …` | milestone gate evaluators |

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
