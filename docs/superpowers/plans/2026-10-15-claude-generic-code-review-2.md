**Verdict: needs correction before continuing.** Approval does not start execution, and that sequencing holds in the API, CLI, and store. But kept evidence and rubric success are self-attested, the outbound query privacy gate is a string the planner sets, approved ceilings are not binding on execution, and the core separation claim has only happy-path tests.

## Blockers

- **Kept evidence is a truthiness check, not a verification.** The function `verify_quote` hashes only the quote string, so the hash is reproducible with no snapshot at all. Nothing downstream recomputes it. `review_claim`, `Store.add_claim`, and `validate_judge_result` each accept any non-empty string, and the judge test proves it by using the literal value `"aaa"`. Fix: bind the verification record to snapshot hash, source id, and span, and have the store accept only records it produced or can recompute against a stored snapshot.

- **A "met" judgment can be stored without a validated judge result.** `Store.add_judgment` takes the outcome string from the caller, never calls `rubric_met`, never recomputes the fingerprint from its own claim rows, and does not compare `body_hash` against the goal's current version. `rubric_met` also accepts any dict shaped like rows, not only validated output. Fix: the store derives outcome and fingerprint itself from stored claims cited by the judgment body.

- **Outbound query privacy is self-declared.** `validate_task` only checks that `query_class` equals the string "public", which the planner sets. It drops the query text from its return value, so the query actually sent is whatever the caller still holds. The `exploration_question` free text passes through unchecked, the `read` tool has no path scope, and `validate_synthesis` accepts any truthy `context_class`. Fix: tasks must reference public context item ids, and the validated task must carry the exact text that goes to the tool.

- **Approved ceilings are not binding on execution.** `validate_ceilings` runs only at approval. `Store.grant` and the grant endpoint accept arbitrary caller limits, the CLI hard-codes a value, and `used_json` is never updated by any file. One bearer token performs approve, grant, and start with no actor recorded and no event for grant, start, or revise. Fix: grant limits must be validated against the approved version's ceilings, and usage must be recorded.

- **The core separation claim has only happy-path tests.** The api, cli, and store tests run approve, grant, start in order and assert a `grant_id` of None that is a literal in the return statement. No test asserts that start before grant, grant before approve, or start with a fenced grant is rejected. Other assertions cannot fail: `model_calls == 0` in the interview test is never incremented anywhere, the judge fingerprint test's first equality compares one row to itself, the research test's status check compares against a value `next_action` never returns, the context test's `retained_ids` is always every id, and the `degree_level` absence checks test keys that were never present.

## Improvements

- In `select_context`, `must_keep` affects sort order only, so score-zero items are still dropped. Outstanding items can also exceed the token limit despite the "bounded" docstring.
- `work_fingerprint` collapses all exploration-only tasks into one fingerprint, which will produce false stall signals.
- `revise` runs from any state with no row version guard, and its post-update running check is dead code.
- `approve` from the revising state ignores `parent_body_hash` and revision lineage against the current version. `start` does not compare the grant's body hash to the goal's current hash.
- Claim ids are a global primary key across goals. Claims can be added to a goal in any state.
- `review_claim` blocks note masquerading only for the exact string `public_fetch`. An allowlist of source kinds is safer.
- Consent never increments `used`, and the recipient is a bare string not checked against the contract's recipients or clearance.
- `validate_followups` restarts ids at q1 on every call, so repeated calls collide.
- The CSRF value is hard-coded, token comparison is not constant-time, and the content length parse sits outside the error handler.
- Non-dict qualifications raise AttributeError rather than ContractError.
- The contract test's substring sentinel `"one"` is fragile. Use a distinctive token.
- The UI renders whatever it is given with no public or private distinction.
- Loading each module under several different keys works today but makes cross-copy exception checks fragile.

## Verdict

Needs correction. Fix the evidence binding, store-derived judgment outcome, query provenance, grant ceiling enforcement, and negative-path tests before continuing.