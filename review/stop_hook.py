"""Shared Stop-hook gate, parametrized over the hosts' stop protocols.

All four stop-capable hosts follow the same gate shape - read a JSON
payload from stdin, allow silently, deny by writing a decision object to
stdout, always exit 0, and fail open on anything unusable - but they
disagree on four points, captured by `StopProtocol`:

- which payload field marks a stop the hook already continued (Claude
  Code and codex: `stop_hook_active`; agy: a nonzero `executionNum`;
  grok: `stopHookActive`),
- where the workspace roots live (Claude Code and codex: the `cwd`
  string; grok: the `workspaceRoot` string, which grok resolves to the
  git root itself; agy: the `workspacePaths` list, every entry of
  which is gated in a single pass - ordering semantics are
  undocumented, a clean first root must not mask unreviewed changes in
  a later one, and the loop guard allows the continued stop cycle
  wholesale, so a denial must name and record every denying root at
  once, with the recovery scoped per root via `--repo` whenever the
  payload is multi-root),
- which decision word blocks the stop (Claude Code, codex, and grok:
  `block`; agy: `continue`),
- which fires are genuine stop attempts (grok's Stop event also fires
  observe-only at session end, where a recorded denial would burn the
  bridge's repeat guard without keeping the session working; the
  other hosts gate every fire, the `gate_when` default).

Claude Code defined the block-style protocol and codex adopted it
verbatim, so those two shims (`review/claude_stop.py`, T16;
`review/codex_stop.py`, T17) share the `CLAUDE_STYLE` instance defined
here; agy's shim (`review/agy_stop.py`, T18) and grok's
(`review/grok_stop.py`, T19) each build their own. The gate
asks the T15 bridge for a ruling and emits the deny decision as a
top-level `{"decision": ..., "reason": ...}` object; an allow is silent
- no output, exit 0.

Duties common to every protocol, keeping the bridge neutral:
- An already-continued stop is allowed immediately without consulting
  the bridge, so the gate can never contribute to a stop-hook loop and
  never records a denial for a stop it did not gate.
- Unusable input (unparseable, not an object, no usable workspace) fails
  open with a logged error - a crashing or blocking hook on bad input
  would trap the session (CI is the backstop, R4).

Anything host-specific (trust model, config location, extra payload
fields) is documented in the host modules, not here.
"""

import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from review.bridge import attempt_stop

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StopProtocol:
    """The three points on which a host's Stop-hook protocol varies.

    Attributes:
        loop_guard_key: Payload field that is truthy when the hook
            already forced a continuation of this stop cycle.
        workspaces: Extracts the workspace-root candidates from the
            payload - a single value or a list. The gate keeps the
            entries that are non-empty strings, gates every one of
            them, and fails open when none remain.
        deny_decision: The `decision` value that blocks the stop.
        gate_when: Whether this payload is a genuine stop attempt.
            Defaults to gating every fire; a host whose Stop event also
            fires for non-stop occasions (grok's observe-only
            session-end fire, whose decision is ignored) supplies a
            predicate so those fires are allowed silently and never
            recorded.
    """

    loop_guard_key: str
    workspaces: Callable[[dict[str, Any]], object]
    deny_decision: str
    gate_when: Callable[[dict[str, Any]], bool] = lambda _payload: True


CLAUDE_STYLE = StopProtocol(
    loop_guard_key="stop_hook_active",
    workspaces=lambda payload: payload.get("cwd"),
    deny_decision="block",
)


def gate_stop(protocol: StopProtocol) -> int:
    """Gate a stop attempt read from stdin under the given protocol.

    Args:
        protocol: The host's Stop-hook protocol.

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
    if not protocol.gate_when(payload):
        return 0
    if payload.get(protocol.loop_guard_key):
        return 0
    raw = protocol.workspaces(payload)
    candidates = raw if isinstance(raw, list) else [raw]
    workspaces = [c for c in candidates if isinstance(c, str) and c]
    if not workspaces:
        logger.error("no usable workspace in stop-hook input; failing open")
        return 0
    denials = [
        (workspace, decision.reason)
        for workspace in workspaces
        if not (decision := attempt_stop(Path(workspace))).allow
    ]
    if not denials:
        return 0
    if len(workspaces) == 1:
        reason = denials[0][1]
    else:
        # Every root is consulted (and its denial recorded) in this one
        # pass: the host's loop guard allows the continued stop cycle
        # wholesale, so a root skipped here would never be gated at all.
        # Any multi-root payload gets root-scoped guidance, even for a
        # single denial - the session's cwd may be a clean root, where
        # the unscoped commands would review the wrong repository.
        labeled = " ".join(f"[{ws}] {r}" for ws, r in denials)
        reason = (
            "the workspace roots named below need review; scope the "
            "recovery to each root by adding `--repo <root>` to every "
            f"`review/run.sh` command. {labeled}"
        )
    output = {"decision": protocol.deny_decision, "reason": reason}
    sys.stdout.write(json.dumps(output) + "\n")
    return 0
