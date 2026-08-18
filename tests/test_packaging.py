"""Tests for T22: the pyproject.toml packaging baseline.

jeltz declares its dependencies in one file, split by audience: the
production dependencies the shipped review engine needs wherever it runs
(a consumer repo included, per T33) and a dev group for developing jeltz
itself. requirements-dev.txt is retired.

The packaging file must not carry a ``[tool.ruff]`` section yet. A TRACKED
ruff config flips hooks/lib/repo-mode.sh to whole-file (strict) enforcement
for this entire tree, so config and full-rules compliance land together in
T24 rather than arriving as a packaging side effect.

What this file does NOT cover: how the declared dependencies get installed.
That moved to tests/test_provisioning.py when T23 replaced pip with uv.
"""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.conftest import git

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
REPO_MODE_LIB = REPO_ROOT / "hooks" / "lib" / "repo-mode.sh"

# The dependency group carrying tooling used to develop jeltz itself.
DEV_GROUP = "dev"

# Tooling that must never reach a consumer provisioning jeltz's production
# dependencies: it exists to test jeltz, not to run the review engine.
DEV_ONLY = {"pytest", "pytest-cov"}


def _requirement_names(specs: list[str]) -> set[str]:
    """Reduce PEP 508 requirement strings to their distribution names.

    Args:
        specs: Requirement strings, e.g. ``["jsonschema>=4.23", "pytest"]``.

    Returns:
        The lower-cased distribution names, with version specifiers, extras,
        and environment markers stripped.
    """
    return {
        re.split(r"[<>=!~;\[\s]", spec, maxsplit=1)[0].strip().lower() for spec in specs
    }


@pytest.fixture(scope="module")
def pyproject() -> dict:
    """The repo's parsed pyproject.toml.

    Returns:
        The decoded TOML document.
    """
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def production_deps(pyproject: dict) -> set[str]:
    """Distribution names of the dependencies needed to RUN the shipped code.

    Returns:
        The names declared under ``[project].dependencies``.
    """
    return _requirement_names(pyproject["project"]["dependencies"])


@pytest.fixture(scope="module")
def dev_deps(pyproject: dict) -> set[str]:
    """Distribution names of the dependencies needed to DEVELOP jeltz.

    Returns:
        The names declared in the dev dependency group.
    """
    return _requirement_names(pyproject["dependency-groups"][DEV_GROUP])


def test_pyproject_declares_project_metadata(pyproject: dict) -> None:
    """The packaging file identifies the project and its Python floor."""
    project = pyproject["project"]
    assert project["name"] == "jeltz"
    assert project["version"], "no version declared"
    assert project["requires-python"].startswith(">=3.11"), (
        f"CLAUDE.md requires Python 3.11+, got {project['requires-python']!r}"
    )


def test_jsonschema_is_a_production_dependency(
    production_deps: set[str], dev_deps: set[str]
) -> None:
    """jsonschema runs the shipped engine, so it is not a dev dependency.

    review/verdict.py imports jsonschema at module scope, and T33 installs
    that module into consumer repos that never install jeltz's test tooling.
    """
    assert "jsonschema" in production_deps
    assert "jsonschema" not in dev_deps, (
        "jsonschema is required to run the shipped review engine, not to develop jeltz"
    )


def test_development_tooling_is_confined_to_the_dev_group(
    production_deps: set[str], dev_deps: set[str]
) -> None:
    """The test tooling is declared for developers and only for developers."""
    assert DEV_ONLY <= dev_deps, f"dev group is missing {DEV_ONLY - dev_deps}"
    assert not (DEV_ONLY & production_deps), (
        f"test tooling leaked into production dependencies: {DEV_ONLY & production_deps}"
    )


def test_pyproject_carries_no_ruff_config(pyproject: dict) -> None:
    """No ``[tool.ruff]`` section ships before T24's full-rules compliance."""
    assert "ruff" not in pyproject.get("tool", {}), (
        "a tracked [tool.ruff] section promotes this tree to strict hook "
        "enforcement; it belongs with T24's cleanup, not with packaging"
    )


def test_committing_this_pyproject_leaves_repo_mode_diff(tmp_path: Path) -> None:
    """A tree gaining this pyproject.toml stays on diff-scoped enforcement."""
    repo = (tmp_path / "consumer").resolve()
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    shutil.copy(PYPROJECT, repo / "pyproject.toml")
    (repo / "sample.py").write_text("VALUE = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "chore: packaging baseline")

    probe = subprocess.run(
        [
            "bash",
            "-c",
            'set -eu; . "$1"; hook_repo_mode "$2"',
            "probe",
            str(REPO_MODE_LIB),
            str(repo / "sample.py"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "diff", (
        f"committing pyproject.toml flipped the tree to {probe.stdout.strip()!r}"
    )


def test_requirements_dev_txt_is_retired() -> None:
    """Dependencies are declared in one file, not two."""
    assert not (REPO_ROOT / "requirements-dev.txt").exists(), (
        "requirements-dev.txt still exists alongside pyproject.toml"
    )
