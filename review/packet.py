"""Build the deterministic review packet (T6).

The packet is the reviewer's complete view of the submitted work: the WIP
message standing in for a commit message, the optional TODO ref (Q1), the
tracked diff, the untracked file list, and the tail of ``make verify``
output. Its diff hash keys a verdict to the exact tree state that was
reviewed, so it covers tracked changes and untracked file content but not
gitignored files.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from review.gitcmd import git, git_bytes, untracked_files

DEFAULT_SIZE_CEILING = 200_000
VERIFY_TAIL_LINES = 50


class PacketTooLargeError(Exception):
    """The rendered packet exceeds the size ceiling (termination condition 4)."""


@dataclass(frozen=True)
class Packet:
    """The reviewer's complete view of one submitted tree state."""

    wip_message: str
    todo_ref: str | None
    diff: str
    untracked: tuple[str, ...]
    verify_tail: str
    diff_hash: str

    def render(self) -> str:
        """Render the packet as the text handed to the reviewer."""
        untracked = "\n".join(self.untracked) or "(none)"
        return (
            "# Review packet\n\n"
            f"TODO item: {self.todo_ref or '(none)'}\n"
            f"WIP message: {self.wip_message}\n"
            f"Diff hash: {self.diff_hash}\n\n"
            "## Diff vs HEAD\n\n"
            f"{self.diff or '(no tracked changes)'}\n\n"
            "## Untracked files\n\n"
            f"{untracked}\n\n"
            "## make verify (tail)\n\n"
            f"{self.verify_tail or '(not run)'}\n"
        )


def tree_state_hash(repo: Path) -> str:
    """Hash the reviewable tree state: tracked diff plus untracked content.

    The binary diff covers every tracked change (staged or not); untracked
    files contribute their path plus either a content digest and the
    git-significant mode (100755 vs 100644 - git commits the executable
    bit, so flipping it changes the WIP commit under review) or, for a
    symlink, its literal target - never the target's content, so
    retargeting a link changes the hash and a broken link still hashes.

    Args:
        repo: The repository whose working tree is being reviewed.

    Returns:
        A sha256 hex digest, stable for a given tree state.
    """
    digest = hashlib.sha256()
    digest.update(git_bytes(repo, "diff", "HEAD", "--binary"))
    for rel in untracked_files(repo):
        file = repo / rel
        digest.update(rel.encode())
        digest.update(b"\0")
        if file.is_symlink():
            digest.update(b"link\0")
            digest.update(str(file.readlink()).encode())
        else:
            mode = b"100755" if file.stat().st_mode & 0o111 else b"100644"
            digest.update(b"file\0" + mode + b"\0")
            digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def build_packet(
    repo: Path,
    wip_message: str,
    todo_ref: str | None = None,
    verify_output: str = "",
    size_ceiling: int = DEFAULT_SIZE_CEILING,
) -> Packet:
    """Build the review packet for the repository's current tree state.

    Args:
        repo: The repository whose working tree is being reviewed.
        wip_message: WIP message standing in for a commit message.
        todo_ref: Optional TODO item reference (Q1: may be absent).
        verify_output: Full ``make verify`` output; only its tail is kept.
        size_ceiling: Maximum rendered packet size in characters.

    Returns:
        The deterministic packet for this tree state.

    Raises:
        PacketTooLargeError: If the rendered packet exceeds the ceiling; the
            caller escalates to a human (termination condition 4) rather
            than reviewing a truncated view.
    """
    tail = "\n".join(verify_output.splitlines()[-VERIFY_TAIL_LINES:])
    packet = Packet(
        wip_message=wip_message,
        todo_ref=todo_ref,
        diff=git(repo, "diff", "HEAD"),
        untracked=untracked_files(repo),
        verify_tail=tail,
        diff_hash=tree_state_hash(repo),
    )
    size = len(packet.render())
    if size > size_ceiling:
        raise PacketTooLargeError(
            f"rendered packet is {size} chars, over the {size_ceiling} ceiling"
        )
    return packet
