#!/bin/bash

# Claude has a very relaxed definition of what it considers to need
# linting and formatting, so we enforce the standards here.
#
# Enforcement scope is repo-aware (see lib/repo-mode.sh):
#
#   strict - whole file, with autofix and formatting. Trees that ship their own
#            ruff config are expected to be clean everywhere, so this is the
#            historical behaviour, unchanged.
#
#   diff   - only lines changed against HEAD. Trees with no staged or committed
#            standard carry a large body of pre-existing violations that are not
#            ours to fix. Worse, whole-file --fix and format rewrite lines we
#            never touched, which buries our actual change and manufactures
#            merge conflicts with whoever else is working in the file.
#
# "Staged or committed" is the precise boundary: the promotion to strict fires
# on the ruff declaration reaching the git index, not on it merely existing in
# the worktree. See _hook_index_declares_ruff in lib/repo-mode.sh.
#
# A new or untracked file is entirely ours, so it gets the strict treatment
# regardless of which tree it lives in.

exit_code=0
input=$(cat)

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/repo-mode.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/repo-mode.sh"

if command -v ruff >/dev/null 2>&1; then
    if echo "$input" | jq -e '.tool_response.filePath | test("\\.py$")' >/dev/null 2>&1; then
        filepath=$(echo "$input" | jq -r '.tool_response.filePath')

        if [ -f "$filepath" ]; then
            mode=$(hook_repo_mode "$filepath")
            ranges=$(hook_changed_ranges "$filepath")

            # Nothing tracked to compare against means every line is ours.
            if [ "$ranges" = "ALL" ]; then
                mode="strict"
            fi

            if [ "$mode" = "strict" ]; then
                # lint before formatting
                # use a development-tolerant ruleset to avoid thrashing during TDD
                lint_json=$(ruff check \
                    --ignore F401,F841,F821 \
                    --fix \
                    --output-format json \
                    "$filepath" 2>/dev/null)
                rc=$?
                if [ $rc -eq 1 ]; then
                    jq -n --argjson errors "$lint_json" \
                        '{decision: "block", reason: "Ruff found linting issues. Fix the reported errors.", lint_errors: $errors}' >&2
                    exit_code=2
                fi

                # format after linting
                if [ 0 -eq $exit_code ]; then
                    # "format" exits successfully regardless of whether it
                    # changes a file or not.  Silent edits causes context
                    # thrashing, but needless re-reading wastes context.
                    # We check if we would make edits, and prompt Claude
                    # only if we do.
                    ruff format --check "$filepath" >/dev/null 2>&1
                    rc=$?

                    if [ 1 -eq $rc ]; then
                        # File needs formatting - do it and notify
                        ruff format "$filepath" >/dev/null 2>&1
                        echo '{"decision": "block", "reason": "File auto-formatted for PEP 8 compliance. Re-read the file before making additional edits to avoid string matching failures."}' >&2
                        exit_code=2
                    elif [ 0 -eq $rc ]; then
                        # No formatting needed - silent success
                        echo '{"suppressOutput": true}' >&2
                        exit_code=0
                    else
                        echo '{"reason": "Ruff format check failed with an unexpected exit code"}' >&2
                        exit_code=$rc
                    fi
                fi

            elif [ -z "$ranges" ]; then
                # Edit left the file identical to HEAD. Nothing of ours to judge.
                echo '{"suppressOutput": true}' >&2
                exit_code=0

            else
                # Diff-scoped lint. No --fix here: autofix is a whole-file
                # rewrite and would touch lines outside our change.
                lint_json=$(ruff check \
                    --ignore F401,F841,F821 \
                    --output-format json \
                    "$filepath" 2>/dev/null)

                if ! echo "$lint_json" | jq -e 'type == "array"' >/dev/null 2>&1; then
                    lint_json='[]'
                fi

                lines_json=$(printf '%s\n' "$ranges" |
                    awk 'NF == 2 { for (i = $1; i <= $2; i++) print i }' |
                    jq -Rn '[inputs | select(length > 0) | tonumber]')

                # Bind the row before switching the pipeline's input to $lines,
                # otherwise "." inside select() refers to the line array.
                scoped=$(jq -n \
                    --argjson errors "$lint_json" \
                    --argjson lines "$lines_json" \
                    '[$errors[] | select(.location.row as $r | $lines | index($r))]')

                scoped_count=$(jq 'length' <<<"$scoped" 2>/dev/null)
                if [ "${scoped_count:-0}" -gt 0 ]; then
                    jq -n --argjson errors "$scoped" \
                        '{decision: "block", reason: "Ruff found linting issues on lines you changed. Fix the reported errors. This tree ships no ruff config, so pre-existing violations on lines you did not touch are deliberately not reported -- do not clean them up, it only adds conflict surface.", lint_errors: $errors}' >&2
                    exit_code=2
                fi

                if [ 0 -eq $exit_code ]; then
                    needs_format=0
                    while read -r start end; do
                        [ -z "$start" ] && continue
                        ruff format --check --range="${start}:1-${end}:9999" \
                            "$filepath" >/dev/null 2>&1
                        [ 1 -eq $? ] && needs_format=1
                    done <<<"$ranges"

                    if [ 1 -eq $needs_format ]; then
                        # Snapshot first: the only sound way to report what the
                        # formatter touched is to compare against the file as it
                        # stood before, in pre-format line numbers.
                        snapshot=$(mktemp "${TMPDIR:-/tmp}/ruffhook.XXXXXX") || snapshot=""
                        if [ -n "$snapshot" ]; then
                            # Clean up on every exit path, not just the happy
                            # one. The snapshot is a verbatim copy of the user's
                            # file, and this hook can be interrupted between
                            # taking it and reporting.
                            trap 'rm -f "$snapshot"' EXIT INT TERM HUP
                            cp "$filepath" "$snapshot"
                        fi

                        # Apply bottom-up. Range formatting can add or remove
                        # blank lines around the region it touches, which would
                        # invalidate the line numbers of every hunk below it.
                        while read -r start end; do
                            [ -z "$start" ] && continue
                            ruff format --range="${start}:1-${end}:9999" \
                                "$filepath" >/dev/null 2>&1
                        done <<<"$(printf '%s\n' "$ranges" | sort -rn)"

                        # Ruff formats the whole statement enclosing a range, so
                        # this can re-indent or delete committed sibling lines
                        # inside a multi-line call. Bounded and semantically
                        # neutral, but never silent -- name the lines it reached
                        # beyond the edit so they can be reviewed rather than
                        # discovered in a diff later.
                        # No explicit removal here -- the EXIT trap above owns
                        # the snapshot's lifetime, so there is exactly one
                        # cleanup path rather than two that can disagree.
                        widened=""
                        [ -n "$snapshot" ] &&
                            widened=$(hook_format_widened "$snapshot" "$filepath" "$ranges")

                        if [ -n "$widened" ]; then
                            jq -n --arg lines "$widened" '{
                                decision: "block",
                                reason: ("Lines you changed were auto-formatted for PEP 8 compliance. Ruff also reformatted the enclosing statement, rewriting or removing these previously unchanged lines (numbered as the file stood before formatting): " + $lines + ". Confirm they are yours to touch before continuing. Re-read the file before making additional edits to avoid string matching failures.")
                            }' >&2
                        else
                            echo '{"decision": "block", "reason": "Lines you changed were auto-formatted for PEP 8 compliance. Ruff formats the whole statement enclosing a change, so adjacent blank lines may also have shifted. Re-read the file before making additional edits to avoid string matching failures."}' >&2
                        fi
                        exit_code=2
                    else
                        echo '{"suppressOutput": true}' >&2
                        exit_code=0
                    fi
                fi
            fi

        else
            echo '{"reason": "File not found"}' >&2
            exit_code=1
        fi

    else
        # Not a Python file
        echo '{"suppressOutput": true}' >&2
        exit_code=0
    fi

else
    # non-blocking
    echo '{"reason": "ruff not found in PATH"}' >&2
    exit_code=1
fi

exit $exit_code
