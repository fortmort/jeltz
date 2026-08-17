"""Tests for T14: the host-neutral stop gate.

The gate is the enforcement point behind every host's Stop hook shim
(T16-T19): it reads `.jeltz/review/state.json`, compares the recorded diff
hash against the current tree, and decides allow or block with a reason
string. It must stay within a 30s hook budget (R5), so it only ever reads
the state file and hashes the tree - it never runs a review. R3 scope
exclusions keep false positives from killing adoption: doc-only edits,
sessions that changed nothing, and an explicit per-clone opt-out marker
(alongside the existing `claude-hook-mode` convention under the git common
dir) all allow the stop. The gate must never deadlock a session: states it
cannot gate - no git repository, no commits yet, an escalated review that
already handed control to a human - fail open with a reason, never block.
"""

import json
from pathlib import Path

import pytest

from review.gate import decide
from review.packet import tree_state_hash
from tests.conftest import git


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
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# guide\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "chore: initial state")
    return repo


def write_state(
    repo: Path,
    verdict: str = "ACCEPTED",
    *,
    diff_hash: str | None = None,
    escalated: list[int] | None = None,
) -> None:
    """Record review state as the T12/T13 orchestrator would have written it."""
    state = {
        "schema_version": 1,
        "backend": "codex",
        "task_ref": None,
        "thread_id": "thread-codex-1",
        "round": 1,
        "diff_hash": tree_state_hash(repo) if diff_hash is None else diff_hash,
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
    if escalated is not None:
        state["escalated"] = escalated
    path = repo / ".jeltz" / "review" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def test_clean_tree_allows(repo: Path) -> None:
    """R3: a session that changed nothing tracked or untracked may stop."""
    decision = decide(repo)
    assert decision.allow
    assert "nothing to review" in decision.reason


def test_doc_only_changes_allow(repo: Path) -> None:
    """R3: doc edits - tracked or untracked, by suffix or under docs/ - pass."""
    (repo / "README.md").write_text("# readme\n\nMore prose.\n")
    (repo / "notes.txt").write_text("scratch notes\n")
    (repo / "CHANGES.rst").write_text("changes\n")
    (repo / "docs" / "diagram.svg").write_text("<svg/>\n")
    decision = decide(repo)
    assert decision.allow
    assert "doc-only" in decision.reason


def test_source_change_without_review_blocks(repo: Path) -> None:
    """An unreviewed tracked source edit is exactly what the gate is for."""
    (repo / "src.py").write_text("VALUE = 2\n")
    decision = decide(repo)
    assert not decision.allow
    assert "no review record" in decision.reason


def test_untracked_source_file_blocks(repo: Path) -> None:
    """New untracked source is reviewed content (it feeds the diff hash)."""
    (repo / "new_module.py").write_text("NEW = True\n")
    decision = decide(repo)
    assert not decision.allow
    assert "no review record" in decision.reason


def test_mixed_docs_and_source_blocks(repo: Path) -> None:
    """A doc edit does not launder a source edit past the gate."""
    (repo / "README.md").write_text("# readme, expanded\n")
    (repo / "src.py").write_text("VALUE = 2\n")
    decision = decide(repo)
    assert not decision.allow


@pytest.mark.parametrize("verdict", ["ACCEPTED", "ACCEPTED_WITH_NON_BLOCKERS"])
def test_accepted_review_allows(repo: Path, verdict: str) -> None:
    """A review of exactly this tree that ended in acceptance opens the gate."""
    (repo / "src.py").write_text("VALUE = 2\n")
    write_state(repo, verdict)
    decision = decide(repo)
    assert decision.allow
    assert "accepted" in decision.reason.lower()


def test_stale_review_blocks(repo: Path) -> None:
    """Editing after the review invalidates it: the hash no longer matches."""
    (repo / "src.py").write_text("VALUE = 2\n")
    write_state(repo, "ACCEPTED")
    (repo / "src.py").write_text("VALUE = 3\n")
    decision = decide(repo)
    assert not decision.allow
    assert "changed" in decision.reason


def test_requires_changes_blocks(repo: Path) -> None:
    """A matching hash is not enough; the verdict must be an acceptance."""
    (repo / "src.py").write_text("VALUE = 2\n")
    write_state(repo, "REQUIRES_CHANGES")
    decision = decide(repo)
    assert not decision.allow
    assert "REQUIRES_CHANGES" in decision.reason


def test_escalated_review_allows_stop(repo: Path) -> None:
    """Escalation ended automation; trapping the session would deadlock it."""
    (repo / "src.py").write_text("VALUE = 2\n")
    write_state(repo, "REQUIRES_CHANGES", escalated=[3])
    decision = decide(repo)
    assert decision.allow
    assert "escalated" in decision.reason


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param(json.dumps([1, 2, 3]), id="not-an-object"),
        pytest.param(json.dumps({"verdict": {"verdict": "ACCEPTED"}}), id="no-hash"),
        pytest.param(
            json.dumps({"diff_hash": 7, "verdict": {"verdict": "ACCEPTED"}}),
            id="hash-not-a-string",
        ),
        pytest.param(json.dumps({"diff_hash": "abc"}), id="no-verdict"),
        pytest.param(
            json.dumps({"diff_hash": "abc", "verdict": "ACCEPTED"}),
            id="verdict-not-an-object",
        ),
    ],
)
def test_corrupt_state_blocks(repo: Path, text: str) -> None:
    """Unusable state is no review record; source changes stay gated."""
    (repo / "src.py").write_text("VALUE = 2\n")
    path = repo / ".jeltz" / "review" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    decision = decide(repo)
    assert not decision.allow
    assert "no review record" in decision.reason


def test_opt_out_marker_disables_gate(repo: Path) -> None:
    """A per-clone `off` marker under the git common dir bypasses the gate."""
    (repo / "src.py").write_text("VALUE = 2\n")
    marker = repo / ".git" / "info" / "jeltz-review-gate"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("off\n")
    decision = decide(repo)
    assert decision.allow
    assert "disabled" in decision.reason


def test_opt_out_marker_must_say_off(repo: Path) -> None:
    """A marker with any other content leaves the gate armed."""
    (repo / "src.py").write_text("VALUE = 2\n")
    marker = repo / ".git" / "info" / "jeltz-review-gate"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("on\n")
    decision = decide(repo)
    assert not decision.allow


def test_non_ascii_doc_paths_stay_docs(repo: Path) -> None:
    """Git C-quotes non-ASCII names; the gate must classify the real path."""
    git(repo, "config", "core.quotepath", "true")
    tracked = "caf\u00e9-notes.md"
    (repo / tracked).write_text("# notes\n")
    git(repo, "add", tracked)
    git(repo, "commit", "-m", "docs: add notes")
    (repo / tracked).write_text("# notes\n\nMore prose.\n")
    (repo / "r\u00e9sum\u00e9.md").write_text("# hello\n")
    decision = decide(repo)
    assert decision.allow
    assert "doc-only" in decision.reason


def test_non_ascii_source_after_review_blocks_as_stale(repo: Path) -> None:
    """A quoted untracked path must hash cleanly, not crash the hook."""
    git(repo, "config", "core.quotepath", "true")
    (repo / "src.py").write_text("VALUE = 2\n")
    write_state(repo, "ACCEPTED")
    (repo / "caf\u00e9.py").write_text("NEW = True\n")
    decision = decide(repo)
    assert not decision.allow
    assert "changed" in decision.reason


def test_outside_a_repository_allows(tmp_path: Path) -> None:
    """No repository means nothing to gate; blocking would deadlock."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "src.py").write_text("VALUE = 1\n")
    decision = decide(plain)
    assert decision.allow
    assert "not a git repository" in decision.reason


def test_repo_without_commits_allows(tmp_path: Path) -> None:
    """A repo with no HEAD cannot be hashed; fail open rather than trap."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "src.py").write_text("VALUE = 1\n")
    decision = decide(repo)
    assert decision.allow
    assert "cannot" in decision.reason
