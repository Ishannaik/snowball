**Verdict: needs correction.** Approval does not start execution, and I found no concrete privacy leak in these files. The blockers are elsewhere: the store does not gate evidence or judgments on grant or start, "kept" is assigned automatically rather than by review, and several tests pass even if the guard they name is deleted.

## Blockers

**1. Evidence and judgments bypass the grant and start gates.** In `generic_store.py`, the methods `add_snapshot`, `add_claim`, and `add_judgment` never read the goal state or any grant row, and `used_json` is never updated anywhere. The grant ceiling is checked once at grant time and never consumed. The end-to-end test shows the consequence: it approves the goal, adds a snapshot, adds a claim, and records a judgment with outcome `rubric_met` without ever calling grant or start. The goal is still in state `approved`, and the rendered page says "not started" next to a supported result. The test name claims there is no implicit start, but what it actually demonstrates is a full research outcome with no start at all. Fix: require state `running`, bind snapshots, claims, and judgments to the active grant and body hash, and decrement the grant's limits.

**2. "kept" is not a review outcome.** `review_claim` in `generic_evidence.py` is never called from the store. `add_claim` writes `state='kept'` unconditionally once the quote span matches. The snapshot and claim tables have no provenance, source kind, fetch recipient, or URL columns, so the "a note cannot be labeled as a public fetch" guard has nothing to bind to. In the end-to-end test, a hand-typed snapshot of "queue A is bounded" is enough to reach `rubric_met`. The judge's requirement for kept claims therefore reduces to "some string was inserted and quoted." Fix: store provenance on snapshots, make kept a transition recorded through `review_claim`, and reject public-fetch provenance for snapshots not produced by a fetch recipient.

**3. Tests that pass without the guard they name.** Each of these raises from an earlier, unrelated check, so deleting the safety guard leaves the test green.

- `test_shell_task_is_rejected`: the task has no `query_text` or `public_context_ids`, so the query check raises even with the tool allowlist removed.
- `test_synthesis_cannot_set_rubric_met`: the synthesis lacks `context_class`, `model_id`, `goal_id`, and `version_id`, so the binding check raises even with the `rubric_met` guard removed.
- `test_human_note_cannot_masquerade_as_fetch`: the claim has no `quote_hash` and is not kind `reasoning`, so the quote-hash check raises even with the provenance guard removed.
- `test_kept_claim_requires_quote_hash`: the claim has no `source_id`, so it fails on "claim has no stored snapshot." The quote-hash path is never reached.
- `test_fingerprint_ignores_uncited_claim`, first assertion: both sides are the single-element list containing `c1`, so it compares a function to itself on identical input. The second assertion has teeth. Nothing tests that an uncited claim is excluded via `validate_judge_result`.
- `test_outstanding_support_stays_and_low_score_is_not_deleted`: `retained_ids` is always every input id by construction, so the "not deleted" assertion cannot fail and says nothing about the store.

The first four pin safety properties and should be rewritten with fixtures that satisfy every other check. The last two are untested claims rather than false ones.

## Corrections worth making, not blockers

- **Evidence is not epoch-fenced.** Claims and snapshots carry no `body_hash` or `epoch`. After revise and re-approve, old claims count toward the new rubric. This is close to a blocker and would be one if revision is a real workflow.
- **Approve after revise checks no lineage.** `approve` accepts state `revising` but never compares `parent_body_hash` to `current_body_hash` or requires `revision` to increase.
- **Grant and start lack the concurrency guard that approve has.** No `BEGIN IMMEDIATE` and no conditional update on `row_version`, so two concurrent grant calls can both succeed.
- **Dead check in revise.** The state is set to `revising` unconditionally, so the following "revision started execution" check can never fire. Revise also works from any state, including draft.
- **Context selection is not bounded.** Outstanding or conflict items in `select_context` bypass both `item_limit` and `token_limit`, contradicting the module docstring. Selection is also clearance-blind.
- **Consent allowance is never consumed.** Nothing increments `used` or `model_calls`, so `consent_current` only ever checks the input digest.
- **Task validation does not bind to clearance.** `validate_task` accepts any `public_context_ids` list without checking a registry or the search recipient's clearance. This is a privacy gap in a flow not shown here, not a demonstrated leak.
- **Follow-up ids can collide.** `validate_followups` always numbers from `q1`, and `apply_answer` validates against the passed question rather than the stored one with the same id.
- **Hardcoded CSRF constant** and a non-constant-time token comparison in the API, plus an unbounded `Content-Length` read.
- **Rubric qualification items** are indexed with `.get` and `[]` without a type check, so a malformed item raises `AttributeError` or `KeyError` rather than `ContractError`.
- **Required questions can be approved as "unknown".** Only "declined" is forbidden. Several tests rely on this, and the "disclosure" answer is unknown while `recipients` still grants clearances. This is a design choice, but the two should be linked or the rule stated.

## What holds up

`approve` touches only the versions, goals, and events tables, returns `grant_id` as null, and the store, CLI, and API tests all assert the state is `approved` rather than `running`. The public projection excludes private answers, private criteria, and private seeds, and the contract test confirms a private value and the body hash do not appear in it. The judge cannot mark a criterion met without claims in state kept, and a public-clearance recipient cannot satisfy a required private criterion.