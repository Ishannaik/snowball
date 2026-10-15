Verdict: **needs correction**. Approval, grant, and start are correctly separated everywhere. The problems sit at the store and interview boundary, where several checks that exist in a module are never reached on the real path.

## Blockers

- **Evidence provenance is unfalsifiable at the store.** In `generic_store.py`, the snapshot writer hardcodes the provenance as a public fetch and takes no source reference. The claim writer then fabricates a claim dict with both provenance fields set to public fetch and calls the reviewer with a fixed verdict of kept. So every stored claim is auto-kept, the "note cannot masquerade as fetch" check in the evidence module is unreachable from the store, and the tool-call unit is never charged. Any text inserted as a snapshot becomes kept evidence that satisfies a met verdict. The existing evidence test exercises only the dead path. Fix: the snapshot writer takes provenance plus a fetch or tool-call reference and charges that unit, and the claim writer passes the stored provenance through.

- **A model can declassify through follow-up labels.** In `generic_interview.py`, the follow-up validator passes the proposer's label straight through. No confirmation state exists for follow-ups despite the docstring, and the answer applier adds the question to the draft on first answer. When the caller mirrors the question label onto the answer, as the end-to-end test does, a model-proposed public question yields a public answer that passes the brief derivation check and consent to an unattested recipient. Fix: force follow-up labels to private, or require explicit user confirmation before a public label is accepted.

- **Private criteria are judged on a free string.** In the judgment writer, authorized criteria are computed from a caller-supplied recipient id. Nothing binds the rows to the recipient that produced them. Submitting any rows under an attested recipient's id makes private required criteria count toward a met rubric. Synthesis validation at least demands a model id and context class. Fix: the judge result carries a recipient binding the store verifies, or the store tags results at invocation time.

- **Public brief text is unconstrained.** The contract builder checks only that the derivation list cites public confirmed answers. The text itself goes verbatim into the public projection. Nothing in the attached files shows the brief to the user before approval, and the UI only escapes free strings. A recipient with private clearance can propose a brief containing private answer content while citing public references. Fix: give the brief an origin and confirmation reference like answers, or assemble it in code from the cited answers.

The first two force the verdict on their own. The third and fourth are the same binding-gap family and should go in the same pass.

## Checks that cannot fail

These are cheap to fix and do not drive the verdict.

- The contract test asserts a never-present key is absent from the body. The interview test makes the same vacuous assertion against a follow-up id.
- The context test asserts the input list is unchanged after calling a pure function. The retained id list is never asserted.
- The revise method runs an unconditional update to the revising state, then checks whether the state is running. That self-check is dead code.
- The API CSRF token is a constant string and the test supplies it. No test covers a missing bearer token.
- The opening-interview test checks a sentinel parameter, not behavior.

## Approval and execution

Clean. Store, CLI, and API all return a null grant id and an approved state, and the API has no start endpoint. Two notes: the grant endpoint treats explicitly empty limits as "use full ceilings" because of a truthiness check, and the store grant method never validates that limit values are non-negative integers, so a string value raises a type error the API does not catch.

## Optional improvements

- The charge method only ever charges sources and source bytes. The other five ceiling units are unenforced by the store.
- No lineage check. Approval ignores revision and parent hash, and the proposal builder hardcodes revision one.
- The claim writer selects snapshots by goal and source id only, so prior-epoch snapshots can back new-version claims without charging the new grant.
- The snapshot writer charges before inserting. A duplicate source id leaves the charge in an open transaction that the next commit flushes.
- Consent is never consumed. The used counter is never incremented and nothing checks consent currency before a call.
- Follow-up ids collide across calls, and the answer applier silently prefers the stored question with that id.
- Context selection is not actually bounded for outstanding or conflict items.
- The task validator's containment check, the key privacy control in research, has no test. The newline join also lets a query span two context items.
- Recipient attestation is a self-declared payload boolean rather than an operator registry lookup.
- Malformed payloads raise type and attribute errors in several validators. The API returns a 500 and the CLI crashes.

Absent controls above are described as not present in the attached files, not as absent from the wider system.