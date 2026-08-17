"""Tests for T19: the grok Stop hook shim.

T19's original premise - that grok has no stop-blocking hook event
(finding 3.5) - was disproven live against grok 1.0.4 during T19: grok
ships a Claude-Code-compatible blocking `Stop` hook, verified by a
headless round trip in which `{"decision": "block", "reason": ...}` on
stdout kept the agent working and the reason reached the model. The
shim therefore follows the T16-T18 shape instead of the originally
specified deny-at-edit PreToolUse gate.

Grok's contract differs from the Claude-Code-style protocol on two
points (verified live, grok 1.0.4):

- The stdin payload is camelCase: the loop guard is `stopHookActive`
  (true on every continuation fire after a block), and the workspace
  root is `workspaceRoot` - grok resolves it to the git root even when
  the session's `cwd` is a subdirectory, and it arrives with a trailing
  slash. The payload adds `hookEventName`, `sessionId`, `cwd`,
  `timestamp`, `transcriptPath`, `promptId`, `permissionMode`,
  `reason`, `lastAssistantMessage`, `backgroundTasks`, and
  `sessionCrons`; the shim tolerates and ignores them all.
- Not every Stop fire is a genuine stop attempt: an observe-only Stop
  also fires at session end (`reason` `"shutdown"` or
  `"channel_closed"`), and its decision output is parsed but ignored.
  The shim must gate only `reason == "end_turn"` - gating a session-end
  fire would record a denial the session can never act on, burning the
  bridge's one-denial-per-tree guard for the next genuine stop.

The deny decision itself is Claude's: a top-level
`{"decision": "block", "reason": ...}` on stdout, silence allows.

These tests pin grok's host contract independently of the other shims:
- A denial is the documented block schema carrying the recovery
  instruction as the reason; the reason reaches the model (verified
  live).
- `stopHookActive` is the loop guard: a stop the hook already continued
  is allowed immediately and consults nothing - the shim must never
  contribute to a stop-hook loop (grok force-stops after 8
  continuations anyway) and must not record a denial for a stop it
  never gated.
- Only `reason == "end_turn"` fires are gated; session-end fires are
  allowed silently and record nothing.
- The gated root is `workspaceRoot`, not `cwd`: the state file and the
  recovery commands live at the repository root, which grok names
  explicitly.
- Never block twice for the same tree: the bridge's repeat-denial guard
  reaches the host through the shim.
- Fail open on unusable input: unparseable stdin or a payload without a
  usable workspace root allows the stop with a logged error (CI is the
  backstop, R4).
"""

import io
import json
import logging
from pathlib import Path

import pytest

from review.grok_stop import main
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


def stop_payload(
    repo: Path, *, reason: str = "end_turn", stop_hook_active: bool = False
) -> str:
    """A grok Stop-hook input payload for the given repo.

    Mirrors a live capture from grok 1.0.4: camelCase fields,
    `workspaceRoot` with its trailing slash, and the grok-specific
    metadata (`promptId`, `lastAssistantMessage`, `backgroundTasks`,
    ...) so every test also pins that the shim tolerates them.
    """
    return json.dumps(
        {
            "hookEventName": "stop",
            "sessionId": "01a0116a-d714-7d91-806e-cf7466cf62f1",
            "cwd": str(repo),
            "workspaceRoot": str(repo) + "/",
            "timestamp": "2026-08-17T20:30:12.425017+00:00",
            "transcriptPath": "/home/user/.grok/sessions/p/s-1/updates.jsonl",
            "promptId": "ce55fe9f-368e-4817-bf30-bca0fb1ca3ed",
            "permissionMode": "default",
            "reason": reason,
            "stopHookActive": stop_hook_active,
            "lastAssistantMessage": "PING",
            "backgroundTasks": [],
            "sessionCrons": [],
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
    """A denial is grok's documented Stop schema: block plus reason."""
    (repo / "src.py").write_text("VALUE = 2\n")
    assert run_hook(monkeypatch, stop_payload(repo)) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"decision", "reason"}
    assert output["decision"] == "block"
    assert "no review record" in output["reason"]
    assert "review/run.sh --new" in output["reason"]


def test_denial_is_recorded_through_the_bridge(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shim's denial arms the bridge's repeat guard for this tree."""
    (repo / "src.py").write_text("VALUE = 2\n")
    run_hook(monkeypatch, stop_payload(repo))
    assert read_state(repo)["denied_hash"] == tree_state_hash(repo)


def test_continuation_fire_allows_without_gating(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stop the hook already continued once is never re-gated.

    `stopHookActive` is true on every fire after a block this turn
    (verified live: the continuation fire of a blocked stop carried
    true where the first fire carried false); the shim allows
    immediately and records nothing.
    """
    (repo / "src.py").write_text("VALUE = 2\n")
    assert run_hook(monkeypatch, stop_payload(repo, stop_hook_active=True)) == 0
    assert capsys.readouterr().out == ""
    assert not (repo / ".jeltz").exists()


@pytest.mark.parametrize("reason", ["shutdown", "channel_closed"])
def test_session_end_fire_is_never_gated(
    reason: str,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The observe-only session-end Stop fire is allowed and unrecorded.

    Grok fires an extra Stop at session end whose decision output is
    parsed but ignored (verified live: `reason` was `"shutdown"`, with
    no `promptId`). Gating it could not keep the session working, but
    it WOULD record a denial - burning the bridge's one-denial-per-tree
    guard so the next genuine stop attempt would be allowed unreviewed.
    """
    (repo / "src.py").write_text("VALUE = 2\n")
    assert run_hook(monkeypatch, stop_payload(repo, reason=reason)) == 0
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
    """A payload without a usable workspace root allows with a logged error."""
    payload = json.dumps(
        {"reason": "end_turn", "stopHookActive": False, "workspaceRoot": ""}
    )
    with caplog.at_level(logging.ERROR):
        assert run_hook(monkeypatch, payload) == 0
    assert capsys.readouterr().out == ""
    assert "failing open" in caplog.text


def test_workspace_root_is_gated_not_cwd(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shim gates `workspaceRoot`, the repository root grok names.

    Verified live: launched from a subdirectory, grok's payload carries
    the subdirectory as `cwd` but still resolves `workspaceRoot` to the
    git root (trailing slash included). The state file and the recovery
    commands live at the root, so that is the tree the shim must gate
    and record.
    """
    sub = repo / "sub"
    sub.mkdir()
    (repo / "src.py").write_text("VALUE = 2\n")
    payload = json.loads(stop_payload(repo))
    payload["cwd"] = str(sub)
    assert run_hook(monkeypatch, json.dumps(payload)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "block"
    assert read_state(repo)["denied_hash"] == tree_state_hash(repo)
    assert not (sub / ".jeltz").exists()


def test_denied_session_reaches_acceptance_and_stops(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T19 acceptance: the T15 loop works end to end through the shim.

    A grok session edits tracked source and tries to stop: the shim
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
