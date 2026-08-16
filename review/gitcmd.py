"""Small shared git runner for the review engine (T6).

Every review-engine module shells out to git the same way: against an
explicit repository path, capturing output, failing fast on a non-zero exit.
Centralizing that here keeps the callers to one obvious spelling and keeps
binary-safe output (needed for diff hashing) next to the text variant.
"""

import subprocess
from pathlib import Path


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    """Run a git command in the given repository and return its stdout.

    Args:
        repo: Repository (or worktree) directory to run in.
        *args: The git subcommand and its arguments.
        input_text: Optional text piped to git's stdin.

    Returns:
        The command's stdout, decoded as text.

    Raises:
        subprocess.CalledProcessError: If git exits non-zero.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    )
    return result.stdout


def git_bytes(repo: Path, *args: str) -> bytes:
    """Run a git command and return its stdout undecoded.

    Binary-safe variant for content that may not be text, such as
    ``git diff --binary`` output fed into a hash.

    Args:
        repo: Repository (or worktree) directory to run in.
        *args: The git subcommand and its arguments.

    Returns:
        The command's raw stdout.

    Raises:
        subprocess.CalledProcessError: If git exits non-zero.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout


def untracked_files(repo: Path) -> tuple[str, ...]:
    """List untracked files, excluding gitignored ones and `.jeltz/`, sorted.

    Gitignored files are excluded on purpose: they are not reviewed content,
    so they belong in neither the packet nor the worktree materialization.
    The review engine's own state under `.jeltz/` is excluded the same way
    even when the consumer has not gitignored it - otherwise writing
    `.jeltz/review/state.json` would change the very diff hash it records,
    and no review could ever match the tree it examined (T12).

    Args:
        repo: Repository whose working tree to list.

    Returns:
        Sorted relative paths of untracked, un-ignored files.
    """
    listing = git(repo, "ls-files", "--others", "--exclude-standard")
    return tuple(
        sorted(path for path in listing.splitlines() if not path.startswith(".jeltz/"))
    )
