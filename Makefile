# Tooling for the jeltz distribution repo itself, not for consumer projects.
# CLAUDE.md in this tree is a reference copy shipped to consumers; the
# standard jeltz holds itself to is: shellcheck-clean shell formatted to the
# .editorconfig contract (T25), the full ruff rule set declared in
# pyproject.toml (T24), and a pytest-driven
# subprocess test suite. That tracked [tool.ruff] section also puts this tree
# on whole-file hook enforcement via hooks/lib/repo-mode.sh -- deliberately,
# since the tree is clean under those rules (see TODO.md T1, T22, T24).
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
RUFF := $(VENV)/bin/ruff

PREFLIGHT := UV="$(UV)" SHELLCHECK="$(SHELLCHECK)" SHFMT="$(SHFMT)" ./tools/preflight.sh

.PHONY: lint test verify venv lock preflight preflight-shell preflight-uv check-install

verify: preflight lint test

# verify checks everything up front, so one run names every gap rather than
# revealing prerequisites one build at a time. The narrower targets check
# only what they invoke: lint needs no shell tools it does not run and
# provisioning needs no shell linters. Note that lint DOES need uv, because
# ruff comes from the venv - but transitively, through `venv`, so the
# preflight keeps gating exactly the tools uv cannot install.
preflight:
	@$(PREFLIGHT) uv shellcheck shfmt

preflight-shell:
	@$(PREFLIGHT) shellcheck shfmt

preflight-uv:
	@$(PREFLIGHT) uv

# Two languages, two toolchains, one gate. shellcheck and shfmt are external
# prerequisites the preflight gates; ruff is uv-provisioned like the rest of
# the Python tooling, so it arrives through `venv` (which needs uv) rather
# than through the preflight (which gates only what uv cannot install).
#
# Running from $(VENV) rather than PATH is the point of provisioning it: the
# lockfile decides which ruff version judges this tree, so two checkouts
# cannot disagree about whether the code is clean.
#
# Ruff is handed the tree rather than a file list, for the reason SH_SOURCES
# is a wildcard: a new Python file is covered without anyone remembering to
# add it. Discovery honours .gitignore and ruff's own excludes, so .venv and
# the caches stay out of it.
#
# shfmt gets -d and nothing else, deliberately (T25). The shell formatting
# contract lives in .editorconfig, where editors read it too, and shfmt
# discards every EditorConfig formatting option the moment it is handed any
# parser or printer flag. Restoring the old `-i 4 -ci` here would therefore
# not reinforce the contract, it would silently replace it.
lint: preflight-shell venv
	$(SHELLCHECK) -x -P hooks $(SH_SOURCES)
	$(SHFMT) -d $(SH_SOURCES)
	$(RUFF) check .
	$(RUFF) format --check .

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
