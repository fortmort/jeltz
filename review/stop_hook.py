"""Shared Stop-hook gate for hosts speaking the Claude-Code-style protocol.

Claude Code defined the protocol and codex adopted it verbatim, so both
host shims (`review/claude_stop.py`, T16; `review/codex_stop.py`, T17)
delegate here. The gate reads the Stop-hook payload from stdin, asks the
T15 bridge for a ruling, and translates a denial into the documented
Stop decision schema: a top-level `{"decision": "block", "reason": ...}`
on stdout (`hookSpecificOutput` decisions belong to other events such as
PreToolUse). An allow is silent - no output, exit 0.

Duties of the protocol, keeping the bridge neutral:
- `stop_hook_active` true means the host is already continuing because
  of a Stop hook; the gate allows immediately without consulting the
  bridge, so it can never contribute to a stop-hook loop and never
  records a denial for a stop it did not gate.
- Unusable input (unparseable, not an object, no usable `cwd`) fails
  open with a logged error - a crashing or blocking hook on bad input
  would trap the session (CI is the backstop, R4).

Anything host-specific (trust model, config location, extra payload
fields) is documented in the host modules, not here.
"""

import json
import logging
import sys
from pathlib import Path

from review.bridge import attempt_stop

logger = logging.getLogger(__name__)


def gate_stop() -> int:
    """Gate a stop attempt from a Claude-Code-style Stop-hook payload.

    Returns:
        The hook's exit code: always 0, with the deny decision (if any)
        emitted as JSON on stdout.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("unusable stop-hook input (%s); failing open", exc)
        return 0
    if not isinstance(payload, dict):
        logger.error("stop-hook input is not an object; failing open")
        return 0
    if payload.get("stop_hook_active"):
        return 0
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        logger.error("no usable cwd in stop-hook input; failing open")
        return 0
    decision = attempt_stop(Path(cwd))
    if not decision.allow:
        output = {"decision": "block", "reason": decision.reason}
        sys.stdout.write(json.dumps(output) + "\n")
    return 0
