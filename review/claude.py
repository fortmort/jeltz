"""Claude adapter: drive `claude -p` as the fallback reviewer backend (T11).

A fresh review opens a new session on a generated ``--session-id`` (a
UUID that did not previously exist) whose echo in the envelope is
verified, never trusted, and a re-review resumes that same session with
``--resume <id>`` (D1). The canonical verdict schema is
passed inline via --json-schema minus its 2020-12 draft declaration,
which claude 2.1.233 rejects (probed live); the shipped tool allowlist
rides --tools: omitting Edit and Write keeps consumer PostToolUse hooks from
firing inside the reviewer, while Bash-issued writes stay caught by the
T6 integrity check (R7). Per 3.7 the adapter refuses to run while
ANTHROPIC_API_KEY is set (it flips Claude Code to API billing) unless
API billing is explicitly allowed, and never passes --bare, whose auth
is strictly API-key based. The fresh framing points at the project skill
path explicitly: the T3 spike found a user-scope copy of the same skill
name shadows the project copy headless.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any

from review.adapter import (
    AdapterProcessError,
    ReviewerAdapter,
    ThreadContinuityError,
    fence_bare_verdict,
    run_backend,
    telemetry,
    tool_policy,
)
from review.verdict import draftless_schema

DEFAULT_TIMEOUT = 600.0


class ClaudeAdapter(ReviewerAdapter):
    """Reviewer backend running `claude -p` against the project skill."""

    def __init__(
        self,
        claude_bin: str = "claude",
        timeout: float = DEFAULT_TIMEOUT,
        allow_api_billing: bool = False,
    ) -> None:
        """Configure the backend invocation.

        Args:
            claude_bin: The claude executable to run.
            timeout: Seconds before a hung backend is killed.
            allow_api_billing: Run even when ANTHROPIC_API_KEY is set in
                the environment (3.7: the key flips Claude Code from
                subscription to API billing).
        """
        self.claude_bin = claude_bin
        self.timeout = timeout
        self.allow_api_billing = allow_api_billing

    def _send(
        self, prompt: str, worktree: Path, thread_id: str | None
    ) -> tuple[str, str]:
        """Run one claude single-prompt turn and return its output and id.

        Args:
            prompt: The text to send; a fresh review is framed with the
                skill invocation, an existing session gets it raw.
            worktree: The review checkout; claude runs with it as cwd.
            thread_id: Existing session to resume, or None.

        Returns:
            The envelope's result text (fenced if it arrived as bare
            verdict JSON) and the session id for later resumes.

        Raises:
            AdapterProcessError: If ANTHROPIC_API_KEY is set without the
                API-billing opt-in, the binary is missing, exits nonzero,
                exceeds the timeout, emits a broken envelope, or reports
                a failed run (is_error or a non-success subtype).
            ThreadContinuityError: If a fresh review's envelope does not
                echo the generated session id (D1: the id is verified,
                not trusted - an unechoed session, existing or unrelated,
                would be wrongly resumed by every later round).
        """
        if "ANTHROPIC_API_KEY" in os.environ and not self.allow_api_billing:
            raise AdapterProcessError(
                "ANTHROPIC_API_KEY is set: claude would run on API billing "
                "(3.7); unset it or opt in with --allow-api-billing"
            )
        argv = [self.claude_bin]
        generated = str(uuid.uuid4())
        if thread_id is None:
            argv.extend(["--session-id", generated])
            # The T3 spike: a user-scope copy of the same skill name
            # shadows the project copy headless, so point at the project
            # skill path explicitly.
            prompt = (
                "/skeptical-reviewer HEAD\n\n"
                "Use the project skill at "
                ".claude/skills/skeptical-reviewer in this checkout, "
                f"not any user-scope copy of the same name.\n\n{prompt}"
            )
        else:
            argv.extend(["--resume", thread_id])
        argv.extend(
            [
                "--tools",
                ",".join(tool_policy("claude").allow),
                "--output-format",
                "json",
                "--json-schema",
                # claude rejects a schema declaring the 2020-12 draft
                # (probed live), so the declaration is dropped.
                json.dumps(draftless_schema()),
                "-p",
                prompt,
            ]
        )
        stdout, stderr = run_backend("claude", argv, worktree, self.timeout)
        text, session, cost = _parse_envelope(stdout, stderr)
        if thread_id is None and session != generated:
            # Only this adapter knows the generated id, so the fresh-path
            # echo is verified here; the resume path stays with the
            # template method's continuity check.
            raise ThreadContinuityError(
                f"claude answered on session {session or '(none)'} "
                f"instead of the generated {generated}"
            )
        if cost:
            self._record_cost(cost)
        return fence_bare_verdict(text), session


def _parse_envelope(stdout: str, stderr: str) -> tuple[str, str, dict[str, Any]]:
    """Extract text, session id, and cost data from the claude envelope.

    Raises:
        AdapterProcessError: On an unparseable or non-object envelope, a
            failed run (is_error true or subtype not success), or a
            non-string result field without structured output.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterProcessError(
            f"claude emitted an unparseable envelope: {stdout.strip()[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise AdapterProcessError(
            f"claude envelope is not an object: {stdout.strip()[:200]}"
        )
    subtype = data.get("subtype", "")
    text = data.get("result", "")
    if data.get("is_error") or subtype != "success":
        raise AdapterProcessError(
            "claude run failed "
            f"(subtype {subtype or '(none)'}): "
            f"{str(text).strip()[:200]} {stderr.strip()}".strip()
        )
    structured = data.get("structured_output")
    if isinstance(structured, dict):
        text = json.dumps(structured)
    elif not isinstance(text, str):
        raise AdapterProcessError(
            f"claude envelope result is not a string: {stdout.strip()[:200]}"
        )
    session = data.get("session_id", "")
    if not isinstance(session, str):
        # A non-string id can never be resumed (and would poison a later
        # resume argv), so it degrades exactly like a missing one: the
        # template method raises ThreadContinuityError (D1).
        session = ""
    return text, session, telemetry(data)
