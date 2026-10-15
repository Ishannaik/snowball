# Snowball Goal-Driven Research Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Put a user-approved, versioned goal control plane above the existing Snowball acquisition and evidence engine so that research for a fully funded on-campus master's (the reference workflow) runs only under an approved contract and execution grant, proposes bounded typed hypotheses, qualifies candidates per constraint with honest assurance, and is driven from a writable, authenticated console.

**Architecture:** Four prompt layers (static system policy, immutable versioned contract, mutable versioned strategy, compiled per-task prompts). New modules tools/goal.py, tools/interview.py, tools/prompts.py, tools/planner.py, tools/qualify.py, tools/ledger.py and tools/console.py sit above the retained FetchRouter, LockEngine, TokenBudgetMiddleware, Store, occurrence leases and wiki compiler in tools/orchestrator.py and tools/wiki_compiler.py. SQLite remains the only state store; the console and the worker share it through WAL.

**Tech Stack:** Python 3.10+, stdlib http.server, sqlite3 and Decimal, existing curl_cffi and PyYAML, plain HTML/CSS/JS in tools/console.py, public AGY/OMP model routes and Exa. A public bounded native transport is adopted only after output-limit/usage conformance; opaque OMP accounting remains explicitly estimated and cannot enable hard provider-token mode. No new dependencies.

**Spec:** docs/superpowers/specs/2026-10-09-snowball-goal-harness-design.md

**Status:** User-approved for implementation on 2026-10-10; execution in progress, runtime acceptance pending. Fable 5.1 produced the initial design and plan; the integration review corrected them using independent UX/evidence audits and four Gemini 3.8 Flash source analyses. Unchecked tasks remain unimplemented. Design-schema smoke evidence does not mean runtime acceptance. Recommendation values require explicit contract/grant approval before acquisition.

## Global Constraints

- Preserve the CAST(strftime('%s','now') AS INTEGER) due comparison in SnowballOrchestrator.select_topics and the existing regression tests/test_orchestrator.py RuntimeBehaviorGates.test_topic_due_boundary_and_explicit_override byte for byte. Goal gating goes before select_topics, never inside it.
- Preserve SSRF deny list and pinning, robots, wall and access-denial policy, 429 cooldown, paid ceilings at zero, deterministic locks, substring guard, quote and number checks, injection quarantine and canary, private-topic isolation, occurrence leases, wiki human-edit preservation, retention and backups.
- Models stay tool-less; code validates every typed request and executes it; models never decide novelty and never see private profile values.
- No silent fallback engine for any role. Unavailable means deferred with a reason.
- No feature stubs: every new CLI command, API route and state transition in this plan is fully implemented and tested in its task.
- Remove obsolete code paths rather than keeping aliases; never delete evidence rows.
- Follow the _sibling import convention; never put tools/ on sys.path (tools/queue.py shadows the stdlib).
- SQLite connections are thread-affine: each HTTP request owns/closes its connection; network workers return results to the worker's owning writer thread. All compound mutations use Store.tx() (BEGIN IMMEDIATE). Concurrency tests use distinct connections to one temporary database, never one shared Store.
- In a parallel implementation wave, children skip build/lint/tests/formatters while shared edits are in flight. The integration owner runs targeted regressions and scenario smoke once the relevant slice settles, then one full offline suite after integration. Update docs for real changes; do not re-pin incidental wording/wiring tests.
- Do not enable or start snowball-loop.service on Oracle as part of this plan.

## File map

| Path | Status | Responsibility after this plan |
|---|---|---|
| tools/orchestrator.py | EXISTING | Store with ordered migrations; goal gate in run(); leased fetch, extract, review, evaluate tasks; observation pipeline; ReviewEngine; LocalPrivateEngine; outcomes; status |
| tools/goal.py | NEW | goal state machine, contracts, profile fields, grants, strategies, events, CAS, runnable_goal, binding_valid, redact |
| tools/interview.py | NEW | question bank, validators, next_question, compile_public_frame |
| tools/prompts.py | NEW | SYSTEM_POLICY, PROMPTS registry, typed public requests, compile_* and validate_public_request |
| tools/planner.py | NEW | gap_proposals, planner_proposals, validate_proposals, schedule |
| tools/qualify.py | NEW | FIELD_KEYS, coverage, derive_funding_packages, evaluate, contradictions, Decimal FX/cash flow, completion_status |
| tools/ledger.py | NEW | atomic reserve_batch, settle, mark_lost, headroom, GrantExhausted |
| tools/console.py | NEW | HTTP server, sessions, CSRF, origin, read and control API, embedded UI |
| tools/visualize.py | EXISTING | terminal view and read-model queries; server classes removed |
| tools/ask.py | EXISTING | human_note labelling; private attestation for the local model |
| tools/wiki_compiler.py | EXISTING | provenance per indexed region |
| snowball.py | EXISTING | goal, console commands; serve dispatches to console |
| config.env, config.oracle.env | EXISTING | REVIEW_*, PRIVATE_MODEL_*, CONSOLE_*, PLAN_* keys |
| systemd/snowball-dashboard.service | EXISTING | console command |
| tests/test_goal_control.py, tests/test_console_api.py, tests/test_privacy_projection.py, tests/test_planner_contracts.py, tests/test_qualification.py, tests/test_resource_ledger.py, tests/test_migration_v4.py, tests/test_frontier_priority.py | NEW | consumer-visible regressions per task |
| tests/test_migration_v4.py fixtures | NEW | Python builder creates a representative v3 database/raw/wiki bundle inside each test's TemporaryDirectory; no committed binary |
| docs/superpowers/specs/2026-10-09-snowball-goal-harness-design.md | NEW | the design |
| docs/SYSTEM_DESIGN.md, README.md, docs/PLAN.md, topics.highered.yml | EXISTING | amendments |

## Shared contracts (implemented by the named tasks)

```
# tools/goal.py (T1)
class VersionConflict(Exception): current_row_version: int
class IllegalTransition(Exception): from_state: str; to_state: str
class ValidationError(Exception): field: str; message: str
@dataclass Goal: id, slug, title, state, current_contract_version, current_strategy_version, epoch, row_version, revision_from_state, stop_reason
@dataclass ProfileField: key, value, status, provenance, visibility, row_version
@dataclass ContractVersion: goal_id, version, status, body, body_hash, profile_snapshot, profile_snapshot_hash, frame, frame_hash
@dataclass Grant: id, goal_id, contract_version, mode, state, limits, starts_at, expires_at, row_version
@dataclass RunnableGoal: goal, contract, grant, frame   # no raw private profile values
create_goal(store, slug, title, actor) -> Goal
record_answer(store, goal_id, field_key, value, confirm, visibility, expected_row_version, actor) -> ProfileField   # expected_row_version is the goal CAS; every draft edit advances it
propose_contract(store, goal_id, body, base_version, actor) -> ContractVersion
approve_contract(store, goal_id, version, body_hash, expected_row_version, actor, idempotency_key) -> Goal
grant_execution(store, goal_id, contract_version, mode, limits, expires_at, expected_row_version, actor, idempotency_key) -> Grant
transition(store, goal_id, to_state, reason, expected_row_version, actor, idempotency_key) -> Goal
runnable_goal(store, now_ts) -> RunnableGoal | None   # also returns reason via runnable_reason(store, now_ts) -> str
binding_valid(db, goal_id, contract_version, strategy_version, epoch) -> bool   # reject wrong contract/epoch; an admitted prior strategy remains valid unless explicitly cancelled
redact(value) -> str

# tools/interview.py (T2)
@dataclass Question: field_key, text, answer_type, required, visibility_default, depends_on: tuple[str, ...]
@dataclass PublicFrame: domain, public_objective, public_criteria, field_tags, regions, intake_windows, required_source_fields, frame_version
QUESTIONS: list[Question]
next_question(fields: dict[str, ProfileField]) -> Question | None
validate_answer(question, raw) -> object
compile_public_frame(contract_body: dict, approved_snapshot: ProfileSnapshot) -> PublicFrame   # whitelisted public projection only

# tools/prompts.py (T3)
@dataclass PublicSourceSnapshot: id, url, title, text, content_hash, source_cycle_label, applicability
@dataclass PublicModelRequest: frame, public_sources, public_metadata, role, prompt_version
@dataclass PublicSearchRequest: frame, query, public_anchor_ids
@dataclass CompiledPrompt: system, user, prompt_version, source_snapshot_ids, public_frame_hash
class PrivacyViolation(Exception): field_key: str
compile_extract_fields(frame, program, field_keys, public_source: PublicSourceSnapshot) -> CompiledPrompt
compile_review_observations(frame, observations, public_source: PublicSourceSnapshot) -> CompiledPrompt
compile_plan_hypotheses(frame, public_coverage_summary, exploration_budget) -> CompiledPrompt
validate_public_request(request: PublicModelRequest | PublicSearchRequest) -> None

# tools/ledger.py (T4)
class GrantExhausted(Exception): counter, used, ceiling, resets_at
reserve_batch(store, grant_id, operation_key, amounts: dict[str, int], binding) -> list[int]
settle(store, reservation_id, actual) -> None
mark_lost(store, reservation_id) -> None
headroom(store, grant_id, day) -> dict[str, int]

# tools/qualify.py (T5)
FIELD_KEYS: tuple[str, ...]
@dataclass ProgramCoverage: program_key, public_unknown_fields, public_contested_fields, stale
@dataclass Coverage: programs: list[ProgramCoverage]
@dataclass FundingPackage: key, program_key, cycle_label, award_keys, components, compatibility_basis_ids, cash_flow, basis_ids
@dataclass ConstraintEvaluation: program_key, package_key, constraint_key, status, basis, reason
@dataclass CompletionStatus: ready: bool, substate: str, detail: dict
coverage(store, goal_id, contract_version) -> Coverage
derive_funding_packages(store, program_key, cycle_label) -> list[FundingPackage]
evaluate(store, goal, contract_version, package_key, epoch) -> list[ConstraintEvaluation]   # loads the frozen profile locally
completion_status(store, goal) -> CompletionStatus

# tools/planner.py (T6)
@dataclass Proposal: kind, program_key, field_key, query, url, rationale, origin   # origin gap|hypothesis
@dataclass ValidatedProposal: proposal, query_id, page_id, decision, reason
@dataclass SchedulePlan: selected_query_ids, selected_page_ids, decision_ids, reservation_ids
gap_proposals(store, goal, coverage) -> list[Proposal]
planner_proposals(engine, frame, coverage, budget) -> list[Proposal]
validate_proposals(store, goal, proposals) -> list[ValidatedProposal]
schedule(store, goal, validated, wave_id) -> SchedulePlan

# tools/console.py (T7)
serve_console(host, port, db_path, allowed_origins: tuple[str, ...], cookie_secure: bool) -> None
```

These are interface inventories, not runnable Python class definitions. Implement fully typed dataclasses and real fixtures under the stated `_sibling` convention. `ProfileSnapshot` is the immutable contract-owned mapping defined in T1. The core does not hardcode the master's objective; this plan fully specifies the master's-funding profile rather than claiming unimplemented support for every domain.

## Tasks

### T0: Spec, config and documentation baseline

**Files:** create docs/superpowers/specs/2026-10-09-snowball-goal-harness-design.md; modify docs/SYSTEM_DESIGN.md, README.md, docs/PLAN.md, config.env, config.oracle.env, topics.highered.yml.
**Consumes:** approved design. **Produces:** a proposed policy/config contract and safe operator baseline. Apply behavioral spec/config cutovers only alongside their consuming implementation; do not publish a changed runtime claim before code works.

- [ ] Save the design markdown as the spec file. Add section 1.6 (goal control plane, amended I1, assurance levels) to docs/SYSTEM_DESIGN.md and mark sections 1.1, 2.1, 2.4, 2.5 host and 32M-token figures as historical with a pointer to README Oracle Deployment.
- [ ] Replace docs/SYSTEM_DESIGN.md section 7.5 tier 3 fail-closed sentence with the assurance table from the spec section 6.2 and 8.3.
- [ ] Document the REVIEW_*, PLAN_*, PRIVATE_MODEL_* and CONSOLE_* keys/roles from the spec. Keep existing consumers/config working until T3–T6's integrated cutover; then remove obsolete GROK_VERIFIER_*/INDIA keys with all callers/tests, retain only GROK_X_* for acquisition, and install Oracle's truthful Gemini/Claude role IDs and Origin. Private model settings stay unset until an attested isolated local daemon exists.
- [ ] Add a header comment to topics.highered.yml stating the seed focus text is historical draft context (nationality and 2027 are not approved facts) and that goal seeds compile from the approved frame.
- [ ] README separates proposed commands/behavior from current runtime until the matching task is implemented. The Oracle operator baseline keeps research disabled; this plan never starts it implicitly.

**Acceptance:** documentation states the proposed I1 change and cutover boundary, without disabling existing configuration ahead of its consumers. Obsolete configuration is removed at the integrated code cutover, not by a premature text-only stage.

### T1: Migration v4 and the goal control plane store

**Files:** modify tools/orchestrator.py (Store schema/migration entry points and Config), snowball.py (explicit migrate command), tools/retention.py (bundle/restore and reference protection); create tools/goal.py, tests/test_migration_v4.py, tests/test_goal_control.py; extend tests/test_knowledge_ops.py.
**Consumes:** existing Store.tx, schema v3. **Produces:** explicit ordered migrations, immutable profile snapshots, goal/version associations, safe per-thread connection entry points, CAS/idempotency and runnable_goal.

- [ ] Separate fresh-database initialization, explicit ordered migration and connect-existing/schema-check paths. A status read, CLI inspection or console request never migrates. Preserve existing v2/v3 data semantics and IDs; version-gate the currently unconditional v2 migration.
- [ ] Build a consistent cutover bundle in the existing `data/backup` convention: VACUUM snapshot, referenced raw/source bytes with SHA-256 manifest, and human-edited wiki files. Refuse insufficient disk or failed manifest verification before changing schema. With services stopped, set foreign_keys=OFF before BEGIN, rebuild only enum-constrained tables that need it, preserve indexes/triggers/FTS behavior, apply additive columns elsewhere, create spec tables and goal_pages/goal_queries/goal_topics associations, and run foreign_key_check/quick_check before updating schema_version. Restore foreign_keys=ON in finally. Record the minimum successful bundle needed for rollback.
- [ ] Implement the exact transition table and frozen profile snapshots. All draft-field edits use goal CAS and advance row_version. Approval hashes cover body, confirmed values, provenance and visibility. Atomic idempotency stores actor/operation/key/request hash, status and response; conflicting reuse raises IdempotencyConflict. Revision cancels scheduling, approval revokes old grants, and cancellation never auto-resumes. `pausing` exposes outstanding attempts until settled/fenced.
- [ ] Create model-call attribution storage with explicit actual/estimated/legacy usage semantics. Preserve historical data without inventing a provider. Prepare configuration fields as needed, but perform engine/caller/config removal together in T3–T6's owning integration slice using available LSP references, or a documented search if no server exists.
- [ ] Migration regressions build a representative populated v3 DB/raw/wiki bundle in a temporary directory, call the explicit migration command, and prove all rows/IDs, relevant FK/index/trigger/FTS behavior, legacy provenance and pinned source bytes survive. Force backup/migration failure and prove no version advance; a second explicit invocation is idempotent. Status/connect-existing refuses old schema without writing.
- [ ] Goal regressions cover confirmation, missing required scope, wrong/stale approval hash, draft-edit races, snapshot immutability across revisions, grant replacement/mode, start, pausing/drain, explicit resume, safe revision cancellation, stale evaluations, stop/revocation and same-key/different-payload conflicts. Existing code-mode or CLI topic overrides cannot bypass the gate.

**Consumer regression:** approve v1 with a private zero upfront threshold; revise and edit the draft to a nonzero threshold and different sharing policy. Until v2 is explicitly reviewed and approved, v1 evaluation and its public frame are unchanged. A replay of the accepted approval succeeds; reuse of its key with a different request conflicts. Cancelling the revision cannot resume the worker.

**Commands:** python -m unittest tests.test_migration_v4 tests.test_goal_control -v. The integration owner runs the full suite after dependent callers and fixtures settle.

### T2: Interview, profile privacy and public frame

**Files:** create tools/interview.py, tests/test_privacy_projection.py; modify tools/goal.py (snapshot construction), snowball.py (goal new, show, interview, propose, diff, approve, grant, pause, resume, stop, revise, replan).
**Consumes:** T1 store and transitions. **Produces:** question bank, typed validators, PublicFrame, CLI goal commands.

- [ ] Implement the question groups, private defaults and dependency ordering in spec 5.1. Monetary input uses Decimal or integer minor units with currency; academic criteria preserve grading scale, credentials and evidence-backed equivalence rather than guessed conversions. Missing material funding/intake/scope values remain unknown. A broad public research scope requires an explicit acknowledgement; unknown is not silently “worldwide”.
- [ ] `compile_public_frame` reads the frozen snapshot and an explicit field whitelist. Degree/modality come from the approved typed profile; generic code cannot hardcode masters/on_campus. Public frame hashing covers only public projected data and its schema version, not private values or the private contract hash.
- [ ] `goal propose --from-fields` freezes confirmed values, provenance and visibility, builds criterion/profile references, computes the reviewed body+snapshot hash and the separate public frame. Show the actual proposed diff and incomplete requirements before approval.
- [ ] Implement each goal CLI command with the specified CAS/idempotency fields and structured exit codes. Interview is genuinely back-and-forth, persists answers/confirmation and permits revision only in draft states. Private entry uses non-echo input and is not repeated in logs or JSON diagnostics. No history import silently confirms a field.
- [ ] Regressions prove unconfirmed history cannot lock a field, missing required scope blocks proposal, and frozen public projection omits private values/hashes even when coincidental public numbers match them. Prompt-boundary tests are added with the real T3 compiler, not a hook stub.

**Consumer regression:** an explicitly approved public master's/on-campus frame is compiled from a snapshot with a private `max_upfront_cash={amount:0,currency:INR}`. The frame contains the public criteria but no finance/profile fields or private hashes. A draft privacy toggle cannot change an already approved frame.

**Commands:** python -m unittest tests.test_privacy_projection -v; python snowball.py goal new --slug t --title t --db /tmp/t.db; python snowball.py goal interview --db /tmp/t.db.

### T3: Prompt registry, engines and truthful attribution

**Files:** create tools/prompts.py; modify tools/orchestrator.py (Engine classes, EXTRACT_SYSTEM and VERIFY_SYSTEM removal, TokenBudgetMiddleware spend rows, extract_engine, verify_engine, extract_page_entities), tools/ask.py (query_local_model), tools/eval_gates.py (diagnostics for review and private endpoints); extend tests/test_orchestrator.py.
**Consumes:** T1 Config/model-call records; T2 PublicFrame. **Produces:** real typed compiled prompts, ReviewEngine, explicitly bounded/estimated public transport, LocalPrivateEngine without silent fallback.

- [ ] Implement the short versioned system policy, PROMPTS registry and compilers accepting PublicSourceSnapshot/PublicFrame/public metadata only. Fence untrusted source text. `validate_public_request` rejects private-profile/human-note provenance and unsupported schema before dispatch; do not scan every numeric private value against public source text.
- [ ] Make the public engine boundary accept the typed public request and validate provenance/schema itself; no unvalidated raw-string engine bypass. Compile source context only from explicit public snapshots, not caller-supplied private dictionaries.
- [ ] Cut over GrokAdversarialVerifier to ReviewEngine and all callers/config/tests with no compatibility alias. Preserve GROK_X_* solely for public X acquisition. Attribution is per-call and records requested/reported model and actual vs estimated/legacy usage; an availability failure does not become a successful review.
- [ ] Prove the selected public adapter forwards its configured output limit and parses actual upstream usage, including system/reasoning accounting when the provider exposes it. If opaque OMP cannot supply a defensible bound, block hard provider-token mode and display estimated usage; never claim a hard billing ceiling. Native AGY conformance is a recorded throwaway scenario before production use, not an assumed proxy capability.
- [ ] Replace LocalOrnithFallback with one explicitly configured LocalPrivateEngine. Block the known public AGY proxy and all configured public transports. Loopback is not proof of local inference: private interpretation requires operator attestation plus documented daemon egress isolation; otherwise retain `unknown/local_model_required`. Deterministic private evaluation needs no model.
- [ ] Extraction unavailable in auto mode defers without a local/cloud substitute. Remove the global INDIA relevance penalty; personal fit belongs in the per-goal frozen-profile evaluator.
- [ ] `ask` refuses an unattested private model request visibly; only an explicitly requested `--no-model` path performs non-model retrieval. No silent downgrade. Diagnostics report endpoint role, availability and usage mode without secrets.
- [ ] Consumer regressions cover unavailable extraction/review with honest task/assurance states; a private zero/GPA must not reject a public zero-fee/GPA page, while a request object carrying private provenance is refused. Repeated/mixed-model calls retain separate attribution, and unknown/unsupported usage modes cannot claim a hard cap.

**Consumer regression:** submit a public source saying “Tuition is 0 for the 2027 cohort” while the private frozen budget is zero. Extraction can process that source, but an attempted private-profile payload is rejected before dispatch. With the reviewer unavailable the evidence stays pending/unknown, never qualified.

**Commands:** python -m unittest tests.test_orchestrator -v; python snowball.py diagnostics --json.

### T4: Resource ledger and leased fetch and commit

**Files:** create tools/ledger.py, tests/test_resource_ledger.py; modify tools/orchestrator.py (wave1_fetch, record_fetch, wave4_commit, reap, new_task, claim_task, DomainBuckets).
**Consumes:** T1 execution_grants and tasks binding columns. **Produces:** atomic reservations, leased fetch and commit, lost accounting, domain in-flight cap.

- [ ] Implement reserve_batch/settle/mark_lost/headroom with current goal/contract/epoch and active-grant checks in the same transaction. Reserve all operation counters, shared total and role model allowances atomically. Key attempts by stable operation identity; settlement/replay is idempotent. Day and lifetime windows and queued/visited unique-node accounting are distinct; a replacement grant does not refill the lifetime ceiling.
- [ ] Scoped aggregate counters are goal/day, goal/lifetime and shared model-role totals, not reset by contract version. Freeze absolute lifetime ceilings in the approved contract and preserve accumulated usage through revisions. A human-approved larger ceiling may increase headroom; new approval does not erase earlier usage.
- [ ] Fetch admission atomically claims a leased task, reserves attempts/pages/rechecks and acquires the persisted domain slot before network I/O. Network work owns no shared Store; it returns raw results for a lease-checked commit on the owner thread. Redirects and retries consume their defined allowance; every acquisition/expansion/replay path uses the same gate.
- [ ] A late result may store correctly scoped immutable raw evidence and conservative usage, but cannot update current-goal evaluations or frontier bindings. Retain origin strategy metadata; replan alone does not reject previously admitted work with the same contract/epoch. No transaction remains open during external I/O.
- [ ] Reaping/timeout marks ambiguous dispatched attempts lost without refund; provably undispatched work can release its bundle. An actual usage overrun halts further admissions and is reported, never silently called “within budget”. Pause drains/fences in-flight calls before showing paused.
- [ ] Node associations enforce unique queued/visited and depth ceilings across seeds, links, hub/name expansion, redirects, rechecks and replay. Reservations are all-or-nothing; admission failure records a code reason without half-consuming unrelated counters.
- [ ] Consumer regressions use separate SQLite connections in eight concurrent workers against a five-admission ceiling; exactly five operations are admitted, with no duplicate charge on settlement. Cover a partially insufficient bundle, unknown outage usage, day rollover while calls remain in flight, lifetime ceilings after grant renewal, and stale-epoch results that retain raw bytes but cannot qualify.

**Consumer regression:** a file-backed temporary DB is migrated once, then each thread opens/closes its own connect-existing Store and submits a unique operation key. Five of eight attempts can reserve an allowance of five. Replaying one admitted key cannot increase usage; a sixth new operation is rejected even if the goal is given a replacement grant with the same lifetime limit.

**Commands:** python -m unittest tests.test_resource_ledger -v.

### T5: Evidence model and qualification

**Files:** create tools/qualify.py, tests/test_qualification.py; modify tools/orchestrator.py (wave2_extract, wave3_verify, wave4_commit, new evaluate task kind), tools/wiki_compiler.py (observation sections and provenance), tools/ask.py (human_note labelling); extend tests/test_knowledge_ops.py.
**Consumes:** T3 compiled prompts, T4 leases. **Produces:** programs, snapshots, observations with assurance, funding packages, constraint evaluations, contradictions, FX, completion status.

- [ ] Successful fetches preserve exact immutable UTF-8 text bytes, full SHA-256 and format/span metadata in source_snapshots. Source cycle/effective scope is evidenced metadata or unknown, never copied from the requested intake. Unchanged bytes can reuse a snapshot; changes preserve history and invalidate affected current observations.
- [ ] Extraction emits subject/award/intake-bound observations. Validate field keys, exact quote spans and numbers/units/periods against the immutable snapshot; map chunks to original offsets. Store code_checked assurance, model/prompt versions and task bindings. Source prose cannot define or overwrite criteria.
- [ ] Review records positive keep as reviewed_kept, drop as reviewed_dropped; missing/malformed/outage/budget verdicts remain pending/unavailable. Mechanical quote checks alone never qualify evidence. Keep raw rejected/contradicted observations available for audit.
- [ ] Build separate FundingPackage records for incompatible schemes. Stacking requires sourced compatibility. Decimal/minor-unit math compares the same period/currency with sourced FX; missing period, grading equivalence, timing or policy applicability is unknown. Loans cannot supply grant coverage. Preserve competitive/RA/TA/renewal conditions even when accepted. Compute total coverage and peak own-cash liquidity separately, without adding proof-of-funds to fees or double-counting credited deposits.
- [ ] Evaluate each package against the immutable approved snapshot and current epoch. Generic control code uses typed profile values rather than global master's constants. A PhD is not a match; do not execute the historical PhD alternative seed without separate user-approved scope.
- [ ] Wiki code-owned observation regions are regenerated without touching human edits. Human notes remain labelled annotations, never qualified source evidence. Update retention to pin referenced current/historical snapshots and legacy evidence; do not rmtree a dated directory containing pinned bytes.
- [ ] Regressions cover unknown/rejected evidence, every source-to-subject/award/cycle binding, incompatible awards, credited deposit/proof-of-funds cash flow, reimbursement timing, period/currency/FX gaps, unresolved conflicting official sources, a 2026 announcement explicitly for 2027, a stale call newly retrieved, and full negative coverage that is not research_ready. Positive readiness needs the approved number of genuine matches, not merely evaluated candidates.

**Consumer regression:** program P offers award A with tuition-only coverage and award B with stipend-only coverage; their terms explicitly prohibit combining them. No full-funding package may combine A and B. Separately, a deposit credited toward a fee and a proof-of-funds balance requirement cannot be blindly added together to manufacture or reject an upfront-cash fit.

**Commands:** python -m unittest tests.test_qualification tests.test_knowledge_ops -v.

### T6: Planner, frontier priority and the goal-gated wave

**Files:** create tools/planner.py, tests/test_planner_contracts.py, tests/test_frontier_priority.py; modify tools/orchestrator.py (run, supervise, wave0_kb_first, pop_frontier, refill_frontier, seed_from_yaml, status), tools/goal.py (strategy ledger helpers).
**Consumes:** T1 runnable_goal, T3 compile_plan_hypotheses, T4 ledger, T5 coverage. **Produces:** goal-gated wave, gap and hypothesis queries, persisted decisions, outcomes and substates, strategy versions.

- [ ] Gate admissions before seeding, manual --topic and pending replay. Dry-run uses an isolated copy, makes no calls and explains the same blockers. Adopt public topics through goal_topics associations without overwriting global owners; candidate/alternative scope must already be approved.
- [ ] Generate code-owned public gap templates and bounded model hypotheses. Validate schema, approved scope/anchors, seen URLs, public provenance, cooldown and deterministic dedupe. Persist model rationale separately from authoritative code reasons. Schedule shared sources/queries through per-goal/version associations and atomic resource bundles.
- [ ] Implement hard eligibility and priority per spec 6.4, with novelty only a tie-breaker and bounded exploration. Store a decision and score components for each considered candidate; do not promote a model's claimed novelty.
- [ ] Persist the monotonic strategy ledger after each bounded wave. Replan changes tactics, never contract/profile/criteria. A canonical work/coverage fingerprint excludes free-form rationale, timestamps and strategy numbers; repeated work with no code-measured evidence progress yields stalled and an operator-visible decision, not an endless prompt-rewrite loop.
- [ ] Research-ready uses the positive package/constraint predicate. Exhaustion, blocked access, unknown intake/eligibility, no-match-among-checked or stalled remain honest partial outcomes. Monitoring needs separate explicit mode/grant consent. Supervisor idles safely for nonrunnable states.
- [ ] Regressions exercise invented/out-of-scope URLs, absent public anchors, private-provenance requests, duplicate hypotheses, caps, shared sources supporting two goals, and canonical repeated-work stalls without rejecting productive work on distinct targets. Contract criteria/hash cannot change through replanning.
- [ ] tests/test_frontier_priority.py: an unknown-field official-domain page outranks a novel forum page; exploration capped at the share; a paused domain yields deferred with the pause time in the reason; a stale epoch node is dropped; a topic inside its recheck window still allows a gap query for its program (cooldown separation); the existing due test continues to pass.

**Consumer regression:** a source page is associated with two approved goals at different per-goal depths; scoring and evaluation for one cannot overwrite the other. A proposed URL not present in public discoveries is dropped. Repeated source/query work without new supported facts cannot be marked progress merely because the strategy version increments.

**Commands:** python -m unittest tests.test_planner_contracts tests.test_frontier_priority tests.test_orchestrator -v; python snowball.py run-once --dry-run --json --db /tmp/t.db.

### T7: Console server, sessions and control API

**Files:** create tools/console.py, tests/test_console_api.py; modify tools/visualize.py (remove DashboardServer, DashboardHandler, serve_dashboard, STATIC_ROUTES, API_ROUTES; keep query functions and terminal view), snowball.py (serve to console; console token command), systemd/snowball-dashboard.service.
**Consumes:** T1 goal functions, T2 interview, T5 coverage and evaluations, T6 decisions and status. **Produces:** authenticated writable API and read API on one stdlib server.

- [ ] Implement ThreadingHTTPServer with per-request read-only or short-lived writable connection factories; every handler closes its connection in finally. Assert current schema without migrations. No shared writable Store or cross-thread SQLite connection; control binds loopback.
- [ ] Bootstrap checks Host/Origin/body/schema and a bounded unauthenticated login rate, then atomically consumes the one-use token to create the Secure/HttpOnly/SameSite session. It is the explicit exception to existing-session/CSRF requirements. All other writes, including logout, require session+CSRF+Origin plus body/rate/CAS checks. Never accept forwarded identity/origin headers as authority.
- [ ] All new goal/private-derived read routes require the operator session, not merely interview/contract values. Preserve only public-safe legacy read projections, excluding private counts and goal metadata. Use route/actor-scoped idempotency with request hash and stored result/status; changed-payload replay conflicts.
- [ ] Implement the spec routes and structured error semantics. Private fields are returned only in authenticated interview/contract views; activity/evidence/shortlist are also authenticated and do not expose thresholds through public handlers.
- [ ] Preserve CSP, COOP, CORP, nosniff, no-referrer/no-store and safe source-link rendering; untrusted content cannot become HTML or a control instruction.
- [ ] Real HTTP regressions cover unauthenticated new reads/writes, concurrent bootstrap reuse, missing/foreign Origin independent of rewritten Host, stale approval/CAS, private-count isolation, same-key/same-request replay and changed-payload conflict, concurrent controls with separate connections, revoked/expired sessions and source XSS. Do not test incidental copy or static-file forwarding.

**Consumer regression:** two simultaneous logins reuse one bootstrap token; exactly one session is created. A logged-in mutation with a foreign Origin is denied despite the valid loopback Host. Reusing an accepted approval key with a changed hash conflicts. Anonymous `/api/status` cannot reveal private entities or per-goal fit counts.

**Commands:** python -m unittest tests.test_console_api -v; python snowball.py console token --ttl 600 --db /tmp/t.db; python snowball.py serve --port 0 --db /tmp/t.db.

### T8: Console UI

**Files:** modify tools/console.py (embedded HTML, CSS, JS); update API contract regressions only where consumer-visible behavior changes.
**Consumes:** T7 API. **Produces:** the five views with real empty, paused, blocked, outage, no-match, unknown and pending states.

- [ ] HTML skeleton with tabs Goal, Shortlist, Activity, Evidence, Graph (Graph last); sign-in dialog; skip link; aria-live status region; existing tab keyboard model.
- [ ] CSS: reuse the existing tokens and add --met, --unmet, --unknown, --conditional, --contested; 8 px scale; responsive breakpoints at 720 px and 1200 px; reduced-motion rule; focus-visible outline.
- [ ] Plain JS drives the real authenticated API: interview/confirmation, immutable contract diff/hash, execution grant and explicit state transitions. Handle 401 sign-in, 409 reload of the changed contract instead of blind automatic retry, and user-visible outage/error states. Show actual leased/in-flight work while pausing; never render an immediate paused claim.
- [ ] Distinguish no goal, needs information, proposed, running/pausing/paused, blocked, stalled, exhausted, reviewer unavailable, unknown/conditional/contested, no match among checked and research-ready-not-an-offer. Undated is unknown deadline, not open. No demo/fake rows or post-hoc “verified funding” labels.
- [ ] Verify the actual browser surface and keyboard/mobile layout using the recorded scenarios. Do not create permanent tests for copied HTML/CSS/JS, wording or incidental UI defaults. Existing contracts must still protect authorization, provenance and private-data boundaries.

**Commands:** python -m unittest tests.test_console_api -v; manual browser check recorded as scenario evidence B01 to B06 in section Evidence.

### T9: Supervisor, systemd, status and operations

**Files:** modify tools/orchestrator.py (supervise/status/print_status), service templates and docs; update tools/retention.py reference-pin/bundle integration and tools/eval_gates.py operational diagnostics; extend tests/test_e2e_full.py and tests/test_process_control.py.
**Consumes:** T6 outcomes, T7 server. **Produces:** idle-safe supervisor, truthful status, operator docs.

- [ ] supervise handles the new outcomes with bounded sleeps; STOP file semantics unchanged; a KeyboardInterrupt or STOP leaves goal state untouched.
- [ ] status and print_status show goal slug and state, contract version, grant expiry, headroom per counter, substate and reason, review and private endpoint availability by role and model id.
- [ ] systemd dashboard unit: Description Snowball console, ExecStart snowball.py serve (same flags); keep ReadWritePaths data; document CONSOLE_* in the environment file reference. The loop unit is not enabled by this plan.
- [ ] tests/test_e2e_full.py: supervisor outcome no-runnable-goal when no goal exists; goal-paused when paused; research-ready idle; kill-and-restart scenario leaves zero stale commits and one lost reservation.
- [ ] README Oracle section: cutover steps from spec 10.7 verbatim, with the note that enabling snowball-loop is a separate decision.

**Commands:** python -m unittest tests.test_e2e_full tests.test_process_control -v; python snowball.py status --json --db /tmp/t.db.

### T10: Data-preserving cutover rehearsal and scenario evidence

**Files:** create docs/superpowers/evidence/2026-goal-harness-scenarios.md (redacted); modify data/gate-evidence.jsonl only through the existing ledger tool.
**Consumes:** all tasks. **Produces:** recorded CLI, browser and Oracle scenario evidence, separate from the 59 legacy gates.

- [ ] Restore a complete DB/source/wiki bundle to an isolated directory, invoke the explicit migration command, and verify rows, IDs, FK/integrity/FTS behavior, quote spans, raw references and human edits before and after. Rehearse rollback; never open a copied DB expecting an implicit migration.
- [ ] CLI scenarios C01–C06: draft/interview, incomplete-goal rejection, immutable proposal/diff/approval, wrong hash/CAS conflict, separate execution grant and dry-run with zero external calls. Redact all profile/credential output.
- [ ] Browser scenarios B01–B06: bootstrap/sign-in, no-goal interview, review/diff approval, real unknown/conditional matrices/evidence, pausing/in-flight/paused controls, empty-vs-blocked graph and mobile/keyboard interaction. Verify auth/Origin rejection separately against the same server; no visual evidence is inferred from API tests.
- [ ] Oracle O01–O03 only after the user approves the goal, limits and this bounded live smoke: keep the service disabled, run one manual wave with a four-page cap, observe current evidence/decisions/accounting and stop. Without that approval mark live research not exercised; finish offline/isolated rehearsal. Verify stale callbacks cannot qualify and grant revocation leaves idle behavior. No applications or outreach.
- [ ] Record each scenario with command, exit code, timestamps and redacted artifact path; mark anything not run as blocked, never as passed.

**Commands:** python -m unittest discover -s tests -p 'test_*.py'; ruff check .; .venv/bin/python snowball.py status --db data/highered.db --json; curl -i -X POST -H 'Origin: https://evil.example' https://snowball.ishannaik.com/api/goals/1/transition.

## Dependencies and ownership

- T0 approval/baseline first, T1 migration/store next. T2 and T4 can run independently after T1; T3 requires T2's real public projection. T5 follows T3/T4; T6 follows T5; T7 requires the integrated goal/interview/qualification/planner APIs; T8 follows T7; T9 follows T6/T7; T10 is the final rehearsal/acceptance phase.
- One integration owner edits tools/orchestrator.py, shared migration/config/entry points and interfaces. Other agents own disjoint new modules and coordinate only through the declared contracts. Use worktrees or disjoint ownership; never parallel-edit the shared Store.
- Skip checks while sibling edits are in flight. The owner runs targeted red/green regressions and actual scenario smoke after the slice settles; one full offline suite and lint after integration. No half-built module, stub or inferred browser pass counts as a completed task.

## Evidence policy

Unit tests are offline and deterministic. Scenario evidence carries commands, exit codes and timestamps. A green suite is not a scenario pass. Private values are redacted from every artifact. The legacy gate ledger is untouched by this plan except where a task explicitly records a new blocked or verified row.
