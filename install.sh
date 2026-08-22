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
#
# THE REMOVAL RULE (T27)
#
# This script writes into directories it does not own: someone's repository,
# someone's $HOME, both of which hold work it has no business deleting. So it
# removes only what the manifest records it installed, and it removes it with
# primitives that cannot take more than they are pointed at: `rm` with no -r
# on one recorded file, `rmdir` on a directory it has just emptied, `ln -s`
# with no -f so a link is never created over something. There is deliberately
# no recursive force-delete anywhere in this file.
#
# The same reasoning governs what it creates, and inspecting first is not
# enough there: a path can be replaced between the moment it is checked and
# the moment it is written. So a create is either create-or-fail, or it
# happens somewhere else first. A skill copy is assembled in a `mktemp -d`
# directory inside the install root - unpredictable, so nobody can be waiting
# at it - and placed with `mv -h`, which does not follow a symlink standing
# at the destination, it fails. The .agents/skills link is only ever created,
# with `ln -s` and no -f, so it can never land on top of anything. The
# manifest uses the cheaper form of the same idea: a noclobber redirect,
# whose O_CREAT|O_EXCL will not follow a symlink either.
#
# Nothing here overwrites a path it cannot attribute to itself, and there is
# no cleanup that would: a copy that could not be placed is LEFT where it is
# and named on the way out. Sweeping it away would mean walking a directory
# and removing whatever is in it, and the staging path can be swapped after
# it is created just like any other - at which point the sweeping up would be
# someone else's files.
#
# Two consequences are stated rather than papered over, and neither reaches
# outside the install root:
#
#   - A file at a path the manifest records is removed even if its content
#     was swapped after inspection. Recorded paths are exactly what this
#     script is licensed to delete.
#   - If a real DIRECTORY appears at a destination between inspection and the
#     write, it swallows what was going there - `mv` puts the copy inside it,
#     `ln -s` puts the link inside it - and both exit 0. That is the one case
#     a create cannot report by failing, so it is checked immediately after
#     and refused. Nothing in that directory is touched: the moved name is
#     unpredictable and `ln` will not overwrite an existing entry.
#
#
# Anything else in the way - a file jeltz never installed, a symlink where a
# skill directory belongs, a directory with someone's work in it - stops the
# run with the path and what to do about it. Every target is inspected before
# any of them is touched, so a refusal leaves the previous install exactly as
# it was rather than half-replaced.

set -euo pipefail

JELTZ_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MANIFEST_NAME=".jeltz-manifest"

# Relative paths the loaded manifest records, one per line. bash 3.2 (still
# what /bin/bash is on macOS) has no associative arrays, so membership is a
# newline-delimited case match rather than a lookup.
MANIFEST_PATHS=""

# Where .agents/skills must point for codex and antigravity to find the
# skills. Both functions that touch the link compare against this.
AGENTS_LINK_TARGET="../.claude/skills"

usage() {
    echo "usage: install.sh [--check] <consumer-repo> | install.sh --user" >&2
    exit 2
}

# refuse <what was found>
#
# Stop, naming the path and the way out. Every caller runs in the main shell,
# never a subshell, so this exit ends the install rather than a fork of it.
refuse() {
    echo "refusing to continue: $1" >&2
    echo "jeltz removes only what its manifest records installing." >&2
    echo "Inspect that path, then remove or move it yourself and re-run." >&2
    exit 1
}

jeltz_version() {
    git -C "$JELTZ_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown"
}

sha256() {
    shasum -a 256 -- "$1" | cut -d' ' -f1
}

# load_manifest <skills-root>
#
# Read the paths recorded under <skills-root> into MANIFEST_PATHS, or leave it
# empty when there is no manifest. The manifest is a text file in a directory
# anyone can edit, so an entry that escapes the root stops the run: a path
# outside the root is not ours to delete, and not ours to vouch for either.
load_manifest() {
    local root="$1"
    local manifest="$root/$MANIFEST_NAME"
    local hash rel
    MANIFEST_PATHS=""
    # The manifest is also written, at the end of an install. A symlink here
    # would aim that write at whatever it points at, which is how validating
    # the paths recorded INSIDE it can be true and beside the point.
    if [ -L "$manifest" ] || { [ -e "$manifest" ] && [ ! -f "$manifest" ]; }; then
        refuse "$manifest is not a regular file; jeltz's own manifest always is"
    fi
    [ -f "$manifest" ] || return 0
    while read -r hash rel; do
        [ "$hash" = "#" ] && continue
        [ -z "$rel" ] && continue
        case "$rel" in
            /* | .. | ../* | */../* | */..)
                refuse "$manifest records $rel, which is outside $root"
                ;;
        esac
        MANIFEST_PATHS="$MANIFEST_PATHS$rel"$'\n'
    done <"$manifest"
}

# manifest_lists <relative-path>
#
# True when the loaded manifest recorded installing exactly this path.
manifest_lists() {
    case $'\n'"$MANIFEST_PATHS" in
        *$'\n'"$1"$'\n'*) return 0 ;;
    esac
    return 1
}

# assert_plain_directory <path> <complaint>
#
# Refuse unless <path> is a real directory. A symlink is not one for this
# purpose: removing the recorded files "inside" it would reach through it into
# a directory jeltz never installed.
assert_plain_directory() {
    if [ -L "$1" ] || [ ! -d "$1" ]; then
        refuse "$2"
    fi
}

# assert_removable <skills-root> <name>
#
# Refuse unless <skills-root>/<name> is either absent or a plain directory
# holding nothing but files the manifest recorded installing. Read-only: it
# runs over every target before any of them is written to.
#
# Empty directories are allowed through even unrecorded, because emptying and
# removing one destroys nothing.
assert_removable() {
    local root="$1" name="$2"
    local target="$root/$name"
    local p rel
    [ -e "$target" ] || [ -L "$target" ] || return 0
    assert_plain_directory "$target" "$target is not a directory jeltz installed"
    while IFS= read -r -d '' p; do
        rel="${p#"$root"/}"
        if [ -L "$p" ]; then
            refuse "$p is a symlink; jeltz installs none under $root"
        elif [ -d "$p" ]; then
            continue
        elif [ ! -f "$p" ]; then
            refuse "$p is neither a regular file nor a directory"
        elif ! manifest_lists "$rel"; then
            refuse "$p is not recorded in $root/$MANIFEST_NAME"
        fi
    done < <(find "$target" -mindepth 1 -depth -print0)
}

# remove_installed <skills-root> <name>
#
# Clear a validated target: each recorded file individually, then the
# directories bottom-up with rmdir, which takes only what is already empty.
# The target's shape is re-asserted here rather than trusted from
# assert_removable, so a directory that became a symlink in between is not
# deleted through. What is deliberately NOT re-checked is the content of a
# recorded file: a path the manifest records is removed on that authority
# alone, which is the licence this whole script runs on.
remove_installed() {
    local root="$1" name="$2"
    local target="$root/$name"
    local p rel
    [ -e "$target" ] || [ -L "$target" ] || return 0
    assert_plain_directory "$target" "$target changed while the install was running"
    while IFS= read -r -d '' p; do
        rel="${p#"$root"/}"
        if [ -d "$p" ] && [ ! -L "$p" ]; then
            rmdir -- "$p" || refuse "$p is not empty"
        elif manifest_lists "$rel"; then
            rm -f -- "$p" || refuse "$p could not be removed"
        else
            refuse "$p appeared while the install was running"
        fi
    done < <(find "$target" -mindepth 1 -depth -print0)
    rmdir -- "$target" || refuse "$target is not empty"
}

# install_skills_into <skills-root>
#
# Copy every skills/<name>/ from the repo into <skills-root> and stamp the
# manifest there. The manifest is written last: it is the record of what was
# installed, and until the copies exist it would be describing nothing.
install_skills_into() {
    local root="$1"
    local manifest="$root/$MANIFEST_NAME"
    local src name target staging stage_name rel f hash lines
    mkdir -p "$root"
    load_manifest "$root"

    for src in "$JELTZ_ROOT"/skills/*/; do
        [ -d "$src" ] || refuse "$JELTZ_ROOT/skills holds no skills to install"
        assert_removable "$root" "$(basename "$src")"
    done

    lines="# jeltz $(jeltz_version)"$'\n'
    for src in "$JELTZ_ROOT"/skills/*/; do
        name=$(basename "$src")
        target="$root/$name"
        remove_installed "$root" "$name"
        # Build the copy where nobody can be waiting for it. mktemp -d creates
        # atomically under an unpredictable name, so every byte written here
        # lands inside the install root no matter what appears at $target.
        staging=$(mktemp -d "$root/.jeltz-install.XXXXXX")
        stage_name="${staging##*/}"
        cp -R -- "$src." "$staging/" ||
            refuse "$name could not be copied; the partial copy is in $staging"
        # -h is what makes this safe: handed a symlink at the destination, mv
        # does not follow it, it fails. A plain mv - or cp writing to $target
        # directly - would put the files wherever the link pointed.
        #
        # A failure leaves the staged copy where it is and says so. Removing
        # it would mean walking a directory and deleting whatever is in it,
        # which is the thing this script does not do: an unpredictable name
        # stops anyone getting there first, but nothing stops the directory
        # being swapped afterwards, and then the sweeping up would be someone
        # else's files.
        mv -h -- "$staging" "$target" ||
            refuse "$target appeared while $name was being installed; the new copy is in $staging"
        # The one case the rename cannot refuse: a real directory at $target
        # swallows the move instead of being replaced by it.
        if [ -e "$target/$stage_name" ] || [ -L "$target/$stage_name" ]; then
            refuse "$target was a directory when $name was placed, so the new copy is now in $target/$stage_name"
        fi
        # Arrival proves every shipped file is there; it does not prove
        # nothing else is. The placed directory has to BE the shipped tree,
        # or the install certifies a skill carrying a file jeltz never
        # shipped - the state a reinstall then refuses to touch.
        while IFS= read -r -d '' f; do
            rel="${f#"$target"/}"
            if [ -L "$f" ] || { [ -d "$f" ] && [ ! -d "$src$rel" ]; } ||
                { [ ! -d "$f" ] && [ ! -f "$src$rel" ]; }; then
                refuse "$target/$rel is not part of the $name jeltz ships"
            fi
        done < <(find "$target" -mindepth 1 -print0)
        # The manifest is built from what jeltz SHIPS, never from what is
        # found at the destination, and every shipped file is checked to have
        # arrived intact. Reading the destination instead would record
        # whatever ended up there - so a copy substituted after it was
        # written would be certified as jeltz's own, and the next ordinary
        # reinstall would delete someone else's file on that authority.
        while IFS= read -r -d '' f; do
            rel="${f#"$src"}"
            hash=$(sha256 "$f")
            if [ ! -f "$target/$rel" ] || [ -L "$target/$rel" ] ||
                [ "$(sha256 "$target/$rel")" != "$hash" ]; then
                refuse "$target/$rel is not the $name jeltz shipped, so it will not be recorded as one"
            fi
            lines="$lines$hash  $name/$rel"$'\n'
        done < <(find "$src" -type f -print0 | sort -z)
    done
    # Unlink first, then create under noclobber: O_CREAT|O_EXCL refuses to
    # follow a symlink, so one appearing in between makes the stamp fail
    # rather than overwrite whatever it points at.
    rm -f -- "$manifest"
    (
        set -C
        printf '%s' "$lines" >"$manifest"
    ) || refuse "$manifest could not be stamped; something took its place"
}

# check_files <skills-root>
#
# Verify every manifest entry hashes to what was installed. Reports each
# drifted or missing file; returns 1 on any drift. load_manifest runs first
# for its validation: an entry pointing outside the root must not be verified
# as if it were installed content, because a clean report here is what licenses
# the next reinstall to act on it.
check_files() {
    local root="$1" ok=0
    local manifest="$root/$MANIFEST_NAME"
    local hash rel
    if [ ! -f "$manifest" ]; then
        echo "no manifest at $manifest; not a jeltz install" >&2
        return 1
    fi
    load_manifest "$root"
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

# assert_agents_link_replaceable <consumer-repo>
#
# ln cannot put a link where a real directory already is - handed one, it
# creates the link inside it - so a non-symlink there has to go first. An
# empty directory may go (rmdir destroys nothing, and repairing a replaced
# link is a reinstall's job); a directory with contents, or a file, is
# someone's work and stops the run.
#
# So is a symlink pointing anywhere other than AGENTS_LINK_TARGET. Replacing
# one means overwriting a path this script cannot prove it made, and the only
# primitive that can replace a symlink will just as silently replace the
# regular file that may be there instead by the time it runs - which is the
# same file this function refuses when it arrives a moment earlier. Timing is
# not allowed to decide whether someone's file survives, so a link that is
# not ours is reported and left alone.
assert_agents_link_replaceable() {
    local repo="$1"
    local link="$repo/.agents/skills"
    if [ -L "$link" ]; then
        [ "$(readlink "$link")" = "$AGENTS_LINK_TARGET" ] ||
            refuse "$link points at $(readlink "$link"), not $AGENTS_LINK_TARGET"
        return 0
    fi
    [ -e "$link" ] || return 0
    [ -d "$link" ] || refuse "$link is a file where the .agents/skills symlink belongs"
    [ -z "$(find "$link" -mindepth 1)" ] ||
        refuse "$link is a directory with contents in it, not the .agents/skills symlink"
}

# install_agents_link <consumer-repo>
#
# Point .agents/skills at .claude/skills. The link is only ever CREATED, never
# written over: `ln -s` without -f fails if anything is already there, so a
# file or link arriving after the path was inspected is refused rather than
# replaced. The one thing that may be removed first is an empty directory,
# and rmdir is the check and the removal in one step.
install_agents_link() {
    local repo="$1"
    local link="$repo/.agents/skills"
    # Already ours and already right: touch nothing. Re-read rather than
    # trusting the earlier inspection, since doing nothing is always safe.
    if [ -L "$link" ]; then
        [ "$(readlink "$link")" = "$AGENTS_LINK_TARGET" ] ||
            refuse "$link points at $(readlink "$link"), not $AGENTS_LINK_TARGET"
        return 0
    fi
    if [ -e "$link" ]; then
        rmdir -- "$link" || refuse "$link is not an empty directory"
    fi
    ln -s "$AGENTS_LINK_TARGET" "$link" ||
        refuse "$link could not be created; something is in the way"
    # The one thing `ln -s` will not refuse: handed a directory it creates the
    # link INSIDE it and exits 0, which would leave codex and antigravity with
    # no discovery under an install that claimed success. Check what is there,
    # not what ln returned. Nothing in that directory is touched.
    if [ ! -L "$link" ] || [ "$(readlink "$link")" != "$AGENTS_LINK_TARGET" ]; then
        refuse "$link is a directory, so the new symlink went inside it as ${AGENTS_LINK_TARGET##*/}"
    fi
}

# project_install <consumer-repo>
project_install() {
    local repo="$1"
    [ -d "$repo" ] || usage
    assert_agents_link_replaceable "$repo"
    install_skills_into "$repo/.claude/skills"
    mkdir -p "$repo/.agents"
    install_agents_link "$repo"
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
