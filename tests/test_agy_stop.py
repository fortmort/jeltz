"""Tests for T18: the antigravity Stop hook shim.

Antigravity's Stop hook contract differs from the Claude-Code-style
protocol on both sides of the pipe (verified live against agy 1.1.13
during T18, corroborating the vendor hooks reference): the stdin payload
is camelCase and carries `workspacePaths` (a list - the first entry is
the workspace root) instead of `cwd`, plus `executionNum`,
`terminationReason`, `fullyIdle`, `conversationId`, `transcriptPath`,
`artifactDirectoryPath`, `modelName`, and `error`; a denial is
`{"decision": "continue", "reason": ...}` on stdout (continue = keep
working, i.e. block the stop), and silence allows the stop.

These tests pin agy's host contract independently of the other shims:
- A denial is the documented continue schema carrying the recovery
  instruction as the reason; the reason reaches the model (verified
  live).
- `executionNum` is the loop guard: it counts Stop-hook firings within
  the same stop cycle - 0 on the first attempt, incrementing on each
  forced continuation, and resetting to 0 for each independent stop
  (verified live, including by resuming a conversation whose previous
  stop had reached 1 and observing the next stop cycle start at 0
  again). A nonzero `executionNum` means the hook already forced a
  continuation of this very stop, so the shim allows immediately and
  consults nothing - it must never contribute to a stop-hook loop, and
  it must not record a denial for a stop it never gated.
- Every mounted workspace root in `workspacePaths` is gated: ordering
  semantics are undocumented, so a clean first root must not mask
  unreviewed changes in a later one.
- Never block twice for the same tree: the bridge's repeat-denial guard
  reaches the host through the shim.
- Fail open on unusable input: unparseable stdin or a payload without a
  usable workspace path allows the stop with a logged error (CI is the
  backstop, R4).
"""

import io
import json
import logging
from pathlib import Path

import pytest

from review.agy_stop import main
from review.packet import tree_state_hash
from tests.conftest import git
from tests.test_run import events, install_fake_codex, run_main, verdict_obj


def make_repo(path: Path) -> Path:
    """A clean repo at path: one commit of source and docs."""
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    (path / "src.py").write_text("VALUE = 1\n")
    (path / "README.md").write_text("# readme\n")
    git(path, "add", "-A")
    git(path, "commit", "-m", "chore: initial state")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clean repo: one commit of source and docs, nothing modified."""
    return make_repo(tmp_path / "repo")


def run_hook(monkeypatch: pytest.MonkeyPatch, payload: str) -> int:
    """Run the shim's main() with the given text on stdin."""
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    return main()


def stop_payload(repo: Path, *, execution_num: int = 0) -> str:
    """An agy Stop-hook input payload for the given repo.

    Mirrors a live capture from agy 1.1.13: camelCase fields,
    `workspacePaths` as a list, and the agy-specific metadata
    (`conversationId`, `terminationReason`, `fullyIdle`, ...) so every
    test also pins that the shim tolerates them.
    """
    return json.dumps(
        {
            "artifactDirectoryPath": "/home/user/.gemini/antigravity-cli/brain/c-1",
            "conversationId": "c-1",
            "error": "",
            "executionNum": execution_num,
            "fullyIdle": True,
            "modelName": "gemini-3.1-pro-low",
            "terminationReason": "NO_TOOL_CALL",
            "transcriptPath": "/home/user/.gemini/antigravity-cli/brain/c-1/t.jsonl",
            "workspacePaths": [str(repo)],
        }
    )


def read_state(repo: Path) -> dict:
    return json.loads((repo / ".jeltz" / "review" / "state.json").read_text())


def test_allowed_stop_is_silent(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean tree stops without output: silence plus exit 0 is proceed."""
    assert run_hook(monkeypatch, stop_payload(repo)) == 0
    assert capsys.readouterr().out == ""
    assert not (repo / ".jeltz").exists()


def test_denied_stop_emits_the_documented_decision(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A denial is agy's documented Stop schema: continue plus reason."""
    (repo / "src.py").write_text("VALUE = 2\n")
    assert run_hook(monkeypatch, stop_payload(repo)) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"decision", "reason"}
    assert output["decision"] == "continue"
    assert "no review record" in output["reason"]
    assert "review/run.sh --new" in output["reason"]


def test_denial_is_recorded_through_the_bridge(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shim's denial arms the bridge's repeat guard for this tree."""
    (repo / "src.py").write_text("VALUE = 2\n")
    run_hook(monkeypatch, stop_payload(repo))
    assert read_state(repo)["denied_hash"] == tree_state_hash(repo)


def test_repeat_execution_allows_without_gating(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stop the hook already continued once is never re-gated.

    `executionNum` > 0 is agy's signal that this stop cycle already
    went through the hook; the shim allows immediately and records
    nothing. Safe because the counter resets to 0 for each independent
    stop, proven live by a resumed-conversation probe (agy 1.1.13).
    """
    (repo / "src.py").write_text("VALUE = 2\n")
    assert run_hook(monkeypatch, stop_payload(repo, execution_num=1)) == 0
    assert capsys.readouterr().out == ""
    assert not (repo / ".jeltz").exists()


def test_second_stop_for_same_tree_is_not_blocked(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The bridge's one-denial-per-tree guard reaches the host."""
    (repo / "src.py").write_text("VALUE = 2\n")
    run_hook(monkeypatch, stop_payload(repo))
    capsys.readouterr()
    assert run_hook(monkeypatch, stop_payload(repo)) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("payload", ["not json at all", "[1, 2]"])
def test_unusable_input_fails_open(
    payload: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Input the shim cannot parse allows the stop with a logged error."""
    with caplog.at_level(logging.ERROR):
        assert run_hook(monkeypatch, payload) == 0
    assert capsys.readouterr().out == ""
    assert "failing open" in caplog.text


def test_missing_workspace_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A payload without a usable workspace path allows with a logged error."""
    payload = json.dumps(
        {"terminationReason": "NO_TOOL_CALL", "executionNum": 0, "workspacePaths": []}
    )
    with caplog.at_level(logging.ERROR):
        assert run_hook(monkeypatch, payload) == 0
    assert capsys.readouterr().out == ""
    assert "failing open" in caplog.text


def test_changes_in_a_later_workspace_are_gated(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dirty repo anywhere in workspacePaths denies the stop, scoped.

    `workspacePaths` carries every mounted root (agy `--add-dir`) and
    its ordering semantics are undocumented, so the shim gates all of
    them - a clean first root must not mask unreviewed changes in a
    later one. The reason must name the denying root and scope the
    recovery to it with `--repo`: the session's cwd is typically the
    clean primary root, where the unscoped literal command would review
    the wrong repository.
    """
    other = make_repo(tmp_path / "other")
    (other / "src.py").write_text("VALUE = 2\n")
    payload = json.loads(stop_payload(repo))
    payload["workspacePaths"] = [str(repo), str(other)]
    assert run_hook(monkeypatch, json.dumps(payload)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "continue"
    assert "review/run.sh --new" in output["reason"]
    assert str(other) in output["reason"]
    assert "--repo" in output["reason"]
    assert read_state(other)["denied_hash"] == tree_state_hash(other)
    assert not (repo / ".jeltz").exists()
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj("ACCEPTED"))}])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(other)]) == 0
    capsys.readouterr()
    assert run_hook(monkeypatch, json.dumps(payload)) == 0
    assert capsys.readouterr().out == ""


def test_all_dirty_workspaces_are_denied_in_one_pass(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every dirty root is denied and recorded on the first stop.

    The executionNum guard allows the continued stop cycle wholesale,
    so the single denial must name every denying root and arm the
    bridge's repeat guard for each of them at once - a later dirty root
    must not need a stop cycle of its own to be gated.
    """
    other = make_repo(tmp_path / "other")
    (repo / "src.py").write_text("VALUE = 2\n")
    (other / "src.py").write_text("VALUE = 2\n")
    payload = json.loads(stop_payload(repo))
    payload["workspacePaths"] = [str(repo), str(other)]
    assert run_hook(monkeypatch, json.dumps(payload)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "continue"
    assert str(repo) in output["reason"]
    assert str(other) in output["reason"]
    assert "--repo" in output["reason"]
    assert "review/run.sh --new" in output["reason"]
    assert read_state(repo)["denied_hash"] == tree_state_hash(repo)
    assert read_state(other)["denied_hash"] == tree_state_hash(other)


def test_two_dirty_roots_recover_through_one_stop_cycle(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T18 acceptance for multi-root: the full lifecycle of one cycle.

    Both roots are denied together on the first attempt; the continued
    stop (executionNum 1) is allowed wholesale by the loop guard - safe
    only because both roots already hold their denial record; the
    session recovers each root with the literal command; the next
    independent stop is silent.
    """
    other = make_repo(tmp_path / "other")
    (repo / "src.py").write_text("VALUE = 2\n")
    (other / "src.py").write_text("VALUE = 2\n")

    def payload(execution_num: int) -> str:
        data = json.loads(stop_payload(repo, execution_num=execution_num))
        data["workspacePaths"] = [str(repo), str(other)]
        return json.dumps(data)

    assert run_hook(monkeypatch, payload(0)) == 0
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert "review/run.sh --new" in reason
    assert run_hook(monkeypatch, payload(1)) == 0
    assert capsys.readouterr().out == ""
    accepted = {"stdout": events(verdict_obj("ACCEPTED"))}
    home = install_fake_codex(tmp_path, [accepted, accepted])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(repo)]) == 0
    assert run_main(monkeypatch, home, ["--new", "--repo", str(other)]) == 0
    capsys.readouterr()
    assert run_hook(monkeypatch, payload(0)) == 0
    assert capsys.readouterr().out == ""


def test_unusable_workspace_entries_are_skipped(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Entries that are not non-empty strings are skipped, not fatal."""
    (repo / "src.py").write_text("VALUE = 2\n")
    payload = json.loads(stop_payload(repo))
    payload["workspacePaths"] = [None, "", str(repo)]
    assert run_hook(monkeypatch, json.dumps(payload)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "continue"
    assert "review/run.sh --new" in output["reason"]


def test_denied_session_reaches_acceptance_and_stops(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T18 acceptance: the T15 loop works end to end through the shim.

    An agy session edits tracked source and tries to stop: the shim
    denies once with the instruction, the session runs the literal
    command against a scripted backend, reaches acceptance, and the next
    stop is silent.
    """
    (repo / "src.py").write_text("VALUE = 2\n")
    assert run_hook(monkeypatch, stop_payload(repo)) == 0
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert "review/run.sh --new" in reason
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj("ACCEPTED"))}])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(repo)]) == 0
    capsys.readouterr()
    assert run_hook(monkeypatch, stop_payload(repo)) == 0
    assert capsys.readouterr().out == ""
