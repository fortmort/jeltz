#!/bin/bash

# Install the skills shipped by this repo into a consumer project or a user
# home, so all four supported hosts discover them.
#
# Project scope (default):
#   .claude/skills/<name>/   real copies  -> Claude Code, grok (native)
#   .agents/skills           symlink      -> codex, antigravity
#
# The symlink is the load-bearing trick, spike-verified 2026-08-15 against
# codex 0.147.0 and agy 1.1.13: both discover project skills through
# .agents/skills even when it is a symlink into .claude/skills, and
# antigravity needs no .agents/skills.json for the standard location.
#
# User scope (--user):
#   $HOME/.claude/skills/<name>/   -> Claude Code, grok
#   $CODEX_HOME/skills/<name>/     -> codex ($CODEX_HOME defaults to ~/.codex)
#
# A manifest (.jeltz-manifest) is stamped beside the installed skills:
# a version header plus one sha256 line per file. --check recomputes and
# reports drift, exit 1.

set -euo pipefail

JELTZ_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MANIFEST_NAME=".jeltz-manifest"

usage() {
    echo "usage: install.sh [--check] <consumer-repo> | install.sh --user" >&2
    exit 2
}

jeltz_version() {
    git -C "$JELTZ_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown"
}

sha256() {
    shasum -a 256 "$1" | cut -d' ' -f1
}

# install_skills_into <skills-root>
#
# Copy every skills/<name>/ from the repo into <skills-root> and stamp the
# manifest there.
install_skills_into() {
    local root="$1" src name rel
    mkdir -p "$root"
    local manifest="$root/$MANIFEST_NAME"
    echo "# jeltz $(jeltz_version)" >"$manifest"
    for src in "$JELTZ_ROOT"/skills/*/; do
        name=$(basename "$src")
        rm -rf "${root:?}/$name"
        cp -R "$src" "$root/$name"
        while IFS= read -r -d '' f; do
            rel="${f#"$root"/}"
            echo "$(sha256 "$f")  $rel" >>"$manifest"
        done < <(find "$root/$name" -type f -print0 | sort -z)
    done
}

# check_files <skills-root>
#
# Verify every manifest entry hashes to what was installed. Reports each
# drifted or missing file; returns 1 on any drift.
check_files() {
    local root="$1" ok=0 hash rel
    local manifest="$root/$MANIFEST_NAME"
    if [ ! -f "$manifest" ]; then
        echo "no manifest at $manifest; not a jeltz install" >&2
        return 1
    fi
    while read -r hash rel; do
        [ "$hash" = "#" ] && continue
        [ -z "$rel" ] && continue
        if [ ! -f "$root/$rel" ]; then
            echo "MISSING $rel"
            ok=1
        elif [ "$(sha256 "$root/$rel")" != "$hash" ]; then
            echo "DRIFTED $rel"
            ok=1
        fi
    done <"$manifest"
    return "$ok"
}

# check_agents_link <consumer-repo>
#
# Verify .agents/skills is a symlink resolving to .claude/skills. Codex and
# antigravity discover skills only through it, so a broken or retargeted
# link is drift even when every installed file still hashes clean.
check_agents_link() {
    local repo="$1" resolved expected
    local link="$repo/.agents/skills"
    if [ ! -L "$link" ]; then
        echo "MISSING .agents/skills symlink (codex/antigravity discovery)"
        return 1
    fi
    resolved=$(readlink -f "$link" 2>/dev/null) || resolved=""
    expected=$(readlink -f "$repo/.claude/skills")
    if [ "$resolved" != "$expected" ]; then
        echo "DRIFTED .agents/skills symlink (points at ${resolved:-nothing}, expected $expected)"
        return 1
    fi
    return 0
}

# check_project_install <consumer-repo>
check_project_install() {
    local repo="$1" ok=0
    check_agents_link "$repo" || ok=1
    check_files "$repo/.claude/skills" || ok=1
    exit "$ok"
}

# project_install <consumer-repo>
project_install() {
    local repo="$1"
    [ -d "$repo" ] || usage
    install_skills_into "$repo/.claude/skills"
    mkdir -p "$repo/.agents"
    # ln -sfn cannot replace a real directory, only a symlink; if something
    # turned the link into a directory, clear it so reinstall repairs it.
    if [ -e "$repo/.agents/skills" ] && [ ! -L "$repo/.agents/skills" ]; then
        rm -rf "$repo/.agents/skills"
    fi
    ln -sfn "../.claude/skills" "$repo/.agents/skills"
    echo "installed $(jeltz_version) into $repo"
}

# user_install: $HOME/.claude/skills and $CODEX_HOME/skills.
user_install() {
    install_skills_into "$HOME/.claude/skills"
    install_skills_into "${CODEX_HOME:-$HOME/.codex}/skills"
    echo "installed $(jeltz_version) at user scope"
}

check=0
user=0
target=""
while [ $# -gt 0 ]; do
    case "$1" in
        --check) check=1 ;;
        --user) user=1 ;;
        -*) usage ;;
        *) target="$1" ;;
    esac
    shift
done

if [ "$user" = 1 ]; then
    [ "$check" = 1 ] && usage
    user_install
elif [ -n "$target" ]; then
    if [ "$check" = 1 ]; then
        check_project_install "$target"
    else
        project_install "$target"
    fi
else
    usage
fi
