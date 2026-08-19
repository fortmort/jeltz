# TODO: In-Session Skeptical Review Loop

Design analysis and decomposed task list for two related goals:

1. Run `skeptical-reviewer` from inside a coding session instead of by hand in a
   second terminal.
2. Enforce that review on developers who do not follow `tdd-phase-loop`.

`jeltz` is a **distribution repo**. It ships skills, hooks, and scripts that get
installed into other projects (checked into a consumer project's root so that
project's `CLAUDE.md` picks them up) or onto individual developer machines. The
`CLAUDE.md` in this repo is a reference copy that ships to consumers; it is not
a rulebook governing `jeltz` itself.

Four assistants are in scope: Claude Code, codex, antigravity (`agy`), and
grok. All four can act as the reviewer, and all four can enforce the gate
(the "only three" premise fell during T19 - see 3.5).

Status: T1-T20 and T22-T25 complete; next task is T26. T32-T39 (T32-T37
added 2026-08-17 after T20's acceptance, T38 during T23, T39 during T24)
are Phase 5 work and must land before Phase 6. T21 (documentation) was moved out of Phase 4
to the end of Phase 5 on 2026-08-17: it documents installation, and
installation is rewritten by T23 (uv, now landed), T27 (install.sh
hardening), T33 (installer ships the engine), and T34 (installer wires the
gate) - three of which do not exist yet at all.

---

## 1. Problem

**Problem A - workflow friction (us).** The review is the only manual step in an
otherwise autonomous loop. Current practice: manually commit the WIP, switch to
a codex terminal, invoke `$skeptical-reviewer HEAD vs. the item 1 TODOs`
against the installed skill, then copy the findings back into the Claude coding
session by hand. The skill itself is not copy-pasted - it is installed on both
hosts and invoked by name. What is manual is the *terminal switch* and the
*copy-back*.

**Problem B - unenforced norms (everyone else).** Other developers on the
codebase generate code with GenAI assistants and skip per-task review entirely.
The result misses project norms (100% coverage, passing tests, clean
`make verify`) and this surfaces only when something downstream breaks. Asking
has not worked. The gate has to be mechanical, and it has to work on whichever
assistant they happen to use.

---

## 2. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Re-review happens in the **existing reviewer thread**, told to check amended work against its own previous blockers. | Locked |
| D2 | ~~Load the skill via `base-instructions` on the codex MCP tool.~~ **Reversed by the T8 A/B (2026-08-16): the codex adapter invokes the installed skill via `codex exec`; the MCP path is not shipped.** See 2.1. |
| D3 | The Stop hook gate is in scope and is the primary deliverable for Problem B. | Locked |
| D4 | The reviewer backend is pluggable. Codex is the default; no backend's billing model is assumed permanent. | Locked |
| D5 | The reviewer **must not author code**. Cache and artifact writes (pytest, ruff, mypy, coverage) are expected and permitted; modifications to tracked source are not. | Locked, restated |
| D6 | **Every reviewer adapter is a subprocess transport.** All four hosts expose the same shape - spawn a CLI, pass a prompt, parse structured output, resume by id - so adapters stay uniform ~50-statement `_send` primitives behind the T7 template method, and timeout/kill semantics come free from the process boundary. MCP or vendor-SDK integrations are out unless a needed capability is unreachable from the CLI (none is today; the T8 A/B in 2.1 is the supporting evidence, and 3.6 forces shell-out anyway because hooks cannot reach host MCP tools). | Locked (2026-08-16, generalizes the T8 D2 settlement to T9-T11) |

### 2.1 D2 should probably be reversed

D2 was chosen when the working assumption was that the skill had to be
*injected* into a codex session. That assumption was wrong. The skill is
already installed on codex and invoked by name, and that flow works today.

Verified: `codex debug prompt-input '$skeptical-reviewer HEAD vs. the item 1
TODOs'` passes the string through as a literal 45-character user message. It is
not expanded at the CLI layer. The model resolves it against the
`<skills_instructions>` block, which lists each skill's name, description, and
`file:` locator, and reads `SKILL.md` itself.

So the lowest-risk adapter sends exactly the prompt string used today, to a
session where the skill is installed. That is reproducing a known-good flow
rather than changing it.

This matters beyond fidelity, because D2 constrains the engine (section 3.2):

| Approach | Engine | Structured output | Thread resume |
|---|---|---|---|
| Installed skill, invoked by name | `codex exec` | `--output-schema` | `codex exec resume <id>` |
| `base-instructions` injection | MCP over stdio JSON-RPC | none (skill must self-format) | `codex-reply` |

Dropping D2 removes the JSON-RPC client entirely and gains native schema
enforcement. Recommendation: default to the installed skill, keep
`base-instructions` behind a config switch, and A/B them in T8.

**Settled by the T8 A/B (2026-08-16, live, codex-cli 0.147.0).** Both paths
reviewed the same seeded fixture (a `greet.py` violating CLAUDE.md norms):

| Axis | exec + installed skill | MCP + `base-instructions` |
|---|---|---|
| Verdict parse reliability | `--output-schema` enforces the shape structurally | self-formatted fence; worked once, unenforced (the R2 risk) |
| Token cost | 71,011 in / 2,216 out (48,384 cached) | 46,712 in / 1,707 out (24,832 cached) |
| Review quality | 4 blockers, per-norm granularity | 3 blockers, norms merged into one finding |
| Client complexity | subprocess + JSONL | stdio JSON-RPC client; 618 `codex/event` notifications in one review |

The MCP path is ~35% cheaper on input tokens because `base-instructions`
replaces codex's ~14.5k-token default system prompt, but structural verdict
enforcement and a trivially simpler client win for automation. D2 is
reversed; the MCP client is not shipped (revisit only if token cost becomes
the binding constraint). Grok's `--system-prompt-override` can still supply
a second injection data point in T10 if wanted.

---

## 3. Verified findings

Verified 2026-08-14 against codex-cli **0.147.0**, Claude Code **2.1.232**,
antigravity `agy` **1.1.13**, and grok **1.0.4**. Version-pinned; re-verify
before relying on them (section 8).

### 3.1 The codex MCP server is generic, not a hardcoded reviewer

`codex mcp-server` exposes exactly two tools:

- `codex` - `prompt` (required), `base-instructions`, `developer-instructions`,
  `cwd`, `model`, `sandbox`, `approval-policy`, `config`, `compact-prompt`.
- `codex-reply` - `threadId`, `prompt`.

A live round-trip returns `{"structuredContent": {"threadId": "...", "content":
"PONG"}}`. Each `codex` call opens a new thread; `codex-reply` continues it.

The hardcoded reviewer is the `codex review` CLI subcommand, backed by the
built-in `~/.codex/skills/.system/review-agent` skill. It is not exposed over
MCP. Do not use it; it competes with our skill.

### 3.2 `codex exec` cannot override base instructions

No `--base-instructions` flag. Under `--strict-config`: `base_instructions`,
`experimental_instructions_file`, `instructions_file`, and `user_instructions`
are rejected as unknown fields. `instructions` and `developer_instructions`
parse but do nothing - `codex debug prompt-input -c instructions="ZZMARKERZZ"`
is byte-identical to baseline (14585 bytes both ways, marker absent).

Consequence: `base-instructions` is available only over MCP. See 2.1.

### 3.3 All four hosts use the same SKILL.md format

| Host | Skill discovery |
|---|---|
| Claude Code | `~/.claude/skills/`, project `.claude/skills/` |
| Codex | `$CODEX_HOME/skills/` (project scope unverified, T2) |
| Antigravity | `.agents/skills/` at repo root, or `~/.gemini/config/` |
| Grok | `.grok/skills/` **plus Claude Code's `.claude/skills/` and `~/.claude/skills/` natively** |

All four use YAML frontmatter with `name` and `description`, and all use
progressive disclosure. One authored `SKILL.md` serves every host, which is the
premise of shipping them from this repo.

### 3.4 Antigravity is a viable host, with sharp edges

`agy` 1.1.13, auth `oauth-personal` (subscription, not an API key).

```sh
agy -p "..." --output-format json --model gemini-3.1-pro-low
# {"conversation_id":"...","status":"SUCCESS","response":"...",
#  "duration_seconds":4.07,"num_turns":1,"usage":{...}}
```

- `--conversation <ID>` resumes a thread, satisfying D1.
- `--json-schema` enforces structured final output.
- `--model` offers gemini-3.7/3.6/3.5-flash, gemini-3.1-pro, gpt-oss-120b, and
  claude-sonnet-4-6 / claude-opus-4-6-thinking. Routing to Claude models through
  agy is a different billing path than Claude Code and defeats model diversity
  when the coder is already Claude. Prefer gemini-3.1-pro for review.
- Baseline 7.5k-15.6k input tokens per fresh conversation; 4-8s round trip.

Customizations live in `.agents/` at the repo root and are explicitly designed
to be checked into VCS - `skills/<name>/SKILL.md`, `hooks.json`,
`mcp_config.json`, plus `skills.json` / `plugins.json` for registering
directories elsewhere in the tree via workspace-relative paths. The vendor docs
recommend this pattern by name.

**Sharp edge 1 - project skills need a project context.** With
`.agents/skills/pong-check/SKILL.md` present and `.agents/skills.json`
registering it, `agy -p "list every skill available to you"` returned only the
two built-in skills. Adding `--new-project` to the identical command returned
`agy-customizations, antigravity-guide, pong-check`. Same repo, same files, one
flag.

**Sharp edge 2 - permission denial is a silent success.** When a skill needs a
tool call headless mode cannot prompt for, the run is auto-denied and returns
`status: "SUCCESS"` with `response: ""` (or the `response` key omitted - agy
drops empty JSON keys), with only a stderr note. An adapter that trusts
`status` will record an empty review as a pass. The correct fix is an
allow-rule under `permissions.allow`, not `--dangerously-skip-permissions`.
Resolved by the T9 probe (agy 1.1.13, live):
- **Syntax:** grant strings of the form `command(<target>)`, matched by
  command prefix (`command(git)` covers `git log`, `git status`, ...). The
  spelling comes from agy's own denial message and its hooks contract
  (`permissionOverrides: ["command(npm test)"]`). File-read tools are
  auto-allowed headless; only `command` (and edit) permissions deny.
- **Location:** the user-global `~/.gemini/antigravity-cli/settings.json`
  (the file agy's vendor docs name as *the* CLI configuration), under
  `permissions.allow`. A project-scoped `.agents/settings.json` carrying
  the same schema does NOT take - probed live with the correct syntax in
  both a trusted and an untrusted workspace, denial persisted in both.
- **Verified live (2026-08-16):** with
  `"permissions": {"allow": ["command(git)", "command(make)"]}` added to
  the user-global settings.json (applied by the human - the session's
  harness rightly blocks an agent from editing permission config), the
  exact probe that had been auto-denied ran to a full response with no
  stderr note, and a complete two-round review ran end to end under the
  allowlist (see T9 acceptance evidence).

**Sharp edge 3 - workspace trust.** `~/.gemini/antigravity-cli/settings.json`
carries a `trustedWorkspaces` list. Consumer projects must be trusted.

### 3.5 Grok has the best reviewer interface (and, corrected: a Stop hook)

`grok` 1.0.4, logged in via grok.com on a **free plan**, single model
`grok-4.6`.

```sh
grok -p "..." --output-format json
# {"text":"PONG","stopReason":"end_turn","sessionId":"01a00215-...",
#  "requestId":"...","thought":"...","usage":{...},"num_turns":1,
#  "total_cost_usd":0.02682,"modelUsage":{...}}
```

Strengths, all verified:

- Richest headless contract of the four: final text, stop reason, session id,
  request id, reasoning trace, per-model usage, and a computed cost.
- `--json-schema` constrains output and implies `--output-format json`.
- `--resume <id>`, `--fork-session`, `--session-id` cover D1 cleanly.
- `--system-prompt-override` is a true base-instructions equivalent, and
  `--rules` appends to the system prompt - so the D2 experiment is available
  here even though it is not on `codex exec`.
- `--tools` / `--disallowed-tools` / `--allow` / `--deny` / `--permission-mode`
  give precise control for D5 and R6.
- `--worktree` / `--worktree-ref` are built in, which may simplify T6.
- 3.5s round trip, ~13k input tokens baseline.

**It already reads our Claude Code configuration.** `grok inspect` in this repo
reports the five `~/.claude/skills` entries as `user [claude]`, loads
`CLAUDE.md` as project instructions, and falls back to
`.claude/settings.local.json` for permissions. Its documented compatibility
table covers `.claude/skills/`, `.claude/agents/`, `.claude/plugins/`,
`~/.claude/plugins/installed_plugins.json`, `.mcp.json`, `CLAUDE.md`, and
`.claude/settings*.json`. Claude Code **plugins** are consumed whole, including
`hooks/hooks.json`. Nothing needs to be installed for grok specifically.

**~~The disqualifying gap: no Stop hook.~~ Corrected during T19: grok DOES
ship a blocking `Stop` hook** (verified live, grok 1.0.4, 2026-08-17 - the
original 2026-08-14 finding missed it). The hook is Claude-Code-compatible
(`{"decision": "block", "reason": ...}` on stdout; the reason reaches the
model - proven by a headless round trip whose response grew a second line
after the block). Full contract in 3.6. The originally planned deny-at-edit
`PreToolUse` workaround is therefore unnecessary and was not built; T19
shipped a standard Stop shim instead (see the T19 requirement change).

**Operational caveat.** Interface quality and operational suitability are
separate axes. The interface is the best of the four; the free plan's rate
limits and availability are not guaranteed. That makes grok a strong fallback
and a weak default primary, on interface grounds alone.

### 3.6 Stop-blocking support is not universal

| Host | Mechanism |
|---|---|
| Claude Code | `Stop` hook returns a top-level `{"decision": "block", "reason": "..."}` or exits 2 with the reason on stderr; `hookSpecificOutput` decisions belong to other events (PreToolUse, PermissionRequest). Receives `stop_hook_active`. Default timeout 600s. (Re-verified against the hooks reference during T16 review; the row previously recorded a nested deny schema that Claude Code ignores for Stop.) |
| Codex | Speaks the Claude-Code-style Stop protocol verbatim: `Stop` hook blocks with a top-level `{"decision": "block", "reason": "..."}` on exit 0 (or exit 2 with the reason on stderr); other nonzero exits fail open. Input adds `turn_id`, `model`, `permission_mode`, `last_assistant_message`, nullable `transcript_path`; receives `stop_hook_active`. Config: `~/.codex/hooks.json` or `[[hooks.Stop]]` tables in `config.toml`. Trust model: non-managed hooks need one-time trust via `/hooks`; `--dangerously-bypass-hook-trust` skips it (never recommend). Caveat: hooks in repo-local `.codex/config.toml` reportedly do not fire in interactive sessions (openai/codex#17532) - install at user scope. (Verified against the codex hooks reference during T17; codex-cli 0.147.0.) |
| Antigravity | `Stop` hook blocks with `{"decision": "continue", "reason": "..."}` on stdout (continue = keep working; the reason reaches the model); silence allows. Input is camelCase: `workspacePaths` (list of every mounted workspace root, `--add-dir` mounts more than one; ordering semantics undocumented - there is no `cwd`), `executionNum` (counts Stop-hook firings within one stop cycle: 0 on the first attempt, incrementing on each forced continuation, and resetting to 0 for each independent stop - proven by resuming a conversation whose previous stop had reached 1 and observing the next cycle start at 0; the `stop_hook_active` analog), `terminationReason` (`NO_TOOL_CALL` on a normal print-mode stop), `fullyIdle`, `conversationId`, `transcriptPath`, `artifactDirectoryPath`, `modelName`, `error`. Config: `.agents/hooks.json` at the workspace root or user-global `~/.gemini/config/hooks.json` under `{"<hook-name>": {"Stop": [{"type": "command", "command": ..., "timeout": ...}]}}`. Default timeout 30s. Caveat: in print mode, hooks (like skills, 3.4) fire only with a project context - `--new-project` or an existing project. Also `PostInvocation` with `terminationBehavior: "force_continue"`, and `PreToolUse` with `deny`. (Verified live during T18: dump-hook payload capture, a continue-decision round trip, and a resumed-conversation probe of the `executionNum` reset; agy 1.1.13.) |
| Grok | `Stop` hook blocks with the Claude vocabulary: top-level `{"decision": "block", "reason": "..."}` on stdout (exit 2 with stderr also blocks; other failures fail open); the reason reaches the model. Input is camelCase: `workspaceRoot` (grok resolves it to the git root even when the session's `cwd` is a subdirectory; arrives with a trailing slash), `cwd`, `stopHookActive` (true on every fire after a block this turn - the `stop_hook_active` analog), `reason` (`end_turn` on a genuine stop; an extra observe-only Stop fires at session end with `shutdown`/`channel_closed`, its decision parsed but ignored - a gate MUST filter on `end_turn` or it records denials the session can never act on), `hookEventName`, `sessionId`, `promptId`, `permissionMode`, `timestamp`, `transcriptPath`, `lastAssistantMessage`, `backgroundTasks`, `sessionCrons`. Grok force-stops after 8 continuations per turn. Config: any `*.json` under project `.grok/hooks/` (silently skipped until one-time folder trust via `/hooks-trust` or `--trust`, stored in `~/.grok/trusted_folders.toml`) or `~/.grok/hooks/` (always trusted); also `[[hooks.Stop]]` in config.toml layers, and Claude-compat sources (`.claude/settings*.json`) are scanned too. Default Stop-gate timeout 600s. (Verified live during T19: dump-hook payload capture headless, a block-decision round trip, a `stopHookActive` continuation fire, the session-end observe fire, and a subdirectory-launch probe of `workspaceRoot`; grok 1.0.4. Corrects the 2026-08-14 "no Stop hook" finding.) |

Antigravity's 30-second default hook timeout is far too short to run a review
inline, so the gate must read a pre-computed state file rather than perform the
review. That constraint is what makes one gate implementation portable.

A hook is an external process and cannot call its host's MCP tools. It shells
out to the review engine, which is why the engine is a standalone script.

### 3.7 Claude Code headless billing is paused, not settled

Anthropic's support page states that Claude Agent SDK, `claude -p`, and
third-party app usage "still draw from your subscription's usage limits" - the
June 15, 2026 move to a separate API-rate credit pool was paused before taking
effect. Two constraints follow:

- `ANTHROPIC_API_KEY` in the environment flips Claude Code to API billing. The
  Claude adapter must scrub it or refuse to run.
- Never use `--bare`. Its help text says auth is "strictly ANTHROPIC_API_KEY or
  apiKeyHelper ... OAuth and keychain are never read".

Per D4 this argues for a clean adapter boundary, not for preferring Claude as
the reviewer. Same-model-family reviewers also share blind spots.

---

## 4. Target architecture

### 4.1 Review loop

Two entry paths reach the same engine. The gate does not run the review - it
denies the stop and hands back an instruction that makes the coding session run
it (T15).

```
  coding session (Claude Code / codex / antigravity / grok)
        |                                    |
        | tdd-phase-loop PHASE 4 (T20)       | Stop attempted with
        |                                    | unreviewed changes
        |                                    v
        |                              gate hook (T14) - state file only
        |                                    |
        |                                    | deny + recovery instruction (T15)
        |                                    v
        |                              coding session runs the loop
        |                                    |
        +------------------+-----------------+
                           v
                     review/run.sh
        |
        | 1. build packet (TODO ref, WIP message, diff, verify tail)
        | 2. materialize disposable worktree; commit WIP inside it
        | 3. dispatch to reviewer adapter
        |
        +--> adapters/codex   (installed skill, "$skeptical-reviewer <ref>";
        |      exec resume for re-review)
        +--> adapters/agy     (--new-project, --json-schema, --conversation)
        +--> adapters/grok    (--json-schema, --resume, --tools allowlist)
        +--> adapters/claude  (fallback; API-key preflight)
        |
        | 4. parse structured verdict
        | 5. write .jeltz/review/state.json
        v
  exit 0  accepted
  exit 10 requires changes  --> reviewer-response in the coding session
  exit 20 escalate to human --> stop, print dossier
```

The disposable worktree does three jobs: it lets the reviewer run `make verify`
freely (D5 permits cache writes), it keeps any accidental edit away from the
developer's tree, and - because the orchestrator commits the WIP *inside* it -
it hands the reviewer a real commit to review. That preserves the current
mental model (`$skeptical-reviewer HEAD vs. ...`) without requiring the
developer to commit first, and without the loop committing to their branch.

State (`.jeltz/review/state.json`) carries task ref, round number, reviewer
thread/session/conversation id, the **diff hash** of the reviewed state,
per-blocker id history, and the last verdict. The diff hash keeps the gate
honest: a review is valid only for the tree state it examined.

### 4.2 Termination policy

A **round** is one review. Round 1 is the initial review; every later round is a
re-review of amended work. The cap is three rounds total - the initial review
plus at most two remediation cycles.

Escalate to a human on any of:

1. Round count reaches 3 and the verdict is still REQUIRES CHANGES.
2. A blocker id reappears after the coder claimed it fixed (thrash).
3. `reviewer-response` marks a blocker INVALID and the reviewer re-asserts it
   (genuine disagreement; needs a tiebreak, not another round).
4. Packet exceeds the size ceiling (the task was too large to review).

Conditions 2 and 3 require blocker ids stable across rounds, which is why the
verdict cannot be scraped from prose.

### 4.3 Distribution matrix

Four hosts, but only **three packaging targets for skills**, because grok
consumes the Claude Code layout natively (3.5):

| Target | Project scope (checked into consumer repo) | User scope | Serves |
|---|---|---|---|
| Claude Code layout | `.claude/skills/`, `.claude/settings.json`, or a plugin with `hooks/hooks.json` | `~/.claude/` | Claude Code, grok |
| Codex layout | unverified - T2 spike | `$CODEX_HOME/skills/`, `config.toml` | Codex |
| Antigravity layout | `.agents/skills/`, `.agents/hooks.json`, `.agents/skills.json` | `~/.gemini/config/` | Antigravity |

**Exception (T19): the Stop-gate hook is per-host even on grok.** Grok does
scan `.claude/settings.json` hooks, but it would then run the T16 shim, whose
snake_case `stop_hook_active` guard and unfiltered session-end fires do not
match grok's camelCase payload (3.6). The grok gate therefore ships as its own
hook file under `.grok/hooks/` invoking `review/grok_stop.py` (T34 installs
the wiring, T21 documents it), and grok project hooks need a one-time
`/hooks-trust` (or `--trust`) grant.

Packaging the Claude Code target as a **plugin** is worth evaluating in T2:
grok discovers plugin skills, agents, hooks, and MCP servers as a unit, so one
plugin directory could cover two hosts with a single install step.

Project scope is strongly preferred for Problem B: a developer who clones the
repo gets the gate without installing anything, and the gate is versioned with
the code it guards.

The T2 installer covers only the skills rows of this matrix. Shipping the
review engine into consumers is T33; installing the per-host stop-gate
wiring is T34 - today neither is installed by anything (verified
2026-08-17: install.sh handles skills only).

---

## 5. Risks and open questions

- **R1. Silent adapter failures.** Antigravity returns `SUCCESS` with an empty
  response on permission denial (3.4). Assume every host has an equivalent.
  Every adapter needs a positive-output assertion, not a status check.
- **R2. Verdict parsing.** Native schema enforcement exists on grok, agy, and
  `codex exec`; not on the codex MCP path. Where unavailable, parse a fenced
  JSON block, allow one repair round, then escalate. Never infer from prose.
- **R3. Gate false positives.** The gate must not fire on doc-only edits or
  sessions that touched nothing tracked, or the first person it annoys will
  delete it. Primary adoption risk for Problem B.
- **R4. Gate bypass.** Anyone can delete a hook (and grok's project hooks
  additionally need a one-time folder-trust grant before they run at all,
  3.6). This raises the floor for honest mistakes; it is not an adversarial
  control. CI remains the real backstop and should eventually verify a review
  record per commit.
- **R5. Hook timeouts vary wildly** - 600s on Claude Code, 30s default on
  antigravity. The gate must be a fast state-file check; the review runs
  elsewhere.
- **R6. Reviewer needs tool permissions.** A review that cannot run `git show`
  or `make verify` is worthless, but the reviewer must not author code (D5).
  Each host expresses this differently: codex `--sandbox` / `approval-policy`,
  Claude Code `--tools` without Edit/Write, agy `permissions.allow`, grok
  `--tools` / `--deny`. The allowlist is per-host and ships as config.
- **R7. Tool allowlists cannot enforce D5 on their own.** Every reviewer needs
  shell access to run `make verify`, and shell access can edit source. Removing
  the dedicated edit tool (grok `search_replace`, Claude `Edit`/`Write`) raises
  the bar but does not close the hole. The failure is not merely untidy: a
  reviewer that patches its own checkout, reruns the tests, and passes them is
  reporting a verdict about a tree that is not the submitted work. The
  disposable worktree bounds the blast radius but detects nothing. Mechanical
  enforcement therefore has to be post-hoc integrity verification of the review
  checkout (T6), not permissions alone.
- **Q1.** Is the review keyed to a TODO item or a bare diff? Problem A implies
  the former; the developers in Problem B have no TODO item. The skill needs a
  "no TODO item supplied" mode that reviews against `CLAUDE.md` norms alone.
- **Q2.** Is `.jeltz/` per-clone and untracked (like the existing
  `claude-hook-mode` override) or committed as an audit trail?
- **Q3.** Does the consumer project pick the reviewer backend, or the
  developer? A project-scoped default with a per-developer override is the
  likely answer, but it changes the config file layout.

---

## 6. Tasks

Ordered. Each is intended to be one red/green/refactor cycle unless noted.

### Phase 0 - distribution foundations

#### T1. Repo tooling for shipped artifacts - DONE
Goal: make the shipped scripts testable without pretending `jeltz` is a
consumer project.

Delivered: root `Makefile` with `lint` (shellcheck `-x -P hooks` + `shfmt -i 4
-ci -d`, wildcarded over `hooks/*.sh` and `hooks/lib/*.sh` so new scripts are
covered automatically; the two formatting flags moved into `.editorconfig` in
T25), `test` (pytest from a self-bootstrapping stdlib
`.venv`), and `verify` (lint + test). Tests in `tests/test_makefile.py`;
`.gitignore` added for the venv and tool caches.

Decisions recorded:
- **Test runner: pytest driving subprocesses**, not `bats`. T4's verdict
  parser is Python and mandates pytest per `CLAUDE.md`, so bats would be a
  second runner; neither was installed.
- **Bootstrap: `python3 -m venv` + pip**, not `uv run --with pytest` - the
  installed uv 0.8.1 fails with a `--with` overlay bug on the Homebrew
  interpreter ("failed to read from file .../uvx: stream did not contain
  valid UTF-8"). Revisit if uv is upgraded.
- **No pyproject.toml / ruff config added**, deliberately: a tracked
  `[tool.ruff]` section would flip `hooks/lib/repo-mode.sh` to strict for
  this tree. The venv approach needs no packaging file at all.
- **Coverage standard for `jeltz` itself:** `CLAUDE.md`'s 100% rule is a
  consumer norm for Python application code. `jeltz` holds itself to:
  shellcheck- and shfmt-clean shell, and behavioral pytest coverage of every
  shipped artifact's acceptance criteria - no numeric coverage gate while the
  codebase is shell. Revisit when Python modules land (T4).

#### T2. Multi-host, multi-scope installer - DONE
Goal: one source of truth, installed correctly on any host and scope.

Delivered: `install.sh` (project scope default, `--check`, `--user`),
`make check-install TARGET=<dir>`, tests in `tests/test_installer.py`.

Spike results (2026-08-15, codex 0.147.0, agy 1.1.13):
- **Codex DOES discover project-scoped skills**, in both `.codex/skills/`
  and `.agents/skills/` - not in `.claude/skills/` or bare `skills/`.
- **Both codex and antigravity follow a symlinked `.agents/skills`.** With
  `.agents/skills -> ../.claude/skills`, codex's prompt input and agy's
  skill list (with `--new-project`) both report the `.claude/skills`
  content. Antigravity needed **no `.agents/skills.json`** for the standard
  location, so none is generated.
- Resulting project layout, one directory serving four hosts:
  `.claude/skills/<name>/` (real copies; Claude Code + grok native) plus
  the committed `.agents/skills` symlink (codex + antigravity).

Decisions recorded:
- **Plugin packaging: deferred.** The plain `.claude/skills/` layout already
  covers Claude Code and grok at project scope; a plugin only helps
  user-scope distribution and hook bundling. Revisit when the hook shims
  (T16-T18) ship.
- **Drift stamp:** `.claude/skills/.jeltz-manifest` - `# jeltz <short-sha>`
  header plus one sha256 line per installed file. `--check` reports
  `DRIFTED`/`MISSING` per file AND validates that `.agents/skills` is a
  symlink resolving to `.claude/skills` (added after review: a missing or
  retargeted link is drift for codex/antigravity even when every file
  hashes clean). Exit 1 on any drift. Reinstall repairs all of it,
  including the link having been replaced by a real directory.
- **User scope** installs real copies into `$HOME/.claude/skills` and
  `$CODEX_HOME/skills` (default `~/.codex`). Antigravity user scope is
  explicitly out of T2's scope - see the Deferred section for why and for
  the re-entry condition.
- **Caveat:** the symlink requires symlink-capable checkouts/filesystems;
  Windows consumers without developer mode would need the duplicated-copy
  fallback (not implemented).

### Phase 1 - the skill contract

#### T3. Revise `skeptical-reviewer/SKILL.md` - DONE
Goal: keep what works, add what automation needs.

Delivered: rewritten `skills/skeptical-reviewer/SKILL.md`; contract pinned by
`tests/test_reviewer_skill.py` (11 behavioral tests against the skill text,
including parsing the embedded verdict example).

The revised contract:
- Ref argument defaulting to `HEAD`; committed mode reviews the ref's diff
  and commit message(s).
- Uncommitted mode (requested explicitly, or ref is the literal word
  `uncommitted`): reviews `git diff HEAD` plus untracked files; the task
  description stands in for the commit message.
- Q1 handled: with no TODO item, review against `CLAUDE.md` norms alone,
  without inventing task-level requirements.
- All `@CLAUDE.md` / `@TODO.md` expansions replaced by a "read these files
  first" step with plain relative paths.
- D5 in prose: read, run, and analyze freely (cache/artifact writes OK);
  never author or modify source, tests, or config. Restated in Hard
  Constraints. Mechanical enforcement remains T6/R6.
- Re-review mode: judge each prior blocker by id as resolved / unresolved /
  regressed; ids stable across rounds; no unrelated new lines of attack
  unless they are genuine blockers, whatever round introduced the flaw.
- Machine-readable verdict: exactly one fenced JSON block ending the review,
  `{schema_version: 1, verdict, round, blockers: [{id, file, line, claim,
  why}], non_blockers: [...]}` - the verdict of record; prose is for humans.

Decisions recorded:
- **Verdict enum for the JSON block:** `ACCEPTED`,
  `ACCEPTED_WITH_NON_BLOCKERS`, `REQUIRES_CHANGES` (machine-safe forms of
  the three prose verdicts). T4's schema must use these.
- **Re-review blockers carry a `disposition` field** (resolved / unresolved /
  regressed) in the JSON block, giving T13's thrash detection structured
  input.
- **Invocation examples are argument-shaped, not host-shaped** - no
  `$skill-name` / `/skill-name` prefix in the skill body, since each host
  spells invocation differently.
- **Both verdict arrays are always required** (empty when a category has no
  findings) - added after the live spike caught agy omitting an empty
  `non_blockers` key.
- **Re-review admits any genuine blocker, whatever round introduced the
  flaw** (added after review): restricting new findings to
  amendment-introduced ones would suppress issues missed in round 1.

Acceptance evidence (live four-host spike, 2026-08-15): the installed skill
was run headless on claude 2.1.233, codex 0.147.0, agy 1.1.13, and grok
1.0.4 against the same fixture commit (a `greet.py` violating three
CLAUDE.md norms, no TODO item). All four produced a norms-only review,
independently found the same three violations, and emitted a parseable
verdict block with complete blocker fields. The four real verdict blocks
are committed as `tests/fixtures/reviewer-verdicts/<host>.json`, asserted
by `test_all_four_hosts_emitted_valid_verdicts`, and are the golden
fixtures T4's parser must accept ("validates against T4" is closed out when
T4's schema lands, next task).

Adapter findings from the spike (feed into T8-T11):
- **claude:** a user-scope copy of the same skill name shadows the project
  copy in headless mode - the adapter prompt must point at the project
  skill path explicitly (or user installs must be kept current).
- **agy:** headless `command` permission is auto-denied (3.4 sharp edge 2
  reconfirmed); a project `.agents/settings.json` `permissions.allow`
  guess did NOT take (resolved in T9: the syntax was close, but the file
  must be the user-global settings.json - see 3.4). Workaround that
  produced a full review: instruct file-tool-only review. Also: agy omits
  empty JSON keys unless told not to.
- **grok:** with partial `--allow` rules, the first tool call outside the
  allowlist silently ends the run - `stopReason: "cancelled"`, exit 0,
  narration-only text, no stderr (an R1 silent failure; T10 must assert on
  verdict presence, never on exit status). Rules use `Bash(...)/Read(...)`
  prefixes; `--allow Read --allow "Bash(git*)"` was not sufficient for a
  full review; `--always-approve --deny Edit --deny Write` completed in 6
  turns at $0.12. Flag placement: `--max-turns` before `-p`. (Resolved in
  T10: the adapter sidesteps permission rules entirely by passing the
  shipped allow list as `--tools` - grok's built-in tool allowlist, under
  which a headless Bash call completes with `end_turn`, verified live -
  and asserts on the envelope's stopReason, never exit status.)

#### T4. Verdict schema and parser - DONE
Goal: the verdict block becomes a contract code can enforce, not a convention.

Delivered: `review/verdict.schema.json` (standalone JSON Schema, draft
2020-12, shippable verbatim as `--json-schema` input; `strict_schema()`
derives the variant codex `--output-schema` requires) and
`review/verdict.py` (`parse_verdict`, `validate_verdict` with semantic
checks, `parse_with_repair`, typed `VerdictError` hierarchy). Contract
pinned by 27 tests in `tests/test_verdict.py`, including validation of all
four T3 golden host fixtures, the SKILL.md embedded example, and a live
codex strict-mode verdict (`tests/fixtures/codex-strict-verdict.json`).

Behavior as specified in the original acceptance:
- Valid input: the single verdict-shaped fenced block is extracted and
  validated; fenced JSON without a `schema_version` key (e.g. quoted
  config) is ignored, so reviewers quoting JSON do not break parsing.
- Empty/whitespace output: `EmptyOutputError`, `repairable = False`, never
  retried (R1: a dead adapter must not be repaired into a pass).
- Missing block, unparseable JSON, schema violation, multiple
  verdict-shaped blocks: distinct typed errors, all `repairable = True`.
- `parse_with_repair(text, rerun)` allows exactly one repair round: on a
  repairable failure it sends a concrete re-emit instruction through the
  `rerun` callback and parses the result; a second failure escalates by
  raising. Adapters (T8-T11) supply `rerun` as a same-thread follow-up.
- Semantic validation (added after review; blocker rule amended after the
  T5 review): a schema-valid verdict that contradicts its own findings
  raises `SemanticViolationError` (repairable). A blocker is **active**
  unless its `disposition` is `resolved`: accepting verdicts must carry no
  active blockers (resolved prior blockers stay listed so their ids
  survive an accepting re-review - the T5 join), `REQUIRES_CHANGES` must
  carry at least one active blocker, `ACCEPTED` carries no non-blockers
  while `ACCEPTED_WITH_NON_BLOCKERS` carries some, and finding ids must be
  unique across both arrays (they drive T13 thrash tracking).

Schema decisions recorded:
- Findings in **both** arrays require `id`/`file`/`line`/`claim`/`why`
  (all four golden fixtures already comply); `line` is an integer >= 1.
- Optional `disposition` on findings, enum resolved/unresolved/regressed,
  **nullable** (null means absent, so strict-mode output round-trips);
  `round` is an integer >= 1. Every property carries an explicit `type`.
- The canonical schema permits extra keys (no `additionalProperties:
  false`) so a host adding fields does not fail an otherwise sound
  verdict. Codex `--output-schema` enforces OpenAI strict structured
  output and needs the opposite, so `strict_schema()` mechanically
  hardens a copy: every object closed, every declared field required
  (optional keys become required-but-nullable), canonical stays the
  validation source of truth.

Native-host schema evidence (live probes, 2026-08-15):
- **codex 0.147.0:** `--output-schema` with the canonical schema is a 400
  (`'additionalProperties' is required to be supplied and to be false`);
  property schemas also need an explicit `type` (bare `const` rejected).
  The `strict_schema()` variant was accepted end to end; the emitted
  verdict is committed as `tests/fixtures/codex-strict-verdict.json`.
  Adapter note (T8): schema-constrained output arrives as raw JSON, so
  validate it with `validate_verdict`, not the fence-extracting
  `parse_verdict`.
- **agy 1.1.13:** `--json-schema <file>` accepted the canonical schema;
  the response still arrived fence-wrapped inside the JSON envelope, so
  the T9 adapter should run it through `parse_verdict` anyway.
- **grok 1.0.4:** `--json-schema` takes the schema **inline as a JSON
  string, not a file path** (a path is rejected with `invalid JSON`);
  with `"$(cat schema.json)"` it emitted raw, schema-valid JSON
  (T10 note).

Repo decisions recorded:
- **The T1 coverage revisit is settled:** Python under `review/` carries a
  100% line-coverage gate (`pytest-cov`, `--cov-fail-under=100` in
  `make test`); shell keeps the behavioral-pytest standard.
- New venv deps: `jsonschema` and `pytest-cov` plus transitives, all
  MIT/permissive. Dependencies live in a tracked list and `make test`
  depends on it through a venv stamp (added after review), so editing the
  list reinstalls into an existing venv instead of leaving checkouts with
  T1's pytest-only venv failing at import. (The list was
  `requirements-dev.txt` until T22 replaced it with `pyproject.toml`.)
- Root `conftest.py` puts the repo root on `sys.path` so shipped Python
  modules import without packaging metadata (which T1 deliberately avoids;
  T22's pyproject.toml is metadata-only and installs no jeltz code, so this
  still holds).

#### T5. Update `reviewer-response/SKILL.md` - DONE
Goal: make the fixer's output a machine-joinable half of the review loop.

Delivered: rewritten `skills/reviewer-response/SKILL.md`; contract pinned by
10 behavioral tests in `tests/test_response_skill.py` (same text-as-contract
approach as T3, including parsing the embedded response example).

The revised contract:
- Findings are consumed **by the verdict's stable finding ids**: every id
  gets exactly one classification, no paraphrased titles, no skipped ids.
- New output section E ends the response with exactly one fenced JSON
  block: `{schema_version: 1, round, dispositions: [{id, disposition,
  reason}]}`, disposition enum `fixed` / `rejected-invalid` /
  `deferred-non-blocker`, same ids the reviewer used - so the orchestrator
  diffs it mechanically against the next round's verdict (a `fixed` id
  coming back unresolved/regressed is condition-2 thrash; a
  `rejected-invalid` id coming back at all is condition 3).
- Deadlock rule (termination condition 3) stated: a rejection the reviewer
  re-asserts is genuine disagreement - escalate to a human tiebreak with
  both positions; never re-reject, never silently capitulate.
- Silent scope expansion forbidden: every change must map to a specific
  finding id and section B must say which; forced collateral edits are
  declared under the id that forced them.
- Host-portable text: 7-bit ASCII throughout, `@CLAUDE.md` expansion
  replaced with a plain-path read instruction, final-status lines now
  ASCII literals (`-- ` not em dash) so automation can match them
  byte-for-byte.

Decisions recorded:
- **Response block detection key is `dispositions`**, not `schema_version`
  alone, so tooling scanning fixer output never confuses it with a verdict
  block (which `parse_verdict` identifies by `schema_version`).
- **The three final-status lines are part of the contract** and pinned as
  exact ASCII literals; T12/T20 may match them literally.
- The example response reuses the `gate-ignores-symlink` id from the
  skeptical-reviewer example, demonstrating the cross-skill id join.
- **Verdict semantics reconciled (added after review):** the T4 validator
  had rejected any accepting verdict with a non-empty blockers array,
  which made the successful join unrepresentable exactly when every
  blocker was fixed. Now a blocker is *active* unless disposed
  `resolved` **in a re-review (round 2+)**; round one has no prior
  blockers, so a round-one disposition never deactivates anything (a
  strict-mode host is forced to emit the key on fresh findings - the
  live codex fixture carries round-one `unresolved` - and a mislabeled
  `resolved` must not bypass the gate). Accepting re-reviews list prior
  blockers as `resolved` (skeptical-reviewer SKILL.md states this
  explicitly) and `REQUIRES_CHANGES` needs an active blocker.
  `tests/test_response_skill.py` executes the round-two join end to end
  through `validate_verdict` instead of merely asserting the skill
  mentions it.

### Phase 2 - the review engine

#### T6. Packet builder and disposable review worktree - DONE
Goal: the review engine's foundation - what the reviewer sees, where it runs,
and the mechanical enforcement of D5.

Delivered: `review/packet.py`, `review/worktree.py`, and a shared
`review/gitcmd.py` git runner; behavior pinned by 13 tests in
`tests/test_packet.py` and 20 in `tests/test_worktree.py`, on a real-git
`dirty_repo` fixture in `tests/conftest.py`. All `review/` modules hold the
100% coverage gate.

Behavior as specified in the original acceptance:
- `build_packet(repo, wip_message, todo_ref=None, verify_output="",
  size_ceiling=...)` returns a frozen, deterministic `Packet` (WIP message,
  optional TODO ref per Q1, `git diff HEAD`, sorted untracked list, last 50
  lines of verify output) with a `render()` for the reviewer prompt. Over
  the ceiling it raises `PacketTooLargeError` (termination condition 4)
  instead of truncating.
- `tree_state_hash` / `Packet.diff_hash`: sha256 over the binary tracked
  diff plus each untracked file's path and content digest
  (length-delimited). Stable across rebuilds of the same tree; changes on
  any tracked or untracked content change; gitignored files affect nothing.
- `review_worktree(repo, wip_message)` context-manages a detached worktree:
  applies the developer's `diff HEAD --binary`, copies untracked files,
  commits the WIP inside with the engine's own identity and `--no-verify`
  (consumer hooks and git config cannot block or alter materialization),
  `--allow-empty` so a clean tree still yields a reviewable HEAD. The
  developer's tree is never touched; cleanup (remove + prune + temp dir)
  is unconditional in `finally`, surviving crashes and even the checkout
  being deleted out from under it.
- Integrity check (R7): `snapshot()` records HEAD plus the untracked set;
  `verify_integrity()` raises `IntegrityError` on HEAD movement, any
  tracked mutation (edit, delete, staged anything), or a novel untracked
  write - naming the offending paths. Caches (`.venv`, `__pycache__`,
  pytest/ruff/mypy caches, coverage artifacts) are allowlisted by explicit
  fnmatch pattern.

Decisions recorded:
- **The consumer's .gitignore is not the allowlist.** The check lists with
  `git status --porcelain -uall --ignored=matching`, so a write into an
  ignored path (e.g. `*.log`) still surfaces and must pass the explicit
  CACHE_ALLOWLIST - closing the "hide the write somewhere gitignored"
  channel the R7 text warned about. Packet-side untracked listing keeps
  excluding ignored files (they are not reviewed content).
- `IntegrityError` is a distinct typed error, never a verdict, per T7's
  requirement that a failed check cannot be recorded as an accepted review.
- Cleanup is branch-free on purpose: `worktree remove --force` is
  best-effort (unchecked), then always `worktree prune` + temp-dir removal;
  `git worktree remove --force` already succeeds on a deleted checkout, so
  a conditional fallback was unreachable code.
- grok's built-in `--worktree` is superseded: the host-neutral worktree is
  where the WIP commit and the integrity snapshot happen, so every adapter
  (T8-T11) receives the same checkout; per-host worktree features go unused.

Hardened after review (three blockers, all reproduced by the reviewer):
- **The integrity check does not trust the index.** `git status` goes
  blind when the reviewer runs `update-index --assume-unchanged` (edits,
  deletions, and type swaps all hidden - reproduced in tests), so
  `snapshot()` also fingerprints every file in HEAD's tree straight from
  disk (type, content sha256, executable bit; symlinks fingerprint their
  literal target) and `verify_integrity()` re-fingerprints and compares.
  The status scan stays as the first-line check for staged mutations and
  novel untracked writes.
- **Symlinks are reviewed faithfully.** The diff hash digests an untracked
  symlink's literal target (never the target's content - retargeting
  between equal-content files changes the hash, and a broken link still
  hashes), and materialization copies with `follow_symlinks=False` so the
  reviewer sees the developer's symlink, not a regular-file copy.
- **The hash covers the git-significant mode.** An untracked regular file
  contributes 100755 vs 100644 alongside its content digest (second
  review round): git commits the executable bit, so chmod +x changes the
  WIP commit under review and must invalidate a prior review.
- **Crash-safety is recovery, not just `finally`.** SIGKILL (what a hook
  timeout does, R5) skips `finally` and leaks the checkout plus its git
  registration - reproduced by killing a subprocess mid-review. Each
  review writes a pid marker beside its checkout; `reap_stale_worktrees()`
  removes any `jeltz-review-*` worktree whose owner is dead (missing or
  garbage marker counts as dead, live reviews are never touched), and
  `review_worktree()` reaps on entry so the next review self-heals. T12's
  orchestrator should also call it at startup.

#### T7. Adapter interface - DONE
Goal: pin the contract before writing four of them.

Delivered: `review/adapter.py` and the shipped `review/tool-allowlists.json`;
contract pinned by 18 tests in `tests/test_adapter.py`, all driven by a
scripted fake adapter with no network call, per the acceptance. `review/`
holds the 100% coverage gate (adapter.py adds 53 statements).

Behavior as specified in the original acceptance:
- `ReviewerAdapter.review(packet, worktree, mode=new|resume, thread_id)`
  returns a frozen `ReviewResult(verdict, thread_id, raw)`. The prompt is
  `packet.render()`; `raw` is the output the verdict was parsed from.
- Subclasses (T8-T11) implement only `_send(prompt, worktree, thread_id) ->
  (raw, thread_id)`. The `review()` template method owns everything a host
  could get wrong: mode/thread validation (resume requires a thread id, new
  forbids one, unknown modes fail fast), the positive-output assertion (R1:
  empty output raises `EmptyOutputError` with NO repair attempt), and T4's
  single same-thread repair round (R2: the repair instruction goes back on
  the thread the review ran on; a second bad output escalates by raising).
- Typed transport errors: `AdapterError` base, `AdapterProcessError` for a
  backend that died - never a verdict, and the worktree is still cleaned up.
- `conduct_review(repo, adapter, wip_message, todo_ref, verify_output, mode,
  thread_id, size_ceiling)` runs one full round: build packet, materialize
  the T6 worktree, snapshot, dispatch, verify integrity. An oversized packet
  raises `PacketTooLargeError` before any reviewer contact (condition 4);
  a reviewer that edits a tracked file raises `IntegrityError` even when its
  verdict was ACCEPTED - the verdict is discarded, so the orchestrator can
  never record a tampered review as accepted (R7).
- `tool_policy(host) -> ToolPolicy(allow, deny)` loads the shipped per-host
  allowlist config (R6); unknown hosts raise KeyError. Policies are
  non-empty and non-contradictory for all four hosts; edit tools are denied
  by name (claude/grok Edit+Write, grok search_replace, codex apply_patch).

Decisions recorded:
- **The contract layer is a template method, not a convention.** R1 and R2
  live in `ReviewerAdapter.review()`, so no host adapter can forget the
  empty-output assertion or mishandle the repair protocol; adapters are
  reduced to a transport primitive.
- **Integrity is checked inside `conduct_review`, after the adapter and
  before the result escapes the worktree context** - the accepting-verdict
  discard is structural, not a caller obligation.
- **Allowlists ship as data (`review/tool-allowlists.json`), not code**, so
  consumers can inspect and projects can override them without touching the
  engine; each entry carries a note tying it to its host task. The agy tool
  names are provisional until T9 resolves the `permissions.allow` schema.
- `mode` is a plain string pair ("new"/"resume") rather than an enum -
  it crosses a shell boundary in T12 (`run.sh --new|--resume`), where
  strings are the native currency.

Hardened after review (one blocker, reproduced by the reviewer):
- **Thread continuity is verified, not trusted.** The template method had
  validated only the caller's mode/thread pairing while accepting whatever
  thread id `_send` returned - so a backend answering a resume on a fresh
  thread (silently restarting the review without its prior findings), a new
  review returning no thread id (unresumable), or a repair round drifting
  to another thread all passed undetected. `review()` now raises a typed
  `ThreadContinuityError` (an `AdapterError`, never a verdict) in all three
  cases, and the fake adapter can script returned thread ids independently
  of the requested one so the echo behavior of a well-behaved backend can
  no longer mask the check.

#### T8. Codex adapter (plus the D2 A/B) - DONE
Goal: the default reviewer backend, and the A/B that settles D2.

Delivered: `review/codex.py` (`CodexAdapter`, 56 statements, 100% coverage);
18 tests in `tests/test_codex_adapter.py`, driven by a scripted fake codex
binary that speaks the live-probed JSONL dialect - no test contacts a real
backend. The D2 A/B ran live and settled D2 (section 2.1): the exec path
ships, the MCP `base-instructions` path does not, so there is no config
switch and no JSON-RPC client.

**REQUIREMENT CHANGE - accepted by accepting this task.** The original T8
text required the MCP alternate path to ship behind a config switch. That
requirement was written before the A/B (which T8 also required) existed;
the A/B rejected the MCP arm on verdict-enforcement and client-complexity
grounds, so shipping it would mean maintaining a dead stdio JSON-RPC
client under the 100% coverage gate purely as a record of the losing arm.
The alternate-path deliverable is therefore dropped, not implemented.
Human acceptance of T8 is acceptance of this change; reject it to have
the MCP path built as specified. Nothing is lost meanwhile: the losing
arm's numbers are recorded in 2.1, the MCP tool surface stays verified in
3.1 with re-probe commands in section 8, and 2.1 names the re-entry
condition (token cost becoming the binding constraint).

Behavior:
- Fresh review: `codex exec --json --sandbox read-only --output-schema
  <strict schema> "$skeptical-reviewer HEAD\n\n<packet>"` - the known-good
  installed-skill invocation framing the T6 packet, run with the disposable
  worktree as cwd. The strict schema is `strict_schema()` written to a temp
  file per send.
- Re-review (D1): `codex exec resume <thread> --json -c
  sandbox_mode="read-only" --output-schema ... <packet>` - the resume
  subcommand has no `--sandbox` flag (probed live on 0.147.0), so the config
  override is the supported spelling. Resume and repair sends are not
  re-framed with the skill invocation; the thread already has it.
- Stream parsing: thread id from `thread.started`, output from the LAST
  `agent_message` item (interim narration messages are ignored); non-JSON
  and non-object lines are skipped. A stream with no agent message degrades
  to empty raw output and no `thread.started` to an empty thread id - the
  transport reports what it saw, and the T7 template method's typed R1/D1
  checks own the failure.
- Bare schema-constrained verdict JSON is fenced before return so T4's
  fence-extracting parser accepts it; output that is prose, already fenced,
  or JSON that is not verdict-shaped passes through untouched.
- Transport failures are all `AdapterProcessError`: missing binary, nonzero
  exit (carrying stderr), and a hung backend killed at the timeout
  (constructor arg, default 600s) - never a hang, never a verdict.
- **The backend never sees the caller's stdin** (`stdin=DEVNULL`). Found by
  the first live acceptance run, which timed out at 600s: codex documents
  that piped stdin is appended to the prompt as a `<stdin>` block, so it
  blocks until EOF - and the engine will be invoked from hooks and wrapper
  scripts whose stdin is exactly an open-but-silent pipe. Reproduced
  deterministically in a test (a held-open pipe on fd 0 stalled the review
  pre-fix, passes post-fix); the identical live round completed in 59s once
  stdin was detached.

Decisions recorded:
- **`-o/--output-last-message` is not used**: the `--json` stream already
  carries the final message; a second channel would be a second parser.
- **The worktree is the subprocess cwd** rather than `--cd`, identically on
  both paths (resume lacks `--cd` anyway); resume-by-UUID is not affected
  by codex's session cwd filtering (probed live: resume from a different
  cwd than the session opened in works and preserves the thread).
- **Degrade, don't duplicate**: the adapter never raises for empty output
  or a missing thread id itself - those are the template method's R1 and
  D1 assertions, and duplicating them in a subclass would drift.

Acceptance evidence (live, codex-cli 0.147.0, 2026-08-16): `conduct_review`
ran end to end against a seeded fixture repo through the real CLI - round
one (mode new) returned a schema-valid REQUIRES_CHANGES with two blockers
and a thread id; round two (mode resume) came back on the SAME thread with
a round-2 verdict re-asserting both blocker ids with dispositions, passing
the D1 continuity check. Resume was issued from a different worktree cwd
than round one, confirming resume-by-UUID ignores session cwd filtering.
The killed-backend criterion is covered by the scripted-fake timeout test
(typed error in under 15s against a 30s hang) and was also exercised live
by the stdin stall the first acceptance run caught.

Hardened after review (two blockers):
- **Every spawn failure is typed, not just a missing binary.** `_run` had
  caught only `FileNotFoundError`, so a configured binary that exists but
  lacks the exec bit escaped as raw `PermissionError` (reproduced by the
  reviewer and by a regression test). The handler now catches `OSError` -
  the superclass of every process-start failure - and maps it to
  `AdapterProcessError` naming the binary, upholding T7's typed
  transport-error contract.
- **The dropped MCP deliverable is an explicit requirement change**, not a
  silent rewrite: recorded above with its rationale and rejection path, and
  bound to the human acceptance of this task.

#### T9. Antigravity adapter - DONE
Goal was: the agy backend behind the T7 template method, plus resolving the
`permissions.allow` schema.

Delivered: `review/agy.py` (46 statements, 100% coverage) and
`tests/test_agy_adapter.py` (20 tests against a scripted fake agy speaking
the live-probed 1.1.13 envelope dialect). The resolved permissions schema
is recorded in 3.4 sharp edge 2 and reflected in
`review/tool-allowlists.json` (agy allow entries are now real
`command(<target>)` grant strings; deny names are agy's native
`edit_file`/`write_to_file`, pinned by test).

Behavior:
- Fresh review: `agy -p "/skeptical-reviewer HEAD\n\n<packet>"
  --output-format json --json-schema <canonical schema path>
  --model gemini-3.1-pro-high --new-project`. The canonical (not strict)
  schema ships verbatim as the --json-schema file, per the T4 live probe;
  `--new-project` is mandatory or project skills do not load (sharp
  edge 1). Model default is `gemini-3.1-pro-high` (spec: gemini-3.1-pro-*;
  ids verified via `agy models`).
- Re-review: same argv with `--conversation <id>` instead of
  `--new-project`, prompt sent raw (no skill re-framing on an existing
  conversation, mirroring T8). Probed live: the envelope echoes the same
  `conversation_id` and increments `num_turns`, and resume works without
  `--new-project` because the conversation carries its project.
- Envelope handling: `response` passes to the T4 parser verbatim - agy
  fences verdict JSON itself (T4 probe), so unlike codex there is no
  normalization step. `conversation_id` is the thread id; a missing id
  degrades to "" so the template method raises ThreadContinuityError
  (degrade-don't-duplicate).
- Hard errors (all AdapterProcessError, never a verdict, never a hang):
  spawn failure (OSError superclass), timeout (killed), nonzero exit
  (stderr attached), unparseable envelope, non-SUCCESS status, and the
  T9-specific one - **SUCCESS with an empty or missing `response`**,
  which is the live signature of a headless permission denial (sharp
  edge 2). This check deliberately lives in the adapter rather than
  deferring to the template method's EmptyOutputError: the denial reason
  exists only on stderr, which only the adapter can see, so the typed
  error carries it. stdin is /dev/null (same hazard class T8 hit live).

Decisions:
- Skill framing is the slash-command spelling `/skeptical-reviewer HEAD`
  (agy expands slash commands in print mode; codex's `$skill` spelling is
  codex-specific).
- `--print-timeout` is left at agy's default; the subprocess timeout is
  the engine's own enforcement, and a self-terminated agy surfaces
  through the status/empty-response checks anyway.

Acceptance evidence (live, agy 1.1.13, 2026-08-16):
- "A permission denial fails loudly": the denial probe reproduced sharp
  edge 2 exactly (SUCCESS + empty response + stderr-only reason); the
  adapter's typed error path is pinned by tests carrying the real stderr
  denial text.
- "Review runs end to end with an allowlist": after the human added
  `permissions.allow: ["command(git)", "command(make)"]` to the
  user-global settings.json (the harness rightly blocks an agent from
  editing permission config), the previously denied probe ran to a full
  response, and a two-round `conduct_review` acceptance run completed
  through the real backend with no blanket approval: round 1 fresh
  review returned REQUIRES_CHANGES (round 1, blocker `shout_placement`),
  round 2 resumed via `--conversation` and returned round 2 with the
  same blocker id on the SAME conversation (THREAD_PRESERVED True).
  Sharp edge 3 (workspace trust) is confirmed as user-global config in
  the same file.

Hardened after review (two blockers):
- **Allowlisted acceptance run executed and recorded** (above) - the
  completing commit had honestly declared it pending; the reviewer
  correctly held T9 open until the positive evidence existed.
- **The envelope boundary validates shape, not just syntax.** Valid JSON
  that is not an object (`[]`, `null`) and a null `response` under
  SUCCESS escaped as raw AttributeError, contradicting the documented
  typed-error guarantee; a null response is materially an empty response
  under sharp edge 2. All three now map to AdapterProcessError (the null
  response through the loud denial path), reproduced test-first with the
  reviewer's exact payloads, and a null `conversation_id` is normalized
  so it degrades to the template method's ThreadContinuityError.
- **(Round 2) Any non-string `conversation_id` degrades, not just null.**
  A truthy non-string id (e.g. the integer 123) had slipped through into
  ReviewResult.thread_id, where a later resume would splice it into
  subprocess argv and die as raw TypeError. It now degrades exactly like
  a missing id - ThreadContinuityError via the template method -
  reproduced test-first with the reviewer's payload.

#### T10. Grok adapter - DONE
Goal was: the grok backend behind the T7 template method, with preflighted
skill discovery and per-turn cost recording.

Delivered: `review/grok.py` (67 statements, 100% coverage) and
`tests/test_grok_adapter.py` (33 tests against a scripted fake grok
speaking the live-probed 1.0.4 envelope dialect). The shared
bare-verdict-fencing normalization moved to `review/adapter.py` as
`fence_bare_verdict` (codex and grok both emit schema-constrained output
bare; the codex adapter now imports it - behavior unchanged, pinned by
both hosts' suites).

Behavior:
- Fresh review: preflight `grok inspect --json` runs in the review
  checkout and raises a typed error unless the machine-readable skills
  array contains an exact, enabled `skeptical-reviewer` entry (3.5: no
  install step exists, so discovery is asserted, not assumed; probed
  live - a project `.claude/skills/` copy lists with source type
  `project`, ahead of any user copy). Then
  `grok --tools Read,Grep,Glob,Bash --always-approve --output-format json
  --json-schema <canonical schema INLINE as a JSON string - a file path
  is rejected> -p "/skeptical-reviewer HEAD\n\n<sequencing
  instruction>\n\n<packet>"`. The allow list comes from
  `tool-allowlists.json` at call time, so the shipped data stays the
  single source (D6/T7: allowlists ship as data - grok is the host where
  the data can actually be applied per-invocation).
- Re-review: same argv with `--resume <id>` prepended and no preflight or
  skill re-framing; probed live, the envelope echoes the same sessionId
  with context intact (D1).
- Envelope handling: when `structuredOutput` is present its parsed object
  is the verdict carrier (under tool use `text` concatenates the model's
  message with the structured output - probed live - so text is
  unreliable); otherwise `text` is used. Either way a bare verdict object
  is fenced via the shared `fence_bare_verdict` for the T4 parser. A
  non-string `sessionId` degrades to "" so the template method raises
  ThreadContinuityError (the T9 round-2 lesson, baked in from the start).
- Hard errors (all AdapterProcessError, never a verdict, never a hang):
  spawn failure, timeout, nonzero exit, unparseable or non-object
  envelope, non-string text, preflight failure or non-discovery, and the
  T10-specific one - **any stopReason other than `end_turn`**, the live
  signature of a run that died inside grok with exit 0 and
  narration-only text (the T3 silent-cancel edge: assert on the
  envelope, never on exit status). stdin is /dev/null.
- Cost recording: every successful turn's `total_cost_usd`/`usage` land
  on the round's `ReviewResult.costs` in turn order - review state a
  host-neutral writer can persist alongside the verdict and thread id,
  surviving separate per-round processes. The T7 contract grew a
  `costs` tuple (default empty; codex/agy report nothing) and a
  `_record_cost` hook on the base adapter. Envelopes reporting no
  telemetry contribute no entry. The calibration data for the round
  budget.

Sharp edges resolved live (grok 1.0.4, 2026-08-16):
- **Headless permission auto-cancel.** With only `--tools`, a non-git
  bash command silently cancels the run (stopReason `cancelled`, exit 0,
  num_turns 1); git commands complete because grok auto-approves its
  built-in safe commands. The adapter therefore passes
  `--always-approve`, bounded by `--tools` - the edit tools (Edit,
  Write, `search_replace`) do not exist in the session at all, which is
  strictly tighter than the spike's `--deny` pair. Bash can still write:
  D5 is enforced by the T6 integrity check (R7), pinned by a test where
  a fake-reviewer shell edit raises IntegrityError over an accepting
  verdict.
- **Schema-constrained placeholder verdicts.** Invoked with
  --json-schema alone, the model emitted a schema-valid placeholder
  verdict as its first message without running a single tool (blocker id
  literally `placeholder`). The fresh-review framing now carries an
  explicit sequencing instruction (review with tools first; the final
  message is the complete verdict, never a placeholder), after which the
  live run performed a full tool-using review.
- **`text` vs `structuredOutput`.** See envelope handling above.

Acceptance evidence (live two-round run through the real backend,
2026-08-16): round 1 fresh review returned REQUIRES_CHANGES (round 1,
blocker `shout-untested`, $0.101, 6013 output tokens - a genuine
tool-using review); round 2 resumed via `--resume` and returned round 2
on the SAME session with the same blocker id carried as `unresolved`
(THREAD_PRESERVED True, $0.096); both turns' cost/usage were recorded.
The shell-edit-caught-by-integrity-check clause is pinned at unit level
(see above).

Hardened after review (two blockers):
- **Discovery is an exact parsed entry, not a substring.** The preflight
  had substring-matched the human-readable `grok inspect` output, which
  would also match a config warning that the skill failed to load
  (reproduced by the reviewer). It now runs `grok inspect --json` and
  requires an exact skills-array entry with `compatibilityStatus`
  enabled; unparseable inspect output is a typed error. Reproduced
  test-first with the reviewer's warning-text payload, plus
  disabled-skill and prose-output cases; the new preflight verified
  live against grok 1.0.4. (Round 2) A non-array `skills` member (JSON
  null, a scalar) had escaped as raw TypeError; it is now failed
  discovery, reproduced test-first with the reviewer's payloads.
- **Telemetry enters review state through the contract.** cost_log had
  been process-local adapter state a host-neutral writer could not
  reach and separate per-round processes would lose; and missing
  envelope fields were recorded as None placeholders. Per-turn entries
  now ride on `ReviewResult.costs` (see Cost recording above) and
  telemetry-free envelopes contribute nothing - both reproduced
  test-first.

#### T11. Claude adapter - DONE
Goal was: the fallback claude backend behind the T7 template method, with
the 3.7 billing preflight.

Delivered: `review/claude.py` (50 statements, 100% coverage) and
`tests/test_claude_adapter.py` (30 tests, 31 instances, against a scripted
fake claude speaking the live-probed 2.1.233 envelope dialect, echoing
the sent session id like the real CLI). All four
adapters now share `run_backend` and the telemetry collector extracted
into `review/adapter.py` during refactor (behavior unchanged; each
adapter's private `_run` copy deleted).

Behavior:
- Preflight per 3.7: every invocation (fresh and resume alike) refuses
  with a clear message while `ANTHROPIC_API_KEY` is set - it flips Claude
  Code to API billing - unless the adapter was constructed with
  `allow_api_billing=True`. The refusal fires before any spawn. Never
  `--bare` (its auth is strictly API-key based).
- Fresh review: `claude --session-id <generated uuid4> --tools
  Read,Grep,Glob,Bash --output-format json --json-schema <draftless
  canonical schema, inline> -p "/skeptical-reviewer HEAD\n\n<project-path
  pointer>\n\n<packet>"`. The framing points at
  `.claude/skills/skeptical-reviewer` explicitly - the T3 spike found a
  user-scope copy of the same name shadows the project copy headless.
- Re-review: same argv with `--resume <id>` instead of `--session-id`,
  prompt sent raw. Probed live: the envelope echoes the same session_id
  on both fresh and resumed runs, and `--resume` preserves context. A
  fresh review verifies that echo against the generated id and raises
  ThreadContinuityError on mismatch (D1: verified, not trusted) - only
  the adapter knows the generated id, so this check cannot live in the
  template method; the resume path stays with the template's check.
- Envelope handling: `structured_output` (parsed object) is preferred as
  the verdict carrier; bare-JSON `result` text is normalized by the
  shared `fence_bare_verdict`; a missing or non-string `session_id`
  degrades to "" so the template method raises ThreadContinuityError
  (degrade-don't-duplicate). Per-turn `total_cost_usd`/`usage` ride
  `ReviewResult.costs` via the shared telemetry collector.
- Hard errors (all AdapterProcessError, never a verdict, never a hang):
  API-key refusal, spawn failure, timeout (killed), nonzero exit (stderr
  attached - probed live: an unknown --resume id exits 1 with stderr
  only), unparseable or non-object envelope, non-string `result` without
  structured output, and a failed run - `is_error` true or `subtype` not
  `success`, either signal alone. stdin is /dev/null.

Sharp edge resolved live: claude 2.1.233's `--json-schema` validator
rejects any schema declaring the 2020-12 draft ("no schema with key or
ref ..."), while the schema body - `$id` and `$defs` included - validates
unchanged. `review/verdict.py` gained `draftless_schema()` (canonical
minus the `$schema` declaration), reproduced test-first after the first
live acceptance attempt failed on it.

Acceptance evidence (live two-round run, 2026-08-16, claude 2.1.233):
preflight refusal fired with "ANTHROPIC_API_KEY is set: claude would run
on API billing (3.7); unset it or opt in with --allow-api-billing";
round 1 opened fresh session b7275069-45a3-42ab-bcc9-de3f94ffbcf1 and
returned REQUIRES_CHANGES with three genuine blockers (shout-no-tests,
print-in-production, missing-type-hints; $0.748, 6937 output tokens);
round 2 resumed the same session, round 2, all three blockers carried
`unresolved`, THREAD_PRESERVED True ($0.529). Both turns' cost/usage
landed on `ReviewResult.costs`.

Hardened after review (one blocker): the generated `--session-id` had
been discarded after sending - any nonempty envelope session_id was
accepted as the thread, so a backend answering on an existing or
unrelated session would have been silently resumed by every later
round. Reproduced test-first with an unechoed-session fake (review
succeeded pre-fix); the fresh path now retains the generated id and
raises ThreadContinuityError on an echo mismatch, the fake echoes the
sent id like the real CLI (live probe evidence: p1/p2 envelopes echo
`--session-id`/`--resume` exactly), and the fresh-session and repair
tests now assert the loop's thread ids ARE the generated ids.

#### T12. Orchestrator: one round - DONE
Goal was: `review/run.sh --new | --resume`: packet, worktree, dispatch,
parse, state write; exit codes 0 / 10 / 20; human-readable review to stdout,
machine state to `.jeltz/review/state.json`. Acceptance: exit code and state
file agree with the verdict in every fixture case.

Delivered: `review/run.py` (107 statements, 100% coverage) behind
`review/run.sh`, a thin POSIX wrapper that resolves the checkout and execs
the module (`python3 -c`, so there is no uncoverable `__main__` block);
20 tests (25 instances) in `tests/test_run.py`, driven in-process plus one
end-to-end run of `run.sh` itself, all through a scripted fake codex binary
on PATH - no test contacts a real backend. Makefile `SH_SOURCES` now
wildcards `review/*.sh` so the wrapper sits under shellcheck/shfmt.

Behavior:
- CLI: exactly one of `--new` / `--resume`; `--backend agy|claude|codex|grok`
  (default codex per D4, `--new` only - a resumed review stays on its
  recorded backend, so `--resume --backend` is a usage error); `--repo`,
  `--wip-message`, `--todo-ref` (Q1: optional), `--verify-output FILE`,
  `--size-ceiling`, and `--allow-api-billing` for the claude preflight (3.7).
- Exit protocol: 0 for ACCEPTED / ACCEPTED_WITH_NON_BLOCKERS, 10 for
  REQUIRES_CHANGES, 20 escalate to a human - currently raised only by
  `PacketTooLargeError` (termination condition 4; conditions 1-3 are T13's),
  1 for every operational failure (dead backend, unrepairable verdict,
  tampered checkout, unusable state, unreadable verify file), 2 for usage
  errors (argparse). The reviewer's raw output goes to stdout; diagnostics
  go to stderr via logging.
- State (`.jeltz/review/state.json`, written via tmp-file + rename so a
  gate hook never reads a torn file): schema_version, backend, task_ref,
  thread_id, round, diff_hash, the full verdict object, and a history of
  per-round records `{round, verdict, blockers: [{id, disposition}],
  costs}` - the raw material for T13's thrash/dispute conditions and T14's
  hash comparison. `--resume` appends to history on round+1; `--new`
  resets to round 1 on a fresh thread.
- Startup calls `reap_stale_worktrees` before anything else (the T6 note),
  so a dead prior review is reclaimed even when the run then fails early.

Decisions recorded:
- **`.jeltz/` is never reviewable content.** `untracked_files` (gitcmd) now
  excludes it, which keeps review state out of the diff hash, the packet,
  and the worktree materialization alike. Found red: writing `state.json`
  changed the very hash it records, so no review could ever match the tree
  it examined and the T14 gate would force re-reviews forever.
- **The diff hash is computed before dispatch, not after.** If the
  developer edits the tree mid-review, the recorded hash mismatches the
  tree and the gate forces a re-review; hashing afterward would record the
  edited tree as reviewed when the reviewer saw the older one.
- **A failed round never writes state.** Transport, verdict, and integrity
  failures exit 1 with the previous valid record intact - an accepting
  verdict over a tampered checkout is discarded (R7), not persisted.

Hardened after review (one blocker, reproduced red):
- **The verdict's declared round is verified, not trusted.** The declared
  round decides whether dispositions may deactivate blockers, so a fresh
  review declaring round 2 with "resolved" blockers laundered unresolved
  findings into an exit-0 acceptance and wrote state whose top-level round
  contradicted the verdict's. `parse_with_repair` now takes the
  orchestrator's `expected_round` (threaded through `review()` and
  `conduct_review`); a mismatch is a repairable `WrongRoundError` - one
  same-thread repair, then failure with no state write. Tested for --new
  and --resume, including the repaired-on-thread success path.

#### T13. Escalation policy engine - DONE
Goal was: all four termination conditions from 4.2, plus the escalation
dossier (disputed blockers, both sides' positions, round history), each
condition independently triggerable.

Delivered: `review/escalation.py` (engine, 128 statements) plus wiring in
`review/run.py` (now 153 statements); behavior pinned by 22 tests
(33 instances) in `tests/test_escalation.py` and 10 new orchestrator tests
(13 instances) in `tests/test_run.py`. All `review/` modules hold the 100%
coverage gate.

Behavior:
- `parse_response` extracts the coder's T5 section E disposition block from
  either bare JSON or full fixer output, keying detection on the pinned
  `dispositions` key so a verdict block in the same output is never
  mistaken for the response, and validates it (schema_version 1, integer
  round, enum `fixed` / `rejected-invalid` / `deferred-non-blocker`,
  non-empty unique ids, per-entry reason).
- `evaluate(state, response)` joins the response against the round's
  verdict by stable finding ids and decides conditions 1-3:
  condition 1 when round >= 3 (`MAX_ROUNDS`) and the verdict is still
  REQUIRES_CHANGES; condition 2 (thrash) when a `fixed` id comes back
  not-resolved, or the reviewer itself marks a blocker `regressed`;
  condition 3 (dispute) when a `rejected-invalid` id comes back at all -
  even listed as `resolved`, since that claims a fix that never happened.
  All triggered conditions are reported together; each disputed blocker
  appears exactly once, carrying both sides' positions verbatim.
- `packet_escalation` covers condition 4, and `render_dossier` writes the
  human tiebreak dossier: conditions, per-blocker coder/reviewer
  positions, and the round history (or "no completed rounds").
- `review/run.sh` gains `--response-file` (resume-only; with `--new` it is
  a usage error, exit 2). The file is read and validated before dispatch -
  unreadable, blockless, or answering the wrong round is an operational
  failure (exit 1) that never contacts the reviewer.
- Every escalation exits 20 and writes `.jeltz/review/escalation.md`.
  Conditions 1-3 still record the round's state first (the round did
  complete; T14's gate needs it); condition 4 keeps writing no state.

Decisions recorded:
- **Escalation is a policy outcome, not a failure**: state is written
  before the exit-20 decision, so an escalated review is resumable by a
  human without losing the thread or the history.
- **`regressed` alone is thrash**: the reviewer marking a blocker
  regressed testifies that a previously resolved finding broke again,
  so condition 2 fires even when no response file was supplied.
- **The fenced-JSON pattern is shared**: `review/verdict.py` now exports
  `FENCED_JSON` and both extractors (verdict, response) use it, keeping
  the two halves of the wire format in lockstep.

Hardened after review (two blockers, both reproduced red):
- **Escalation is terminal.** The escalating round's state gains an
  `escalated` marker (the triggered conditions, recorded in the same
  atomic write as the round), and `_plan_round` refuses to resume past
  it - so round 4 can never run, let alone launder an escalated review
  into an exit-0 acceptance. Recovery is a human tiebreak followed by
  `--new`. Condition 4 needs no marker: no round ran, no state changed,
  and re-running the same command deterministically re-escalates.
- **Response evidence is mandatory and complete.** A resume past
  REQUIRES_CHANGES without `--response-file` is refused (conditions 2-3
  would otherwise be silently disabled), and `verify_coverage` enforces
  the T5 exactly-once contract before dispatch: every finding id from
  the prior verdict answered, no unknown ids. A voluntary re-review
  after acceptance still needs no response - there is nothing to answer.
  `_load_state` now also requires the recorded verdict itself.

### Phase 3 - enforcement

#### T14. Gate logic (host-neutral) - DONE
Goal was: one implementation, thin host shims; read
`.jeltz/review/state.json`, compare its diff hash against the tree, decide
allow / block with a reason string, within a 30s hook budget (R5), with the
R3 scope exclusions.

Delivered: `review/gate.py` (68 statements) plus `git_paths` in
`review/gitcmd.py` (now 15 statements); behavior pinned by 17 tests
(22 instances) in `tests/test_gate.py` and one packet regression test. All
`review/` modules hold the 100% coverage gate.

Behavior:
- `decide(repo)` returns a frozen `GateDecision(allow, reason)`. It only
  reads the state file and hashes the tree - it never runs a review.
- Allow paths, in order: per-clone opt-out marker
  `<git-common-dir>/info/jeltz-review-gate` whose first line reads `off`
  (the `claude-hook-mode` convention: per-clone, never committed); not a
  git repository; a tree git cannot diff (no commits yet) fails open;
  nothing changed (tracked or untracked); doc-only changes (suffixes
  `.md` / `.rst` / `.txt`, or anything under `docs/`); a recorded review
  whose diff hash matches the tree and whose verdict is ACCEPTED or
  ACCEPTED_WITH_NON_BLOCKERS; an escalated review (automation ended - a
  human owns it, and blocking the stop would trap the session).
- Block paths: unreviewed source changes with no usable state (corrupt
  state is "no review record", never a crash); a recorded hash that no
  longer matches the tree (the review is stale); a matching hash whose
  verdict is still REQUIRES_CHANGES.

Decisions recorded:
- **Untracked source gates the stop.** The changed-path set is the tracked
  diff plus untracked files, matching `tree_state_hash` scope - a new
  unreviewed module cannot bypass the gate just because it was never
  `git add`ed.
- **The gate fails open, never closed.** Every state it cannot gate (no
  repo, no HEAD, escalated) allows with a reason; R4 says this raises the
  floor for honest mistakes, and CI is the backstop - a deadlocked session
  is the one outcome that guarantees the hook gets deleted (R3).
- **The gate's state loader is looser than the orchestrator's** on
  purpose: it needs only `diff_hash`, `verdict`, and `escalated`, and
  anything unusable is a block-with-instruction, which T15 turns into a
  recovery path.

Hardened after review (one blocker, reproduced red):
- **Git path listings are NUL-delimited and fsdecode'd.** Newline-delimited
  git output C-quotes non-ASCII paths (`core.quotePath`), so a quoted doc
  name was misclassified as source and a quoted untracked path crashed
  `tree_state_hash` with FileNotFoundError - a crash in the hook path.
  `review/gitcmd.py` gained `git_paths` (runs with `-z`, decodes with
  `os.fsdecode`); both `untracked_files` and the gate's changed-path scan
  use it, fixing the packet builder and the gate together. Regression
  tests cover a tracked and an untracked non-ASCII doc (allowed), a
  non-ASCII source file after a review (blocks as stale, no crash), and
  the packet carrying the real on-disk path.

#### T15. Stop-gate recovery bridge - DONE
Goal was: make a denied stop actually produce a review. Without this, T14
blocks a noncompliant session and leaves it nowhere to go - and that session
is the entire Problem B audience.
Delivered: `review/bridge.py` (35 statements) exposing `attempt_stop(repo)`,
the single call every host shim (T16-T19) makes; behavior pinned by 10
tests in `tests/test_bridge.py`, including the end-to-end acceptance
fixture.
Behavior:
- Allows from the T14 gate pass through verbatim; nothing is written.
- A denial's reason ends with the literal recovery instruction: run
  `review/run.sh --new`; on exit 10 apply `reviewer-response`, then
  `review/run.sh --resume --response-file <response>`; repeat until exit 0
  or exit 20; on exit 20 stop and hand `.jeltz/review/escalation.md` to a
  human. Host-neutral prose - no `tdd-phase-loop` assumed.
- Repeat-denial guard: each denial merges `denied_hash` into
  `.jeltz/review/state.json` (atomic, via the orchestrator's shared
  `write_state`), and the bridge never denies twice for the same hash - the
  second attempt is allowed with a logged warning. New edits change the
  hash and re-arm the guard.
Decisions recorded:
- The denial marker merges into whatever the state file holds, preserving a
  real review record; a denial-only record satisfies neither the gate's nor
  the orchestrator's loader, so it cannot masquerade as a review. A
  completed round rewrites state wholesale, clearing the marker - each
  review cycle gets exactly one denial.
- Posture when the instruction is ignored: the gate has raised the floor
  and recorded the skip; the session stops with a logged warning and CI
  (R4) is the backstop. A hook cannot escalate further.
- `stop_hook_active` is Claude Code's host-specific field and is the T16
  shim's job; the bridge stays host-neutral.
Acceptance shown end-to-end: a session with no knowledge of
`tdd-phase-loop` edits tracked source, is denied once with the instruction,
runs the literal command against a scripted backend, reaches acceptance,
and stops cleanly - no deadlock, no second denial for the same hash, no
unbounded loop.

Hardened after review (one blocker, reproduced red):
- **An unrecordable denial fails open, never crashes.** With `.jeltz`
  present as a regular file, recording the marker raised
  NotADirectoryError out of the hook - no instruction returned, no marker
  written, so every retry would deny again and the outcome depended on
  each host's crash handling. `attempt_stop` now catches OSError from the
  state write, logs the failure, and allows the stop with a
  failing-open reason (CI is the backstop, R4). Regression test pins the
  posture.

#### T16. Claude Code Stop hook shim - DONE
Goal was: translate T14's decision to `decision: "deny"` / exit 2, carrying
T15's instruction as the reason; respect `stop_hook_active`; never block
twice for the same state.
Delivered: `review/claude_stop.py` (26 statements) exposing `main()`, the
first host shim over the T15 bridge; behavior pinned by 8 tests
(9 instances) in `tests/test_claude_stop.py`, including the end-to-end
acceptance fixture through the shim.
Behavior:
- Reads the Stop-hook payload from stdin; a denial is emitted as the
  documented Stop schema - a top-level `{"decision": "block",
  "reason": ...}` on stdout with exit 0, the reason being the bridge's
  verbatim gate reason plus recovery instruction. An allow is silent -
  no output, exit 0.
- `stop_hook_active` true allows immediately without consulting the
  bridge: the shim never contributes to a stop-hook loop and never
  records a denial for a stop it did not gate.
- Never blocks twice for the same tree: the bridge's repeat-denial guard
  reaches the host through the shim (second attempt is silent).
- Fail-open on unusable input: unparseable stdin, a non-object payload,
  or a missing/unusable `cwd` allows the stop with a logged error (CI is
  the backstop, R4).
Decisions recorded:
- Of the two documented blocking mechanisms (structured JSON with
  exit 0 vs exit 2 with the reason on stderr), the shim uses the
  structured-JSON path: the reason travels in a typed field instead of
  scraped stderr, and a constant exit 0 keeps the fail-open contract
  trivially auditable - every path out of `main` is an exit the host
  treats as success.
- No `__main__` block or shell wrapper in this task: `review/run.sh`
  sets the convention (a thin `.sh` resolves PYTHONPATH and execs
  `main()`), and hook wiring/installation is T21's deliverable - the
  Makefile's shell-lint list and installer tests pin any new script, so
  it must land with its own tests, not as a refactor side effect.
Acceptance shown end-to-end through the shim: a session edits tracked
source and tries to stop; the shim denies once with the instruction, the
session runs the literal command against a scripted backend, reaches
acceptance, and the next stop is silent.

Hardened after review (one blocker, reproduced red):
- **The denial now speaks Claude Code's actual Stop schema.** The shim
  emitted `{"hookSpecificOutput": {"hookEventName": "Stop", "decision":
  "deny", ...}}`, faithfully implementing section 3.6's research row -
  which was wrong: the hooks reference specifies a top-level
  `{"decision": "block", "reason": ...}` for Stop, and nests decisions
  under `hookSpecificOutput` only for other events (PreToolUse,
  PermissionRequest). A real Claude session would have ignored the
  denial entirely. Tests were flipped to the documented schema first
  (red), the emission fixed, and the 3.6 row corrected with a note on
  the re-verification. The schema test now pins the exact key set so a
  wrapper regression cannot sneak back in.

#### T17. Codex Stop hook shim - DONE
Goal was: same decision and instruction, codex hooks schema; document
installation under the trust model without
`--dangerously-bypass-hook-trust`.
Delivered: `review/codex_stop.py` exposing `main()`, the second host shim
over the T15 bridge; behavior pinned by 8 tests (9 instances) in
`tests/test_codex_stop.py`, including the end-to-end acceptance fixture
through the shim. Protocol research first: section 3.6's codex row
recorded only that a hooks system exists, and T16's blocker came from
exactly that kind of thin row, so the schema was verified against the
codex hooks reference before RED - codex speaks the Claude-Code-style
Stop protocol verbatim (same `cwd`/`stop_hook_active` payload core, same
top-level `{"decision": "block", "reason": ...}` block on exit 0). The
3.6 row now records the full verified protocol.
Behavior: identical to T16's gate - documented block schema carrying the
bridge's reason plus recovery instruction, silent allow,
`stop_hook_active` immediate pass-through, one denial per tree, fail-open
on unusable input, every exit 0. The codex tests pin this contract
independently of the Claude tests (every payload also carries the
codex-specific fields - `turn_id`, `model`, `permission_mode`,
`last_assistant_message`, null `transcript_path` - so tolerance of them
is pinned too), letting the hosts diverge later without silent breakage.
Installation under the trust model (documented in the module docstring;
README surfacing is T21): configure at user scope (`~/.codex/hooks.json`
or `[[hooks.Stop]]` in `~/.codex/config.toml`) and trust the hook once
interactively via `/hooks`; never pass `--dangerously-bypass-hook-trust`
- bypassing trust is exactly the habit the gate should not teach. User
scope also sidesteps the repo-local interactive-hooks caveat
(openai/codex#17532).
Refactoring: since the two hosts share one protocol, the gate body moved
from `review/claude_stop.py` to a host-neutral shared module
`review/stop_hook.py` (`gate_stop()`, the former `claude_stop.main`
verbatim); both host shims are now thin facades (`main = gate_stop`)
whose docstrings carry only host-specific facts (trust model and payload
extras for codex; the exit-0-vs-exit-2 choice and 600s timeout for
Claude Code). This replaces GREEN's cross-host layering (codex importing
from the claude module) and gives T18 a place to reuse the stdin
parse/fail-open scaffolding even though antigravity's emission schema
differs.
Acceptance shown end-to-end through the shim: a codex session edits
tracked source and tries to stop; the shim denies once with the
instruction, the session runs the literal command against a scripted
backend, reaches acceptance, and the next stop is silent.

#### T18. Antigravity Stop hook shim - DONE
Goal was: same decision and instruction as the other shims,
`{"decision": "continue", "reason": ...}` in `.agents/hooks.json`, well inside
the 30s default timeout.
Delivered: `review/agy_stop.py` exposing `main()`, the third host shim over
the T15 bridge, and `tests/test_agy_stop.py` (12 tests, 13 instances). Protocol
research first, per the T17 process guard: section 3.6's antigravity row was
verified live against agy 1.1.13 before any test was written - a dump-only
Stop hook captured the real payload (camelCase, `workspacePaths` list, no
`cwd`, no `stop_hook_active`), and a second probe proved that
`{"decision": "continue", "reason": ...}` re-enters the loop with the reason
reaching the model and that `executionNum` increments 0 -> 1 on the forced
continuation. The 3.6 row now records the verified contract, including the
print-mode caveat (hooks, like skills, need a project context).
Behavior: identical gate semantics to T16/T17 - silent allow, one denial per
tree carrying the bridge reason plus `review/run.sh --new`, immediate allow
without gating when `executionNum` is nonzero (agy's `stop_hook_active`
analog), fail-open with a logged error on unusable input or no usable
workspace path, every exit 0. Every entry of `workspacePaths` is gated in a
single pass (the gate treats non-repo directories as nothing-to-gate, so
extra mounts are safe); any denial from a multi-root payload - even a
single denying root - names each denying root and scopes the recovery with
`--repo <root>` on every `review/run.sh` command (the flag is top-level in
run.py, so it covers `--resume` too), and every root's denial is recorded
at once - necessary because the `executionNum` guard allows the continued
stop cycle wholesale. The 30s timeout is safe because the shim only reads
the pre-computed state file.
Review round 1 (two blockers): (1) the `executionNum` guard rested on a
single-cycle observation - resolved by proving the reset invariant live with
a resumed-conversation probe (a stop cycle after one reaching `executionNum`
1 starts at 0 again), per the reviewer's own condition; (2) gating only
`workspacePaths[0]` could let a clean first root mask a dirty later one
(`--add-dir` makes multi-root real) - fixed test-first: the gate now
iterates every usable entry, skipping non-string/empty ones, failing open
only when none remain.
Review round 2 (one blocker): short-circuiting on the first denying root
interacted fatally with the proven `executionNum` guard - the continued
stop cycle is allowed wholesale, so a second dirty root that was never
consulted on the first pass would never be gated at all. Fixed test-first:
the gate evaluates all roots before emitting, records every denial in one
pass, and a multi-root denial names each denying root with per-root
`--repo` recovery guidance; a two-dirty-root lifecycle test covers deny ->
guarded continuation -> per-root recovery -> silent stop.
Review round 3 (one blocker): the round-2 fix keyed the scoped guidance on
multiple *denials*, so a clean-primary/dirty-secondary payload still got
the unscoped instruction - which, run from the clean primary cwd, reviews
the wrong repository. Fixed test-first: the scoped per-root guidance now
keys on the payload being multi-root (after filtering unusable entries);
only a genuinely single-workspace payload keeps the byte-identical plain
reason. The clean-first/dirty-second test now asserts the denying root's
path, the `--repo` guidance, and a successful scoped recovery to a silent
stop.
Installation (README surfacing is T21): `.agents/hooks.json` at the workspace
root (designed to be checked into VCS) or user-global
`~/.gemini/config/hooks.json`.
Refactoring: `review/stop_hook.py`'s gate is now parametrized by a frozen
`StopProtocol` dataclass capturing the only three points on which the hosts
disagree - the loop-guard key (`stop_hook_active` vs `executionNum`), the
workspace-roots extraction (the `cwd` string vs the `workspacePaths` list,
every entry gated), and the deny decision word (`block` vs `continue`). `stop_hook.py` defines the
`CLAUDE_STYLE` instance shared by the Claude Code and codex facades; agy's
facade builds its own instance. All three shims are now thin facades whose
docstrings carry only host-specific facts. (The fail-open log line for a bad
workspace is now host-neutral: "no usable workspace" rather than "no usable
cwd".)
Acceptance shown end-to-end through the shim: an agy session edits tracked
source and tries to stop; the shim denies once with the instruction, the
session runs the literal command against a scripted backend, reaches
acceptance, and the next stop is silent.

#### T19. Grok Stop hook shim (was: deny-at-edit gate) - DONE
Goal was: the only enforcement shape available on grok, believed to be a
`PreToolUse` deny-at-edit gate because finding 3.5 said grok had no
stop-blocking hook event.

**REQUIREMENT CHANGE - accepted by accepting this task.** Pre-RED research
disproved the premise live (grok 1.0.4, 2026-08-17): grok ships a blocking,
Claude-Code-compatible `Stop` hook, proven by a headless round trip in which
`{"decision": "block", "reason": ...}` kept the agent working and the reason
reached the model (the response grew a second line), with `stopHookActive`
true on the continuation fire. The deny-at-edit gate was a workaround for a
gap that does not exist - its own spec called it "weaker than a stop gate"
and left the entry-condition threshold unresolved - so T19 shipped the
standard Stop shim in the T16-T18 shape instead (the bridge and gate
docstrings had always said "shims (T16-T19)"). Human acceptance of T19 is
acceptance of this change; reject it to have the deny-at-edit `PreToolUse`
gate built as originally specified. The corrected host contract is recorded
in 3.5/3.6 with the probe evidence.

Delivered: `review/grok_stop.py` (5 statements, 100% coverage) and
`tests/test_grok_stop.py` (10 tests, 12 instances) pinning grok's
live-verified contract; findings 3.5/3.6, R4, and 4.3 corrected.

Behavior:
- Runs the shared `review.stop_hook.gate_stop` under grok's protocol:
  loop guard `stopHookActive` (camelCase; true on every fire after a block
  this turn), workspace root from `workspaceRoot` (grok resolves it to the
  git root even when the session's cwd is a subdirectory - verified live by
  a subdirectory launch; gated instead of `cwd` because the state file and
  the recovery commands live at the root), deny decision `block` (Claude's
  vocabulary; the reason carries the T15 recovery instruction).
- Gates only `reason == "end_turn"`: grok also fires an observe-only Stop at
  session end (`shutdown`/`channel_closed`) whose decision is parsed but
  ignored - gating it would record a denial the session can never act on,
  burning the bridge's one-denial-per-tree guard for the next genuine stop.
  Implemented as a new optional `StopProtocol.gate_when` predicate
  (default: gate every fire), so the other three shims are untouched.
- Safety: grok force-stops after 8 continuations per turn and Stop gates
  default to a 600s timeout, so the shim can never trap a session even
  without its own guards; hook failures fail open on grok's side too.

Installation (T34 installs the wiring, T21 documents it): a JSON hook file
under project `.grok/hooks/`
(any name) with a `Stop` entry invoking the shim; project hooks are silently
skipped until a one-time folder-trust grant (`/hooks-trust` or `--trust`,
recorded in `~/.grok/trusted_folders.toml`). User scope: `~/.grok/hooks/`,
always trusted. The Claude-compat `.claude/settings.json` path is NOT used
for grok (see 4.3): it would run the T16 shim against a camelCase payload.

Acceptance: original criterion ("a grok session cannot silently accumulate
unreviewed changes past the configured threshold") is satisfied strictly
more strongly - no unreviewed source change survives a stop attempt at all.
`test_denied_session_reaches_acceptance_and_stops` runs the T15 loop end to
end through the shim; the deny round trip, continuation-fire guard,
session-end filter, and workspaceRoot resolution are each pinned by a test
mirroring a live capture.

#### T20. Wire `tdd-phase-loop` to the loop - DONE
Goal was: PHASE 3's terminal stop becomes PHASE 4 (REVIEW): run `--new`; on
exit 10 invoke `reviewer-response` in-session (keeping the coder's context),
then `--resume`; the human approval gate moves to after convergence.

Delivered: `skills/tdd-phase-loop/SKILL.md` rewritten around a fourth phase,
with the contract pinned by `tests/test_phase_loop_skill.py` (12 tests, the
T3/T5 skill-test pattern - the skill text IS the contract four hosts consume).

Behavior:
- REFACTOR no longer ends the workflow. Its literal is now
  `REFACTOR PHASE COMPLETE -- proceeding to REVIEW.` and PHASE 4 begins
  automatically; the terminal literal is
  `REVIEW PHASE COMPLETE -- awaiting human approval.` All four phase-stop
  lines are exact 7-bit ASCII literals automation can match byte-for-byte
  (the T5 precedent).
- PHASE 4 runs the loop from inside the coding session: `review/run.sh
  --new`; on exit 10 apply `reviewer-response` in this same session (the
  coder's context - TODO item, diff, reasoning - stays available to the
  fixer), save the complete output including the fenced `dispositions`
  block to a response file, then `review/run.sh --resume --response-file
  <response>`; repeat until exit 0 (proceed to the final STOP) or exit 20
  (stop, hand `.jeltz/review/escalation.md` to the human). Other exit codes
  are operational failures to fix and retry - never a reason to skip the
  review.
- The human gate moves to after convergence: the human approves work the
  reviewer has already accepted, or arbitrates an escalation dossier -
  never raw REFACTOR output. The single-human-stop rule and the no-commit
  rule survive unchanged.
- Host-portable text: 7-bit ASCII throughout (the pre-T20 file carried em
  dashes and curly quotes) and plain relative paths replacing the
  Claude-specific `@CLAUDE.md`/`@TODO.md` expansion syntax.

Decisions recorded:
- **The skill and the T15 bridge walk one path.**
  `test_review_phase_runs_the_bridge_commands` joins the skill text against
  `review.bridge.RECOVERY_INSTRUCTION`, pinning the same command literals
  in both - a session following the skill and a session recovering from a
  denied stop run identical commands, so neither can drift alone.
- **Exit codes drive the loop, not status-line matching.** PHASE 4 branches
  on `review/run.sh` exit codes (0/10/20); the reviewer-response
  final-status lines stay available (T5 pinned them as ASCII literals) but
  the skill does not depend on parsing them.
- **The phase-stop literals changed shape (em dash to ` -- `).** Anything
  matching the old em-dash `PHASE COMPLETE` literals must track the
  shipped file; the repo itself has no such matcher (verified by sweep).

Acceptance: the original criterion - a full task completes RED through
REVIEW with no terminal switching and no copy-paste - is satisfied by
composition: the skill's PHASE 4 commands are the same literals the T13
orchestrator tests execute end to end (`--new` through `--resume
--response-file` to exit 0/20), and the join test guarantees the skill
invokes exactly that machinery from within the session.

Hardened after review (two blockers, both reproduced red):
- **The instructed commands now carry the task context.** A bare `--new`
  reviewed under the CLI defaults (placeholder WIP message, no TODO ref,
  "verification not run") - a norms-only review, not a judgment of the
  implementation against the task. The skill's `--new` command now passes
  `--todo-ref`, `--wip-message` (the PHASE 3 commit message), and
  `--verify-output` (the saved `make verify` output); every `--resume`
  re-supplies the message and freshly re-run verify evidence, since only
  the task ref persists in review state. Evidence files live under
  `.jeltz/review/`, the one in-repo path excluded from the packet's
  untracked scan and diff hash, so saving them cannot dirty the tree
  under review. `test_instructed_new_invocation_reaches_reviewer_with_
  context` executes the skill's literal command against the scripted
  backend and asserts the packet the reviewer received carries all three
  values and none of the defaults.
- **The final commit message survives review fixes.** PHASE 3 writes the
  message before PHASE 4 may change code; the terminal summary previously
  restated it, handing the human a message describing a pre-review tree.
  Exit-10 remediation now ends by updating the message to cover the
  review-driven fixes, the updated form is what the resume submits and
  what the terminal summary presents, and the hard constraint reads:
  produced once in PHASE 3, updated (never reissued) in PHASE 4.
- **The message travels by file, never through shell syntax** (round-2
  blocker). The first fix interpolated the commit message into a
  double-quoted `--wip-message "..."` argument; double quotes do not stop
  backtick or `$` expansion, and this repo's own commit messages carry
  backticked commands. `review/run.py` gained `--wip-message-file`
  (mutually exclusive with `--wip-message`; read verbatim; an unreadable
  file exits 1 before contacting the reviewer), and both skill commands
  use it, with the message saved under `.jeltz/review/` alongside the
  other evidence. Pinned by CLI tests passing a hostile multiline message
  (backticks, `$()`, `$VAR`, both quote kinds) asserting byte-for-byte
  arrival in the packet, plus the skill-side executable test now routing
  the same hostile message through the skill's literal command; a skill
  test also asserts no inline `--wip-message` interpolation remains.

### Phase 5 - packaging, tooling, and hardening

#### T22. Packaging baseline: pyproject.toml with split dependencies - DONE
Goal: one declarative packaging file; requirements-dev.txt retired.

Delivered: `pyproject.toml` (project metadata, production dependencies,
PEP 735 dev dependency group, metadata-only build config); the Makefile
provisions its venv stamp from it with `pip install . --group dev` behind
a new `make venv` target; `requirements-dev.txt` deleted. Contract pinned
by 7 tests in `tests/test_packaging.py` plus the rewritten dependency-change
test in `tests/test_makefile.py`.

Behavior as specified in the original acceptance:
- **Dependencies split by audience.** `[project].dependencies` carries
  `jsonschema` - `review/verdict.py` imports it at module scope, and T33
  ships that module into consumer repos that never install jeltz's test
  tooling, so it is production, not dev. `[dependency-groups].dev` carries
  `pytest` and `pytest-cov` (`ruff` joined in T24). Tests assert both
  directions: nothing dev leaks into production and jsonschema is not
  demoted to dev.
- **Fresh-checkout provisioning from pyproject.toml alone**, proven by a
  test that provisions a venv in a scratch tree holding only the Makefile,
  the packaging file, and the two files it references - no
  requirements-dev.txt anywhere - then imports all three dependencies.
- **No `[tool.ruff]` section**, asserted directly and, more usefully, by a
  test that commits this exact pyproject.toml into a scratch git repo and
  asserts `hook_repo_mode` still answers `diff`. That is the acceptance
  criterion the T1 note warned about, now mechanically pinned rather than
  described.

Decisions recorded:
- **PEP 735 dependency groups, not extras, for dev tooling.** Groups are
  absent from distribution metadata, so a consumer provisioning jeltz's
  production dependencies (T33) structurally cannot pull pytest; an extra
  could be requested by name. pip 26.1 and uv both support `--group`, so
  T23's swap needs no re-modelling. Requires pip >= 25.1.
- **The distribution is metadata-only** (`[tool.setuptools] packages = []`).
  jeltz ships by file copy (install.sh), never as a wheel of its own code,
  so the build backend exists only to make the dependency metadata
  installable. This keeps T1's arrangement intact (root `conftest.py` puts
  the working tree on `sys.path`; nothing installed can shadow it) and
  avoids putting a top-level module named `review` - a very collidable name
  - into a consumer's site-packages.
- **`make venv` is a named target**, so provisioning is invokable and
  testable on its own rather than only as a side effect of `make test`;
  T23 changes what it runs, not what callers invoke.
- Version `0.1.0`, matching the existing `v0.1.0` tag; license declared as
  MIT with `license-files` (the repo's LICENSE.md), per CLAUDE.md's
  permissive requirement.

Hardened after review (one blocker, valid):
- **Provisioning no longer assumes a new enough pip.** `python -m venv`
  seeds the interpreter's BUNDLED pip, and `--group` needs pip >= 25.1
  while CPython 3.11 - the floor this task declares - bundles 24.0. A fresh
  checkout on any interpreter older than the development machine's would
  have died with `no such option: --group`, defeating this task's own
  acceptance criterion; the provisioning test could not see it because it
  used the ambient python3 (3.14, pip 26.1.2). The stamp recipe now raises
  the venv's pip past the dependency-group floor before installing (a no-op
  when already satisfied), and `PYTHON ?= python3` makes the interpreter
  substitutable so the old-pip path is testable at all. Pinned by a test
  driving a stub interpreter whose venv carries a pip that rejects
  `--group` exactly as 24.0 does - fully offline, and dropping the raise
  step fails it. (Superseded one task later: T23 removed pip from the
  Makefile entirely, so both the raise step and its stub-interpreter test
  are gone. The mechanism they guarded no longer exists; the equivalent
  guard is now T23's uv version floor.)
- Recorded, not acted on: the reviewer's non-blocker about the
  fresh-provisioning test installing floating dependencies from a live
  index. Every `make test` already provisions from the network, so this
  adds no new class of dependency, and T23 commits a lockfile - the durable
  determinism fix, one task away. A pip-side constraint file built now
  would be deleted there.

Fallout found and fixed in-task (test-first, like any other change):
`pip install .` makes setuptools write a `*.egg-info/` directory into the
tree it builds from. That is a novel untracked write, so the T6 integrity
check failed it as reviewer tampering (`IntegrityError`) - meaning every
review of this repo would have failed operationally the moment a reviewer
ran `make verify` in the disposable worktree. Reproduced by a new test in
`tests/test_worktree.py`, then allowlisted in `CACHE_ALLOWLIST` alongside
the other verification artifacts (it is fallout from any consumer whose
test target installs its own project, not a jeltz quirk), and added to
`.gitignore`.

#### T23. Move provisioning from pip to uv - DONE
Goal: uv is the single installer for dev and CI use.

Delivered: `tools/preflight.sh` (the external-prerequisite gate), a Makefile
rebuilt around `uv sync --locked --no-install-project` with a new `make lock`
target and overridable tool paths, the committed `uv.lock`, and a README
Development section. Contract pinned by 15 tests in
`tests/test_provisioning.py`; two pip-era tests retired and T1's staleness
test rewritten around the new mechanism.

Behavior as specified in the original acceptance:
- **No Makefile path invokes pip**, asserted over `venv`, `lock`, `test`,
  and `verify`. The check runs against an UNPROVISIONED scratch tree: in a
  checkout that is already current make prints no recipe at all, and a
  recipe nobody printed proves nothing (the first draft of this test passed
  vacuously for exactly that reason).
- **Every Python-ecosystem tool arrives via uv, at the pinned version.** A
  fresh checkout carrying only the Makefile, pyproject.toml, uv.lock, and
  the two files the packaging metadata references provisions, and the
  installed pytest / pytest-cov / jsonschema versions are compared against
  the lockfile's pins - identical, not merely importable. That is what
  committing the lock buys, so that is what the test asserts.
- **A stale lockfile stops the build instead of being rewritten**, proven
  offline: the scratch tree's pyproject.toml gains a dependency, `make venv`
  fails, uv.lock is byte-identical afterwards, and the message names both
  the file and `uv lock`.
- **External prerequisites fail fast, by name, with a remedy** - each of uv,
  shellcheck, and shfmt individually, and two missing at once reported in
  one pass rather than one build at a time.
- **The uv floor is enforced, not just documented:** a stub uv reporting
  0.4.0 is refused with both the found and required versions, and a stub at
  the floor is accepted and observed installing from the lockfile (the
  positive control - without it, a check that rejected every uv would pass
  the negative test). Both stubs are offline.
- **Standard targets are unchanged for callers**: `make lint/test/verify`
  keep their names, meaning, and gates (417 tests, 100% coverage on
  `review/`); only the provisioning underneath changed.
- **README documents the prerequisites and the floor in one place**, and the
  test reads the floor out of `tools/preflight.sh` so documentation and
  enforcement cannot drift apart silently.

Decisions recorded:
- **`uv sync --locked`, not plain `uv sync`.** Plain sync silently
  re-resolves and REWRITES uv.lock when pyproject.toml moves ahead of it.
  That is a tracked-file mutation, and the reviewer runs `make verify`
  inside the T6 worktree where any tracked mutation fails the review (R7) -
  so a developer who edited dependencies without re-locking would get a
  bogus integrity failure instead of a clear "re-lock" message. `--locked`
  refuses; `make lock` is the deliberate refresh path. Same reasoning
  retires the T22 non-blocker about installing floating dependencies from a
  live index: the pins now make a fresh provision reproducible.
- **`--no-install-project`.** The distribution is metadata-only (T22
  `packages = []`), so building jeltz during provisioning would install
  nothing while writing `build/` and `*.egg-info/` into the tree - the exact
  artifacts T22 had to allowlist in the integrity check. Not building them
  is better than allowlisting them, and a test asserts the tree stays clean.
  The allowlist entries stay: they exist for consumer repos whose own test
  target installs their own project.
- **The venv stamp is gone.** T1/T22 rebuilt a stamp file when the
  dependency declaration changed, because pip was too slow to run every
  time. An up-to-date `uv sync` costs ~20ms, so `test` now depends on an
  unconditional `venv`: cheaper than the staleness bug it removes, and a
  mechanism that cannot itself go stale.
- **The uv floor is 0.8.1 - the version actually verified here** - and it is
  checked at runtime. Older uv releases very likely work; none was
  available to test, and T22's blocker was precisely a declared floor that
  nothing verified. Documenting an unverifiable number would repeat it.
- **Tool paths are Makefile variables** (`UV`, `SHELLCHECK`, `SHFMT`). They
  exist so the prerequisite and version tests can point one tool at a stub
  or a nonexistent path without rewriting PATH, which is what makes the
  fail-fast behavior testable at all.
- **The preflight is a script, not an inline recipe**, so jeltz's own
  standard (shellcheck- and shfmt-clean shell) applies to it; `tools/*.sh`
  joined `SH_SOURCES`.
- License: uv is MIT/Apache-2.0, install-time tooling only and not a code
  dependency, per CLAUDE.md.

Hardened after review (no blockers; two of three non-blockers fixed):
- **Each target now requires only the tools it runs.** The first cut made
  `lint`, `venv`, and `test` all depend on one all-tools preflight, so
  `make lint` failed without uv and `make venv` failed without shellcheck.
  preflight.sh now takes the tools to check as arguments (defaulting to all
  three when run bare), `verify` still asks for everything so one run names
  every gap, and `lint`/`venv`/`lock` ask only for what they invoke. Pinned
  by tests that fail against the old wiring. Note for T24: ruff is
  uv-provisioned, so restoring lint's uv requirement is `lint:
  preflight-shell venv`, not a preflight change - recorded in T24.
- **The README test checks the join, not just the words.** It now asserts
  the `## Development` section preflight.sh names actually exists, and that
  each prerequisite carries an install command inside it. The assertions
  passed on first run because the documentation was already correct, so
  their teeth were confirmed by mutation: removing the section heading,
  removing shfmt's install command, and unsetting the documented floor each
  fail the test.
- Recorded, not acted on: the two vacuous lint tests. Filed as T38 during
  this task, and the reviewer independently agreed the deferral is right -
  the defect predates T23 and none of its provisioning coverage rests on
  those tests.

Collateral, declared:
- README.md was normalized to 7-bit ASCII (curly quotes, em dashes, and one
  emoji rewritten as its `\u2728` escape). Not cosmetic preference: the
  installed 7bit hook enforces whole-file ASCII, so the file could not be
  edited to add the Development section without it.
- Removed the leftover `build/` and `jeltz.egg-info/` directories left by
  T22's pip installs. Nothing creates them now.

#### T24. Full ruff rules in pyproject.toml + Makefile lint - DONE
Goal: the repo's own standard becomes the full ruff rule set; the
abbreviated set stays where it belongs (the ruff.sh consumer hook, which
keeps its narrow rules on purpose to avoid red/green/refactor thrash).

Delivered: `[tool.ruff]` in pyproject.toml, `ruff` in the dev dependency
group (locked at 0.16.3), a `make lint` that runs ruff beside
shellcheck/shfmt and provisions first, and the whole Python tree brought
into compliance. Contract pinned by 9 tests in
`tests/test_lint_standard.py`, plus the tripwire rewrite in
`tests/test_provisioning.py` and the two T22 tests this task deliberately
reverses (removed from `tests/test_packaging.py`, which now says where
their subject went).

Behavior as specified in the original acceptance:
- **The standard is declared, not incanted**: line-length 100,
  target-version py311, select E/W/F/I/B/C4/UP/ARG/SIM, ignore E501 and
  B008, `known-first-party = ["review"]`.
- **The whole tree complies** - `review/`, `tests/`, `conftest.py` - and
  `ruff format --check` reproduces the committed formatting. Both are
  asserted by running ruff over the tree, and both were confirmed to have
  teeth by planting a violating file and a misformatted one (each fails
  the matching test; probe removed).
- **`make lint` runs `ruff check` and `ruff format --check`** alongside the
  shell linters, from `$(VENV)/bin/ruff` rather than PATH, so the lockfile
  decides which ruff version judges this tree.
- **Lint is re-coupled to provisioning**: `lint: preflight-shell venv`
  restores the uv requirement transitively, exactly as T23 predicted. The
  preflight is unchanged - it gates only what uv cannot install.
- **This tree now resolves to strict** hook enforcement, asserted by
  committing the real pyproject.toml into a scratch repo and probing
  `hook_repo_mode`. That is the T1 ordering hazard, taken deliberately:
  the section that declares the standard is the section that tells the
  hooks to enforce it on whole files.
- **The shipped ruff.sh rule set is untouched**, guarded by a test that
  fails if either the abbreviated ignore list changes or a `--select`
  appears (both mutations verified to fail it).

Decisions recorded:
- **`src = ["."]`, not `["review", "tests"]` as this task originally
  specified.** Pointing src AT the packages tells ruff to look for
  first-party modules INSIDE them, which demotes `tests.conftest` to
  third-party: probed live, that spelling produces 14 I001 errors and
  would reorder import blocks wrongly. The repo root is where the
  first-party packages live, so that is what src names. The test pins the
  behavior (a `review` import sharing a block with pytest is flagged)
  rather than the literal value.
- **line-length 100 was honoured as specified**, at a cost worth naming:
  the tree was formatted at ruff's default 88, so adopting 100 reformatted
  30 files (~2,100 lines). The change is mechanical and mostly collapses
  artificially split assertions back onto one line, but it does mean this
  commit is the blame target for much of the test suite.
- **Ruff is handed the tree, not a file list.** `make lint` runs
  `ruff check .` for the same reason SH_SOURCES is a wildcard: a new
  Python file is covered without anyone remembering to list it. The tests
  invoke ruff the same way, so lint coverage cannot drift between them.
- **`.issubset()` rather than the SIM300 autofix.** Seven assertions read
  `REQUIRED <= actual.keys()`; ruff's fix flips them to
  `actual.keys() >= REQUIRED`, which reads backwards for a
  required-keys check. The explicit method is clean under the rule and
  says what it means.
- License: ruff is MIT, dev-only tooling, per CLAUDE.md.

Hardened after review (no blockers; both actionable non-blockers fixed):
- **The config contract is pinned where loosening it matters.** `src` is
  asserted exactly and the ignore list as an exact set, not a subset: a
  new ignore is a hole in the standard and would have slipped past the
  original subset check (the reviewer's example, adding F401, is now a
  test failure). Selections stay a lower bound on purpose - adding a rule
  family raises the standard and needs no permission from a test. Both
  assertions were mutation-checked: F401-in-ignore and the TODO's literal
  `src = ["review", "tests"]` each fail, and the second also fails the
  clean-tree test, which is the behavioral guard behind the literal.
- **The Makefile comment above the preflight targets no longer claims
  lint needs no provisioner.** It was true when T23 wrote it and this task
  falsified it; it now says lint's uv requirement is transitive through
  `venv`, which is the part that keeps the preflight gating only what uv
  cannot install.
- Recorded, not acted on: the vacuous shell-lint tests, still deferred to
  T38 with the reviewer's agreement. They are shell-lint coverage and
  nothing in T24's ruff coverage rests on them.

Real defects the new rules found (each fixed, not suppressed):
- `review/stop_hook.py` - the default `gate_when` lambda took a `payload`
  it never read (ARG005); renamed `_payload` so the signature says so.
- `review/verdict.py` - `Callable` imported from `typing` (UP035), which
  has been the deprecated spelling since 3.9; now `collections.abc`.
- `tests/test_installer.py`, `tests/test_makefile.py` - imports left
  behind by earlier refactors (F401).
- `tests/test_run.py` - a test requested the `capsys` fixture and never
  used it (ARG001), so it read as if it asserted on output.
- `tests/test_worktree.py` - nested `with` (SIM117), now one statement.

#### T39. Test git-fixture boilerplate is duplicated eight ways (found during T24)
Goal: one definition of "a scratch git repo with a pinned identity".
- Evidence (2026-08-18): `init` + three `git config` calls (user.name,
  user.email, commit.gpgsign) appear verbatim in `tests/conftest.py` and
  in seven test modules (`test_bridge`, `test_gate`, `test_agy_stop`,
  `test_claude_stop`, `test_codex_stop`, `test_grok_stop`,
  `test_lint_standard`). Every one exists for the same reason - the suite
  must not depend on the machine's global git config - so a change to
  that reasoning has eight places to reach.
- Extract a `scratch_repo(path)` helper (or fixture) into
  `tests/conftest.py` beside the existing `git()` and `dirty_repo`, and
  route the copies through it. Behavior must not change: the tests that
  need a dirty tree keep building one on top.
Not fixed inside T24: it predates this task (the boilerplate arrived with
T6) and touching seven unrelated modules to fix scaffolding is exactly the
scope creep the reviewer-response rules forbid.
Acceptance: one definition, seven call sites, `make verify` green.

#### T25. shfmt formatting contract via .editorconfig - DONE
Goal: `shfmt -d <sources>` reproduces committed formatting with no
Makefile-side flags to remember.

Delivered: a root `.editorconfig`, a `make lint` whose shfmt invocation is
`shfmt -d $(SH_SOURCES)` and nothing more, and 8 tests in
`tests/test_shell_formatting.py`.

Behavior as specified in the original acceptance:
- **The contract is declared**: `[*]` charset utf-8, end_of_line lf,
  insert_final_newline, trim_trailing_whitespace; `[*.sh]` indent_style
  space, indent_size 4, switch_case_indent true.
- **`root = true` ends the search at the repo boundary**, so how this
  tree's shell is formatted cannot depend on an .editorconfig above
  wherever it was cloned.
- **Plain `shfmt -d` over SH_SOURCES is clean and `make lint` passes.** No
  shell source needed reformatting: the declared keys reproduce `-i 4 -ci`
  exactly, so this changed where the contract lives, not what it says -
  which is the outcome the clean-sources test existed to establish rather
  than assume.
- **One source of truth, enforced**: a test asserts the shfmt line make
  runs carries `-d` and no other flag.

Decisions recorded:
- **Removing the flags IS the mechanism, not tidiness.** shfmt discards
  every EditorConfig formatting option the moment it is given any parser or
  printer flag (shfmt(1)), so leaving `-i 4 -ci` in place would have left
  the .editorconfig declaring a standard nothing enforced. Made executable
  rather than asserted in a comment: under the repo's own .editorconfig, a
  single `-i 0` turns the contract-formatted probe into a diff.
- **Markdown is exempt from the trim**, deviating from this task's literal
  "trimmed trailing whitespace for all files". Two trailing spaces are a
  Markdown hard line break, and the shipped
  `skills/security-audit/SKILL.md` carries 13 of them - a blanket `[*]`
  trim would reflow a consumer-facing artifact the first time anyone opened
  it in an editor. `[*.md] trim_trailing_whitespace = false` is the
  conventional carve-out, and it is pinned by its own test so the exemption
  stays a decision rather than an omission.
- **The source list is derived from make, not restated.** The tests pull
  the shfmt line out of `make -n lint` and run it verbatim, so a source
  that lint covers cannot go unchecked here - the T38 defect, avoided in a
  new place rather than reproduced.
- **The `[*]` keys are declared, not gated.** Charset, line ending, final
  newline, and trimming are editor directives; shfmt is the enforcement
  path this task scoped. The tree already complies (swept 2026-08-19: no
  CRLF, no file missing a final newline, and exactly one file with trailing
  whitespace - the exempt Markdown one above).

Teeth confirmed by mutation, not assumed:
- Re-indenting a block of `tools/preflight.sh` to two spaces fails the
  clean-sources test. That was the one test that passed in RED, because it
  runs whatever shfmt line make declares and the flags were still there.
- Deleting `switch_case_indent` from the .editorconfig makes the real tree
  diff (`install.sh`'s argument-parsing `case`), so the key is
  behaviorally load-bearing and not merely asserted by the config test.
- Both files restored and verified byte-identical afterwards.

Collateral, declared:
- The Makefile header said "shellcheck + shfmt clean shell" and the T1
  entry recorded `shfmt -i 4 -ci -d`; both described the retired invocation
  and now point at the .editorconfig contract. The lint comment block says
  why no formatting flag may come back. README gained a paragraph naming
  where the shell contract lives and why lint passes shfmt nothing but
  `-d`.

#### T26. pytest and coverage gates move into pyproject.toml
Goal: the 100% bar is declared configuration, not a Makefile incantation.
- `[tool.pytest.ini_options]`: testpaths, addopts carrying the coverage
  flags (`--cov=review --cov-report=term-missing --cov-fail-under=100`),
  so any bare `pytest` run enforces the same gate `make test` does.
- `[tool.coverage]` sections as needed (source, fail_under 100).
- 100% passing is pytest's exit code; the gate must fail the run on any
  failed, errored, or unexpectedly-skipped test.
- Slim the Makefile test target to invoking pytest; behavior identical.
Acceptance: `pytest` with no arguments and `make test` enforce the same
100% coverage and 100% pass bar; a deliberately missed line, a failing
test, and an unexpectedly-skipped test each fail both the same way.

#### T27. install.sh security hardening: no recursive force-delete
Goal: install.sh either acts safely or errors with a reason - it must
never `rm -rf`.
- Known dangers to remove (reviewed 2026-08-16): the
  `rm -rf "${root:?}/$name"` in install_skills_into (line 52), the
  `rm -rf "$repo/.agents/skills"` in project_install (line 124), and the
  check-then-act race between the `[ -e ] && [ ! -L ]` test (line 123)
  and that delete - the path can change between test and removal even in
  a user-controlled directory.
- Replace delete-then-copy with a safe strategy: only remove what jeltz
  provably installed (e.g. validate against the manifest before touching
  anything, remove files individually and directories with non-forced
  rmdir), and on anything unexpected - unmanifested files, a directory
  where a symlink should be, content that changed between inspection and
  action - stop and tell the user exactly what was found and how to
  resolve it manually.
- Do a full defensive pass over the script while there: quoting, set -e
  interactions, TOCTOU on every test-then-act pair, behavior on
  hostile/degenerate paths.
- Extend the pytest subprocess suite with the refusal cases (unexpected
  file in a skill dir, real directory at the symlink location, manifest
  mismatch) before rewriting - red first, per the loop.
Acceptance: no `rm -rf` (or equivalent forced recursive delete) remains
in install.sh; every refusal path is exercised by a test and produces an
actionable error message; install/reinstall/check flows still pass the
existing suite.

(T32-T37 were added 2026-08-17 after T20's acceptance review; they are
numbered after the Phase 6 tasks but must land before Phase 6 begins.)

#### T32. Collision-resistant temp and evidence files (multi-agent hygiene)
Goal: nothing the loop writes can collide when several agents run in the
same directory (2026-08-17 direction: assume multiple agents per
directory; unique names via UUID/session id, created atomically).
- `write_state` (review/run.py) composes its atomic write through a
  FIXED temp name (`state.json.tmp`): two concurrent writers race on the
  temp file even though the final `replace` is atomic. Switch to a
  unique per-writer temp (`tempfile.mkstemp` in the state directory) +
  `os.replace`, and handle crash leftovers safely (age- or pid-guarded
  cleanup, never "delete all *.tmp").
- The T20 skill instructs fixed example evidence names
  (`.jeltz/review/verify.txt`, `wip-message.txt`, and the response
  file). Make the instruction collision-resistant: create each file with
  `mktemp` under `.jeltz/review/` (unique and atomic) and pass the
  resulting paths to `--verify-output` / `--wip-message-file` /
  `--response-file`; update the skill text and the tests that pin it.
- Audit every other write for the same property. Already correct and the
  model to follow: worktree parents come from `mkdtemp` with a pid file,
  and `reap_stale_worktrees` reaps only when `_owner_alive` proves the
  owning process is gone (verified 2026-08-17) - a live concurrent
  agent's worktree survives.
- Scope boundary: this task is transient files only. The shared per-tree
  singletons (`state.json`, `escalation.md`) need a real concurrency
  protocol, which is T36 - do not half-solve it here.
Acceptance: no fixed-name temp path remains in shipped code or skill
text; a test exercises two interleaved `write_state` writers and the
surviving file is always one writer's complete, well-formed output
(never torn); the audit's findings are recorded in this entry.

#### T33. Installer ships the review engine (depends on T22)
Goal: a consumer repo gets a working `review/run.sh` from install.sh
alone - consumers do not operate out of the jeltz checkout, and today
install.sh ships skills only (verified 2026-08-17: no review/ or hooks/
handling at all).
- Project-scope install copies the review engine into the consumer repo:
  `review/` (the Python package, `run.sh`, `verdict.schema.json`,
  `tool-allowlists.json`), manifest-stamped like the skills so `--check`
  reports drift on engine files too. The bridge's recovery instruction
  and the T20 skill both hardcode the `review/run.sh` relative path, so
  the install location is fixed by contract.
- The installer MUST provision the consumer's production dependencies
  (2026-08-17 direction: "the install must install the review scripts
  and dependencies"; T22's split makes `jsonschema` production). Decide
  the mechanism in-task - e.g. uv/pip provisioning a consumer
  environment from jeltz's pyproject - but installing them is required;
  a preflight in run.sh that names a missing dependency actionably is
  defense-in-depth for later environment breakage, never a substitute
  for installation. Note: T23's uv move provisions jeltz DEVELOPMENT
  only - it does not cover consumers by itself (2026-08-17 question,
  answered).
- Verify run.sh's import assumptions hold when vendored: it execs
  `python3 -c 'from review.run import main'` relative to its own
  location, which must keep working from the consumer root.
- Respect T27's no-rm-rf rules; coordinate ordering with it.
Acceptance: starting from an environment where `jsonschema` is not
importable and a fresh consumer repo with no jeltz checkout on disk,
running install.sh alone yields a `review/run.sh --new` that completes a
round against the scripted backend using the environment the installer
provisioned; tampering with an engine file trips `--check`; breaking the
environment afterward produces the preflight's named, actionable error.

#### T34. Installer wires the stop gate per host (depends on T33)
Goal: the Problem B gate is actually installed, not just documented -
nothing installs the T16-T19 shims' hook configs today (T19 recorded
"wiring is T21", but T21 is documentation only).
- Project scope for the three hosts whose project hooks fire, at each
  host's 3.6-verified config location: Claude Code Stop-hook entry
  invoking `review/claude_stop.py`, agy `.agents/hooks.json` invoking
  `review/agy_stop.py`, grok a `.grok/hooks/` JSON file invoking
  `review/grok_stop.py` - with grok's one-time folder-trust requirement
  (`/hooks-trust` or `--trust`, 3.6) surfaced in the install output,
  since an untrusted project hook is silently skipped.
- Codex is user scope by contract, not project scope: repo-local hooks
  reportedly do not fire in interactive sessions (3.6,
  openai/codex#17532). The codex gate (`review/codex_stop.py`) installs
  into `~/.codex/hooks.json` (or user config.toml) as an explicit,
  consented step of the `--user` flow - never silently from a project
  install - with codex's one-time `/hooks` trust step surfaced. Record
  the Problem B implication: a codex developer is gated only after that
  user-scope step; the consumer repo cannot ship it, and CI (R4)
  remains the backstop there.
- Merge, never clobber: an existing consumer settings file gains the
  hook entry; anything unmergeable is a refusal with an actionable
  message (T27 philosophy).
- Print the per-clone opt-out pointer (`info/jeltz-review-gate`, T14) so
  R3's escape hatch is discoverable at install time.
Acceptance: after a project-scope install, the three project-scope
hosts' configs invoke the right shims, and after the user-scope step,
codex's does (all pinned by tests over the written files); pre-existing
consumer settings survive byte-for-byte outside the added entry; the
grok and codex trust steps appear in the respective install output;
`--check` covers the wiring files.

#### T35. Fix the recursive `make test` re-execution; profile the rest
Goal: suite wall time proportionate to its size; today ~220s for ~395
tests, and the dominant cost is already attributed (2026-08-17):
`test_make_test_passes_on_clean_checkout` takes 113s because it re-runs
the ENTIRE suite recursively inside `make test` (bounded to one level by
`JELTZ_MAKE_TEST_INNER`) - so venv re-provisioning/pip downloads are NOT
the main cause; the recursion is, and the T23 uv move alone will not fix
it.
- Fix the recursion: the self-referential `make test` check should
  prove wiring (make provisions, then invokes pytest with the coverage
  gate), not re-execute every test - e.g. bound the inner run to a
  cheap subset via a make/pytest variable while keeping the outer gates
  intact, or assert on `-n` dry-run output plus a minimal real run.
- Record the before/after wall-clock numbers in this entry, plus a full
  `pytest --durations=25` profile of the post-fix suite. That profile is
  the decomposition input: T37 takes only the single highest-cost
  attributed cause, and every further cause worth fixing gets its own
  numbered task filed here during T35 (one cause per task), so no
  open-ended optimization ever sits inside one task.
- Keep the gates intact: 100% coverage, 100% pass; no test deleted or
  weakened to win time. Long-tail optimization of other suites is
  explicitly out of scope here (T37).
Acceptance: the recursive full-suite re-execution is gone; before/after
numbers and the durations profile are recorded here; `make verify` still
green with the same gates.

#### T36. Concurrency-safe review state protocol (depends on T32)
Goal: two agents in the same directory each complete a full review
lifecycle without corrupting each other's records. Last-writer-wins is
NOT acceptable (2026-08-17 review): T32's unique staging files stop torn
JSON, but a whole-file overwrite still lets one agent clobber another's
`thread_id`, `round`, verdict, or denial marker - and a lock held only
for the duration of `write_state` has the same logical failure, just
narrower.
- Design first, then implement, and record the decision and rationale:
  either session-scoped records (state keyed by session/agent identity,
  the T14 gate consulting whichever record matches the tree hash) or
  serialization covering the complete review lifecycle (a lock spanning
  new -> rounds -> verdict, with crash/staleness recovery so an
  abandoned lock cannot wedge the directory - the pid-file pattern
  `reap_stale_worktrees` uses is the house precedent).
- The bridge's denial merge (`_record_denial` in review/bridge.py) is a
  read-modify-write over the same file and must live under the same
  protocol: today a concurrent denial can drop a just-written review
  record or vice versa.
- The T14 gate and T15 bridge readers must keep ruling correctly against
  the new state shape.
- The escalation dossier is under the same protocol - no exemption: it
  is the evidence a human tiebreak runs on, so losing one to an
  overwrite defeats the escalation path. Either session-scoped dossier
  paths or serialization proving a dossier cannot be replaced before its
  tiebreak is resolved; every literal `.jeltz/review/escalation.md`
  reference (the T15 recovery instruction, the T20 skill, run.py's
  messages) must track whatever naming lands.
Acceptance: a test interleaves two agents through independent
new/resume lifecycles in one directory and both finish with intact
per-agent histories (thread, round, verdict); a concurrent
denial-vs-state-write test loses neither record; two concurrent
escalations lose neither dossier; the gate's ruling for the tree is
still correct afterward; the design decision is recorded in this entry.

#### T37. Fix the single highest-cost profiled test (depends on T35)
Goal: one bounded fix for the one attributed cause T35's profile ranks
highest - nothing else. Further causes are separate tasks, filed during
T35's decomposition step; this task's scope is fixed the moment the
profile exists.
- Take the top entry of T35's durations profile, attribute its cost
  (expected suspects: subprocess-heavy adapter, worktree, or
  makefile-fixture setup), and apply one fix - e.g. share the expensive
  fixture where isolation permits, or batch redundant subprocess spawns.
  (In-test pip provisioning is already gone: T23 replaced it with a cached
  uv sync, which cut the provisioning tests from ~130s to ~3s.)
- Gates intact: 100% coverage, 100% pass; no test deleted or weakened.
Acceptance: the targeted cause's before/after numbers recorded here and
the top profile entry's cost materially reduced; `make verify` green.

#### T38. The lint-failure tests are vacuous (found during T23)
Goal: `make lint` is proven to catch broken shell, not merely proven to
exit non-zero.
- Evidence (2026-08-18): `tests/test_makefile.py`'s `lint_tree` fixture
  copies only the Makefile and `hooks/` into a scratch tree, but
  `SH_SOURCES` also names `install.sh` (and now `tools/*.sh`), so
  shellcheck dies with `install.sh: openBinaryFile: does not exist`
  before it ever reads the deliberately broken file. Both
  `test_make_lint_fails_on_shellcheck_violation` and
  `test_make_lint_fails_on_formatting_violation` therefore assert
  `returncode != 0` against a failure that has nothing to do with the
  violation they inject. Reproduced directly: the same tree lints
  non-zero with the injected file removed.
- Fix the fixture so the scratch tree carries every path `SH_SOURCES`
  resolves, then re-run both tests with the injected violation removed
  and confirm they now PASS (they must fail only because of the
  violation). A fixture that silently drops a source is the same defect
  in a new place, so derive what to copy rather than listing it.
- While there, check the sibling scratch-tree fixtures for the same
  class of vacuous pass.
Left for its own task rather than fixed inside T23: T23 changed what the
Makefile provisions, not what lint covers, and this predates it (the gap
opened when install.sh joined SH_SOURCES in T2).
Acceptance: with the injected violation removed each test fails, with it
present each passes for the stated reason; `make verify` green.

#### T21. Install and configuration documentation (moved from Phase 4)
- README section on the review loop and installing the gate, per host and
  scope.
- Config reference: backend, model, max rounds, size ceiling, opt-out.
- Record how to re-verify section 3 (section 8) and against which versions.

Moved here on 2026-08-17, before Phase 6 and after every task that changes
installation. It documents how to install and configure the system, and
that procedure does not exist yet in its final form: T33 is what makes the
engine installable at all, T34 is what installs the per-host gate wiring
(the second bullet's subject), T23 changes how dependencies are
provisioned, and T27 rewrites install.sh's copy/refusal behavior. Written
in Phase 4 it would have documented a procedure no code performs, then been
rewritten four times. It stays a Phase 5 task rather than folding into
T28-T30 because it is user-facing README/config documentation, not the
branch-history extraction those tasks perform - and Phase 6 begins by
assuming the shipped documentation is already correct.

### Phase 6 - branch closeout

TODO.md is the working spec for this feature branch only; the squash
commit will collapse the granular history, so the architectural and
implementation documentation recorded here MUST be preserved in durable
documents under docs/ (which does not exist yet) before the file is
removed. The extraction is split into bounded, conversation-sized
topics (T28-T30), each reorganized for a reader who never saw the
TODOs - by topic, not by task number, keeping task-numbered acceptance
evidence only where it documents a verified-against version. These run
late deliberately: content is only stable once every Phase 5 task lands
(T21-T27 and T32-T39).

#### T28. docs/: architecture and decision log
- Extract the problem statement (section 1), the decision log D1-D6
  with rationale and settlement history (section 2), and the target
  architecture (section 4): review loop, engine/gate split, state file
  and exit-code protocol, escalation policy.
Acceptance: a reader with this document alone understands why the
system exists, what shape it has, and why each locked decision went the
way it did.

#### T29. docs/: per-host findings and adapter contracts
- Extract the section 3 verified findings and sharp edges for all four
  hosts: envelope dialects, permission and approval models, skill
  discovery, the claude billing constraint (3.7), and the per-adapter
  argv contracts and hard-error taxonomies recorded in the T8-T11 DONE
  entries.
- Include the tool-allowlist rationale (why deny lists are
  documentation, why D5 rests on the integrity check) and the pinned
  host versions each finding was verified against.
Acceptance: a reader with this document alone can maintain or re-derive
any of the four adapters without spelunking branch history.

#### T30. docs/: re-verification procedure + completeness audit
- Extract the re-verification procedure (section 8's commands and
  section 3.1's re-probe guidance) with the host versions they were
  last run against.
- Then the closing audit: sweep the remaining TODO.md end to end for
  any architectural or implementation documentation not yet carried by
  docs/ or README, and move what the sweep finds. This audit is the
  gate T31 depends on - nothing durable may exist only in TODO.md after
  it.
Acceptance: docs/ + README alone (no TODO.md, no branch history) carry
the architecture, the per-host constraints, how to re-verify them, and
everything else the audit surfaced; the audit's result (including
"nothing further found") is recorded in the task's commit message.

#### T31. Remove TODO.md
Goal: the branch merges without its scaffolding.
- Final task of the branch, after the T30 audit is accepted: delete
  TODO.md and fix every reference that treats the ROOT planning
  document as jeltz's design source (the Makefile T1 comment, any
  docs/ or README mentions) so nothing points at a dead file.
- Explicitly out of scope: the shipped skills' references to TODO
  files (tdd-phase-loop, project-bootstrap, skeptical-reviewer). Those
  refer to a CONSUMER project's own task list - same filename,
  different file - and are intentional consumer-facing contract, not
  references to this document.
- The git commits remain the canonical fine-grained record until the
  squash; docs/ (T28-T30) carries everything meant to outlive it.
Acceptance: no tracked file references the root TODO.md as a design
source; the consumer-facing TODO references in skills/ are byte-for-byte
unchanged; `make verify` green.

---

## 7. Deferred

- **Reviewer model diversity policy.** Four vendors are now reachable -
  Anthropic, OpenAI, Google, xAI. Today's value comes from the reviewer being a
  different vendor than the coder. That deserves a documented rule rather than
  an ad-hoc per-project choice, especially since agy can route to Claude models
  and would silently collapse the diversity it was chosen for.
- **CI-side verification** that every commit carries a review record (R4). This
  is the only enforcement path that covers grok completely.
- **Antigravity user-scope install.** Deliberately dropped from T2's scope
  (2026-08-15 review): the `~/.gemini/config/` location in the 3.3 table is
  unverified vendor documentation, and verifying it requires mutating the
  real `~/.gemini` - agy's auth lives there, so a sandboxed `$HOME` probe
  cannot run. Project scope covers antigravity fully via the `.agents/skills`
  symlink and is the Problem B path. Pick this up only if a user-scope agy
  consumer actually appears; verify the location first with a probe skill.

---

## 8. Verification commands

Re-run when any host updates.

```sh
# codex MCP tool surface
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | codex mcp-server

# whether a codex config key actually reaches the prompt
codex debug prompt-input -c 'instructions="ZZMARKERZZ"' | grep -c ZZMARKERZZ

# whether a codex config key is even recognized
codex exec --strict-config -c 'base_instructions="x"' --sandbox read-only "x"

# whether codex expands $skill-name before the model sees it
codex debug prompt-input '$skeptical-reviewer HEAD'

# antigravity: project skill discovery (compare with and without --new-project)
agy -p "List the names of every skill available to you. Do not call any tools." \
    --output-format json --new-project

# antigravity customization reference (bundled with the CLI)
ls ~/.gemini/antigravity-cli/builtin/skills/agy-customizations/docs/

# grok: what it discovers for this directory, including [claude] skills
grok inspect

# grok: headless contract and cost reporting
grok -p "Reply with exactly the word PONG." --output-format json

# grok: hooks and Claude Code compatibility reference (bundled)
grep -n -A20 '^## Hooks' ~/.grok/README.md
grep -n -A20 '^## Claude Code Compatibility' ~/.grok/README.md
```
