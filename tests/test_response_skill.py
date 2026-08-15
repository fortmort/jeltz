"""Tests for T5: the revised reviewer-response skill contract.

Like the skeptical-reviewer skill (T3), this SKILL.md is consumed verbatim by
four hosts, so its contract IS its text. These tests pin what the review loop
automation depends on: findings are consumed by the verdict's stable finding
ids; the fixer emits a per-id disposition (fixed / rejected-invalid /
deferred-non-blocker) with a reason; the output embeds a machine-readable
response block that the orchestrator can diff against the next round's verdict
by id; the deadlock rule from termination condition 3 (reviewer re-asserts a
finding the fixer rejected -> escalate to a human) is stated; and silent scope
expansion is forbidden by requiring every change to map to a finding id.
"""

import json
import re
from pathlib import Path

from review.verdict import validate_verdict

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "reviewer-response" / "SKILL.md"
SKILL_TEXT = SKILL_PATH.read_text()

RESPONSE_TOP_KEYS = {"schema_version", "round", "dispositions"}
DISPOSITION_KEYS = {"id", "disposition", "reason"}
DISPOSITION_ENUM = {"fixed", "rejected-invalid", "deferred-non-blocker"}

FINAL_STATUS_LINES = (
    "ALL FINDINGS ADDRESSED -- ready for re-review.",
    "ONLY NON-BLOCKERS REMAIN -- ready for acceptance.",
    "INVALID FINDINGS ONLY -- no changes required.",
)


def _fenced_json_blocks(text: str) -> list[str]:
    """Return the contents of every ```json fenced block in the text."""
    return re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)


def _example_responses() -> list[dict]:
    """Parse every fenced JSON block that looks like a response example."""
    responses = []
    for block in _fenced_json_blocks(SKILL_TEXT):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "dispositions" in data:
            responses.append(data)
    return responses


def test_frontmatter_serves_all_hosts() -> None:
    """YAML frontmatter with name and description survives the rewrite.

    All four hosts discover skills through this exact frontmatter shape
    (spike-verified, TODO.md section 3.3).
    """
    assert SKILL_TEXT.startswith("---\n"), "missing YAML frontmatter"
    frontmatter = SKILL_TEXT.split("---", 2)[1]
    assert re.search(r"^name:\s*reviewer-response\s*$", frontmatter, re.MULTILINE)
    assert re.search(r"^description:\s*\S", frontmatter, re.MULTILINE)


def test_skill_is_seven_bit_ascii() -> None:
    """The skill text is pure 7-bit ASCII.

    The repo's shipped artifacts are ASCII-only; curly quotes and em dashes
    in the current file would also make the literal final-status strings
    unmatchable from ASCII-only tooling.
    """
    assert SKILL_TEXT.isascii(), "skill text contains non-ASCII characters"


def test_no_claude_specific_at_references() -> None:
    """No @CLAUDE.md / @TODO.md expansion syntax anywhere in the skill.

    The @ prefix is Claude Code expansion; codex, agy, and grok receive it
    as a literal string, so the skill must use plain relative paths.
    """
    assert "@CLAUDE.md" not in SKILL_TEXT, "Claude-specific @CLAUDE.md reference"
    assert "@TODO.md" not in SKILL_TEXT, "Claude-specific @TODO.md reference"


def test_findings_are_consumed_by_id() -> None:
    """The skill keys its work on the verdict's stable finding ids.

    Thrash detection (termination condition 2) and the deadlock rule
    (condition 3) both join on finding id, so classification and fixes must
    reference the reviewer's ids, not paraphrased finding titles.
    """
    assert re.search(r"\bfinding id\b", SKILL_TEXT, re.IGNORECASE), (
        "the skill never mentions finding ids"
    )
    assert re.search(r"\bby id\b", SKILL_TEXT, re.IGNORECASE), (
        "findings are not addressed by id"
    )


def test_per_id_dispositions_with_reasons() -> None:
    """Each finding id receives one disposition from the fixed enum plus a reason."""
    for disposition in sorted(DISPOSITION_ENUM):
        assert disposition in SKILL_TEXT, f"disposition '{disposition}' missing"
    assert re.search(r"\breason\b", SKILL_TEXT, re.IGNORECASE), (
        "dispositions do not require a reason"
    )


def test_deadlock_rule_escalates_reasserted_rejections() -> None:
    """Termination condition 3 is stated: rejection + re-assertion escalates.

    When the fixer rejects a finding as invalid and the reviewer re-asserts
    it in the next round, that is genuine disagreement needing a human
    tiebreak, not another remediation round.
    """
    assert re.search(r"re-?assert", SKILL_TEXT, re.IGNORECASE), (
        "no mention of the reviewer re-asserting a rejected finding"
    )
    assert re.search(r"\bescalat\w+\b[^.\n]*\bhuman\b", SKILL_TEXT, re.IGNORECASE), (
        "re-assertion does not escalate to a human"
    )


def test_silent_scope_expansion_forbidden() -> None:
    """Every change must be attributable to a finding id; no silent expansion.

    Undeclared extra changes are what make round counts explode: they hand
    the next review round new surface that no finding asked for.
    """
    assert re.search(
        r"(every|each)[^.\n]*\bchange\b[^.\n]*\bfinding\b", SKILL_TEXT, re.IGNORECASE
    ), "changes are not required to map to a specific finding"
    assert re.search(r"\bsilent\w*\b[^.\n]*\bscope\b", SKILL_TEXT, re.IGNORECASE), (
        "silent scope expansion is not called out"
    )


def test_response_block_example_parses() -> None:
    """The skill embeds a fenced JSON response block example.

    The block carries schema_version, round, and a dispositions array keyed
    by finding id -- the machine-readable half of the output that the
    orchestrator diffs against the next round's verdict.
    """
    responses = _example_responses()
    assert responses, "no fenced JSON response example with a dispositions key"
    for response in responses:
        assert RESPONSE_TOP_KEYS <= response.keys(), (
            f"response example missing keys: {RESPONSE_TOP_KEYS - response.keys()}"
        )
        assert response["dispositions"], "response example has no dispositions"
        for entry in response["dispositions"]:
            assert DISPOSITION_KEYS <= entry.keys(), (
                f"disposition entry missing: {DISPOSITION_KEYS - entry.keys()}"
            )
            assert entry["disposition"] in DISPOSITION_ENUM, (
                f"disposition '{entry['disposition']}' not in {DISPOSITION_ENUM}"
            )


def test_output_diffable_against_next_verdict_by_id() -> None:
    """The skill states the diffability contract with the next round's verdict.

    The next round's verdict judges each prior blocker id as resolved /
    unresolved / regressed; the response's per-id dispositions must use the
    same ids so the two documents join mechanically.
    """
    assert re.search(
        r"next round[^.\n]*\bverdict\b|verdict[^.\n]*next round",
        SKILL_TEXT,
        re.IGNORECASE,
    ), "no stated relationship to the next round's verdict"
    assert re.search(r"\bsame id", SKILL_TEXT, re.IGNORECASE), (
        "id continuity with the verdict is not required"
    )


def test_successful_round_two_join_validates() -> None:
    """The example response joins with a schema-valid accepting re-review.

    This executes the acceptance criterion instead of merely describing it:
    build the round-two verdict the skill's example response predicts -
    every `fixed` id returns as a prior blocker disposed `resolved`, the
    `deferred-non-blocker` id stays a non-blocker - and run it through the
    T4 validator. The join must be representable precisely in the success
    case, when every blocker was fixed and the verdict accepts.
    """

    def _finding(fid: str, disposition: str | None) -> dict:
        return {
            "id": fid,
            "file": "install.sh",
            "line": 1,
            "claim": "as stated in round one",
            "why": "as stated in round one",
            "disposition": disposition,
        }

    response = _example_responses()[0]
    fixed = [e["id"] for e in response["dispositions"] if e["disposition"] == "fixed"]
    deferred = [
        e["id"]
        for e in response["dispositions"]
        if e["disposition"] == "deferred-non-blocker"
    ]
    assert fixed and deferred, (
        "the example must exercise both verdict-joinable dispositions"
    )
    next_verdict = {
        "schema_version": 1,
        "verdict": "ACCEPTED_WITH_NON_BLOCKERS",
        "round": response["round"] + 1,
        "blockers": [_finding(fid, "resolved") for fid in fixed],
        "non_blockers": [_finding(fid, None) for fid in deferred],
    }
    validate_verdict(next_verdict)
    resolved_ids = {
        b["id"] for b in next_verdict["blockers"] if b["disposition"] == "resolved"
    }
    assert set(fixed) <= resolved_ids, "a fixed id is missing from the join"


def test_final_status_lines_are_ascii_literals() -> None:
    """The three final-status lines survive in exact 7-bit ASCII form.

    Automation (T12's orchestrator, T20's loop) matches these literally, so
    they must be byte-stable and typeable from ASCII tooling.
    """
    for line in FINAL_STATUS_LINES:
        assert line in SKILL_TEXT, f"final status line missing: {line!r}"
