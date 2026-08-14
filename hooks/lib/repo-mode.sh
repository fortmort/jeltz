#!/bin/bash

# Shared helpers for repo-aware Claude Code hooks.
#
# Two enforcement regimes:
#
#   strict - the tree declares its own standard (a git-TRACKED ruff config), so
#            every file in it is expected to be clean and whole-file
#            enforcement is correct. This is the historical behaviour.
#
#   diff   - the tree declares no standard. Pre-existing violations are not
#            ours to fix, and rewriting shared files manufactures merge
#            conflicts with whoever else is editing them. Only lines changed
#            against HEAD are enforced.
#
# The mode is DERIVED, not configured. A tree upgrades itself to strict the
# moment it gains a tracked ruff config, so there is no local switch to
# remember to flip and no way to leave a repo permanently degraded.
#
# Detection mirrors ruff's own config resolution: a pyproject.toml only counts
# when it actually carries a [tool.ruff] section, which is why a project can
# have a pyproject.toml and still resolve to the user-level fallback.
#
# It adds one condition ruff itself does not apply: the config must be TRACKED.
# An untracked config is a work in progress, and promoting a whole tree to
# whole-file enforcement is not something a scratch file should be able to do
# by existing. This matters concretely -- the first step of adopting a ruff
# config is creating an untracked one, which would otherwise flip every file in
# the tree to strict before any of the cleanup that config implies has landed.
#
# These run on every single file edit, so the directory walk uses parameter
# expansion rather than forking dirname at each level.

# _hook_parent <dir>
#
# Echoes the parent of an absolute directory path, or the path itself when it
# is already the root. Pure string manipulation, no subprocess.
_hook_parent() {
    local d="${1%/}"
    d="${d%/*}"
    [ -z "$d" ] && d="/"
    echo "$d"
}

# _hook_dirof <filepath>
#
# Echoes the containing directory of a file path, canonicalised when the path
# is relative.
_hook_dirof() {
    local p="$1" d
    case "$p" in
        */*) d="${p%/*}" ;;
        *) d="." ;;
    esac
    [ -z "$d" ] && d="/"
    case "$d" in
        /*) echo "$d" ;;
        *) (cd "$d" 2>/dev/null && pwd) || echo "" ;;
    esac
}

# _hook_index_declares_ruff <dir>
#
# True when the git INDEX copy of <dir>/pyproject.toml carries a [tool.ruff]
# section.
#
# Reading the worktree copy is not enough. The common path to adopting ruff is
# adding [tool.ruff] to a pyproject.toml that is ALREADY tracked, so a check
# that only asks "is this file tracked?" promotes the whole tree to strict the
# instant the section is typed -- before it is staged, and before any of the
# cleanup it implies has landed. Ask the index what it actually holds.
#
# Outside a git repo there is no index, so the worktree copy is used.
_hook_index_declares_ruff() {
    local d="$1"
    if git -C "$d" rev-parse --show-toplevel >/dev/null 2>&1; then
        git -C "$d" show ":./pyproject.toml" 2>/dev/null |
            grep -q '^\[tool\.ruff'
    else
        grep -q '^\[tool\.ruff' "$d/pyproject.toml" 2>/dev/null
    fi
}

# _hook_is_tracked <filepath>
#
# True when git tracks the file. Outside a git repo there is nothing to check,
# so existence alone is accepted.
_hook_is_tracked() {
    local p="$1" d
    d=$(_hook_dirof "$p")
    [ -z "$d" ] && return 1
    git -C "$d" rev-parse --show-toplevel >/dev/null 2>&1 || return 0
    git -C "$d" ls-files --error-unmatch -- "$p" >/dev/null 2>&1
}

# hook_repo_mode <filepath>
#
# Echoes "strict" or "diff".
#
# Resolution order:
#   1. An uncommitted override in <git-common-dir>/info/claude-hook-mode.
#   2. A .claude-hook-mode file anywhere from the file's directory to the
#      repo root.
#   3. A ruff config (ruff.toml, .ruff.toml, or pyproject.toml carrying a
#      [tool.ruff] section) anywhere from the file's directory to the repo
#      root -> strict.
#   4. Otherwise -> diff.
hook_repo_mode() {
    local path="$1"
    local dir root gitdir prev

    dir=$(_hook_dirof "$path")
    [ -z "$dir" ] && {
        echo "diff"
        return
    }

    # An explicit override beats detection. Kept under the git common dir so it
    # is per-clone and never committed -- it is not a negotiation with anyone
    # else working in the repo.
    gitdir=$(git -C "$dir" rev-parse --git-common-dir 2>/dev/null)
    if [ -n "$gitdir" ]; then
        case "$gitdir" in
            /*) ;;
            *) gitdir="$dir/$gitdir" ;;
        esac
        if [ -f "$gitdir/info/claude-hook-mode" ]; then
            head -n1 "$gitdir/info/claude-hook-mode" | tr -d '[:space:]'
            return
        fi
    fi

    root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)

    while :; do
        if [ -f "$dir/.claude-hook-mode" ]; then
            head -n1 "$dir/.claude-hook-mode" | tr -d '[:space:]'
            return
        fi

        if [ -f "$dir/ruff.toml" ] && _hook_is_tracked "$dir/ruff.toml"; then
            echo "strict"
            return
        fi

        if [ -f "$dir/.ruff.toml" ] && _hook_is_tracked "$dir/.ruff.toml"; then
            echo "strict"
            return
        fi

        if [ -f "$dir/pyproject.toml" ] && _hook_index_declares_ruff "$dir"; then
            echo "strict"
            return
        fi

        # Stop at the repo root. Walking past it would let an unrelated parent
        # directory decide the mode for this repo.
        if [ -n "$root" ] && [ "$dir" = "$root" ]; then
            break
        fi
        if [ "$dir" = "/" ]; then
            break
        fi

        prev="$dir"
        dir=$(_hook_parent "$dir")

        # Belt and braces: never spin if the walk stops making progress.
        if [ "$dir" = "$prev" ]; then
            break
        fi
    done

    echo "diff"
}

# hook_changed_ranges <filepath>
#
# Echoes one "<start> <end>" pair per changed hunk (inclusive, 1-indexed) for
# the file as it now stands versus HEAD, or the single token "ALL" when the
# whole file should be treated as ours.
#
# "ALL" is returned for files outside git, repos without a HEAD, and untracked
# files. An untracked file is one we just created, so every line in it is ours
# and deserves the full standard.
#
# Pure-deletion hunks contribute no lines and are skipped.
hook_changed_ranges() {
    local path="$1"
    local dir root

    dir=$(_hook_dirof "$path")
    [ -z "$dir" ] && {
        echo "ALL"
        return
    }

    root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)
    [ -z "$root" ] && {
        echo "ALL"
        return
    }

    git -C "$root" rev-parse --verify HEAD >/dev/null 2>&1 || {
        echo "ALL"
        return
    }

    if ! git -C "$root" ls-files --error-unmatch -- "$path" >/dev/null 2>&1; then
        echo "ALL"
        return
    fi

    git -C "$root" diff -U0 HEAD -- "$path" 2>/dev/null | awk '
        /^@@/ {
            # Hunk header: @@ -old,count +new,count @@
            match($0, /\+[0-9]+(,[0-9]+)?/)
            spec = substr($0, RSTART + 1, RLENGTH - 1)
            n = split(spec, part, ",")
            start = part[1] + 0
            count = (n > 1 ? part[2] + 0 : 1)
            if (count > 0) print start, start + count - 1
        }'
}

# hook_changed_lines <filepath>
#
# Echoes one changed line number per line, or "ALL". Convenience wrapper over
# hook_changed_ranges for checks that work line-at-a-time rather than on
# contiguous regions.
hook_changed_lines() {
    local ranges
    ranges=$(hook_changed_ranges "$1")

    if [ "$ranges" = "ALL" ]; then
        echo "ALL"
        return
    fi

    echo "$ranges" | awk 'NF == 2 { for (i = $1; i <= $2; i++) print i }'
}

# hook_format_widened <snapshot> <filepath> <ranges>
#
# Echoes, space-separated, the lines of <snapshot> that the formatter rewrote
# or removed and that lie OUTSIDE <ranges>.
#
# `ruff format --range` formats the whole syntactic statement enclosing a
# range, so a one-line change inside a multi-line call legitimately re-indents
# sibling lines that were already committed. That is bounded and semantically
# neutral, but it must not happen silently: silently rewriting a committed line
# is exactly the behaviour diff-scoping exists to prevent.
#
# Comparing sets of line NUMBERS before and after formatting is not sound.
# Formatting changes the line count, so every line below an insertion or
# deletion shifts, and a rewritten committed line can land on a number the
# original range already contained -- reporting nothing while the message
# claims only the changed lines moved. It also reports post-format numbers
# against pre-format ranges, so even a hit can name the wrong line.
#
# Instead, diff the pre-format snapshot against the result and read the OLD
# side of each hunk. Those numbers are in the same coordinate system as
# <ranges>, which is the only comparison that means anything. A hunk with an
# old-side count of zero is a pure insertion and rewrote nothing, so it
# contributes no lines.
hook_format_widened() {
    local snapshot="$1" path="$2" ranges="$3"

    [ "$ranges" = "ALL" ] && {
        echo ""
        return
    }
    [ -f "$snapshot" ] || {
        echo ""
        return
    }

    git diff --no-index --unified=0 -- "$snapshot" "$path" 2>/dev/null | awk '
        /^@@/ {
            # Hunk header: @@ -old,count +new,count @@
            match($0, /-[0-9]+(,[0-9]+)?/)
            spec = substr($0, RSTART + 1, RLENGTH - 1)
            n = split(spec, part, ",")
            start = part[1] + 0
            count = (n > 1 ? part[2] + 0 : 1)
            for (i = 0; i < count; i++) print start + i
        }' | RANGES="$ranges" awk '
        BEGIN {
            n = split(ENVIRON["RANGES"], rows, "\n")
            for (i = 1; i <= n; i++)
                if (split(rows[i], p, " ") == 2)
                    for (j = p[1] + 0; j <= p[2] + 0; j++) mine[j] = 1
        }
        !($1 in mine) { print $1 }
    ' | sort -n -u | tr '\n' ' ' | sed 's/ *$//'
}
