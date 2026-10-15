The approve, grant, and start separation holds, and the public projection hashing is sound. The blockers are elsewhere: label enforcement stops at the contract projection, the "kept" evidence chain is open, and one key test passes for the wrong reason.

## What passes

- **Approval does not start execution.** The store's approve method only sets state to approved and returns a null grant id. Grant requires the approved state. Start requires the granted state plus an active grant row. The store, CLI, and API tests all confirm this.
- **Public projection excludes private data.** The public hash omits the body hash, private criteria, and private answers. The contract test proves a private change leaves the public hash unchanged.

## Blockers

**Privacy: label checks exist only in the projection, not at egress.**

- The public brief's provenance is never validated. A brief labeled public can list a private question in its sources and still pass. Nothing checks that `derived_from` names public, confirmed answers, or even that the ids exist.
- The recipients, seeds, stopping policy, and monitoring sections pass through contract validation untouched. The recipient kinds constant is defined and never used, so no recipient has a checked kind or clearance.
- A research task's public query class is a string the model supplies. The validated output drops the query text, so what is validated is not what is executed.
- The forbidden tool list is a denylist. A task naming `bash`, `python`, or `http` passes.

**Judge success: nothing ties "kept" to verified evidence.**

- The claim review function returns a kept state without requiring the span check to have run. A kept claim can carry a null quote hash.
- Reasoning claims only need a non-empty basis list. The docstring says the basis must resolve to kept claims, but the ids are never looked up. A reasoning claim built on rejected claims is kept.
- The claims and judgments tables exist but no method writes them. The evidence fingerprint is computed and discarded. The rubric-met function accepts any dict, so a positive outcome rests on whatever claim states the caller asserts in memory.

**Approval integrity: the wrong contract can be approved.**

- The store never compares the contract's own goal id to the goal being approved. A contract whose body says one goal is stored under another.
- The row version check is a read followed by an unconditional update. Under the threaded API, two approvals with the same row version both succeed.

**Tests that cannot fail or test the wrong rule.**

- The unconfirmed-import test sets a value on an unconfirmed answer. The "only confirmed answers have a value" rule raises first with the same field, so the assertion passes. Removing the unconfirmed check from approval readiness leaves this test green.
- Three assertions are tautologies. The retained ids list is always every input id. The interview model-call counter is a constant nothing increments. The next-action status can never equal "rubric_met".
- The reasoning-basis test only tries an empty list, which is the one case the code handles.
- No test calls start before grant and expects an error, in the store, CLI, or API.
- The context selector's docstring says bounded, but outstanding and conflict items bypass both the item and token limits. The test does not exercise this path.

## Optional improvements

- The CSRF header value is a hardcoded literal. The Origin check is the real defense, so either drop the header or make it per session.
- The bearer token comparison uses plain inequality rather than a constant-time compare, and there is no request body size cap.
- Grant limits are never checked against the approved ceilings, and the used counter is never updated. Consent usage is likewise never incremented.
- Follow-up ids restart at the first index each round, so a later answer can bind to a different stored question with the same id.
- Revision approvals never check the parent body hash or revision number against the current version. The revise method's post-check for a running state is dead code.
- Malformed payload shapes raise uncaught type errors in the API and surface as 500s.
- The quote hash covers quote text only, not the snapshot or span, so identical quotes from different sources are indistinguishable.
- The fingerprint test's first assertion compares identical inputs.

**Not verifiable from these files.** Ceiling enforcement, label filtering at prompt assembly, claim persistence, and the executor's tool allowlist are absent from the attachment. They may exist elsewhere, but nothing here shows them.

## Verdict

Needs correction. The approval state machine and public hashing are suitable as written. The harness should not proceed until label enforcement reaches the egress points, kept claims require a passed span check with a persisted judgment, the approve method binds the contract to its goal, and the unconfirmed-import test is rewritten so it exercises the rule it names.