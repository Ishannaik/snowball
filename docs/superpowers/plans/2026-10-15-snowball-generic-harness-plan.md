# Integrated generic Snowball implementation plan

> **For agentic workers:** use subagent-driven-development with disjoint file owners. The parent integrates and runs checks after writers settle.

**Goal:** Complete and test the approved autonomous research tool, not a master's evaluator or an OMP clone.

**Architecture:** Reuse public acquisition guards and explicit model transports. One durable generic control plane owns immutable goal versions, admission, citations, independent review, context and outcomes. The CLI and authenticated five-view console use the same Service. Approval, grant, start and actual research execution remain separate.

**Tech Stack:** Python 3.10+, SQLite WAL, stdlib HTTP/JSON and existing guarded public acquisition infrastructure.

**Spec:** `docs/superpowers/specs/2026-10-10-snowball-generic-goal-harness-design.md`

## Constraints

- No domain-specific required fields or financial rule engine.
- Preserve existing worktree and unrelated edits. Existing specialist rows remain historical, never approved generic contracts.
- Read-only access never creates or migrates a database. Explicit migration preserves a checksummed DB/source/wiki bundle.
- No arbitrary shell, outreach, purchases, login bypass or private data disclosure.
- Cloud build/review source excludes credentials, actual databases and personal context.
- Bind admission, calls, evidence and judgments to goal/version/epoch. Resource admission is atomic, no transaction spans I/O.
- Missing review/citation, malformed/truncated output, unavailable roles, private required criteria and stale results never imply success.
- No personal research or Oracle acquisition-loop start. No commits/push without the user's delivery instruction.

## Ownership and executable boundaries

| Owner | Files | Deliverable |
| --- | --- | --- |
| DurableRuntime | generic_store.py, store/admission tests | Durable goals/drafts/versions, atomic grants/reservations, evidence review, tasks/read views |
| RolesAndPrivacy | generic_roles.py, contract/interview/context/judge/evidence/research and tests | Explicit HTTP roles, actual consent consumption, strict JSON/identity, hard label-scoped context |
| PublicAcquisition | generic_acquisition.py and tests | Reused SSRF/robots/wall/redirect controls, canonical URLs, bounded public search/fetch |
| ConsoleIntegration | generic_api.py, generic_ui.py and tests | Auth/session/CSRF/CAS/idempotency and real five-view workflow |
| CliCutover | generic_cli.py, snowball.py, CLI tests | Default generic goal/run/serve commands and structured errors |
| Parent | generic_runtime.py, generic_migration.py, integration scenarios and README | End-to-end orchestration, preserving migration, integration and actual surface verification |

## Phases and checks

- [ ] Contract and storage: source-bound immutable snapshots; explicit unknown-to-independent-review transitions; review invalidation; CAS/lineage; preserved migration. Verify two-connection admissions, duplicate dispatch, renewal lifetime limits, stale callbacks and read-only refusal.
- [ ] Roles and context: explicit public projections and consent before first cloud call; requested/reported identity and usage receipts; independent review/judge; hard item/token bounds with deferred conflicting/outstanding references. Verify negative egress and malformed/truncated/outage outcomes.
- [ ] Autonomous runtime: planner tasks target rubric IDs; public queries derive from authorized inputs; fetch only seeds/discovered links; parallel workers return to the owning integration thread; research claims undergo exact quote checks and separate review; synthesis and fresh judge drive next task or terminal outcome. Verify unrelated goals without code/schema edits.
- [ ] User controls: persistent adaptive questions and editable proposals, meaningful hash review, separate grant/start/run, truthful pause/drain/revision, authenticated read state. Browser-test actual desktop/mobile/keyboard flows and same-server security failures.
- [ ] Operator integration: generic CLI/supervisor/diagnostics/answers/export/retention; source/wiki backup and isolated restore. Never dispatch from paused, exhausted, stopped or unapproved goals.
- [ ] Evidence and delivery: integrated unit regressions after writers settle, actual non-personal bounded model/public-tool smoke, full CLI/browser proof, independent source review of the integrated runtime, truthful README. Personal research remains unstarted.

## Integration rulings

- The previous 112-item specialist board is superseded, not completed. The approved generic spec remains the acceptance authority.
- Preserve the current linked worktree; never delete unrelated work or copy private data to build agents.
- Keep the existing generic module names during integration to avoid unrelated rename churn; default commands cut over together.
- Parallel workers skip intermediate tests/build/lint/formatting; the integration owner verifies after the wave. This overrides per-worker test-run ceremony, not regression coverage.
- A successful component source review does not close end-to-end tool acceptance. Only actual exercised workflow evidence does.
