I reviewed only the attached text and used no tools. I don't know the paths of existing modules other than the ones the attachment names, so I list those by role.

## 1. Architecture

**Rule: models propose and code decides.** Every model output is stored as a typed proposal. Only code commits it, and it does so inside a transaction that re-checks the binding, the privacy label and the ceilings.

| Layer | Contents | Trust |
|---|---|---|
| **Kernel (code)** | Contract validation and hashing, privacy labels and outbound-data checks, admission ledger, binding and fencing, snapshot and quote verification, checking results against their schemas, outcome state machine, working-context limits, auth/CAS/idempotency | Trusted. The same for every goal. Tested without models. |
| **Roles (models)** | Interviewer, brief drafter, planner, researcher, reviewer, ranker, synthesizer, judge | Untrusted proposers. Each role is an input/output schema, a versioned prompt with no domain content, a configured model, and a clearance. |
| **Goal package (data)** | Locked version: questions, answers, labels, rubric, public brief, recipients, source scope, ceilings, stopping policy | Approved by the user. This is the only input that changes per goal. |

| Decision | Proposed by | Decided by |
|---|---|---|
| Goal, rubric and brief content | Interviewer + user | User approval of the exact hash |
| Next research task | Planner | Code: scope, ceilings, leases, duplicate fingerprints |
| What a source says | Researcher | Code: snapshot, quote offsets and hash |
| Whether a claim holds | Reviewer | Code accepts only verdicts bound to a claim ID |
| Criterion status | Judge | Code checks the rows and computes rubric-met from them. The judge has no overall "met" field. |
| Working-context membership | Ranker | Code: size limits plus items that must stay (conflicts, gaps) |
| Who may see what | User | Code: data can go to a recipient only if its label is within that recipient's clearance |

**What makes it a harness rather than a one-goal app:**
1. Adding a goal means adding data rows. It never means editing code or prompts.
2. Rubric-met is a pure function of validated judge rows and the locked rubric. A domain-specific branch can't exist because the kernel receives no domain input.
3. Roles are set in configuration and their identity is recorded. Nothing is wired per goal.
4. Every role call is recorded and can be replayed, so the loop's behaviour can be tested without depending on model quality.
5. A test runs two unrelated goals through the same code from the first working end-to-end loop onward, not only at final acceptance.
6. Goals can only be extended with data: runtime questions, rubric, seeds, and labelled user guidance. No plugin code is ever loaded.

## 2. Improvements

### Blockers in the spec and contract

1. **The public rubric is the full rubric.** `build_version` sets `"public_rubric": rubric`, so private criteria reach public recipients.
   - *Fix:* give each criterion a label. Make `public_rubric` a list of criterion IDs the user selects, check that it is a subset of the rubric and that every selected criterion has an empty label, and put only those in the projection.
2. **`public_id` can be used to confirm guesses about private answers.**
   - It is a hash of `body_hash` and sits inside the projection, so it is also inside `public_hash`.
   - Many private answers are low-entropy (yes/no, small numbers). Anyone with the public ID can test guesses offline.
   - *Fix:* the store assigns a random `secrets.token_hex(16)` at approval and keeps the mapping on the server. `public_hash` covers public content only.
3. **There is no public brief artifact.** §3 and §9 require a reviewed brief, but the code just filters raw answers mechanically.
   - *Fix:* add `public_brief {text, derived_from[], label}`. Its label is the union of its sources' labels.
   - If that label isn't empty, approval needs an explicit disclosure approval bound to `digest(text)` for each recipient.
   - The projection becomes: brief, public criteria, ceilings and public seeds.
4. **A private/public flag can't express per-recipient permission.** Also, `recipients`, `source_scope` and `monitoring` are hashed without validation, and `source_scope` goes into the projection verbatim.
   - *Fix:* a label is a set of context-class IDs, and an empty set means public.
   - Every question, answer, criterion, brief and seed carries a label.
   - Recipients are `{id: {kind: model|search|fetch, attested, clearance[]}}`.
   - Data may go out only if its label is within the recipient's clearance. Derived data gets the union of its inputs' labels.
   - Validate each seed with its own label. `monitoring` is either null or a reference to a separate grant.
5. **The defaults leak.**
   - The objective, deliverable and limits default to public.
   - An answer can be public under a private question, which leaks the question ID.
   - Model-proposed question IDs can describe private content.
   - *Fix:* every answer defaults to private, and publishing one needs a consent reference. An answer's label must include at least its question's label. Code assigns opaque IDs to model-proposed questions (e.g. `q7`).
6. **Confirmation can be forged.** `state="confirmed"` with `provenance="imported_history"` passes validation.
   - *Fix:* split `origin` from confirmation. A confirmed answer whose origin isn't user-stated needs a `confirmation_ref` that the store verifies: actor, time, and a digest of the confirmed value.
7. **Some data has two sources of truth:** `payload.objective` vs the objective answer, `ceilings` vs the `limits` text, and `recipients` vs the `disclosure` text.
   - *Fix:* read the objective and deliverable only from confirmed answers, and require both to be confirmed.
   - Make `limits` and `disclosure` `object` questions whose validated values are the ceilings and recipients. Alternatively, drop them from the questions and review the structured fields directly.
8. **`_reject_domain` scans user values.** It would reject a legitimate runtime answer object with a key like `tuition`, which contradicts §3, and it proves nothing about whether the schema is domain-free.
   - *Fix:* delete it. Make the schema closed, rejecting unknown structural keys at every level. Enforce domain neutrality with a static test over code, database schema and prompts.
9. **Answer values aren't checked against `input_type`, and floats are hashed.**
   - *Fix:* validate each input type. Store decimals as canonical strings. Require choices to be in `choices`. Limit list/object size and depth. A `document_reference` must resolve to a stored, labelled document.
10. **A rubric can be met vacuously.** It may contain no required criteria, and duplicate qualification IDs aren't rejected.
    - *Fix:* require at least one required criterion, plus an implicit required "deliverable supported" row. Qualification IDs must be unique within a rubric, and a locked version contains only accepted qualifications.
11. **Fields named in the spec are missing:** stopping policy, schema version and goal lineage. Without `goal_id` in the hashed body, an approval can be replayed onto another goal.
    - *Fix:* add `schema`, `goal_id`, `revision`, `parent_body_hash` and a generic `stopping_policy` (for example, partial report after N checkpoints without progress, on exhaustion, or on an empty frontier).
12. **The limit units are unclear.** `model_usage` has no unit, `concurrent_work` is a concurrency cap mixed in with lifetime ceilings, and there is no upper bound.
    - *Fix:* use `model_tokens`, charging unknown usage conservatively, and optionally `est_cost_microusd`. Move concurrency to its own `concurrency` field. Reject values above an operator maximum.

### Blockers in the plan

13. **The API is frozen too early and covers too little.** Each worker will invent its own binding tuple, labels, evidence references and role result shapes.
    - *Fix:* P0 freezes the corrected contract plus labels, bindings `(goal_id, version_id, grant_id, strategy_id, epoch, context_version)`, evidence reference shapes and role input/output schemas.
14. **The store and migration come at P7, after the judge and context work.** CAS, durable counters, fencing and judgment invalidation would all be built against mocks.
    - *Fix:* build the store and migration second.
15. **No phase owns several core pieces:** model adapters, the restricted worker tool loop, reuse of the existing acquisition code, and ledger admission. Spec §13 had these; the plan dropped them.
    - *Fix:* add an explicit phase for them.

### Optional

- **Judge exposure to injected source text:** give the judge verified quotes and claim metadata only, never raw page text, and mark it as data.
- **Hash stability:** sort questions, answers and criteria by ID before hashing. Normalize strings to NFC, tag each digest with its kind, and raise `ContractError` on NaN.
- **ID format:** require `[a-z0-9_.-]{1,64}`, and reject bad IDs rather than silently stripping them.
- **Per-criterion digest:** store one in the version, so "IDs persist only with unchanged definitions" can be checked mechanically.
- **Judge `rubric_issue` channel:** let the judge flag a criterion it can't assess as a revision proposal, which is never applied automatically.
- **Define `strategy`:** make it a stored planner checkpoint (gap set, chosen tasks, rationale reference).
- **Move build-process rules out of the spec:** Grok slices, Gemini roles and the 32-agent cap belong in the plan, not spec §13.
- **Drop the `generic_` prefix now,** while only one module has it.
- **Treat CLI-agent transports as unattested by default.** They load hidden memory and rules.

## 3. Module map

| Action | Files |
|---|---|
| **Reuse after checking current behaviour** | `tools/ledger.py` (admission, reservations, lost usage, leases); `tools/engine_transport.py` (explicit-payload paths only); `tools/migration.py` (add one ordered generic step); `snowball.py` (rewire the entry point); the existing acquisition/SSRF/robots module, console auth/session/CSRF code, and visual tokens (paths not in the attachment) |
| **Create** | `tools/generic_labels.py` (base module, frozen in P0); `tools/generic_bindings.py` (frozen in P0); `tools/generic_roles.py` (role schemas, adapters, record/replay); `tools/generic_evidence.py` (snapshots, claims, quote checks, review); `tools/generic_store.py`; `tools/generic_privacy.py` (outbound-data checks, disclosure approvals); `tools/generic_interview.py`; `tools/generic_research.py` (planner + worker tool loop); `tools/generic_judge.py`; `tools/generic_context.py`; `tools/generic_api.py`; `tools/generic_ui.py`; `tools/generic_supervisor.py`; `prompts/roles/*.md`; `tests/fixtures/goals/` (two unrelated synthetic goals); `tests/cassettes/`; `tests/test_neutrality.py`; `tests/test_two_goal_gate.py` |
| **Fix, then freeze** | `tools/generic_contract.py` and `tests/test_generic_contract.py` (blockers 1–12) |
| **Freeze as history** | `docs/superpowers/specs/2026-10-09-snowball-goal-harness-design.md` and its plan; `tools/goal_schema.py` (read-only, used only by migration, then removed from active imports; historical rows shown as opaque JSON) |
| **Delete at cutover** | The master's question bank, finance matrices, funding-package evaluator, specialist prompts, and the active `programs`/`funding_packages` tables. The data stays in the rollback bundle and historical tables. |

## 4. Phases

| # | Phase | Acceptance test |
|---|---|---|
| P0 | Contract fixes and frozen shared types | **Private changes don't leak:** changing any private answer, question text, criterion or seed leaves the projection and `public_hash` unchanged. Changing any public field changes them. `public_id` can't be derived from the body. |
| P1 | Store and preserving migration | Migrate a synthetic v2/v3/v4 database: the bundle checksums verify and restore byte-identically into an isolated root. Old v4 contracts are marked historical, with no generic version, grant or task. Read-only commands beforehand leave the database byte-identical. |
| P2 | Labels, outbound-data checks, role adapters (fake plus one explicit-payload adapter), record/replay | **Canary test:** unique marker strings in private answers never reach an uncleared model, search or fetch recipient through a capturing transport. A reported model mismatch is rejected and charged. |
| P3 | Interview, review, approval (CLI) | A scripted session: the opening shown with zero model calls, consent recorded before the first follow-up call, at least one follow-up, an optional question declined, a required answer marked unknown, an imported answer blocking approval until confirmed. Approval creates no grant and no tasks. |
| P4 | Minimal end-to-end loop: grant/start, planner→worker loop, quote checks, admission, pause, fencing | Replayed run with a forced restart: per-unit counters match ledger totals. After pause, zero new external calls. A result dispatched before a revision is stored raw but changes no current findings. |
| P5 | Evidence review, judge, synthesis | **Judge failure matrix:** missing citation, unkept claim, unknown or duplicate ID, truncated output, unaccepted qualification, a public-only judge on a private criterion, a required criterion left unknown. None of these produces rubric-met. A valid case does. Disputing a cited claim invalidates the result; an unrelated new claim does not. |
| P6 | Working context, refresh, priority, stall detection | Refresh happens right before turns 11 and 21, and stale responses don't advance the counter. A low-scoring item leaves context but can still be retrieved, while a low-scoring conflict stays. Repeated fingerprints end as `stalled`. |
| P7 | Two-goal gate | Two unrelated synthetic goals reach judged outcomes on the same code and prompts. The neutrality scan finds nothing, and the generic entry points import no specialist modules. |
| P8 | API and CLI parity | Bootstrap tokens work once. Bad CSRF or Origin is rejected. CAS conflicts are returned, not retried. Idempotent replay behaves correctly. Each CLI command's output equals the API's. |
| P9 | Five-view UI | Desktop and mobile, keyboard only: interview → approve → grant → start → pause/drain → results/evidence → empty or blocked graph. No made-up rows. |
| P10 | Cutover: supervisor, diagnostics, wiki/ask, retention, delete specialist code | From `snowball.py`, no specialist imports remain and the full-tree neutrality scan passes. A supervisor restart keeps the counters and never starts a revised version. |
| P11 | Bounded live proof, review, docs | One bounded live run on a synthetic public goal with no personal data, labelled as live. Model identity and usage are recorded, and it ends in an honest outcome. The README reproduces it. |

## 5. Features from other agent harnesses

**Borrow later:**
- An evaluation suite over a fixed set of goals, plus a judge calibration set (planted false citations and conflicts) to measure false rubric-met rates for each model and prompt version.
- Schema-constrained structured output, with one bounded and charged repair retry.
- Declarative per-goal tool policies checked at admission.
- A content-addressed fetch cache with TTL, shared across goals only for public sources.
- A deterministic calculator tool whose operands must cite claim IDs and whose output is a labelled reasoning finding. This needs no domain rules.
- Two independent judges for rubric-met, within budget.
- Human spot-checks on a sample of kept claims.
- Local tracing and a prompt-version registry.

**Never borrow:**
- Shell or code execution, file writes, a browser with cookies or JS, forms, credentials, outreach or purchases.
- Memory that persists across goals, or inherited rule and memory files loaded into roles.
- Third-party plugin or MCP loading at runtime.
- "Keep going until done" loops, cron jobs, or auto-continue after exhaustion.
- Auto-approve or permission-bypass modes.
- Silent fallback to another model.
- Self-grading in the producer's context, or debate used as proof of success.
- Goal decomposition that rewrites the objective.
- Auto-compaction that moves private context into shared context.
- Third-party telemetry.

## 6. Verdict

**The architecture is suitable. The spec and plan need one more narrow correction before workers start in parallel.** The changes are limited to spec §3, §9 and §10 (blockers 1–12) and the plan's phase order and frozen types (13–15). That correction is P0 itself and doesn't need another design-review cycle, as long as it stays within the minimum fixes above.

Don't freeze `generic_contract.py` or start P2+ workers until P0's test passes. As currently written it already ships three privacy leaks: blockers 1, 2 and 5.