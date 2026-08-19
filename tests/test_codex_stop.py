"""Tests for T17: the codex Stop hook shim.

Codex speaks the Claude-Code-style Stop hook protocol (verified against
the codex hooks reference during T17): the hook reads a JSON payload from
stdin and blocks the stop with a top-level `{"decision": "block",
"reason": ...}` on stdout plus exit 0. The payload carries the same core
fields as Claude Code's (`session_id`, `cwd`, `hook_event_name`,
`stop_hook_active`) plus codex-specific extras the shim must tolerate:
`turn_id`, `model`, `permission_mode`, `last_assistant_message`, and a
`transcript_path` that may be null. Allows produce no output at all:
silence plus exit 0 is codex's "proceed".

These tests pin codex's host contract independently of the Claude shim,
so the two hosts can diverge later without silent breakage:
- A denial is the documented block schema carrying the recovery
  instruction as the reason.
- `stop_hook_active` is respected: when codex reports it already forced
  a continuation, the shim allows immediately and consults nothing - it
  must never contribute to a stop-hook loop, and it must not record a
  denial for a stop it never gated.
- Never block twice for the same tree: the bridge's repeat-denial guard
  reaches the host through the shim.
- Fail open on unusable input: unparseable stdin or a payload without a
  usable `cwd` allows the stop with a logged error (CI is the backstop,
  R4).
"""

import io
import json
import logging
from pathlib import Path

import pytest

from review.codex_stop import main
from review.packet import tree_state_hash
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


def run_hook(monkeypatch: pytest.MonkeyPatch, payload: str) -> int:
    """Run the shim's main() with the given text on stdin."""
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    return main()


def stop_payload(repo: Path, *, active: bool = False) -> str:
    """A codex Stop-hook input payload for the given repo.

    Includes the codex-specific fields (`turn_id`, `model`,
    `permission_mode`, `last_assistant_message`, null `transcript_path`)
    so every test also pins that the shim tolerates them.
    """
    return json.dumps(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "transcript_path": None,
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "model": "gpt-5.1-codex",
            "permission_mode": "default",
            "stop_hook_active": active,
            "last_assistant_message": "Done.",
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
    """A denial is the documented Stop schema: top-level block plus reason."""
    (repo / "src.py").write_text("VALUE = 2\n")
    assert run_hook(monkeypatch, stop_payload(repo)) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"decision", "reason"}
    assert output["decision"] == "block"
    assert "no review record" in output["reason"]
    assert "review/run.sh --new" in output["reason"]


def test_denial_is_recorded_through_the_bridge(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shim's denial arms the bridge's repeat guard for this tree."""
    (repo / "src.py").write_text("VALUE = 2\n")
    run_hook(monkeypatch, stop_payload(repo))
    assert read_state(repo)["denied_hash"] == tree_state_hash(repo)


def test_stop_hook_active_allows_without_gating(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An already-continued stop is never re-gated and records nothing."""
    (repo / "src.py").write_text("VALUE = 2\n")
    assert run_hook(monkeypatch, stop_payload(repo, active=True)) == 0
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


def test_missing_cwd_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A payload without a usable cwd allows the stop with a logged error."""
    payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
    with caplog.at_level(logging.ERROR):
        assert run_hook(monkeypatch, payload) == 0
    assert capsys.readouterr().out == ""
    assert "failing open" in caplog.text


def test_denied_session_reaches_acceptance_and_stops(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T17 acceptance: the T15 loop works end to end through the shim.

    A codex session edits tracked source and tries to stop: the shim
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
