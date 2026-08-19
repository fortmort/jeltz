"""Disposable review worktree and post-review integrity check (T6).

The worktree hands the reviewer a real commit containing the developer's
uncommitted and untracked work - preserving the ``skeptical-reviewer HEAD``
mental model - without touching the developer's tree, and is destroyed
afterwards even on a crash. The integrity check enforces D5 mechanically
(R7): tool allowlists cannot stop a shell-equipped reviewer from editing its
checkout, so the orchestrator snapshots the checkout at handoff and fails
the review on any tracked mutation, HEAD movement, or novel untracked write
afterwards. Verification caches are allowlisted by explicit pattern, not via
.gitignore, so a novel write surfaces even when the consumer repo ignores it.
"""

import fnmatch
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from review.gitcmd import git, untracked_files

CACHE_ALLOWLIST: tuple[str, ...] = (
    ".venv/*",
    "__pycache__/*",
    "*/__pycache__/*",
    "*.pyc",
    ".pytest_cache/*",
    ".ruff_cache/*",
    ".mypy_cache/*",
    ".coverage",
    ".coverage.*",
    "htmlcov/*",
    # Build metadata from a test target that installs the project itself
    # (jeltz's own does, since T22). Listed both ways because git reports an
    # ignored directory as a directory and an unignored one file by file.
    "*.egg-info",
    "*.egg-info/*",
)

_UNTRACKED_CODES = ("??", "!!")


class IntegrityError(Exception):
    """The review checkout was mutated; the verdict judges different code.

    A distinct typed error rather than a verdict on purpose (T7): the
    orchestrator must never be able to record a failed integrity check as
    an accepted review.
    """


@dataclass(frozen=True)
class Snapshot:
    """The review checkout's state at reviewer handoff.

    Tracked content is fingerprinted from the filesystem, not from git
    status: a reviewer with shell access can silence status with index
    flags such as ``update-index --assume-unchanged``, so the check must
    not trust the index.
    """

    head: str
    tracked: dict[str, tuple[str, ...]]
    untracked: frozenset[str]


def _fingerprint(file: Path) -> tuple[str, ...]:
    """Fingerprint one path's type, content, and executable mode."""
    if file.is_symlink():
        return ("link", str(file.readlink()))
    if not file.exists():
        return ("missing",)
    mode = "x" if file.stat().st_mode & 0o111 else ""
    return ("file", hashlib.sha256(file.read_bytes()).hexdigest(), mode)


def _tracked_state(worktree: Path) -> dict[str, tuple[str, ...]]:
    """Fingerprint every file named by HEAD's tree, straight from disk."""
    paths = git(worktree, "ls-tree", "-r", "HEAD", "--name-only").splitlines()
    return {path: _fingerprint(worktree / path) for path in paths}


def _listing(worktree: Path) -> list[tuple[str, str]]:
    """Return (status, path) pairs, including ignored and nested untracked.

    ``--ignored=matching`` is what keeps the consumer's .gitignore from
    acting as the allowlist: ignored writes surface here and must pass
    CACHE_ALLOWLIST like any other untracked path.
    """
    output = git(worktree, "status", "--porcelain", "-uall", "--ignored=matching")
    return [(line[:2], line[3:]) for line in output.splitlines()]


def _allowlisted(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in CACHE_ALLOWLIST)


def snapshot(worktree: Path) -> Snapshot:
    """Record the checkout state before handing it to the reviewer.

    Args:
        worktree: The review checkout.

    Returns:
        The HEAD commit and the set of untracked (and ignored) paths.
    """
    untracked = frozenset(path for code, path in _listing(worktree) if code in _UNTRACKED_CODES)
    return Snapshot(
        head=git(worktree, "rev-parse", "HEAD").strip(),
        tracked=_tracked_state(worktree),
        untracked=untracked,
    )


def verify_integrity(worktree: Path, before: Snapshot) -> None:
    """Fail the review if the checkout no longer matches the snapshot.

    Args:
        worktree: The review checkout, after the reviewer ran.
        before: The snapshot taken at handoff.

    Raises:
        IntegrityError: On HEAD movement, tracked mutation (content, type,
            or executable mode - compared from disk, so index flags cannot
            hide it), or a novel untracked write outside the allowlist.
    """
    head = git(worktree, "rev-parse", "HEAD").strip()
    if head != before.head:
        raise IntegrityError(f"HEAD moved from {before.head} to {head}")
    mutated: list[str] = []
    novel: list[str] = []
    for code, path in _listing(worktree):
        if code not in _UNTRACKED_CODES:
            mutated.append(path)
        elif path not in before.untracked and not _allowlisted(path):
            novel.append(path)
    if mutated:
        raise IntegrityError(f"tracked files mutated in review checkout: {sorted(mutated)}")
    if novel:
        raise IntegrityError(f"novel untracked writes in review checkout: {sorted(novel)}")
    changed = sorted(
        path for path in before.tracked if _fingerprint(worktree / path) != before.tracked[path]
    )
    if changed:
        raise IntegrityError(f"tracked content mutated in review checkout: {changed}")


def _materialize_wip(repo: Path, worktree: Path, wip_message: str) -> None:
    """Reproduce the developer's uncommitted state and commit it as the WIP."""
    diff = git(repo, "diff", "HEAD", "--binary")
    if diff:
        git(worktree, "apply", "--whitespace=nowarn", input_text=diff)
    for rel in untracked_files(repo):
        destination = worktree / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / rel, destination, follow_symlinks=False)
    git(worktree, "add", "-A")
    git(
        worktree,
        "-c",
        "user.name=jeltz-review",
        "-c",
        "user.email=review@jeltz.invalid",
        "commit",
        "--allow-empty",
        "--no-verify",
        "-m",
        wip_message,
    )


def _discard(repo: Path, worktree: Path, parent: Path) -> None:
    """Remove the worktree and its registration, tolerating prior damage.

    Removal may fail if the reviewer damaged the checkout, so it is not
    checked; the unconditional prune and parent removal guarantee that
    neither a stale registration nor the directory survives.
    """
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
        capture_output=True,
        text=True,
    )
    git(repo, "worktree", "prune")
    shutil.rmtree(parent, ignore_errors=True)


def _owner_alive(pid_file: Path) -> bool:
    """Whether the process recorded in the pid marker is still running."""
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def reap_stale_worktrees(repo: Path) -> list[str]:
    """Remove leftover review worktrees whose owning process has died.

    A ``finally`` block cannot run when the review process is killed
    outright (hook timeouts do exactly that, R5), so recovery has to happen
    from outside the dead process: every new review reaps first, and the
    orchestrator may call this at startup. Liveness comes from the pid
    marker each review writes beside its checkout; a live review's worktree
    is never touched.

    Args:
        repo: The developer's repository.

    Returns:
        The paths of the worktrees that were reclaimed.
    """
    reaped: list[str] = []
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if not line.startswith("worktree "):
            continue
        worktree = Path(line[len("worktree ") :])
        parent = worktree.parent
        if not parent.name.startswith("jeltz-review-"):
            continue
        if _owner_alive(parent / "pid"):
            continue
        _discard(repo, worktree, parent)
        reaped.append(str(worktree))
    return reaped


@contextmanager
def review_worktree(repo: Path, wip_message: str) -> Iterator[Path]:
    """Yield a disposable worktree with the WIP committed on top of HEAD.

    The WIP commit is made with the review engine's own identity and
    ``--no-verify`` so a consumer repo's hooks and git config cannot block
    or alter the materialization. Entry first reaps any worktree a killed
    earlier review left behind, then records this process's pid beside the
    new checkout so a future reap can prove staleness.

    Args:
        repo: The developer's repository.
        wip_message: Commit message for the WIP commit inside the worktree.

    Yields:
        The worktree path, clean, with the WIP as its HEAD commit.
    """
    reap_stale_worktrees(repo)
    # Resolved so the yielded path matches git's canonical registration
    # (macOS tempdirs are behind a /var -> /private/var symlink).
    parent = Path(tempfile.mkdtemp(prefix="jeltz-review-")).resolve()
    (parent / "pid").write_text(str(os.getpid()))
    worktree = parent / "checkout"
    try:
        git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        _materialize_wip(repo, worktree, wip_message)
        yield worktree
    finally:
        _discard(repo, worktree, parent)
