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
    """One review round's outcome: the T7 contract tuple."""

    verdict: dict[str, Any]
    thread_id: str
    raw: str


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
    ) -> ReviewResult:
        """Run one review round against the packet.

        Args:
            packet: The T6 review packet; its rendering is the prompt.
            worktree: The review checkout the reviewer runs against.
            mode: "new" opens a fresh reviewer thread; "resume" continues
                an existing one for a re-review (D1).
            thread_id: Required for "resume", forbidden for "new".

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

        verdict = parse_with_repair(raw, rerun)
        return ReviewResult(
            verdict=verdict,
            thread_id=tid,
            raw=repaired[-1] if repaired else raw,
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
        result = adapter.review(packet, worktree, mode=mode, thread_id=thread_id)
        verify_integrity(worktree, before)
    return result
