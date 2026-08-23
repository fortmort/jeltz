---
name: tdd-phase-loop
description: Autonomous red/green/refactor/review TDD with selective test running, explicit stop points, editor & hook interaction rules, automatic TODO updates, an in-session skeptical review loop, and a single final commit message.
---

Read these files from the repo root (plain relative paths) before starting:
- Your coding norms are defined in `CLAUDE.md`.
- The project specification is in `docs/PROJECT_SPECIFICATION.md`.
- Your task list is in `TODO.md`.

You must follow a **strict, phase-gated TDD workflow** for this project.
You are not allowed to skip phases or merge them.

---

## Global Rules

1. Always use red/green/refactor TDD.
2. Never write implementation code before tests.
3. Never move to a new TODO item until I explicitly approve.
4. You may automatically proceed from RED -> GREEN -> REFACTOR -> REVIEW
   without waiting for approval.
5. Only the final STOP requires human review. It comes after the review
   loop converges: the reviewer accepts the work (or escalates) before a
   human ever looks at it.
6. I will commit manually; you must not commit or proceed on your own.
7. Use **selective test running** to reduce cycle time:
   - RED: run only the newly written tests
   - GREEN: run only the newly written tests
   - REFACTOR: run the full test suite via `make verify`

---

## Editor & Hook Interaction Rules (Critical for RED Phase)

To avoid thrashing with ruff, pre-commit hooks, and unused-import
stripping, you must follow these rules:

### 1. All new tests must be added in a single edit
Write the **entire test function** in one edit, including:
- imports
- fixtures
- mocks
- assertions
- test body

Do **not** split test creation across multiple edits.

### 2. RED-phase tests may reference missing symbols
It is acceptable for tests to reference:
- missing functions
- missing classes
- missing modules
- missing imports
- ImportErrors
- NameErrors

These are **expected** in RED.
Do **not** attempt to fix missing symbols during RED.

### 3. Do not attempt to outsmart the linter
Do **not**:
- sequence imports and tests across multiple edits
- pre-import symbols
- avoid ImportError
- make RED tests pass linting

RED tests may fail import, typecheck, and runtime.

### 4. Ruff will not strip imports used in the same edit
Therefore:
- always write imports + test code in the same edit
- never write imports alone
- never write test bodies alone

### 5. Never attempt to satisfy the linter in RED
The linter may fail in RED.
The only requirement is that the **test file is syntactically valid
Python**.

### 6. GREEN, REFACTOR, and REVIEW may use multi-edit sequences
Only RED requires the "single edit" rule.

---

## PHASE 1 - RED (write failing tests only)

- Read the next task from `TODO.md`.
- Write **ONLY the failing test(s)** required for this task.
- Follow the **Editor & Hook Interaction Rules** above.
- Run **only the newly written tests**, confirming they fail for the
  correct reason.
- When tests are written and confirmed failing, STOP and output exactly:

`RED PHASE COMPLETE -- proceeding to GREEN.`

Also include a short GitHub-style WIP message summarizing:
- What you tested
- Why these tests are needed now

After outputting this message, automatically begin PHASE 2.

---

## PHASE 2 - GREEN (write minimum implementation)

- Implement **ONLY the minimum code** required to make the new tests
  pass.
- **Do NOT** refactor or clean up beyond what is strictly necessary.
- Run **only the newly written tests**, confirming they now pass.
- When the new tests pass, STOP and output exactly:

`GREEN PHASE COMPLETE -- proceeding to REFACTOR.`

Also include a short GitHub-style WIP message summarizing:
- What code you added or changed
- Which tests are now passing because of it

After outputting this message, automatically begin PHASE 3.

---

## PHASE 3 - REFACTOR (improve design without changing behavior)

- Refactor the code and tests to improve clarity, structure, and
  maintainability.
- **Do NOT** change externally observable behavior.
- Keep tests passing at all times.
- Run the **full test suite** using `make verify` to ensure no
  regressions.

Before ending the phase, you must:

### Update `TODO.md`
- Mark the current task as complete.
- Remove or modify the relevant TODO entry as appropriate.
- Ensure the TODO list reflects the new project state so the session can
  be safely restarted.

### Generate a single final commit message
Produce **one** GitHub-style commit message summarizing the entire task,
including:
- What the feature or fix *does*
- The tests added
- The implementation added
- The refactoring performed
- The final behavior of the code

This commit message must describe **what the code accomplishes**, not
just what changed.

### Then STOP and output exactly:

`REFACTOR PHASE COMPLETE -- proceeding to REVIEW.`

Also include a short GitHub-style WIP message summarizing:
- What you refactored
- **What the code now *does***
- Why the design is better now

After outputting this message, automatically begin PHASE 4.

---

## PHASE 4 - REVIEW (run the skeptical review loop in this session)

The task is not finished until an independent skeptical review accepts
it. You run that review yourself, from inside this same session - never
by asking the human to run it, and never by relaying findings through
another terminal.

- Gather the review evidence first: save the full `make verify` output
  from PHASE 3 and the final commit message to files under
  `.jeltz/review/`. That directory is excluded from the review's diff
  and untracked scan, so the verify, message, and response files never
  dirty the tree under review; any other in-repo location would.

  Create each evidence file with `mktemp`, which reserves a name no
  other session can hold:

  `mkdir -p .jeltz/review`

  `verify_file=$(mktemp .jeltz/review/verify.XXXXXXXX)`

  `message_file=$(mktemp .jeltz/review/wip-message.XXXXXXXX)`

  Several agents may be working in this directory at once. Under a
  fixed name one session's verify output or commit message is handed to
  another session's reviewer with nothing to signal the swap, and the
  verdict then judges work that was never under review. The commit
  message must travel by file for a second reason: it is arbitrary
  text, and its backticks, `$(...)`, and quotes would be expanded or
  mangled by the shell if pasted into a command line.

- Start the review by running:

  `review/run.sh --new --todo-ref "<todo-ref>" --wip-message-file "$message_file" --verify-output "$verify_file"`

  in the same shell, so each flag receives the path `mktemp` reserved
  above: `<todo-ref>` names the TODO item you just completed,
  `$message_file` holds the final commit message from PHASE 3
  byte-for-byte, and `$verify_file` is the saved `make verify` output.
  The orchestrator builds the review packet from exactly these values
  plus the diff - omitting them would hand the reviewer a placeholder
  message and no task to judge the implementation against. It reviews
  the packet in a disposable worktree and reports the verdict as its
  exit code.

- **On exit 0 (accepted):** the loop has converged. Proceed to the final
  STOP below.

- **On exit 10 (changes required):** apply the `reviewer-response` skill
  to the verdict's findings in this same session, keeping your context
  for the task - classify every finding id, fix valid blockers
  test-first, and reject invalid findings with reasons. Then, because
  the fixes changed the tree: re-run `make verify` and save its output
  over the verify file, and update the final commit message so it still
  describes the entire task including the review-driven fixes. Save the
  complete reviewer-response output - including the fenced JSON
  `dispositions` block - to a response file under `.jeltz/review/`,
  reserved with `mktemp` like the others; the orchestrator validates
  that file before dispatch and refuses one without the block.

  `response_file=$(mktemp .jeltz/review/response.XXXXXXXX)`

  Then resume the same reviewer thread:

  `review/run.sh --resume --response-file "$response_file" --wip-message-file "$message_file" --verify-output "$verify_file"`

  where `$message_file` now holds the updated commit message - only the
  TODO ref persists in review state, so the message and the fresh verify
  evidence must be passed again on every resume.

- **Repeat until exit 0 (accepted) or exit 20 (escalated).**

- **On exit 20 (escalated to a human):** the loop detected thrash or
  genuine disagreement and has terminated. Do not retry, do not start a
  new review. STOP and hand `.jeltz/review/escalation.md` to the human,
  then await their tiebreak.

- Any other exit code is an operational failure (fix the reported
  problem, then retry the same command); never work around the loop by
  skipping the review.

### Then STOP and output exactly:

`REVIEW PHASE COMPLETE -- awaiting human approval.`

Also include a short GitHub-style WIP message summarizing:
- The review outcome (rounds taken, findings fixed or rejected)
- The final commit message, updated for any review-driven fixes - this
  is the one message the human applies

The human approves only after this convergence; acceptance by the
reviewer happens before the human sees the task. Do not begin a new task
until I explicitly approve.

---

## Hard Constraints

- Never write implementation code in PHASE 1.
- Never refactor in PHASE 2.
- Never start a new TODO item without explicit approval.
- Always respect the RED -> GREEN -> REFACTOR -> REVIEW order.
- Always run tests before claiming a phase is complete.
- Always update `TODO.md` at the end of PHASE 3 before stopping.
- Never skip PHASE 4 or present unreviewed work to the human.
- Only the final STOP requires human approval.
- Produce the final commit message at the end of PHASE 3; PHASE 4 may
  only update it to reflect review-driven fixes, never produce another.
- Follow the **Editor & Hook Interaction Rules** strictly during RED.

Begin now with **PHASE 1 (RED)** for the next task in `TODO.md`.
