Review is based on reading the attached files only. I ran nothing and used no personal data.

**Blockers**

1. **Privacy leak in task validation.** The task gate in `generic_research.validate_task` only checks that `query_text` is contained in the public context. The `exploration_question` field is free text from the planner and is returned in the cleaned task. If a planner with private clearance builds tasks and the cleaned task is forwarded to a search or fetch recipient, private context can travel in that field. Fix: containment-check it the same way as the query text, or strip it from the returned dict and keep it inside the trust boundary.

2. **No test proves evidence writes are refused outside the running state.** The store refuses snapshots, claims, and judgments unless the goal is running, and revise fences active grants. No test exercises either refusal. The current tests check that approve returns a null grant id, which is a literal constant in the code. A regression that let evidence writes happen after approval or after revise would pass the suite. Add tests that call add_snapshot in the approved state and after revise and assert the state error.

**Must confirm outside these files.** The context selector in `generic_context.select_context` has no notion of labels or recipient clearance. It ranks on score, outstanding criteria, and conflict only. If it assembles the context for a model call, label filtering must happen before it, and that code is not attached.

**Ceilings are paper-only within this layer.** Only sources, source bytes, and tool calls are ever charged. Model calls, model tokens, elapsed seconds, and expansion tasks are granted but never debited. The charging method is private, and interview consent never increments its used counter. I cannot verify enforcement from the attached files. Charging also reads the grant outside the write transaction, so concurrent workers can overrun a limit. The approve method already uses an immediate transaction and is the pattern to copy.

**Verified as sound**

- Approval does not start execution. Approve stores a locked version and returns no grant. Grant and start are separate store calls, and the API exposes no start route.
- The judge cannot declare success without kept evidence. Met and conditional verdicts require kept support claims, required criteria must be inside the recipient's authorized scope, and the store rejects private claims cited on any row by a recipient without private clearance.
- Human notes cannot be labeled as public fetches, and claims from notes are always private.
- Rendered source text is escaped.

**Optional improvements**

- **Claim verdicts default to kept.** The claim submitter sets its own verdict, and nothing in these files can later move a claim to disputed or rejected. The span and hash check is the real gate, so this is not a blocker, but make the verdict explicit.
- **Conditional counts as met.** The stored outcome string hides whether a qualification was used. Surface it.
- **Revision chain is unenforced.** Approving in the revising state does not check revision number or parent body hash against the current version. The interview can only propose revision one.
- **Vacuous assertions.** The items identity check in the context test cannot fail because the function is pure. Both assertions about a "degree_level" key cannot fail. Several tests assert only the error type, so they pass if the function fails for an unrelated reason. Assert the field as well.
- **API robustness.** Malformed JSON and missing payload keys are outside the handled exception set, and there is no body size limit. The grant route has no test.
- **Hardcoded CSRF value.** The bearer token requirement makes cross-site requests moot, so this is cosmetic, but generate it per process like the token.
- **Dead code.** The post-update running check in revise can never trigger.

**Verdict: needs correction.** Fix the exploration question gap and add the state-refusal tests before continuing. Confirm where label filtering happens before context selection and where model call ceilings are charged.