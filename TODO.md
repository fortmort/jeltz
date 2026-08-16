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

Status: T1-T8 complete; next task is T9.

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
  guess did NOT take (schema still unresolved, T9). Workaround that
  produced a full review: instruct file-tool-only review. Also: agy omits
  empty JSON keys unless told not to.
- **grok:** with partial `--allow` rules, the first tool call outside the
  allowlist silently ends the run - `stopReason: "cancelled"`, exit 0,
  narration-only text, no stderr (an R1 silent failure; T10 must assert on
  verdict presence, never on exit status). Rules use `Bash(...)/Read(...)`
  prefixes; `--allow Read --allow "Bash(git*)"` was not sufficient for a
  full review; `--always-approve --deny Edit --deny Write` completed in 6
  turns at $0.12. Flag placement: `--max-turns` before `-p`.

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
