"""Tests for T6: the disposable review worktree and its integrity check.

The worktree hands the reviewer a real commit containing the developer's
uncommitted and untracked work - preserving the `$skeptical-reviewer HEAD`
mental model - without ever touching the developer's tree, and is destroyed
afterwards even on a crash. The integrity check is R7's mechanical
enforcement of D5: tool allowlists cannot stop a shell-equipped reviewer from
editing its checkout, so the orchestrator snapshots the checkout before the
review and fails the review on any tracked mutation, HEAD movement, or novel
untracked write afterwards. Verification caches are allowlisted by explicit
pattern, not via .gitignore, so a novel write shows up even when the consumer
repo ignores it.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from review.worktree import (
    IntegrityError,
    reap_stale_worktrees,
    review_worktree,
    snapshot,
    verify_integrity,
)
from tests.conftest import git

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_worktree_carries_the_wip_as_a_real_commit(dirty_repo: Path) -> None:
    """Uncommitted and untracked work arrives as one WIP commit on HEAD."""
    source_head = git(dirty_repo, "rev-parse", "HEAD").strip()
    with review_worktree(dirty_repo, "wip: bump the value") as worktree:
        assert git(worktree, "status", "--porcelain") == ""
        subject = git(worktree, "log", "-1", "--format=%s").strip()
        assert subject == "wip: bump the value"
        assert git(worktree, "rev-parse", "HEAD~1").strip() == source_head
        assert (worktree / "src.py").read_text() == "VALUE = 2\n"
        assert (worktree / "pkg" / "mod.py").read_text() == "NEW = True\n"


def test_developer_tree_is_untouched(dirty_repo: Path) -> None:
    """Nothing done in the worktree leaks back into the developer's tree."""
    before_status = git(dirty_repo, "status", "--porcelain")
    before_head = git(dirty_repo, "rev-parse", "HEAD")
    with review_worktree(dirty_repo, "wip: hands off") as worktree:
        (worktree / "src.py").write_text("VALUE = 999\n")
    assert git(dirty_repo, "status", "--porcelain") == before_status
    assert git(dirty_repo, "rev-parse", "HEAD") == before_head
    assert (dirty_repo / "src.py").read_text() == "VALUE = 2\n"


def test_worktree_is_destroyed_on_clean_exit(dirty_repo: Path) -> None:
    """The checkout and its git registration are gone after the context."""
    with review_worktree(dirty_repo, "wip: cleanup") as worktree:
        kept = worktree
    assert not kept.exists()
    assert str(kept) not in git(dirty_repo, "worktree", "list")


def test_worktree_is_destroyed_on_crash(dirty_repo: Path) -> None:
    """Cleanup is guaranteed when the review loop dies mid-flight."""
    with pytest.raises(RuntimeError, match="boom"):
        with review_worktree(dirty_repo, "wip: crash") as worktree:
            kept = worktree
            raise RuntimeError("boom")
    assert not kept.exists()
    assert str(kept) not in git(dirty_repo, "worktree", "list")


def test_cleanup_survives_a_deleted_worktree(dirty_repo: Path) -> None:
    """Even if the checkout vanished, no stale registration is left behind."""
    with review_worktree(dirty_repo, "wip: nuked") as worktree:
        shutil.rmtree(worktree)
    assert str(worktree) not in git(dirty_repo, "worktree", "list")


def test_killed_process_worktree_is_reaped(dirty_repo: Path, tmp_path: Path) -> None:
    """SIGKILL skips finally blocks (R5 hook timeouts kill processes), so a
    killed review leaks its worktree; reaping recovers checkout and
    registration from outside the dead process."""
    script = tmp_path / "hold_review.py"
    script.write_text(
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "from review.worktree import review_worktree\n"
        "with review_worktree(Path(sys.argv[1]), 'wip: doomed') as worktree:\n"
        "    print(worktree, flush=True)\n"
        "    time.sleep(300)\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(script), str(dirty_repo)],
        stdout=subprocess.PIPE,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
    )
    assert process.stdout is not None
    leaked = Path(process.stdout.readline().strip())
    process.kill()
    process.wait()
    assert leaked.exists(), "expected the kill to leak the worktree"
    assert str(leaked) in git(dirty_repo, "worktree", "list")
    reaped = reap_stale_worktrees(dirty_repo)
    assert str(leaked) in reaped
    assert not leaked.exists()
    assert str(leaked) not in git(dirty_repo, "worktree", "list")


def test_reap_spares_live_reviews(dirty_repo: Path) -> None:
    """A worktree owned by a running process is not stale and must survive."""
    with review_worktree(dirty_repo, "wip: alive") as worktree:
        assert reap_stale_worktrees(dirty_repo) == []
        assert worktree.exists()


def test_reap_treats_a_missing_pid_marker_as_stale(dirty_repo: Path) -> None:
    """No owner marker means no proof of life; the worktree is reclaimed."""
    with review_worktree(dirty_repo, "wip: orphan") as worktree:
        (worktree.parent / "pid").unlink()
        assert str(worktree) in reap_stale_worktrees(dirty_repo)
        assert not worktree.exists()


def test_clean_tree_still_produces_a_wip_commit(dirty_repo: Path) -> None:
    """With nothing uncommitted, the WIP commit is empty but still exists."""
    git(dirty_repo, "add", "-A")
    git(dirty_repo, "commit", "-m", "chore: absorb the WIP")
    with review_worktree(dirty_repo, "wip: empty review") as worktree:
        subject = git(worktree, "log", "-1", "--format=%s").strip()
        assert subject == "wip: empty review"


def test_untouched_checkout_passes_integrity(dirty_repo: Path) -> None:
    """A reviewer that only reads passes; pre-snapshot untracked files too."""
    with review_worktree(dirty_repo, "wip: honest") as worktree:
        (worktree / "notes.txt").write_text("pre-existing before snapshot\n")
        before = snapshot(worktree)
        verify_integrity(worktree, before)


def test_cache_writes_are_allowlisted(dirty_repo: Path) -> None:
    """D5 permits verification artifacts: pycache, pytest cache, venv, coverage."""
    with review_worktree(dirty_repo, "wip: verify run") as worktree:
        before = snapshot(worktree)
        (worktree / "__pycache__").mkdir()
        (worktree / "__pycache__" / "src.cpython-311.pyc").write_bytes(b"\x00")
        (worktree / ".pytest_cache" / "v").mkdir(parents=True)
        (worktree / ".pytest_cache" / "v" / "stamp").write_text("x\n")
        (worktree / ".venv" / "bin").mkdir(parents=True)
        (worktree / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (worktree / ".coverage").write_text("coverage data\n")
        verify_integrity(worktree, before)


def test_packaging_metadata_writes_are_allowlisted(dirty_repo: Path) -> None:
    """Build metadata from a verify run is an artifact, not a mutation.

    A project whose test target installs itself (jeltz itself does, since
    T22: ``pip install . --group dev``) makes setuptools write an .egg-info
    directory into the tree it builds from. A reviewer running ``make
    verify`` in the checkout must not fail integrity for that.
    """
    with review_worktree(dirty_repo, "wip: verify run") as worktree:
        before = snapshot(worktree)
        egg_info = worktree / "consumer.egg-info"
        egg_info.mkdir()
        (egg_info / "PKG-INFO").write_text("Metadata-Version: 2.4\n")
        (egg_info / "SOURCES.txt").write_text("pyproject.toml\n")
        verify_integrity(worktree, before)


def test_tracked_edit_fails_the_review(dirty_repo: Path) -> None:
    """R7: a reviewer edit to tracked source fails the review by name."""
    with review_worktree(dirty_repo, "wip: tamper") as worktree:
        before = snapshot(worktree)
        (worktree / "src.py").write_text("VALUE = 3\n")
        with pytest.raises(IntegrityError, match="src.py"):
            verify_integrity(worktree, before)


def test_tracked_deletion_fails_the_review(dirty_repo: Path) -> None:
    """Deleting tracked content is a mutation, not a cache write."""
    with review_worktree(dirty_repo, "wip: delete") as worktree:
        before = snapshot(worktree)
        (worktree / "docs.md").unlink()
        with pytest.raises(IntegrityError, match="docs.md"):
            verify_integrity(worktree, before)


def test_novel_untracked_write_fails_the_review(dirty_repo: Path) -> None:
    """A new file outside the cache allowlist is flagged, not shrugged off."""
    with review_worktree(dirty_repo, "wip: backdoor") as worktree:
        before = snapshot(worktree)
        (worktree / "backdoor.py").write_text("EVIL = True\n")
        with pytest.raises(IntegrityError, match="backdoor.py"):
            verify_integrity(worktree, before)


def test_gitignored_novel_write_still_fails_the_review(dirty_repo: Path) -> None:
    """The consumer's .gitignore is not the allowlist: ignored writes surface."""
    with review_worktree(dirty_repo, "wip: ignored channel") as worktree:
        before = snapshot(worktree)
        (worktree / "sneak.log").write_text("payload\n")
        with pytest.raises(IntegrityError, match="sneak.log"):
            verify_integrity(worktree, before)


def test_assume_unchanged_edit_still_fails_the_review(dirty_repo: Path) -> None:
    """R7: an index flag must not hide a tracked edit from the check.

    `git update-index --assume-unchanged` makes `git status` report a
    mutated file as clean, so the integrity check must compare actual
    tracked content, not trust the index.
    """
    with review_worktree(dirty_repo, "wip: index games") as worktree:
        before = snapshot(worktree)
        git(worktree, "update-index", "--assume-unchanged", "src.py")
        (worktree / "src.py").write_text("VALUE = 3\n")
        assert git(worktree, "status", "--porcelain") == ""
        with pytest.raises(IntegrityError, match="src.py"):
            verify_integrity(worktree, before)


def test_assume_unchanged_deletion_fails_the_review(dirty_repo: Path) -> None:
    """A deletion hidden by the index flag is still a tracked mutation."""
    with review_worktree(dirty_repo, "wip: vanishing act") as worktree:
        before = snapshot(worktree)
        git(worktree, "update-index", "--assume-unchanged", "docs.md")
        (worktree / "docs.md").unlink()
        assert git(worktree, "status", "--porcelain") == ""
        with pytest.raises(IntegrityError, match="docs.md"):
            verify_integrity(worktree, before)


def test_assume_unchanged_type_swap_fails_the_review(dirty_repo: Path) -> None:
    """Replacing a tracked file with a symlink is a mutation even when hidden."""
    with review_worktree(dirty_repo, "wip: type games") as worktree:
        before = snapshot(worktree)
        git(worktree, "update-index", "--assume-unchanged", "docs.md")
        (worktree / "docs.md").unlink()
        (worktree / "docs.md").symlink_to("src.py")
        assert git(worktree, "status", "--porcelain") == ""
        with pytest.raises(IntegrityError, match="docs.md"):
            verify_integrity(worktree, before)


def test_untracked_symlink_is_materialized_as_a_symlink(dirty_repo: Path) -> None:
    """The reviewer must see the developer's symlink, not a copy of its target."""
    (dirty_repo / "link.py").symlink_to("src.py")
    with review_worktree(dirty_repo, "wip: faithful links") as worktree:
        assert (worktree / "link.py").is_symlink()
        assert str((worktree / "link.py").readlink()) == "src.py"


def test_reviewer_commit_fails_the_review(dirty_repo: Path) -> None:
    """Committing over the WIP moves HEAD; the verdict would judge other code."""
    with review_worktree(dirty_repo, "wip: rewrite") as worktree:
        before = snapshot(worktree)
        (worktree / "src.py").write_text("VALUE = 3\n")
        git(worktree, "add", "-A")
        git(
            worktree,
            "-c",
            "user.name=Reviewer",
            "-c",
            "user.email=reviewer@example.invalid",
            "commit",
            "-m",
            "sneaky rewrite",
        )
        with pytest.raises(IntegrityError, match="HEAD"):
            verify_integrity(worktree, before)
