"""Tests for T6: the deterministic review packet.

The packet is the reviewer's complete view of the submitted work: the WIP
message standing in for a commit message, the optional TODO ref (Q1), the
tracked diff, the untracked file list, and the tail of `make verify` output.
Its diff hash is what `.jeltz/review/state.json` keys a verdict to - a review
is valid only for the tree state it examined - so the hash must be
deterministic for a given tree and must change when any reviewed content,
tracked or untracked, changes. The size ceiling is termination condition 4:
a packet too large to review escalates instead of silently truncating.
"""

import re
from pathlib import Path

import pytest

from review.packet import (
    VERIFY_TAIL_LINES,
    PacketTooLargeError,
    build_packet,
)
from tests.conftest import git


def test_packet_captures_tree_state(dirty_repo: Path) -> None:
    """The packet carries WIP message, TODO ref, diff, untracked, and tail."""
    packet = build_packet(
        dirty_repo,
        wip_message="wip: bump the value",
        todo_ref="T6",
        verify_output="lint ok\ntests ok\n",
    )
    assert packet.wip_message == "wip: bump the value"
    assert packet.todo_ref == "T6"
    assert "VALUE = 2" in packet.diff
    assert "pkg/mod.py" in packet.untracked
    assert packet.verify_tail.splitlines()[-1] == "tests ok"


def test_todo_ref_is_optional(dirty_repo: Path) -> None:
    """Q1: the Problem B audience has no TODO item; the packet still builds."""
    packet = build_packet(dirty_repo, wip_message="wip: no todo item")
    assert packet.todo_ref is None
    assert isinstance(packet.render(), str)


def test_packet_is_deterministic(dirty_repo: Path) -> None:
    """Identical tree state and inputs produce an identical packet."""
    first = build_packet(
        dirty_repo, wip_message="wip: same", todo_ref="T6", verify_output="ok\n"
    )
    second = build_packet(
        dirty_repo, wip_message="wip: same", todo_ref="T6", verify_output="ok\n"
    )
    assert first == second
    assert first.diff_hash == second.diff_hash
    assert first.render() == second.render()


def test_diff_hash_is_a_sha256_hex_digest(dirty_repo: Path) -> None:
    """The hash is a lowercase hex sha256, stable enough for state.json."""
    packet = build_packet(dirty_repo, wip_message="wip: hash shape")
    assert re.fullmatch(r"[0-9a-f]{64}", packet.diff_hash)


def test_diff_hash_tracks_tracked_changes(dirty_repo: Path) -> None:
    """Editing a tracked file changes the hash: the old review is stale."""
    before = build_packet(dirty_repo, wip_message="wip: x").diff_hash
    (dirty_repo / "src.py").write_text("VALUE = 3\n")
    after = build_packet(dirty_repo, wip_message="wip: x").diff_hash
    assert before != after


def test_diff_hash_tracks_untracked_content(dirty_repo: Path) -> None:
    """Untracked files are reviewed content; editing one changes the hash."""
    before = build_packet(dirty_repo, wip_message="wip: x").diff_hash
    (dirty_repo / "pkg" / "mod.py").write_text("NEW = False\n")
    after = build_packet(dirty_repo, wip_message="wip: x").diff_hash
    assert before != after


def test_diff_hash_tracks_untracked_symlink_target(dirty_repo: Path) -> None:
    """Retargeting a symlink changes the tree state even with equal content."""
    (dirty_repo / "target_a.txt").write_text("same content\n")
    (dirty_repo / "target_b.txt").write_text("same content\n")
    (dirty_repo / "current").symlink_to("target_a.txt")
    before = build_packet(dirty_repo, wip_message="wip: x").diff_hash
    (dirty_repo / "current").unlink()
    (dirty_repo / "current").symlink_to("target_b.txt")
    after = build_packet(dirty_repo, wip_message="wip: x").diff_hash
    assert before != after


def test_diff_hash_tracks_untracked_executable_bit(dirty_repo: Path) -> None:
    """chmod +x changes the committed mode (100755), so the hash must move."""
    tool = dirty_repo / "tool.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o644)
    before = build_packet(dirty_repo, wip_message="wip: x").diff_hash
    tool.chmod(0o755)
    after = build_packet(dirty_repo, wip_message="wip: x").diff_hash
    assert before != after


def test_ignored_files_do_not_affect_the_packet(dirty_repo: Path) -> None:
    """Gitignored files are neither listed nor hashed (R3: no false staleness)."""
    before = build_packet(dirty_repo, wip_message="wip: x")
    (dirty_repo / "junk.log").write_text("noise\n")
    after = build_packet(dirty_repo, wip_message="wip: x")
    assert "junk.log" not in after.untracked
    assert after.diff_hash == before.diff_hash


def test_clean_tree_builds_an_empty_but_stable_packet(dirty_repo: Path) -> None:
    """A tree with nothing to review still hashes deterministically."""
    git(dirty_repo, "add", "-A")
    git(dirty_repo, "commit", "-m", "chore: absorb the WIP")
    packet = build_packet(dirty_repo, wip_message="wip: nothing left")
    assert packet.diff == ""
    assert packet.untracked == ()
    assert re.fullmatch(r"[0-9a-f]{64}", packet.diff_hash)


def test_verify_tail_keeps_only_the_last_lines(dirty_repo: Path) -> None:
    """Long verify output is truncated to its tail, where failures land."""
    lines = [f"line {i}" for i in range(VERIFY_TAIL_LINES * 3)]
    packet = build_packet(
        dirty_repo, wip_message="wip: tail", verify_output="\n".join(lines)
    )
    tail = packet.verify_tail.splitlines()
    assert len(tail) == VERIFY_TAIL_LINES
    assert tail[-1] == lines[-1]
    assert lines[0] not in tail


def test_size_ceiling_is_enforced(dirty_repo: Path) -> None:
    """A packet over the ceiling raises instead of truncating (condition 4)."""
    with pytest.raises(PacketTooLargeError):
        build_packet(dirty_repo, wip_message="wip: too big", size_ceiling=10)


def test_render_carries_every_section(dirty_repo: Path) -> None:
    """The rendered packet exposes every field the reviewer needs to see."""
    packet = build_packet(
        dirty_repo,
        wip_message="wip: render",
        todo_ref="T6",
        verify_output="verify tail line\n",
    )
    rendered = packet.render()
    for expected in (
        "wip: render",
        "T6",
        "VALUE = 2",
        "pkg/mod.py",
        "verify tail line",
        packet.diff_hash,
    ):
        assert expected in rendered


def test_untracked_non_ascii_paths_are_real_paths(dirty_repo: Path) -> None:
    """Git C-quotes non-ASCII names; the packet must carry the real path.

    The quoted form ("caf\\303\\251.py") names no file on disk, so it
    would both mislead the reviewer and crash the diff hash.
    """
    git(dirty_repo, "config", "core.quotepath", "true")
    (dirty_repo / "caf\u00e9.py").write_text("NEW = True\n")
    packet = build_packet(dirty_repo, wip_message="wip: unicode filename")
    assert "caf\u00e9.py" in packet.untracked
