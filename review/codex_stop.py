"""Codex Stop hook shim (T17).

Codex speaks the Claude-Code-style Stop hook protocol (verified against
the codex hooks reference during T17): the same stdin payload core
(`cwd`, `stop_hook_active`) and the same blocking output - a top-level
`{"decision": "block", "reason": ...}` on stdout with exit 0. The full
gate behavior therefore lives in the shared
`review.stop_hook.gate_stop`; these host facts are codex-specific:

- Installation lives under codex's hook trust model: configure the hook
  at user scope (`~/.codex/hooks.json` or `[[hooks.Stop]]` in
  `~/.codex/config.toml`) and trust it once interactively via `/hooks`.
  Never pass `--dangerously-bypass-hook-trust` - it exists for
  automation that vets hook sources some other way, and bypassing trust
  is exactly the habit the gate should not teach.
- The payload adds `turn_id`, `model`, `permission_mode`,
  `last_assistant_message`, and a `transcript_path` that may be null;
  the shim tolerates and ignores them all.
"""

from review.stop_hook import CLAUDE_STYLE, gate_stop

__all__ = ["main"]


def main() -> int:
    """Run the shared gate under the Claude-Code-style Stop protocol."""
    return gate_stop(CLAUDE_STYLE)
