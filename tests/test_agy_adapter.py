"""Tests for T9: the antigravity (agy) adapter.

The adapter drives `agy -p` against the installed skeptical-reviewer skill.
A fresh review invokes the skill with the packet as context, enforces the
canonical verdict schema via --json-schema, and opens a project context with
--new-project (3.4 sharp edge 1: project skills load only with a project);
a re-review resumes the prior conversation with --conversation <id> (D1).
Output is a single JSON envelope whose `response` carries the reviewer's
text; agy fences verdict JSON itself, so the response passes to the T4
parser verbatim. Sharp edge 2 is the load-bearing failure mode: a headless
permission denial reports status SUCCESS with an empty `response` and puts
the reason only on stderr, so an empty response is a hard typed error that
carries the stderr note - never a silent pass.

A scripted fake agy binary speaks the live-probed envelope dialect
(agy 1.1.13); no test contacts a real backend.
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
from review.agy import AgyAdapter
from review.verdict import load_schema

FAKE_AGY = '''#!/usr/bin/env python3
"""Scripted stand-in for the agy CLI: one response file per invocation."""
import json
import os
import sys
import time

home = os.path.dirname(os.path.abspath(__file__))
count_file = os.path.join(home, "count")
n = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
open(count_file, "w").write(str(n))
record = {"argv": sys.argv[1:], "cwd": os.getcwd()}
if "--json-schema" in sys.argv:
    schema_path = sys.argv[sys.argv.index("--json-schema") + 1]
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

DENIAL_NOTE = (
    'jetski: no output produced - a tool required the "command" permission '
    "that headless mode cannot prompt for, so it was auto-denied. Add an "
    "allow-rule under permissions.allow in settings.json "
    "(e.g. command(<target>)).\n"
)


def install_fake_agy(tmp_path: Path, responses: list[dict]) -> Path:
    """Install a scripted agy stand-in; returns its home directory.

    Each entry in responses scripts one invocation: keys stdout, stderr,
    exit, sleep, read_stdin. Every invocation records argv, cwd, and the
    parsed content of any --json-schema file into call<n>.json.
    """
    home = tmp_path / "fake-agy"
    home.mkdir()
    for i, spec in enumerate(responses, 1):
        (home / f"resp{i}.json").write_text(json.dumps(spec))
    binary = home / "agy"
    binary.write_text(FAKE_AGY)
    binary.chmod(0o755)
    return home


def calls(home: Path) -> list[dict]:
    """Read back the fake's per-invocation records, in call order."""
    return [json.loads(p.read_text()) for p in sorted(home.glob("call*.json"))]


def fenced_verdict(verdict: str = "ACCEPTED", round_number: int = 1) -> str:
    """A reviewer response as agy emits it live: fenced verdict JSON."""
    body = json.dumps(
        {
            "schema_version": 1,
            "verdict": verdict,
            "round": round_number,
            "blockers": [],
            "non_blockers": [],
        }
    )
    return f"```json\n{body}\n```\n"


def envelope(
    response: str | None,
    conversation: str | None = "conv-agy-1",
    status: str = "SUCCESS",
) -> str:
    """An agy --output-format json envelope in the live-probed dialect.

    Passing None omits the key entirely - agy drops empty JSON keys, so
    a denied run can arrive with no `response` key at all.
    """
    data: dict = {
        "status": status,
        "duration_seconds": 4.25,
        "num_turns": 1,
        "usage": {"input_tokens": 7637, "output_tokens": 296},
    }
    if conversation is not None:
        data["conversation_id"] = conversation
    if response is not None:
        data["response"] = response
    return json.dumps(data) + "\n"


def adapter_for(home: Path, timeout: float = 30) -> AgyAdapter:
    """An AgyAdapter wired to the fake binary."""
    return AgyAdapter(agy_bin=str(home / "agy"), timeout=timeout)


def test_fresh_review_runs_print_json_with_schema_and_project(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Round one: agy -p --output-format json --json-schema --new-project."""
    home = install_fake_agy(tmp_path, [{"stdout": envelope(fenced_verdict())}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: agy round one")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.thread_id == "conv-agy-1"
    (call,) = calls(home)
    argv = call["argv"]
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--new-project" in argv
    assert argv[argv.index("--model") + 1] == "gemini-3.1-pro-high"
    assert call["schema"] == load_schema()


def test_fresh_review_invokes_the_installed_skill(dirty_repo: Path, tmp_path: Path) -> None:
    """The prompt is the slash-command skill invocation plus the packet."""
    home = install_fake_agy(tmp_path, [{"stdout": envelope(fenced_verdict())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: skill framing", todo_ref="T9")
    (call,) = calls(home)
    argv = call["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert prompt.startswith("/skeptical-reviewer HEAD")
    assert "## Diff vs HEAD" in prompt
    assert "VALUE = 2" in prompt
    assert "T9" in prompt


def test_review_runs_inside_the_disposable_worktree(dirty_repo: Path, tmp_path: Path) -> None:
    """agy works in the T6 checkout, never the developer's tree."""
    home = install_fake_agy(tmp_path, [{"stdout": envelope(fenced_verdict())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: worktree cwd")
    (call,) = calls(home)
    assert "jeltz-review-" in call["cwd"]
    assert call["cwd"] != str(dirty_repo)


def test_resume_reuses_the_prior_conversation(dirty_repo: Path, tmp_path: Path) -> None:
    """D1: a re-review is --conversation <id>, not a fresh project."""
    home = install_fake_agy(
        tmp_path,
        [{"stdout": envelope(fenced_verdict(round_number=2), conversation="conv-prior")}],
    )
    result = conduct_review(
        dirty_repo,
        adapter_for(home),
        "wip: round two",
        mode="resume",
        thread_id="conv-prior",
    )
    assert result.thread_id == "conv-prior"
    (call,) = calls(home)
    argv = call["argv"]
    assert argv[argv.index("--conversation") + 1] == "conv-prior"
    assert "--new-project" not in argv
    assert not argv[argv.index("-p") + 1].startswith("/skeptical-reviewer")


def test_fenced_response_reaches_the_parser_verbatim(dirty_repo: Path, tmp_path: Path) -> None:
    """agy fences verdict JSON itself (T4 probe): no normalization."""
    response = f"The work is sound.\n\n{fenced_verdict()}"
    home = install_fake_agy(tmp_path, [{"stdout": envelope(response)}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: verbatim")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.raw == response
    assert len(calls(home)) == 1


def test_empty_response_is_a_loud_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """3.4 sharp edge 2: a permission denial reports SUCCESS with an empty
    response and puts the reason only on stderr. The adapter must fail
    loudly with that reason, never record an empty review as a pass."""
    home = install_fake_agy(tmp_path, [{"stdout": envelope(""), "stderr": DENIAL_NOTE}])
    with pytest.raises(AdapterProcessError, match="auto-denied"):
        conduct_review(dirty_repo, adapter_for(home), "wip: silent denial")


def test_missing_response_key_is_a_loud_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """agy omits empty JSON keys, so a denied run can lack `response`."""
    home = install_fake_agy(tmp_path, [{"stdout": envelope(None), "stderr": DENIAL_NOTE}])
    with pytest.raises(AdapterProcessError, match="auto-denied"):
        conduct_review(dirty_repo, adapter_for(home), "wip: dropped key")


def test_non_success_status_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A non-SUCCESS envelope is a transport failure, not a verdict."""
    home = install_fake_agy(
        tmp_path,
        [{"stdout": envelope(fenced_verdict(), status="FAILURE")}],
    )
    with pytest.raises(AdapterProcessError, match="FAILURE"):
        conduct_review(dirty_repo, adapter_for(home), "wip: failed status")


def test_unparseable_envelope_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """Stdout that is not a JSON envelope is a transport failure."""
    home = install_fake_agy(tmp_path, [{"stdout": "not an envelope\n"}])
    with pytest.raises(AdapterProcessError):
        conduct_review(dirty_repo, adapter_for(home), "wip: broken envelope")


def test_backend_nonzero_exit_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A dying agy surfaces as AdapterProcessError carrying stderr."""
    home = install_fake_agy(tmp_path, [{"stdout": "", "stderr": "agy: boom", "exit": 1}])
    with pytest.raises(AdapterProcessError, match="boom"):
        conduct_review(dirty_repo, adapter_for(home), "wip: dead backend")


def test_missing_binary_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """An uninstalled agy is a transport failure, not a crash."""
    adapter = AgyAdapter(agy_bin=str(tmp_path / "no-such-agy"))
    with pytest.raises(AdapterProcessError, match="no-such-agy"):
        conduct_review(dirty_repo, adapter, "wip: missing binary")


def test_non_executable_binary_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """Any spawn failure is a transport failure: a configured binary that
    exists but lacks the exec bit must not escape as raw PermissionError."""
    binary = tmp_path / "agy-no-exec-bit"
    binary.write_text(FAKE_AGY)
    binary.chmod(0o644)
    adapter = AgyAdapter(agy_bin=str(binary))
    with pytest.raises(AdapterProcessError, match="agy-no-exec-bit"):
        conduct_review(dirty_repo, adapter, "wip: unspawnable binary")


def test_hung_backend_is_killed_into_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A hung agy is killed into a typed error, never a hang."""
    home = install_fake_agy(tmp_path, [{"sleep": 30, "stdout": envelope(fenced_verdict())}])
    start = time.monotonic()
    with pytest.raises(AdapterProcessError, match="[Tt]ime"):
        conduct_review(dirty_repo, adapter_for(home, timeout=1), "wip: hung backend")
    assert time.monotonic() - start < 15


def test_backend_cannot_wait_on_the_callers_stdin(dirty_repo: Path, tmp_path: Path) -> None:
    """A caller whose stdin never closes (a hook, a wrapper script) must
    not stall the review: the backend gets /dev/null, not our stdin."""
    home = install_fake_agy(tmp_path, [{"read_stdin": True, "stdout": envelope(fenced_verdict())}])
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        result = conduct_review(dirty_repo, adapter_for(home, timeout=5), "wip: open stdin")
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)
    assert result.verdict["verdict"] == "ACCEPTED"


def test_envelope_without_conversation_id_is_a_continuity_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """No conversation id means the review can never be resumed (D1)."""
    home = install_fake_agy(tmp_path, [{"stdout": envelope(fenced_verdict(), conversation=None)}])
    with pytest.raises(ThreadContinuityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: no conversation")


def test_repair_round_resumes_the_conversation(dirty_repo: Path, tmp_path: Path) -> None:
    """R2: the single repair goes back via --conversation on the same id."""
    home = install_fake_agy(
        tmp_path,
        [
            {"stdout": envelope("I forgot the verdict block.")},
            {"stdout": envelope(fenced_verdict())},
        ],
    )
    result = conduct_review(dirty_repo, adapter_for(home), "wip: repairable")
    assert result.verdict["verdict"] == "ACCEPTED"
    first, second = calls(home)
    assert "--conversation" not in first["argv"]
    argv = second["argv"]
    assert argv[argv.index("--conversation") + 1] == "conv-agy-1"
    assert "fenced JSON" in argv[argv.index("-p") + 1]


@pytest.mark.parametrize("stdout", ["[]\n", "null\n"])
def test_non_object_envelope_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path, stdout: str
) -> None:
    """Valid JSON that is not an envelope object is a transport failure,
    not an AttributeError escaping the typed boundary."""
    home = install_fake_agy(tmp_path, [{"stdout": stdout}])
    with pytest.raises(AdapterProcessError):
        conduct_review(dirty_repo, adapter_for(home), "wip: non-object")


def test_null_response_is_a_loud_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A null response is materially an empty response (sharp edge 2):
    the same loud typed error, never an AttributeError."""
    stdout = json.dumps({"status": "SUCCESS", "conversation_id": "conv-agy-1", "response": None})
    home = install_fake_agy(tmp_path, [{"stdout": stdout, "stderr": DENIAL_NOTE}])
    with pytest.raises(AdapterProcessError, match="auto-denied"):
        conduct_review(dirty_repo, adapter_for(home), "wip: null response")


def test_non_string_conversation_id_is_a_continuity_error(dirty_repo: Path, tmp_path: Path) -> None:
    """D1: a conversation id that is not a string can never be resumed
    (and would poison a later resume argv), so it degrades exactly like
    a missing one - ThreadContinuityError, never a leaked integer."""
    stdout = json.dumps({"status": "SUCCESS", "conversation_id": 123, "response": fenced_verdict()})
    home = install_fake_agy(tmp_path, [{"stdout": stdout}])
    with pytest.raises(ThreadContinuityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: numeric conversation")
