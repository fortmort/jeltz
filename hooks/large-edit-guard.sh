#!/bin/bash
#
# large-edit-guard.sh - Claude Code hook to guard against accidental file rewrites
#
# Detects when Write/Edit/MultiEdit operations would change a large portion of a file,
# which may indicate an accidental rewrite rather than a targeted edit.
#
# Behavior (low-friction “second chance”):
#   - First attempt over threshold: BLOCK via JSON permissionDecision:"deny"
#     and show Claude a detailed warning in permissionDecisionReason.
#     Also records a short-lived, ONE-TIME “retry token” keyed by (tool, file, proposal-hash).
#   - If Claude retries the SAME operation within RETRY_WINDOW seconds:
#     the hook allows it to proceed (no stdout), so Claude Code falls through to the normal
#     user permission prompt for that tool call.
#
# Opportunistic cleanup:
#   - Each invocation will remove a small batch of expired token files to keep CACHE_DIR tidy.
#
# Install as a PreToolUse hook in ~/.claude/settings.json:
#
# {
#   "hooks": {
#     "PreToolUse": [
#       {
#         "matcher": "Write|Edit|MultiEdit",
#         "hooks": [
#           { "type": "command", "command": "~/.claude/hooks/large-edit-guard.sh" }
#         ]
#       }
#     ]
#   }
# }
#
# Configuration via environment variables:
#   LARGE_EDIT_THRESHOLD        - Percentage threshold to trigger block+retry-token (default: 50)
#   LARGE_EDIT_WARN_THRESHOLD   - Percentage threshold to warn to stderr (default: 25)
#   LARGE_EDIT_MIN_LINES        - Minimum file size to check (default: 20)
#   LARGE_EDIT_ALLOW_PATTERNS   - Colon-separated glob patterns to skip (glob-style)
#   LARGE_EDIT_RETRY_WINDOW     - Seconds retry token remains valid (default: 120)
#   LARGE_EDIT_CACHE_DIR        - Override cache directory for retry tokens
#   LARGE_EDIT_CLEANUP_BATCH    - Max tokens to check per run (default: 20)
#   LARGE_EDIT_LOG_LEVEL        - Logging via syslog: off|error|warn|info|debug (default: off)
#   LARGE_EDIT_LOG_TAG          - Syslog tag for logger (default: claude.large-edit-guard)
#
# Notes:
# - Outputs MUST be JSON to stdout only when blocking (permissionDecision:"deny").
# - When allowing, the hook should output NOTHING and exit 0.
# - Uses per-file+per-proposal hashing so retry allowance is NOT blanket across files or edits.
#

set -euo pipefail

# Security: token files should be private
umask 077

# Configuration
THRESHOLD="${LARGE_EDIT_THRESHOLD:-50}"
WARN_THRESHOLD="${LARGE_EDIT_WARN_THRESHOLD:-25}"
MIN_LINES="${LARGE_EDIT_MIN_LINES:-20}"
ALLOW_PATTERNS="${LARGE_EDIT_ALLOW_PATTERNS:-}"
RETRY_WINDOW="${LARGE_EDIT_RETRY_WINDOW:-120}"
CLEANUP_BATCH="${LARGE_EDIT_CLEANUP_BATCH:-20}"

# Logging (syslog via logger)
LOG_LEVEL="${LARGE_EDIT_LOG_LEVEL:-off}"   # off|error|warn|info|debug
LOG_TAG="${LARGE_EDIT_LOG_TAG:-claude.large-edit-guard}"

log_level_num() {
  case "$1" in
    off)   echo 0 ;;
    error) echo 1 ;;
    warn)  echo 2 ;;
    info)  echo 3 ;;
    debug) echo 4 ;;
    *)     echo 0 ;;
  esac
}

LOG_LEVEL_N="$(log_level_num "$LOG_LEVEL")"

log_syslog() {
  # $1 = severity (error|warn|info|debug), $2... = message
  local sev="$1"; shift
  local pri="user.notice"
  case "$sev" in
    error) pri="user.err" ;;
    warn)  pri="user.warning" ;;
    info)  pri="user.info" ;;
    debug) pri="user.debug" ;;
  esac
  # Avoid stdout; logger writes to syslog/journald.
  logger -t "$LOG_TAG" -p "$pri" -- "$*"
}

log_error() { [[ "$LOG_LEVEL_N" -ge 1 ]] && log_syslog error "$*"; }
log_warn()  { [[ "$LOG_LEVEL_N" -ge 2 ]] && log_syslog warn  "$*"; }
log_info()  { [[ "$LOG_LEVEL_N" -ge 3 ]] && log_syslog info  "$*"; }
log_debug() { [[ "$LOG_LEVEL_N" -ge 4 ]] && log_syslog debug "$*"; }

# Cache directory (default XDG, then ~/.cache)
DEFAULT_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/claude/hooks/large-edit-guard"
CACHE_DIR="${LARGE_EDIT_CACHE_DIR:-$DEFAULT_CACHE_DIR}"
mkdir -p "$CACHE_DIR"

log_debug "event=hook_invoked cache_dir=$CACHE_DIR threshold=$THRESHOLD warn_threshold=$WARN_THRESHOLD retry_window_s=$RETRY_WINDOW cleanup_batch=$CLEANUP_BATCH"

# Read tool input from stdin (do not write anything else to stdout unless emitting JSON)
INPUT=$(cat)

# Extract tool name and file path
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

log_debug "event=parsed_input tool=$TOOL_NAME file=$FILE_PATH input_bytes=${#INPUT}"

# Exit early if we can't parse the input
if [[ -z "$FILE_PATH" ]]; then
    log_debug "event=exit reason=no_file_path"
    exit 0
fi

# Check if file matches any allowed patterns (skip checking)
if [[ -n "$ALLOW_PATTERNS" ]]; then
    IFS=':' read -ra PATTERNS <<< "$ALLOW_PATTERNS"
    for pattern in "${PATTERNS[@]}"; do
        # shellcheck disable=SC2053
        if [[ "$FILE_PATH" == $pattern ]]; then
            log_info "event=skip reason=allow_pattern tool=$TOOL_NAME file=$FILE_PATH pattern=$pattern"
            exit 0
        fi
    done
fi

# If file doesn't exist, this is a new file - allow it
if [[ ! -f "$FILE_PATH" ]]; then
    log_info "event=allow reason=new_file tool=$TOOL_NAME file=$FILE_PATH"
    exit 0
fi

# Read existing file content
OLD_CONTENT=$(cat "$FILE_PATH")
OLD_LINES=$(echo "$OLD_CONTENT" | wc -l)
OLD_BYTES=${#OLD_CONTENT}

# Skip small files
if [[ "$OLD_LINES" -lt "$MIN_LINES" ]]; then
    log_info "event=skip reason=file_too_small tool=$TOOL_NAME file=$FILE_PATH old_lines=$OLD_LINES min_lines=$MIN_LINES"
    exit 0
fi

log_debug "event=file_qualifies tool=$TOOL_NAME file=$FILE_PATH old_lines=$OLD_LINES old_bytes=$OLD_BYTES"

# --- Helpers ---

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | awk '{print $1}'
    else
        shasum -a 256 | awk '{print $1}'
    fi
}

token_file_for_key() {
    local key="$1"
    printf '%s/%s.token' "$CACHE_DIR" "$key"
}

# Opportunistically clean up expired tokens (bounded work per run)
cleanup_expired_tokens() {
    local now removed checked ts elapsed
    now=$(date +%s)
    removed=0
    checked=0

    shopt -s nullglob
    local tokens=("$CACHE_DIR"/*.token)
    shopt -u nullglob

    [[ ${#tokens[@]} -eq 0 ]] && return 0

    for token in "${tokens[@]}"; do
        [[ -f "$token" ]] || continue

        checked=$((checked + 1))
        if [[ "$checked" -gt "$CLEANUP_BATCH" ]]; then
            break
        fi

        ts=$(head -1 "$token" 2>/dev/null || echo "0")
        [[ "$ts" =~ ^[0-9]+$ ]] || ts=0
        elapsed=$((now - ts))

        if [[ "$elapsed" -ge "$RETRY_WINDOW" ]]; then
            rm -f "$token"
            removed=$((removed + 1))
        fi
    done

    log_debug "event=cleanup checked=$checked removed=$removed window_s=$RETRY_WINDOW"
}

compute_proposal_hash() {
    case "$TOOL_NAME" in
        Write)
            echo "$INPUT" | jq -r '.tool_input.content // ""' | sha256
            ;;
        Edit)
            local old new
            old=$(echo "$INPUT" | jq -r '.tool_input.old_string // ""')
            new=$(echo "$INPUT" | jq -r '.tool_input.new_string // ""')
            printf '%s\0%s' "$old" "$new" | sha256
            ;;
        MultiEdit)
            echo "$INPUT" | jq -c '.tool_input.edits // []' | sha256
            ;;
        *)
            printf '' | sha256
            ;;
    esac
}

compute_token_key() {
    local proposal_hash="$1"
    printf '%s\n%s\n%s\n' "$FILE_PATH" "$TOOL_NAME" "$proposal_hash" | sha256
}

should_allow_retry_and_consume() {
    local token_file="$1"
    local now ts elapsed

    [[ -f "$token_file" ]] || return 1

    now=$(date +%s)
    ts=$(head -1 "$token_file" 2>/dev/null || echo "0")
    [[ "$ts" =~ ^[0-9]+$ ]] || ts=0

    elapsed=$((now - ts))
    if [[ "$elapsed" -lt "$RETRY_WINDOW" ]]; then
        rm -f "$token_file"
        log_info "event=token_consume result=allow_retry tool=$TOOL_NAME file=$FILE_PATH elapsed_s=$elapsed"
        return 0
    fi

    rm -f "$token_file"
    log_debug "event=token_expired result=deny tool=$TOOL_NAME file=$FILE_PATH elapsed_s=$elapsed"
    return 1
}

record_retry_token() {
    local token_file="$1"
    local now tmp

    now=$(date +%s)
    tmp="$(mktemp "${CACHE_DIR}/.token.tmp.XXXXXX")"
    printf '%s\n' "$now" > "$tmp"
    mv "$tmp" "$token_file"
    log_debug "event=token_record tool=$TOOL_NAME file=$FILE_PATH ts=$now token_file=$token_file"
}

deny_with_reason() {
    local reason="$1"
    log_warn "event=deny tool=$TOOL_NAME file=$FILE_PATH reason=large_change"
    jq -n --arg reason "$reason" '{
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": $reason
        }
    }'
    exit 0
}

report_warning() {
    local change_percent=$1
    local edit_type=$2
    log_warn "event=warn tool=$TOOL_NAME file=$FILE_PATH percent=$change_percent edit_type=$edit_type"
    echo "[$0]: WARNING - Moderately large $edit_type (${change_percent}%)" >&2
    echo "File: $FILE_PATH" >&2
}

# Block on first attempt, allow on immediate retry of SAME proposal
report_large_edit() {
    local change_percent=$1
    local edit_type=$2
    local details=$3

    cleanup_expired_tokens || true

    local proposal_hash token_key token_file
    proposal_hash="$(compute_proposal_hash)"
    token_key="$(compute_token_key "$proposal_hash")"
    token_file="$(token_file_for_key "$token_key")"

    log_info "event=large_detected tool=$TOOL_NAME file=$FILE_PATH percent=$change_percent threshold=$THRESHOLD edit_type=$edit_type"

    if should_allow_retry_and_consume "$token_file"; then
        # Allow normal Claude Code user permission flow
        exit 0
    fi

    record_retry_token "$token_file"

    # Machine-parsable header (single line) followed by details.
    local header
    header="LARGE_EDIT_GUARD v1 action=blocked stage=first_attempt tool=${TOOL_NAME} percent=${change_percent} threshold=${THRESHOLD} retry_window_s=${RETRY_WINDOW} file=${FILE_PATH}"

    local warning_msg
    warning_msg="${header}

[LARGE EDIT WARNING] ~${change_percent}% of file would be modified (threshold: ${THRESHOLD}%)

${details}

GUIDANCE:
- Try to make the change smaller and more targeted.
- If a smaller change is possible, do that instead.
- If the large change is truly necessary, retry the SAME operation now (within ${RETRY_WINDOW}s).
  The retry will be allowed to proceed to the normal user permission prompt."

    deny_with_reason "$warning_msg"
}

# --- Tool-specific checks ---

case "$TOOL_NAME" in
    Write)
        NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
        [[ -z "$NEW_CONTENT" ]] && { log_debug "event=allow reason=empty_content tool=$TOOL_NAME file=$FILE_PATH"; exit 0; }

        NEW_LINES=$(echo "$NEW_CONTENT" | wc -l)
        NEW_BYTES=${#NEW_CONTENT}

        set +e
        diff_out=$(diff --unchanged-line-format='.' --old-line-format='' --new-line-format='' \
            <(echo "$OLD_CONTENT") <(echo "$NEW_CONTENT") 2>/dev/null)
        diff_rc=$?
        set -e

        [[ "$diff_rc" -eq 2 ]] && { log_error "event=allow reason=diff_error tool=$TOOL_NAME file=$FILE_PATH"; exit 0; }

        UNCHANGED_LINES=$(printf '%s' "$diff_out" | wc -c)
        RETAINED_PERCENT=$((UNCHANGED_LINES * 100 / OLD_LINES))
        CHANGED_PERCENT=$((100 - RETAINED_PERCENT))

        DETAILS="File: ${FILE_PATH}
Existing size: $OLD_LINES lines, $OLD_BYTES bytes
New size: $NEW_LINES lines, $NEW_BYTES bytes
Estimated change: ${CHANGED_PERCENT}%"

        log_debug "event=write_stats tool=$TOOL_NAME file=$FILE_PATH old_lines=$OLD_LINES new_lines=$NEW_LINES changed_percent=$CHANGED_PERCENT"

        if [[ "$CHANGED_PERCENT" -gt "$THRESHOLD" ]]; then
            report_large_edit "$CHANGED_PERCENT" "write" "$DETAILS"
        elif [[ "$CHANGED_PERCENT" -gt "$WARN_THRESHOLD" ]]; then
            report_warning "$CHANGED_PERCENT" "write"
        fi
        ;;

    Edit)
        OLD_STRING=$(echo "$INPUT" | jq -r '.tool_input.old_string // empty')
        [[ -z "$OLD_STRING" ]] && { log_debug "event=allow reason=empty_old_string tool=$TOOL_NAME file=$FILE_PATH"; exit 0; }

        OLD_STRING_BYTES=${#OLD_STRING}
        CHANGE_PERCENT=$((OLD_STRING_BYTES * 100 / OLD_BYTES))

        DETAILS="File: ${FILE_PATH}
Replacing: $(echo "$OLD_STRING" | wc -l) lines ($OLD_STRING_BYTES bytes)
Percentage of file: ${CHANGE_PERCENT}%"

        log_debug "event=edit_stats tool=$TOOL_NAME file=$FILE_PATH old_bytes=$OLD_BYTES replaced_bytes=$OLD_STRING_BYTES changed_percent=$CHANGE_PERCENT"

        if [[ "$CHANGE_PERCENT" -gt "$THRESHOLD" ]]; then
            report_large_edit "$CHANGE_PERCENT" "edit" "$DETAILS"
        elif [[ "$CHANGE_PERCENT" -gt "$WARN_THRESHOLD" ]]; then
            report_warning "$CHANGE_PERCENT" "edit"
        fi
        ;;

    MultiEdit)
        EDIT_COUNT=$(echo "$INPUT" | jq -r '.tool_input.edits | length // 0')
        [[ "$EDIT_COUNT" -le 0 ]] && { log_debug "event=allow reason=empty_edits tool=$TOOL_NAME file=$FILE_PATH"; exit 0; }

        TOTAL_OLD_BYTES=0
        for ((i=0; i<EDIT_COUNT; i++)); do
            s=$(echo "$INPUT" | jq -r ".tool_input.edits[$i].old_string // empty")
            TOTAL_OLD_BYTES=$((TOTAL_OLD_BYTES + ${#s}))
        done

        CHANGE_PERCENT=$((TOTAL_OLD_BYTES * 100 / OLD_BYTES))

        DETAILS="File: ${FILE_PATH}
Number of edits: $EDIT_COUNT
Total bytes being replaced: $TOTAL_OLD_BYTES
Percentage of file: ${CHANGE_PERCENT}%"

        log_debug "event=multiedit_stats tool=$TOOL_NAME file=$FILE_PATH edit_count=$EDIT_COUNT replaced_bytes=$TOTAL_OLD_BYTES changed_percent=$CHANGE_PERCENT"

        if [[ "$CHANGE_PERCENT" -gt "$THRESHOLD" ]]; then
            report_large_edit "$CHANGE_PERCENT" "multi-edit" "$DETAILS"
        elif [[ "$CHANGE_PERCENT" -gt "$WARN_THRESHOLD" ]]; then
            report_warning "$CHANGE_PERCENT" "multi-edit"
        fi
        ;;

    *)
        log_debug "event=allow reason=unknown_tool tool=$TOOL_NAME file=$FILE_PATH"
        exit 0
        ;;
esac

exit 0
