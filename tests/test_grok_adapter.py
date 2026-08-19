"""Tests for T10: the grok adapter.

The adapter drives `grok -p` against the natively discovered
skeptical-reviewer skill. Grok needs no install step - it reads
`.claude/skills/` and `~/.claude/skills/` directly (3.5) - so a fresh
review first runs `grok inspect` as a preflight and asserts the skill is
actually discovered rather than assuming it. The review itself passes the
canonical verdict schema inline via --json-schema (grok rejects a file
path), applies the shipped tool allowlist via --tools, and a re-review
resumes the prior session with --resume <id> (D1). Output is a single JSON
envelope whose `text` carries the schema-constrained output as bare JSON,
normalized into the fenced form the T4 parser expects (as on codex).

The load-bearing sharp edge (T3 spike): a run that dies inside grok ends
with exit 0, `stopReason: "cancelled"`, and narration-only text - so the
adapter asserts on the envelope's stop reason, never on exit status. Each
turn's `total_cost_usd` and `usage` are recorded into review state; grok
is the calibration host for the round budget.

A scripted fake grok binary speaks the live-probed envelope dialect
(grok 1.0.4); no test contacts a real backend.
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
    tool_policy,
)
from review.grok import GrokAdapter
from review.verdict import EmptyOutputError, load_schema
from review.worktree import IntegrityError

FAKE_GROK = '''#!/usr/bin/env python3
"""Scripted stand-in for the grok CLI: one response file per invocation."""
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
sys.stdout.write(spec.get("stdout", ""))
sys.exit(spec.get("exit", 0))
'''


def inspect_listing(*names: str, status: str = "enabled") -> dict:
    """A `grok inspect --json` response listing the given skills."""
    return {
        "stdout": json.dumps(
            {
                "projectTrusted": True,
                "skills": [
                    {
                        "name": name,
                        "source": {"type": "project"},
                        "compatibilityStatus": status,
                    }
                    for name in names
                ],
                "configWarnings": [],
            }
        )
        + "\n"
    }


INSPECT_OK = inspect_listing("skeptical-reviewer", "review")

INSPECT_NO_SKILL = inspect_listing("review")


def install_fake_grok(tmp_path: Path, responses: list[dict]) -> Path:
    """Install a scripted grok stand-in; returns its home directory.

    Each entry in responses scripts one invocation - including `inspect`
    preflights, which consume a response slot like any other call: keys
    stdout, stderr, exit, sleep, read_stdin, and write (a file created in
    the invocation cwd, simulating a reviewer that edits its checkout).
    Every invocation records argv, cwd, and the parsed value of any
    inline --json-schema argument into call<n>.json.
    """
    home = tmp_path / "fake-grok"
    home.mkdir()
    for i, spec in enumerate(responses, 1):
        (home / f"resp{i}.json").write_text(json.dumps(spec))
    binary = home / "grok"
    binary.write_text(FAKE_GROK)
    binary.chmod(0o755)
    return home


def calls(home: Path) -> list[dict]:
    """Read back the fake's per-invocation records, in call order."""
    return [json.loads(p.read_text()) for p in sorted(home.glob("call*.json"))]


def verdict_body(verdict: str = "ACCEPTED", round_number: int = 1) -> str:
    """A schema-constrained verdict as grok emits it live: bare JSON."""
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
    text: str | None,
    session: str | None = "sess-grok-1",
    stop: str | None = "end_turn",
    cost: float = 0.026888,
) -> str:
    """A grok --output-format json envelope in the live-probed dialect.

    Passing None for text, session, or stop omits that key entirely.
    """
    data: dict = {
        "requestId": "req-1",
        "num_turns": 1,
        "usage": {"input_tokens": 13062, "output_tokens": 106},
        "total_cost_usd": cost,
    }
    if text is not None:
        data["text"] = text
    if session is not None:
        data["sessionId"] = session
    if stop is not None:
        data["stopReason"] = stop
    return json.dumps(data) + "\n"


def adapter_for(home: Path, timeout: float = 30) -> GrokAdapter:
    """A GrokAdapter wired to the fake binary."""
    return GrokAdapter(grok_bin=str(home / "grok"), timeout=timeout)


def test_fresh_review_runs_single_prompt_with_schema_and_tools(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """Round one: grok --tools <allow> --always-approve --output-format
    json --json-schema <inline canonical schema> -p, with the shipped
    allowlist applied. --always-approve is required headless: without it
    the first non-git bash call is silently cancelled (probed live); the
    --tools list bounds what it can approve."""
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": envelope(verdict_body())}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: grok round one")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.thread_id == "sess-grok-1"
    preflight, call = calls(home)
    assert preflight["argv"] == ["inspect", "--json"]
    argv = call["argv"]
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ",".join(tool_policy("grok").allow)
    assert "--always-approve" in argv
    assert "--resume" not in argv
    assert call["schema"] == load_schema()


def test_preflight_asserts_skill_discovery_in_the_worktree(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """3.5: no install step exists, so discovery must be asserted, not
    assumed - `grok inspect` runs in the review checkout before any
    reviewer contact."""
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": envelope(verdict_body())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: preflight")
    preflight, call = calls(home)
    assert preflight["cwd"] == call["cwd"]
    assert "jeltz-review-" in preflight["cwd"]


def test_preflight_missing_skill_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A checkout where grok does not discover the skill must fail loudly
    before the review starts, not run a skill-less review."""
    home = install_fake_grok(tmp_path, [INSPECT_NO_SKILL])
    with pytest.raises(AdapterProcessError, match="skeptical-reviewer"):
        conduct_review(dirty_repo, adapter_for(home), "wip: undiscovered skill")
    assert len(calls(home)) == 1


def test_preflight_warning_mention_is_not_discovery(dirty_repo: Path, tmp_path: Path) -> None:
    """The skill name appearing outside the skills array - e.g. a config
    warning that it failed to load - is not discovery. The preflight
    parses `grok inspect --json` and requires an exact skill entry,
    never a substring match over the whole output."""
    listing = json.loads(inspect_listing("review")["stdout"])
    listing["configWarnings"] = ["skeptical-reviewer failed to load"]
    home = install_fake_grok(tmp_path, [{"stdout": json.dumps(listing) + "\n"}])
    with pytest.raises(AdapterProcessError, match="skeptical-reviewer"):
        conduct_review(dirty_repo, adapter_for(home), "wip: warning mention")
    assert len(calls(home)) == 1


def test_preflight_disabled_skill_is_not_discovery(dirty_repo: Path, tmp_path: Path) -> None:
    """A listed skill whose compatibilityStatus is not enabled cannot be
    invoked, so it must not pass the preflight."""
    home = install_fake_grok(tmp_path, [inspect_listing("skeptical-reviewer", status="disabled")])
    with pytest.raises(AdapterProcessError, match="skeptical-reviewer"):
        conduct_review(dirty_repo, adapter_for(home), "wip: disabled skill")


def test_unparseable_inspect_output_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """Inspect output that is not JSON (e.g. the human-readable listing)
    is a transport failure, not a discovery pass."""
    home = install_fake_grok(
        tmp_path,
        [{"stdout": "  Skills (1)\n  - skeptical-reviewer  project [claude]\n"}],
    )
    with pytest.raises(AdapterProcessError):
        conduct_review(dirty_repo, adapter_for(home), "wip: prose inspect")


@pytest.mark.parametrize("skills", [None, 1])
def test_malformed_skills_array_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path, skills: object
) -> None:
    """A skills member that is not an array (JSON null, a scalar) is
    failed discovery, never a raw TypeError escaping the typed
    boundary."""
    stdout = json.dumps({"projectTrusted": True, "skills": skills}) + "\n"
    home = install_fake_grok(tmp_path, [{"stdout": stdout}])
    with pytest.raises(AdapterProcessError, match="skeptical-reviewer"):
        conduct_review(dirty_repo, adapter_for(home), "wip: malformed skills")


def test_preflight_failure_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A dying `grok inspect` (not logged in, broken install) is a
    transport failure carrying its stderr."""
    home = install_fake_grok(tmp_path, [{"stdout": "", "stderr": "grok: not logged in", "exit": 1}])
    with pytest.raises(AdapterProcessError, match="not logged in"):
        conduct_review(dirty_repo, adapter_for(home), "wip: dead preflight")


def test_fresh_review_invokes_the_installed_skill(dirty_repo: Path, tmp_path: Path) -> None:
    """The prompt is the slash-command skill invocation plus the packet."""
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": envelope(verdict_body())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: skill framing", todo_ref="T10")
    _, call = calls(home)
    argv = call["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert prompt.startswith("/skeptical-reviewer HEAD")
    assert "## Diff vs HEAD" in prompt
    assert "VALUE = 2" in prompt
    assert "T10" in prompt
    # Probed live: without explicit sequencing the schema-constrained
    # model emits a placeholder verdict before doing any review work.
    assert "never a placeholder" in prompt


def test_review_runs_inside_the_disposable_worktree(dirty_repo: Path, tmp_path: Path) -> None:
    """grok works in the T6 checkout, never the developer's tree."""
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": envelope(verdict_body())}])
    conduct_review(dirty_repo, adapter_for(home), "wip: worktree cwd")
    _, call = calls(home)
    assert "jeltz-review-" in call["cwd"]
    assert call["cwd"] != str(dirty_repo)


def test_resume_reuses_the_prior_session(dirty_repo: Path, tmp_path: Path) -> None:
    """D1: a re-review is --resume <id> on the same session - no fresh
    preflight, no skill re-invocation."""
    home = install_fake_grok(
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
    assert "inspect" not in argv
    assert "--always-approve" in argv
    assert not argv[argv.index("-p") + 1].startswith("/skeptical-reviewer")


def test_bare_schema_output_is_fenced_for_the_parser(dirty_repo: Path, tmp_path: Path) -> None:
    """Schema-constrained text arrives as bare JSON (live probe) and is
    normalized into the fenced block form the T4 parser expects."""
    body = verdict_body()
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": envelope(body)}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: bare verdict")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.raw == f"```json\n{body}\n```\n"


def test_structured_output_is_preferred_over_text(dirty_repo: Path, tmp_path: Path) -> None:
    """Under tool use the envelope's `text` concatenates the model's
    message with the structured output (probed live), so when grok
    supplies the parsed `structuredOutput` object the adapter uses it
    and ignores text entirely."""
    body = verdict_body()
    data = json.loads(envelope(f"prose preamble\n{body}{body}"))
    data["structuredOutput"] = json.loads(body)
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": json.dumps(data) + "\n"}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: structured")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.raw == f"```json\n{body}\n```\n"


def test_prose_with_fenced_verdict_passes_verbatim(dirty_repo: Path, tmp_path: Path) -> None:
    """Text already carrying prose or a fence is not double-wrapped."""
    text = f"The work is sound.\n\n```json\n{verdict_body()}\n```\n"
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": envelope(text)}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: verbatim")
    assert result.verdict["verdict"] == "ACCEPTED"
    assert result.raw == text


def test_cancelled_run_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """The T3 sharp edge: a run that dies inside grok ends with exit 0,
    stopReason "cancelled", and narration-only text. The adapter must
    assert on the stop reason, never on exit status."""
    home = install_fake_grok(
        tmp_path,
        [
            INSPECT_OK,
            {
                "stdout": envelope(
                    "I attempted to run make verify but was stopped.",
                    stop="cancelled",
                )
            },
        ],
    )
    with pytest.raises(AdapterProcessError, match="cancelled"):
        conduct_review(dirty_repo, adapter_for(home), "wip: silent cancel")


def test_missing_stop_reason_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """An envelope with no stopReason cannot prove the run finished."""
    home = install_fake_grok(
        tmp_path, [INSPECT_OK, {"stdout": envelope(verdict_body(), stop=None)}]
    )
    with pytest.raises(AdapterProcessError, match=r"\(none\)"):
        conduct_review(dirty_repo, adapter_for(home), "wip: no stop reason")


def test_empty_text_is_an_empty_output_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A finished run with empty text is R1's empty output: never
    repaired, never recorded as a pass."""
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": envelope("")}])
    with pytest.raises(EmptyOutputError):
        conduct_review(dirty_repo, adapter_for(home), "wip: empty text")


def test_null_text_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A non-string text field is a broken envelope, never a crash."""
    stdout = json.dumps({"text": None, "sessionId": "sess-grok-1", "stopReason": "end_turn"})
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": stdout}])
    with pytest.raises(AdapterProcessError, match="text"):
        conduct_review(dirty_repo, adapter_for(home), "wip: null text")


def test_unparseable_envelope_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """Stdout that is not a JSON envelope is a transport failure."""
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": "not an envelope\n"}])
    with pytest.raises(AdapterProcessError):
        conduct_review(dirty_repo, adapter_for(home), "wip: broken envelope")


@pytest.mark.parametrize("stdout", ["[]\n", "null\n"])
def test_non_object_envelope_is_a_typed_error(
    dirty_repo: Path, tmp_path: Path, stdout: str
) -> None:
    """Valid JSON that is not an envelope object is a transport failure,
    not an AttributeError escaping the typed boundary."""
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": stdout}])
    with pytest.raises(AdapterProcessError):
        conduct_review(dirty_repo, adapter_for(home), "wip: non-object")


def test_backend_nonzero_exit_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A dying grok surfaces as AdapterProcessError carrying stderr."""
    home = install_fake_grok(
        tmp_path, [INSPECT_OK, {"stdout": "", "stderr": "grok: boom", "exit": 1}]
    )
    with pytest.raises(AdapterProcessError, match="boom"):
        conduct_review(dirty_repo, adapter_for(home), "wip: dead backend")


def test_missing_binary_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """An uninstalled grok is a transport failure, not a crash."""
    adapter = GrokAdapter(grok_bin=str(tmp_path / "no-such-grok"))
    with pytest.raises(AdapterProcessError, match="no-such-grok"):
        conduct_review(dirty_repo, adapter, "wip: missing binary")


def test_non_executable_binary_is_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """Any spawn failure is a transport failure: a configured binary that
    exists but lacks the exec bit must not escape as raw PermissionError."""
    binary = tmp_path / "grok-no-exec-bit"
    binary.write_text(FAKE_GROK)
    binary.chmod(0o644)
    adapter = GrokAdapter(grok_bin=str(binary))
    with pytest.raises(AdapterProcessError, match="grok-no-exec-bit"):
        conduct_review(dirty_repo, adapter, "wip: unspawnable binary")


def test_hung_backend_is_killed_into_a_typed_error(dirty_repo: Path, tmp_path: Path) -> None:
    """A hung grok is killed into a typed error, never a hang."""
    home = install_fake_grok(
        tmp_path, [INSPECT_OK, {"sleep": 30, "stdout": envelope(verdict_body())}]
    )
    start = time.monotonic()
    with pytest.raises(AdapterProcessError, match="[Tt]ime"):
        conduct_review(dirty_repo, adapter_for(home, timeout=1), "wip: hung backend")
    assert time.monotonic() - start < 15


def test_backend_cannot_wait_on_the_callers_stdin(dirty_repo: Path, tmp_path: Path) -> None:
    """A caller whose stdin never closes (a hook, a wrapper script) must
    not stall the review: the backend gets /dev/null, not our stdin."""
    home = install_fake_grok(
        tmp_path,
        [INSPECT_OK, {"read_stdin": True, "stdout": envelope(verdict_body())}],
    )
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


def test_envelope_without_session_id_is_a_continuity_error(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """No session id means the review can never be resumed (D1)."""
    home = install_fake_grok(
        tmp_path, [INSPECT_OK, {"stdout": envelope(verdict_body(), session=None)}]
    )
    with pytest.raises(ThreadContinuityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: no session")


def test_non_string_session_id_is_a_continuity_error(dirty_repo: Path, tmp_path: Path) -> None:
    """D1: a session id that is not a string can never be resumed (and
    would poison a later resume argv), so it degrades exactly like a
    missing one - ThreadContinuityError, never a leaked integer."""
    stdout = json.dumps({"text": verdict_body(), "sessionId": 123, "stopReason": "end_turn"})
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": stdout}])
    with pytest.raises(ThreadContinuityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: numeric session")


def test_repair_round_resumes_the_session(dirty_repo: Path, tmp_path: Path) -> None:
    """R2: the single repair goes back via --resume on the same id."""
    home = install_fake_grok(
        tmp_path,
        [
            INSPECT_OK,
            {"stdout": envelope("I forgot the verdict block.")},
            {"stdout": envelope(verdict_body())},
        ],
    )
    result = conduct_review(dirty_repo, adapter_for(home), "wip: repairable")
    assert result.verdict["verdict"] == "ACCEPTED"
    _, first, second = calls(home)
    assert "--resume" not in first["argv"]
    argv = second["argv"]
    assert argv[argv.index("--resume") + 1] == "sess-grok-1"
    assert "fenced JSON" in argv[argv.index("-p") + 1]


def test_shell_issued_edit_is_caught_by_the_integrity_check(
    dirty_repo: Path, tmp_path: Path
) -> None:
    """T10 acceptance: --tools narrows the path to an edit but bash stays
    enabled for make verify, so a shell-issued edit in the review checkout
    is caught by the integrity check (R7), not by the allowlist - even
    when the verdict itself accepts."""
    home = install_fake_grok(
        tmp_path,
        [
            INSPECT_OK,
            {"write": "sneaky.txt", "stdout": envelope(verdict_body())},
        ],
    )
    with pytest.raises(IntegrityError):
        conduct_review(dirty_repo, adapter_for(home), "wip: reviewer edit")


def test_costs_are_recorded_into_review_state(dirty_repo: Path, tmp_path: Path) -> None:
    """T10: grok reports total_cost_usd and usage per turn; both land on
    the round's ReviewResult in turn order - review state that any
    host-neutral writer can persist across separate round processes,
    not private adapter state. The calibration data for the round
    budget."""
    home = install_fake_grok(
        tmp_path,
        [
            INSPECT_OK,
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


def test_missing_telemetry_records_no_cost_entry(dirty_repo: Path, tmp_path: Path) -> None:
    """An envelope reporting neither cost nor usage contributes no
    entry - never a {None, None} placeholder polluting review state."""
    stdout = (
        json.dumps(
            {
                "text": verdict_body(),
                "sessionId": "sess-grok-1",
                "stopReason": "end_turn",
            }
        )
        + "\n"
    )
    home = install_fake_grok(tmp_path, [INSPECT_OK, {"stdout": stdout}])
    result = conduct_review(dirty_repo, adapter_for(home), "wip: no telemetry")
    assert result.costs == ()
