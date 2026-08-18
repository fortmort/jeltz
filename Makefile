# Tooling for the jeltz distribution repo itself, not for consumer projects.
# CLAUDE.md in this tree is a reference copy shipped to consumers; the
# standard jeltz holds itself to is: shellcheck + shfmt clean shell, and a
# pytest-driven subprocess test suite. pyproject.toml carries no [tool.ruff]
# section on purpose -- a TRACKED ruff config flips hooks/lib/repo-mode.sh to
# whole-file strict mode for this tree, so it lands with T24's full-rules
# compliance rather than with packaging (see TODO.md T1, T22, T24).
#
# uv is the single installer for every Python-ecosystem tool (T23), resolved
# from the committed uv.lock so two checkouts get identical versions. uv,
# shellcheck, and shfmt are external prerequisites it cannot provide; the
# preflight check names any that are missing before a recipe runs. Each is
# overridable (UV=, SHELLCHECK=, SHFMT=) so tests can point one at a stub.

SH_SOURCES := install.sh $(wildcard hooks/*.sh hooks/lib/*.sh review/*.sh tools/*.sh)

UV ?= uv
SHELLCHECK ?= shellcheck
SHFMT ?= shfmt

VENV := .venv
PYTEST := $(VENV)/bin/pytest

PREFLIGHT := UV="$(UV)" SHELLCHECK="$(SHELLCHECK)" SHFMT="$(SHFMT)" ./tools/preflight.sh

.PHONY: lint test verify venv lock preflight preflight-shell preflight-uv check-install

verify: preflight lint test

# verify checks everything up front, so one run names every gap rather than
# revealing prerequisites one build at a time. The narrower targets check
# only what they invoke: `make lint` needs no provisioner, and provisioning
# needs no shell linters.
preflight:
	@$(PREFLIGHT) uv shellcheck shfmt

preflight-shell:
	@$(PREFLIGHT) shellcheck shfmt

preflight-uv:
	@$(PREFLIGHT) uv

# Shell tools only, so this needs no provisioner today. T24 puts ruff in the
# dev dependency group and runs it here; ruff arrives from the venv, not from
# the preflight, so that task's edit is `lint: preflight-shell venv` - which
# brings uv back transitively. tests/test_provisioning.py's
# test_lint_does_not_require_the_python_provisioner is the tripwire and is
# expected to change with it.
lint: preflight-shell
	$(SHELLCHECK) -x -P hooks $(SH_SOURCES)
	$(SHFMT) -i 4 -ci -d $(SH_SOURCES)

# Python under review/ carries a 100% coverage gate (the T1 decision,
# revisited now that T4 landed Python modules). Shell keeps the behavioral
# pytest standard instead.
# Provisioning first, unconditionally: an up-to-date sync costs milliseconds,
# which is cheaper than the class of bug where tests run against a venv that
# predates a dependency change.
test: venv
	$(PYTEST) tests --cov=review --cov-report=term-missing --cov-fail-under=100

# Install the dev environment from the lockfile. --locked refuses a lock that
# trails pyproject.toml rather than re-resolving: the pins are the point, and
# a silent rewrite would mutate a tracked file -- which is a failed review
# when the reviewer runs make verify inside the T6 worktree. Run `make lock`
# to refresh it deliberately.
#
# --no-install-project installs the dependencies without building jeltz
# itself. The distribution is metadata-only (T22 `packages = []`), so building
# it would install nothing while writing egg-info and build/ droppings into
# the tree that the T6 integrity check reads as tampering.
venv: preflight-uv
	$(UV) sync --locked --no-install-project

lock: preflight-uv
	$(UV) lock

# Drift check for an installed consumer repo: make check-install TARGET=/path
check-install:
	@test -n "$(TARGET)" || { echo "usage: make check-install TARGET=<consumer-repo>" >&2; exit 2; }
	./install.sh --check "$(TARGET)"
