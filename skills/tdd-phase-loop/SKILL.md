---
name: tdd-phase-loop
description: Autonomous red/green/refactor TDD with selective test running, explicit stop points, editor & hook interaction rules, automatic TODO updates, and a single final commit message.
---

Your coding norms are defined in `@CLAUDE.md`.
The project specification is in `@docs/PROJECT_SPECIFICATION.md`.
Your task list is in `@TODO.md`.

You must follow a **strict, phase-gated TDD workflow** for this project.
You are not allowed to skip phases or merge them.

---

## Global Rules

1. Always use red/green/refactor TDD.
2. Never write implementation code before tests.
3. Never move to a new TODO item until I explicitly approve.
4. You may automatically proceed from RED -> GREEN -> REFACTOR without waiting for approval.
5. Only the final STOP requires human review.
6. I will commit manually; you must not commit or proceed on your own.
7. Use **selective test running** to reduce cycle time:
   - RED: run only the newly written tests
   - GREEN: run only the newly written tests
   - REFACTOR: run the full test suite via `make verify`

---

## Editor & Hook Interaction Rules (Critical for RED Phase)

To avoid thrashing with ruff, pre‑commit hooks, and unused‑import stripping, you must follow these rules:

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
The only requirement is that the **test file is syntactically valid Python**.

### 6. GREEN and REFACTOR may use multi-edit sequences
Only RED requires the “single edit” rule.

---

## PHASE 1 — RED (write failing tests only)

- Read the next task from `@TODO.md`.
- Write **ONLY the failing test(s)** required for this task.
- Follow the **Editor & Hook Interaction Rules** above.
- Run **only the newly written tests**, confirming they fail for the correct reason.
- When tests are written and confirmed failing, STOP and output exactly:

`RED PHASE COMPLETE — proceeding to GREEN.`

Also include a short GitHub‑style WIP message summarizing:
- What you tested
- Why these tests are needed now

After outputting this message, automatically begin PHASE 2.

---

## PHASE 2 — GREEN (write minimum implementation)

- Implement **ONLY the minimum code** required to make the new tests pass.
- **Do NOT** refactor or clean up beyond what is strictly necessary.
- Run **only the newly written tests**, confirming they now pass.
- When the new tests pass, STOP and output exactly:

`GREEN PHASE COMPLETE — proceeding to REFACTOR.`

Also include a short GitHub‑style WIP message summarizing:
- What code you added or changed
- Which tests are now passing because of it

After outputting this message, automatically begin PHASE 3.

---

## PHASE 3 — REFACTOR (improve design without changing behavior)

- Refactor the code and tests to improve clarity, structure, and maintainability.
- **Do NOT** change externally observable behavior.
- Keep tests passing at all times.
- Run the **full test suite** using `make verify` to ensure no regressions.

Before ending the phase, you must:

###  Update `@TODO.md`
- Mark the current task as complete.
- Remove or modify the relevant TODO entry as appropriate.
- Ensure the TODO list reflects the new project state so the session can be safely restarted.

###  Generate a single final commit message
Produce **one** GitHub‑style commit message summarizing the entire task, including:
- What the feature or fix *does*
- The tests added
- The implementation added
- The refactoring performed
- The final behavior of the code

This commit message must describe **what the code accomplishes**, not just what changed.

###  Then STOP and output exactly:

`REFACTOR PHASE COMPLETE — awaiting review.`

Also include a short GitHub‑style WIP message summarizing:
- What you refactored
- **What the code now *does***
- Why the design is better now

Do not begin a new task until I explicitly approve.

---

## Hard Constraints

- Never write implementation code in PHASE 1.
- Never refactor in PHASE 2.
- Never start a new TODO item without explicit approval.
- Always respect the RED -> GREEN -> REFACTOR order.
- Always run tests before claiming a phase is complete.
- Always update `@TODO.md` at the end of PHASE 3 before stopping.
- Only the final STOP requires human approval.
- Only produce a commit message at the end of PHASE 3.
- Follow the **Editor & Hook Interaction Rules** strictly during RED.

Begin now with **PHASE 1 (RED)** for the next task in `@TODO.md`.
