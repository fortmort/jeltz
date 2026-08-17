"""Tests for T15: the stop-gate recovery bridge.

T14's gate decides allow or block; the bridge makes a denied stop actually
produce a review. `attempt_stop(repo)` is what the host shims (T16-T19)
call: allows pass through untouched, while a denial carries a deterministic
recovery instruction - the literal command to run (`review/run.sh --new`),
what to do on exit 10 (apply `reviewer-response`, then
`review/run.sh --resume`), and what to do on exit 20 (stop and hand the
dossier to a human). The instruction is host-neutral prose: it must not
assume `tdd-phase-loop` is installed, because the sessions this exists for
are precisely the ones not running it.

Repeat-denial guard: the bridge records the denied diff hash in
`.jeltz/review/state.json` and never denies twice for the same hash, so a
session that ignores the instruction can stop on its second attempt with a
logged warning rather than being trapped (the gate raises the floor; CI is
the backstop, R4). The denial record must not masquerade as review state:
the orchestrator must still refuse to resume from it, and the gate must
still see "no review record". A completed review rewrites state wholesale,
which clears the marker - each new review cycle gets a fresh denial.
"""

import json
import logging
from pathlib import Path

import pytest

from review.bridge import attempt_stop
from review.gate import decide
from review.packet import tree_state_hash
from review.run import main
from tests.conftest import git
from tests.test_run import events, install_fake_codex, run_main, verdict_obj


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clean repo: one commit of source and docs, nothing modified."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "src.py").write_text("VALUE = 1\n")
    (repo / "README.md").write_text("# readme\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "chore: initial state")
    return repo


def write_review_state(repo: Path, verdict: str) -> None:
    """Record review state for the current tree as the orchestrator would."""
    state = {
        "schema_version": 1,
        "backend": "codex",
        "task_ref": None,
        "thread_id": "thread-codex-1",
        "round": 1,
        "diff_hash": tree_state_hash(repo),
        "verdict": {
            "schema_version": 1,
            "round": 1,
            "verdict": verdict,
            "blockers": [],
            "non_blockers": [],
            "summary": "recorded by the test fixture",
        },
        "history": [],
    }
    path = repo / ".jeltz" / "review" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def read_state(repo: Path) -> dict:
    return json.loads((repo / ".jeltz" / "review" / "state.json").read_text())


def test_allow_passes_through_without_writing(repo: Path) -> None:
    """An allowed stop is the gate's ruling verbatim; no state appears."""
    decision = attempt_stop(repo)
    assert decision.allow
    assert "nothing to review" in decision.reason
    assert not (repo / ".jeltz").exists()


def test_denial_carries_the_recovery_instruction(repo: Path) -> None:
    """A denial tells the session exactly how to get reviewed, per exit code."""
    (repo / "src.py").write_text("VALUE = 2\n")
    decision = attempt_stop(repo)
    assert not decision.allow
    assert "no review record" in decision.reason
    assert "review/run.sh --new" in decision.reason
    assert "exit 10" in decision.reason
    assert "reviewer-response" in decision.reason
    assert "review/run.sh --resume" in decision.reason
    assert "exit 20" in decision.reason
    assert "human" in decision.reason


def test_denial_is_recorded_for_the_denied_tree(repo: Path) -> None:
    """The denial is keyed to the exact tree hash it was issued for."""
    (repo / "src.py").write_text("VALUE = 2\n")
    attempt_stop(repo)
    assert read_state(repo)["denied_hash"] == tree_state_hash(repo)


def test_second_attempt_for_same_tree_allows_with_warning(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A session that ignores the instruction is not trapped: one denial only."""
    (repo / "src.py").write_text("VALUE = 2\n")
    assert not attempt_stop(repo).allow
    with caplog.at_level(logging.WARNING):
        decision = attempt_stop(repo)
    assert decision.allow
    assert "already denied" in decision.reason
    assert "already denied" in caplog.text


def test_further_changes_arm_a_fresh_denial(repo: Path) -> None:
    """The guard is per tree state, not per session: new edits gate again."""
    (repo / "src.py").write_text("VALUE = 2\n")
    assert not attempt_stop(repo).allow
    assert attempt_stop(repo).allow
    (repo / "src.py").write_text("VALUE = 3\n")
    decision = attempt_stop(repo)
    assert not decision.allow
    assert "review/run.sh --new" in decision.reason


def test_denial_preserves_existing_review_state(repo: Path) -> None:
    """Recording a denial must not clobber a real review record."""
    (repo / "src.py").write_text("VALUE = 2\n")
    write_review_state(repo, "REQUIRES_CHANGES")
    assert not attempt_stop(repo).allow
    state = read_state(repo)
    assert state["backend"] == "codex"
    assert state["thread_id"] == "thread-codex-1"
    assert state["verdict"]["verdict"] == "REQUIRES_CHANGES"
    assert state["denied_hash"] == tree_state_hash(repo)


def test_denial_record_is_not_review_state(repo: Path) -> None:
    """A denial-only record neither satisfies the gate nor resumes a review."""
    (repo / "src.py").write_text("VALUE = 2\n")
    attempt_stop(repo)
    assert "no review record" in decide(repo).reason
    assert main(["--resume", "--repo", str(repo)]) == 1


def test_corrupt_state_still_records_the_denial(repo: Path) -> None:
    """Unparseable state cannot crash the bridge or defeat the guard."""
    (repo / "src.py").write_text("VALUE = 2\n")
    path = repo / ".jeltz" / "review" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all")
    assert not attempt_stop(repo).allow
    assert read_state(repo)["denied_hash"] == tree_state_hash(repo)
    assert attempt_stop(repo).allow


def test_unrecordable_denial_fails_open_with_logged_error(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the marker cannot persist, the bridge must not crash or trap.

    An unrecorded denial would deny again on every attempt - a trap - and
    a crashing hook leaves the outcome to each host's error handling. The
    only posture consistent with "never deadlock" is to log the failure
    and fail open; CI (R4) is the backstop.
    """
    (repo / "src.py").write_text("VALUE = 2\n")
    (repo / ".jeltz").write_text("a file where the state dir belongs\n")
    with caplog.at_level(logging.ERROR):
        decision = attempt_stop(repo)
    assert decision.allow
    assert "failing open" in decision.reason
    assert "cannot record" in caplog.text


def test_naive_session_is_denied_once_then_reviewed_to_acceptance(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T15 acceptance: deny once, follow the instruction, stop cleanly.

    A session with no knowledge of tdd-phase-loop edits tracked source and
    tries to stop. It is denied exactly once, runs the literal command from
    the reason, reaches acceptance, and its next stop attempt is allowed -
    no deadlock, no second denial for the same hash, no unbounded loop.
    """
    (repo / "src.py").write_text("VALUE = 2\n")
    first = attempt_stop(repo)
    assert not first.allow
    assert "review/run.sh --new" in first.reason
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj("ACCEPTED"))}])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(repo)]) == 0
    second = attempt_stop(repo)
    assert second.allow
    assert "accepted" in second.reason.lower()
    assert "denied_hash" not in read_state(repo)
