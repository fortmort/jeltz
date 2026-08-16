"""Escalation policy engine: when automated review must stop (T13).

Section 4.2 caps a review at three rounds and names four termination
conditions: (1) round three still ends REQUIRES_CHANGES, (2) thrash - a
blocker the coder claimed fixed comes back, (3) dispute - a blocker the
coder rejected as invalid is re-asserted, (4) the packet exceeded the
size ceiling. Conditions 2 and 3 join the coder's reviewer-response
disposition block (T5 section E) against the next round's verdict by
stable finding ids. Every escalation renders a dossier for the human
tiebreak: the disputed blockers, both sides' positions, and the round
history.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from review.verdict import FENCED_JSON

MAX_ROUNDS = 3

DOSSIER_PATH = Path(".jeltz") / "review" / "escalation.md"

RESPONSE_DISPOSITIONS = ("fixed", "rejected-invalid", "deferred-non-blocker")

CONDITION_DESCRIPTIONS = {
    1: "round cap reached with blockers still open",
    2: "thrash: a blocker the coder claimed fixed came back",
    3: "dispute: a blocker the coder rejected as invalid was re-asserted",
    4: "the change was too large to review",
}


class ResponseError(Exception):
    """The reviewer-response disposition block is missing or malformed."""


@dataclass(frozen=True)
class DisputedBlocker:
    """One blocker the escalation dossier must present to a human."""

    blocker_id: str
    condition: int
    coder_position: str
    reviewer_position: str


@dataclass(frozen=True)
class Escalation:
    """The decision to stop: which conditions fired, over which blockers."""

    conditions: tuple[int, ...]
    blockers: tuple[DisputedBlocker, ...]
    detail: str = ""


def parse_response(text: str) -> dict[str, Any]:
    """Extract and validate the coder's disposition block (T5 section E).

    Accepts either the bare JSON object or full fixer output containing
    exactly one fenced JSON block carrying the `dispositions` key - the
    pinned detection key, so a verdict block in the same output is never
    mistaken for the response.

    Raises:
        ResponseError: If no block, more than one block, or a malformed
            block is found.
    """
    candidates = []
    for block in FENCED_JSON.findall(text):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "dispositions" in data:
            candidates.append(data)
    if not candidates:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "dispositions" in data:
            candidates.append(data)
    if not candidates:
        raise ResponseError("no dispositions block in the response")
    if len(candidates) > 1:
        raise ResponseError(f"{len(candidates)} dispositions blocks; expected one")
    return _validated_response(candidates[0])


def _validated_response(data: dict[str, Any]) -> dict[str, Any]:
    """Check the response block against the pinned T5 contract."""
    if data.get("schema_version") != 1:
        raise ResponseError(f"unknown schema_version: {data.get('schema_version')!r}")
    if not isinstance(data.get("round"), int):
        raise ResponseError("round must be an integer")
    if not isinstance(data["dispositions"], list):
        raise ResponseError("dispositions must be a list")
    for entry in data["dispositions"]:
        if not isinstance(entry, dict):
            raise ResponseError(f"disposition entry is not an object: {entry!r}")
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ResponseError("disposition entry needs a non-empty id")
        if entry.get("disposition") not in RESPONSE_DISPOSITIONS:
            raise ResponseError(
                f"unknown disposition for {entry['id']}: {entry.get('disposition')!r}"
            )
        if not isinstance(entry.get("reason"), str):
            raise ResponseError(f"disposition for {entry['id']} needs a reason")
    ids = [entry["id"] for entry in data["dispositions"]]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ResponseError(f"duplicate disposition ids: {sorted(duplicates)}")
    return data


def verify_coverage(response: dict[str, Any], verdict: dict[str, Any]) -> None:
    """Check the response answers the verdict's findings exactly (T5).

    The contract requires every finding id from the verdict exactly once;
    a partial response could silently omit the claimed-fixed or rejected
    blocker whose comeback conditions 2 and 3 key on.

    Args:
        response: A parsed disposition block.
        verdict: The verdict the response claims to answer.

    Raises:
        ResponseError: Naming the unanswered or unknown finding ids.
    """
    expected = {f["id"] for f in verdict["blockers"] + verdict["non_blockers"]}
    answered = {d["id"] for d in response["dispositions"]}
    missing = sorted(expected - answered)
    if missing:
        raise ResponseError(f"response leaves findings unanswered: {missing}")
    unknown = sorted(answered - expected)
    if unknown:
        raise ResponseError(f"response answers unknown finding ids: {unknown}")


def _reviewer_position(blocker: dict[str, Any]) -> str:
    """The reviewer's side of a finding, quoted from the verdict."""
    disposition = blocker.get("disposition") or "none"
    return f"{blocker['claim']} ({blocker['why']}); disposition: {disposition}"


def evaluate(
    state: dict[str, Any], response: dict[str, Any] | None = None
) -> Escalation | None:
    """Decide whether the just-completed round terminates the review.

    Args:
        state: Review state as the orchestrator records it: the round
            number and the round's full validated verdict.
        response: The coder's parsed disposition block answering the
            previous round, or None when no response was supplied.

    Returns:
        The escalation (conditions 1-3) or None to let the loop continue.

    Raises:
        ValueError: If the response answers a round other than the one
            preceding this verdict - a caller bug, never a policy call.
    """
    round_number = state["round"]
    verdict = state["verdict"]
    if response is not None and response["round"] != round_number - 1:
        raise ValueError(
            f"response answers round {response['round']}, "
            f"but this verdict ends round {round_number}"
        )
    blockers_by_id = {b["id"]: b for b in verdict["blockers"]}
    claims = {d["id"]: d for d in (response or {}).get("dispositions", [])}
    conditions: set[int] = set()
    disputed: list[DisputedBlocker] = []
    seen: set[str] = set()

    def dispute(condition: int, blocker: dict[str, Any], coder: str) -> None:
        conditions.add(condition)
        seen.add(blocker["id"])
        disputed.append(
            DisputedBlocker(
                blocker_id=blocker["id"],
                condition=condition,
                coder_position=coder,
                reviewer_position=_reviewer_position(blocker),
            )
        )

    for claim in claims.values():
        blocker = blockers_by_id.get(claim["id"])
        if blocker is None:
            continue
        coder = f"{claim['disposition']}: {claim['reason']}"
        if claim["disposition"] == "rejected-invalid":
            dispute(3, blocker, coder)
        elif (
            claim["disposition"] == "fixed" and blocker.get("disposition") != "resolved"
        ):
            dispute(2, blocker, coder)
    for blocker in verdict["blockers"]:
        if blocker.get("disposition") == "regressed" and blocker["id"] not in seen:
            dispute(2, blocker, "claimed fixed in an earlier round")
    if round_number >= MAX_ROUNDS and verdict["verdict"] == "REQUIRES_CHANGES":
        conditions.add(1)
        for blocker in verdict["blockers"]:
            if blocker["id"] in seen:
                continue
            if blocker.get("disposition") == "resolved":
                continue
            claim = claims.get(blocker["id"])
            coder = (
                f"{claim['disposition']}: {claim['reason']}"
                if claim
                else "no position recorded"
            )
            dispute(1, blocker, coder)
    if not conditions:
        return None
    return Escalation(conditions=tuple(sorted(conditions)), blockers=tuple(disputed))


def packet_escalation(detail: str) -> Escalation:
    """Condition 4: the packet was too large to review at all."""
    return Escalation(conditions=(4,), blockers=(), detail=detail)


def render_dossier(escalation: Escalation, history: list[dict[str, Any]]) -> str:
    """Render the human-facing escalation dossier as markdown.

    Args:
        escalation: The termination decision to present.
        history: The per-round records from review state, oldest first.

    Returns:
        The dossier text: conditions, disputed blockers with both sides'
        positions, and the round history.
    """
    lines = [
        "# Escalation dossier",
        "",
        "Automated review has terminated; a human decision is required.",
        "",
        "## Termination conditions",
        "",
    ]
    for condition in escalation.conditions:
        lines.append(f"- condition {condition}: {CONDITION_DESCRIPTIONS[condition]}")
    if escalation.detail:
        lines.append(f"  ({escalation.detail})")
    lines += ["", "## Disputed blockers", ""]
    if escalation.blockers:
        for entry in escalation.blockers:
            lines += [
                f"### {entry.blocker_id} (condition {entry.condition})",
                f"- coder: {entry.coder_position}",
                f"- reviewer: {entry.reviewer_position}",
                "",
            ]
    else:
        lines += ["- none", ""]
    lines += ["## Round history", ""]
    if history:
        for record in history:
            blockers = ", ".join(
                f"{b['id']} ({b['disposition'] or 'no disposition'})"
                for b in record["blockers"]
            )
            lines.append(
                f"- round {record['round']}: {record['verdict']}"
                + (f"; blockers: {blockers}" if blockers else "")
            )
    else:
        lines.append("- no completed rounds")
    return "\n".join(lines) + "\n"
