"""Shared helpers and fixtures for the test suite.

The packet builder and the review worktree both operate on a real git
repository in a known dirty state: one commit of tracked content, an
uncommitted tracked modification, and untracked files (including one inside
a brand-new directory). The fixture builds that repository fresh per test so
no test depends on another's mutations, and pins its own git identity so the
suite does not depend on the machine's global git config.

``run_make`` lives here rather than in either Makefile-driven test module
because both drive the same build from different angles - repo tooling (T1)
and provisioning (T23) - and two copies of the invocation would drift.
"""

import os
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


def run_make(
    target: str,
    cwd: Path,
    *,
    dry_run: bool = False,
    overrides: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run ``make <target>`` and capture its output.

    Args:
        target: The make target to invoke.
        cwd: Directory to run make in.
        dry_run: When True, pass ``-n`` so make prints commands without
            executing them.
        overrides: ``NAME=value`` arguments appended to the command line,
            used to point a tool variable at a stub or a missing path.
        env: Extra environment variables layered over the current
            environment.
        timeout: Seconds to wait. A build that should have failed fast trips
            this instead of hanging the suite.

    Returns:
        The completed process with stdout and stderr captured as text.
    """
    return subprocess.run(
        ["make", *(["-n"] if dry_run else []), target, *overrides],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=timeout,
    )


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
