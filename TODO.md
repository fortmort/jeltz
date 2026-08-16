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

Status: T1-T12 complete; next task is T13.

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

#### T1. Repo tooling for shipped artifacts - DONE
Goal: make the shipped scripts testable without pretending `jeltz` is a
consumer project.

Delivered: root `Makefile` with `lint` (shellcheck `-x -P hooks` + `shfmt -i 4
-ci -d`, wildcarded over `hooks/*.sh` and `hooks/lib/*.sh` so new scripts are
covered automatically), `test` (pytest from a self-bootstrapping stdlib
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
  MIT/permissive. Dependencies live in a tracked `requirements-dev.txt`
  and `make test` depends on it through a venv stamp (added after
  review), so editing the list reinstalls into an existing venv instead
  of leaving checkouts with T1's pytest-only venv failing at import.
- Root `conftest.py` puts the repo root on `sys.path` so shipped Python
  modules import without packaging metadata (which T1 deliberately avoids).

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
17 tests (22 instances) in `tests/test_run.py`, driven in-process plus one
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
- Record how to re-verify section 3 (section 8) and against which versions.

### Phase 5 - packaging, tooling, and hardening

#### T22. Packaging baseline: pyproject.toml with split dependencies
Goal: one declarative packaging file; requirements-dev.txt retired.
- Add `pyproject.toml` with project metadata and dependencies split by
  audience: production dependencies for running the shipped code
  (`jsonschema` - `review/verdict.py` imports it at runtime in consumer
  contexts, so it is NOT a dev dependency despite living in
  requirements-dev.txt today) and a dev group for developing jeltz
  itself (`pytest`, `pytest-cov`; `ruff` joins this group in T24, the
  task that lands its config).
- Point the Makefile's venv provisioning at pyproject.toml (still pip in
  this task; the uv swap is T23) and delete requirements-dev.txt,
  including the stamp dependency comment logic that references it.
- Deliberately NO `[tool.ruff]` section in this task:
  `hooks/lib/repo-mode.sh` derives strict mode from a git-TRACKED ruff
  config, so the flip must land together with full-rules compliance
  (T24), not as a packaging side effect (the exact hazard the T1 note in
  the Makefile records).
Acceptance: a fresh checkout provisions and passes `make verify` from
pyproject.toml alone; requirements-dev.txt is gone; repo-mode detection
still resolves jeltz to non-strict.

#### T23. Move provisioning from pip to uv
Goal: uv is the single installer for dev and CI use.
- Makefile provisions with uv (venv creation and dependency sync from
  pyproject.toml); commit the lockfile so installs are reproducible.
- Keep the standard targets (`make lint/test/verify`) working unchanged
  for callers; only the provisioning underneath changes.
- Account for every tool the Makefile invokes: Python tooling (pytest,
  pytest-cov, later ruff) is uv-provisioned from pyproject.toml;
  `shellcheck` and `shfmt` are external Go/Haskell binaries no Python
  package manager can provide, so they stay documented prerequisites -
  and `make verify` must fail fast with a clear message naming any
  missing one instead of a bare command-not-found.
- Update README/bootstrap instructions; document the uv version floor
  and the external prerequisites in one place.
- License check for any new tooling per CLAUDE.md (uv itself is
  MIT/Apache-2.0, install-time only, not a code dependency).
Acceptance: a fresh checkout with uv plus the documented external
binaries (shellcheck, shfmt) reaches a green `make verify`; every
Python-ecosystem tool arrives via uv; no Makefile path invokes pip; a
missing external prerequisite produces a named, actionable error.

#### T24. Full ruff rules in pyproject.toml + Makefile lint
Goal: the repo's own standard becomes the full ruff rule set; the
abbreviated set stays where it belongs (the ruff.sh consumer hook, which
keeps its narrow rules on purpose to avoid red/green/refactor thrash).
- Add `ruff` to the pyproject dev dependency group (declared here, not
  in T22, so the tool and its config land together and are provisioned
  by uv like the rest of the Python tooling).
- Add to pyproject.toml (jeltz-specific values filled in):
  `[tool.ruff]` line-length 100, target-version py311, src = review and
  tests; `[tool.ruff.lint]` select E, W, F, I, B, C4, UP, ARG, SIM;
  ignore E501 (formatter's job) and B008; `[tool.ruff.lint.isort]`
  known-first-party = review.
- Bring the whole Python tree (review/, tests/, conftest.py) into
  compliance with that full set in the same task: committing the tracked
  `[tool.ruff]` section flips repo-mode.sh to strict for this tree the
  moment it lands, so config and cleanup are one atomic change (the T1
  ordering hazard, now on purpose).
- `make lint` must run the full ruff (`ruff check` and
  `ruff format --check`) alongside the existing shellcheck/shfmt; update
  the Makefile header comment that currently documents the deliberate
  absence of a tracked ruff config.
- Do NOT touch the abbreviated rule list inside the shipped ruff.sh
  hook - that is consumer-facing phase tooling, not the repo standard.
Acceptance: `make verify` green with the full rule set enforced;
repo-mode.sh now resolves jeltz to strict and the diff-aware hooks still
behave (T1's tests keep passing); ruff.sh's shipped rule set unchanged.

#### T25. shfmt formatting contract via .editorconfig
Goal: `shfmt -d <sources>` reproduces committed formatting with no
Makefile-side flags to remember.
- Add a root .editorconfig: charset utf-8, lf, final newline, trimmed
  trailing whitespace for all files; for `*.sh`: indent_style space,
  indent_size 4, switch_case_indent true (shfmt reads these plus its own
  extension keys).
- Simplify the Makefile shfmt invocation to rely on .editorconfig
  instead of inline `-i 4 -ci` flags; reformat any shell source the new
  contract diffs.
Acceptance: plain `shfmt -d` over SH_SOURCES is clean; `make lint`
passes; the .editorconfig and Makefile agree on one formatting source of
truth.

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

### Phase 6 - branch closeout

TODO.md is the working spec for this feature branch only; the squash
commit will collapse the granular history, so the architectural and
implementation documentation recorded here MUST be preserved in durable
documents under docs/ (which does not exist yet) before the file is
removed. The extraction is split into bounded, conversation-sized
topics (T28-T30), each reorganized for a reader who never saw the
TODOs - by topic, not by task number, keeping task-numbered acceptance
evidence only where it documents a verified-against version. These run
late deliberately: content is only stable once T12-T27 land.

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
