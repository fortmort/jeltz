"""Tests for T3: the revised skeptical-reviewer skill contract.

The skill is a single SKILL.md consumed verbatim by four hosts (Claude Code,
codex, antigravity, grok), so its contract IS its text. These tests pin the
elements automation depends on: host-portable file references (no
Claude-specific @-expansion), an explicit ref argument defaulting to HEAD, an
uncommitted-changes mode, a no-TODO-item mode, the D5 never-author-code
constraint stated in prose, a re-review mode with per-blocker dispositions,
and a machine-readable verdict block whose example JSON actually parses and
carries the fields the T4 parser will require.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "skeptical-reviewer" / "SKILL.md"
SKILL_TEXT = SKILL_PATH.read_text()

VERDICT_TOP_KEYS = {"schema_version", "verdict", "round", "blockers", "non_blockers"}
BLOCKER_KEYS = {"id", "file", "line", "claim", "why"}


def _fenced_json_blocks(text: str) -> list[str]:
    """Return the contents of every ```json fenced block in the text."""
    return re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)


def _example_verdicts() -> list[dict]:
    """Parse every fenced JSON block that looks like a verdict example."""
    verdicts = []
    for block in _fenced_json_blocks(SKILL_TEXT):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "schema_version" in data:
            verdicts.append(data)
    return verdicts


def test_frontmatter_serves_all_hosts() -> None:
    """YAML frontmatter with name and description survives the rewrite.

    All four hosts discover skills through this exact frontmatter shape
    (spike-verified, TODO.md section 3.3).
    """
    assert SKILL_TEXT.startswith("---\n"), "missing YAML frontmatter"
    frontmatter = SKILL_TEXT.split("---", 2)[1]
    assert re.search(r"^name:\s*skeptical-reviewer\s*$", frontmatter, re.MULTILINE)
    assert re.search(r"^description:\s*\S", frontmatter, re.MULTILINE)


def test_no_claude_specific_at_references() -> None:
    """No @CLAUDE.md / @TODO.md expansion syntax anywhere in the skill.

    The @ prefix is Claude Code expansion; codex, agy, and grok receive it
    as a literal string, so the skill must use plain relative paths.
    """
    assert "@CLAUDE.md" not in SKILL_TEXT, "Claude-specific @CLAUDE.md reference"
    assert "@TODO.md" not in SKILL_TEXT, "Claude-specific @TODO.md reference"


def test_reads_project_norms_first() -> None:
    """The skill tells the reviewer to read CLAUDE.md, by plain path, first."""
    assert "CLAUDE.md" in SKILL_TEXT, "no reference to the project norms file"
    assert re.search(r"\bread\b[^.\n]*\bCLAUDE\.md\b", SKILL_TEXT, re.IGNORECASE), (
        "no explicit instruction to read CLAUDE.md"
    )


def test_ref_argument_defaults_to_head() -> None:
    """The review target is a git ref argument defaulting to HEAD."""
    assert re.search(r"\bref\b", SKILL_TEXT, re.IGNORECASE), (
        "no mention of a ref argument"
    )
    assert re.search(r"\bdefault\w*\b[^.\n]*\bHEAD\b", SKILL_TEXT), (
        "HEAD is not documented as the default ref"
    )


def test_uncommitted_mode_documented() -> None:
    """An uncommitted-changes mode exists and reviews the working tree.

    The mode must define what it reviews (the diff against HEAD plus
    untracked files), not merely mention the word.
    """
    assert re.search(r"\buncommitted\b", SKILL_TEXT, re.IGNORECASE), (
        "no uncommitted-changes review mode"
    )
    assert "git diff HEAD" in SKILL_TEXT, (
        "uncommitted mode does not define its diff source"
    )
    assert re.search(r"\buntracked\b", SKILL_TEXT, re.IGNORECASE), (
        "uncommitted mode does not cover untracked files"
    )


def test_no_todo_mode_reviews_against_norms() -> None:
    """With no TODO item supplied, the review runs against CLAUDE.md norms."""
    assert re.search(r"no TODO item", SKILL_TEXT, re.IGNORECASE), (
        "no documented behavior for a missing TODO item"
    )


def test_reviewer_must_not_author_code() -> None:
    """D5 is stated in prose: read/run/analyze freely, never modify code."""
    assert re.search(
        r"(never|must not)\b[^.\n]*\b(author|modify|edit)", SKILL_TEXT, re.IGNORECASE
    ), "D5 (reviewer must not author code) is not stated"


def test_rereview_mode_judges_prior_blockers() -> None:
    """Re-review mode judges each prior blocker by id with a disposition."""
    assert re.search(r"\bre-review\b", SKILL_TEXT, re.IGNORECASE), "no re-review mode"
    for disposition in ("resolved", "unresolved", "regressed"):
        assert re.search(rf"\b{disposition}\b", SKILL_TEXT, re.IGNORECASE), (
            f"re-review disposition '{disposition}' missing"
        )


def test_rereview_does_not_suppress_preexisting_blockers() -> None:
    """New genuine blockers are admissible in any round, whatever their origin.

    A serious issue missed in round one must not be suppressed in later
    rounds: restricting new findings to those introduced by the amendment
    would let incorrect work reach acceptance.
    """
    assert "introduced by the amendment" not in SKILL_TEXT, (
        "re-review restricts new blockers to amendment-introduced ones"
    )
    assert re.search(
        r"unless they\s+are genuine blockers", SKILL_TEXT, re.IGNORECASE
    ), "re-review does not admit genuine new blockers"


def test_blocker_ids_stable_across_rounds() -> None:
    """The skill requires blocker ids to be stable across review rounds."""
    assert re.search(r"\bstable\b[^.\n]*\bround", SKILL_TEXT, re.IGNORECASE), (
        "id stability across rounds is not required"
    )


def test_verdict_block_example_parses() -> None:
    """The skill embeds a fenced JSON verdict example with the T4 fields."""
    verdicts = _example_verdicts()
    assert verdicts, "no fenced JSON verdict example with schema_version"
    for verdict in verdicts:
        assert VERDICT_TOP_KEYS <= verdict.keys(), (
            f"verdict example missing keys: {VERDICT_TOP_KEYS - verdict.keys()}"
        )


def test_all_four_hosts_emitted_valid_verdicts() -> None:
    """Golden verdicts captured live from all four hosts have the full shape.

    tests/fixtures/reviewer-verdicts/ holds the verdict block each host
    (claude, codex, agy, grok) actually emitted when running this skill
    against the same norms-violating fixture commit (spike 2026-08-15).
    This is T3's acceptance evidence that one SKILL.md yields a parseable
    verdict everywhere, and the fixture set T4's parser must accept.
    """
    fixture_dir = REPO_ROOT / "tests" / "fixtures" / "reviewer-verdicts"
    hosts = {p.stem for p in fixture_dir.glob("*.json")}
    assert hosts == {"claude", "codex", "agy", "grok"}, (
        f"expected one verdict per host, found {sorted(hosts)}"
    )
    for path in sorted(fixture_dir.glob("*.json")):
        verdict = json.loads(path.read_text())
        assert VERDICT_TOP_KEYS <= verdict.keys(), (
            f"{path.name} missing keys: {VERDICT_TOP_KEYS - verdict.keys()}"
        )
        assert verdict["blockers"], f"{path.name} found no blockers in the fixture"
        for blocker in verdict["blockers"]:
            assert BLOCKER_KEYS <= blocker.keys(), (
                f"{path.name} blocker missing: {BLOCKER_KEYS - blocker.keys()}"
            )


def test_verdict_example_blockers_carry_required_fields() -> None:
    """Example blockers carry id, file, line, claim, and why."""
    verdicts = _example_verdicts()
    blockers = [b for v in verdicts for b in v.get("blockers", [])]
    assert blockers, "no verdict example demonstrates a populated blocker"
    for blocker in blockers:
        assert BLOCKER_KEYS <= blocker.keys(), (
            f"blocker example missing keys: {BLOCKER_KEYS - blocker.keys()}"
        )
