I have enough sourced material, so I'm writing up the recommendations now.

# Agent-harness modules Snowball can adapt

**Bottom line:** The most useful pieces for Snowball are **omp's approval tiers**, **omp's goal mode** and **pi's session tree** (a history log where each entry points to its parent). Claude Code only offers designs to copy, because its license is proprietary. Most of what makes omp and pi coding agents (code editing, language-server and debugger tools, browser control) doesn't fit a research harness and isn't listed.

**Lineage:** omp ([can1357/oh-my-pi](https://github.com/can1357/oh-my-pi), MIT) describes itself as "a fork of pi-mono" that "periodically ports upstream changes," and says its architecture differs from upstream ([porting-from-pi-mono.md](https://github.com/can1357/oh-my-pi/blob/main/docs/porting-from-pi-mono.md)). pi's repo `badlogic/pi-mono` now resolves to [earendil-works/pi](https://github.com/earendil-works/pi) (MIT), and its npm packages are moving from `@mariozechner` to `@earendil-works`.

## How each harness gates tool use

| | omp | pi | Claude Code | OpenHands SDK |
|---|---|---|---|---|
| Permission mechanism | Each tool has a tier (`read`/`write`/`exec`), plus per-tool `allow`/`prompt`/`deny` | None built in. "Project trust" only controls what a project folder may load at startup. Extensions can block calls via `tool_call` | `PreToolUse` hooks return `allow`/`deny`/`ask`/`defer` | The model labels each risky action with a `security_risk`; a `ConfirmationPolicy` decides when to ask the user |
| Default | `yolo` (everything auto-approved) | Runs with the user's full permissions and doesn't ask before tool calls | Not checked | `ConfirmRisky` defaults to threshold `HIGH`, `confirm_unknown=True`. The page doesn't say whether that policy is on by default |
| When something fails | With no settings and no user, falls back to `always-ask`. A prompt with no user to answer fails | A crashing `tool_call` handler blocks the tool | Exit code 2 blocks; other exit codes don't, unless `onFailure: block` is set | Actions with unknown risk need confirmation by default |
| License | MIT | MIT | "© Anthropic PBC. All rights reserved" | MIT per the repo README; the architecture page doesn't say |

## Ranked modules to adapt (8)

**1. omp approval mode (execution grants)**
- **Source:** [docs/approval-mode.md](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md), MIT.
- **What it solves:** Every tool must declare its tier, and a missing declaration counts as `exec`. There are per-tool overrides, and it fails closed when no one can answer. Approval is re-checked after extensions change a tool's arguments, so the check applies to what actually runs.
- **Smallest adaptation:**
  - Keep the tiers and the order decisions are resolved in.
  - Change the default from `yolo` to `always-ask`.
  - Store each user grant as a session record with a scope.
  - Put `eval` under the same rules as shell. The doc says `bash.patterns` doesn't cover `eval` unless `tools.approval.eval` is set.
  - Use pi's `tool_call` hook ([extensions.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)) as the enforcement point.
- **Hazards to note:**
  - omp subagents run headless in `yolo`, so the parent `task` approval is the only boundary.
  - omp's URL `read` is not always read-only: "some remote or conversion paths are Exec."

**2. omp goal mode (locked goal)**
- **Source:** [omp.sh/docs/goal](https://omp.sh/docs/goal), MIT (repo license).
- **What it solves:** The user sets the goal with `/goal set`. It has a status, an optional token budget (`budget-limited` triggers one wrap-up turn), and a completion record. Before finishing, every deliverable must be checked, and a final answer that lists open blockers doesn't count as done.
- **Smallest adaptation:**
  - Freeze the goal text and store its hash.
  - Allow replacement only through a user command that records a link to the old goal.
  - Require the rubric (#4) to pass before the completion record is written.
- **Not stated:** whether the model can change the goal, which tool marks completion, and where the source code lives.

**3. pi session tree JSONL (evidence ledger)**
- **Source:** [session-format.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md), MIT.
- **What it solves:** Every entry has an `id`/`parentId`.
  - `custom` entries (written with `pi.appendEntry()`) are stored but kept out of the model's context.
  - `context_edit` hides an entry from future context while leaving the raw history unchanged.
  - `label` bookmarks entries, and `branch_summary` keeps a summary of abandoned branches.

  Together this is an evidence log whose origins can be traced.
- **Smallest adaptation:** Add an `evidence` custom type with fields for source URL, retrieval time, content hash and the claims it supports. Rubric checks then cite entry IDs.

**4. Claude Code Stop hook (runtime rubric gate), design only**
- **Source:** [code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks), proprietary, so don't copy the code.
- **What it solves:**
  - `{"decision":"block","reason":...}` stops the agent from ending its turn.
  - `stop_hook_active` tells a hook it's already in a forced continuation, so it can avoid looping.
  - An 8-in-a-row continuation cap prevents runaway loops.
  - The page describes `/goal` as "a session-scoped prompt-based Stop hook."
- **Smallest adaptation:** When the agent tries to finish, run the rubric. A failing check returns the specific rubric items that failed, and the continuation cap is kept. pi's `agent_before_settle` is the natural MIT-licensed hook for this. Its docs only call it "an actionable boundary," so its exact behavior isn't confirmed.

**5. omp secrets (privacy boundary)**
- **Source:** [docs/secrets.md](https://github.com/can1357/oh-my-pi/blob/main/docs/secrets.md), MIT.
- **What it solves:** Sensitive values are masked before anything is sent to the model provider. Detection uses environment variables, regex patterns and a user-defined `secrets.yml`. Placeholder hashes use a per-install key so the provider can't reverse them.
- **Smallest adaptation:**
  - Add patterns for research subjects' personal data.
  - Use the one-way `replace` mode for personal data. The default `obfuscate` mode restores the real value before tool execution, which would let it leak into tool calls.
- **Gap:** Static system prompts and tool definitions are not scanned.

**6. pi compaction with structured summaries (goal kept through summarization)**
- **Source:** [compaction.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md), MIT.
- **What it solves:** When context fills up, pi summarizes older history under fixed headings: `## Goal`, `## Constraints & Preferences`, `## Progress`, `## Key Decisions`, and so on. It appends `<read-files>`/`<modified-files>` lists, and `session_before_compact` can replace the summary.
- **Smallest adaptation:** The `## Goal` section is written by the model, so it can drift over repeated summaries. Use the hook to insert the locked goal word for word, and add a `## Evidence IDs` section.

**7. OpenHands `security_risk` and `ConfirmRisky`**
- **Source:** [docs.openhands.dev/sdk/arch/security](https://docs.openhands.dev/sdk/arch/security.md), code in [software-agent-sdk](https://github.com/OpenHands/software-agent-sdk/tree/main/openhands-sdk/openhands/sdk/security). The repo is MIT; the architecture page doesn't state a license.
- **What it solves:** Every tool that isn't read-only gets a required risk field that the model fills in. Actions with unknown risk need confirmation by default.
- **Smallest adaptation:** Treat the model's self-reported risk as something that can only raise the bar: required approval is the stricter of the declared risk and the tool's fixed tier (#1). The model must never be able to lower it.
- **Not stated:** how rejection works, and the name of the paused state.

**8. omp time-traveling stream rules (TTSR), visible mode only**
- **Source:** [docs/ttsr-injection-lifecycle.md](https://github.com/can1357/oh-my-pi/blob/main/docs/ttsr-injection-lifecycle.md), MIT. The implementation is in `packages/coding-agent/src/export/ttsr.ts`.
- **What it solves:** Regex/`scope` rules watch the model's output while it streams. They can catch personal data or out-of-scope intent ("I'll email…") before a tool call runs.
- **Smallest adaptation:** Use only the non-interrupting reminder mode, log every trigger as an evidence entry, and never use `contextMode: discard` (see "Features not to copy" #3).

**Also considered:** [pi-durable](https://github.com/earendil-works/pi/tree/main/packages/durable) (MIT) has atomic commits and `requestId` idempotency, which means a retried submission returns the existing one instead of running twice. That fits grants well, but the package is marked "Experimental" and says "The API changes without notice." Copy the pattern, not the package.

## Features not to copy (3)

1. **Unrestricted shell by default.** omp defaults to `yolo`, and its doc says approval "is approval control, not containment" ([approval-mode.md](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md)). pi "does not include a built-in permission system" ([security.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)).
2. **Credential brokering.** omp's auth gateway attaches stored OAuth tokens and API keys to outbound calls. Its account pools are "a trusted-client routing policy, not an authorization boundary," and `--no-auth` turns off inbound authentication ([auth-broker-gateway.md](https://github.com/can1357/oh-my-pi/blob/main/docs/auth-broker-gateway.md)).
3. **Silent goal or prompt rewriting.**
   - pi's `before_agent_start` can return `systemPrompt`/`forceSystemPrompt`, which replaces the whole system prompt ([extensions.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)).
   - omp TTSR adds hidden `ttsr-injection` messages and, with `discard`, deletes the model's partial output ([ttsr-injection-lifecycle.md](https://github.com/can1357/oh-my-pi/blob/main/docs/ttsr-injection-lifecycle.md)).

**The other two categories you named:**
- **Outreach:** None of the sources describe an email or messaging tool. The closest outward-facing feature is omp's `/collab`. Its full-control links let remote guests send prompts to the host's agent, and "possession of the link is the trust boundary" ([collab.md](https://github.com/can1357/oh-my-pi/blob/main/docs/collab.md)). Don't copy it.
- **Purchases:** None of the four harnesses' docs describe a purchasing capability.

Sources:
- [can1357/oh-my-pi](https://github.com/can1357/oh-my-pi) · [approval-mode.md](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md) · [secrets.md](https://github.com/can1357/oh-my-pi/blob/main/docs/secrets.md) · [ttsr-injection-lifecycle.md](https://github.com/can1357/oh-my-pi/blob/main/docs/ttsr-injection-lifecycle.md) · [collab.md](https://github.com/can1357/oh-my-pi/blob/main/docs/collab.md) · [auth-broker-gateway.md](https://github.com/can1357/oh-my-pi/blob/main/docs/auth-broker-gateway.md) · [porting-from-pi-mono.md](https://github.com/can1357/oh-my-pi/blob/main/docs/porting-from-pi-mono.md) · [omp.sh/docs/tools](https://omp.sh/docs/tools) · [omp.sh/docs/subagents](https://omp.sh/docs/subagents) · [omp.sh/docs/goal](https://omp.sh/docs/goal)
- [badlogic/pi-mono → earendil-works/pi](https://github.com/badlogic/pi-mono) · [extensions.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md) · [session-format.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md) · [compaction.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md) · [security.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md) · [pi-durable](https://github.com/earendil-works/pi/tree/main/packages/durable)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks) · [Claude Code LICENSE.md](https://github.com/anthropics/claude-code/blob/main/LICENSE.md)
- [OpenHands SDK security](https://docs.openhands.dev/sdk/arch/security.md) · [OpenHands software-agent-sdk](https://github.com/OpenHands/software-agent-sdk)
- Orientation only: [Better Stack omp guide](https://betterstack.com/community/guides/ai/oh-my-pi-ai-coding-agent/) · [gittrend pi-mono](https://gittrend.io/repo/badlogic/pi-mono) · [OpenHands SDK security API reference](https://docs.openhands.dev/sdk/api-reference/openhands.sdk.security)