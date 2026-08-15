"""Tests for T4: the verdict schema and parser.

The reviewer's fenced JSON block is the verdict of record (TODO.md 4.2, R2).
This module pins:

- a shipped JSON Schema file, reusable verbatim as ``--json-schema`` (agy,
  grok) and ``--output-schema`` (codex exec) input;
- a parser that extracts the fenced block from reviewer prose and validates
  it, raising typed errors - never inferring a verdict from prose;
- empty output as a hard, non-repairable failure (R1: silent adapter
  failures must not read as a pass);
- exactly one repair round on repairable failures, then escalation.

The golden fixtures in tests/fixtures/reviewer-verdicts/ are the verdict
blocks all four hosts actually emitted in the T3 spike; the schema and
parser must accept every one of them.
"""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from review.verdict import (
    SCHEMA_PATH,
    EmptyOutputError,
    InvalidVerdictJSONError,
    MissingVerdictBlockError,
    MultipleVerdictBlocksError,
    SchemaViolationError,
    SemanticViolationError,
    VerdictError,
    parse_verdict,
    parse_with_repair,
    strict_schema,
    validate_verdict,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "reviewer-verdicts"
SKILL_PATH = REPO_ROOT / "skills" / "skeptical-reviewer" / "SKILL.md"

VALID_VERDICT: dict[str, Any] = {
    "schema_version": 1,
    "verdict": "REQUIRES_CHANGES",
    "round": 1,
    "blockers": [
        {
            "id": "gate-ignores-symlink",
            "file": "install.sh",
            "line": 120,
            "claim": "--check passes when the .agents/skills symlink is gone",
            "why": "codex and antigravity lose discovery while check reports clean",
        }
    ],
    "non_blockers": [],
}


def _review(verdict: dict[str, Any]) -> str:
    """Wrap a verdict dict in realistic reviewer prose with a fenced block."""
    return (
        "### 1. Requirement Summary\n\nNorms-only review.\n\n"
        "### 6. Machine-Readable Verdict\n\n"
        "```json\n" + json.dumps(verdict, indent=2) + "\n```\n"
    )


def _mutated(**changes: Any) -> dict[str, Any]:
    """A deep copy of VALID_VERDICT with top-level keys replaced or removed."""
    verdict = copy.deepcopy(VALID_VERDICT)
    for key, value in changes.items():
        if value is None:
            verdict.pop(key, None)
        else:
            verdict[key] = value
    return verdict


# --- the shipped schema file ------------------------------------------------


def test_schema_file_ships_the_verdict_contract() -> None:
    """The schema is a standalone JSON file hosts can consume verbatim.

    It must declare its dialect, require all five top-level keys (the agy
    spike showed empty keys get omitted unless required), and pin the
    verdict enum T3 locked in.
    """
    assert SCHEMA_PATH.is_file(), f"no schema file at {SCHEMA_PATH}"
    schema = json.loads(SCHEMA_PATH.read_text())
    assert "$schema" in schema, "schema does not declare its JSON Schema dialect"
    assert set(schema["required"]) == {
        "schema_version",
        "verdict",
        "round",
        "blockers",
        "non_blockers",
    }
    assert set(schema["properties"]["verdict"]["enum"]) == {
        "ACCEPTED",
        "ACCEPTED_WITH_NON_BLOCKERS",
        "REQUIRES_CHANGES",
    }


def test_golden_fixtures_from_all_four_hosts_validate() -> None:
    """Every verdict a real host emitted in the T3 spike passes validation."""
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    assert {p.stem for p in fixtures} == {"claude", "codex", "agy", "grok"}
    for path in fixtures:
        validate_verdict(json.loads(path.read_text()))


def test_skill_example_validates() -> None:
    """The example embedded in SKILL.md satisfies the schema it advertises."""
    blocks = re.findall(r"```json\s*\n(.*?)```", SKILL_PATH.read_text(), re.DOTALL)
    examples = [
        data
        for data in (json.loads(b) for b in blocks)
        if isinstance(data, dict) and "schema_version" in data
    ]
    assert examples, "SKILL.md no longer embeds a verdict example"
    for example in examples:
        validate_verdict(example)


# --- validation rules -------------------------------------------------------


def test_wrong_verdict_enum_is_a_schema_violation() -> None:
    """A verdict outside the locked enum is rejected, repairably."""
    with pytest.raises(SchemaViolationError) as excinfo:
        validate_verdict(_mutated(verdict="LGTM"))
    assert excinfo.value.repairable


def test_round_must_be_a_positive_integer() -> None:
    """round: 0 and a stringly-typed round are both rejected."""
    with pytest.raises(SchemaViolationError):
        validate_verdict(_mutated(round=0))
    with pytest.raises(SchemaViolationError):
        validate_verdict(_mutated(round="1"))


def test_omitted_non_blockers_key_is_rejected() -> None:
    """Both arrays are always required (the agy lesson from the T3 spike)."""
    with pytest.raises(SchemaViolationError):
        validate_verdict(_mutated(non_blockers=None))


def test_incomplete_finding_is_rejected() -> None:
    """Every finding must carry id, file, line, claim, and why."""
    blocker = dict(VALID_VERDICT["blockers"][0])
    del blocker["why"]
    with pytest.raises(SchemaViolationError):
        validate_verdict(_mutated(blockers=[blocker]))


def test_disposition_is_optional_but_constrained() -> None:
    """Re-review dispositions validate; values outside the enum do not."""
    resolved = dict(VALID_VERDICT["blockers"][0], disposition="resolved")
    validate_verdict(_mutated(round=2, blockers=[resolved]))
    bogus = dict(VALID_VERDICT["blockers"][0], disposition="wontfix")
    with pytest.raises(SchemaViolationError):
        validate_verdict(_mutated(round=2, blockers=[bogus]))


# --- parsing reviewer output ------------------------------------------------


def test_parse_returns_the_embedded_verdict() -> None:
    """A well-formed review parses to exactly the embedded verdict object."""
    assert parse_verdict(_review(VALID_VERDICT)) == VALID_VERDICT


def test_parse_ignores_non_verdict_json_blocks() -> None:
    """A quoted JSON block without schema_version does not confuse parsing."""
    text = (
        'The config under review:\n\n```json\n{"backend": "codex"}\n```\n\n'
        + _review(VALID_VERDICT)
    )
    assert parse_verdict(text) == VALID_VERDICT


def test_empty_output_is_a_hard_failure() -> None:
    """Empty or whitespace-only output raises a non-repairable error (R1)."""
    for text in ("", "   \n\t\n"):
        with pytest.raises(EmptyOutputError) as excinfo:
            parse_verdict(text)
        assert not excinfo.value.repairable


def test_prose_without_a_block_is_missing_and_repairable() -> None:
    """Prose-only output is never inferred from; it is a repairable miss."""
    with pytest.raises(MissingVerdictBlockError) as excinfo:
        parse_verdict("Looks great, ACCEPTED, ship it.")
    assert excinfo.value.repairable


def test_only_non_verdict_blocks_is_missing_and_repairable() -> None:
    """Parseable JSON blocks that are not verdicts do not become one."""
    text = 'Config quoted below.\n\n```json\n{"backend": "codex"}\n```\n'
    with pytest.raises(MissingVerdictBlockError) as excinfo:
        parse_verdict(text)
    assert excinfo.value.repairable


def test_broken_json_in_the_block_is_repairable() -> None:
    """A fenced block that fails to parse as JSON is repairable, not fatal."""
    text = 'Review.\n\n```json\n{"schema_version": 1,\n```\n'
    with pytest.raises(InvalidVerdictJSONError) as excinfo:
        parse_verdict(text)
    assert excinfo.value.repairable


def test_two_verdict_blocks_are_ambiguous() -> None:
    """Two verdict-shaped blocks cannot be silently disambiguated."""
    text = _review(VALID_VERDICT) + "\n" + _review(_mutated(verdict="ACCEPTED"))
    with pytest.raises(MultipleVerdictBlocksError) as excinfo:
        parse_verdict(text)
    assert excinfo.value.repairable


def test_schema_invalid_block_is_repairable() -> None:
    """parse_verdict surfaces schema violations with the repairable flag."""
    with pytest.raises(SchemaViolationError) as excinfo:
        parse_verdict(_review(_mutated(non_blockers=None)))
    assert excinfo.value.repairable


# --- the single repair round ------------------------------------------------


def test_repair_round_recovers_a_malformed_review() -> None:
    """One rerun with a concrete instruction turns a miss into a verdict."""
    calls: list[str] = []

    def rerun(instruction: str) -> str:
        calls.append(instruction)
        return _review(VALID_VERDICT)

    verdict = parse_with_repair("Prose only, no block.", rerun)
    assert verdict == VALID_VERDICT
    assert len(calls) == 1
    assert calls[0].strip(), "repair instruction must not be empty"
    assert "json" in calls[0].lower(), "instruction does not say what to emit"


def test_repair_round_escalates_after_one_retry() -> None:
    """A second bad output escalates; the reviewer is not retried forever."""
    calls: list[str] = []

    def rerun(instruction: str) -> str:
        calls.append(instruction)
        return "Still prose, still no block."

    with pytest.raises(VerdictError):
        parse_with_repair("Prose only, no block.", rerun)
    assert len(calls) == 1, "exactly one repair round is allowed"


def test_empty_output_is_not_repaired() -> None:
    """Empty output escalates immediately; no repair round is attempted."""
    calls: list[str] = []

    def rerun(instruction: str) -> str:
        calls.append(instruction)
        return _review(VALID_VERDICT)

    with pytest.raises(EmptyOutputError):
        parse_with_repair("", rerun)
    assert not calls, "R1: an empty review must never be retried into a pass"


# --- verdict semantics (the machine verdict must match its own findings) ----


def test_accepting_verdicts_with_blockers_is_contradictory() -> None:
    """An accepting verdict carrying blockers is rejected, repairably."""
    for verdict in ("ACCEPTED", "ACCEPTED_WITH_NON_BLOCKERS"):
        with pytest.raises(SemanticViolationError) as excinfo:
            validate_verdict(_mutated(verdict=verdict))
        assert excinfo.value.repairable


def test_requires_changes_without_blockers_is_contradictory() -> None:
    """REQUIRES_CHANGES must name at least one blocker."""
    with pytest.raises(SemanticViolationError):
        validate_verdict(_mutated(verdict="REQUIRES_CHANGES", blockers=[]))


def test_non_blocker_verdicts_match_the_non_blockers_array() -> None:
    """ACCEPTED means no notes; ACCEPTED_WITH_NON_BLOCKERS means notes."""
    note = dict(VALID_VERDICT["blockers"][0], id="minor-note")
    with pytest.raises(SemanticViolationError):
        validate_verdict(_mutated(verdict="ACCEPTED", blockers=[], non_blockers=[note]))
    with pytest.raises(SemanticViolationError):
        validate_verdict(
            _mutated(verdict="ACCEPTED_WITH_NON_BLOCKERS", blockers=[], non_blockers=[])
        )


def test_consistent_verdicts_validate() -> None:
    """The three verdicts each validate with matching findings arrays."""
    validate_verdict(_mutated(verdict="ACCEPTED", blockers=[], non_blockers=[]))
    note = dict(VALID_VERDICT["blockers"][0], id="minor-note")
    validate_verdict(
        _mutated(verdict="ACCEPTED_WITH_NON_BLOCKERS", blockers=[], non_blockers=[note])
    )
    validate_verdict(VALID_VERDICT)


def test_duplicate_finding_ids_are_rejected() -> None:
    """Finding ids must be unique across the whole review (both arrays)."""
    first = dict(VALID_VERDICT["blockers"][0])
    twin = dict(VALID_VERDICT["blockers"][0], line=2)
    with pytest.raises(SemanticViolationError):
        validate_verdict(_mutated(blockers=[first, twin]))
    cross = dict(VALID_VERDICT["blockers"][0])
    with pytest.raises(SemanticViolationError):
        validate_verdict(_mutated(non_blockers=[cross]))


def test_null_disposition_is_accepted() -> None:
    """A null disposition (strict-mode output for 'absent') validates."""
    nulled = dict(VALID_VERDICT["blockers"][0], disposition=None)
    validate_verdict(_mutated(blockers=[nulled]))


# --- the strict schema variant for codex --output-schema --------------------


def test_strict_schema_satisfies_codex_structured_output_rules() -> None:
    """The derived strict variant obeys the rules codex enforces.

    Verified live against codex 0.147.0 (TODO.md T4): every object node
    must set additionalProperties false and require every declared field,
    and every property schema must carry an explicit type. The canonical
    schema stays permissive; this variant is what the codex adapter ships.
    """

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
                for prop in node["properties"].values():
                    assert "type" in prop, f"untyped property schema: {prop}"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(strict_schema())


def test_codex_strict_output_fixture_validates() -> None:
    """The verdict codex emitted live under the strict schema validates.

    Captured 2026-08-15 from codex 0.147.0 with --output-schema; strict
    mode forces the disposition key, so it round-trips through the
    canonical validation the parser applies.
    """
    fixture = REPO_ROOT / "tests" / "fixtures" / "codex-strict-verdict.json"
    validate_verdict(json.loads(fixture.read_text()))
