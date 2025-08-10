#!/bin/bash

# Claude has a very relaxed definition of what it considers to need
# linting and formatting, so we enforce the standards here.

exit_code=0
input=$(cat)

if command -v ruff > /dev/null 2>&1; then
    if echo "$input" | jq -e '.tool_response.filePath | test("\\.py$")' > /dev/null 2>&1; then
        filepath=$(echo "$input" | jq -r '.tool_response.filePath')
        if [ -f "$filepath" ]; then
            echo "Running ruff on $filepath"
            # Claude will only process stderr ...
            ruff check --fix "$filepath" 1>&2
            rc=$?
            if [ $rc -eq 1 ]; then
                # ... and only on exit code 2 (blocking)
                exit_code=2	
            fi
         echo "Ruff formatting complete for $filepath"
        fi
    fi
    if [ 0 -eq $exit_code ]; then
        # format after linting
        ruff format "$filepath"
    fi
    exit $exit_code
else
    echo "ruff not found in PATH" >&2
    exit 1
fi
