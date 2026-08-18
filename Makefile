# Tooling for the jeltz distribution repo itself, not for consumer projects.
# CLAUDE.md in this tree is a reference copy shipped to consumers; the
# standard jeltz holds itself to is: shellcheck + shfmt clean shell, and a
# pytest-driven subprocess test suite. pyproject.toml carries no [tool.ruff]
# section on purpose -- a TRACKED ruff config flips hooks/lib/repo-mode.sh to
# whole-file strict mode for this tree, so it lands with T24's full-rules
# compliance rather than with packaging (see TODO.md T1, T22, T24).

SH_SOURCES := install.sh $(wildcard hooks/*.sh hooks/lib/*.sh review/*.sh)

PYTHON ?= python3
VENV := .venv
PYTEST := $(VENV)/bin/pytest
DEPS_STAMP := $(VENV)/.deps-stamp

.PHONY: lint test verify venv check-install

verify: lint test

lint:
	shellcheck -x -P hooks $(SH_SOURCES)
	shfmt -i 4 -ci -d $(SH_SOURCES)

# Python under review/ carries a 100% coverage gate (the T1 decision,
# revisited now that T4 landed Python modules). Shell keeps the behavioral
# pytest standard instead.
# Depending on the stamp (not on the venv existing) means edits to
# pyproject.toml reinstall into an existing venv instead of leaving it stale.
test: $(DEPS_STAMP)
	$(PYTEST) tests --cov=review --cov-report=term-missing --cov-fail-under=100

# Provision the development environment from pyproject.toml alone: the
# shipped engine's production dependencies (the `.`) plus the dev group used
# to develop jeltz. The `.` installs no jeltz code -- packages = [] makes the
# built distribution metadata-only -- so nothing shadows the working tree.
#
# The venv is seeded with the interpreter's BUNDLED pip, and PEP 735
# dependency groups need pip >= 25.1 (CPython 3.11, the declared floor,
# bundles 24.0). Raise it before installing rather than assume the ambient
# one is new enough; already-satisfied is a no-op, so this costs nothing on a
# current interpreter.
venv: $(DEPS_STAMP)

$(DEPS_STAMP): pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet 'pip>=25.1'
	$(VENV)/bin/pip install --quiet . --group dev
	touch $@

# Drift check for an installed consumer repo: make check-install TARGET=/path
check-install:
	@test -n "$(TARGET)" || { echo "usage: make check-install TARGET=<consumer-repo>" >&2; exit 2; }
	./install.sh --check "$(TARGET)"
