"""Claude Code Stop hook shim (T16).

Claude Code originated the Stop-hook protocol this repo gates on, and
the full gate behavior lives in the shared `review.stop_hook.gate_stop`
(top-level `{"decision": "block", "reason": ...}` on stdout, silent
allow, `stop_hook_active` pass-through, fail-open on unusable input).
Claude-Code-specific host facts:

- Of the two documented blocking mechanisms (structured JSON with
  exit 0 vs exit 2 with the reason on stderr), the gate uses the
  structured-JSON path; every exit is 0, which Claude Code treats as
  success.
- The Stop hook's default timeout is 600s - far more than the gate
  needs, since it only reads the pre-computed state file.
- Hook wiring and installation are T21's deliverable.
"""

from review.stop_hook import gate_stop as main

__all__ = ["main"]
