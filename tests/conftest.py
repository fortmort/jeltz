"""Shared fixtures for the review-engine tests (T6 onward).

The packet builder and the review worktree both operate on a real git
repository in a known dirty state: one commit of tracked content, an
uncommitted tracked modification, and untracked files (including one inside
a brand-new directory). The fixture builds that repository fresh per test so
no test depends on another's mutations, and pins its own git identity so the
suite does not depend on the machine's global git config.
"""

import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    """Run a git command in the given repository and return its stdout.

    Args:
        repo: Repository (or worktree) directory to run in.
        *args: The git subcommand and its arguments.

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
    )
    return result.stdout


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    """A repo with one commit, a tracked modification, and untracked files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "src.py").write_text("VALUE = 1\n")
    (repo / "docs.md").write_text("# docs\n")
    (repo / ".gitignore").write_text("*.log\n.venv/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "chore: initial state")
    (repo / "src.py").write_text("VALUE = 2\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("NEW = True\n")
    return repo
