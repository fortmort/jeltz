"""Antigravity adapter: drive `agy -p` as the reviewer backend (T9).

A fresh review invokes the installed skeptical-reviewer skill by slash
command, enforces the canonical verdict schema via --json-schema, and opens
a project context with --new-project (3.4 sharp edge 1: project skills load
only with a project); a re-review resumes the prior conversation with
--conversation <id> (D1). Output is one JSON envelope (verified live
against agy 1.1.13) whose `response` carries the reviewer's text already
fenced, so it passes to the T4 parser verbatim. Sharp edge 2 is handled
here and not deferred to the template method: a headless permission denial
reports status SUCCESS with an empty `response` and puts the reason only on
stderr, which only the adapter can see - so an empty response raises
AdapterProcessError carrying that stderr note, never a silent pass.
"""

import json
from pathlib import Path

from review.adapter import AdapterProcessError, ReviewerAdapter, run_backend
from review.verdict import SCHEMA_PATH

DEFAULT_TIMEOUT = 600.0
DEFAULT_MODEL = "gemini-3.1-pro-high"


class AgyAdapter(ReviewerAdapter):
    """Reviewer backend running `agy -p` against the installed skill."""

    def __init__(
        self,
        agy_bin: str = "agy",
        timeout: float = DEFAULT_TIMEOUT,
        model: str = DEFAULT_MODEL,
    ) -> None:
        """Configure the backend invocation.

        Args:
            agy_bin: The agy executable to run.
            timeout: Seconds before a hung backend is killed.
            model: The agy model id to review with.
        """
        self.agy_bin = agy_bin
        self.timeout = timeout
        self.model = model

    def _send(
        self, prompt: str, worktree: Path, thread_id: str | None
    ) -> tuple[str, str]:
        """Run one agy print-mode turn and return its output and thread id.

        Args:
            prompt: The text to send; a fresh review is framed with the
                installed-skill invocation, an existing conversation gets
                it raw.
            worktree: The review checkout; agy runs with it as cwd.
            thread_id: Existing conversation to resume, or None.

        Returns:
            The envelope's `response` (agy fences verdict JSON itself) and
            the conversation id for later resumes.

        Raises:
            AdapterProcessError: If the binary is missing, exits nonzero,
                exceeds the timeout, emits a broken or non-SUCCESS
                envelope, or reports SUCCESS with an empty response
                (3.4 sharp edge 2: a headless permission denial).
        """
        argv = [
            self.agy_bin,
            "-p",
            prompt if thread_id else f"/skeptical-reviewer HEAD\n\n{prompt}",
            "--output-format",
            "json",
            "--json-schema",
            str(SCHEMA_PATH),
            "--model",
            self.model,
        ]
        if thread_id is None:
            argv.append("--new-project")
        else:
            argv.extend(["--conversation", thread_id])
        stdout, stderr = run_backend("agy", argv, worktree, self.timeout)
        return _parse_envelope(stdout, stderr)


def _parse_envelope(stdout: str, stderr: str) -> tuple[str, str]:
    """Extract the response and conversation id from the agy envelope.

    Raises:
        AdapterProcessError: On an unparseable or non-object envelope, a
            non-SUCCESS status, or a SUCCESS envelope whose response is
            empty, missing, or not a string - the live signature of a
            headless permission denial, whose reason exists only on
            stderr (sharp edge 2).
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterProcessError(
            f"agy emitted an unparseable envelope: {stdout.strip()[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise AdapterProcessError(
            f"agy envelope is not an object: {stdout.strip()[:200]}"
        )
    status = data.get("status", "")
    if status != "SUCCESS":
        raise AdapterProcessError(
            f"agy reported status {status or '(none)'}: {stderr.strip()}"
        )
    response = data.get("response", "")
    if not isinstance(response, str) or not response.strip():
        raise AdapterProcessError(
            "agy reported SUCCESS with an empty response - the signature "
            f"of a headless permission denial: {stderr.strip()}"
        )
    conversation = data.get("conversation_id", "")
    if not isinstance(conversation, str):
        # A non-string id can never be resumed (and would poison a later
        # resume argv), so it degrades exactly like a missing one: the
        # template method raises ThreadContinuityError (D1).
        conversation = ""
    return response, conversation
