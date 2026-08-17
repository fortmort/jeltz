"""Claude Code Stop hook shim (T16).

Reads the Stop-hook payload from stdin, asks the T15 bridge for a ruling,
and translates a denial into Claude Code's documented Stop decision
schema: a top-level `{"decision": "block", "reason": ...}` on stdout
(`hookSpecificOutput` decisions belong to other events such as
PreToolUse). An allow is silent - no output, exit 0.

Host-specific duties live here, keeping the bridge neutral:
- `stop_hook_active` true means Claude Code is already continuing because
  of a Stop hook; the shim allows immediately without consulting the
  bridge, so it can never contribute to a stop-hook loop and never
  records a denial for a stop it did not gate.
- Unusable input (unparseable, not an object, no usable `cwd`) fails
  open with a logged error - a crashing or blocking hook on bad input
  would trap the session (CI is the backstop, R4).
"""

import json
import logging
import sys
from pathlib import Path

from review.bridge import attempt_stop

logger = logging.getLogger(__name__)


def main() -> int:
    """Gate a Claude Code stop attempt from a Stop-hook stdin payload.

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
