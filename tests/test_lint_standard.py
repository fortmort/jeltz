"""Tests for T24: jeltz holds its own Python to the full ruff rule set.

Two rule sets exist in this repo and they are not the same thing. The
shipped consumer hook (``hooks/ruff.sh``) runs a deliberately abbreviated
set so red/green/refactor does not thrash on half-written code. The repo's
own standard is the full set declared here, enforced by ``make lint``.

Committing a tracked ``[tool.ruff]`` section also flips
``hooks/lib/repo-mode.sh`` to whole-file (strict) enforcement for this tree,
which is why the config and the cleanup it implies land in one task rather
than arriving as a packaging side effect (T1, T22).
"""

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.conftest import git, run_make

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "uv.lock"
RUFF = REPO_ROOT / ".venv" / "bin" / "ruff"
SHIPPED_HOOK = REPO_ROOT / "hooks" / "ruff.sh"
REPO_MODE_LIB = REPO_ROOT / "hooks" / "lib" / "repo-mode.sh"

# The rule families the repo standard selects. Wider than ruff's default
# (E4, E7, E9, F): the point of this task is that jeltz enforces more than
# the abbreviated set it ships to consumers.
REQUIRED_RULES = {"E", "W", "F", "I", "B", "C4", "UP", "ARG", "SIM"}

# Rules deliberately switched off: line length is the formatter's job, and
# B008 forbids a call in a default argument, which is idiomatic elsewhere.
# This is the COMPLETE list, and asserted as such - an ignore is a hole in
# the standard, so a new one is a decision to make in review, not a line to
# slip past a subset check.
DECLARED_IGNORES = {"E501", "B008"}

# Where ruff looks for first-party code. The repo root, because that is
# where the `review` and `tests` packages live; naming the packages
# themselves points ruff INSIDE them and demotes tests.conftest to
# third-party (14 I001 errors, probed live during T24).
DECLARED_SRC = ["."]

# The abbreviated set the shipped hook suppresses while code is in flight.
# T24 must not touch it - it is consumer-facing phase tooling.
HOOK_ABBREVIATION = "--ignore F401,F841,F821"

# A file whose only faults are outside ruff's default rule set: an unused
# argument (ARG001) and a pointless comprehension (C416). It proves the
# declared config actually broadens enforcement rather than restating it.
PROBE_BEYOND_DEFAULTS = (
    "def check(items: list[int], unused: int) -> list[int]:\n"
    '    """Probe."""\n'
    "    return [item for item in items]\n"
)

# Third-party and first-party imports in one block. Flagged only when the
# isort configuration knows `review` is this repo's own code.
PROBE_IMPORT_GROUPING = (
    "import pytest\nfrom review.verdict import parse_verdict\n\nUSES = (pytest, parse_verdict)\n"
)


def _ruff(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    """Run the provisioned ruff and capture its output.

    Args:
        *args: Arguments passed to ruff.
        cwd: Directory to run in.

    Returns:
        The completed process with stdout and stderr captured as text.
    """
    assert RUFF.is_file(), (
        f"no ruff in the provisioned venv ({RUFF}); it must arrive from uv "
        "like the rest of the Python tooling"
    )
    return subprocess.run([str(RUFF), *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture(scope="module")
def pyproject() -> dict:
    """The repo's parsed pyproject.toml.

    Returns:
        The decoded TOML document.
    """
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_ruff_is_provisioned_as_development_tooling(pyproject: dict) -> None:
    """ruff is a dev dependency, locked like everything else uv installs.

    It lints jeltz; it is not needed to run the shipped review engine, so a
    consumer provisioning production dependencies (T33) must not get it.
    """
    dev = " ".join(pyproject["dependency-groups"]["dev"])
    production = " ".join(pyproject["project"]["dependencies"])
    assert "ruff" in dev, "ruff is not declared in the dev dependency group"
    assert "ruff" not in production, (
        "ruff lints jeltz; it is not a dependency of the shipped engine"
    )
    assert 'name = "ruff"' in LOCKFILE.read_text(encoding="utf-8"), (
        "ruff is declared but not pinned in uv.lock; run `make lock`"
    )


def test_the_repo_declares_the_full_rule_set(pyproject: dict) -> None:
    """The standard is declared configuration, not a Makefile incantation.

    Selections are asserted as a lower bound and exclusions as an exact set,
    because the two move in opposite directions: adding a rule family raises
    the standard and needs no permission from this test, while adding an
    ignore lowers it and should not pass unnoticed.
    """
    ruff_config = pyproject["tool"]["ruff"]
    assert ruff_config["line-length"] == 100
    assert ruff_config["target-version"] == "py311"
    assert ruff_config["src"] == DECLARED_SRC, (
        f"src is {ruff_config['src']}, which changes what counts as first-party; "
        "the clean-tree test is what fails when it is wrong, but say so here"
    )

    lint = ruff_config["lint"]
    selected = set(lint["select"])
    assert REQUIRED_RULES.issubset(selected), f"rule families missing: {REQUIRED_RULES - selected}"
    ignored = set(lint["ignore"])
    assert ignored == DECLARED_IGNORES, (
        f"the ignore list changed: {ignored ^ DECLARED_IGNORES}. Each ignore is a "
        "documented hole in the standard; adding one is a decision, not an edit"
    )
    assert "review" in lint["isort"]["known-first-party"], (
        "the engine package is not declared first-party, so its imports sort "
        "with third-party libraries"
    )


def test_make_lint_runs_ruff_alongside_the_shell_linters() -> None:
    """``make lint`` enforces the Python standard as well as the shell one."""
    result = run_make("lint", REPO_ROOT, dry_run=True)
    assert result.returncode == 0, f"make -n lint failed:\n{result.stdout}\n{result.stderr}"
    assert "ruff check" in result.stdout, f"make lint does not lint Python:\n{result.stdout}"
    assert "ruff format --check" in result.stdout, (
        f"make lint does not check Python formatting:\n{result.stdout}"
    )
    assert "shellcheck" in result.stdout and "shfmt" in result.stdout, (
        f"make lint dropped the shell linters:\n{result.stdout}"
    )


def test_the_python_tree_satisfies_the_full_rule_set() -> None:
    """Every Python file in the repo is clean under the declared rules.

    The config and the compliance it implies land together: a tracked
    ``[tool.ruff]`` section promotes this tree to whole-file hook
    enforcement the moment it is committed, so leaving violations behind
    would block the next edit to any file carrying one.

    Ruff is left to discover the tree, exactly as ``make lint`` does, so a
    new file cannot be clean here and unlinted there.
    """
    result = _ruff("check", "--no-cache", "--output-format", "concise")
    assert result.returncode == 0, (
        f"the Python tree violates its own rule set:\n{result.stdout}\n{result.stderr}"
    )


def test_the_python_tree_is_formatted_to_the_declared_width() -> None:
    """``ruff format`` reproduces the committed formatting exactly.

    Formatting is a gate, not a suggestion: ``make lint`` checks rather than
    rewrites, so a contributor is told to run the formatter instead of
    finding their build has quietly edited their files.
    """
    result = _ruff("format", "--no-cache", "--check")
    assert result.returncode == 0, (
        f"committed formatting does not match the declared width:\n{result.stdout}\n{result.stderr}"
    )


def test_the_rule_set_catches_what_ruff_defaults_miss(tmp_path: Path) -> None:
    """The declared configuration broadens enforcement, it does not restate it.

    Without this control, a config that selected nothing useful would still
    pass the clean-tree tests above.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE_BEYOND_DEFAULTS)

    configured = _ruff(
        "check",
        "--no-cache",
        "--config",
        str(PYPROJECT),
        "--output-format",
        "concise",
        str(probe),
    )
    assert "ARG001" in configured.stdout, f"unused arguments are not enforced:\n{configured.stdout}"
    assert "C416" in configured.stdout, (
        f"comprehension rules are not enforced:\n{configured.stdout}"
    )

    default = _ruff("check", "--no-cache", "--isolated", "--output-format", "concise", str(probe))
    assert default.returncode == 0, (
        f"the probe is caught by ruff's defaults, so it proves nothing:\n{default.stdout}"
    )


def test_first_party_imports_sort_apart_from_third_party(tmp_path: Path) -> None:
    """``review`` is this repo's own code and its imports are grouped as such."""
    probe = tmp_path / "grouping.py"
    probe.write_text(PROBE_IMPORT_GROUPING)

    result = _ruff(
        "check",
        "--no-cache",
        "--config",
        str(PYPROJECT),
        "--output-format",
        "concise",
        str(probe),
    )
    assert "I001" in result.stdout, (
        f"a first-party import sharing a block with pytest was accepted:\n{result.stdout}"
    )


def test_committing_this_pyproject_flips_the_tree_to_strict(tmp_path: Path) -> None:
    """The tracked ruff config promotes this tree to whole-file enforcement.

    This is the T1 ordering hazard, now deliberate: the same section that
    declares the standard is what tells the shipped hooks to enforce it on
    whole files rather than only on changed lines.
    """
    repo = (tmp_path / "consumer").resolve()
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    shutil.copy(PYPROJECT, repo / "pyproject.toml")
    (repo / "sample.py").write_text("VALUE = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "chore: adopt the ruff standard")

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
    assert probe.stdout.strip() == "strict", (
        f"a tracked ruff config left the tree on {probe.stdout.strip()!r} enforcement"
    )


def test_the_shipped_hook_keeps_its_abbreviated_rule_set() -> None:
    """The consumer hook's tolerant rules are not the repo's own standard.

    ``hooks/ruff.sh`` runs against code mid-cycle, where unused imports and
    undefined names are expected states rather than defects. Tightening it to
    match this repo's standard would make the shipped tooling fight TDD.
    """
    hook = SHIPPED_HOOK.read_text(encoding="utf-8")
    assert hook.count(HOOK_ABBREVIATION) == 2, (
        "the shipped hook's abbreviated rule set changed; it is consumer-facing "
        "phase tooling, not the repo standard"
    )
    assert "--select" not in hook, (
        "the shipped hook now pins a rule selection; consumers' own ruff config "
        "is supposed to decide that"
    )
