"""One-round review orchestrator behind ``review/run.sh`` (T12).

One invocation runs one round: build the packet, materialize the disposable
worktree, dispatch to the selected reviewer adapter (codex by default, D4),
parse the structured verdict, and record machine state in
``.jeltz/review/state.json`` while the human-readable review goes to stdout.
Exit codes are the protocol the gate (T14) and tdd-phase-loop (T20) consume:
0 accepted, 10 requires changes, 20 escalate to a human. Operational
failures exit 1 and never write state, so a failed round cannot clobber the
last valid review record.
"""

import argparse
import json
import logging
import sys
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
from review.grok import GrokAdapter
from review.packet import DEFAULT_SIZE_CEILING, PacketTooLargeError, tree_state_hash
from review.verdict import VerdictError
from review.worktree import IntegrityError, reap_stale_worktrees

EXIT_ACCEPTED = 0
EXIT_FAILURE = 1
EXIT_REQUIRES_CHANGES = 10
EXIT_ESCALATE = 20

STATE_VERSION = 1
STATE_PATH = Path(".jeltz") / "review" / "state.json"

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
    parser.add_argument(
        "--wip-message",
        default="WIP under review",
        help="WIP message standing in for a commit message",
    )
    parser.add_argument("--todo-ref", help="TODO item under review (Q1)")
    parser.add_argument(
        "--verify-output", help="file holding the full `make verify` output"
    )
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
            "--backend applies to --new only; "
            "a resumed review stays on its recorded backend"
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


def _plan_round(repo: Path, args: argparse.Namespace) -> _Plan | None:
    """Resolve the round from the CLI mode and any recorded state.

    A fresh review starts round 1 on the chosen backend; a resume
    continues the recorded backend and thread and extends its history.
    Returns None (after logging the reason) when there is nothing usable
    to resume.
    """
    if not args.resume:
        return _Plan(
            mode="new",
            backend=args.backend or "codex",
            thread_id=None,
            round_number=1,
            history=[],
            task_ref=args.todo_ref,
        )
    state = _load_state(repo)
    if state is None:
        logger.error(
            "no resumable review state in %s; start with --new",
            repo / STATE_PATH,
        )
        return None
    return _Plan(
        mode="resume",
        backend=state["backend"],
        thread_id=state["thread_id"],
        round_number=state["round"] + 1,
        history=state["history"],
        task_ref=args.todo_ref or state.get("task_ref"),
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
    return data


def _write_state(repo: Path, state: dict[str, Any]) -> None:
    """Write review state atomically so readers never see a torn file."""
    path = repo / STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def _round_record(round_number: int, result: ReviewResult) -> dict[str, Any]:
    """One history entry: the round's verdict and per-blocker dispositions."""
    return {
        "round": round_number,
        "verdict": result.verdict["verdict"],
        "blockers": [
            {"id": b["id"], "disposition": b.get("disposition")}
            for b in result.verdict["blockers"]
        ],
        "costs": list(result.costs),
    }


def main(argv: list[str] | None = None) -> int:
    """Run one review round; the return value is the process exit code."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(message)s", force=True
    )
    args = _parse_args(argv)
    repo = Path(args.repo).resolve()
    reap_stale_worktrees(repo)
    plan = _plan_round(repo, args)
    if plan is None:
        return EXIT_FAILURE
    verify_output = ""
    if args.verify_output:
        try:
            verify_output = Path(args.verify_output).read_text()
        except OSError as exc:
            logger.error("cannot read --verify-output: %s", exc)
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
            args.wip_message,
            todo_ref=plan.task_ref,
            verify_output=verify_output,
            mode=plan.mode,
            thread_id=plan.thread_id,
            size_ceiling=args.size_ceiling,
            expected_round=plan.round_number,
        )
    except PacketTooLargeError as exc:
        logger.error("escalate to a human (termination condition 4): %s", exc)
        return EXIT_ESCALATE
    except (AdapterError, VerdictError, IntegrityError) as exc:
        logger.error("review round failed: %s", exc)
        return EXIT_FAILURE
    sys.stdout.write(result.raw if result.raw.endswith("\n") else result.raw + "\n")
    _write_state(
        repo,
        {
            "schema_version": STATE_VERSION,
            "backend": plan.backend,
            "task_ref": plan.task_ref,
            "thread_id": result.thread_id,
            "round": plan.round_number,
            "diff_hash": diff_hash,
            "verdict": result.verdict,
            "history": [*plan.history, _round_record(plan.round_number, result)],
        },
    )
    if result.verdict["verdict"] == "REQUIRES_CHANGES":
        return EXIT_REQUIRES_CHANGES
    return EXIT_ACCEPTED
