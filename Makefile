# Tooling for the jeltz distribution repo itself, not for consumer projects.
# CLAUDE.md in this tree is a reference copy shipped to consumers; the
# standard jeltz holds itself to is: shellcheck + shfmt clean shell, and a
# pytest-driven subprocess test suite. No tracked ruff config is added here
# on purpose -- it would flip hooks/lib/repo-mode.sh to strict mode (see
# TODO.md T1).

SH_SOURCES := install.sh $(wildcard hooks/*.sh hooks/lib/*.sh review/*.sh)

VENV := .venv
PYTEST := $(VENV)/bin/pytest
DEPS_STAMP := $(VENV)/.deps-stamp

.PHONY: lint test verify check-install

verify: lint test

lint:
	shellcheck -x -P hooks $(SH_SOURCES)
	shfmt -i 4 -ci -d $(SH_SOURCES)

# Python under review/ carries a 100% coverage gate (the T1 decision,
# revisited now that T4 landed Python modules). Shell keeps the behavioral
# pytest standard instead.
# Depending on the stamp (not on the venv existing) means edits to
# requirements-dev.txt reinstall into an existing venv instead of leaving
# it stale.
test: $(DEPS_STAMP)
	$(PYTEST) tests --cov=review --cov-report=term-missing --cov-fail-under=100

$(DEPS_STAMP): requirements-dev.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --quiet -r requirements-dev.txt
	touch $@

# Drift check for an installed consumer repo: make check-install TARGET=/path
check-install:
	@test -n "$(TARGET)" || { echo "usage: make check-install TARGET=<consumer-repo>" >&2; exit 2; }
	./install.sh --check "$(TARGET)"
