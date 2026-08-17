"""Grok Stop hook shim (T19).

T19's original premise - grok has no stop-blocking hook (finding 3.5) -
was disproven live against grok 1.0.4 during T19: grok ships a
Claude-Code-compatible blocking `Stop` hook, so this shim follows the
T16-T18 shape instead of the originally specified deny-at-edit
PreToolUse gate. Grok follows the shared gate shape but varies from the
Claude-Code-style protocol, so it runs `review.stop_hook.gate_stop`
under its own `StopProtocol` instead of the shared `CLAUDE_STYLE`:

- Input is camelCase: the loop guard is `stopHookActive` (true on every
  fire after a block this turn - verified live on the continuation fire
  of a blocked stop), and the workspace root is `workspaceRoot`, which
  grok resolves to the git root even when the session's `cwd` is a
  subdirectory (trailing slash included; verified live). The payload
  adds `hookEventName`, `sessionId`, `cwd`, `timestamp`,
  `transcriptPath`, `promptId`, `permissionMode`, `reason`,
  `lastAssistantMessage`, `backgroundTasks`, and `sessionCrons`; the
  shim tolerates and ignores them all.
- Not every Stop fire is a genuine stop attempt: an observe-only Stop
  also fires at session end (`reason` `"shutdown"` or
  `"channel_closed"`; verified live), and its decision output is parsed
  but ignored. The shim gates only `reason == "end_turn"` - gating a
  session-end fire could not keep the session working, but it WOULD
  record a denial, burning the bridge's one-denial-per-tree guard for
  the next genuine stop.
- A denial is Claude's vocabulary: a top-level `{"decision": "block",
  "reason": ...}` on stdout (the reason reaches the model - verified
  live by a headless round trip); silence allows. Every exit is 0.
  Exit 2 with stderr also blocks, but the JSON path is the one shared
  with the other shims. Grok force-stops after 8 continuations per
  turn, so the shim can never trap a session even without its guards.
- Config: any `*.json` file under `.grok/hooks/` at the project root
  (requires one-time folder trust via `/hooks-trust` or launching with
  `--trust`; until trusted, project hooks are silently skipped) or
  `~/.grok/hooks/` at user scope (always trusted). `Stop` entries
  default to a 600s timeout - generous for this shim, which only reads
  the pre-computed state file (3.6).
"""

from review.stop_hook import StopProtocol, gate_stop

__all__ = ["main"]

_PROTOCOL = StopProtocol(
    loop_guard_key="stopHookActive",
    workspaces=lambda payload: payload.get("workspaceRoot"),
    deny_decision="block",
    gate_when=lambda payload: payload.get("reason") == "end_turn",
)


def main() -> int:
    """Run the shared gate under grok's Stop protocol."""
    return gate_stop(_PROTOCOL)
