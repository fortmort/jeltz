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
grok. All four can act as the reviewer. Only three can enforce the gate.

Note on location: `skills/tdd-phase-loop/SKILL.md` reads `@TODO.md` at the
consumer repo root. This file is `docs/TODO.md` by request. See T21.

Status: analysis complete, no implementation started.

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
| D2 | Load the skill via `base-instructions` on the codex MCP tool. | **Revisit - see 2.1** |
| D3 | The Stop hook gate is in scope and is the primary deliverable for Problem B. | Locked |
| D4 | The reviewer backend is pluggable. Codex is the default; no backend's billing model is assumed permanent. | Locked |
| D5 | The reviewer **must not author code**. Cache and artifact writes (pytest, ruff, mypy, coverage) are expected and permitted; modifications to tracked source are not. | Locked, restated |

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

---

## 3. Verified findings

Verified 2026-08-14 against codex-cli **0.147.0**, Claude Code **2.1.232**,
antigravity `agy` **1.1.13**, and grok **1.0.4**. Version-pinned; re-verify
before relying on them (section 9).

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
`status: "SUCCESS"` with `response: ""`, with only a stderr note. An adapter
that trusts `status` will record an empty review as a pass. The correct fix is
an allow-rule under `permissions.allow`, not `--dangerously-skip-permissions`;
the exact schema and file location are unresolved (T9).

**Sharp edge 3 - workspace trust.** `~/.gemini/antigravity-cli/settings.json`
carries a `trustedWorkspaces` list. Consumer projects must be trusted.

### 3.5 Grok has the best reviewer interface and no way to gate a stop

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

**The disqualifying gap: no Stop hook.** Grok's documented hook events are
pre/post-tool-use and session start/end, configured in `.grok/hooks/` or as
`[[hooks.<Event>]]` in a config layer, behind a project trust prompt. There is
no stop-blocking event. A grok session cannot be prevented from ending.

The workaround is to gate *entry* rather than *exit*: a `PreToolUse` hook
matching the edit tools (`search_replace`, `bash`) that denies edits while an
unreviewed state exists. That is a different enforcement shape - deny-at-edit
rather than deny-at-stop - and needs its own design (T19).

**Operational caveat.** Interface quality and operational suitability are
separate axes. The interface is the best of the four; the free plan's rate
limits and availability are not guaranteed. That makes grok a strong fallback
and a weak default primary, on interface grounds alone.

### 3.6 Stop-blocking support is not universal

| Host | Mechanism |
|---|---|
| Claude Code | `Stop` hook returns `{"hookSpecificOutput": {"hookEventName": "Stop", "decision": "deny", "reason": "..."}}` or exits 2. Receives `stop_hook_active`. Default timeout 600s. |
| Codex | Hooks system (`config.toml` or `hooks.json`) with `Stop`, `PreToolUse`, `SessionStart`, plus a trust model (`/hooks`, `--dangerously-bypass-hook-trust`). |
| Antigravity | `.agents/hooks.json` `Stop` handler returns `{"decision": "continue", "reason": "..."}`. Also `PostInvocation` with `terminationBehavior: "force_continue"`, and `PreToolUse` with `deny`. Default timeout 30s. |
| Grok | **None.** PreToolUse / PostToolUse / session start / end only. See 3.5. |

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

Four hosts, but only **three packaging targets**, because grok consumes the
Claude Code layout natively (3.5):

| Target | Project scope (checked into consumer repo) | User scope | Serves |
|---|---|---|---|
| Claude Code layout | `.claude/skills/`, `.claude/settings.json`, or a plugin with `hooks/hooks.json` | `~/.claude/` | Claude Code, grok |
| Codex layout | unverified - T2 spike | `$CODEX_HOME/skills/`, `config.toml` | Codex |
| Antigravity layout | `.agents/skills/`, `.agents/hooks.json`, `.agents/skills.json` | `~/.gemini/config/` | Antigravity |

Packaging the Claude Code target as a **plugin** is worth evaluating in T2:
grok discovers plugin skills, agents, hooks, and MCP servers as a unit, so one
plugin directory could cover two hosts with a single install step.

Project scope is strongly preferred for Problem B: a developer who clones the
repo gets the gate without installing anything, and the gate is versioned with
the code it guards.

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
- **R4. Gate bypass.** Anyone can delete a hook, and grok cannot be gated at
  stop at all (3.5). This raises the floor for honest mistakes; it is not an
  adversarial control. CI remains the real backstop and should eventually
  verify a review record per commit - which is also the only gate that covers
  grok users completely.
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

#### T1. Repo tooling for shipped artifacts
Goal: make the shipped scripts testable without pretending `jeltz` is a
consumer project.
- `Makefile` with `lint` (shellcheck + shfmt, already used by hand per
  `.claude/settings.local.json`) and `test`.
- Decide the test runner for shell: pytest driving subprocesses, or `bats`
  (MIT, not currently installed). Whichever is chosen ships with the repo, not
  with the artifacts.
- Caution: adding a tracked `[tool.ruff]` section flips
  `hooks/lib/repo-mode.sh` from `diff` to `strict` for this tree. Only add one
  deliberately.
- Coverage: `CLAUDE.md`'s 100% rule is a consumer norm. Record what standard
  `jeltz` holds itself to; do not silently inherit a rule written for Python
  application code.
Acceptance: `make lint` and `make test` pass on a clean checkout.

#### T2. Multi-host, multi-scope installer
Goal: one source of truth, installed correctly on any host and scope.
- Implement the 4.3 matrix. Project scope is the default.
- Evaluate packaging the Claude Code target as a plugin so one artifact covers
  Claude Code and grok, including `hooks/hooks.json`.
- Spike: does codex discover project-scoped skills, or is `$CODEX_HOME` the
  only location? `codex debug prompt-input` from inside a project with a
  candidate directory answers this in one command.
- Generate `.agents/skills.json` for antigravity so a consumer's skills can
  live in one shared directory rather than duplicated per host.
- Stamp an installed version so consumers can detect drift from what this repo
  ships.
- `make check-install` reports drift.
Acceptance: a fresh consumer repo gets working skills on all four hosts from
one command; `check-install` detects a hand-edited installed copy.

### Phase 1 - the skill contract

#### T3. Revise `skeptical-reviewer/SKILL.md`
Goal: keep what works, add what automation needs.
- Keep the committed-changes review target - it matches actual practice. Accept
  a ref argument, defaulting to `HEAD`.
- Add an uncommitted mode for the enforcement path, where no WIP commit exists.
  (In the orchestrated path the worktree makes the WIP commit, so the reviewer
  still sees a commit.)
- Handle Q1: define behavior when no TODO item is supplied - review against
  `CLAUDE.md` norms alone.
- Replace `@CLAUDE.md` / `@TODO.md` with plain relative paths plus an explicit
  "read these first" step. The `@` prefix is Claude-specific expansion; codex,
  agy, and grok receive it as a literal string.
- State the D5 constraint in prose: read, run, and analyze freely; never author
  or modify code. Mechanical enforcement is T6 and R6.
- Add a re-review mode: given prior blockers by id, judge each resolved,
  unresolved, or regressed, and do not open unrelated new lines of attack
  unless they are blockers.
- Append a machine-readable verdict block: `{schema_version, verdict, round,
  blockers: [{id, file, line, claim, why}], non_blockers: [...]}`, with `id`
  stable across rounds.
Acceptance: the same file produces a usable review on all four hosts, and the
JSON block validates against T4.

#### T4. Verdict schema and parser
- JSON Schema for the verdict block, reusable as `--json-schema` (agy, grok)
  and `--output-schema` (codex exec) input.
- Parser: extract the fenced block, validate, raise typed errors.
- One repair round on malformed output, then escalate. Never infer from prose.
- Treat empty output as a hard failure (R1).
Acceptance: valid, malformed, missing, multiple-block, and empty inputs each
behave as specified. Golden reviewer outputs as fixtures.

#### T5. Update `reviewer-response/SKILL.md`
- Consume findings by id; emit per-id dispositions
  (fixed / rejected-invalid / deferred-non-blocker) with reasons.
- Add the deadlock rule from termination condition 3.
- Forbid silent scope expansion, which is what makes round counts explode.
Acceptance: output is diffable against the next round's verdict by id.

### Phase 2 - the review engine

#### T6. Packet builder and disposable review worktree
- Packet: TODO ref (optional per Q1), WIP message, diff, untracked list,
  `make verify` tail. Enforce the size ceiling.
- Worktree: materialize the reviewed state including uncommitted and untracked
  changes, commit the WIP inside it, guarantee cleanup on crash. Evaluate
  grok's built-in `--worktree` / `--worktree-ref` as a shortcut for that host.
- Integrity check (R7): snapshot the review checkout's tracked content and
  untracked file set before handing it to the reviewer, and verify after.
  Tracked-source changes **fail the review** - they do not merely warn, because
  a verdict produced against a mutated checkout is a verdict about different
  code. Verification artifacts and caches are allowlisted by pattern, not
  ignored wholesale, so a novel write shows up rather than slipping through.
- The check is host-neutral and runs in the orchestrator, so it holds for any
  adapter, including future ones with tool surfaces nobody has audited.
Acceptance: deterministic packet and stable diff hash for a given tree state;
reviewer can run `make verify` to completion without tripping the check; a
reviewer command that edits a tracked file in the review checkout fails the
review with a specific error; no tracked source file in the developer's tree is
modified (cache writes ignored, per D5).

#### T7. Adapter interface
Goal: pin the contract before writing four of them.
- `review(packet, mode=new|resume, thread_id) -> (verdict, thread_id, raw)`.
- Mandatory positive-output assertion (R1) and typed transport errors.
- Per-host tool allowlist as shipped config (R6). Treat this as defence in
  depth, not enforcement; T6's integrity check is what actually holds (R7).
- A failed integrity check is a distinct typed error, not a verdict. The
  orchestrator must never be able to record it as an accepted review.
Acceptance: a fake adapter exercises every orchestrator path without a network
call, including one that edits a tracked file and one that returns empty
output.

#### T8. Codex adapter (plus the D2 A/B)
- Default path: `codex exec` against the installed skill with the prompt form
  used today (`$skeptical-reviewer <ref> vs. <todo ref>`), `--output-schema`,
  `-o`, `--json`; `codex exec resume <id>` for re-review (D1).
- Alternate path behind a config switch: MCP over stdio with
  `base-instructions` and `codex-reply`.
- Compare on review quality, token cost, and verdict parse reliability. Record
  the result and settle D2 in this document. Grok's
  `--system-prompt-override` offers a cheap second data point for the same
  question.
Acceptance: fresh review and threaded re-review both work against a fixture
repo; a killed backend surfaces as a typed error, not a hang.

#### T9. Antigravity adapter
- `agy -p --output-format json --json-schema <schema> --model gemini-3.1-pro-*`,
  with explicit project context (3.4 sharp edge 1).
- Empty `response` is a hard error (3.4 sharp edge 2).
- Resolve the `permissions.allow` schema and file location so the reviewer gets
  read and command permissions without `--dangerously-skip-permissions`.
- `--conversation <id>` for re-review (D1).
- Document the workspace trust requirement for consumer repos.
Acceptance: a review runs end to end with an allowlist rather than blanket
approval; a permission denial fails loudly.

#### T10. Grok adapter
- `grok -p --output-format json --json-schema <schema>`, `--resume <id>` for
  re-review (D1).
- `--tools` allowlist excluding `search_replace`. This narrows the path to an
  edit; it does not close it, because `bash` stays enabled for `make verify`.
  D5 is enforced by T6's integrity check (R7).
- No install step needed: grok reads `.claude/skills/` and `~/.claude/skills/`
  natively (3.5). Assert this in the adapter's preflight rather than assuming.
- Record `total_cost_usd` and `usage` into review state - grok reports both,
  which makes it the best host for calibrating the round budget.
Acceptance: fresh review and resumed re-review both work; a shell-issued edit
in the review checkout is caught by the integrity check rather than by the
allowlist.

#### T11. Claude adapter (fallback)
- `claude -p "/skeptical-reviewer ..." --session-id $(uuidgen)
  --output-format json --json-schema <schema> --tools "Read,Grep,Glob,Bash"`.
  Omitting Edit and Write keeps consumer PostToolUse hooks from firing inside
  the reviewer, but `Bash` can still write - D5 is enforced by T6's integrity
  check (R7), not by this list.
- Preflight per 3.7: refuse when `ANTHROPIC_API_KEY` is set unless
  `--allow-api-billing` is passed. Never `--bare`.
- Round 1 opens a new session with a generated `--session-id`; later rounds use
  `--resume <id>` on that same session (D1).
Acceptance: preflight refuses by default with a clear message; each new review
loop starts on a session id that did not previously exist, and re-reviews
within a loop reuse it.

#### T12. Orchestrator: one round
- `review/run.sh --new | --resume`: packet, worktree, dispatch, parse, state
  write. Exit codes 0 / 10 / 20.
- Human-readable review to stdout, machine state to `.jeltz/review/state.json`.
Acceptance: exit code and state file agree with the verdict in every fixture
case.

#### T13. Escalation policy engine
- All four termination conditions from 4.2.
- Escalation dossier: disputed blockers, both sides' positions, round history.
Acceptance: each condition is independently triggerable; the dossier names the
specific disagreement.

### Phase 3 - enforcement

#### T14. Gate logic (host-neutral)
Goal: one implementation, thin host shims.
- Read `.jeltz/review/state.json`; compare its diff hash against the tree.
- Decide allow / block, with a reason string.
- Fast enough for a 30s hook budget (R5) - state file only, never a review.
- Scope exclusions per R3: docs-only, no tracked changes, opt-out marker
  alongside the existing `claude-hook-mode` convention.
Acceptance: blocks unreviewed source changes, allows doc-only edits, cannot
deadlock a session.

#### T15. Stop-gate recovery bridge
Goal: make a denied stop actually produce a review. Without this, T14 blocks a
noncompliant session and leaves it nowhere to go - and that session is the
entire Problem B audience.
- The gate's `reason` string is the only channel back into the coding session,
  so it must carry a deterministic instruction, not a complaint: the literal
  command to run (`review/run.sh --new`), what to do on exit 10 (invoke
  `reviewer-response`, then `review/run.sh --resume`), and what to do on exit
  20 (stop and surface the dossier to a human).
- Host-neutral prose plus a literal command. Do not assume `tdd-phase-loop` is
  installed - that is T20's path, and the developers this exists for are
  precisely the ones not running it.
- Recursion and repeat-denial guard: record in `.jeltz/review/state.json` that
  a denial was issued for a given diff hash, and never deny twice for the same
  hash. On Claude Code also respect `stop_hook_active`. A session that ignores
  the instruction must be able to stop on the second attempt with a logged
  warning rather than being trapped.
- Decide the posture when the instruction is ignored outright. A hook cannot
  escalate further; the gate has raised the floor and recorded the skip, and CI
  (R4) is the backstop.
Acceptance: an end-to-end fixture in which a session with no knowledge of
`tdd-phase-loop` edits tracked source, attempts to stop, is denied once,
follows the instruction, and reaches acceptance or escalation - with no
deadlock, no second denial for the same hash, and no unbounded loop.

#### T16. Claude Code Stop hook shim
- Translate T14's decision to `decision: "deny"` / exit 2, carrying T15's
  instruction as the reason; respect `stop_hook_active`; never block twice for
  the same state.

#### T17. Codex Stop hook shim
- Same decision and instruction, codex hooks schema; document installation
  under the trust model without `--dangerously-bypass-hook-trust`.

#### T18. Antigravity Stop hook shim
- Same decision and instruction, `{"decision": "continue", "reason": ...}` in
  `.agents/hooks.json`; keep well inside the 30s default timeout.

#### T19. Grok deny-at-edit gate
Goal: the only enforcement shape available on grok (3.5).
- `PreToolUse` hook matching `search_replace` and `bash`, denying edits while
  the tree is in an unreviewed state.
- Decide the entry condition: gating every edit is too aggressive for a normal
  TDD cycle, so it likely keys on "changes exist that were never reviewed AND
  the session is past some threshold" rather than on the first edit.
- Document that this is weaker than a stop gate and that CI (R4) is the real
  backstop for grok users.
Acceptance: a grok session cannot silently accumulate unreviewed changes past
the configured threshold.

#### T20. Wire `tdd-phase-loop` to the loop
- PHASE 3's terminal stop becomes PHASE 4 (REVIEW): run `--new`; on exit 10
  invoke `reviewer-response` in-session (keeping the coder's context), then
  `--resume`; the human approval gate moves to after convergence.
Acceptance: a full task completes RED through REVIEW with no terminal switching
and no copy-paste.

### Phase 4 - documentation

#### T21. Install and configuration documentation
- README section on the review loop and installing the gate, per host and
  scope.
- Config reference: backend, model, max rounds, size ceiling, opt-out.
- Resolve the `TODO.md` path split noted at the top of this file, plus Q2 and
  Q3.
- Record how to re-verify section 3 (section 9) and against which versions.

---

## 7. Deferred

- **Reviewer model diversity policy.** Four vendors are now reachable -
  Anthropic, OpenAI, Google, xAI. Today's value comes from the reviewer being a
  different vendor than the coder. That deserves a documented rule rather than
  an ad-hoc per-project choice, especially since agy can route to Claude models
  and would silently collapse the diversity it was chosen for.
- **CI-side verification** that every commit carries a review record (R4). This
  is the only enforcement path that covers grok completely.

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
