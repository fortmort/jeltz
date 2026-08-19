"""Tests for T23: uv is the single provisioner for jeltz's own tooling.

Every Python-ecosystem tool the Makefile runs arrives through uv, resolved
from the committed lockfile so two checkouts install the same versions. No
Makefile path invokes pip.

uv cannot provide `shellcheck` (Haskell) or `shfmt` (Go), and it cannot
provide itself, so those three stay external prerequisites - and a missing
or too-old one has to fail with a message that names it and says what to
do, not with a bare command-not-found from the middle of a build.
"""

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.conftest import run_make

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCKFILE = REPO_ROOT / "uv.lock"
PREFLIGHT = REPO_ROOT / "tools" / "preflight.sh"
README = REPO_ROOT / "README.md"

# Everything a fresh checkout needs before it can provision. Scratch trees
# get exactly this and nothing else, so a provisioning step that reaches for
# an unlisted file fails here rather than in a contributor's first clone.
CHECKOUT_FILES = ("Makefile", "pyproject.toml", "uv.lock", "README.md", "LICENSE.md")

# External binaries no Python package manager can install.
EXTERNAL_TOOLS = ("uv", "shellcheck", "shfmt")

# Make variables naming each external tool, so a test can point one at a
# path that does not exist without touching PATH.
TOOL_OVERRIDES = {"uv": "UV", "shellcheck": "SHELLCHECK", "shfmt": "SHFMT"}

# Checks that must hold without a network: a lockfile refusal is decided
# from the lock and pyproject.toml alone, and a test that quietly needed
# an index would pass here and fail on a plane.
OFFLINE = {"UV_OFFLINE": "1"}


def _checkout(tmp_path: Path) -> Path:
    """Build a scratch tree holding only what a fresh checkout provisions from.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        Root of the scratch tree.
    """
    tree = tmp_path / "checkout"
    tree.mkdir()
    for name in CHECKOUT_FILES:
        shutil.copy(REPO_ROOT / name, tree / name)
    shutil.copytree(REPO_ROOT / "tools", tree / "tools")
    return tree


def _stub_uv(root: Path, version: str, log: Path) -> Path:
    """Write a stub uv reporting ``version`` and logging every other call.

    Args:
        root: Directory to write the stub into.
        version: Version string the stub reports for ``--version``.
        log: File the stub appends each non-version invocation to.

    Returns:
        Path to the executable stub.
    """
    stub = root / "stub-uv"
    stub.write_text(
        f'''#!/bin/sh
set -eu
if [ "$1" = "--version" ]; then
    echo "uv {version} (stubbed 2026-01-01)"
    exit 0
fi
echo "$@" >> "{log}"
exit 0
'''
    )
    stub.chmod(0o755)
    return stub


def _locked_version(package: str) -> str:
    """Return the version the committed lockfile pins for ``package``.

    Args:
        package: Distribution name as it appears in the lockfile.

    Returns:
        The pinned version string.
    """
    lock = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))
    for entry in lock["package"]:
        if entry["name"] == package:
            return str(entry["version"])
    raise AssertionError(f"{package} is not pinned in uv.lock")


def _uv_floor() -> str:
    """Return the minimum uv version the preflight check enforces.

    Returns:
        The version string declared in tools/preflight.sh.
    """
    match = re.search(r'UV_MIN_VERSION="([0-9][0-9.]*)"', PREFLIGHT.read_text(encoding="utf-8"))
    assert match, "tools/preflight.sh declares no UV_MIN_VERSION"
    return match.group(1)


@pytest.mark.parametrize("target", ["venv", "lock", "test", "verify"])
def test_no_make_target_invokes_pip(tmp_path: Path, target: str) -> None:
    """Provisioning runs through uv; no Makefile path shells out to pip.

    Run against an unprovisioned tree on purpose: in a checkout that is
    already up to date make prints nothing, and a recipe nobody printed
    proves nothing.
    """
    result = run_make(target, _checkout(tmp_path), dry_run=True)
    assert result.returncode == 0, f"make -n {target} failed:\n{result.stdout}\n{result.stderr}"
    assert result.stdout.strip(), f"make -n {target} printed no recipe to inspect"
    assert not re.search(r"\bpip\b", result.stdout), (
        f"make {target} still invokes pip:\n{result.stdout}"
    )


def test_provisioning_installs_the_versions_pinned_in_the_lockfile(
    tmp_path: Path,
) -> None:
    """A fresh checkout gets exactly the versions the lockfile records.

    This is what committing the lock buys: two checkouts provisioned days
    apart run the same tool versions, so a green ``make verify`` here means
    the same thing it means there.
    """
    tree = _checkout(tmp_path)
    result = run_make("venv", tree)
    assert result.returncode == 0, (
        f"make venv failed on a fresh checkout:\n{result.stdout}\n{result.stderr}"
    )

    python = tree / ".venv" / "bin" / "python"
    installed = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m;"
            "print(m.version('pytest'), m.version('pytest-cov'), m.version('jsonschema'))",
        ],
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, (
        f"provisioned venv is missing declared dependencies:\n{installed.stderr}"
    )
    assert installed.stdout.split() == [
        _locked_version("pytest"),
        _locked_version("pytest-cov"),
        _locked_version("jsonschema"),
    ], f"installed versions drifted from the lockfile: {installed.stdout!r}"


def test_provisioning_leaves_no_build_artifacts_in_the_tree(tmp_path: Path) -> None:
    """Provisioning installs dependencies without building jeltz itself.

    jeltz ships by file copy and its distribution is metadata-only (T22), so
    building it during provisioning would buy nothing and cost something: the
    build backend writes egg-info and build directories into the tree, which
    the T6 integrity check reads as reviewer tampering.
    """
    tree = _checkout(tmp_path)
    result = run_make("venv", tree)
    assert result.returncode == 0, (
        f"make venv failed on a fresh checkout:\n{result.stdout}\n{result.stderr}"
    )
    artifacts = sorted(
        str(path.relative_to(tree))
        for pattern in ("*.egg-info", "build")
        for path in tree.glob(pattern)
    )
    assert not artifacts, f"provisioning wrote build artifacts into the tree: {artifacts}"


def test_the_committed_lockfile_matches_the_declared_dependencies() -> None:
    """The lockfile is current, and ships with the repo rather than ignored.

    A lockfile that trails pyproject.toml is worse than none: it pins the
    wrong thing while looking authoritative.
    """
    assert LOCKFILE.is_file(), "no uv.lock committed alongside pyproject.toml"
    ignored = subprocess.run(["git", "check-ignore", "-q", str(LOCKFILE)], cwd=REPO_ROOT)
    assert ignored.returncode != 0, "uv.lock is gitignored; consumers never see the pins"

    check = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **OFFLINE},
    )
    assert check.returncode == 0, (
        f"uv.lock does not match pyproject.toml; run `make lock`:\n{check.stderr}"
    )


def test_a_stale_lockfile_stops_provisioning_instead_of_being_rewritten(
    tmp_path: Path,
) -> None:
    """Provisioning refuses a lockfile that trails pyproject.toml.

    Silently re-resolving would defeat the pins and, worse, rewrite a tracked
    file: the reviewer runs ``make verify`` inside the T6 worktree, where any
    tracked mutation fails the review outright. Refusing names the problem
    instead of manufacturing one.
    """
    tree = _checkout(tmp_path)
    packaging = tree / "pyproject.toml"
    packaging.write_text(
        packaging.read_text().replace(
            '"jsonschema>=4.23",', '"jsonschema>=4.23",\n    "tomli-w>=1.0",'
        )
    )
    lock_before = (tree / "uv.lock").read_bytes()

    result = run_make("venv", tree, env=OFFLINE, timeout=120)
    assert result.returncode != 0, (
        f"provisioning accepted a stale lockfile:\n{result.stdout}\n{result.stderr}"
    )
    assert (tree / "uv.lock").read_bytes() == lock_before, (
        "provisioning rewrote the tracked lockfile instead of refusing it"
    )
    combined = result.stdout + result.stderr
    assert "uv.lock" in combined and "uv lock" in combined, (
        f"the failure does not say how to refresh the lockfile:\n{combined}"
    )


@pytest.mark.parametrize("tool", EXTERNAL_TOOLS)
def test_a_missing_external_prerequisite_is_named_with_a_remedy(tmp_path: Path, tool: str) -> None:
    """A missing external binary fails the build by name, before any work."""
    tree = _checkout(tmp_path)
    override = f"{TOOL_OVERRIDES[tool]}={tmp_path}/definitely-absent-{tool}"
    result = run_make("verify", tree, overrides=(override,), timeout=120)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"make verify passed without {tool}:\n{combined}"
    assert tool in combined, f"the failure does not name {tool}:\n{combined}"
    assert re.search(r"install|upgrade|README", combined), (
        f"the failure gives no remedy for a missing {tool}:\n{combined}"
    )


def test_lint_provisions_the_python_tooling_it_runs(tmp_path: Path) -> None:
    """``make lint`` provisions before linting, because ruff arrives from uv.

    T23 narrowed each target to the tools it actually invokes, and lint ran
    shell tools only. T24 put ruff in the dev dependency group, so lint now
    runs Python tooling too - and that tooling comes through the venv, not
    through the preflight, which gates only what uv cannot install. The
    requirement on uv is therefore transitive (``lint: ... venv``, ``venv:
    preflight-uv``): it must provision first, and it must fail without uv
    rather than linting against whatever ruff happens to be on PATH.
    """
    dry = run_make("lint", REPO_ROOT, dry_run=True)
    assert dry.returncode == 0, f"make -n lint failed:\n{dry.stdout}\n{dry.stderr}"
    assert "sync" in dry.stdout, f"make lint runs ruff without provisioning it first:\n{dry.stdout}"
    assert dry.stdout.index("sync") < dry.stdout.index("ruff"), (
        f"provisioning runs after the linter it provisions:\n{dry.stdout}"
    )

    result = run_make("lint", REPO_ROOT, overrides=(f"UV={tmp_path}/absent-uv",), timeout=120)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"make lint passed without the provisioner that supplies ruff:\n{combined}"
    )
    assert "uv" in combined, f"the failure does not name the missing tool:\n{combined}"


@pytest.mark.parametrize("target", ["venv", "lock"])
def test_provisioning_does_not_require_the_shell_tools(tmp_path: Path, target: str) -> None:
    """Provisioning runs uv only, so shellcheck and shfmt need not be present."""
    tree = _checkout(tmp_path)
    result = run_make(
        target,
        tree,
        overrides=(
            f"SHELLCHECK={tmp_path}/absent-shellcheck",
            f"SHFMT={tmp_path}/absent-shfmt",
        ),
        timeout=300,
    )
    assert result.returncode == 0, (
        f"make {target} failed without the shell linters, which it never invokes:"
        f"\n{result.stdout}\n{result.stderr}"
    )


def test_every_missing_prerequisite_is_reported_in_one_pass(tmp_path: Path) -> None:
    """Two missing tools produce two named findings, not one at a time.

    Discovering prerequisites one build at a time is the failure mode this
    check exists to remove.
    """
    tree = _checkout(tmp_path)
    result = run_make(
        "verify",
        tree,
        overrides=(
            f"SHELLCHECK={tmp_path}/absent-shellcheck",
            f"SHFMT={tmp_path}/absent-shfmt",
        ),
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, "make verify passed with two tools missing"
    assert "shellcheck" in combined and "shfmt" in combined, (
        f"only some missing prerequisites were reported:\n{combined}"
    )


def test_an_unsupported_uv_version_is_refused_by_name(tmp_path: Path) -> None:
    """A uv older than the documented floor stops the build with both numbers.

    The floor is only worth declaring if something checks it, and the check is
    only actionable if it says what was found and what is needed.
    """
    tree = _checkout(tmp_path)
    stub = _stub_uv(tmp_path, "0.4.0", tmp_path / "uv-calls.log")
    result = run_make("venv", tree, overrides=(f"UV={stub}",), timeout=120)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"an unsupported uv provisioned anyway:\n{combined}"
    assert "0.4.0" in combined, f"the failure does not name the version found:\n{combined}"
    assert _uv_floor() in combined, f"the failure does not name the required version:\n{combined}"


def test_a_supported_uv_provisions_from_the_lockfile(tmp_path: Path) -> None:
    """A uv at the floor is accepted and told to install from the lockfile.

    The positive control for the version gate - without it, a check that
    rejected every uv would pass the test above - and the assertion that
    provisioning resolves from the committed pins rather than the network.
    """
    tree = _checkout(tmp_path)
    log = tmp_path / "uv-calls.log"
    stub = _stub_uv(tmp_path, _uv_floor(), log)
    result = run_make("venv", tree, overrides=(f"UV={stub}",), timeout=120)

    assert result.returncode == 0, f"a supported uv was rejected:\n{result.stdout}\n{result.stderr}"
    calls = log.read_text().splitlines() if log.exists() else []
    assert any(call.startswith("sync") for call in calls), (
        f"provisioning never ran uv sync:\n{calls}"
    )
    assert any("--locked" in call for call in calls), (
        f"provisioning does not install from the committed lockfile:\n{calls}"
    )


def test_readme_documents_the_prerequisites_and_the_uv_floor() -> None:
    """The prerequisites and the enforced uv floor are documented in one place.

    The floor is read out of the check itself, so documentation and
    enforcement cannot drift apart silently.
    """
    text = README.read_text(encoding="utf-8")
    assert "## Development" in text, (
        "tools/preflight.sh sends a blocked contributor to the Development "
        "section of README.md, and there is no such section"
    )
    documented = text.split("## Development", 1)[1]
    for tool in EXTERNAL_TOOLS:
        installs = [line for line in documented.splitlines() if tool in line and "install" in line]
        assert installs, f"README documents no way to install the {tool} prerequisite"
    assert _uv_floor() in documented, (
        f"README does not document the enforced uv floor ({_uv_floor()})"
    )
