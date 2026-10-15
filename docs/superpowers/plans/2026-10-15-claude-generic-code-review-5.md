**Verdict: needs correction.** Approval separation and judge evidence gating both hold up under tracing. Four concrete problems block: one interview-time privacy leak, one clearance rule enforced in only one of two gates, grant limits that nothing enforces, and two tests that pass for the wrong reason.

## Verified as sound

- **Approval does not start execution.** The store's approve method writes a version, sets state to approved, records a grant id of None, and inserts no grant row. Grant requires the approved state and checks limits against ceilings. Start requires the granted state plus an active grant row. CLI and API both route through these same methods. Revise fences active grants and moves the goal out of running, so evidence writes stop.
- **Judge cannot succeed without kept evidence.** Claims only enter the store through add_claim, which verifies the quote span and snapshot hash before inserting as kept. The store's add_judgment loads claims only from the store, requires kept support for met or conditional verdicts, and rubric_met returns false when any required criterion lies outside the recipient's authorized scope. Synthesis cannot set rubric_met.
- **Public projection excludes private data.** The projection carries only brief, public criteria, ceilings, and public seeds. The public hash excludes the body hash and public id. The contract test for private changes checks this for real.

## Blockers

1. **Privacy leak in the interview.** The apply_answer function in generic_interview.py validates the answer against the question the caller passes, not the stored draft question with the same id. If the stored question is private and the caller passes the same id with an empty label, the stored question is kept but the public-labeled answer is stored anyway. Then record_consent reads the label from the answer when one exists, so an unattested public recipient is cleared to see it. Approval later rejects the mismatch, but the disclosure already happened during the interview. Fix: look up the stored question by id and require the answer label to include the stored label.

2. **Attestation rule enforced in only one gate.** The interview's record_consent refuses any non-empty label for an unattested recipient. The contract validator accepts a recipient with attested false and clearance private. The store's add_judgment computes authorized criteria from clearance alone. So a private criterion routes to an unattested judge. Fix: require attested when clearance is non-empty inside the contract's recipient validator, which covers both gates.

3. **Grant limits never bind.** This is outside the four named categories, but it is the same class of problem. The grant's used_json column is written as an empty object and never read or updated. Neither add_snapshot nor add_claim consults the grant's sources or source_bytes limits. The required limits question, the required ceilings, and the ceiling check in grant all feed a control that nothing enforces. Fix: record usage in the grant row on each snapshot and refuse writes past the limit, or state clearly that enforcement lives elsewhere and add a test proving the store records usage.

4. **Two tests pass for the wrong reason.**
   - In test_generic_store.py the kept-claim test calls add_claim on a goal still in draft. The state check raises before any claim field is read. The test would pass if quote verification were deleted. If the goal were running, the missing quote key would raise KeyError rather than StoreError and the assertion would fail. Fix: approve, grant, and start first, then assert that a claim with a wrong span or missing snapshot is refused.
   - In test_generic_judge.py the fingerprint test builds the cited list by filtering to the single claim c1, then compares its fingerprint to the fingerprint of that same claim. The two inputs are identical, so the first assertion cannot fail and never exercises the fingerprint produced by validate_judge_result. Fix: call validate_judge_result with extra kept claims present and assert the returned fingerprint equals the fingerprint of only the cited claim.

## Should fix before relying on it

- **No review path into the store.** The review_claim function exists, but nothing transitions a stored claim to disputed or rejected. It also accepts any quote_hash string as checked. The evidence test passes a three-letter placeholder.
- **Claims are unbound.** Claims carry no body hash, epoch, or criterion. After revise, re-approve, and start, add_judgment loads every claim for the goal, and any kept claim can support any criterion.
- **Brief text has no provenance.** Answers from models need a confirmation reference. The public brief's text and derived_from list are unverified model output labeled public by the proposer.
- **Revision chain unchecked.** Approve does not compare the payload's revision or parent hash to the goal's current body hash.
- **Tautological assertions.** The retained_ids check in the context test, both degree_level assertions, and the Goal-before-Graph order checks cannot fail against the current code.

## Optional

- The CSRF header value is a hardcoded constant. With bearer auth it adds nothing.
- The CLI grant command hardcodes sources to two, so it always fails against a ceiling of one.
- The API catches only KeyError, sqlite errors, and ValueError. A TypeError from a non-integer limit kills the handler thread. Content-Length parsing sits outside the try block.
- The store accepts empty limits on a grant. Grant, start, and revise have no row-version guard or immediate transaction.
- In select_context, outstanding or conflicting items bypass both the item and token limits, so the selection is not bounded.
- The revise method's post-check for a running state is unreachable.
- validate_task does not check public_context_ids against the contract's public answers.