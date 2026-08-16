"""Adapter interface between the review engine and a host CLI (T7).

The adapter is the pluggable boundary D4 demands: each host (codex, agy,
grok, claude - T8-T11) implements one `_send` primitive, and the shared
template method handles everything a host could get wrong - the positive
output assertion (R1), verdict parsing with exactly one same-thread repair
round (R2), and thread continuity for re-reviews (D1). `conduct_review`
wraps a round in the T6 worktree and integrity check, so a reviewer that
edits its checkout surfaces as `IntegrityError` - a typed error the
orchestrator can never record as an accepted review (R7). Per-host tool
allowlists ship as config (R6) but are defence in depth only.
"""

import json
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from review.packet import DEFAULT_SIZE_CEILING, Packet, build_packet
from review.verdict import parse_with_repair
from review.worktree import review_worktree, snapshot, verify_integrity

ALLOWLIST_PATH = Path(__file__).resolve().parent / "tool-allowlists.json"


class AdapterError(Exception):
    """Base for adapter transport failures (never a verdict)."""


class AdapterProcessError(AdapterError):
    """The backend process died, timed out, or exited abnormally."""


class ThreadContinuityError(AdapterError):
    """The backend broke thread continuity (D1).

    A resumed review that comes back on a different thread has silently
    restarted without the prior findings, which breaks blocker
    dispositions and thrash detection; a new review without a thread id
    can never be resumed. Both are protocol failures, never verdicts.
    """


@dataclass(frozen=True)
class ToolPolicy:
    """One host's reviewer tool allowlist and denylist (R6)."""

    allow: tuple[str, ...]
    deny: tuple[str, ...]


@dataclass(frozen=True)
class ReviewResult:
    """One review round's outcome: the T7 contract tuple.

    `costs` carries the round's per-turn backend telemetry (e.g. grok's
    total_cost_usd and usage, T10) in turn order - part of review state
    so a host-neutral writer can persist it; hosts that report nothing
    leave it empty.
    """

    verdict: dict[str, Any]
    thread_id: str
    raw: str
    costs: tuple[dict[str, Any], ...] = ()


def fence_bare_verdict(raw: str) -> str:
    """Wrap a bare schema-constrained verdict in the fence T4 parses.

    Hosts whose native schema enforcement emits the verdict as a bare
    JSON object (codex --output-schema, grok --json-schema) share this
    normalization. Output already carrying prose or a fence is returned
    untouched; only a message that is itself a verdict-shaped JSON object
    gets wrapped.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(data, dict) and "schema_version" in data:
        return f"```json\n{raw}\n```\n"
    return raw


def run_backend(
    host: str, argv: list[str], worktree: Path, timeout: float
) -> tuple[str, str]:
    """Execute a reviewer backend CLI, mapping every transport failure to
    a typed error (D6: every adapter is a subprocess transport).

    Args:
        host: The host name, used in error messages.
        argv: Full command line; argv[0] is the configured binary.
        worktree: The review checkout; the backend runs with it as cwd
            and /dev/null as stdin so it can never stall on the caller's.
        timeout: Seconds before a hung backend is killed.

    Returns:
        The process's stdout and stderr.

    Raises:
        AdapterProcessError: If the binary cannot be spawned, exceeds the
            timeout, or exits nonzero (stderr carried in the message).
    """
    try:
        proc = subprocess.run(
            argv,
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except OSError as exc:
        # FileNotFoundError, PermissionError, and every other spawn
        # failure: the binary never ran, so this is transport, not
        # verdict (T7 typed-error contract).
        raise AdapterProcessError(
            f"{host} could not be spawned ({argv[0]}): {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdapterProcessError(
            f"{host} timed out after {timeout}s and was killed"
        ) from exc
    if proc.returncode != 0:
        raise AdapterProcessError(
            f"{host} exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout, proc.stderr


def telemetry(data: dict[str, Any]) -> dict[str, Any]:
    """Collect the per-turn cost/usage fields a backend envelope reports.

    Hosts whose envelopes carry billing telemetry (grok, claude) share
    this collector; fields the envelope omits contribute nothing, so a
    telemetry-free envelope yields an empty dict and no cost entry.
    """
    return {
        key: value
        for key in ("total_cost_usd", "usage")
        if (value := data.get(key)) is not None
    }


def tool_policy(host: str) -> ToolPolicy:
    """Load the shipped tool policy for one reviewer host.

    Args:
        host: One of the four supported host names.

    Returns:
        The host's allow and deny tool lists.

    Raises:
        KeyError: If the host has no shipped policy.
    """
    hosts = json.loads(ALLOWLIST_PATH.read_text())["hosts"]
    if host not in hosts:
        raise KeyError(f"no shipped tool policy for reviewer host: {host}")
    entry = hosts[host]
    return ToolPolicy(allow=tuple(entry["allow"]), deny=tuple(entry["deny"]))


class ReviewerAdapter(ABC):
    """Base class every host adapter (T8-T11) implements.

    Subclasses provide only `_send`; the `review` template method owns the
    contract so no adapter can skip the positive-output assertion or the
    repair protocol.
    """

    _round_costs: list[dict[str, Any]]

    def _record_cost(self, entry: dict[str, Any]) -> None:
        """Record one backend turn's cost/usage telemetry for the round.

        Hosts that report telemetry (grok, T10) call this from `_send`;
        the template method attaches the round's entries to its
        ReviewResult so they enter review state with the verdict.
        """
        self._round_costs.append(entry)

    @abstractmethod
    def _send(
        self, prompt: str, worktree: Path, thread_id: str | None
    ) -> tuple[str, str]:
        """Send one prompt to the reviewer and return its output.

        Args:
            prompt: The text to send.
            worktree: The review checkout the reviewer runs against.
            thread_id: Existing reviewer thread to continue, or None to
                open a new one.

        Returns:
            The reviewer's raw output and the thread id it ran on.

        Raises:
            AdapterError: On any transport failure.
        """

    def review(
        self,
        packet: Packet,
        worktree: Path,
        mode: str = "new",
        thread_id: str | None = None,
        expected_round: int | None = None,
    ) -> ReviewResult:
        """Run one review round against the packet.

        Args:
            packet: The T6 review packet; its rendering is the prompt.
            worktree: The review checkout the reviewer runs against.
            mode: "new" opens a fresh reviewer thread; "resume" continues
                an existing one for a re-review (D1).
            thread_id: Required for "resume", forbidden for "new".
            expected_round: When given, the round the verdict must
                declare - the declared round decides whether dispositions
                may deactivate blockers, so it is verified against the
                orchestrator's count, not trusted (T12). A mismatch gets
                the single same-thread repair like any verdict defect.

        Returns:
            The validated verdict, the thread id for later resumes, and
            the raw output the verdict was parsed from.

        Raises:
            ValueError: On an unknown mode or a mode/thread_id mismatch.
            ThreadContinuityError: If a resume (or its repair round) came
                back on a different thread, or any review came back with
                no thread id (D1: the backend's answer is verified, not
                trusted).
            VerdictError: If output is empty (R1, never repaired) or still
                invalid after the single repair round (R2).
            AdapterError: On any transport failure.
        """
        if mode not in ("new", "resume"):
            raise ValueError(f"unknown review mode: {mode}")
        if mode == "resume" and thread_id is None:
            raise ValueError("resume mode requires a thread id")
        if mode == "new" and thread_id is not None:
            raise ValueError("new mode must not carry a thread id")
        self._round_costs = []
        raw, tid = self._send(packet.render(), worktree, thread_id)
        if mode == "resume" and tid != thread_id:
            raise ThreadContinuityError(
                f"resume on thread {thread_id} came back on {tid or '(none)'}"
            )
        if not tid:
            raise ThreadContinuityError("reviewer returned no thread id")
        repaired: list[str] = []

        def rerun(instruction: str) -> str:
            text, rerun_tid = self._send(instruction, worktree, tid)
            if rerun_tid != tid:
                raise ThreadContinuityError(
                    f"repair drifted from thread {tid} to {rerun_tid or '(none)'}"
                )
            repaired.append(text)
            return text

        verdict = parse_with_repair(raw, rerun, expected_round=expected_round)
        return ReviewResult(
            verdict=verdict,
            thread_id=tid,
            raw=repaired[-1] if repaired else raw,
            costs=tuple(self._round_costs),
        )


def conduct_review(
    repo: Path,
    adapter: ReviewerAdapter,
    wip_message: str,
    todo_ref: str | None = None,
    verify_output: str = "",
    mode: str = "new",
    thread_id: str | None = None,
    size_ceiling: int = DEFAULT_SIZE_CEILING,
    expected_round: int | None = None,
) -> ReviewResult:
    """Run one full review round: packet, worktree, adapter, integrity.

    Args:
        repo: The developer's repository.
        adapter: The reviewer backend.
        wip_message: WIP message standing in for a commit message.
        todo_ref: Optional TODO item reference (Q1).
        verify_output: Full `make verify` output; the packet keeps its tail.
        mode: "new" or "resume" (D1).
        thread_id: Reviewer thread to resume, when mode is "resume".
        size_ceiling: Maximum rendered packet size in characters.
        expected_round: When given, the round the verdict must declare
            (verified, not trusted - T12).

    Returns:
        The round's result; only reachable when the integrity check passed.

    Raises:
        PacketTooLargeError: Before any reviewer contact (condition 4).
        IntegrityError: If the reviewer mutated its checkout - raised even
            over an accepting verdict, which is discarded (R7).
        VerdictError: On empty or unrepairable reviewer output.
        AdapterError: On any transport failure.
        ValueError: On an unknown mode or a mode/thread_id mismatch.
    """
    packet = build_packet(
        repo,
        wip_message,
        todo_ref=todo_ref,
        verify_output=verify_output,
        size_ceiling=size_ceiling,
    )
    with review_worktree(repo, wip_message) as worktree:
        before = snapshot(worktree)
        result = adapter.review(
            packet,
            worktree,
            mode=mode,
            thread_id=thread_id,
            expected_round=expected_round,
        )
        verify_integrity(worktree, before)
    return result
