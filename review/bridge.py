"""Stop-gate recovery bridge (T15).

`attempt_stop` is the call the host Stop-hook shims (T16-T19) make. It
wraps the pure T14 gate: allows pass through untouched, and a denial is
turned into a recovery path - the reason carries the literal command to
run and what each exit code means, because the reason string is the only
channel back into the coding session. The prose is host-neutral and does
not assume `tdd-phase-loop` is installed.

Repeat-denial guard: each denial records its diff hash in
`.jeltz/review/state.json`, and the bridge never denies twice for the
same hash - a session that ignores the instruction stops on its second
attempt with a logged warning (the gate raised the floor and recorded
the skip; CI is the backstop, R4). Claude Code's `stop_hook_active`
field is host-specific and is the T16 shim's job.
"""

import json
import logging
from pathlib import Path
from typing import Any

from review.gate import GateDecision, decide
from review.packet import tree_state_hash
from review.run import STATE_PATH, write_state

logger = logging.getLogger(__name__)

RECOVERY_INSTRUCTION = (
    "Run `review/run.sh --new` to start a review. "
    "On exit 10 (changes required), apply the reviewer-response skill to "
    "the findings, then run `review/run.sh --resume --response-file "
    "<response>`. Repeat until exit 0 (accepted) or exit 20 (escalated); "
    "on exit 20, stop and hand .jeltz/review/escalation.md to a human."
)


def _read_raw_state(repo: Path) -> dict[str, Any]:
    """The state file as a dict, or an empty dict if unusable.

    Deliberately shapeless: the bridge merges the denial marker into
    whatever is there without judging it - the gate and the orchestrator
    each validate what they need.
    """
    try:
        data = json.loads((repo / STATE_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_denial(repo: Path, diff_hash: str) -> None:
    """Merge the denied hash into review state, atomically."""
    state = _read_raw_state(repo)
    state["denied_hash"] = diff_hash
    write_state(repo, state)


def attempt_stop(repo: Path) -> GateDecision:
    """Gate a stop attempt, turning a denial into a recovery path.

    Args:
        repo: The repository (or plain directory) the session worked in.

    Returns:
        The gate's ruling; a denial's reason ends with the literal
        recovery instruction, and a repeat attempt for an already-denied
        tree is allowed with a logged warning.
    """
    base = decide(repo)
    if base.allow:
        return base
    diff_hash = tree_state_hash(repo)
    if _read_raw_state(repo).get("denied_hash") == diff_hash:
        reason = (
            "this stop was already denied once for this exact tree and the "
            "instruction was not followed; allowing the stop (CI is the "
            "backstop)"
        )
        logger.warning("%s", reason)
        return GateDecision(True, reason)
    try:
        _record_denial(repo, diff_hash)
    except OSError as exc:
        # An unrecorded denial would deny again on every attempt - a trap.
        reason = (
            f"cannot record the denial ({exc}); failing open rather than "
            "trap the session (CI is the backstop)"
        )
        logger.error("%s", reason)
        return GateDecision(True, reason)
    return GateDecision(False, f"{base.reason}. {RECOVERY_INSTRUCTION}")
