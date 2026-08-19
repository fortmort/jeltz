"""Tests for T25: the shell formatting contract lives in .editorconfig.

``make lint`` carried ``-i 4 -ci`` inline, which made the Makefile the only
statement of how this repo's shell is formatted - invisible to every editor
and to anyone running ``shfmt`` by hand. Moving the contract into a root
.editorconfig gives one source of truth that both the build and the editor
read.

The move only works if the Makefile stops passing formatting flags: shfmt
discards *every* EditorConfig formatting option the moment any parser or
printer flag is given (shfmt(1)). Two configured sources would not merge,
they would silently pick one - so the flags are not merely redundant here,
they are load-bearing in the wrong direction.
"""

import configparser
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.conftest import run_make

REPO_ROOT = Path(__file__).resolve().parent.parent
EDITORCONFIG = REPO_ROOT / ".editorconfig"

# Hygiene declared for every file in the tree, shell or not.
UNIVERSAL_KEYS = {
    "charset": "utf-8",
    "end_of_line": "lf",
    "insert_final_newline": "true",
    "trim_trailing_whitespace": "true",
}

# The shell formatting contract, replacing the Makefile's inline -i 4 -ci.
SHELL_KEYS = {
    "indent_style": "space",
    "indent_size": "4",
    "switch_case_indent": "true",
}

# Markdown is exempt from trimming: two trailing spaces are a hard line
# break, and skills/security-audit/SKILL.md ships 13 of them.
MARKDOWN_SECTION = "*.md"

# The only flags `make lint` may pass shfmt. -d selects diff output; it is
# not a parser or printer flag, so it leaves the EditorConfig contract in
# force. Anything else in this position discards the whole contract.
ALLOWED_LINT_FLAGS = ["-d"]

# A script formatted exactly as the contract demands: four-space indent and
# indented case clauses. Under shfmt's own defaults (tabs, flush case
# clauses) it is misformatted, which is what makes it a control rather than
# a restatement of the defaults.
PROBE_CONTRACT_FORMATTED = (
    "#!/bin/bash\n"
    "\n"
    "classify() {\n"
    '    case "$1" in\n'
    "        strict) echo whole-file ;;\n"
    "        *) echo changed-lines ;;\n"
    "    esac\n"
    "}\n"
)

# An EditorConfig that declares nothing but the end of the search, so the
# probe beside it is judged by shfmt's defaults no matter where pytest's
# temp directory lives.
BARE_EDITORCONFIG = "root = true\n"


def _editorconfig() -> configparser.ConfigParser:
    """Parse the repo's .editorconfig.

    ``root = true`` sits above every section, which configparser rejects, so
    it is read under a synthetic heading.

    Returns:
        The parsed document, with the preamble under ``__preamble__``.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(
        "[__preamble__]\n" + EDITORCONFIG.read_text(encoding="utf-8"),
        source=str(EDITORCONFIG),
    )
    return parser


def _write_probe(directory: Path, editorconfig: str) -> Path:
    """Write the contract-formatted probe under its own EditorConfig tree.

    Args:
        directory: Directory to create; it becomes the root of the search.
        editorconfig: Contents of the .editorconfig written beside the probe.

    Returns:
        Path to the written script.
    """
    directory.mkdir()
    (directory / ".editorconfig").write_text(editorconfig, encoding="utf-8")
    probe = directory / "probe.sh"
    probe.write_text(PROBE_CONTRACT_FORMATTED, encoding="utf-8")
    return probe


@pytest.fixture(scope="module")
def shfmt_command() -> list[str]:
    """The shfmt invocation ``make lint`` runs, taken from make itself.

    Derived rather than restated: a hand-copied source list here could drop
    a file that lint covers (or cover one it does not) and never say so.

    Returns:
        The argv make would execute, split as a shell would split it.
    """
    result = run_make("lint", REPO_ROOT, dry_run=True)
    assert result.returncode == 0, f"make -n lint failed:\n{result.stdout}\n{result.stderr}"
    lines = [
        shlex.split(line)
        for line in result.stdout.splitlines()
        if line.split() and Path(line.split()[0]).name == "shfmt"
    ]
    assert len(lines) == 1, f"expected exactly one shfmt invocation in:\n{result.stdout}"
    return lines[0]


def test_the_repo_declares_a_root_editorconfig() -> None:
    """A root .editorconfig ends the upward search at the repo boundary.

    Without ``root = true`` an .editorconfig anywhere above the checkout
    joins the contract, so how this repo's shell is formatted would depend
    on where it was cloned.
    """
    assert EDITORCONFIG.is_file(), f"no formatting contract at {EDITORCONFIG}"
    assert _editorconfig().get("__preamble__", "root", fallback="") == "true", (
        "the .editorconfig does not declare root = true, so configuration "
        "above the checkout still applies to this tree"
    )


def test_the_universal_section_declares_file_hygiene() -> None:
    """``[*]`` carries the encoding and whitespace rules for every file."""
    universal = _editorconfig()["*"]
    for key, value in UNIVERSAL_KEYS.items():
        assert universal.get(key) == value, (
            f"[*] declares {key} = {universal.get(key)!r}, expected {value!r}"
        )


def test_markdown_keeps_its_trailing_whitespace() -> None:
    """Trimming is switched off for Markdown, where it would delete syntax.

    Two trailing spaces are a hard line break. The shipped
    ``skills/security-audit/SKILL.md`` uses them, so a blanket trim would
    reflow a consumer-facing artifact the first time anyone opened it.
    """
    config = _editorconfig()
    assert MARKDOWN_SECTION in config, (
        f"[{MARKDOWN_SECTION}] is absent, so Markdown inherits the blanket trim"
    )
    assert config[MARKDOWN_SECTION].get("trim_trailing_whitespace") == "false", (
        "Markdown hard line breaks would be trimmed away"
    )


def test_the_shell_section_declares_the_formatting_contract() -> None:
    """``[*.sh]`` carries what the Makefile used to pass on the command line."""
    shell = _editorconfig()["*.sh"]
    for key, value in SHELL_KEYS.items():
        assert shell.get(key) == value, (
            f"[*.sh] declares {key} = {shell.get(key)!r}, expected {value!r}"
        )


def test_make_lint_carries_no_formatting_flags_of_its_own(shfmt_command: list[str]) -> None:
    """lint reads the contract rather than restating it on the command line.

    This is the whole mechanism, not a tidiness preference: shfmt drops
    every EditorConfig formatting option when it is given any parser or
    printer flag, so a leftover ``-i 4 -ci`` would leave the .editorconfig
    declaring a standard nothing enforces.
    """
    flags = [token for token in shfmt_command[1:] if token.startswith("-")]
    assert flags == ALLOWED_LINT_FLAGS, (
        f"make lint passes shfmt {flags}, which silences the .editorconfig "
        f"contract; only {ALLOWED_LINT_FLAGS} keeps it in force"
    )


def test_the_shell_sources_are_clean_under_the_contract(shfmt_command: list[str]) -> None:
    """Every shell source lint covers is formatted as the contract demands."""
    result = subprocess.run(shfmt_command, cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"shell sources differ from the declared contract:\n{result.stdout}\n{result.stderr}"
    )
    assert result.stdout == "", f"shfmt reported a diff:\n{result.stdout}"


def test_the_declared_keys_change_how_shfmt_formats(
    shfmt_command: list[str], tmp_path: Path
) -> None:
    """The contract broadens shfmt's defaults; it does not restate them.

    Without this control the clean-tree test above would pass just as well
    against an .editorconfig that said nothing shfmt reads.
    """
    shfmt = shfmt_command[0]
    under_contract = _write_probe(tmp_path / "contract", EDITORCONFIG.read_text(encoding="utf-8"))
    accepted = subprocess.run(
        [shfmt, "-d", under_contract.name],
        cwd=under_contract.parent,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, (
        f"the declared contract rejects its own formatting:\n{accepted.stdout}"
    )

    bare = _write_probe(tmp_path / "bare", BARE_EDITORCONFIG)
    defaulted = subprocess.run(
        [shfmt, "-d", bare.name],
        cwd=bare.parent,
        capture_output=True,
        text=True,
    )
    assert defaulted.returncode != 0, (
        "the same script is clean under shfmt's defaults, so the declared keys prove nothing"
    )


def test_a_formatting_flag_silences_the_whole_contract(
    shfmt_command: list[str], tmp_path: Path
) -> None:
    """One printer flag discards the EditorConfig contract entirely.

    The reason ``make lint`` may pass no formatting flags, made executable:
    the flag does not merge with the declared keys, it replaces all of them.
    """
    probe = _write_probe(tmp_path / "flagged", EDITORCONFIG.read_text(encoding="utf-8"))
    flagged = subprocess.run(
        [shfmt_command[0], "-i", "0", "-d", probe.name],
        cwd=probe.parent,
        capture_output=True,
        text=True,
    )
    assert flagged.returncode != 0, (
        "a printer flag left the .editorconfig indent in force; the premise "
        "of moving the contract out of the Makefile no longer holds"
    )
