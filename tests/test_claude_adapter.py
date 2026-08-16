"""Tests for T11: the claude adapter (fallback).

The adapter drives `claude -p` against the project skeptical-reviewer
skill. A fresh review opens a new session on a generated `--session-id`
(a UUID that did not previously exist) and a re-review resumes that same
session with `--resume <id>` (D1). The review passes the canonical
verdict schema inline via --json-schema and applies the shipped tool
allowlist via --tools; omitting Edit and Write keeps consumer PostToolUse
hooks from firing inside the reviewer, while Bash-issued writes stay
caught by the T6 integrity check (R7).

The 3.7 preflight: `ANTHROPIC_API_KEY` in the environment flips Claude
Code to API billing, so the adapter refuses to run when it is set unless
API billing was explicitly allowed - and it never passes `--bare`, whose
auth is strictly API-key based. The T3 spike found a user-scope copy of
the same skill name shadows the project copy headless, so the fresh
framing points at the project skill path explicitly.

A scripted fake claude binary speaks the live-probed envelope dialect
(claude 2.1.233): `{type, subtype, is_error, result, session_id,
stop_reason, total_cost_usd, usage}` with `structured_output` carrying
the parsed schema-constrained object. No test contacts a real backend.
"""

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from review.adapter import (
    AdapterProcessError,
    ThreadContinuityError,
    conduct_review,
    tool_policy,
)
from review.claude import ClaudeAdapter
from review.verdict import EmptyOutputError, draftless_schema
from review.worktree import IntegrityError

FAKE_CLAUDE = '''#!/usr/bin/env python3
"""Scripted stand-in for the claude CLI: one response file per invocation."""
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
    record["schema"] = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
with open(os.path.join(home, "call%d.json" % n), "w") as f:
    json.dump(record, f)
spec = json.load(open(os.path.join(home, "resp%d.json" % n)))
if spec.get("read_stdin"):
    sys.stdin.read()
if spec.get("write"):
    open(spec["write"], "w").write("mutated by the reviewer")
time.sleep(spec.get("sleep", 0))
sys.stderr.write(spec.get("stderr", ""))
out = spec.get("stdout", "")
for flag in ("--session-id", "--resume"):
    if flag in sys.argv:
        out = out.replace("SENT-SESSION-ID", sys.argv[sys.argv.index(flag) + 1])
sys.stdout.write(out)
sys.exit(spec.get("exit", 0))
'''


@pytest.fixture(autouse=True)
def _no_ambient_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from an API key in the developer's environment;
    the 3.7 preflight tests set one explicitly."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def install_fake_claude(tmp_path: Path, responses: list[dict]) -> Path:
    """Install a scripted claude stand-in; returns its home directory.

    Each entry in responses scripts one invocation: keys stdout, stderr,
    exit, sleep, read_stdin, and write (a file created in the invocation
    cwd, simulating a reviewer that edits its checkout). Every invocation
    records argv, cwd, and the parsed value of any inline --json-schema
    argument into call<n>.json.
    """
    home = tmp_path / "fake-claude"
    home.mkdir()
    for i, spec in enumerate(responses, 1):
        (home / f"resp{i}.json").write_text(json.dumps(spec))
    binary = home / "claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(0o755)
    return home


def calls(home: Path) -> list[dict]:
    """Read back the fake's per-invocation records, in call order."""
    return [json.loads(p.read_text()) for p in sorted(home.glob("call*.json"))]


def verdict_body(verdict: str = "ACCEPTED", round_number: int = 1) -> str:
    """A schema-constrained verdict as claude emits it live: bare JSON."""
    return json.dumps(
        {
            "schema_version": 1,
            "verdict": verdict,
            "round": round_number,
            "blockers": [],
            "non_blockers": [],
        }
    )


def envelope(
    result: str | None,
    session: str | None = "SENT-SESSION-ID",
    subtype: str | None = "success",
    is_error: bool = False,
    structured: dict | None = None,
    cost: float = 0.024859,
) -> str:
    """A claude --output-format json envelope in the live-probed dialect.

    Passing None for result, session, or subtype omits that key entirely.
    The default session is the SENT-SESSION-ID sentinel, which the fake
    replaces with the id the invocation actually received via
    --session-id or --resume - mirroring the live envelope, which echoes
    the sent id back.
    """
    data: dict = {
        "type": "result",
        "is_error": is_error,
        "num_turns": 1,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 13062, "output_tokens": 106},
        "total_cost_usd": cost,
        "uuid": "a07d1954-7a08-4979-b4cc-1f375ddac29b",
    }
    if result is not None:
        data["result"] = result
    if session is not None:
        data["session_id"] = session
    if subtype is not None:
        data["subtype"] = subtype
    if structured is not None:
        data["structured_output"] = structured
    return json.dumps(data) + "\n"


def adapter_for(
    home: Path, timeout: float = 30, allow_api_billing: bool = False
) -> ClaudeAdapter:
    """A ClaudeAdapter wired to the fake binary."""
    return ClaudeAdapter(
        claude_bin=str(home / "claude"),
        timeout=timeout,
        allow_api_billing=allow_api_billing,
    )


def test_fresh_review_runs_single_prompt_with_schema_and_tools(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Round one: claude --session-id <uuid> --tools <allow>
    --output-format json --json-schema <inline canonical schema> -p, with
    the shipped allowlist applied and never --bare (3.7: its auth is
    strictly API-key based)."""
    home = install_fake_claude(tmp_path, [{"stdout": envelope(verdict_body())}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: claude round one")
    assert result.verdict["verdict"] == "ACCEPTED"
    (call,) = calls(home)
    argv = call["argv"]
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ",".join(tool_policy("claude").allow)
    sent = argv[argv.index("--session-id") + 1]
    assert uuid.UUID(sent)
    assert result.thread_id == sent
    assert "--resume" not in argv
    assert "--bare" not in argv
    # Verified live (claude 2.1.233): --json-schema rejects any schema
    # declaring the 2020-12 draft, so the inline schema is the canonical
    # one minus its $schema declaration.
    assert call["schema"] == draftless_schema()
    assert "$schema" not in call["schema"]


def test_each_fresh_review_opens_a_previously_unused_session(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """T11 acceptance: each new review loop starts on a generated session
    id that did not previously exist - two fresh reviews never share
    one."""
    home = install_fake_claude(
        tmp_path,
        [
            {"stdout": envelope(verdict_body())},
            {"stdout": envelope(verdict_body())},
        ],
    )
    r1 = conduct_review(dirty_repo, adapter_for(home), "wip: loop one")
    r2 = conduct_review(dirty_repo, adapter_for(home), "wip: loop two")
    first, second = calls(home)
    ids = [
        call["argv"][call["argv"].index("--session-id") + 1] for call in (first, second)
    ]
    assert ids[0] != ids[1]
    assert all(uuid.UUID(sid).version == 4 for sid in ids)
    # The loop's thread ids ARE the generated ids - not whatever the
    # backend chose to answer with.
    assert [r1.thread_id, r2.thread_id] == ids


def test_unechoed_fresh_session_is_a_continuity_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """D1: the generated id is verified against the envelope's echo, not
    trusted. A backend that answers on some other session - existing or
    unrelated - must fail the round, or a later resume would continue a
    thread this loop never opened."""
    home = install_fake_claude(
        tmp_path,
        [
            {
                "stdout": envelope(
                    verdict_body(),
                    session="22222222-2222-4222-8222-222222222222",
                )
            }
        ],
    )
    with pytest.raises(ThreadContinuityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: unechoed session")


def test_api_key_in_environment_refuses_by_default(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.7: ANTHROPIC_API_KEY flips Claude Code to API billing, so the
    preflight refuses with a clear message before any backend contact."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    home = install_fake_claude(tmp_path, [{"stdout": envelope(verdict_body())}])
    with pytest.raises(AdapterProcessError, match="ANTHROPIC_API_KEY"):
        conduct_review(dirty_repo, adapter_for(home), "wip: api key set")
    assert calls(home) == []


def test_api_key_refusal_covers_resume(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The billing flip applies to every invocation, not only round one -
    a resumed round with the key set must refuse identically."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    home = install_fake_claude(tmp_path, [{"stdout": envelope(verdict_body())}])
    with pytest.raises(AdapterProcessError, match="ANTHROPIC_API_KEY"):
        conduct_review(
            dirty_repo,
            adapter_for(home),
            "wip: resumed with key",
            mode="resume",
            thread_id="sess-claude-1",
        )
    assert calls(home) == []


def test_allow_api_billing_opts_in(
    dirty_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--allow-api-billing is the explicit opt-in: with it the review
    runs even though the key is set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    home = install_fake_claude(tmp_path, [{"stdout": envelope(verdict_body())}])
    result = conduct_review(
        dirty_repo,
        adapter_for(home, allow_api_billing=True),
        "wip: billing allowed",
    )
    assert result.verdict["verdict"] == "ACCEPTED"


def test_fresh_review_invokes_the_project_skill(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """The prompt is the slash-command skill invocation plus the packet,
    pointing at the project skill path explicitly - the T3 spike found a
    user-scope copy of the same name shadows the project copy headless."""
    home = install_fake_claude(tmp_path, [{"stdout": envelope(verdict_body())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: skill framing", todo_ref="T11")
    (call,) = calls(home)
    argv = call["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert prompt.startswith("/skeptical-reviewer HEAD")
    assert ".claude/skills/skeptical-reviewer" in prompt
    assert "## Diff vs HEAD" in prompt
    assert "VALUE = 2" in prompt
    assert "T11" in prompt


def test_review_runs_inside_the_disposable_worktree(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """claude works in the T6 checkout, never the developer's tree."""
    home = install_fake_claude(tmp_path, [{"stdout": envelope(verdict_body())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: worktree cwd")
    (call,) = calls(home)
    assert "jeltz-review-" in call["cwd"]
    assert call["cwd"] != str(dirty_repo)


def test_resume_reuses_the_prior_session(dirty_repo: Path, tmp_path: Path) -> None:
    """D1: a re-review is --resume <id> on the same session - no new
    --session-id, no skill re-invocation."""
    home = install_fake_claude(
        tmp_path,
        [{"stdout": envelope(verdict_body(round_number=2), session="sess-prior")}],
    )
    result = conduct_review(
        dirty_repo,
        adapter_for(home),
        "wip: round two",
        mode="resume",
        thread_id="sess-prior",
    )
    assert result.thread_id == "sess-prior"
    (call,) = calls(home)
    argv = call["argv"]
    assert argv[argv.index("--resume") + 1] == "sess-prior"
    assert "--session-id" not in argv
    assert not argv[argv.index("-p") + 1].startswith("/skeptical-reviewer")


def test_bare_schema_output_is_fenced_for_the_parser(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Schema-constrained output arrives as bare JSON (live probe) and is
    normalized into the fenced block form the T4 parser expects."""
    body = verdict_body()
    home = install_fake_claude(tmp_path, [{"stdout": envelope(body)}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: bare verdict")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.raw == f"```json\n{body}\n```\n"


def test_structured_output_is_preferred_over_result_text(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """When the envelope supplies the parsed structured_output object the
    adapter uses it and ignores the result text entirely - the parsed
    object is the reliable carrier of the schema-constrained verdict."""
    body = verdict_body()
    home = install_fake_claude(
        tmp_path,
        [
            {
                "stdout": envelope(
                    f"prose preamble\n{body}{body}",
                    structured=json.loads(body),
                )
            }
        ],
    )
    result = conduct_review(dirty_repo, adapter_for(home), "wip: structured")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.raw == f"```json\n{body}\n```\n"


def test_prose_with_fenced_verdict_passes_verbatim(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Result text already carrying prose or a fence is not
    double-wrapped."""
    text = f"The work is sound.\n\n```json\n{verdict_body()}\n```\n"
    home = install_fake_claude(tmp_path, [{"stdout": envelope(text)}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: verbatim")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.raw == text


def test_error_subtype_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """An envelope whose subtype is not success (error_during_execution,
    error_max_turns) is a failed run, never a verdict."""
    home = install_fake_claude(
        tmp_path,
        [
            {
                "stdout": envelope(
                    "Execution failed before an answer.",
                    subtype="error_during_execution",
                    is_error=True,
                )
            }
        ],
    )
    with pytest.raises(AdapterProcessError, match="error_during_execution"):
        conduct_review(dirty_repo, adapter_for(home), "wip: error subtype")


def test_error_flag_without_error_subtype_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """is_error true is a failed run even if subtype still reads success -
    either signal alone must fail the round."""
    home = install_fake_claude(
        tmp_path, [{"stdout": envelope(verdict_body(), is_error=True)}]
    )
    with pytest.raises(AdapterProcessError):
        conduct_review(dirty_repo, adapter_for(home), "wip: error flag")


def test_missing_subtype_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """An envelope with no subtype cannot prove the run succeeded."""
    home = install_fake_claude(
        tmp_path, [{"stdout": envelope(verdict_body(), subtype=None)}]
    )
    with pytest.raises(AdapterProcessError, match=r"\(none\)"):
        conduct_review(dirty_repo, adapter_for(home), "wip: no subtype")


def test_empty_result_is_an_empty_output_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """A successful run with empty result text is R1's empty output:
    never repaired, never recorded as a pass."""
    home = install_fake_claude(tmp_path, [{"stdout": envelope("")}])
    with pytest.raises(EmptyOutputError):
        conduct_review(dirty_repo, adapter_for(home), "wip: empty result")


def test_null_result_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A non-string result field without structured output is a broken
    envelope, never a crash."""
    stdout = json.dumps(
        {
            "result": None,
            "session_id": "sess-claude-1",
            "subtype": "success",
            "is_error": False,
        }
    )
    home = install_fake_claude(tmp_path, [{"stdout": stdout}])
    with pytest.raises(AdapterProcessError, match="result"):
        conduct_review(dirty_repo, adapter_for(home), "wip: null result")


def test_unparseable_envelope_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Stdout that is not a JSON envelope is a transport failure."""
    home = install_fake_claude(tmp_path, [{"stdout": "not an envelope\n"}])
    with pytest.raises(AdapterProcessError):
        conduct_review(dirty_repo, adapter_for(home), "wip: broken envelope")


@pytest.mark.parametrize("stdout", ["[]\n", "null\n"])
def test_non_object_envelope_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path, stdout: str
) -> None:
    """Valid JSON that is not an envelope object is a transport failure,
    not an AttributeError escaping the typed boundary."""
    home = install_fake_claude(tmp_path, [{"stdout": stdout}])
    with pytest.raises(AdapterProcessError):
        conduct_review(dirty_repo, adapter_for(home), "wip: non-object")


def test_backend_nonzero_exit_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """A dying claude (probed live: an unknown --resume id exits 1 with
    stderr only) surfaces as AdapterProcessError carrying that stderr."""
    home = install_fake_claude(
        tmp_path,
        [
            {
                "stdout": "",
                "stderr": "No conversation found with session ID: sess-gone",
                "exit": 1,
            }
        ],
    )
    with pytest.raises(AdapterProcessError, match="No conversation found"):
        conduct_review(dirty_repo, adapter_for(home), "wip: dead backend")


def test_missing_binary_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """An uninstalled claude is a transport failure, not a crash."""
    adapter = ClaudeAdapter(claude_bin=str(tmp_path / "no-such-claude"))
    with pytest.raises(AdapterProcessError, match="no-such-claude"):
        conduct_review(dirty_repo, adapter, "wip: missing binary")


def test_non_executable_binary_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Any spawn failure is a transport failure: a configured binary that
    exists but lacks the exec bit must not escape as raw PermissionError."""
    binary = tmp_path / "claude-no-exec-bit"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(0o644)
    adapter = ClaudeAdapter(claude_bin=str(binary))
    with pytest.raises(AdapterProcessError, match="claude-no-exec-bit"):
        conduct_review(dirty_repo, adapter, "wip: unspawnable binary")


def test_hung_backend_is_killed_into_a_typed_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """A hung claude is killed into a typed error, never a hang."""
    home = install_fake_claude(
        tmp_path, [{"sleep": 30, "stdout": envelope(verdict_body())}]
    )
    start = time.monotonic()
    with pytest.raises(AdapterProcessError, match="[Tt]ime"):
        conduct_review(dirty_repo, adapter_for(home, timeout=1), "wip: hung backend")
    assert time.monotonic() - start < 15


def test_backend_cannot_wait_on_the_callers_stdin(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """A caller whose stdin never closes (a hook, a wrapper script) must
    not stall the review: the backend gets /dev/null, not our stdin."""
    home = install_fake_claude(
        tmp_path, [{"read_stdin": True, "stdout": envelope(verdict_body())}]
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


def test_envelope_without_session_id_is_a_continuity_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """No session id means the review can never be resumed (D1) - the
    envelope's answer is verified, never assumed from the id we sent."""
    home = install_fake_claude(
        tmp_path, [{"stdout": envelope(verdict_body(), session=None)}]
    )
    with pytest.raises(ThreadContinuityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: no session")


def test_non_string_session_id_is_a_continuity_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """D1: a session id that is not a string can never be resumed (and
    would poison a later resume argv), so it degrades exactly like a
    missing one - ThreadContinuityError, never a leaked integer."""
    stdout = json.dumps(
        {
            "result": verdict_body(),
            "session_id": 123,
            "subtype": "success",
            "is_error": False,
        }
    )
    home = install_fake_claude(tmp_path, [{"stdout": stdout}])
    with pytest.raises(ThreadContinuityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: numeric session")


def test_repair_round_resumes_the_session(dirty_repo: Path, tmp_path: Path) -> None:
    """R2: the single repair goes back via --resume on the same id."""
    home = install_fake_claude(
        tmp_path,
        [
            {"stdout": envelope("I forgot the verdict block.")},
            {"stdout": envelope(verdict_body())},
        ],
    )
    result = conduct_review(dirty_repo, adapter_for(home), "wip: repairable")
    assert result.verdict["verdict"] == "ACCEPTED"
    first, second = calls(home)
    assert "--resume" not in first["argv"]
    generated = first["argv"][first["argv"].index("--session-id") + 1]
    argv = second["argv"]
    assert argv[argv.index("--resume") + 1] == generated
    assert "--session-id" not in argv
    assert "fenced JSON" in argv[argv.index("-p") + 1]
    assert result.thread_id == generated


def test_shell_issued_edit_is_caught_by_the_integrity_check(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """--tools omits Edit and Write but Bash stays enabled for make
    verify, so a shell-issued edit in the review checkout is caught by
    the integrity check (R7), not by the allowlist - even when the
    verdict itself accepts."""
    home = install_fake_claude(
        tmp_path, [{"write": "sneaky.txt", "stdout": envelope(verdict_body())}]
    )
    with pytest.raises(IntegrityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: reviewer edit")


def test_costs_are_recorded_into_review_state(dirty_repo: Path, tmp_path: Path) -> None:
    """claude reports total_cost_usd and usage per turn; both land on the
    round's ReviewResult in turn order - review state any host-neutral
    writer can persist (T7 contract, extended in T10)."""
    home = install_fake_claude(
        tmp_path,
        [
            {"stdout": envelope("I forgot the verdict block.", cost=0.01)},
            {"stdout": envelope(verdict_body(), cost=0.02)},
        ],
    )
    result = conduct_review(dirty_repo, adapter_for(home), "wip: cost log")
    assert result.costs == (
        {
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 13062, "output_tokens": 106},
        },
        {
            "total_cost_usd": 0.02,
            "usage": {"input_tokens": 13062, "output_tokens": 106},
        },
    )


def test_missing_telemetry_records_no_cost_entry(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """An envelope reporting neither cost nor usage contributes no
    entry - never a {None, None} placeholder polluting review state."""
    stdout = (
        json.dumps(
            {
                "result": verdict_body(),
                "session_id": "SENT-SESSION-ID",
                "subtype": "success",
                "is_error": False,
            }
        )
        + "\n"
    )
    home = install_fake_claude(tmp_path, [{"stdout": stdout}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: no telemetry")
    assert result.costs == ()
