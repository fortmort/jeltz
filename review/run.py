"""One-round review orchestrator behind ``review/run.sh`` (T12).

One invocation runs one round: build the packet, materialize the disposable
worktree, dispatch to the selected reviewer adapter (codex by default, D4),
parse the structured verdict, and record machine state in
``.jeltz/review/state.json`` while the human-readable review goes to stdout.
Exit codes are the protocol the gate (T14) and tdd-phase-loop (T20) consume:
0 accepted, 10 requires changes, 20 escalate to a human. Escalation is the
T13 policy engine's call (section 4.2 termination conditions), and every
exit 20 leaves a dossier at ``.jeltz/review/escalation.md``; the round that
triggered it is still recorded, because the round did complete. Operational
failures exit 1 and never write state, so a failed round cannot clobber the
last valid review record.

Several agents may run in one directory (T32), so every file this module
writes is either published atomically over its final name or carries a
name no other writer can hold - and nothing is ever deleted without
proof that the process which created it has died.
"""

import argparse
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from review.adapter import (
    AdapterError,
    ReviewerAdapter,
    ReviewResult,
    conduct_review,
)
from review.agy import AgyAdapter
from review.claude import ClaudeAdapter
from review.codex import CodexAdapter
from review.escalation import (
    DOSSIER_PATH,
    Escalation,
    ResponseError,
    evaluate,
    packet_escalation,
    parse_response,
    render_dossier,
    verify_coverage,
)
from review.grok import GrokAdapter
from review.packet import DEFAULT_SIZE_CEILING, PacketTooLargeError, tree_state_hash
from review.verdict import VerdictError
from review.worktree import IntegrityError, process_alive, reap_stale_worktrees

EXIT_ACCEPTED = 0
EXIT_FAILURE = 1
EXIT_REQUIRES_CHANGES = 10
EXIT_ESCALATE = 20

STATE_VERSION = 1
STATE_PATH = Path(".jeltz") / "review" / "state.json"

# `state.json.<pid>.<uuid>.tmp`. The uuid makes the name this write's
# alone; the pid names the process that owns it, which is what lets
# cleanup prove a leftover was abandoned instead of guessing from age.
_STATE_TEMP_NAME = re.compile(rf"{re.escape(STATE_PATH.name)}\.(?P<pid>\d+)\.[0-9a-f]{{32}}\.tmp")

_ADAPTERS = {
    "agy": AgyAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "grok": GrokAdapter,
}

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the orchestrator command line.

    Raises:
        SystemExit: On usage errors (argparse convention, exit code 2).
    """
    parser = argparse.ArgumentParser(
        prog="review/run.sh",
        description="Run one skeptical-review round against the working tree.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--new", action="store_true", help="open a fresh review thread")
    mode.add_argument(
        "--resume",
        action="store_true",
        help="re-review on the thread recorded in review state",
    )
    parser.add_argument(
        "--backend",
        choices=sorted(_ADAPTERS),
        help="reviewer backend for --new (default codex)",
    )
    parser.add_argument("--repo", default=".", help="repository to review")
    wip = parser.add_mutually_exclusive_group()
    wip.add_argument(
        "--wip-message",
        default="WIP under review",
        help="WIP message standing in for a commit message",
    )
    wip.add_argument(
        "--wip-message-file",
        help="file whose contents are the WIP message, byte-for-byte - the "
        "safe transport for real commit messages, whose backticks, $(), and "
        "quotes a shell would expand or mangle if passed inline",
    )
    parser.add_argument("--todo-ref", help="TODO item under review (Q1)")
    parser.add_argument(
        "--response-file",
        help="reviewer-response output answering the round being resumed",
    )
    parser.add_argument("--verify-output", help="file holding the full `make verify` output")
    parser.add_argument(
        "--size-ceiling",
        type=int,
        default=DEFAULT_SIZE_CEILING,
        help="maximum rendered packet size in characters",
    )
    parser.add_argument(
        "--allow-api-billing",
        action="store_true",
        help="let the claude backend run with ANTHROPIC_API_KEY set (3.7)",
    )
    args = parser.parse_args(argv)
    if args.resume and args.backend:
        parser.error(
            "--backend applies to --new only; a resumed review stays on its recorded backend"
        )
    if args.response_file and not args.resume:
        parser.error(
            "--response-file applies to --resume only; "
            "a fresh review has no prior round to respond to"
        )
    return args


@dataclass(frozen=True)
class _Plan:
    """One round's resolved parameters: who reviews, on which thread."""

    mode: str
    backend: str
    thread_id: str | None
    round_number: int
    history: list[Any]
    task_ref: str | None
    prior_verdict: dict[str, Any] | None


def _plan_round(repo: Path, args: argparse.Namespace) -> _Plan | None:
    """Resolve the round from the CLI mode and any recorded state.

    A fresh review starts round 1 on the chosen backend; a resume
    continues the recorded backend and thread and extends its history.
    Returns None (after logging the reason) when there is nothing usable
    to resume - including a review that already escalated: escalation is
    terminal, and the only way onward is a human tiebreak followed by
    --new.
    """
    if not args.resume:
        return _Plan(
            mode="new",
            backend=args.backend or "codex",
            thread_id=None,
            round_number=1,
            history=[],
            task_ref=args.todo_ref,
            prior_verdict=None,
        )
    state = _load_state(repo)
    if state is None:
        logger.error(
            "no resumable review state in %s; start with --new",
            repo / STATE_PATH,
        )
        return None
    if state.get("escalated"):
        logger.error(
            "this review already escalated to a human (dossier at %s); "
            "after the tiebreak, start over with --new",
            repo / DOSSIER_PATH,
        )
        return None
    return _Plan(
        mode="resume",
        backend=state["backend"],
        thread_id=state["thread_id"],
        round_number=state["round"] + 1,
        history=state["history"],
        task_ref=args.todo_ref or state.get("task_ref"),
        prior_verdict=state["verdict"],
    )


def _make_adapter(backend: str, allow_api_billing: bool) -> ReviewerAdapter:
    """Instantiate the selected backend adapter.

    Only claude takes the API-billing opt-in (3.7); the flag is inert for
    every other backend.
    """
    if backend == "claude":
        return ClaudeAdapter(allow_api_billing=allow_api_billing)
    return _ADAPTERS[backend]()


def _load_state(repo: Path) -> dict[str, Any] | None:
    """Read resumable review state, or None if there is nothing usable."""
    try:
        data = json.loads((repo / STATE_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("backend") not in _ADAPTERS:
        return None
    if not isinstance(data.get("thread_id"), str) or not data["thread_id"]:
        return None
    if not isinstance(data.get("round"), int):
        return None
    if not isinstance(data.get("history"), list):
        return None
    if not isinstance(data.get("verdict"), dict):
        return None
    return data


def _state_temp(path: Path) -> Path:
    """A temp path for one write: unique to it, and named for its owner.

    Args:
        path: The state file the temp will be published over.

    Returns:
        A sibling path matching ``_STATE_TEMP_NAME``.
    """
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _reap_state_temps(directory: Path) -> None:
    """Delete temp files abandoned by writers that are no longer running.

    A killed writer cannot clean up after itself - hook timeouts kill
    processes outright (R5) - so the next writer does it, on the same
    terms as ``reap_stale_worktrees``: only files jeltz names this way,
    and only once the process that owns one is provably gone. A live
    agent's unpublished write and a stranger's scratch file both look
    like clutter from here, and neither is ours to delete.

    Args:
        directory: The state directory to sweep.
    """
    for leftover in sorted(directory.iterdir()):
        owner = _STATE_TEMP_NAME.fullmatch(leftover.name)
        if owner is None or process_alive(int(owner["pid"])):
            continue
        try:
            leftover.unlink()
        except OSError as exc:
            # Cleanup is a courtesy. A path that will not go quietly is
            # left exactly where it is rather than forced.
            logger.debug("leaving %s in place: %s", leftover, exc)


def write_state(repo: Path, state: dict[str, Any]) -> None:
    """Write review state atomically so readers never see a torn file.

    Shared with the stop-gate bridge (T15), which merges its denial
    marker into the same file.

    Several agents may be working in one directory (T32), so the temp
    file this write publishes from carries a name no other writer can
    hold. Under a shared name a second writer truncates the first's
    buffer and renames it away mid-write, which loses one round and
    tears the other. Creating the file with ``x`` makes that uniqueness
    proved rather than assumed: the open fails outright if anything -
    a file, a directory, a symlink - already holds the path, and nothing
    this write did not create is ever removed.

    Raises:
        OSError: If the state file cannot be written or published.

    Args:
        repo: The repository whose review state is being written.
        state: The state object to record.
    """
    path = repo / STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _reap_state_temps(path.parent)
    tmp = _state_temp(path)
    # Opened outside the try on purpose: an exclusive create that fails
    # means the path is somebody else's, and the cleanup below must only
    # ever remove a file this write actually created.
    handle = tmp.open("x")
    try:
        with handle:
            handle.write(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, path)
    finally:
        # Published or failed, this writer's temp file is this writer's
        # to remove; leaving it would make the next reap do it later.
        tmp.unlink(missing_ok=True)


def _escalate(repo: Path, escalation: Escalation, history: list[Any]) -> None:
    """Write the escalation dossier and tell the human where it is."""
    dossier = repo / DOSSIER_PATH
    dossier.parent.mkdir(parents=True, exist_ok=True)
    dossier.write_text(render_dossier(escalation, history))
    conditions = ", ".join(str(c) for c in escalation.conditions)
    logger.error(
        "escalate to a human (termination condition %s): dossier at %s",
        conditions,
        dossier,
    )


def _round_record(round_number: int, result: ReviewResult) -> dict[str, Any]:
    """One history entry: the round's verdict and per-blocker dispositions."""
    return {
        "round": round_number,
        "verdict": result.verdict["verdict"],
        "blockers": [
            {"id": b["id"], "disposition": b.get("disposition")} for b in result.verdict["blockers"]
        ],
        "costs": list(result.costs),
    }


def main(argv: list[str] | None = None) -> int:
    """Run one review round; the return value is the process exit code."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s", force=True)
    args = _parse_args(argv)
    repo = Path(args.repo).resolve()
    reap_stale_worktrees(repo)
    plan = _plan_round(repo, args)
    if plan is None:
        return EXIT_FAILURE
    response = None
    if args.response_file:
        try:
            response_text = Path(args.response_file).read_text()
        except OSError as exc:
            logger.error("cannot read --response-file: %s", exc)
            return EXIT_FAILURE
        try:
            response = parse_response(response_text)
        except ResponseError as exc:
            logger.error("unusable --response-file: %s", exc)
            return EXIT_FAILURE
        if response["round"] != plan.round_number - 1:
            logger.error(
                "response answers round %d, but this resume runs round %d",
                response["round"],
                plan.round_number,
            )
            return EXIT_FAILURE
        # argparse pins --response-file to --resume, and every resumable
        # state carries a verdict, so the prior verdict is always here.
        assert plan.prior_verdict is not None
        try:
            verify_coverage(response, plan.prior_verdict)
        except ResponseError as exc:
            logger.error("unusable --response-file: %s", exc)
            return EXIT_FAILURE
    elif (
        plan.mode == "resume"
        and plan.prior_verdict is not None
        and plan.prior_verdict["verdict"] == "REQUIRES_CHANGES"
    ):
        # Conditions 2 and 3 key on the coder's claims; a remediation
        # resume that omits them would silently disable both.
        logger.error(
            "the recorded round ended REQUIRES_CHANGES; a resumed round "
            "needs --response-file with the reviewer-response dispositions"
        )
        return EXIT_FAILURE
    verify_output = ""
    if args.verify_output:
        try:
            verify_output = Path(args.verify_output).read_text()
        except OSError as exc:
            logger.error("cannot read --verify-output: %s", exc)
            return EXIT_FAILURE
    wip_message = args.wip_message
    if args.wip_message_file:
        try:
            wip_message = Path(args.wip_message_file).read_text()
        except OSError as exc:
            logger.error("cannot read --wip-message-file: %s", exc)
            return EXIT_FAILURE
    adapter = _make_adapter(plan.backend, args.allow_api_billing)
    # Hashed before dispatch on purpose: if the developer edits the tree
    # mid-review, the recorded hash mismatches the tree and the T14 gate
    # forces a re-review. Hashing afterward would record the edited tree
    # as reviewed when the reviewer saw the older one.
    diff_hash = tree_state_hash(repo)
    try:
        result = conduct_review(
            repo,
            adapter,
            wip_message,
            todo_ref=plan.task_ref,
            verify_output=verify_output,
            mode=plan.mode,
            thread_id=plan.thread_id,
            size_ceiling=args.size_ceiling,
            expected_round=plan.round_number,
        )
    except PacketTooLargeError as exc:
        _escalate(repo, packet_escalation(str(exc)), plan.history)
        return EXIT_ESCALATE
    except (AdapterError, VerdictError, IntegrityError) as exc:
        logger.error("review round failed: %s", exc)
        return EXIT_FAILURE
    sys.stdout.write(result.raw if result.raw.endswith("\n") else result.raw + "\n")
    state = {
        "schema_version": STATE_VERSION,
        "backend": plan.backend,
        "task_ref": plan.task_ref,
        "thread_id": result.thread_id,
        "round": plan.round_number,
        "diff_hash": diff_hash,
        "verdict": result.verdict,
        "history": [*plan.history, _round_record(plan.round_number, result)],
    }
    escalation = evaluate(state, response)
    if escalation is not None:
        # The marker makes escalation terminal: _plan_round refuses to
        # resume past it, so round 4 can never launder an escalated
        # review into an acceptance. Recorded in the same atomic write
        # as the round itself.
        state["escalated"] = list(escalation.conditions)
    write_state(repo, state)
    if escalation is not None:
        _escalate(repo, escalation, state["history"])
        return EXIT_ESCALATE
    if result.verdict["verdict"] == "REQUIRES_CHANGES":
        return EXIT_REQUIRES_CHANGES
    return EXIT_ACCEPTED
