#!/bin/bash

# Claude has a very relaxed definition of what it considers to need
# linting and formatting, so we enforce the standards here.

exit_code=0
input=$(cat)

if command -v ruff >/dev/null 2>&1; then
    if echo "$input" | jq -e '.tool_response.filePath | test("\\.py$")' >/dev/null 2>&1; then
        filepath=$(echo "$input" | jq -r '.tool_response.filePath')

        if [ -f "$filepath" ]; then
            # lint before formatting
            lint_json=$(ruff check --fix --output-format json "$filepath" 2>/dev/null)
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
