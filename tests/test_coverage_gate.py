"""Tests for T26: the pass and coverage bars are declared configuration.

``make test`` carried ``--cov=review --cov-report=term-missing
--cov-fail-under=100`` on the command line, so the 100% bar existed only
inside a Makefile recipe. A bare ``pytest`` - what an editor's runner, a
bisect script, or a developer in a hurry actually invokes - measured
nothing and enforced nothing, and reported green either way.

The bar now lives in ``pyproject.toml``, which pytest reads on every run.
The recipe is reduced to invoking pytest with no arguments at all, so
``make test`` and a bare ``pytest`` are not two commands kept in agreement:
they are the same command.

The behavioral tests below run that command against disposable projects
built from this repo's own ``pyproject.toml`` and root ``conftest.py``, so
what they exercise is the shipped declaration rather than a restatement of
it. Each failure mode the task names - a missed line, a failing test, an
errored test, a skip nobody declared - gets a probe, and a clean project
gets one too, because failure probes prove nothing against a gate that
fails everything.
"""

import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.conftest import run_make

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
ROOT_CONFTEST = REPO_ROOT / "conftest.py"

# What the declaration must say. The measured package and the bar are
# coverage's own settings, so they belong in [tool.coverage] and nowhere
# else: pytest-cov reads them from there, and a second spelling in addopts
# would override rather than confirm it.
TESTPATHS = ["tests"]
MEASURED_PACKAGE = "review"
REQUIRED_COVERAGE = 100

# Flags that would move a coverage setting back onto the command line.
RESTATING_FLAGS = ("--cov=", "--cov-fail-under")

# A skip that carries this marker is a declared exception. Any other skip
# is a test that silently did not run.
EXPECTED_SKIP_MARKER = "expected_skip"
UNEXPECTED_SKIP_SIGNAL = "unexpected skip"

# The module the probe projects measure. Two functions, so a probe can
# leave one uncovered without needing a second module.
PROBE_MODULE = "def answer() -> int:\n    return 42\n\n\ndef also_answer() -> int:\n    return 42\n"

# Tests that touch every line of it, so any probe built on top of this
# fails for its own reason and not for coverage.
_COVERS_EVERY_LINE = (
    "from review.mod import also_answer, answer\n"
    "\n"
    "\n"
    "def test_answer() -> None:\n"
    "    assert answer() == 42\n"
    "\n"
    "\n"
    "def test_also_answer() -> None:\n"
    "    assert also_answer() == 42\n"
)

CLEAN_PROBE = _COVERS_EVERY_LINE

MISSED_LINE_PROBE = (
    "from review.mod import answer\n\n\ndef test_answer() -> None:\n    assert answer() == 42\n"
)

FAILING_PROBE = _COVERS_EVERY_LINE + (
    "\n\ndef test_that_fails() -> None:\n    assert answer() == 43\n"
)

ERRORING_PROBE = (
    "import pytest\n\n"
    + _COVERS_EVERY_LINE
    + (
        "\n"
        "\n"
        "@pytest.fixture\n"
        "def broken() -> None:\n"
        '    raise RuntimeError("setup blew up")\n'
        "\n"
        "\n"
        "def test_that_errors(broken: None) -> None:\n"
        "    assert True\n"
    )
)

UNEXPECTED_SKIP_PROBE = (
    "import pytest\n\n"
    + _COVERS_EVERY_LINE
    + ('\n\ndef test_that_skips() -> None:\n    pytest.skip("a skip nobody declared")\n')
)

EXPECTED_SKIP_PROBE = (
    "import pytest\n\n"
    + _COVERS_EVERY_LINE
    + (
        "\n"
        "\n"
        f"@pytest.mark.{EXPECTED_SKIP_MARKER}\n"
        "def test_that_skips() -> None:\n"
        '    pytest.skip("a declared skip")\n'
    )
)

# Skips that happen while the module is being imported, before any test
# exists to carry a marker. Both raise Skipped from collection rather than
# from a test body, which is a different pytest code path.
SECOND_MODULE = "test_extra.py"

MODULE_LEVEL_SKIP_PROBE = (
    'import pytest\n\npytest.skip("a skip nobody declared", allow_module_level=True)\n'
)

IMPORT_OR_SKIP_PROBE = 'import pytest\n\npytest.importorskip("a_module_that_is_not_installed")\n'


def _pyproject() -> dict:
    """Parse the repo's pyproject.toml.

    Returns:
        The parsed document.
    """
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _pytest_config() -> dict:
    """The ``[tool.pytest.ini_options]`` table, or an empty one.

    Returns:
        The declared pytest configuration.
    """
    return _pyproject().get("tool", {}).get("pytest", {}).get("ini_options", {})


def _addopts() -> list[str]:
    """The declared ``addopts``, split the way pytest splits it.

    Returns:
        The options every pytest run starts with.
    """
    declared = _pytest_config().get("addopts", [])
    return shlex.split(declared) if isinstance(declared, str) else list(declared)


def _coverage_config(section: str) -> dict:
    """A ``[tool.coverage.<section>]`` table, or an empty one.

    Args:
        section: The coverage sub-table to read, such as ``run``.

    Returns:
        The declared coverage configuration for that section.
    """
    return _pyproject().get("tool", {}).get("coverage", {}).get(section, {})


def _probe_project(root: Path, test_source: str, second_module: str = "") -> Path:
    """Build a disposable project governed by this repo's own configuration.

    The pyproject and the root conftest are copied rather than written, so
    a probe exercises the declaration the repo ships. Everything else is
    the smallest tree that declaration can act on: a measured package and
    a test module.

    Args:
        root: Directory to create as the project root.
        test_source: Contents of the test module.
        second_module: Contents of a further test module, when the probe
            needs one that fails to import without taking the covering
            tests down with it.

    Returns:
        The project root.
    """
    root.mkdir()
    shutil.copy(PYPROJECT, root / "pyproject.toml")
    shutil.copy(ROOT_CONFTEST, root / "conftest.py")
    (root / MEASURED_PACKAGE).mkdir()
    (root / MEASURED_PACKAGE / "mod.py").write_text(PROBE_MODULE, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_probe.py").write_text(test_source, encoding="utf-8")
    if second_module:
        (root / "tests" / SECOND_MODULE).write_text(second_module, encoding="utf-8")
    return root


def _run(command: list[str], project: Path) -> subprocess.CompletedProcess[str]:
    """Run the command ``make test`` runs, in a probe project.

    Args:
        command: The pytest invocation taken from make.
        project: Directory to run in.

    Returns:
        The completed process with output captured as text.
    """
    return subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def pytest_command() -> list[str]:
    """The pytest invocation ``make test`` runs, taken from make itself.

    Derived rather than restated: a hand-copied command here could enforce
    a bar the build does not, and pass while doing it.

    Returns:
        The argv make would execute, with its interpreter resolved so it
        can be run from another directory.
    """
    result = run_make("test", REPO_ROOT, dry_run=True)
    assert result.returncode == 0, f"make -n test failed:\n{result.stdout}\n{result.stderr}"
    lines = [
        shlex.split(line)
        for line in result.stdout.splitlines()
        if line.split() and Path(line.split()[0]).name == "pytest"
    ]
    assert len(lines) == 1, f"expected exactly one pytest invocation in:\n{result.stdout}"
    argv = lines[0]
    argv[0] = str(REPO_ROOT / argv[0])
    return argv


def test_pytest_reads_its_configuration_from_pyproject() -> None:
    """The suite's shape is declared where every pytest run will find it."""
    config = _pytest_config()
    assert config, "pyproject.toml declares no [tool.pytest.ini_options]"
    assert config.get("testpaths") == TESTPATHS, (
        f"testpaths is {config.get('testpaths')!r}, so a bare pytest run does "
        f"not know the suite lives in {TESTPATHS}"
    )


def test_every_pytest_run_measures_coverage() -> None:
    """``addopts`` turns coverage on, so no run can opt out by omission."""
    assert any(option.startswith("--cov") for option in _addopts()), (
        f"addopts is {_addopts()}, which leaves a bare pytest run unmeasured"
    )


def test_the_coverage_bar_is_declared_once() -> None:
    """The measured package and the 100% bar live in [tool.coverage] only.

    Restating either in ``addopts`` would not reinforce it. Command-line
    coverage flags override the configuration file, so the two spellings
    could only ever rank, never agree - the same failure mode the shell
    formatting contract hit in T25.
    """
    assert _coverage_config("run").get("source") == [MEASURED_PACKAGE], (
        f"[tool.coverage.run] source is {_coverage_config('run').get('source')!r}, "
        f"expected [{MEASURED_PACKAGE!r}]"
    )
    assert _coverage_config("report").get("fail_under") == REQUIRED_COVERAGE, (
        f"[tool.coverage.report] fail_under is "
        f"{_coverage_config('report').get('fail_under')!r}, expected {REQUIRED_COVERAGE}"
    )
    restated = [
        option for option in _addopts() if any(option.startswith(flag) for flag in RESTATING_FLAGS)
    ]
    assert restated == [], (
        f"addopts restates {restated}, which overrides [tool.coverage] rather than deferring to it"
    )


def test_the_declared_skip_marker_is_registered() -> None:
    """``expected_skip`` is a declared marker, not a typo waiting to happen."""
    markers = _pytest_config().get("markers", [])
    assert any(marker.startswith(EXPECTED_SKIP_MARKER) for marker in markers), (
        f"markers is {markers}, so {EXPECTED_SKIP_MARKER!r} is undeclared"
    )


def test_make_test_adds_nothing_to_a_bare_pytest_run(pytest_command: list[str]) -> None:
    """The recipe is bare pytest, so the two cannot enforce different bars.

    Arguments here would be a second, invisible statement of what the
    suite is and how much of it must be covered.
    """
    assert pytest_command[1:] == [], (
        f"make test passes pytest {pytest_command[1:]}, so it is no longer the "
        f"same command a developer runs by hand"
    )


def test_a_clean_project_passes(pytest_command: list[str], tmp_path: Path) -> None:
    """The gate lets a fully covered, fully passing suite through.

    Without this the failure probes below would pass just as well against
    configuration that failed every run for any reason at all.
    """
    project = _probe_project(tmp_path / "clean", CLEAN_PROBE)
    result = _run(pytest_command, project)
    assert result.returncode == 0, (
        f"a clean project failed the gate:\n{result.stdout}\n{result.stderr}"
    )


def test_a_missed_line_fails_the_run(pytest_command: list[str], tmp_path: Path) -> None:
    """One uncovered line fails the run even though every test passed."""
    project = _probe_project(tmp_path / "missed", MISSED_LINE_PROBE)
    result = _run(pytest_command, project)
    assert result.returncode != 0, (
        f"an uncovered line passed the gate:\n{result.stdout}\n{result.stderr}"
    )
    assert "coverage" in result.stdout.lower(), (
        f"the run failed without naming coverage as the reason:\n{result.stdout}"
    )


def test_a_failing_test_fails_the_run(pytest_command: list[str], tmp_path: Path) -> None:
    """A failed assertion fails the run, coverage notwithstanding."""
    project = _probe_project(tmp_path / "failing", FAILING_PROBE)
    result = _run(pytest_command, project)
    assert result.returncode != 0, (
        f"a failing test passed the gate:\n{result.stdout}\n{result.stderr}"
    )
    assert "test_that_fails" in result.stdout, (
        f"the run failed without naming the failing test:\n{result.stdout}"
    )


def test_an_errored_test_fails_the_run(pytest_command: list[str], tmp_path: Path) -> None:
    """A test that could not run is a failure, not an absence."""
    project = _probe_project(tmp_path / "erroring", ERRORING_PROBE)
    result = _run(pytest_command, project)
    assert result.returncode != 0, (
        f"an errored test passed the gate:\n{result.stdout}\n{result.stderr}"
    )
    assert "test_that_errors" in result.stdout, (
        f"the run failed without naming the errored test:\n{result.stdout}"
    )


def test_an_undeclared_skip_fails_the_run(pytest_command: list[str], tmp_path: Path) -> None:
    """A skip nobody declared is a test that silently did not run.

    Coverage is 100% here and every other test passes, so the skip is the
    only thing left to fail on.
    """
    project = _probe_project(tmp_path / "skipping", UNEXPECTED_SKIP_PROBE)
    result = _run(pytest_command, project)
    assert result.returncode != 0, (
        f"an undeclared skip passed the gate:\n{result.stdout}\n{result.stderr}"
    )
    assert "test_that_skips" in result.stdout, (
        f"the run failed without naming the skipped test:\n{result.stdout}"
    )
    assert UNEXPECTED_SKIP_SIGNAL in result.stdout.lower(), (
        f"the run failed without saying an undeclared skip caused it:\n{result.stdout}"
    )


@pytest.mark.parametrize(
    "probe",
    [MODULE_LEVEL_SKIP_PROBE, IMPORT_OR_SKIP_PROBE],
    ids=["module-level-skip", "importorskip"],
)
def test_a_skip_during_collection_fails_the_run(
    pytest_command: list[str], tmp_path: Path, probe: str
) -> None:
    """A module that skips itself at import is a test file that did not run.

    This is the path an environment probe takes - ``importorskip`` on a
    tool that is not installed, or a module-level ``skip`` - and it never
    reaches a test, so it cannot be caught where a skipped test is caught.
    The covering tests live in a second module here, so coverage stays at
    100% and the skip is the only thing left to fail on.
    """
    project = _probe_project(tmp_path / "collecting", CLEAN_PROBE, probe)
    result = _run(pytest_command, project)
    assert result.returncode != 0, (
        f"a skip during collection passed the gate:\n{result.stdout}\n{result.stderr}"
    )
    assert SECOND_MODULE.removesuffix(".py") in result.stdout, (
        f"the run failed without naming the skipped module:\n{result.stdout}"
    )
    assert UNEXPECTED_SKIP_SIGNAL in result.stdout.lower(), (
        f"the run failed without saying an undeclared skip caused it:\n{result.stdout}"
    )


def test_a_declared_skip_is_allowed(pytest_command: list[str], tmp_path: Path) -> None:
    """The marker is an exception mechanism, not decoration.

    Some skips are load-bearing - the recursion guard on the nested
    ``make test`` run is one - so the gate has to distinguish a declared
    skip from an accidental one rather than banning both.
    """
    project = _probe_project(tmp_path / "declared", EXPECTED_SKIP_PROBE)
    result = _run(pytest_command, project)
    assert result.returncode == 0, (
        f"a declared skip still failed the gate:\n{result.stdout}\n{result.stderr}"
    )
    assert "1 skipped" in result.stdout, (
        f"the declared skip did not report as a skip:\n{result.stdout}"
    )
