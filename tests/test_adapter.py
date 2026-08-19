"""Tests for T7: the adapter interface contract.

The adapter is the pluggable boundary between the review engine and a host
CLI (codex, agy, grok, claude - T8-T11). T7 pins the contract before four of
them exist: `review(packet, mode, thread_id)` returns (verdict, thread_id,
raw); empty output is a hard typed failure, never repaired (R1); transport
failures are typed errors, not hangs or verdicts; a reviewer edit in the
checkout surfaces as IntegrityError, which the orchestrator can never record
as an accepted review; and each host ships a tool allowlist as config (R6) -
defence in depth only, since shell access can always write (R7).

A scripted fake adapter exercises every path with no network call.
"""

import json
from pathlib import Path

import pytest

from review.adapter import (
    AdapterProcessError,
    ReviewerAdapter,
    ThreadContinuityError,
    conduct_review,
    tool_policy,
)
from review.packet import PacketTooLargeError
from review.verdict import EmptyOutputError, MissingVerdictBlockError
from review.worktree import IntegrityError
from tests.conftest import git


def verdict_text(verdict: str = "ACCEPTED", round_number: int = 1) -> str:
    """Reviewer output ending in one valid fenced verdict block."""
    block = json.dumps(
        {
            "schema_version": 1,
            "verdict": verdict,
            "round": round_number,
            "blockers": [],
            "non_blockers": [],
        }
    )
    return f"The work is sound.\n\n```json\n{block}\n```\n"


class FakeAdapter(ReviewerAdapter):
    """Scripted adapter: pops one canned output (or exception) per send.

    `thread_ids`, when given, scripts the id returned by each send
    independently of the id that was requested - a well-behaved echo would
    mask a backend that silently drops or switches threads.
    """

    def __init__(self, outputs: list, on_send=None, thread_ids=None) -> None:
        self.sent: list[tuple[str, str | None]] = []
        self.outputs = list(outputs)
        self.on_send = on_send
        self.thread_ids = list(thread_ids) if thread_ids is not None else None

    def _send(self, prompt: str, worktree: Path, thread_id: str | None) -> tuple[str, str]:
        self.sent.append((prompt, thread_id))
        if self.on_send is not None:
            self.on_send(worktree)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if self.thread_ids is not None:
            return output, self.thread_ids.pop(0)
        return output, thread_id or "thread-fake-1"


def test_review_round_returns_verdict_thread_and_raw(dirty_repo: Path) -> None:
    """The contract tuple: parsed verdict, thread id, and the raw output."""
    fake = FakeAdapter([verdict_text()])
    result = conduct_review(dirty_repo, fake, "wip: bump the value")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.thread_id == "thread-fake-1"
    assert "```json" in result.raw


def test_reviewer_prompt_is_the_rendered_packet(dirty_repo: Path) -> None:
    """The reviewer sees the T6 packet: diff, TODO ref, the lot."""
    fake = FakeAdapter([verdict_text()])
    conduct_review(dirty_repo, fake, "wip: packet in prompt", todo_ref="T7")
    prompt, thread_id = fake.sent[0]
    assert thread_id is None
    assert "## Diff vs HEAD" in prompt
    assert "VALUE = 2" in prompt
    assert "T7" in prompt


def test_resume_reuses_the_reviewer_thread(dirty_repo: Path) -> None:
    """D1: a re-review goes back to the existing reviewer thread."""
    fake = FakeAdapter([verdict_text(round_number=2)])
    result = conduct_review(
        dirty_repo,
        fake,
        "wip: round two",
        mode="resume",
        thread_id="thread-prior",
    )
    assert fake.sent[0][1] == "thread-prior"
    assert result.thread_id == "thread-prior"


def test_resume_on_a_switched_thread_is_rejected(dirty_repo: Path) -> None:
    """D1: a backend that answers a resume on a different thread has
    silently restarted the review without the prior findings."""
    fake = FakeAdapter([verdict_text(round_number=2)], thread_ids=["thread-hijacked"])
    with pytest.raises(ThreadContinuityError, match="thread-hijacked"):
        conduct_review(
            dirty_repo,
            fake,
            "wip: switched thread",
            mode="resume",
            thread_id="thread-prior",
        )


def test_new_review_must_come_back_with_a_thread_id(dirty_repo: Path) -> None:
    """A review without a thread id can never be resumed; hard failure."""
    fake = FakeAdapter([verdict_text()], thread_ids=[""])
    with pytest.raises(ThreadContinuityError, match="thread id"):
        conduct_review(dirty_repo, fake, "wip: no thread returned")


def test_thread_drift_during_repair_is_rejected(dirty_repo: Path) -> None:
    """The repair round must stay on the review's thread, not start one."""
    fake = FakeAdapter(
        ["I forgot the verdict block.", verdict_text()],
        thread_ids=["thread-fake-1", "thread-drifted"],
    )
    with pytest.raises(ThreadContinuityError, match="thread-drifted"):
        conduct_review(dirty_repo, fake, "wip: drifting repair")


def test_new_mode_rejects_a_stale_thread_id(dirty_repo: Path) -> None:
    """A fresh review on an old thread would leak prior context."""
    fake = FakeAdapter([verdict_text()])
    with pytest.raises(ValueError, match="thread"):
        conduct_review(dirty_repo, fake, "wip: confused", mode="new", thread_id="thread-prior")
    assert fake.sent == []


def test_resume_requires_a_thread_id(dirty_repo: Path) -> None:
    """Resuming nothing is a caller bug, not a fresh review."""
    fake = FakeAdapter([verdict_text()])
    with pytest.raises(ValueError, match="thread"):
        conduct_review(dirty_repo, fake, "wip: no thread", mode="resume")
    assert fake.sent == []


def test_unknown_mode_is_rejected(dirty_repo: Path) -> None:
    """Only new and resume exist; anything else fails fast."""
    fake = FakeAdapter([verdict_text()])
    with pytest.raises(ValueError, match="mode"):
        conduct_review(dirty_repo, fake, "wip: bad mode", mode="rerun")
    assert fake.sent == []


def test_reviewer_edit_is_an_integrity_error_not_a_verdict(dirty_repo: Path) -> None:
    """R7: an ACCEPTED verdict from a tampering reviewer must not escape."""

    def tamper(worktree: Path) -> None:
        (worktree / "src.py").write_text("VALUE = 999\n")

    fake = FakeAdapter([verdict_text()], on_send=tamper)
    with pytest.raises(IntegrityError, match="src.py"):
        conduct_review(dirty_repo, fake, "wip: tampering reviewer")


def test_empty_output_is_a_typed_error_never_repaired(dirty_repo: Path) -> None:
    """R1: a dead adapter is a hard failure; no repair round is attempted."""
    fake = FakeAdapter(["", verdict_text()])
    with pytest.raises(EmptyOutputError):
        conduct_review(dirty_repo, fake, "wip: silence")
    assert len(fake.sent) == 1


def test_transport_failure_is_typed_and_leaves_no_worktree(dirty_repo: Path) -> None:
    """A dying backend surfaces as a typed error and cleans up after itself."""
    fake = FakeAdapter([AdapterProcessError("backend exited 1")])
    with pytest.raises(AdapterProcessError, match="exited"):
        conduct_review(dirty_repo, fake, "wip: dead backend")
    assert "jeltz-review-" not in git(dirty_repo, "worktree", "list")


def test_one_repair_round_stays_on_the_same_thread(dirty_repo: Path) -> None:
    """A missing verdict block gets exactly one same-thread repair (R2)."""
    fake = FakeAdapter(["I forgot the verdict block.", verdict_text()])
    result = conduct_review(dirty_repo, fake, "wip: repairable")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert len(fake.sent) == 2
    repair_prompt, repair_thread = fake.sent[1]
    assert repair_thread == "thread-fake-1"
    assert "fenced JSON" in repair_prompt
    assert "```json" in result.raw


def test_a_failed_repair_escalates(dirty_repo: Path) -> None:
    """The second bad output raises; there is no third attempt."""
    fake = FakeAdapter(["no block here.", "still no block."])
    with pytest.raises(MissingVerdictBlockError):
        conduct_review(dirty_repo, fake, "wip: unrepairable")
    assert len(fake.sent) == 2


def test_oversized_packet_never_reaches_the_adapter(dirty_repo: Path) -> None:
    """Termination condition 4 fires before any reviewer is contacted."""
    fake = FakeAdapter([verdict_text()])
    with pytest.raises(PacketTooLargeError):
        conduct_review(dirty_repo, fake, "wip: huge", size_ceiling=10)
    assert fake.sent == []


def test_tool_allowlists_ship_for_every_host() -> None:
    """R6: each host gets a non-empty, non-contradictory tool policy."""
    for host in ("claude", "codex", "agy", "grok"):
        policy = tool_policy(host)
        assert policy.allow, host
        assert policy.deny, host
        assert not set(policy.allow) & set(policy.deny), host


def test_edit_tools_are_denied_per_host() -> None:
    """The allowlist names each host's edit tools as denied (defence in depth)."""
    assert {"Edit", "Write"} <= set(tool_policy("claude").deny)
    assert {"Edit", "Write", "search_replace"} <= set(tool_policy("grok").deny)
    assert "apply_patch" in tool_policy("codex").deny
    assert {"edit_file", "write_to_file"} <= set(tool_policy("agy").deny)


def test_unknown_host_is_rejected() -> None:
    """A typo'd host name fails fast instead of reviewing with no policy."""
    with pytest.raises(KeyError, match="mush"):
        tool_policy("mush")
