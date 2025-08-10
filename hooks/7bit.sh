#!/bin/bash

# Claude Code loves emojis.  Even when explicitly told "DO NOT", it somehow
# interprets that as "please add more 🦄".  We check for the presence
# of non-7-bit ASCII characters and instruct Claude Code what to do
# (e.g. "Use text like [OK]") and where.
#
# Exit codes:
# 0: No non-7-bit ASCII characters found
# 1: Script usage error (e.g., jq or file command not found, JSON parsing
#    failed, or file not found/readable)
# 2: Non-7-bit ASCII characters found; prints offending lines to STDERR

NON_ASCII_DETECTED=0
TOTAL_NON_ASCII_COUNT=0

# grep needs to support the -P option
GREP_CMD="grep"
if command -v ggrep &>/dev/null; then
  GREP_CMD="ggrep"
else
  if ! grep --help 2>&1 | grep -q -- '-P'; then
    echo "ERROR: Your 'grep' command ('$(command -v grep)') does not support the -P option (Perl-compatible regular expressions)" >&2
    exit 1
  fi
fi

# jq
if ! command -v jq &>/dev/null; then
  echo "ERROR: 'jq' command not found" >&2
  exit 1
fi

# file
if ! command -v file &>/dev/null; then
  echo "ERROR: 'file' command not found" >&2
  exit 1
fi

# slurp STDIN
INPUT_JSON=$(cat)

FILE_PATH=$(echo "$INPUT_JSON" | jq -r '.tool_response.filePath')
if [[ -z "$FILE_PATH" || "$FILE_PATH" == "null" ]]; then
  echo "ERROR: Could not extract 'filePath' from JSON input." >&2
  echo "       Expected JSON format: { \"tool_response\": { \"filePath\": \"/path/to/file\" } }" >&2
  exit 1
fi

get_codepoint() {
  # get Unicode codepoint using iconv
  local char="$1"
  # convert UTF-8 to UTF-32BE and get the hex value
  local hex_val=$(printf '%s' "$char" | iconv -f UTF-8 -t UTF-32BE 2>/dev/null | od -An -tx1 | tr -d ' \n')
  if [[ -n "$hex_val" ]]; then
    # convert hex to decimal (remove leading zeros)
    printf '%d' "0x${hex_val#00}"
  else
    echo "0"
  fi
}

# regular file and is readable?
if [[ -f "$FILE_PATH" && -r "$FILE_PATH" ]]; then
  MIME_TYPE=$(file --mime-type -b "$FILE_PATH")

  # is text or text-like?
  if [[ "$MIME_TYPE" == text/* || \
        "$MIME_TYPE" == application/json || \
        "$MIME_TYPE" == application/javascript || \
        "$MIME_TYPE" == application/x-shellscript || \
        "$MIME_TYPE" == application/xml || \
        "$MIME_TYPE" == application/x-php || \
        "$MIME_TYPE" == application/x-python || \
        "$MIME_TYPE" == application/x-ruby || \
        "$MIME_TYPE" == application/x-perl || \
        "$MIME_TYPE" == application/x-yaml || \
        "$MIME_TYPE" == application/x-csh || \
        "$MIME_TYPE" == application/x-awk || \
        "$MIME_TYPE" == application/x-make || \
        "$MIME_TYPE" == application/x-java || \
        "$MIME_TYPE" == application/x-swift || \
        "$MIME_TYPE" == application/x-objectivec || \
        "$MIME_TYPE" == application/x-go || \
        "$MIME_TYPE" == application/x-rust || \
        "$MIME_TYPE" == application/x-typescript || \
        "$MIME_TYPE" == application/x-markdown || \
        # empty text files
        "$MIME_TYPE" == inode/x-empty ]]; then

    # lines containing non-7-bit ASCII
    OFFENDING_LINES=$("${GREP_CMD}" -nP '[^\x00-\x7F]' "$FILE_PATH")

    if [[ -n "$OFFENDING_LINES" ]]; then
      NON_ASCII_DETECTED=1

      FILE_NON_ASCII_COUNT=$("${GREP_CMD}" -oP '[^\x00-\x7F]' "$FILE_PATH" | wc -l)
      TOTAL_NON_ASCII_COUNT=$((TOTAL_NON_ASCII_COUNT + FILE_NON_ASCII_COUNT))

      # notify Claude
      echo "ERROR: Non-7-bit ASCII characters detected in '$FILE_PATH':" >&2

      unique_chars_list=""
      emoji_chars=""
      symbol_chars=""
      arrow_chars=""
      box_chars=""
      other_chars=""

      while IFS= read -r line; do
        line_num=$(echo "$line" | cut -d: -f1)
        line_content=$(echo "$line" | cut -d: -f2-)

        echo "${line_num}:${line_content}" >&2

        non_ascii_on_line=$(echo "$line_content" | "${GREP_CMD}" -oP '[^\x00-\x7F]' 2>/dev/null || echo "")
        if [[ -n "$non_ascii_on_line" ]]; then
          echo -n "    Non-ASCII character(s): " >&2

          # process each character
          while IFS= read -r char; do
            if [[ -n "$char" ]]; then
              # get proper hex representation
              hex=$(printf '%s' "$char" | od -An -tx1 | tr -d ' \n' | sed 's/\(..\)/0x\1 /g')
              echo -n "'$char' ($hex) " >&2

              if [[ ! "$unique_chars_list" =~ "$char" ]]; then
                unique_chars_list="${unique_chars_list}${char}"

                codepoint=$(get_codepoint "$char")

                # check character ranges and add to appropriate category
                if [[ $codepoint -ge 128512 && $codepoint -le 128591 ]] || \
                   [[ $codepoint -ge 127744 && $codepoint -le 128511 ]] || \
                   [[ $codepoint -ge 129280 && $codepoint -le 129535 ]]; then
                  emoji_chars="${emoji_chars}${char} "
                elif [[ $codepoint -ge 9728 && $codepoint -le 9983 ]] || \
                     [[ $codepoint -ge 9984 && $codepoint -le 10175 ]] || \
                     [[ $codepoint -eq 10004 ]] || [[ $codepoint -eq 10060 ]] || \
                     [[ $codepoint -eq 10062 ]] || [[ $codepoint -eq 10067 ]] || \
                     [[ $codepoint -eq 10068 ]] || [[ $codepoint -eq 10069 ]] || \
                     [[ $codepoint -eq 10071 ]] || [[ $codepoint -eq 10003 ]] || \
                     [[ $codepoint -eq 10013 ]] || [[ $codepoint -eq 10014 ]]; then
                  symbol_chars="${symbol_chars}${char} "
                elif [[ $codepoint -ge 8592 && $codepoint -le 8703 ]] || \
                     [[ $codepoint -ge 8192 && $codepoint -le 8303 ]]; then
                  arrow_chars="${arrow_chars}${char} "
                elif [[ $codepoint -ge 9472 && $codepoint -le 9599 ]]; then
                  box_chars="${box_chars}${char} "
                else
                  other_chars="${other_chars}${char} "
                fi
              fi
            fi
          done <<< "$non_ascii_on_line"
          echo "" >&2
        fi
      done <<< "$OFFENDING_LINES"

      echo "" >&2

      if [[ -n "$unique_chars_list" ]]; then
        echo "Summary of non-ASCII characters by type:" >&2

        if [[ -n "$emoji_chars" ]]; then
          echo "  Emoji characters: $emoji_chars" >&2
          echo "    WARNING: Use text like [OK], [DONE], [WARN], [TODO], [INFO], [ERROR]" >&2
        fi

        if [[ -n "$symbol_chars" ]]; then
          echo "  Symbol characters: $symbol_chars" >&2
          echo "    WARNING: Use text like [CHECK], [X], [!], [*], [+], [-]" >&2
        fi

        if [[ -n "$arrow_chars" ]]; then
          echo "  Arrow characters: $arrow_chars" >&2
          echo "    WARNING: Use ASCII arrows like ->, <-, ^, v, |" >&2
        fi

        if [[ -n "$box_chars" ]]; then
          echo "  Box-drawing characters: $box_chars" >&2
          echo "    WARNING: Use ASCII art like +---, |, \`---" >&2
        fi

        if [[ -n "$other_chars" ]]; then
          echo "  Other characters: $other_chars" >&2
          echo "    WARNING: Replace with appropriate ASCII text" >&2
        fi

        echo "" >&2
      fi
    fi
  else
    echo "INFO: Skipping binary or non-text file: '$FILE_PATH' (MIME type: $MIME_TYPE)" >&2
  fi
elif [[ ! -f "$FILE_PATH" ]]; then
  echo "ERROR: File not found or is not a regular file: '$FILE_PATH'" >&2
  exit 1
elif [[ ! -r "$FILE_PATH" ]]; then
  echo "ERROR: File not readable: '$FILE_PATH'" >&2
  exit 1
fi

if [[ "$NON_ASCII_DETECTED" -eq 1 ]]; then
  echo "Total non-ASCII characters found: $TOTAL_NON_ASCII_COUNT" >&2
  echo "Fix required before commit can proceed." >&2
  exit 2
else
  exit 0
fi
