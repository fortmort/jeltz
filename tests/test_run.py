"""Tests for T12: the one-round review orchestrator.

`review/run.sh --new | --resume` runs one full review round: build the
packet, materialize the disposable worktree, dispatch to the selected
reviewer adapter (codex by default, D4), parse the structured verdict, and
write machine state to `.jeltz/review/state.json` while the human-readable
review goes to stdout. Exit codes are the protocol: 0 accepted, 10 requires
changes (the coding session runs reviewer-response and resumes), 20 escalate
to a human. Operational failures - a dead backend, a tampered checkout, an
unusable state file - exit 1 and never write state, so a failed round cannot
clobber the last valid review record.

A scripted fake codex binary on PATH stands in for the backend; no test
contacts a real reviewer.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from review.packet import tree_state_hash
from review.run import main
from tests.conftest import git

REPO_ROOT = Path(__file__).resolve().parents[1]

FAKE_CODEX = '''#!/usr/bin/env python3
"""Scripted codex stand-in: one response file per invocation."""
import json
import os
import sys

home = os.path.dirname(os.path.abspath(__file__))
count_file = os.path.join(home, "count")
n = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
open(count_file, "w").write(str(n))
record = {"argv": sys.argv[1:], "cwd": os.getcwd()}
with open(os.path.join(home, "call%d.json" % n), "w") as f:
    json.dump(record, f)
spec = json.load(open(os.path.join(home, "resp%d.json" % n)))
if spec.get("write"):
    open(os.path.join(os.getcwd(), spec["write"]), "w").write("tampered\\n")
sys.stderr.write(spec.get("stderr", ""))
sys.stdout.write(spec.get("stdout", ""))
sys.exit(spec.get("exit", 0))
'''


def install_fake_codex(tmp_path: Path, responses: list[dict]) -> Path:
    """Install a scripted codex stand-in; returns its home directory.

    Each entry scripts one invocation: keys stdout, stderr, exit, and
    write (a filename to tamper into the cwd before answering). Every
    invocation records argv and cwd into call<n>.json.
    """
    home = tmp_path / "fake-codex"
    home.mkdir()
    for i, spec in enumerate(responses, 1):
        (home / f"resp{i}.json").write_text(json.dumps(spec))
    binary = home / "codex"
    binary.write_text(FAKE_CODEX)
    binary.chmod(0o755)
    return home


def calls(home: Path) -> list[dict]:
    """Read back the fake's per-invocation records, in call order."""
    return [json.loads(p.read_text()) for p in sorted(home.glob("call*.json"))]


def finding(fid: str, disposition: str | None = None) -> dict:
    """A minimal schema-valid finding under a stable id."""
    return {
        "id": fid,
        "file": "src.py",
        "line": 1,
        "claim": "claim",
        "why": "why",
        "disposition": disposition,
    }


def verdict_obj(
    verdict: str = "ACCEPTED",
    round_number: int = 1,
    blockers: list[dict] | None = None,
    non_blockers: list[dict] | None = None,
) -> dict:
    """A schema-valid verdict object."""
    return {
        "schema_version": 1,
        "verdict": verdict,
        "round": round_number,
        "blockers": blockers or [],
        "non_blockers": non_blockers or [],
    }


def events(verdict: dict, thread: str = "thread-codex-1") -> str:
    """A codex exec --json event stream carrying one bare verdict."""
    lines = [
        json.dumps({"type": "thread.started", "thread_id": thread}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(verdict)},
            }
        ),
    ]
    return "\n".join(lines) + "\n"


def run_main(monkeypatch: pytest.MonkeyPatch, home: Path, args: list[str]) -> int:
    """Invoke the orchestrator with the fake codex first on PATH."""
    monkeypatch.setenv("PATH", f"{home}{os.pathsep}{os.environ['PATH']}")
    return main(args)


def state_path(repo: Path) -> Path:
    return repo / ".jeltz" / "review" / "state.json"


def read_state(repo: Path) -> dict:
    return json.loads(state_path(repo).read_text())


def test_new_accepted_exits_zero_and_prints_review(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """An accepting round exits 0 with the review text on stdout."""
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj())}])
    code = run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)])
    assert code == 0
    out = capsys.readouterr().out
    assert "```json" in out
    assert '"ACCEPTED"' in out


def test_accepted_with_non_blockers_exits_zero(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-blockers do not gate: the round still exits 0."""
    accepted = verdict_obj("ACCEPTED_WITH_NON_BLOCKERS", non_blockers=[finding("nb1")])
    home = install_fake_codex(tmp_path, [{"stdout": events(accepted)}])
    code = run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)])
    assert code == 0


def test_new_requires_changes_exits_ten(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIRES_CHANGES maps to exit 10: run reviewer-response, resume."""
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    home = install_fake_codex(tmp_path, [{"stdout": events(requires)}])
    code = run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)])
    assert code == 10


def test_new_writes_state_file(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State carries backend, thread, round, diff hash, verdict, history."""
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    home = install_fake_codex(tmp_path, [{"stdout": events(requires)}])
    code = run_main(
        monkeypatch,
        home,
        ["--new", "--repo", str(dirty_repo), "--todo-ref", "T12"],
    )
    assert code == 10
    state = read_state(dirty_repo)
    assert state["schema_version"] == 1
    assert state["backend"] == "codex"
    assert state["task_ref"] == "T12"
    assert state["thread_id"] == "thread-codex-1"
    assert state["round"] == 1
    assert state["diff_hash"] == tree_state_hash(dirty_repo)
    assert state["verdict"] == requires
    assert state["history"] == [
        {
            "round": 1,
            "verdict": "REQUIRES_CHANGES",
            "blockers": [{"id": "b1", "disposition": None}],
            "costs": [],
        }
    ]


def test_resume_continues_recorded_thread(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--resume re-reviews on the recorded thread and appends history."""
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    accepted = verdict_obj(
        "ACCEPTED", round_number=2, blockers=[finding("b1", "resolved")]
    )
    home = install_fake_codex(
        tmp_path,
        [{"stdout": events(requires)}, {"stdout": events(accepted)}],
    )
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    assert run_main(monkeypatch, home, ["--resume", "--repo", str(dirty_repo)]) == 0
    resume_argv = calls(home)[1]["argv"]
    assert resume_argv[:3] == ["exec", "resume", "thread-codex-1"]
    state = read_state(dirty_repo)
    assert state["round"] == 2
    assert state["thread_id"] == "thread-codex-1"
    assert state["verdict"]["verdict"] == "ACCEPTED"
    assert [entry["round"] for entry in state["history"]] == [1, 2]
    assert state["history"][1]["blockers"] == [{"id": "b1", "disposition": "resolved"}]


def test_new_resets_prior_state(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh --new starts a new review: round 1, new thread, new history."""
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    home = install_fake_codex(
        tmp_path,
        [
            {"stdout": events(requires)},
            {"stdout": events(verdict_obj(), thread="thread-codex-2")},
        ],
    )
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 0
    assert calls(home)[1]["argv"][0] == "exec"
    assert "resume" not in calls(home)[1]["argv"][:2]
    state = read_state(dirty_repo)
    assert state["round"] == 1
    assert state["thread_id"] == "thread-codex-2"
    assert len(state["history"]) == 1


def test_resume_without_state_fails(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """--resume with nothing to resume exits 1 and points at --new."""
    home = install_fake_codex(tmp_path, [])
    code = run_main(monkeypatch, home, ["--resume", "--repo", str(dirty_repo)])
    assert code == 1
    assert "--new" in capsys.readouterr().err
    assert calls(home) == []
    assert not state_path(dirty_repo).exists()


@pytest.mark.parametrize(
    "state_text",
    [
        "not json at all",
        json.dumps(["not", "an", "object"]),
        json.dumps({"backend": "bogus", "thread_id": "t", "round": 1, "history": []}),
        json.dumps({"backend": "codex", "round": 1, "history": []}),
        json.dumps({"backend": "codex", "thread_id": "t", "round": "1", "history": []}),
        json.dumps({"backend": "codex", "thread_id": "t", "round": 1, "history": {}}),
    ],
    ids=[
        "unparseable",
        "not-an-object",
        "unknown-backend",
        "missing-thread",
        "round-not-int",
        "history-not-list",
    ],
)
def test_resume_with_unusable_state_fails(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    state_text: str,
) -> None:
    """A corrupt or incomplete state file exits 1, never a crash."""
    home = install_fake_codex(tmp_path, [])
    state_path(dirty_repo).parent.mkdir(parents=True)
    state_path(dirty_repo).write_text(state_text)
    code = run_main(monkeypatch, home, ["--resume", "--repo", str(dirty_repo)])
    assert code == 1
    assert "--new" in capsys.readouterr().err
    assert calls(home) == []


def test_mode_flags_are_validated(dirty_repo: Path) -> None:
    """Exactly one mode; --backend is a --new option; backends are closed."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--repo", str(dirty_repo)])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--new", "--resume", "--repo", str(dirty_repo)])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--resume", "--backend", "codex", "--repo", str(dirty_repo)])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--new", "--backend", "emacs", "--repo", str(dirty_repo)])
    assert excinfo.value.code == 2


def test_packet_too_large_escalates_exit_twenty(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """An oversized packet escalates (condition 4) before reviewer contact."""
    home = install_fake_codex(tmp_path, [])
    code = run_main(
        monkeypatch,
        home,
        ["--new", "--repo", str(dirty_repo), "--size-ceiling", "10"],
    )
    assert code == 20
    assert "human" in capsys.readouterr().err
    assert calls(home) == []
    assert not state_path(dirty_repo).exists()


def test_backend_failure_exits_one_without_state(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A dead backend is an operational failure: exit 1, no state write."""
    home = install_fake_codex(tmp_path, [{"exit": 1, "stderr": "boom"}])
    code = run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)])
    assert code == 1
    assert "codex exited 1" in capsys.readouterr().err
    assert not state_path(dirty_repo).exists()


def test_reviewer_edit_discards_verdict(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A tampered checkout exits 1 even over an accepting verdict (R7)."""
    home = install_fake_codex(
        tmp_path, [{"stdout": events(verdict_obj()), "write": "src.py"}]
    )
    code = run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)])
    assert code == 1
    assert "review checkout" in capsys.readouterr().err
    assert not state_path(dirty_repo).exists()


def test_packet_flags_reach_the_reviewer(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WIP message and verify output land in the dispatched packet."""
    verify_file = tmp_path / "verify.txt"
    verify_file.write_text("228 passed, coverage 100%\n")
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj())}])
    code = run_main(
        monkeypatch,
        home,
        [
            "--new",
            "--repo",
            str(dirty_repo),
            "--wip-message",
            "wip: T12 orchestrator",
            "--verify-output",
            str(verify_file),
        ],
    )
    assert code == 0
    argv = calls(home)[0]["argv"]
    assert "--output-schema" in argv
    prompt = argv[-1]
    assert "wip: T12 orchestrator" in prompt
    assert "228 passed, coverage 100%" in prompt


def test_missing_verify_output_file_fails(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """An unreadable --verify-output file exits 1 before any spawn."""
    home = install_fake_codex(tmp_path, [])
    missing = tmp_path / "no-such-verify.txt"
    code = run_main(
        monkeypatch,
        home,
        ["--new", "--repo", str(dirty_repo), "--verify-output", str(missing)],
    )
    assert code == 1
    assert "no-such-verify.txt" in capsys.readouterr().err
    assert calls(home) == []


def test_claude_backend_preflight_refusal(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The claude billing preflight (3.7) surfaces as exit 1, no state."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    home = install_fake_codex(tmp_path, [])
    code = run_main(
        monkeypatch,
        home,
        ["--new", "--backend", "claude", "--repo", str(dirty_repo)],
    )
    assert code == 1
    assert "--allow-api-billing" in capsys.readouterr().err
    assert not state_path(dirty_repo).exists()


def test_startup_reaps_stale_worktrees(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead prior review's worktree is reaped even on early failure."""
    stale = tmp_path / "jeltz-review-stale"
    stale.mkdir()
    git(dirty_repo, "stash")
    git(dirty_repo, "worktree", "add", "--detach", str(stale / "checkout"), "HEAD")
    git(dirty_repo, "stash", "pop")
    (stale / "pid").write_text("garbage")
    home = install_fake_codex(tmp_path, [])
    code = run_main(monkeypatch, home, ["--resume", "--repo", str(dirty_repo)])
    assert code == 1
    assert not (stale / "checkout").exists()
    assert "jeltz-review-stale" not in git(dirty_repo, "worktree", "list")


def test_new_rejects_round_smuggled_dispositions(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A fresh review claiming a later round cannot launder blockers.

    Verdict semantics let `resolved` dispositions deactivate blockers only
    in round 2+, so a round-1 reviewer declaring round 2 would otherwise
    turn unresolved blockers into an exit-0 acceptance. The mismatch gets
    the one same-thread repair; a reviewer that insists fails the round.
    """
    smuggled = verdict_obj(
        "ACCEPTED", round_number=2, blockers=[finding("b1", "resolved")]
    )
    home = install_fake_codex(
        tmp_path,
        [{"stdout": events(smuggled)}, {"stdout": events(smuggled)}],
    )
    code = run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)])
    assert code == 1
    assert "round" in capsys.readouterr().err
    assert not state_path(dirty_repo).exists()
    repair_argv = calls(home)[1]["argv"]
    assert repair_argv[:3] == ["exec", "resume", "thread-codex-1"]


def test_round_mismatch_is_repaired_on_the_same_thread(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong-round verdict that is corrected on repair completes the round."""
    smuggled = verdict_obj(
        "ACCEPTED", round_number=2, blockers=[finding("b1", "resolved")]
    )
    corrected = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    home = install_fake_codex(
        tmp_path,
        [{"stdout": events(smuggled)}, {"stdout": events(corrected)}],
    )
    code = run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)])
    assert code == 10
    state = read_state(dirty_repo)
    assert state["round"] == 1
    assert state["verdict"]["round"] == 1
    repair_prompt = calls(home)[1]["argv"][-1]
    assert "round" in repair_prompt


def test_resume_rejects_wrong_round_verdict(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A resumed round must come back as round 2, or the round fails."""
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    wrong = verdict_obj(
        "ACCEPTED", round_number=5, blockers=[finding("b1", "resolved")]
    )
    home = install_fake_codex(
        tmp_path,
        [
            {"stdout": events(requires)},
            {"stdout": events(wrong)},
            {"stdout": events(wrong)},
        ],
    )
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    code = run_main(monkeypatch, home, ["--resume", "--repo", str(dirty_repo)])
    assert code == 1
    assert "round" in capsys.readouterr().err
    state = read_state(dirty_repo)
    assert state["round"] == 1
    assert state["verdict"] == requires


def test_run_sh_wrapper_end_to_end(dirty_repo: Path, tmp_path: Path) -> None:
    """The shipped shell entry point drives a full round from any cwd."""
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj())}])
    env = dict(os.environ)
    venv_bin = Path(sys.executable).parent
    env["PATH"] = f"{home}{os.pathsep}{venv_bin}{os.pathsep}{env['PATH']}"
    proc = subprocess.run(
        [str(REPO_ROOT / "review" / "run.sh"), "--new", "--repo", str(dirty_repo)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert '"ACCEPTED"' in proc.stdout
    assert state_path(dirty_repo).exists()
