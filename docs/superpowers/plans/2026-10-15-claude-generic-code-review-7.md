Review complete. No privacy leak found in the attached files, and the two central safety properties hold as written: approval does not grant or start, and the judge can only reach a met verdict through span-verified claims. The harness still needs correction before continuing, mainly for two tautological tests and an unusable default grant path.

**Blockers**

- **`test_generic_context.test_outstanding_support_stays_and_low_score_is_not_deleted` cannot fail on its key claim.** The function returns `retained_ids` as every input id, unconditionally. The "not deleted" assertion checks an in-memory list against itself and says nothing about stored evidence. The first two assertions in that test are real.
- **`test_generic_interview.test_opening_makes_no_model_call` cannot fail.** The counter is a literal zero set in the draft and nothing in any attached file increments it. The test is the only backing for the claim and would pass even if the opening did call a model.
- **Default grants cannot do research.** The CLI hardcodes a limits dict with only `sources`, and the API defaults to the same shape. The charging path treats a missing key as a limit breach, so every snapshot write fails for `source_bytes`. The CLI test passes only because it never adds a snapshot. This is fail-closed, so it is safe, but the CLI and API paths are non-functional.

**Should fix before relying on it**

- **Five of seven ceilings are never charged.** Only `sources` and `source_bytes` are debited. Model calls, tool calls, tokens, elapsed time, and expansion tasks are approved and granted but nothing attached enforces them. Interview consent `used` is also never incremented. If the orchestrator outside this bundle does not call the charge path, approved ceilings are decorative.
- **No lineage check on revision.** Approving in the revising state never checks that the parent hash matches the current version or that the revision number increments. A revision can claim revision 1 with no parent.
- **Follow-up ids collide across batches.** Each batch restarts at `q1`. When applying an answer, a stored question wins over the passed one, so a user could confirm an answer against a prompt they never saw. This is a consent-integrity issue.
- **Grant and start lack the guard approve has.** No immediate transaction, no row-version check. Under the threading server, two concurrent grant requests can both succeed and leave two active grants. Budget impact is limited because charging always picks the same row, but the single-active-grant invariant breaks.
- **Event log only records approval.** Grant, start, revise, and judgment leave no event row, which undercuts the kept-evidence premise.
- **Hardcoded CSRF token** is a test fixture in production code. The bearer token on loopback mitigates it, but it should be per-session.

**Optional improvements**

- The dead post-condition at the end of `revise` can never trigger. The `degree_level` and literal-None assertions in the tests are trivially true but harmless documentation.
- Context selection is unbounded when many items are outstanding or in conflict, contradicting the docstring.
- Claims are auto-kept on span match and the store has no dispute or reject path. "Kept" means verbatim, not vetted. Fine for a harness, but the UI wording should match.
- Unbounded request body length. Malformed payloads that raise `TypeError` or `AttributeError` escape the API's error handler and drop the connection instead of returning 409.
- The judgment path does not check that the recipient kind is a model.
- A required opening question can be answered "unknown" and still pass approval. That is a design choice worth stating, not a bug.

**Verdict: needs correction.** Fix the two tautological tests so they exercise real behavior, make the default grants include every charged unit, and confirm the orchestrator charges the remaining ceilings. The privacy projection, the approve-grant-start separation, and the kept-claim gate are sound as implemented and do not need rework.