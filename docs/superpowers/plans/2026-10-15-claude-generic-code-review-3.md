The review covers four categories: privacy leaks, approval starting execution, judge success without kept evidence, and tests that cannot fail. Findings below, blockers first.

## Blockers

- **Judge scoping is never applied where outcomes are persisted.** The store method `add_judgment` passes every criterion id as the authorized set to both the validator and the rubric-met check. The clearance scoping that the judge unit test exercises is dead on the only path that records a rubric_met outcome. The store never looks up which recipient produced the judgment or what clearance it holds. With a required private criterion, a public judge must either produce a verdict on a criterion it should not see, or every judgment is rejected as incomplete. There is also no test for this method. The end-to-end test calls the judge module directly with a hand-built claims list and a hand-built authorized list, so nothing proves the store-to-judge binding. Fix: pass the judging recipient's cleared criterion set into the store call, and add a test that goes through the store.

- **Interview consent has no label gate.** The function `record_consent` accepts any question ids and any recipient string. It never compares answer labels to a recipient clearance, and the draft carries no recipient registry. Answer labels default to private, so a consent naming the constraints or success answers for an unattested model is accepted by code. The disclosure opening question is collected and never read anywhere. The harness claims code enforcement of labels, so this counts as a privacy leak in its own terms.

- **Tests that cannot fail or certify the wrong property.** These inflate confidence in exactly the properties the harness advertises.
  - The contract test asserts that the body does not contain a key named degree_level. The body never has that key.
  - The interview follow-up test asserts the id does not contain degree_level right after asserting the id equals q1.
  - The context test checks that a low-score item appears in the retained list. That list is the input ids echoed back, and the function never touches storage. The "not deleted" claim is untested.
  - The judge fingerprint test compares the fingerprint of a one-element list to the fingerprint of the same one-element list. The uncited-claim drop lives in the validator, which this test never calls.
  - The research test asserts the status is not rubric_met. The planner has no such return value.
  - The store test for kept claims passes because the source id is missing and the snapshot lookup fails. It never reaches the quote check. This one is not a tautology, but it certifies a property it does not test.
  - Same spirit in production code: the post-check in `revise` that raises if state is running can never fire, because the method just set state to revising.

## Clean category

Approval does not start execution. Approve writes state approved with a null grant id. Grant requires approved state and caps limits against the approved ceilings. Start requires an active grant and granted state. The API test, CLI test, store test, and end-to-end test all verify this for real, including the missing-origin rejection.

Kept evidence is also sound at the unit level. The quote check requires an exact span match against the stored snapshot, and met or conditional verdicts require support ids whose stored state is kept. The weakness is the untested integration described in the first blocker, not the evidence check itself.

## Optional improvements

- **Hardcoded CSRF literal.** The handler compares against the string csrf-test. Not a blocker: a browser cannot send the Authorization header cross-origin without a preflight the server does not answer, so the bearer token already blocks cross-site requests. Still a test value shipped in a security check.
- **Grant and start lack transactions and row-version guards.** Two concurrent grant calls both see approved and both insert. Start also does not check the grant's body hash against the goal's current hash.
- **Context selection is not bounded.** Outstanding and conflict items are appended past both the item and token limits. The docstring says bounded.
- **Task validation does not check context ids against labels.** It requires public context ids but has no label map to verify them, so a cleared planner could put private content in a search query unchecked.
- **Unhandled error paths.** Non-dict qualification rows raise AttributeError, missing payload keys raise KeyError in the CLI, and non-integer grant limits raise TypeError in the API thread.
- **Claims persist only the quote hash.** Span and snapshot hash are not stored on the claim, and the review states from the evidence module are never wired into the store.
- **Fragile substring assertion.** The contract test checks that the three-letter string "one" is absent from the public projection. Use a distinctive sentinel.
- **Duplicate module instances.** Files load siblings under different module keys, so exception classes exist twice. Harmless today because callers catch the shared base class.
- **Out of attached scope.** Nothing here updates grant usage or enforces ceilings at runtime. Noted, not a finding against these files.

## Verdict

Needs correction. The first two blockers are missing enforcement at the integration seam rather than bugs in the unit modules, and the third removes real coverage from the properties the tests are named for. All three fixes are small.