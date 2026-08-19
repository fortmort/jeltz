"""Tests for T2: multi-host, multi-scope installer.

One source of truth (``skills/`` in this repo) installed into a consumer
project so all four hosts discover it. Spike-verified layout: Claude Code and
grok read ``.claude/skills/`` natively; codex and antigravity both discover a
``.agents/skills`` symlink pointing at it. A manifest stamped at install time
lets ``--check`` (and ``make check-install``) detect drift from what this
repo shipped.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "install.sh"
SOURCE_SKILLS = sorted(p.parent.name for p in REPO_ROOT.glob("skills/*/SKILL.md"))


def _run_installer(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run install.sh with the given arguments.

    Args:
        *args: Command-line arguments for the installer.
        env: Extra environment variables layered over the current environment.

    Returns:
        The completed process with stdout and stderr captured as text.
    """
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [str(INSTALLER), *args],
        capture_output=True,
        text=True,
        env=full_env,
    )


@pytest.fixture()
def consumer_repo(tmp_path: Path) -> Path:
    """Create an empty git repo standing in for a consumer project."""
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    return repo


@pytest.fixture()
def installed_repo(consumer_repo: Path) -> Path:
    """A consumer repo with a completed project-scope install."""
    result = _run_installer(str(consumer_repo))
    assert result.returncode == 0, f"install failed:\n{result.stdout}\n{result.stderr}"
    return consumer_repo


def test_source_skills_exist() -> None:
    """The repo ships at least the skills the review loop depends on."""
    assert "skeptical-reviewer" in SOURCE_SKILLS
    assert "reviewer-response" in SOURCE_SKILLS


def test_project_install_copies_every_skill(installed_repo: Path) -> None:
    """Every shipped skill lands intact under .claude/skills/."""
    for name in SOURCE_SKILLS:
        src = REPO_ROOT / "skills" / name / "SKILL.md"
        dst = installed_repo / ".claude" / "skills" / name / "SKILL.md"
        assert dst.is_file(), f"missing installed skill: {name}"
        assert dst.read_text() == src.read_text(), f"installed copy of {name} differs from source"


def test_project_install_links_agents_dir(installed_repo: Path) -> None:
    """.agents/skills is a symlink resolving to .claude/skills.

    Spike-verified: both codex and antigravity discover project skills
    through this symlink, which is what lets one directory serve all four
    hosts.
    """
    link = installed_repo / ".agents" / "skills"
    assert link.is_symlink(), ".agents/skills is not a symlink"
    assert link.resolve() == (installed_repo / ".claude" / "skills").resolve()


def test_project_install_stamps_manifest(installed_repo: Path) -> None:
    """The install writes a version-stamped manifest covering every file."""
    manifest = installed_repo / ".claude" / "skills" / ".jeltz-manifest"
    assert manifest.is_file(), "no manifest stamped"
    lines = manifest.read_text().splitlines()
    assert lines and lines[0].startswith("# jeltz "), f"manifest missing version stamp: {lines[:1]}"
    body = "\n".join(lines[1:])
    for name in SOURCE_SKILLS:
        assert f"{name}/SKILL.md" in body, f"{name} not covered by manifest"


def test_check_passes_on_clean_install(installed_repo: Path) -> None:
    """--check exits 0 immediately after an install."""
    result = _run_installer("--check", str(installed_repo))
    assert result.returncode == 0, (
        f"check failed on a clean install:\n{result.stdout}\n{result.stderr}"
    )


def test_check_detects_hand_edited_skill(installed_repo: Path) -> None:
    """--check fails and names the file when an installed copy is edited."""
    victim = installed_repo / ".claude" / "skills" / SOURCE_SKILLS[0] / "SKILL.md"
    victim.write_text(victim.read_text() + "\nhand edit\n")
    result = _run_installer("--check", str(installed_repo))
    assert result.returncode != 0, "check passed despite a hand-edited skill"
    combined = result.stdout + result.stderr
    assert f"{SOURCE_SKILLS[0]}/SKILL.md" in combined, (
        f"drift report does not name the edited file:\n{combined}"
    )


def test_check_detects_deleted_skill(installed_repo: Path) -> None:
    """--check fails when an installed file listed in the manifest is gone."""
    victim = installed_repo / ".claude" / "skills" / SOURCE_SKILLS[0] / "SKILL.md"
    victim.unlink()
    result = _run_installer("--check", str(installed_repo))
    assert result.returncode != 0, "check passed despite a deleted skill"


def test_check_detects_missing_agents_link(installed_repo: Path) -> None:
    """--check fails when the .agents/skills symlink is gone.

    Codex and antigravity discover skills only through this symlink, so a
    check that ignores it would report a clean install while half the hosts
    have lost discovery.
    """
    (installed_repo / ".agents" / "skills").unlink()
    result = _run_installer("--check", str(installed_repo))
    assert result.returncode != 0, "check passed despite a missing .agents/skills symlink"
    combined = result.stdout + result.stderr
    assert ".agents/skills" in combined, (
        f"drift report does not name the missing symlink:\n{combined}"
    )


def test_check_detects_retargeted_agents_link(installed_repo: Path, tmp_path: Path) -> None:
    """--check fails when .agents/skills points somewhere else."""
    decoy = tmp_path / "decoy-skills"
    decoy.mkdir()
    link = installed_repo / ".agents" / "skills"
    link.unlink()
    link.symlink_to(decoy)
    result = _run_installer("--check", str(installed_repo))
    assert result.returncode != 0, "check passed despite a retargeted .agents/skills symlink"


def test_reinstall_repairs_replaced_agents_link(installed_repo: Path) -> None:
    """Reinstall restores the symlink even if it became a real directory."""
    link = installed_repo / ".agents" / "skills"
    link.unlink()
    link.mkdir()
    result = _run_installer(str(installed_repo))
    assert result.returncode == 0, f"reinstall failed:\n{result.stdout}\n{result.stderr}"
    check = _run_installer("--check", str(installed_repo))
    assert check.returncode == 0, "check still fails after reinstalling over a non-symlink"


def test_reinstall_repairs_drift(installed_repo: Path) -> None:
    """Re-running the installer restores a drifted copy to shipped content."""
    victim = installed_repo / ".claude" / "skills" / SOURCE_SKILLS[0] / "SKILL.md"
    victim.write_text("clobbered\n")
    result = _run_installer(str(installed_repo))
    assert result.returncode == 0, f"reinstall failed:\n{result.stdout}\n{result.stderr}"
    check = _run_installer("--check", str(installed_repo))
    assert check.returncode == 0, "check still fails after a repair install"


def test_user_install_covers_claude_and_codex(tmp_path: Path) -> None:
    """--user installs into $HOME/.claude/skills and $CODEX_HOME/skills."""
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    home.mkdir()
    result = _run_installer(
        "--user",
        env={"HOME": str(home), "CODEX_HOME": str(codex_home)},
    )
    assert result.returncode == 0, f"user install failed:\n{result.stdout}\n{result.stderr}"
    for name in SOURCE_SKILLS:
        assert (home / ".claude" / "skills" / name / "SKILL.md").is_file(), (
            f"{name} missing from user-scope Claude install"
        )
        assert (codex_home / "skills" / name / "SKILL.md").is_file(), (
            f"{name} missing from user-scope codex install"
        )


def test_make_check_install_runs_check(installed_repo: Path) -> None:
    """``make check-install TARGET=<dir>`` performs the drift check."""
    clean = subprocess.run(
        ["make", "check-install", f"TARGET={installed_repo}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert clean.returncode == 0, (
        f"make check-install failed on a clean install:\n{clean.stdout}\n{clean.stderr}"
    )
    victim = installed_repo / ".claude" / "skills" / SOURCE_SKILLS[0] / "SKILL.md"
    victim.write_text("clobbered\n")
    dirty = subprocess.run(
        ["make", "check-install", f"TARGET={installed_repo}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert dirty.returncode != 0, "make check-install passed despite a hand-edited install"
