"""Tests for T13: the escalation policy engine.

Automated review must know when to stop and hand the thread to a human.
Section 4.2 pins four termination conditions: (1) round three still ends
REQUIRES_CHANGES, (2) thrash - a blocker the coder claimed fixed comes
back, (3) dispute - a blocker the coder rejected as invalid is
re-asserted, (4) the packet exceeded the size ceiling. The engine joins
the coder's reviewer-response disposition block (the T5 contract) against
the next round's verdict by stable finding ids, and renders an escalation
dossier naming the disputed blockers, both sides' positions, and the
round history.
"""

import json
from typing import Any

import pytest

from review.escalation import (
    ResponseError,
    evaluate,
    packet_escalation,
    parse_response,
    render_dossier,
    verify_coverage,
)


def finding(fid: str, disposition: str | None = None) -> dict[str, Any]:
    """A schema-valid verdict finding with distinctive position text."""
    return {
        "id": fid,
        "file": "src.py",
        "line": 1,
        "claim": f"{fid} lets a symlinked config bypass the gate",
        "why": f"{fid}: the symlink target is never validated",
        "disposition": disposition,
    }


def verdict_obj(
    verdict: str = "REQUIRES_CHANGES",
    round_number: int = 1,
    blockers: list[dict] | None = None,
    non_blockers: list[dict] | None = None,
) -> dict[str, Any]:
    """A schema-valid verdict object."""
    return {
        "schema_version": 1,
        "verdict": verdict,
        "round": round_number,
        "blockers": blockers or [],
        "non_blockers": non_blockers or [],
    }


def state_obj(verdict: dict[str, Any]) -> dict[str, Any]:
    """Review state as run.py records it, reduced to what evaluate reads."""
    return {"round": verdict["round"], "verdict": verdict, "history": []}


def disp(fid: str, disposition: str, reason: str) -> dict[str, str]:
    """One reviewer-response disposition entry (T5 section E)."""
    return {"id": fid, "disposition": disposition, "reason": reason}


def response_obj(round_number: int, dispositions: list[dict]) -> dict[str, Any]:
    """A valid reviewer-response disposition block."""
    return {
        "schema_version": 1,
        "round": round_number,
        "dispositions": dispositions,
    }


def test_clean_progress_is_not_escalated() -> None:
    """Fixed-and-resolved plus a genuinely new blocker is normal progress."""
    verdict = verdict_obj(
        round_number=2,
        blockers=[finding("b1", "resolved"), finding("b2", "unresolved")],
    )
    response = response_obj(1, [disp("b1", "fixed", "patched the gate")])
    assert evaluate(state_obj(verdict), response) is None


def test_early_rounds_without_a_response_are_not_escalated() -> None:
    """An open blocker before the round cap is just the loop working."""
    verdict = verdict_obj(round_number=2, blockers=[finding("b1", "unresolved")])
    assert evaluate(state_obj(verdict)) is None


def test_round_cap_still_failing_escalates() -> None:
    """Condition 1: round three still REQUIRES_CHANGES ends the loop."""
    verdict = verdict_obj(round_number=3, blockers=[finding("b1", "unresolved")])
    escalation = evaluate(state_obj(verdict))
    assert escalation is not None
    assert escalation.conditions == (1,)
    (entry,) = escalation.blockers
    assert entry.blocker_id == "b1"
    assert entry.condition == 1
    assert "no position recorded" in entry.coder_position
    assert "symlinked config" in entry.reviewer_position


def test_round_cap_dossier_lists_only_active_blockers() -> None:
    """Resolved prior blockers stay listed in the verdict (the T5 join
    needs their ids) but have no place in the dossier's disputes."""
    verdict = verdict_obj(
        round_number=3,
        blockers=[finding("b0", "resolved"), finding("b1", "unresolved")],
    )
    escalation = evaluate(state_obj(verdict))
    assert escalation is not None
    assert escalation.conditions == (1,)
    assert [entry.blocker_id for entry in escalation.blockers] == ["b1"]


def test_round_cap_acceptance_is_not_escalated() -> None:
    """An accepting round three terminates normally, not via escalation."""
    verdict = verdict_obj(
        "ACCEPTED", round_number=3, blockers=[finding("b1", "resolved")]
    )
    assert evaluate(state_obj(verdict)) is None


@pytest.mark.parametrize("comeback", ["unresolved", "regressed", None])
def test_fixed_blocker_coming_back_is_thrash(comeback: str | None) -> None:
    """Condition 2: a claimed-fixed id returning active is thrash."""
    verdict = verdict_obj(round_number=2, blockers=[finding("b1", comeback)])
    response = response_obj(1, [disp("b1", "fixed", "guarded the symlink path")])
    escalation = evaluate(state_obj(verdict), response)
    assert escalation is not None
    assert escalation.conditions == (2,)
    (entry,) = escalation.blockers
    assert entry.condition == 2
    assert "fixed" in entry.coder_position
    assert "guarded the symlink path" in entry.coder_position
    assert "symlinked config" in entry.reviewer_position


def test_fixed_blocker_resolved_is_not_thrash() -> None:
    """A fixed claim the reviewer confirms resolved is the success path."""
    verdict = verdict_obj(
        "ACCEPTED", round_number=2, blockers=[finding("b1", "resolved")]
    )
    response = response_obj(1, [disp("b1", "fixed", "guarded the symlink path")])
    assert evaluate(state_obj(verdict), response) is None


def test_regression_without_a_response_is_still_thrash() -> None:
    """A reviewer marking a blocker regressed testifies to thrash itself:
    regressed means resolved in a prior round and broken again."""
    verdict = verdict_obj(round_number=2, blockers=[finding("b1", "regressed")])
    escalation = evaluate(state_obj(verdict))
    assert escalation is not None
    assert escalation.conditions == (2,)


def test_rejected_blocker_reasserted_is_a_dispute() -> None:
    """Condition 3: rejection vs re-assertion needs a human tiebreak."""
    verdict = verdict_obj(round_number=2, blockers=[finding("b1", "unresolved")])
    response = response_obj(
        1, [disp("b1", "rejected-invalid", "os.walk meets CLAUDE.md norms")]
    )
    escalation = evaluate(state_obj(verdict), response)
    assert escalation is not None
    assert escalation.conditions == (3,)
    (entry,) = escalation.blockers
    assert entry.condition == 3
    assert "rejected-invalid" in entry.coder_position
    assert "os.walk meets CLAUDE.md norms" in entry.coder_position
    assert "symlinked config" in entry.reviewer_position


def test_rejected_blocker_relisted_as_resolved_still_disputes() -> None:
    """The T5 contract: a rejected-invalid id coming back AT ALL is
    condition 3. Listing a rejected id as resolved claims a fix that never
    happened; the contradiction needs a human, not another round."""
    verdict = verdict_obj(
        "ACCEPTED", round_number=2, blockers=[finding("b1", "resolved")]
    )
    response = response_obj(1, [disp("b1", "rejected-invalid", "not a defect")])
    escalation = evaluate(state_obj(verdict), response)
    assert escalation is not None
    assert escalation.conditions == (3,)


def test_deferred_non_blockers_never_escalate() -> None:
    """Deferring a non-blocker is a sanctioned outcome, not a dispute."""
    verdict = verdict_obj(
        "ACCEPTED_WITH_NON_BLOCKERS",
        round_number=2,
        non_blockers=[finding("nb1")],
    )
    response = response_obj(
        1, [disp("nb1", "deferred-non-blocker", "not worth reopening the diff")]
    )
    assert evaluate(state_obj(verdict), response) is None


def test_every_triggered_condition_is_reported() -> None:
    """Conditions stack; each disputed blocker appears exactly once."""
    verdict = verdict_obj(
        round_number=3,
        blockers=[
            finding("b1", "unresolved"),
            finding("b2", "regressed"),
            finding("b3", "unresolved"),
        ],
    )
    response = response_obj(
        2,
        [
            disp("b1", "rejected-invalid", "not a real defect"),
            disp("b2", "fixed", "rewrote the traversal"),
        ],
    )
    escalation = evaluate(state_obj(verdict), response)
    assert escalation is not None
    assert escalation.conditions == (1, 2, 3)
    ids = sorted(entry.blocker_id for entry in escalation.blockers)
    assert ids == ["b1", "b2", "b3"]


def test_response_for_a_different_round_is_rejected() -> None:
    """A response must answer the round it follows; a mismatch is a bug."""
    verdict = verdict_obj(round_number=2, blockers=[finding("b1", "unresolved")])
    response = response_obj(5, [disp("b1", "fixed", "patched")])
    with pytest.raises(ValueError, match="round"):
        evaluate(state_obj(verdict), response)


def test_parse_response_accepts_bare_json() -> None:
    """A file holding only the JSON object is the simplest valid input."""
    response = response_obj(1, [disp("b1", "fixed", "patched")])
    assert parse_response(json.dumps(response)) == response


def test_parse_response_finds_the_block_in_fixer_output() -> None:
    """Detection keys on `dispositions` (T5 decision), so a verdict block
    in the same output is never mistaken for the response."""
    response = response_obj(1, [disp("b1", "rejected-invalid", "not a defect")])
    verdict = verdict_obj(blockers=[finding("b1")])
    text = (
        "### A. Classification Summary\n\nb1 is invalid.\n\n"
        f"```json\n{json.dumps(verdict)}\n```\n\n"
        "```json\nnot json at all\n```\n\n"
        "### E. Machine-readable response block\n\n"
        f"```json\n{json.dumps(response)}\n```\n"
    )
    assert parse_response(text) == response


_TWO_BLOCKS = (
    f"```json\n{json.dumps(response_obj(1, []))}\n```\n"
    f"```json\n{json.dumps(response_obj(1, []))}\n```\n"
)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "prose without any block",
        _TWO_BLOCKS,
        json.dumps({**response_obj(1, []), "schema_version": 2}),
        json.dumps({"schema_version": 1, "round": "1", "dispositions": []}),
        json.dumps({"schema_version": 1, "round": 1, "dispositions": {}}),
        json.dumps(response_obj(1, [disp("b1", "wontfix", "nah")])),
        json.dumps(
            response_obj(1, [disp("b1", "fixed", "x"), disp("b1", "fixed", "y")])
        ),
        json.dumps(response_obj(1, [{"id": "b1", "disposition": "fixed"}])),
        json.dumps(response_obj(1, [disp("", "fixed", "x")])),
        json.dumps(response_obj(1, [42])),
    ],
    ids=[
        "empty",
        "prose-only",
        "two-blocks",
        "wrong-schema-version",
        "round-not-int",
        "dispositions-not-list",
        "unknown-disposition",
        "duplicate-ids",
        "missing-reason",
        "empty-id",
        "entry-not-object",
    ],
)
def test_parse_response_rejects_unusable_input(text: str) -> None:
    """Anything but exactly one well-formed dispositions block is refused."""
    with pytest.raises(ResponseError):
        parse_response(text)


def test_verify_coverage_accepts_a_complete_response() -> None:
    """A response answering every finding id - blockers and non-blockers
    alike - satisfies the T5 exactly-once contract."""
    verdict = verdict_obj(blockers=[finding("b1")], non_blockers=[finding("nb1")])
    response = response_obj(
        1,
        [
            disp("b1", "fixed", "patched"),
            disp("nb1", "deferred-non-blocker", "later"),
        ],
    )
    verify_coverage(response, verdict)


def test_verify_coverage_rejects_missing_ids() -> None:
    """An omitted finding id could smuggle a blocker past conditions 2-3."""
    verdict = verdict_obj(blockers=[finding("b1")], non_blockers=[finding("nb1")])
    response = response_obj(1, [disp("b1", "fixed", "patched")])
    with pytest.raises(ResponseError, match="nb1"):
        verify_coverage(response, verdict)


def test_verify_coverage_rejects_unknown_ids() -> None:
    """An id the verdict never raised has no finding to answer."""
    verdict = verdict_obj(blockers=[finding("b1")])
    response = response_obj(
        1, [disp("b1", "fixed", "patched"), disp("ghost", "fixed", "what")]
    )
    with pytest.raises(ResponseError, match="ghost"):
        verify_coverage(response, verdict)


def test_dossier_names_the_specific_disagreement() -> None:
    """The dossier carries the id, both positions, and the round history."""
    verdict = verdict_obj(round_number=2, blockers=[finding("b1", "unresolved")])
    response = response_obj(
        1, [disp("b1", "rejected-invalid", "os.walk meets CLAUDE.md norms")]
    )
    escalation = evaluate(state_obj(verdict), response)
    assert escalation is not None
    history = [
        {
            "round": 1,
            "verdict": "REQUIRES_CHANGES",
            "blockers": [{"id": "b1", "disposition": None}],
            "costs": [],
        },
        {
            "round": 2,
            "verdict": "REQUIRES_CHANGES",
            "blockers": [{"id": "b1", "disposition": "unresolved"}],
            "costs": [],
        },
    ]
    text = render_dossier(escalation, history)
    assert "# Escalation dossier" in text
    assert "condition 3" in text
    assert "b1" in text
    assert "os.walk meets CLAUDE.md norms" in text
    assert "symlinked config" in text
    assert "round 1: REQUIRES_CHANGES" in text
    assert "round 2: REQUIRES_CHANGES" in text


def test_packet_escalation_builds_a_condition_four_dossier() -> None:
    """Condition 4 gets the same artifact even with no completed rounds."""
    escalation = packet_escalation("packet is 300001 chars; ceiling is 200000")
    assert escalation.conditions == (4,)
    assert escalation.blockers == ()
    text = render_dossier(escalation, [])
    assert "condition 4" in text
    assert "300001" in text
    assert "no completed rounds" in text
