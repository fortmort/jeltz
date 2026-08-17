"""Antigravity Stop hook shim (T18).

Antigravity follows the shared gate shape but disagrees with the
Claude-Code-style protocol on every varying point (verified live against
agy 1.1.13 during T18), so this shim runs `review.stop_hook.gate_stop`
under its own `StopProtocol` instead of the shared `CLAUDE_STYLE`:

- Input is camelCase and carries `workspacePaths` (a list of every
  mounted workspace root - agy `--add-dir` mounts more than one)
  instead of `cwd`; every entry is gated in a single pass, because
  ordering semantics are undocumented, a clean first root must not
  mask unreviewed changes in a later one, and the `executionNum` guard
  allows the continued stop cycle wholesale - a denial must therefore
  name and record every denying root at once. A denial from a
  multi-root payload scopes the recovery per root (`--repo <root>` on
  every `review/run.sh` command), since the session's cwd may be a
  clean root where the unscoped commands would review the wrong
  repository. There is no `stop_hook_active`. The loop
  guard is `executionNum`, which counts Stop-hook firings within the
  same stop cycle: 0 on the first attempt, incrementing on each forced
  continuation, and resetting to 0 for each independent stop - proven
  live by resuming a conversation whose previous stop had reached
  `executionNum` 1 and observing the next stop cycle start at 0 again.
  A nonzero value therefore means the hook already continued this very
  stop.
- A denial is `{"decision": "continue", "reason": ...}` on stdout
  (continue = keep working, i.e. block the stop); the reason reaches the
  model. Silence allows the stop. Every exit is 0.
- Config: `.agents/hooks.json` at the workspace root (checked into VCS
  by design) or user-global `~/.gemini/config/hooks.json`. The default
  hook timeout is 30s - fine for this shim, which only reads the
  pre-computed state file (3.6).
- Hooks fire in print mode only with a project context (`--new-project`
  or an existing project), matching the skills sharp edge (3.4).
- The payload adds `conversationId`, `terminationReason`, `fullyIdle`,
  `transcriptPath`, `artifactDirectoryPath`, `modelName`, and `error`;
  the shim tolerates and ignores them all.
"""

from review.stop_hook import StopProtocol, gate_stop

__all__ = ["main"]

_PROTOCOL = StopProtocol(
    loop_guard_key="executionNum",
    workspaces=lambda payload: payload.get("workspacePaths"),
    deny_decision="continue",
)


def main() -> int:
    """Run the shared gate under antigravity's Stop protocol."""
    return gate_stop(_PROTOCOL)
