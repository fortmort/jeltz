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
from review.run import main, write_state
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
    accepted = verdict_obj("ACCEPTED", round_number=2, blockers=[finding("b1", "resolved")])
    home = install_fake_codex(
        tmp_path,
        [{"stdout": events(requires)}, {"stdout": events(accepted)}],
    )
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    response = tmp_path / "response.md"
    response.write_text(
        response_text(1, [{"id": "b1", "disposition": "fixed", "reason": "patched"}])
    )
    code = run_main(
        monkeypatch,
        home,
        ["--resume", "--repo", str(dirty_repo), "--response-file", str(response)],
    )
    assert code == 0
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
        json.dumps({"backend": "codex", "thread_id": "t", "round": 1, "history": []}),
    ],
    ids=[
        "unparseable",
        "not-an-object",
        "unknown-backend",
        "missing-thread",
        "round-not-int",
        "history-not-list",
        "missing-verdict",
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
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj()), "write": "src.py"}])
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
    smuggled = verdict_obj("ACCEPTED", round_number=2, blockers=[finding("b1", "resolved")])
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
    smuggled = verdict_obj("ACCEPTED", round_number=2, blockers=[finding("b1", "resolved")])
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
    wrong = verdict_obj("ACCEPTED", round_number=5, blockers=[finding("b1", "resolved")])
    home = install_fake_codex(
        tmp_path,
        [
            {"stdout": events(requires)},
            {"stdout": events(wrong)},
            {"stdout": events(wrong)},
        ],
    )
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    response = tmp_path / "response.md"
    response.write_text(
        response_text(1, [{"id": "b1", "disposition": "fixed", "reason": "patched"}])
    )
    code = run_main(
        monkeypatch,
        home,
        ["--resume", "--repo", str(dirty_repo), "--response-file", str(response)],
    )
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


def response_text(round_number: int, dispositions: list[dict]) -> str:
    """A reviewer-response fixer output ending in its section E block."""
    block = json.dumps(
        {
            "schema_version": 1,
            "round": round_number,
            "dispositions": dispositions,
        }
    )
    return "### A. Classification Summary\n\nprose\n\n```json\n" + block + "\n```\n"


def dossier_path(repo: Path) -> Path:
    return repo / ".jeltz" / "review" / "escalation.md"


def test_resume_escalates_a_reasserted_rejection(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Condition 3: a rejected-invalid id re-asserted exits 20 with a dossier."""
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    reasserted = verdict_obj(
        "REQUIRES_CHANGES", round_number=2, blockers=[finding("b1", "unresolved")]
    )
    home = install_fake_codex(
        tmp_path, [{"stdout": events(requires)}, {"stdout": events(reasserted)}]
    )
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    response = tmp_path / "response.md"
    response.write_text(
        response_text(
            1,
            [{"id": "b1", "disposition": "rejected-invalid", "reason": "not a defect"}],
        )
    )
    code = run_main(
        monkeypatch,
        home,
        ["--resume", "--repo", str(dirty_repo), "--response-file", str(response)],
    )
    assert code == 20
    assert "human" in capsys.readouterr().err
    dossier = dossier_path(dirty_repo).read_text()
    assert "condition 3" in dossier
    assert "b1" in dossier
    assert "not a defect" in dossier
    assert read_state(dirty_repo)["round"] == 2


def test_round_cap_escalates_after_three_failing_rounds(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Condition 1: round 3 still REQUIRES_CHANGES exits 20, state recorded.

    Each round surfaces a NEW blocker: with dispositions now mandatory, a
    returning id would trip condition 2 or 3 first, so the pure round-cap
    path is a reviewer that keeps finding fresh problems.
    """
    rounds = [
        verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")]),
        verdict_obj(
            "REQUIRES_CHANGES",
            round_number=2,
            blockers=[finding("b1", "resolved"), finding("b2", "unresolved")],
        ),
        verdict_obj(
            "REQUIRES_CHANGES",
            round_number=3,
            blockers=[
                finding("b1", "resolved"),
                finding("b2", "resolved"),
                finding("b3", "unresolved"),
            ],
        ),
    ]
    home = install_fake_codex(tmp_path, [{"stdout": events(v)} for v in rounds])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    first = tmp_path / "response1.md"
    first.write_text(response_text(1, [{"id": "b1", "disposition": "fixed", "reason": "patched"}]))
    args = ["--resume", "--repo", str(dirty_repo), "--response-file", str(first)]
    assert run_main(monkeypatch, home, args) == 10
    second = tmp_path / "response2.md"
    second.write_text(
        response_text(
            2,
            [
                {"id": "b1", "disposition": "fixed", "reason": "patched"},
                {"id": "b2", "disposition": "fixed", "reason": "patched"},
            ],
        )
    )
    args = ["--resume", "--repo", str(dirty_repo), "--response-file", str(second)]
    code = run_main(monkeypatch, home, args)
    assert code == 20
    assert "human" in capsys.readouterr().err
    dossier = dossier_path(dirty_repo).read_text()
    assert "condition 1" in dossier
    assert "b3" in dossier
    assert "condition 2" not in dossier
    assert "condition 3" not in dossier
    state = read_state(dirty_repo)
    assert state["round"] == 3
    assert state["verdict"]["verdict"] == "REQUIRES_CHANGES"


def test_response_file_for_the_wrong_round_fails(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A response answering a round other than the recorded one exits 1."""
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    home = install_fake_codex(tmp_path, [{"stdout": events(requires)}])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    response = tmp_path / "response.md"
    response.write_text(
        response_text(3, [{"id": "b1", "disposition": "fixed", "reason": "patched"}])
    )
    code = run_main(
        monkeypatch,
        home,
        ["--resume", "--repo", str(dirty_repo), "--response-file", str(response)],
    )
    assert code == 1
    assert "round" in capsys.readouterr().err
    assert len(calls(home)) == 1
    assert read_state(dirty_repo)["round"] == 1


def test_response_file_requires_resume(
    dirty_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--response-file with --new is a usage error: no round to respond to."""
    response = tmp_path / "response.md"
    response.write_text(response_text(1, []))
    with pytest.raises(SystemExit) as excinfo:
        main(["--new", "--repo", str(dirty_repo), "--response-file", str(response)])
    assert excinfo.value.code == 2
    assert "no prior round" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [None, "prose without a dispositions block"],
    ids=["missing", "no-block"],
)
def test_unusable_response_file_fails(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    content: str | None,
) -> None:
    """A missing or blockless response file exits 1 before any dispatch."""
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    home = install_fake_codex(tmp_path, [{"stdout": events(requires)}])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    response = tmp_path / "response.md"
    if content is not None:
        response.write_text(content)
    code = run_main(
        monkeypatch,
        home,
        ["--resume", "--repo", str(dirty_repo), "--response-file", str(response)],
    )
    assert code == 1
    assert "response" in capsys.readouterr().err
    assert len(calls(home)) == 1
    assert read_state(dirty_repo)["round"] == 1


def test_packet_too_large_writes_a_dossier(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Condition 4 leaves the same human-facing artifact as conditions 1-3."""
    home = install_fake_codex(tmp_path, [])
    code = run_main(
        monkeypatch,
        home,
        ["--new", "--repo", str(dirty_repo), "--size-ceiling", "10"],
    )
    assert code == 20
    assert "condition 4" in dossier_path(dirty_repo).read_text()
    assert not state_path(dirty_repo).exists()


def test_resume_after_escalation_is_refused(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Escalation is terminal: --resume is refused, --new is the recovery.

    Without this, a round-3 escalation could be followed by an ordinary
    --resume dispatching round 4 - past the cap, and able to overwrite
    the escalated state with an accepting verdict.
    """
    rounds = [
        verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")]),
        verdict_obj(
            "REQUIRES_CHANGES",
            round_number=2,
            blockers=[finding("b1", "resolved"), finding("b2", "unresolved")],
        ),
        verdict_obj(
            "REQUIRES_CHANGES",
            round_number=3,
            blockers=[
                finding("b1", "resolved"),
                finding("b2", "resolved"),
                finding("b3", "unresolved"),
            ],
        ),
    ]
    home = install_fake_codex(
        tmp_path,
        [{"stdout": events(v)} for v in rounds]
        + [{"stdout": events(verdict_obj(), thread="thread-codex-2")}],
    )
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    first = tmp_path / "response1.md"
    first.write_text(response_text(1, [{"id": "b1", "disposition": "fixed", "reason": "patched"}]))
    args = ["--resume", "--repo", str(dirty_repo), "--response-file", str(first)]
    assert run_main(monkeypatch, home, args) == 10
    second = tmp_path / "response2.md"
    second.write_text(
        response_text(
            2,
            [
                {"id": "b1", "disposition": "fixed", "reason": "patched"},
                {"id": "b2", "disposition": "fixed", "reason": "patched"},
            ],
        )
    )
    args = ["--resume", "--repo", str(dirty_repo), "--response-file", str(second)]
    assert run_main(monkeypatch, home, args) == 20
    capsys.readouterr()
    code = run_main(monkeypatch, home, ["--resume", "--repo", str(dirty_repo)])
    assert code == 1
    assert "--new" in capsys.readouterr().err
    assert len(calls(home)) == 3
    assert read_state(dirty_repo)["round"] == 3
    code = run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)])
    assert code == 0
    state = read_state(dirty_repo)
    assert state["round"] == 1
    assert state["thread_id"] == "thread-codex-2"
    assert "escalated" not in state


def test_remediation_resume_requires_a_response_file(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Resuming past REQUIRES_CHANGES without dispositions is refused.

    Conditions 2 and 3 key on the coder's claims; a resume that omits
    them would silently disable both comparisons.
    """
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1")])
    home = install_fake_codex(tmp_path, [{"stdout": events(requires)}])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    code = run_main(monkeypatch, home, ["--resume", "--repo", str(dirty_repo)])
    assert code == 1
    assert "--response-file" in capsys.readouterr().err
    assert len(calls(home)) == 1
    assert read_state(dirty_repo)["round"] == 1


@pytest.mark.parametrize(
    ("answered", "complaint"),
    [
        (["b1"], "b2"),
        (["b1", "b2", "bogus"], "bogus"),
    ],
    ids=["missing-id", "unknown-id"],
)
def test_remediation_response_must_cover_every_finding(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    answered: list[str],
    complaint: str,
) -> None:
    """The T5 contract: every finding id answered exactly once.

    A partial response could omit exactly the claimed-fixed or rejected
    blocker whose comeback conditions 2 and 3 key on.
    """
    requires = verdict_obj("REQUIRES_CHANGES", blockers=[finding("b1"), finding("b2")])
    home = install_fake_codex(tmp_path, [{"stdout": events(requires)}])
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 10
    response = tmp_path / "response.md"
    response.write_text(
        response_text(
            1,
            [{"id": fid, "disposition": "fixed", "reason": "patched"} for fid in answered],
        )
    )
    code = run_main(
        monkeypatch,
        home,
        ["--resume", "--repo", str(dirty_repo), "--response-file", str(response)],
    )
    assert code == 1
    assert complaint in capsys.readouterr().err
    assert len(calls(home)) == 1
    assert read_state(dirty_repo)["round"] == 1


def test_resume_after_acceptance_needs_no_response(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A voluntary re-review after acceptance has no blockers to answer."""
    home = install_fake_codex(
        tmp_path,
        [
            {"stdout": events(verdict_obj())},
            {"stdout": events(verdict_obj("ACCEPTED", round_number=2))},
        ],
    )
    assert run_main(monkeypatch, home, ["--new", "--repo", str(dirty_repo)]) == 0
    code = run_main(monkeypatch, home, ["--resume", "--repo", str(dirty_repo)])
    assert code == 0
    assert read_state(dirty_repo)["round"] == 2


HOSTILE_WIP_MESSAGE = (
    "feat: wire the frobnicator\n"
    "\n"
    "Runs `make verify` and $(hooks) with \"$PATH\" and 'quotes' intact;\n"
    "a commit message is arbitrary text and must never pass through shell\n"
    "syntax on its way to the reviewer.\n"
)


def test_wip_message_file_preserves_hostile_content(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--wip-message-file transports the message byte-for-byte.

    A real commit message contains backticks, $(), $VAR, and quotes -
    content a shell would expand or mangle if interpolated into a
    command line (T20 review). The file path is the safe transport: the
    packet must carry the exact bytes, with nothing executed on the way.
    """
    message = tmp_path / "wip-message.txt"
    message.write_text(HOSTILE_WIP_MESSAGE)
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj())}])
    code = run_main(
        monkeypatch,
        home,
        ["--new", "--repo", str(dirty_repo), "--wip-message-file", str(message)],
    )
    assert code == 0
    packet = calls(home)[0]["argv"][-1]
    assert HOSTILE_WIP_MESSAGE in packet, "message not preserved byte-for-byte"
    assert "WIP under review" not in packet, "packet fell back to the default"
    assert not (dirty_repo / "hooks").exists(), "message content had side effects"


def test_wip_message_file_unreadable_fails(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """An unreadable message file exits 1 before contacting the reviewer."""
    home = install_fake_codex(tmp_path, [])
    code = run_main(
        monkeypatch,
        home,
        [
            "--new",
            "--repo",
            str(dirty_repo),
            "--wip-message-file",
            str(tmp_path / "absent.txt"),
        ],
    )
    assert code == 1
    assert "--wip-message-file" in capsys.readouterr().err
    assert calls(home) == []
    assert not state_path(dirty_repo).exists()


def test_wip_message_flags_are_exclusive(dirty_repo: Path, capsys: pytest.CaptureFixture) -> None:
    """--wip-message and --wip-message-file cannot be combined."""
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--new",
                "--repo",
                str(dirty_repo),
                "--wip-message",
                "inline",
                "--wip-message-file",
                "somewhere.txt",
            ]
        )
    assert excinfo.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


CRASHED_WRITER = """
import os
import pathlib
import sys

sys.path.insert(0, {root!r})

from review.run import write_state

# Die exactly where a killed writer dies: holding a temp file it has
# written but has not yet published over the state file.
os.replace = lambda *args: os._exit(9)
pathlib.Path.replace = lambda self, target: os._exit(9)

write_state(pathlib.Path({repo!r}), {{"schema_version": 1, "writer": "crashed"}})
"""


def crash_a_writer(repo: Path) -> tuple[Path, int]:
    """Leave behind exactly what a writer killed mid-write leaves behind.

    Runs a real writer in a real process and kills it in the window
    between writing its temp file and publishing it, so the leftover
    carries whatever name the implementation gives it - these tests
    never have to know that spelling.

    Args:
        repo: The repository whose review state is being written.

    Returns:
        The leftover temp file and the pid of the process that left it.
    """
    review_dir = state_path(repo).parent
    before = set(review_dir.iterdir()) if review_dir.exists() else set()
    script = CRASHED_WRITER.format(root=str(REPO_ROOT), repo=str(repo))
    process = subprocess.Popen([sys.executable, "-c", script])
    assert process.wait() == 9, "the writer did not die in the intended window"
    left = sorted(set(review_dir.iterdir()) - before - {state_path(repo)})
    assert len(left) == 1, (
        f"expected the killed writer to leave one temp file of its own, found {left}"
    )
    return left[0], process.pid


def test_interleaved_writers_never_leave_a_torn_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writers in one directory publish; they never shred each other.

    T32's premise is several agents per directory. Here the second
    writer completes inside the window where the first still holds an
    unpublished temp file - the interleaving a fixed temp name cannot
    survive, because the second writer's publish renames the first
    writer's temp out from under it. Whichever write lands last, the
    file left behind must be one writer's complete state, never a
    blend of the two and never absent.
    """
    first = {"schema_version": 1, "writer": "first", "padding": "a" * 4096}
    second = {"schema_version": 1, "writer": "second"}
    interleaved: list[bool] = []
    real_os_replace = os.replace
    real_path_replace = Path.replace

    def publishing_after_a_second_writer(publish):
        def wrapper(*args):
            if not interleaved:
                interleaved.append(True)
                write_state(tmp_path, second)
            return publish(*args)

        return wrapper

    monkeypatch.setattr(os, "replace", publishing_after_a_second_writer(real_os_replace))
    monkeypatch.setattr(Path, "replace", publishing_after_a_second_writer(real_path_replace))
    write_state(tmp_path, first)
    monkeypatch.undo()
    assert interleaved, "the second writer never ran; the window was not exercised"
    assert read_state(tmp_path) in (first, second)


def test_crashed_writers_leftovers_are_reaped_and_strangers_files_are_not(
    tmp_path: Path,
) -> None:
    """Cleanup reclaims jeltz's own abandoned temps and nothing else.

    Two writers are killed mid-write, so two temp files are abandoned;
    each has to be its own file, or the second writer overwrote the
    first one's unpublished work on the way past. A killed writer
    cannot clean up after itself, so the next writer does it - but only
    for files it can prove jeltz wrote and abandoned. Deleting every
    `*.tmp` in the directory would destroy work belonging to whoever
    else is running there.
    """
    first, _ = crash_a_writer(tmp_path)
    second, _ = crash_a_writer(tmp_path)
    assert first != second, "two writers shared one temp path"
    stranger = first.parent / "notes.tmp"
    stranger.write_text("someone else's scratch\n")
    lookalike = first.parent / "state.json.scratch.tmp"
    lookalike.write_text("not jeltz's either\n")
    write_state(tmp_path, {"schema_version": 1, "writer": "next"})
    assert not first.exists(), "a crashed writer's temp file was never reclaimed"
    assert not second.exists(), "a crashed writer's temp file was never reclaimed"
    assert stranger.read_text() == "someone else's scratch\n"
    assert lookalike.read_text() == "not jeltz's either\n"


def test_a_live_writers_temp_file_is_never_reaped(tmp_path: Path) -> None:
    """Cleanup spares a temp file whose owner is still running.

    Staleness has to be proved, not assumed: a concurrent agent's
    unpublished temp file looks identical to an abandoned one, and the
    only thing separating them is whether the process that created it
    is alive.
    """
    leftover, dead_pid = crash_a_writer(tmp_path)
    assert f".{dead_pid}." in leftover.name, (
        "a writer's temp file does not name the process that created it, "
        "so cleanup cannot tell an abandoned file from a live one"
    )
    live = leftover.parent / leftover.name.replace(f".{dead_pid}.", f".{os.getpid()}.", 1)
    leftover.rename(live)
    write_state(tmp_path, {"schema_version": 1, "writer": "next"})
    assert live.exists(), "a running writer's unpublished temp file was deleted"


def test_a_directory_wearing_a_leftover_name_cannot_block_a_write(tmp_path: Path) -> None:
    """An undeletable path in the state directory stops nothing.

    Cleanup is best-effort by design: it must never turn a hostile or
    merely odd path into a failed review round, and it must never try
    to force one away.
    """
    leftover, _ = crash_a_writer(tmp_path)
    occupied = leftover.parent / leftover.name
    leftover.unlink()
    occupied.mkdir()
    state = {"schema_version": 1, "writer": "next"}
    write_state(tmp_path, state)
    assert read_state(tmp_path) == state
    assert occupied.is_dir(), "cleanup removed a directory it could not prove was a temp file"


def test_the_state_file_keeps_the_permissions_a_plain_write_gives_it(tmp_path: Path) -> None:
    """Publishing through a temp file does not change who can read state.

    A guard on the implementation rather than a driver of it: the temp
    file is an internal detail, so the published state file must carry
    the mode the writing process's umask would have produced anyway.
    """
    write_state(tmp_path, {"schema_version": 1})
    reference = tmp_path / "reference.json"
    reference.write_text("{}\n")
    assert state_path(tmp_path).stat().st_mode & 0o777 == reference.stat().st_mode & 0o777


def test_a_temp_path_something_already_holds_is_never_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An occupied temp path stops the write instead of being overwritten.

    The name carries a uuid, so this cannot realistically happen - but
    "cannot realistically" is not a guarantee, and what it would hide is
    jeltz writing over, and then deleting, a file it never created.
    Forcing the collision proves the write fails closed and leaves the
    occupant exactly as it was.
    """
    state_dir = state_path(tmp_path).parent
    state_dir.mkdir(parents=True)
    occupied = state_dir / "state.json.taken.tmp"
    occupied.write_text("someone else's file\n")
    monkeypatch.setattr("review.run._state_temp", lambda _: occupied)
    with pytest.raises(FileExistsError):
        write_state(tmp_path, {"schema_version": 1})
    assert occupied.read_text() == "someone else's file\n", (
        "a file jeltz did not create was overwritten or removed"
    )
    assert not state_path(tmp_path).exists()
