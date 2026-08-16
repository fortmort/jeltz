"""Extract and validate the skeptical-reviewer verdict block.

The fenced JSON block ending a review is the verdict of record; prose is for
humans and is never inferred from (TODO.md R2). Empty reviewer output is a
hard failure so a silently dead adapter cannot read as a pass (R1). Every
other failure mode is repairable exactly once via ``parse_with_repair``.
"""

import json
import re
from pathlib import Path
from typing import Any, Callable

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parent / "verdict.schema.json"

_FENCED_JSON = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


class VerdictError(Exception):
    """Base for every verdict extraction or validation failure.

    Attributes:
        repairable: Whether one reviewer repair round may be attempted.
    """

    repairable: bool = True


class EmptyOutputError(VerdictError):
    """The reviewer produced no output at all; never repaired (R1)."""

    repairable = False


class MissingVerdictBlockError(VerdictError):
    """Output contains prose but no verdict-shaped fenced JSON block."""


class InvalidVerdictJSONError(VerdictError):
    """A fenced JSON block exists but does not parse as JSON."""


class MultipleVerdictBlocksError(VerdictError):
    """More than one verdict-shaped block; the verdict is ambiguous."""


class SchemaViolationError(VerdictError):
    """The verdict block parses as JSON but violates the schema."""


class SemanticViolationError(VerdictError):
    """The verdict block is schema-valid but internally contradictory."""


class WrongRoundError(VerdictError):
    """The verdict declares a different round than the one being run.

    The declared round is load-bearing: dispositions may deactivate
    blockers only in round 2+, so a round-1 reviewer declaring a later
    round could launder unresolved blockers into an acceptance (T12).
    """


def load_schema() -> dict[str, Any]:
    """Load the shipped verdict JSON Schema.

    Returns:
        The schema document as a dict.
    """
    return json.loads(SCHEMA_PATH.read_text())


def draftless_schema() -> dict[str, Any]:
    """Derive the variant claude --json-schema requires (T11).

    Verified live against claude 2.1.233: its validator rejects any
    schema declaring the 2020-12 draft ("no schema with key or ref
    https://json-schema.org/draft/2020-12/schema"), while the schema
    body - $id and $defs included - validates unchanged. Dropping the
    declaration is the whole transform.

    Returns:
        A copy of the canonical schema without its $schema declaration.
    """
    schema = load_schema()
    del schema["$schema"]
    return schema


def validate_verdict(data: dict[str, Any]) -> None:
    """Validate a parsed verdict object against the shipped schema.

    Args:
        data: The candidate verdict object.

    Raises:
        SchemaViolationError: If the object violates the schema.
    """
    try:
        jsonschema.validate(data, load_schema())
    except jsonschema.ValidationError as exc:
        raise SchemaViolationError(exc.message) from exc
    _check_semantics(data)


def _check_semantics(data: dict[str, Any]) -> None:
    """Reject schema-valid verdicts that contradict their own findings.

    The verdict is the gate's record of truth and finding ids drive
    remediation and thrash tracking, so a verdict/array mismatch or a
    duplicated id is a correctness failure, not polish.

    A blocker is *active* unless its disposition is ``resolved`` - and a
    disposition can mark a blocker inactive only in a re-review (round
    2+), because dispositions judge prior blockers and round one has
    none; a round-one blocker is active whatever its disposition claims
    (hosts using strict structured output are forced to emit the key on
    fresh findings). Resolved prior blockers stay listed under their
    stable ids in an accepting re-review (the T5 join depends on those
    ids surviving the success case); only active blockers justify - and
    are required by - ``REQUIRES_CHANGES``.

    Args:
        data: A schema-valid verdict object.

    Raises:
        SemanticViolationError: If the verdict contradicts its findings
            arrays or reuses a finding id within the review.
    """
    verdict = data["verdict"]
    blockers = data["blockers"]
    non_blockers = data["non_blockers"]
    rereview = data["round"] > 1
    active = [
        f for f in blockers if not (rereview and f.get("disposition") == "resolved")
    ]
    if verdict == "REQUIRES_CHANGES":
        if not active:
            raise SemanticViolationError("REQUIRES_CHANGES with no active blockers")
    elif active:
        raise SemanticViolationError(f"{verdict} with active (non-resolved) blockers")
    if verdict == "ACCEPTED" and non_blockers:
        raise SemanticViolationError("ACCEPTED with non-empty non_blockers")
    if verdict == "ACCEPTED_WITH_NON_BLOCKERS" and not non_blockers:
        raise SemanticViolationError("ACCEPTED_WITH_NON_BLOCKERS with no non_blockers")
    ids = [finding["id"] for finding in blockers + non_blockers]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise SemanticViolationError(
            f"finding ids reused within the review: {sorted(duplicates)}"
        )


def strict_schema() -> dict[str, Any]:
    """Derive the strict variant codex --output-schema requires.

    Verified live against codex 0.147.0 (TODO.md T4): OpenAI structured
    output rejects any object without ``additionalProperties: false`` and a
    ``required`` list naming every declared property, and any property
    schema without an explicit ``type``. The canonical schema already types
    every property and makes ``disposition`` nullable, so hardening is
    purely mechanical; optional-in-canonical keys become required-but-null.

    Returns:
        A deep, hardened copy of the canonical schema.
    """
    schema = load_schema()
    _harden(schema)
    return schema


def _harden(node: Any) -> None:
    """Recursively close objects and require every declared property."""
    if isinstance(node, dict):
        if "properties" in node:
            node["additionalProperties"] = False
            node["required"] = sorted(node["properties"])
        for value in node.values():
            _harden(value)
    elif isinstance(node, list):
        for value in node:
            _harden(value)


def parse_verdict(text: str) -> dict[str, Any]:
    """Extract and validate the single verdict block from reviewer output.

    Args:
        text: The reviewer's complete output.

    Returns:
        The validated verdict object.

    Raises:
        EmptyOutputError: Output is empty or whitespace; not repairable.
        MissingVerdictBlockError: No verdict-shaped fenced JSON block.
        InvalidVerdictJSONError: Fenced JSON present but unparseable.
        MultipleVerdictBlocksError: More than one verdict-shaped block.
        SchemaViolationError: The block violates the verdict schema.
    """
    if not text.strip():
        raise EmptyOutputError("reviewer produced no output")
    blocks = _FENCED_JSON.findall(text)
    if not blocks:
        raise MissingVerdictBlockError("no fenced JSON block in reviewer output")
    candidates: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError as exc:
            parse_errors.append(str(exc))
            continue
        if isinstance(data, dict) and "schema_version" in data:
            candidates.append(data)
    if not candidates:
        if parse_errors:
            raise InvalidVerdictJSONError(parse_errors[0])
        raise MissingVerdictBlockError(
            "no fenced JSON block carries a schema_version key"
        )
    if len(candidates) > 1:
        raise MultipleVerdictBlocksError(
            f"{len(candidates)} verdict-shaped blocks; expected exactly one"
        )
    validate_verdict(candidates[0])
    return candidates[0]


def parse_with_repair(
    text: str,
    rerun: Callable[[str], str],
    expected_round: int | None = None,
) -> dict[str, Any]:
    """Parse reviewer output, allowing exactly one repair round.

    Args:
        text: The reviewer's complete output.
        rerun: Callback that sends a repair instruction back to the same
            reviewer thread and returns its new output.
        expected_round: When given, the round the verdict must declare;
            a mismatch is repairable like any other verdict defect.

    Returns:
        The validated verdict object.

    Raises:
        VerdictError: If the output is empty (never repaired), or if the
            repaired output still fails to parse or validate.
        WrongRoundError: If the (possibly repaired) verdict declares a
            round other than expected_round.
    """

    def checked(candidate: str) -> dict[str, Any]:
        data = parse_verdict(candidate)
        if expected_round is not None and data["round"] != expected_round:
            raise WrongRoundError(
                f"verdict declares round {data['round']}, "
                f"but this is review round {expected_round}"
            )
        return data

    try:
        return checked(text)
    except VerdictError as exc:
        if not exc.repairable:
            raise
        instruction = (
            "Your review is missing a valid machine-readable verdict "
            f"({exc}). Re-emit ONLY the verdict as exactly one fenced JSON "
            "block matching the documented schema: schema_version, verdict, "
            "round, blockers, non_blockers - both arrays always present."
        )
        return checked(rerun(instruction))
