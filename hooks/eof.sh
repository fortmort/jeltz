#!/bin/bash

# Claude is consistently inconsistent with end-of-file newlines, despite
# continued prompting, so we use ed to ensure that text files (that
# aren't already handled by a formatter / linter) end with a newline.
# ed should silently exit if the file is not text, but we limit this
# to common text file extensions ... just in case.

input=$(cat)
if command -v ed > /dev/null 2>&1; then
    if echo "$input" | jq -e '
        .tool_response.filePath
        | test("\\.sh$|\\.md$|\\.txt$|\\.json$|\\.toml$|\\.yaml$|\\.ini$")
    ' > /dev/null 2>&1; then
        filepath=$(echo "$input" | jq -r '.tool_response.filePath')
        if [ -f "$filepath" ]; then
            echo "Running ed on $filepath"
            ed -s "$filepath" <<< w
        fi
    fi
else
    echo "ed not found in PATH" >&2
    exit 1
fi
