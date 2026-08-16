"""Tests for T8: the codex adapter.

The adapter drives `codex exec` against the installed skeptical-reviewer
skill (D2 settled: the exec path, not MCP). A fresh review invokes the skill
by name with the packet as context and pins the verdict shape with
--output-schema; a re-review runs `codex exec resume <thread>` on the prior
reviewer thread (D1). The JSONL event stream supplies the thread id and the
final agent message; schema-constrained output arrives as bare JSON and is
normalized into the fenced form the T4 parser expects. A backend that dies,
hangs, or is missing surfaces as a typed AdapterProcessError, never a hang
or a verdict.

A scripted fake codex binary speaks the live-probed JSONL dialect
(codex-cli 0.147.0); no test contacts a real backend.
"""

import json
import os
import time
from pathlib import Path

import pytest

from review.adapter import (
    AdapterProcessError,
    ThreadContinuityError,
    conduct_review,
)
from review.codex import CodexAdapter
from review.verdict import EmptyOutputError, strict_schema

FAKE_CODEX = '''#!/usr/bin/env python3
"""Scripted stand-in for the codex CLI: one response file per invocation."""
import json
import os
import sys
import time

home = os.path.dirname(os.path.abspath(__file__))
count_file = os.path.join(home, "count")
n = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
open(count_file, "w").write(str(n))
record = {"argv": sys.argv[1:], "cwd": os.getcwd()}
if "--output-schema" in sys.argv:
    schema_path = sys.argv[sys.argv.index("--output-schema") + 1]
    record["schema"] = json.load(open(schema_path))
with open(os.path.join(home, "call%d.json" % n), "w") as f:
    json.dump(record, f)
spec = json.load(open(os.path.join(home, "resp%d.json" % n)))
if spec.get("read_stdin"):
    sys.stdin.read()
time.sleep(spec.get("sleep", 0))
sys.stderr.write(spec.get("stderr", ""))
sys.stdout.write(spec.get("stdout", ""))
sys.exit(spec.get("exit", 0))
'''


def install_fake_codex(tmp_path: Path, responses: list[dict]) -> Path:
    """Install a scripted codex stand-in; returns its home directory.

    Each entry in responses scripts one invocation: keys stdout, stderr,
    exit, sleep. Every invocation records argv, cwd, and the content of
    any --output-schema file into call<n>.json.
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


def bare_verdict(verdict: str = "ACCEPTED", round_number: int = 1) -> str:
    """A schema-constrained final message: raw JSON, no fence."""
    return json.dumps(
        {
            "schema_version": 1,
            "verdict": verdict,
            "round": round_number,
            "blockers": [],
            "non_blockers": [],
        }
    )


def events(*texts: str, thread: str = "thread-codex-1") -> str:
    """A codex exec --json event stream in the live-probed dialect."""
    lines = [json.dumps({"type": "thread.started", "thread_id": thread})]
    lines.append(json.dumps({"type": "turn.started"}))
    for i, text in enumerate(texts):
        lines.append(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"item_{i}",
                        "type": "agent_message",
                        "text": text,
                    },
                }
            )
        )
    lines.append(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
    )
    return "\n".join(lines) + "\n"


def adapter_for(home: Path, timeout: float = 30) -> CodexAdapter:
    """A CodexAdapter wired to the fake binary."""
    return CodexAdapter(codex_bin=str(home / "codex"), timeout=timeout)


def test_fresh_review_runs_exec_with_schema_and_json(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Round one: codex exec --json --sandbox read-only --output-schema."""
    home = install_fake_codex(tmp_path, [{"stdout": events(bare_verdict())}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: codex round one")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.thread_id == "thread-codex-1"
    (call,) = calls(home)
    argv = call["argv"]
    assert argv[0] == "exec"
    assert "--json" in argv
    assert "read-only" in argv[argv.index("--sandbox") + 1]
    assert call["schema"] == strict_schema()


def test_fresh_review_invokes_the_installed_skill(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """The prompt is the known-good skill invocation plus the packet."""
    home = install_fake_codex(tmp_path, [{"stdout": events(bare_verdict())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: skill framing", todo_ref="T8")
    (call,) = calls(home)
    prompt = call["argv"][-1]
    assert prompt.startswith("$skeptical-reviewer HEAD")
    assert "## Diff vs HEAD" in prompt
    assert "VALUE = 2" in prompt
    assert "T8" in prompt


def test_review_runs_inside_the_disposable_worktree(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """codex works in the T6 checkout, never the developer's tree."""
    home = install_fake_codex(tmp_path, [{"stdout": events(bare_verdict())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: worktree cwd")
    (call,) = calls(home)
    assert "jeltz-review-" in call["cwd"]
    assert call["cwd"] != str(dirty_repo)


def test_resume_runs_exec_resume_on_the_prior_thread(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """D1: a re-review is codex exec resume <thread>, not a fresh session."""
    home = install_fake_codex(
        tmp_path,
        [{"stdout": events(bare_verdict(round_number=2), thread="thread-prior")}],
    )
    result = conduct_review(
        dirty_repo,
        adapter_for(home),
        "wip: round two",
        mode="resume",
        thread_id="thread-prior",
    )
    assert result.thread_id == "thread-prior"
    (call,) = calls(home)
    argv = call["argv"]
    assert argv[:3] == ["exec", "resume", "thread-prior"]
    assert "--sandbox" not in argv
    assert any(a.startswith("sandbox_mode=") for a in argv)
    assert not argv[-1].startswith("$skeptical-reviewer")


def test_bare_schema_json_is_normalized_for_the_parser(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """--output-schema emits raw JSON; the adapter fences it (T4 note)."""
    home = install_fake_codex(tmp_path, [{"stdout": events(bare_verdict())}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: bare json")
    assert "```json" in result.raw
    assert len(calls(home)) == 1


def test_fenced_output_is_passed_through_unwrapped(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """A reviewer that already fenced its verdict is not double-wrapped."""
    fenced = f"The work is sound.\n\n```json\n{bare_verdict()}\n```\n"
    home = install_fake_codex(tmp_path, [{"stdout": events(fenced)}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: fenced")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.raw == fenced


def test_the_last_agent_message_carries_the_verdict(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Interim narration messages are not mistaken for the verdict."""
    home = install_fake_codex(
        tmp_path,
        [{"stdout": events("Reading the diff now.", bare_verdict())}],
    )
    result = conduct_review(dirty_repo, adapter_for(home), "wip: narration")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert len(calls(home)) == 1


def test_garbage_lines_in_the_stream_are_ignored(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Non-JSON noise on stdout does not break event parsing."""
    noisy = "Shell cwd was reset to /somewhere\n" + events(bare_verdict())
    home = install_fake_codex(tmp_path, [{"stdout": noisy}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: noisy")
    assert result.verdict["verdict"] == "ACCEPTED"


def test_json_lines_that_are_not_objects_are_ignored(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """A JSON line that is not an event object is skipped, not crashed on."""
    noisy = "42\n" + events(bare_verdict())
    home = install_fake_codex(tmp_path, [{"stdout": noisy}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: json noise")
    assert result.verdict["verdict"] == "ACCEPTED"


def test_non_verdict_json_message_is_not_fenced(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Only verdict-shaped JSON gets wrapped; other JSON goes to repair."""
    home = install_fake_codex(
        tmp_path,
        [
            {"stdout": events('["not", "a", "verdict"]')},
            {"stdout": events(bare_verdict())},
        ],
    )
    result = conduct_review(dirty_repo, adapter_for(home), "wip: odd json")
    assert result.verdict["verdict"] == "ACCEPTED"
    first, second = calls(home)
    assert "fenced JSON" in second["argv"][-1]


def test_backend_nonzero_exit_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """A dying codex surfaces as AdapterProcessError carrying stderr."""
    home = install_fake_codex(
        tmp_path, [{"stdout": "", "stderr": "codex: boom", "exit": 1}]
    )
    with pytest.raises(AdapterProcessError, match="boom"):
        conduct_review(dirty_repo, adapter_for(home), "wip: dead backend")


def test_missing_binary_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """An uninstalled codex is a transport failure, not a crash."""
    adapter = CodexAdapter(codex_bin=str(tmp_path / "no-such-codex"))
    with pytest.raises(AdapterProcessError, match="no-such-codex"):
        conduct_review(dirty_repo, adapter, "wip: missing binary")


def test_non_executable_binary_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Any spawn failure is a transport failure: a configured binary that
    exists but lacks the exec bit must not escape as raw PermissionError."""
    binary = tmp_path / "codex-no-exec-bit"
    binary.write_text(FAKE_CODEX)
    binary.chmod(0o644)
    adapter = CodexAdapter(codex_bin=str(binary))
    with pytest.raises(AdapterProcessError, match="codex-no-exec-bit"):
        conduct_review(dirty_repo, adapter, "wip: unspawnable binary")


def test_hung_backend_is_killed_into_a_typed_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """T8 acceptance: a killed backend is a typed error, not a hang."""
    home = install_fake_codex(
        tmp_path, [{"sleep": 30, "stdout": events(bare_verdict())}]
    )
    start = time.monotonic()
    with pytest.raises(AdapterProcessError, match="[Tt]ime"):
        conduct_review(dirty_repo, adapter_for(home, timeout=1), "wip: hung backend")
    assert time.monotonic() - start < 15


def test_backend_cannot_wait_on_the_callers_stdin(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """codex appends piped stdin to the prompt and waits for EOF, so a
    caller whose stdin never closes (a hook, a wrapper script) must not
    stall the review: the backend gets /dev/null, not our stdin."""
    home = install_fake_codex(
        tmp_path, [{"read_stdin": True, "stdout": events(bare_verdict())}]
    )
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        result = conduct_review(
            dirty_repo, adapter_for(home, timeout=5), "wip: open stdin"
        )
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)
    assert result.verdict["verdict"] == "ACCEPTED"


def test_stream_without_thread_id_is_a_continuity_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """No thread.started event means the review can never be resumed."""
    stream = (
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "agent_message",
                    "text": bare_verdict(),
                },
            }
        )
        + "\n"
    )
    home = install_fake_codex(tmp_path, [{"stdout": stream}])
    with pytest.raises(ThreadContinuityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: no thread")


def test_stream_without_agent_message_is_empty_output(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """R1: a stream with no agent message is a dead adapter, never a pass."""
    home = install_fake_codex(tmp_path, [{"stdout": events()}])
    with pytest.raises(EmptyOutputError):
        conduct_review(dirty_repo, adapter_for(home), "wip: silent stream")


def test_repair_round_resumes_the_review_thread(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """R2: the single repair goes back via exec resume on the same thread."""
    home = install_fake_codex(
        tmp_path,
        [
            {"stdout": events("I forgot the verdict block.")},
            {"stdout": events(bare_verdict())},
        ],
    )
    result = conduct_review(dirty_repo, adapter_for(home), "wip: repairable")
    assert result.verdict["verdict"] == "ACCEPTED"
    first, second = calls(home)
    assert first["argv"][0] == "exec"
    assert first["argv"][1] != "resume"
    assert second["argv"][:3] == ["exec", "resume", "thread-codex-1"]
    assert "fenced JSON" in second["argv"][-1]
