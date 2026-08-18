"""Tests for T1: repo tooling for shipped artifacts.

``jeltz`` is a distribution repo for shell hooks and skills. These tests pin
the acceptance criteria for its own tooling: ``make lint`` (shellcheck +
shfmt) and ``make test`` (pytest driving subprocesses) both pass on a clean
checkout, and lint actually catches broken or misformatted shell.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Guard variable bounding the self-referential ``make test`` check to a
# single level of recursion.
_INNER_RUN_ENV = "JELTZ_MAKE_TEST_INNER"


def _run_make(
    target: str, cwd: Path, *, dry_run: bool = False, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``make <target>`` and capture its output.

    Args:
        target: The make target to invoke.
        cwd: Directory to run make in.
        dry_run: When True, pass ``-n`` so make prints commands without
            executing them.
        env: Extra environment variables layered over the current environment.

    Returns:
        The completed process with stdout and stderr captured as text.
    """
    cmd = ["make"]
    if dry_run:
        cmd.append("-n")
    cmd.append(target)
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=full_env)


@pytest.fixture()
def lint_tree(tmp_path: Path) -> Path:
    """Copy the Makefile and hooks into a scratch tree for lint experiments.

    Returns:
        Root of a disposable tree where shell files can be broken without
        touching the real checkout.
    """
    shutil.copy(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copytree(REPO_ROOT / "hooks", tmp_path / "hooks")
    return tmp_path


def test_make_lint_passes_on_clean_checkout() -> None:
    """``make lint`` exits 0 against the repo's own shell sources."""
    result = _run_make("lint", REPO_ROOT)
    assert result.returncode == 0, (
        f"make lint failed on a clean checkout:\n{result.stdout}\n{result.stderr}"
    )


def test_make_lint_fails_on_shellcheck_violation(lint_tree: Path) -> None:
    """A script with a shellcheck finding makes ``make lint`` fail."""
    bad = lint_tree / "hooks" / "broken.sh"
    bad.write_text("#!/bin/bash\necho $undefined_and_unquoted\n")
    result = _run_make("lint", lint_tree)
    assert result.returncode != 0, (
        "make lint passed despite a shellcheck violation in hooks/broken.sh"
    )


def test_make_lint_fails_on_formatting_violation(lint_tree: Path) -> None:
    """A shellcheck-clean but misformatted script makes ``make lint`` fail."""
    bad = lint_tree / "hooks" / "misformatted.sh"
    bad.write_text('#!/bin/bash\nif true; then\n  echo "two-space indent"\nfi\n')
    result = _run_make("lint", lint_tree)
    assert result.returncode != 0, (
        "make lint passed despite a formatting violation in hooks/misformatted.sh"
    )


def test_make_test_invokes_pytest() -> None:
    """The ``test`` target delegates to pytest over the tests directory."""
    result = _run_make("test", REPO_ROOT, dry_run=True)
    assert result.returncode == 0, (
        f"make -n test failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "pytest" in result.stdout, (
        f"make test does not invoke pytest:\n{result.stdout}"
    )


def test_make_verify_aggregates_lint_and_test() -> None:
    """``make verify`` runs the lint and test targets as one step."""
    result = _run_make("verify", REPO_ROOT, dry_run=True)
    assert result.returncode == 0, (
        f"make -n verify failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "shellcheck" in result.stdout, (
        f"make verify does not run lint:\n{result.stdout}"
    )
    assert "pytest" in result.stdout, (
        f"make verify does not run tests:\n{result.stdout}"
    )


def test_dependency_changes_reinstall_before_tests() -> None:
    """Editing the tracked dependency declaration retriggers installation.

    A checkout whose .venv predates a dependency change must install the new
    dependencies on the next ``make test`` instead of failing at import time,
    so the test target has to depend on the file that declares them - since
    T22 that is pyproject.toml, the single packaging file.
    """
    declaration = REPO_ROOT / "pyproject.toml"
    assert declaration.is_file(), "no tracked dependency declaration to install from"
    before = declaration.stat()
    try:
        os.utime(declaration)
        result = _run_make("test", REPO_ROOT, dry_run=True)
    finally:
        # Restore the original mtime so the real venv stamp stays fresh and
        # later make runs do not pay a needless reinstall.
        os.utime(declaration, (before.st_atime, before.st_mtime))
    assert result.returncode == 0, (
        f"make -n test failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "pip install" in result.stdout, (
        f"a changed dependency declaration does not reinstall:\n{result.stdout}"
    )
    assert "requirements-dev" not in result.stdout, (
        f"provisioning still reads the retired requirements file:\n{result.stdout}"
    )


def test_make_test_passes_on_clean_checkout() -> None:
    """``make test`` exits 0 on a clean checkout.

    The inner pytest run re-enters this test file, so an environment marker
    bounds the recursion to one nested level: the inner instance of this test
    skips instead of spawning a third run.
    """
    if os.environ.get(_INNER_RUN_ENV):
        pytest.skip("inner make test run; recursion bounded to one level")
    result = _run_make("test", REPO_ROOT, env={_INNER_RUN_ENV: "1"})
    assert result.returncode == 0, (
        f"make test failed on a clean checkout:\n{result.stdout}\n{result.stderr}"
    )
