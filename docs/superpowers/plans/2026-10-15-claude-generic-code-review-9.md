Review complete. Findings below are from reading the attached files only.

## Verdict: needs correction

One blocker, in the evidence layer. Everything else is suitable to continue.

## Blocker

**Evidence has no privacy class, so disclosure is not enforced for claims.** The disclosure model works for answers and for criteria but stops before evidence.

- In `generic_store.Store.add_claim`, the stored claim dict and the `generic_claims` table carry no provenance, source kind, or label. The snapshot's `human_note` provenance is checked once in `review_claim` and then discarded.
- In `Store.add_judgment`, criteria are filtered by recipient clearance, but every claim for the body hash is passed to the validator unfiltered.
- Result: a recipient with clearance `[]` can have a `met` verdict accepted on a claim quoted from a private human note. The stored judgment and fingerprint cannot show this happened.
- Fix: persist provenance and a label on each claim, and have `add_judgment` reject support claims whose provenance or label exceeds the recipient's clearance.

## Checked and clean

- **Approval does not start execution.** `approve` sets state `approved` and returns no grant. `grant` requires `approved`. `start` requires `granted` plus an active grant. `revise` fences grants. The API has no start route.
- **Judge success requires kept evidence.** `validate_judge_result` rejects `met` and `conditional` with empty or non-kept support. `add_judgment` loads claims from the database, not from the caller. Note the definition: in `add_claim` the verdict defaults to `kept` on span match, so "kept" means "quote matches snapshot," not "reviewed."
- **No whole test is unfalsifiable.** Each test has at least one assertion that would fail if the guarded behaviour changed.

## Optional improvements

Tests and dead checks:
- The `degree_level` assertions in the contract and interview tests cannot fail. Decorative.
- The context test asserts the input list is unchanged. `retained_ids` is never checked.
- `test_generic_api` hardcodes the same `csrf-test` literal the handler compares against, so it cannot detect that the token is a fixture.
- `Store.revise` ends with a self-check that cannot fire.
- Nothing tests that snapshot, claim, or judgment writes are refused in `approved` or `granted` state. The core guarantee is tested by label, not capability. Strongly recommended.
- The end-to-end test renders fixture strings, not store-derived state.

Store and API:
- `add_snapshot` charges the grant before the insert and does not roll back if the insert fails.
- `grant`, `start`, and `_charge` are not transactional under the threaded server. Two concurrent grants can both succeed.
- `grant` does not type-check limit values. Strings crash, negatives pass.
- `approve` from `revising` does not check revision or parent hash against the current version.
- The CSRF token is a hardcoded literal and adds nothing beyond the Origin and bearer checks. Token comparison is not constant-time.
- The 409 error body echoes the failing field name, which can be a private question id. Low risk.

Contract and interview:
- Required opening questions can be answered `unknown` at approval. Required only blocks `declined` and missing.
- `validate_followups` reuses `q1..` ids across calls, so `apply_answer` can bind an answer to a stale question.
- Public brief text is unverifiable against private content. Inherent, and user-approved.

Judge, research, context:
- `rubric_met` treats `conditional` as met and the stored outcome drops the qualification.
- `validate_synthesis` does not check that cited claims are kept.
- `select_context` limits are soft for outstanding items, so the selection is not actually bounded.