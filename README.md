# Jeltz: Claude Code Hooks

Claude Code is consistently inconsistent:

- **IOUs**: Declarations without delivery
  - `TODO: handle edge cases` (never handled)
  - `FIXME: this might break` (it already did)
  - Placeholder functions like `def process(): pass` with no logic
  - Comments that promise tests, docs, or cleanup - but deliver none
- **Hallucinations**: Imagined implementation posing as progress
  - Functions returning variables that were never defined
  - `return {"status": "success"}` in code that does not even succeed
  - References to nonexistent modules, classes, or APIs
  - Docstrings describing behavior that the code does not implement
- **Slop**: Emoji glitter, formatting roulette, and newline chaos
  - `print("\u2728 Done!")` (that escape is a sparkles emoji)
  - Mixed tabs and spaces, random blank lines
  - Files missing EOF newlines

These hooks and skills try to make Claude Code more deterministic: less Vogon poetry, more reproducible prose.

## Development

### Prerequisites

Three binaries have to be on `PATH` before anything else works. `make` names
any that are missing, with the remedy, before it runs a build step.

| Tool | Minimum | Why it is not installed for you | Install |
|---|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.8.1 | It is the installer everything else arrives through. | `brew install uv` |
| [shellcheck](https://www.shellcheck.net/) | any | Haskell binary; no Python package manager ships it. | `brew install shellcheck` |
| [shfmt](https://github.com/mvdan/sh) | any | Go binary; likewise. | `brew install shfmt` |

The uv minimum is the oldest version this repo is verified against, and it is
enforced, not merely documented: `tools/preflight.sh` reads the installed
version and refuses an older one by name. Older uv releases may well work;
they are simply untested here.

Everything from the Python ecosystem - pytest, pytest-cov, jsonschema, ruff -
is provisioned by uv from `pyproject.toml` and the committed `uv.lock`, so no
Python tool needs installing by hand and every checkout gets the same
versions. uv supplies the interpreter too if the host has no Python matching
`requires-python`.

### Working on the repo

```sh
make verify   # lint + tests; provisions the environment first
make venv     # provision .venv from uv.lock, nothing else
make lint     # shellcheck + shfmt over the shell, ruff over the Python
make test     # pytest with the 100% coverage gate on review/
```

`make test` syncs before running, so tests never execute against an
environment that predates a dependency change. `make lint` syncs too, because
the ruff that judges this tree is the one the lockfile pins - not whichever
version happens to be on `PATH`.

The Python here is held to the full ruff rule set declared in
`pyproject.toml`, which is deliberately stricter than the abbreviated set the
shipped `hooks/ruff.sh` runs in consumer projects: that one tolerates unused
imports and undefined names because it fires mid-edit, where those are states
rather than defects.

The shell formatting contract lives in `.editorconfig` - four-space indents
and indented `case` clauses - so an editor and `make lint` read the same
rules. That is why the Makefile hands `shfmt` no formatting flags: shfmt
ignores EditorConfig entirely once it is given any parser or printer flag, so
a flag there would quietly replace the contract rather than restate it.

### Changing dependencies

Edit `pyproject.toml`, then refresh the pins:

```sh
make lock     # uv lock; commit the resulting uv.lock
```

Provisioning uses `uv sync --locked`, which **refuses** a lockfile that
trails `pyproject.toml` instead of quietly re-resolving it. That is
deliberate: the pins are the reproducibility guarantee, and a sync that
rewrote `uv.lock` on its own would mutate a tracked file - which fails the
review loop's integrity check when a reviewer runs `make verify` inside its
disposable worktree.
