#!/bin/bash

# Check the external prerequisites the Makefile cannot install for itself.
#
# uv provisions every Python-ecosystem tool from pyproject.toml and uv.lock,
# so pytest and friends need no check here - they either exist in .venv or
# `uv sync` puts them there. Three things are outside that reach:
#
#   - uv          the provisioner itself
#   - shellcheck  Haskell; no Python package manager ships it
#   - shfmt       Go; likewise
#
# Missing them mid-build produces a bare command-not-found from the middle of
# a recipe, which says nothing about what to install. This reports every
# problem at once, by name, with the remedy - then exits non-zero before any
# real work starts.
#
# Which tools to check comes from the arguments, defaulting to all of them:
# `make verify` asks for everything, so one run tells a contributor about
# every gap, while `make lint` asks only for what it runs and does not fail
# on a missing provisioner it never invokes. Tool PATHS come from the
# environment, so the Makefile's UV / SHELLCHECK / SHFMT overrides reach the
# check unchanged.

set -euo pipefail

# The oldest uv this repo is verified against. uv resolves PEP 735 dependency
# groups and syncs from the committed lockfile; both are long-standing, but an
# unverifiable floor is not a floor, so this names the version actually
# exercised. README.md documents this same number.
UV_MIN_VERSION="0.8.1"

UV="${UV:-uv}"
SHELLCHECK="${SHELLCHECK:-shellcheck}"
SHFMT="${SHFMT:-shfmt}"

problems=0

# Report one unmet prerequisite: what is wrong, then what to do about it.
report() {
    printf 'jeltz: %s\n' "$1" >&2
    printf '       %s\n' "$2" >&2
    problems=$((problems + 1))
}

# Report a tool that is not on PATH. Returns non-zero when it is missing, so
# callers can skip follow-up checks that would need to run it.
require() {
    local name="$1" binary="$2" remedy="$3"
    if ! command -v "$binary" >/dev/null 2>&1; then
        report "missing prerequisite: $name (looked for '$binary')" "$remedy"
        return 1
    fi
}

# True when version $1 is at least version $2, by version-sort ordering.
at_least() {
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" = "$2" ]
}

# Report a uv that is present but older than this repo is verified against.
check_uv_version() {
    local found=""
    found="$("$UV" --version 2>/dev/null | awk '{print $2}')" || found=""
    if [ -z "$found" ]; then
        report "uv did not report a version ('$UV --version')" \
            "check the install: uv self update, or brew install uv"
    elif ! at_least "$found" "$UV_MIN_VERSION"; then
        report "uv $found is too old: jeltz needs uv >= $UV_MIN_VERSION" \
            "upgrade it: uv self update, or brew upgrade uv"
    fi
}

# Check one named prerequisite. The `|| true` guards are load-bearing under
# set -e: require() reports by returning non-zero, and every tool has to be
# checked before anything exits.
check() {
    case "$1" in
        uv)
            if require uv "$UV" \
                "install it: brew install uv, or https://docs.astral.sh/uv/"; then
                check_uv_version
            fi
            ;;
        shellcheck)
            require shellcheck "$SHELLCHECK" \
                "install it: brew install shellcheck, or https://www.shellcheck.net/" || true
            ;;
        shfmt)
            require shfmt "$SHFMT" \
                "install it: brew install shfmt, or https://github.com/mvdan/sh" || true
            ;;
        *)
            printf 'jeltz: preflight asked for unknown prerequisite %s\n' "$1" >&2
            exit 2
            ;;
    esac
}

if [ "$#" -eq 0 ]; then
    set -- uv shellcheck shfmt
fi
for tool in "$@"; do
    check "$tool"
done

if [ "$problems" -gt 0 ]; then
    printf 'jeltz: %d unmet prerequisite(s); see the Development section of README.md\n' \
        "$problems" >&2
    exit 1
fi
