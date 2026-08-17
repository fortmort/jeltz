"""Host-neutral stop-gate decision logic (T14).

One implementation behind every host's Stop hook shim (T16-T19): read
`.jeltz/review/state.json`, compare its recorded diff hash against the
current tree, and decide allow or block with a reason string. The gate
only reads the state file and hashes the tree - it never runs a review -
so it fits a 30s hook budget (R5). R3 scope exclusions (doc-only edits,
nothing changed, per-clone opt-out marker) and fail-open handling of
ungateable states (no repository, no commits, an escalated review) keep
it from deadlocking a session.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from review.gitcmd import git, git_paths, untracked_files
from review.packet import tree_state_hash
from review.run import STATE_PATH

GATE_MARKER_NAME = "jeltz-review-gate"
DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})
DOC_DIRS = frozenset({"docs"})
ACCEPTING_VERDICTS = frozenset({"ACCEPTED", "ACCEPTED_WITH_NON_BLOCKERS"})


@dataclass(frozen=True)
class GateDecision:
    """One stop-gate ruling: whether to allow the stop, and why."""

    allow: bool
    reason: str


def _git_common_dir(repo: Path) -> Path:
    """Resolve the git common dir, absolute, for the per-clone marker.

    Raises:
        subprocess.CalledProcessError: If ``repo`` is not inside a git
            repository.
    """
    common = git(repo, "rev-parse", "--git-common-dir").strip()
    path = Path(common)
    return path if path.is_absolute() else repo / path


def _marker_says_off(marker: Path) -> bool:
    """Whether the opt-out marker exists and its first line reads ``off``.

    Follows the ``claude-hook-mode`` convention: the marker lives under the
    git common dir so it is per-clone and never committed, and only its
    first line (whitespace-stripped) is significant. A missing or empty
    marker leaves the gate armed.
    """
    try:
        first_line = marker.read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return False
    return first_line == "off"


def _changed_paths(repo: Path) -> tuple[str, ...]:
    """All reviewable changed paths: tracked modifications plus untracked.

    NUL-delimited on purpose: newline-delimited git output C-quotes
    non-ASCII paths, which would misclassify quoted doc names as source.
    """
    tracked = git_paths(repo, "diff", "HEAD", "--name-only", "-z")
    return tracked + untracked_files(repo)


def _is_doc(path: str) -> bool:
    """Whether a path is documentation by suffix or by leading directory."""
    parts = PurePosixPath(path)
    return parts.suffix in DOC_SUFFIXES or parts.parts[0] in DOC_DIRS


def _load_state(repo: Path) -> dict[str, Any] | None:
    """Read gate-relevant review state, or None if there is nothing usable.

    Looser than the orchestrator's loader on purpose: the gate only needs
    the diff hash, the verdict, and the escalation marker, and anything it
    cannot use is simply "no review record" - a block, never a crash.
    """
    try:
        data = json.loads((repo / STATE_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("diff_hash"), str):
        return None
    if not isinstance(data.get("verdict"), dict):
        return None
    return data


def decide(repo: Path) -> GateDecision:
    """Decide whether a session may stop with the tree in its current state.

    Args:
        repo: The repository (or plain directory) the session worked in.

    Returns:
        The gate's ruling with a human-readable reason.
    """
    try:
        marker = _git_common_dir(repo) / "info" / GATE_MARKER_NAME
    except subprocess.CalledProcessError:
        return GateDecision(True, "not a git repository; nothing to gate")
    if _marker_says_off(marker):
        return GateDecision(True, f"review gate disabled by {marker}")
    try:
        changed = _changed_paths(repo)
    except subprocess.CalledProcessError:
        return GateDecision(
            True, "cannot inspect the tree (no commit to diff against); failing open"
        )
    if not changed:
        return GateDecision(True, "nothing to review: the tree is unchanged")
    if all(_is_doc(path) for path in changed):
        return GateDecision(True, "doc-only changes are out of review scope")
    state = _load_state(repo)
    if state is None:
        return GateDecision(False, "unreviewed source changes with no review record")
    if state["diff_hash"] != tree_state_hash(repo):
        return GateDecision(
            False, "the tree changed after the last review; it is stale"
        )
    if state.get("escalated"):
        return GateDecision(
            True, "review escalated to a human; automated gating ends here"
        )
    verdict = state["verdict"].get("verdict")
    if verdict in ACCEPTING_VERDICTS:
        return GateDecision(True, "reviewed and accepted for this exact tree")
    return GateDecision(
        False, f"the last review ended {verdict}; blockers are still open"
    )
