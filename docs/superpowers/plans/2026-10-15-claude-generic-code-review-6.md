**Verdict: needs correction.** Approval does not start execution anywhere in these files. The approve path in the API, CLI, store, and interview ends in state `approved` with no grant, and grant and start are separate transitions with distinct preconditions. The blockers are elsewhere: the outbound query path, evidence surviving revision, and review states that cannot be reached in the durable path.

## Blockers

**1. Outbound search queries are never checked against disclosure (privacy leak).** The task validator in generic_research.py receives only the rubric. It never sees answers, labels, or recipients. The docstring promises "the exact public query and its context ids", but the enforcement is a non-empty list of strings. The contract defines search and fetch recipient kinds precisely to gate this, and nothing consults them. This task passes validation:

```python
validate_task({"criterion_id": "supported", "tool": "search",
               "query_text": "<private secret text>",
               "public_context_ids": ["private_note"]}, rubric)
```

A planner with private clearance can emit a query containing private context, and the harness will send it to an unattested search recipient.

**2. Evidence survives revision, so a judge can reach rubric_met on stale claims.** Snapshots and claims carry no body hash or epoch column. The judgment path in generic_store.py loads every claim for the goal. This sequence is accepted end to end: approve v1, grant, start, add snapshot and claim, revise, approve v2 with a different objective and rubric, grant, start, add a judgment citing the v1 claim. The outcome is `rubric_met`. Approve also never checks the payload's parent hash or revision against the stored current version, so lineage is self-declared.

**3. Review states are unreachable in the durable path.** The store's add_claim writes `kept` unconditionally on a span match. The review function in generic_evidence.py is never called by the store. Snapshots have no source kind or provenance field. So "disputed", "rejected", and "a note cannot be labeled as a public fetch" exist only in a unit test against an unwired function. The end-to-end test shows the consequence: arbitrary text passed to add_snapshot becomes kept evidence and yields `rubric_met`. The test suite advertises a guarantee the store cannot enforce.

## Assertions that cannot fail

- In the context test, `assertIn("noise", selected["retained_ids"])` is unfalsifiable. The function returns every input id as retained unconditionally.
- Both `assertNotIn("degree_level", ...)` assertions in the contract and interview tests check for a string nothing ever produces.
- In the store's revise method, the post-update check for state `running` is dead code. The UPDATE just set the state to `revising`.

The remaining assertions in those tests do discriminate and can fail.

## Optional improvements

- **CLI grant cannot do research.** It hardcodes a limit of two sources with no source_bytes key, so the charge step rejects every snapshot. Fails closed, but the CLI path is unusable past start.
- **Grant has a race.** Only approve uses an immediate transaction. Under the threading server, two concurrent grant calls can both pass the state check and create two active grants.
- **Snapshot charges before insert.** A duplicate source id raises after the usage UPDATE with the transaction left open, so the next commit records a charge for a snapshot that was never stored.
- **Hardcoded CSRF token.** The literal test value is in production code.
- **Attestation is payload-asserted.** A model-proposed recipient of kind search or fetch with attested true and private clearance validates. The user must notice it at approval.
- **Public brief text is free text.** Only derived_from is checked, so a model-proposed brief can paraphrase private answers.
- **Follow-up ids restart at q1 each batch** and collide with stored questions, so a later follow-up silently binds to an earlier one.
- **Fragile substring check.** The contract test asserts "one" is absent from the public projection, which will break on ordinary words.
- **Core privacy guards are untested.** No test covers a public answer on a private question, a private criterion in public_criterion_ids, an unattested recipient with clearance, or a brief citing a private answer. The guards read as correct but are unproven.
- **Consent usage and call ceilings are never incremented** in these files. If the runner lives elsewhere, that is out of scope here.