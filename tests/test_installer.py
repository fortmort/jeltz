"""Tests for T2: multi-host, multi-scope installer, and T27: its safety.

One source of truth (``skills/`` in this repo) installed into a consumer
project so all four hosts discover it. Spike-verified layout: Claude Code and
grok read ``.claude/skills/`` natively; codex and antigravity both discover a
``.agents/skills`` symlink pointing at it. A manifest stamped at install time
lets ``--check`` (and ``make check-install``) detect drift from what this
repo shipped.

The installer writes into directories it does not own - a consumer repo, a
developer's ``$HOME``. T27 pins the rule that makes that safe: it removes only
what its own manifest records installing, and stops with an actionable message
on anything else. Those tests set up the situations where the difference shows.
"""

import hashlib
import os
import re
import shutil
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


# ---------------------------------------------------------------------------
# T27: the installer removes only what it can prove it installed.
#
# Every case below puts something in the install path that jeltz did not put
# there, and asserts two things: the run stops with an explanation, and the
# thing is still on disk afterwards. A recursive force-delete satisfies
# neither - it takes the file and reports success.
# ---------------------------------------------------------------------------

MANIFEST_NAME = ".jeltz-manifest"

# ``rm`` invoked with a recursive flag, in any spelling and any flag order.
RECURSIVE_DELETE = re.compile(r"\brm\s+(?:-\S+\s+)*(?:-\S*[rR]|--recursive)")


def _code_lines(source: str) -> list[tuple[int, str]]:
    """Return the numbered lines of a shell script that are not comments.

    Args:
        source: The script's full text.

    Returns:
        ``(line number, text)`` for every line that runs.
    """
    return [
        (n, line)
        for n, line in enumerate(source.splitlines(), 1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _sha256(path: Path) -> str:
    """Return a file's sha256 in the hex form the manifest records."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _skills_root(repo: Path) -> Path:
    """Return the project-scope skills root inside a consumer repo."""
    return repo / ".claude" / "skills"


def _manifest_entry_outside_the_root(repo: Path, victim: Path) -> str:
    """Append a truthful manifest line for a file outside the install root.

    The hash is correct, so nothing downstream can reject the entry by
    noticing drift; only the path itself is out of bounds.

    Args:
        repo: The consumer repo holding the install.
        victim: A file outside the skills root.

    Returns:
        The relative path as written into the manifest.
    """
    manifest = _skills_root(repo) / MANIFEST_NAME
    rel = os.path.relpath(victim, manifest.parent)
    manifest.write_text(f"{manifest.read_text()}{_sha256(victim)}  {rel}\n")
    return rel


def _assert_refused(result: subprocess.CompletedProcess[str], *named: str) -> None:
    """Assert the installer stopped, said what it found, and said what to do.

    Args:
        result: The finished installer run.
        *named: Substrings the message must contain, normally the offending
            path - a refusal that does not name it cannot be acted on.
    """
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"the installer did not refuse:\n{combined}"
    for needle in named:
        assert needle in combined, f"the refusal never names {needle}:\n{combined}"
    assert "remove" in combined.lower(), (
        f"the refusal does not say how to resolve it by hand:\n{combined}"
    )


def test_the_installer_never_deletes_recursively() -> None:
    """No ``rm -r`` in any spelling survives in install.sh.

    The safe strategy is per-file ``rm`` and non-forced ``rmdir``: both fail
    loudly on anything unexpected, so the blast radius of a mistake - or of a
    path that changes between inspection and action - is one file.
    """
    offenders = [
        f"{INSTALLER.name}:{n}: {line.strip()}"
        for n, line in _code_lines(INSTALLER.read_text())
        if RECURSIVE_DELETE.search(line)
    ]
    assert not offenders, "install.sh still force-deletes recursively:\n" + "\n".join(offenders)


def test_reinstall_refuses_an_unmanifested_file_in_a_skill_directory(installed_repo: Path) -> None:
    """A file jeltz did not install stops the run instead of being deleted."""
    stray = _skills_root(installed_repo) / SOURCE_SKILLS[0] / "notes.md"
    stray.write_text("hand-written notes\n")
    result = _run_installer(str(installed_repo))
    _assert_refused(result, f"{SOURCE_SKILLS[0]}/notes.md")
    assert stray.is_file() and stray.read_text() == "hand-written notes\n", (
        "the refused install destroyed the file it refused over"
    )


def test_reinstall_refuses_skills_it_cannot_prove_it_installed(installed_repo: Path) -> None:
    """Without a manifest the installed skills are unattributable, so stop.

    The manifest is the only record of what jeltz put there. Gone, every
    directory in the install root might be someone's hand-written skill.
    """
    (_skills_root(installed_repo) / MANIFEST_NAME).unlink()
    victim = _skills_root(installed_repo) / SOURCE_SKILLS[0] / "SKILL.md"
    before = victim.read_text()
    result = _run_installer(str(installed_repo))
    _assert_refused(result, SOURCE_SKILLS[0])
    assert victim.read_text() == before, "the refused install rewrote a skill anyway"


def test_reinstall_refuses_a_symlink_where_a_skill_directory_belongs(
    installed_repo: Path, tmp_path: Path
) -> None:
    """A symlink in the install root is not something jeltz installed.

    The directory it points at holds a file at a path the manifest records,
    so removing "the installed files" through it would delete someone else's
    work. The symlink stands in for the last shipped skill while an earlier
    one is left drifted: catching this has to happen in the inspection pass,
    or the refusal arrives after earlier skills have already been rewritten.
    """
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    (victim_dir / "SKILL.md").write_text("someone else's file\n")
    drifted = _skills_root(installed_repo) / SOURCE_SKILLS[0] / "SKILL.md"
    drifted.write_text("clobbered\n")
    target = _skills_root(installed_repo) / SOURCE_SKILLS[-1]
    shutil.rmtree(target)
    target.symlink_to(victim_dir)
    result = _run_installer(str(installed_repo))
    _assert_refused(result, SOURCE_SKILLS[-1])
    assert (victim_dir / "SKILL.md").read_text() == "someone else's file\n", (
        "the installer followed a symlink out of the install root and deleted through it"
    )
    assert drifted.read_text() == "clobbered\n", (
        "the symlink was found only once the install was already writing"
    )


def test_reinstall_refuses_a_non_empty_directory_at_the_agents_link(installed_repo: Path) -> None:
    """A directory with content where the symlink belongs stops the run.

    An empty one is repaired (that is a reinstall's job); a non-empty one is
    someone's files, and the refusal comes before any install work so nothing
    is half-applied.
    """
    drifted = _skills_root(installed_repo) / SOURCE_SKILLS[0] / "SKILL.md"
    drifted.write_text("clobbered\n")
    link = installed_repo / ".agents" / "skills"
    link.unlink()
    link.mkdir()
    stray = link / "someone-elses-work.md"
    stray.write_text("not jeltz's\n")
    result = _run_installer(str(installed_repo))
    _assert_refused(result, ".agents/skills")
    assert stray.is_file() and stray.read_text() == "not jeltz's\n", (
        "the refused install destroyed the directory it refused over"
    )
    assert drifted.read_text() == "clobbered\n", (
        "the install rewrote skills before discovering it would have to refuse"
    )


def test_install_refuses_a_manifest_entry_outside_the_install_root(
    installed_repo: Path, tmp_path: Path
) -> None:
    """A manifest cannot license deleting a path outside the install root."""
    victim = tmp_path / "victim.txt"
    victim.write_text("not jeltz's\n")
    rel = _manifest_entry_outside_the_root(installed_repo, victim)
    result = _run_installer(str(installed_repo))
    _assert_refused(result, rel)
    assert victim.is_file() and victim.read_text() == "not jeltz's\n", (
        "a manifest entry escaping the install root reached a file outside it"
    )


def test_check_refuses_a_manifest_entry_outside_the_install_root(
    installed_repo: Path, tmp_path: Path
) -> None:
    """--check rejects such an entry rather than verifying it.

    The hash is correct, so a check that just recomputes hashes reports a
    clean install - and the next reinstall acts on the entry.
    """
    victim = tmp_path / "victim.txt"
    victim.write_text("not jeltz's\n")
    rel = _manifest_entry_outside_the_root(installed_repo, victim)
    result = _run_installer("--check", str(installed_repo))
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"--check accepted a manifest entry outside the root:\n{combined}"
    )
    assert rel in combined, f"--check does not name the out-of-bounds entry:\n{combined}"


def test_a_refused_install_leaves_the_previous_install_untouched(installed_repo: Path) -> None:
    """The installer validates every target before it writes anything.

    Otherwise a refusal partway through leaves some skills updated, some not,
    and a manifest that describes neither.
    """
    assert len(SOURCE_SKILLS) > 1, "this test needs at least two shipped skills to order"
    drifted = _skills_root(installed_repo) / SOURCE_SKILLS[0] / "SKILL.md"
    drifted.write_text("clobbered\n")
    stray = _skills_root(installed_repo) / SOURCE_SKILLS[-1] / "notes.md"
    stray.write_text("hand-written notes\n")
    manifest = _skills_root(installed_repo) / MANIFEST_NAME
    manifest_before = manifest.read_text()
    result = _run_installer(str(installed_repo))
    _assert_refused(result, f"{SOURCE_SKILLS[-1]}/notes.md")
    assert drifted.read_text() == "clobbered\n", (
        "the install repaired an earlier skill before refusing over a later one"
    )
    assert manifest.read_text() == manifest_before, "the refused install rewrote the manifest"


def test_install_leaves_skills_it_did_not_install_alone(installed_repo: Path) -> None:
    """A directory in the install root that jeltz did not ship is untouched.

    User scope installs into ``$HOME/.claude/skills``, where a developer's own
    skills sit beside the shipped ones. Removing only what the manifest
    records means those are neither deleted nor a reason to refuse: the
    installer looks only at the names it ships.
    """
    mine = _skills_root(installed_repo) / "my-own-skill"
    mine.mkdir()
    (mine / "SKILL.md").write_text("mine\n")
    result = _run_installer(str(installed_repo))
    assert result.returncode == 0, (
        f"an unrelated skill blocked the install:\n{result.stdout}\n{result.stderr}"
    )
    assert (mine / "SKILL.md").read_text() == "mine\n", "the install removed an unrelated skill"


def test_the_shipped_skills_contain_no_symlinks() -> None:
    """Nothing under skills/ is a symlink, which the removal rule relies on.

    ``cp -R`` reproduces a symlink as a symlink, and the installer refuses to
    remove one - it cannot tell a link it placed from a link pointing out of
    the install root. Committing one here would install cleanly once and
    refuse every reinstall afterwards.
    """
    links = [
        str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "skills").rglob("*") if p.is_symlink()
    ]
    assert not links, f"the removal rule cannot handle shipped symlinks: {links}"


# ---------------------------------------------------------------------------
# T27, second review: the paths the installer writes to are attackable too.
#
# The rule covers what is removed. These cover what is created - a manifest
# written through a symlink, a copy that lands through one, a link reported as
# installed when it went somewhere else - and the windows between inspecting a
# path and acting on it.
# ---------------------------------------------------------------------------


def _racing_stub(bin_dir: Path, command: str, matching: str, action: str) -> dict[str, str]:
    """Shadow a command so it sabotages the path it just acted on.

    A race cannot be tested by waiting for one. The window is opened
    deliberately instead: the stub runs the real command, then does to its
    target what a process winning the race would do. What is under test is
    what the installer does next.

    Args:
        bin_dir: Directory to create and place first on PATH.
        command: Command to shadow, e.g. ``rmdir``.
        matching: Shell glob the acted-on path must match, so unrelated
            invocations of the same command pass through untouched.
        action: Shell command run with ``$target`` bound to that path.

    Returns:
        The environment overrides that put the stub on PATH.
    """
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / command
    stub.write_text(
        "#!/bin/sh\n"
        f'/bin/{command} "$@" || exit $?\n'
        'for arg in "$@"; do target=$arg; done\n'
        'case "$target" in\n'
        f"    {matching}) {action} ;;\n"
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_reinstall_refuses_a_manifest_that_is_a_symlink(
    installed_repo: Path, tmp_path: Path
) -> None:
    """The manifest is written, so a symlink there aims that write elsewhere.

    Validating the paths recorded *in* the manifest is no protection if the
    manifest itself is the link: the stamp at the end of an install would
    follow it and overwrite whatever it points at.
    """
    outside = tmp_path / "elsewhere.txt"
    manifest = _skills_root(installed_repo) / MANIFEST_NAME
    shutil.move(str(manifest), str(outside))
    outside.write_text(f"{outside.read_text()}IMPORTANT USER DATA\n")
    manifest.symlink_to(outside)
    result = _run_installer(str(installed_repo))
    _assert_refused(result, MANIFEST_NAME)
    assert "IMPORTANT USER DATA" in outside.read_text(), (
        "the install stamped the manifest through the symlink, over a file outside the root"
    )


def test_check_refuses_a_manifest_that_is_a_symlink(installed_repo: Path, tmp_path: Path) -> None:
    """--check will not certify an install whose manifest is a symlink.

    Every hash still matches, so a check that only recomputes them calls the
    install healthy - and a healthy report is what sends someone back to the
    installer that would then write through it.
    """
    outside = tmp_path / "elsewhere.txt"
    manifest = _skills_root(installed_repo) / MANIFEST_NAME
    shutil.move(str(manifest), str(outside))
    manifest.symlink_to(outside)
    result = _run_installer("--check", str(installed_repo))
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"--check certified a symlinked manifest:\n{combined}"
    assert MANIFEST_NAME in combined, f"--check does not name the manifest:\n{combined}"


def test_a_skill_directory_replaced_mid_install_is_not_written_through(
    installed_repo: Path, tmp_path: Path
) -> None:
    """A symlink appearing after removal must not become the copy's target.

    The gap between emptying a skill directory and writing the new one is the
    installer's own window. ``cp -R`` handed a symlink at the destination
    follows it, so the shipped files land wherever it points.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    env = _racing_stub(
        tmp_path / "bin",
        "rmdir",
        f"*/{SOURCE_SKILLS[0]}",
        f'ln -s "{victim}" "$target"',
    )
    result = _run_installer(str(installed_repo), env=env)
    _assert_refused(result, SOURCE_SKILLS[0])
    assert not list(victim.iterdir()), (
        f"the install copied through the symlink into {victim}: {list(victim.iterdir())}"
    )


def test_a_directory_appearing_at_the_agents_link_is_not_reported_as_installed(
    installed_repo: Path, tmp_path: Path
) -> None:
    """``ln -s`` handed a directory puts the link inside it and exits 0.

    That is the one failure mode a non-overwriting create does not catch by
    itself: codex and antigravity lose discovery while the installer reports
    success. The link that was made has to be checked, not the exit status.
    """
    link = installed_repo / ".agents" / "skills"
    link.unlink()
    link.mkdir()
    theirs = tmp_path / "theirs.txt"
    theirs.write_text("someone else's work\n")
    env = _racing_stub(
        tmp_path / "bin",
        "rmdir",
        "*/.agents/skills",
        f'mkdir "{link}" && /bin/cp "{theirs}" "{link}/"',
    )
    result = _run_installer(str(installed_repo), env=env)
    _assert_refused(result, ".agents/skills")
    assert "installed" not in result.stdout, (
        f"the installer reported success over a broken link:\n{result.stdout}"
    )
    assert (link / "theirs.txt").read_text() == "someone else's work\n", (
        "the directory that appeared lost content to the install"
    )


def test_a_manifest_symlinked_mid_install_is_not_written_through(
    installed_repo: Path, tmp_path: Path
) -> None:
    """The stamp is a create, not an overwrite.

    Refusing a symlinked manifest up front closes the state; this closes the
    window after it, where the path is clear when checked and a link by the
    time the stamp is written.
    """
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("IMPORTANT USER DATA\n")
    env = _racing_stub(
        tmp_path / "bin",
        "rm",
        f"*/{MANIFEST_NAME}",
        f'ln -s "{outside}" "$target"',
    )
    result = _run_installer(str(installed_repo), env=env)
    _assert_refused(result, MANIFEST_NAME)
    assert outside.read_text() == "IMPORTANT USER DATA\n", (
        "the stamp followed a symlink that appeared after the manifest was cleared"
    )


def test_a_skill_path_symlinked_during_the_copy_is_not_written_through(
    installed_repo: Path, tmp_path: Path
) -> None:
    """The copy is built elsewhere, so a symlink at the destination is too late.

    The window that matters is the one right before the new files are put in
    place. Nothing is written at that path - the copy is already complete
    under a name nobody can predict - and the move that places it refuses to
    follow a symlink rather than delivering through it.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    target = _skills_root(installed_repo) / SOURCE_SKILLS[0]
    env = _racing_stub(
        tmp_path / "bin",
        "cp",
        "*/.jeltz-install.*",
        f'ln -s "{victim}" "{target}"',
    )
    result = _run_installer(str(installed_repo), env=env)
    _assert_refused(result, SOURCE_SKILLS[0])
    assert not list(victim.iterdir()), (
        f"the install delivered through the symlink into {victim}: {list(victim.iterdir())}"
    )


def test_a_directory_appearing_at_a_skill_path_keeps_its_contents(
    installed_repo: Path, tmp_path: Path
) -> None:
    """A real directory swallows the move instead of refusing it.

    This is the one case a create cannot report by failing, so it is caught
    afterwards. What must hold either way is that the directory keeps what was
    in it: the name being moved in is unpredictable, so it can collide with
    nothing, and the refusal says where the new copy ended up.
    """
    target = _skills_root(installed_repo) / SOURCE_SKILLS[0]
    theirs = tmp_path / "theirs.txt"
    theirs.write_text("someone else's work\n")
    env = _racing_stub(
        tmp_path / "bin",
        "cp",
        "*/.jeltz-install.*",
        f'mkdir "{target}" && /bin/cp "{theirs}" "{target}/"',
    )
    result = _run_installer(str(installed_repo), env=env)
    _assert_refused(result, SOURCE_SKILLS[0])
    assert (target / "theirs.txt").read_text() == "someone else's work\n", (
        "the directory that appeared lost content to the install"
    )
    assert "installed" not in result.stdout, (
        f"the installer reported success over a directory it could not replace:\n{result.stdout}"
    )


def test_reinstall_refuses_a_retargeted_agents_link(installed_repo: Path, tmp_path: Path) -> None:
    """A link pointing elsewhere is reported, not overwritten.

    Replacing it means overwriting a path without being able to prove jeltz
    made it, and no shell primitive replaces a symlink while refusing the
    regular file that could be there instead by the time the write lands. So
    the link is left alone and the reader is told: an install that cannot act
    safely says so.
    """
    decoy = tmp_path / "decoy-skills"
    decoy.mkdir()
    link = installed_repo / ".agents" / "skills"
    link.unlink()
    link.symlink_to(decoy)
    drifted = _skills_root(installed_repo) / SOURCE_SKILLS[0] / "SKILL.md"
    drifted.write_text("clobbered\n")
    result = _run_installer(str(installed_repo))
    _assert_refused(result, ".agents/skills")
    assert link.is_symlink() and link.readlink() == decoy, (
        "the refused install changed the link it refused over"
    )
    assert drifted.read_text() == "clobbered\n", (
        "the link was found only once the install was already writing"
    )
    assert not list(decoy.iterdir()), f"the install wrote into the decoy: {list(decoy.iterdir())}"


def test_a_file_appearing_at_the_agents_link_is_not_destroyed(
    installed_repo: Path, tmp_path: Path
) -> None:
    """The link is only ever created, never written over.

    A regular file there when the path is inspected is refused as someone's
    work. One that arrives a moment later has to be refused too, or timing
    alone decides whether it survives - so the creation is `ln -s` with no
    -f, which fails rather than replace.
    """
    link = installed_repo / ".agents" / "skills"
    link.unlink()
    link.mkdir()
    env = _racing_stub(
        tmp_path / "bin",
        "rmdir",
        "*/.agents/skills",
        f"printf 'their notes\\n' >\"{link}\"",
    )
    result = _run_installer(str(installed_repo), env=env)
    _assert_refused(result, ".agents/skills")
    assert link.is_file() and link.read_text() == "their notes\n", (
        "the install destroyed a file that appeared where the link goes"
    )


def test_a_refused_install_names_the_copy_it_left_behind(
    installed_repo: Path, tmp_path: Path
) -> None:
    """A half-built copy is reported, not swept away.

    Deleting it would mean walking a directory and removing whatever is in
    it, which is the recursive delete this task exists to remove - and the
    staging path can be replaced after it is created just like any other.
    So it stays, and the refusal says where it is.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    target = _skills_root(installed_repo) / SOURCE_SKILLS[0]
    env = _racing_stub(tmp_path / "bin", "cp", "*/.jeltz-install.*", f'ln -s "{victim}" "{target}"')
    result = _run_installer(str(installed_repo), env=env)
    assert result.returncode != 0, "the install did not refuse"
    leftovers = [p.name for p in _skills_root(installed_repo).glob(".jeltz-install.*")]
    assert len(leftovers) == 1, f"expected the staged copy to remain, found {leftovers}"
    assert leftovers[0] in result.stdout + result.stderr, (
        f"the refusal does not say where the staged copy is:\n{result.stdout}\n{result.stderr}"
    )


def test_a_directory_swapped_in_for_the_staged_copy_keeps_its_contents(
    installed_repo: Path, tmp_path: Path
) -> None:
    """Staging is a path like any other: it can be replaced after it is made.

    An unpredictable name stops anyone getting there first, but nothing stops
    the directory being swapped afterwards. Anything that then tidied up
    "its" staging directory would be deleting someone else's files with no
    way to tell.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    target = _skills_root(installed_repo) / SOURCE_SKILLS[0]
    theirs = tmp_path / "theirs.txt"
    theirs.write_text("someone else's work\n")
    env = _racing_stub(
        tmp_path / "bin",
        "cp",
        "*/.jeltz-install.*",
        f'staged=${{target%/}}; /bin/mv "$staged" "$staged.real"; mkdir "$staged"; '
        f'/bin/cp "{theirs}" "$staged/"; ln -s "{victim}" "{target}"',
    )
    result = _run_installer(str(installed_repo), env=env)
    assert result.returncode != 0, "the install did not refuse"
    swapped = [p for p in _skills_root(installed_repo).glob(".jeltz-install.*") if p.is_dir()]
    survivors = [p / "theirs.txt" for p in swapped if (p / "theirs.txt").is_file()]
    assert survivors, (
        f"the install deleted the contents of a directory it did not create: {swapped}"
    )


def test_a_substituted_staged_copy_is_never_recorded_as_jeltz_content(
    installed_repo: Path, tmp_path: Path
) -> None:
    """The manifest records what jeltz ships, not what it finds on disk.

    A staged copy replaced after it was written places cleanly - there is
    nothing at the destination for the rename to object to. Recording the
    result would launder someone else's file into jeltz's own inventory, and
    the next ordinary reinstall would then delete it as its own.
    """
    theirs = tmp_path / "theirs.txt"
    theirs.write_text("IMPORTANT USER DATA\n")
    target = _skills_root(installed_repo) / SOURCE_SKILLS[0]
    env = _racing_stub(
        tmp_path / "bin",
        "cp",
        "*/.jeltz-install.*",
        f'staged=${{target%/}}; /bin/mv "$staged" "$staged.real"; mkdir "$staged"; '
        f'/bin/cp "{theirs}" "$staged/theirs.txt"',
    )
    result = _run_installer(str(installed_repo), env=env)
    _assert_refused(result, SOURCE_SKILLS[0])
    assert (target / "theirs.txt").read_text() == "IMPORTANT USER DATA\n", (
        "the substituted file was destroyed"
    )
    manifest = (_skills_root(installed_repo) / MANIFEST_NAME).read_text()
    assert "theirs.txt" not in manifest, f"a file jeltz never shipped was recorded:\n{manifest}"
    again = _run_installer(str(installed_repo))
    _assert_refused(again, "theirs.txt")
    assert (target / "theirs.txt").read_text() == "IMPORTANT USER DATA\n", (
        "the next reinstall deleted the file as if jeltz had installed it"
    )


def test_an_extra_file_arriving_with_a_staged_copy_stops_the_install(
    installed_repo: Path, tmp_path: Path
) -> None:
    """Checking that every shipped file arrived is not the same as equality.

    A copy can be intact and still carry something jeltz never shipped. The
    installed directory has to be exactly the shipped tree, or the install
    certifies a skill with a stranger's file in it - the very state a
    reinstall then refuses to touch.
    """
    target = _skills_root(installed_repo) / SOURCE_SKILLS[0]
    env = _racing_stub(
        tmp_path / "bin",
        "cp",
        "*/.jeltz-install.*",
        'staged=${target%/}; printf "not shipped\\n" >"$staged/extra.md"',
    )
    result = _run_installer(str(installed_repo), env=env)
    _assert_refused(result, "extra.md")
    assert (target / "extra.md").read_text() == "not shipped\n", (
        "the install destroyed the file it refused over"
    )
