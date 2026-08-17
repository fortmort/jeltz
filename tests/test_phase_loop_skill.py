"""Tests for T20: the tdd-phase-loop skill wired to the review loop.

The shipped `skills/tdd-phase-loop/SKILL.md` is consumed verbatim by four
hosts, so its contract IS its text (the T3/T5 precedent). T20 replaces
PHASE 3's terminal stop with PHASE 4 (REVIEW): the session itself runs
`review/run.sh --new`, applies the reviewer-response skill in the same
session on exit 10 (keeping the coder's context), resumes with
`--response-file`, and repeats until the reviewer accepts (exit 0) or
escalates (exit 20). The human approval gate moves to after convergence -
a task is only presented to the human once the machine loop has finished
with it. That closes Problem A: no terminal switching, no copy-paste.

These tests pin what the loop automation and the T15 bridge depend on:
- The four phases appear in RED -> GREEN -> REFACTOR -> REVIEW order and
  the phase-stop literals are exact 7-bit ASCII strings automation can
  match byte-for-byte.
- PHASE 3 no longer awaits review; its literal hands off to PHASE 4.
- PHASE 4 runs the same literal commands the bridge's denial instruction
  names, so a session recovering from a denied stop and a session
  following the skill walk one path.
- The instructed commands hand the reviewer the actual task context:
  `--todo-ref`, `--wip-message`, and `--verify-output` on `--new`, and
  fresh evidence again on every `--resume` - executed end to end against
  the scripted backend, asserting the packet the reviewer receives.
- Exit codes drive the loop: 0 stops for human approval, 10 invokes
  reviewer-response in-session, 20 stops with the escalation dossier.
- The response file carries the fixer's complete output including the
  machine-readable dispositions block (the orchestrator refuses a
  blockless file).
- The final commit message is updated after review-driven fixes, so the
  one message the human applies never describes a pre-review tree.
- The skill stays host-portable: ASCII, frontmatter, no Claude-specific
  `@` expansion syntax.
"""

import re
import shlex
from pathlib import Path

import pytest

from review.bridge import RECOVERY_INSTRUCTION
from tests.test_run import (
    HOSTILE_WIP_MESSAGE,
    calls,
    events,
    install_fake_codex,
    run_main,
    verdict_obj,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "tdd-phase-loop" / "SKILL.md"
SKILL_TEXT = SKILL_PATH.read_text()

PHASE_STOP_LITERALS = (
    "RED PHASE COMPLETE -- proceeding to GREEN.",
    "GREEN PHASE COMPLETE -- proceeding to REFACTOR.",
    "REFACTOR PHASE COMPLETE -- proceeding to REVIEW.",
    "REVIEW PHASE COMPLETE -- awaiting human approval.",
)

# The commands the T15 bridge hands a denied session. The skill must use
# the same literals so both entry paths into the loop are one path.
REVIEW_COMMANDS = (
    "review/run.sh --new",
    "review/run.sh --resume --response-file",
)

ESCALATION_DOSSIER = ".jeltz/review/escalation.md"


def _phase4_command(flag: str) -> str:
    """The full backticked review/run.sh command the skill instructs.

    The skill names each command exactly once, so the match is the
    command a session following the text will actually run.
    """
    matches = re.findall(rf"`(review/run\.sh {flag}[^`]*)`", SKILL_TEXT)
    assert len(matches) == 1, (
        f"expected exactly one backticked `review/run.sh {flag}` command, "
        f"found {len(matches)}"
    )
    return matches[0]


def test_frontmatter_serves_all_hosts() -> None:
    """YAML frontmatter with name and description survives the rewrite.

    All four hosts discover skills through this exact frontmatter shape
    (spike-verified, TODO.md section 3.3).
    """
    assert SKILL_TEXT.startswith("---\n"), "missing YAML frontmatter"
    frontmatter = SKILL_TEXT.split("---", 2)[1]
    assert re.search(r"^name:\s*tdd-phase-loop\s*$", frontmatter, re.MULTILINE)
    assert re.search(r"^description:\s*\S", frontmatter, re.MULTILINE)


def test_skill_is_seven_bit_ascii() -> None:
    """The skill text is pure 7-bit ASCII.

    The repo's shipped artifacts are ASCII-only; em dashes and curly
    quotes in the phase-stop literals would make them unmatchable from
    ASCII-only tooling.
    """
    assert SKILL_TEXT.isascii(), "skill text contains non-ASCII characters"


def test_no_claude_specific_at_references() -> None:
    """No @CLAUDE.md / @TODO.md expansion syntax anywhere in the skill.

    The @ prefix is Claude Code expansion; codex, agy, and grok receive
    it as a literal string, so the skill must use plain relative paths.
    """
    assert "@CLAUDE.md" not in SKILL_TEXT, "Claude-specific @CLAUDE.md reference"
    assert "@TODO.md" not in SKILL_TEXT, "Claude-specific @TODO.md reference"
    assert "@docs/" not in SKILL_TEXT, "Claude-specific @docs/ reference"


def test_phases_run_red_green_refactor_review_in_order() -> None:
    """Four phases exist and appear in workflow order.

    PHASE 4 (REVIEW) is the T20 addition: the machine review loop is a
    phase of the workflow, not a separate manual step.
    """
    headers = [
        "PHASE 1 - RED",
        "PHASE 2 - GREEN",
        "PHASE 3 - REFACTOR",
        "PHASE 4 - REVIEW",
    ]
    positions = [SKILL_TEXT.find(h) for h in headers]
    assert -1 not in positions, (
        f"missing phase header: {headers[positions.index(-1)]!r}"
    )
    assert positions == sorted(positions), "phase headers out of order"
    assert re.search(r"RED -> GREEN -> REFACTOR -> REVIEW", SKILL_TEXT), (
        "the four-phase order is not stated as a constraint"
    )


def test_phase_stop_literals_are_ascii() -> None:
    """The four phase-stop lines are exact 7-bit ASCII literals.

    Automation matches these byte-for-byte (the T5 precedent for the
    reviewer-response final-status lines).
    """
    for line in PHASE_STOP_LITERALS:
        assert line in SKILL_TEXT, f"phase-stop literal missing: {line!r}"


def test_refactor_hands_off_to_review_not_the_human() -> None:
    """PHASE 3's terminal stop is gone; REFACTOR proceeds to REVIEW.

    The pre-T20 literal ended the workflow at `awaiting review.`, which
    meant a terminal switch and a copy-paste. The only awaited party now
    is the human, after the machine loop converges.
    """
    assert "awaiting review." not in SKILL_TEXT, (
        "PHASE 3 still ends the workflow before the review loop"
    )
    assert re.search(r"automatically begin PHASE 4", SKILL_TEXT), (
        "REFACTOR does not hand off to PHASE 4"
    )


def test_review_phase_runs_the_bridge_commands() -> None:
    """PHASE 4 uses the same literal commands as the T15 denial.

    A session denied at stop and a session following the skill must walk
    the identical recovery path, so the commands are pinned in both
    places and joined here.
    """
    for command in REVIEW_COMMANDS:
        assert command in RECOVERY_INSTRUCTION, (
            f"bridge instruction lost command: {command!r}"
        )
        assert command in SKILL_TEXT, f"skill missing command: {command!r}"


def test_new_review_passes_task_context() -> None:
    """The instructed `--new` command hands the reviewer the task.

    A bare `review/run.sh --new` reviews under the CLI defaults - a
    placeholder WIP message, no TODO ref, and "verification not run" -
    which demotes the skeptical review to a norms-only pass. The skill
    must pass the real context: the completed TODO item, the PHASE 3
    final commit message, and the saved `make verify` output.
    """
    command = _phase4_command("--new")
    for flag in ("--todo-ref", "--wip-message-file", "--verify-output"):
        assert flag in command, f"`--new` command missing {flag}"
    assert "--wip-message " not in SKILL_TEXT and '--wip-message "' not in SKILL_TEXT, (
        "the message is interpolated into shell syntax; a commit message "
        "contains backticks, $(), and quotes the shell would expand or "
        "mangle - it must travel by file"
    )


def test_resume_passes_fresh_evidence() -> None:
    """Every `--resume` re-supplies the WIP message and fresh evidence.

    Only the task ref persists in review state; the WIP message falls
    back to the CLI placeholder on resume, and the exit-10 fixes changed
    the code, so the skill must re-run `make verify` and pass its output
    again alongside the response file.
    """
    command = _phase4_command("--resume --response-file")
    for flag in ("--wip-message-file", "--verify-output"):
        assert flag in command, f"`--resume` command missing {flag}"
    assert re.search(r"re-run[^.]*`make verify`", SKILL_TEXT, re.IGNORECASE), (
        "the skill never re-runs make verify after exit-10 fixes"
    )


def test_instructed_new_invocation_reaches_reviewer_with_context(
    dirty_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running the skill's `--new` command builds a contextful packet.

    Executes the acceptance criterion instead of describing it: take the
    exact command the skill instructs, substitute its placeholders, run
    it against the scripted backend, and assert the packet the reviewer
    received carries the TODO ref, the real WIP message, and the verify
    evidence - not the CLI defaults.
    """
    verify_file = tmp_path / "verify.txt"
    verify_file.write_text("387 passed in 220.16s\nTotal coverage: 100.00%\n")
    message_file = tmp_path / "wip-message.txt"
    message_file.write_text(HOSTILE_WIP_MESSAGE)
    command = (
        _phase4_command("--new")
        .replace("<todo-ref>", "T42. Wire the frobnicator")
        .replace("<message-file>", str(message_file))
        .replace("<verify-file>", str(verify_file))
    )
    home = install_fake_codex(tmp_path, [{"stdout": events(verdict_obj())}])
    argv = shlex.split(command)[1:] + ["--repo", str(dirty_repo)]
    assert run_main(monkeypatch, home, argv) == 0
    capsys.readouterr()
    packet = calls(home)[0]["argv"][-1]
    assert "T42. Wire the frobnicator" in packet, "TODO ref missing from packet"
    assert HOSTILE_WIP_MESSAGE in packet, "WIP message not preserved byte-for-byte"
    assert "387 passed in 220.16s" in packet, "verify evidence missing from packet"
    assert "WIP under review" not in packet, "packet fell back to the CLI default"


def test_evidence_files_stay_out_of_the_reviewed_tree() -> None:
    """The saved evidence lives under `.jeltz/review/`, not the tree.

    Any other in-repo location would enter the packet's untracked list
    and the diff hash, so writing the verify output would itself dirty
    the tree under review. `.jeltz/` is the one path the packet builder
    excludes.
    """
    assert re.search(
        r"(verify|response)[^.]*under `\.jeltz/review/`"
        r"|`\.jeltz/review/`[^.]*(verify|response)",
        SKILL_TEXT,
        re.IGNORECASE,
    ), "no instruction keeping the evidence files under .jeltz/review/"


def test_exit_codes_drive_the_loop() -> None:
    """Exit 0 converges, exit 10 fixes in-session, exit 20 escalates."""
    assert re.search(r"exit 0[^.\n]*accept", SKILL_TEXT, re.IGNORECASE), (
        "exit 0 is not tied to acceptance"
    )
    assert re.search(r"exit 10[^.\n]*reviewer-response", SKILL_TEXT, re.IGNORECASE), (
        "exit 10 does not invoke reviewer-response"
    )
    assert re.search(r"exit 20[^.\n]*(escalat|human)", SKILL_TEXT, re.IGNORECASE), (
        "exit 20 does not stop for a human"
    )
    assert ESCALATION_DOSSIER in SKILL_TEXT, "the escalation dossier path is not named"
    assert re.search(r"[Rr]epeat[^.\n]*exit 0[^.\n]*exit 20", SKILL_TEXT), (
        "the loop's termination condition is not stated"
    )


def test_fixer_runs_in_session() -> None:
    """reviewer-response runs in the same session, keeping the context.

    The whole point of T20: the coder's context (the TODO item, the
    diff, the reasoning) stays available to the fixer. Spawning a fresh
    session or asking the human to relay findings recreates Problem A.
    """
    assert re.search(
        r"reviewer-response[^.]*\b(same|this) session", SKILL_TEXT, re.IGNORECASE
    ) or re.search(
        r"\b(same|this) session[^.]*reviewer-response", SKILL_TEXT, re.IGNORECASE
    ), "reviewer-response is not required to run in the same session"


def test_response_file_carries_the_dispositions_block() -> None:
    """The saved response includes the machine-readable block.

    `review/run.sh --resume` validates the response file before dispatch
    and refuses a blockless one, so the skill must say the file carries
    the complete reviewer-response output including the fenced
    dispositions block.
    """
    assert re.search(
        r"response file[^.]*complete", SKILL_TEXT, re.IGNORECASE
    ) or re.search(r"complete[^.]*response file", SKILL_TEXT, re.IGNORECASE), (
        "the response file is not required to be the complete output"
    )
    assert re.search(r"\bdispositions\b", SKILL_TEXT), (
        "the machine-readable dispositions block is never mentioned"
    )


def test_commit_message_survives_review_fixes() -> None:
    """Exit-10 fixes update the final commit message before the STOP.

    PHASE 3 writes the message before PHASE 4 may change code and tests;
    merely restating it after convergence would hand the human a message
    describing a pre-review tree. The skill must require updating it to
    cover the review-driven changes, and the terminal summary presents
    that final form.
    """
    assert re.search(
        r"update[^.]*final commit message[^.]*review", SKILL_TEXT, re.IGNORECASE
    ), "the commit message is not updated after review-driven fixes"
    assert "The final commit message from PHASE 3" not in SKILL_TEXT, (
        "the terminal summary still restates the pre-review message"
    )


def test_human_gate_moves_after_convergence() -> None:
    """Human approval happens once, after the machine loop converges.

    Before T20 the human reviewed raw REFACTOR output; now the human
    approves work the reviewer has already accepted (or arbitrates an
    escalation). The skill must state both the single human stop and its
    new position.
    """
    assert re.search(
        r"human[^.\n]*after[^.\n]*(accept|converg)", SKILL_TEXT, re.IGNORECASE
    ) or re.search(
        r"(accept\w*|converg\w*)[^.\n]*before[^.\n]*human", SKILL_TEXT, re.IGNORECASE
    ), "the human gate is not placed after convergence"
    assert re.search(r"[Oo]nly the final STOP requires human", SKILL_TEXT), (
        "the single-human-stop rule is gone"
    )


def test_commit_discipline_survives() -> None:
    """The skill still forbids committing and still ends with one message.

    The review loop reviews a WIP commit made inside a disposable
    worktree by the orchestrator; the session itself must still never
    commit, and the single final commit message remains the deliverable
    the human applies by hand.
    """
    assert re.search(r"must not commit", SKILL_TEXT, re.IGNORECASE), (
        "the no-commit rule is gone"
    )
    assert re.search(
        r"(one|single|a)\b[^.\n]*final commit message", SKILL_TEXT, re.IGNORECASE
    ), "the single final commit message is gone"
