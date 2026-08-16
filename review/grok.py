"""Grok adapter: drive `grok -p` as the reviewer backend (T10).

Grok needs no install step: it discovers `.claude/skills/` and
`~/.claude/skills/` natively (3.5), so a fresh review first runs
`grok inspect` in the review checkout and asserts the skeptical-reviewer
skill is actually discovered. The review itself passes the canonical
verdict schema inline via --json-schema (grok 1.0.4 rejects a file path),
applies the shipped tool allowlist via --tools, and invokes the skill by
slash command; a re-review resumes the prior session with --resume <id>
(D1). Output is one JSON envelope (verified live) whose `text` carries the
schema-constrained output as bare JSON, normalized into the fenced form
the T4 parser expects. The load-bearing sharp edge (T3 spike): a run that
dies inside grok ends with exit 0, stopReason "cancelled", and
narration-only text, so the adapter asserts on the stop reason, never on
exit status. Each turn's total_cost_usd and usage are recorded into the
round's ReviewResult costs - review state, the calibration data for the
round budget.
"""

import json
from pathlib import Path
from typing import Any

from review.adapter import (
    AdapterProcessError,
    ReviewerAdapter,
    fence_bare_verdict,
    run_backend,
    telemetry,
    tool_policy,
)
from review.verdict import load_schema

DEFAULT_TIMEOUT = 600.0


class GrokAdapter(ReviewerAdapter):
    """Reviewer backend running `grok -p` against the discovered skill."""

    def __init__(
        self, grok_bin: str = "grok", timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        """Configure the backend invocation.

        Args:
            grok_bin: The grok executable to run.
            timeout: Seconds before a hung backend is killed.
        """
        self.grok_bin = grok_bin
        self.timeout = timeout

    def _send(
        self, prompt: str, worktree: Path, thread_id: str | None
    ) -> tuple[str, str]:
        """Run one grok single-prompt turn and return its output and thread id.

        Args:
            prompt: The text to send; a fresh review is framed with the
                skill invocation, an existing session gets it raw.
            worktree: The review checkout; grok runs with it as cwd.
            thread_id: Existing session to resume, or None.

        Returns:
            The envelope's `text` (fenced if it arrived as bare verdict
            JSON) and the session id for later resumes.

        Raises:
            AdapterProcessError: If the binary is missing, exits nonzero,
                exceeds the timeout, emits a broken envelope, ends without
                stopReason end_turn (the exit-0 silent-cancel edge), or -
                on a fresh review - does not discover the skill.
        """
        argv = [self.grok_bin]
        if thread_id is None:
            self._preflight(worktree)
            # The sequencing instruction is load-bearing (probed live):
            # without it the schema-constrained model emits a placeholder
            # verdict as its first message instead of reviewing.
            prompt = (
                "/skeptical-reviewer HEAD\n\n"
                "Do the full review with your tools before answering; "
                "your final message is the complete verdict object - "
                f"never a placeholder or partial verdict.\n\n{prompt}"
            )
        else:
            argv.extend(["--resume", thread_id])
        # --always-approve is required headless: without it the first
        # non-git bash call is silently cancelled (probed live; grok
        # auto-approves only its built-in safe commands). It is bounded
        # by --tools - the edit tools do not exist in the session, and
        # bash-issued writes are caught by the integrity check (R7).
        argv.extend(
            [
                "--tools",
                ",".join(tool_policy("grok").allow),
                "--always-approve",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(load_schema()),
                "-p",
                prompt,
            ]
        )
        stdout, stderr = run_backend("grok", argv, worktree, self.timeout)
        text, session, cost = _parse_envelope(stdout, stderr)
        if cost:
            self._record_cost(cost)
        return fence_bare_verdict(text), session

    def _preflight(self, worktree: Path) -> None:
        """Assert grok discovers the skeptical-reviewer skill (3.5).

        Discovery means an exact, enabled entry in `grok inspect --json`'s
        skills array - a substring of the human-readable listing would
        also match config warnings about a skill that failed to load.

        Raises:
            AdapterProcessError: If `grok inspect --json` fails, emits
                unparseable output, or its skills array has no enabled
                skeptical-reviewer entry.
        """
        stdout, _ = run_backend(
            "grok", [self.grok_bin, "inspect", "--json"], worktree, self.timeout
        )
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AdapterProcessError(
                f"grok inspect emitted unparseable JSON: {stdout.strip()[:200]}"
            ) from exc
        skills = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(skills, list):
            # A missing or non-array skills member (JSON null, a scalar)
            # is failed discovery, never a raw TypeError.
            skills = []
        for entry in skills:
            if (
                isinstance(entry, dict)
                and entry.get("name") == "skeptical-reviewer"
                and entry.get("compatibilityStatus", "enabled") == "enabled"
            ):
                return
        raise AdapterProcessError(
            "grok does not discover an enabled skeptical-reviewer skill "
            f"in the review checkout ({worktree})"
        )


def _parse_envelope(stdout: str, stderr: str) -> tuple[str, str, dict[str, Any]]:
    """Extract text, session id, and cost data from the grok envelope.

    Raises:
        AdapterProcessError: On an unparseable or non-object envelope, a
            non-string text field, or a stopReason other than end_turn -
            the live signature of a run that died inside grok with exit 0
            and narration-only text (T3 sharp edge).
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterProcessError(
            f"grok emitted an unparseable envelope: {stdout.strip()[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise AdapterProcessError(
            f"grok envelope is not an object: {stdout.strip()[:200]}"
        )
    stop = data.get("stopReason", "")
    text = data.get("text", "")
    if stop != "end_turn":
        raise AdapterProcessError(
            "grok run ended without a final answer "
            f"(stopReason {stop or '(none)'}): "
            f"{str(text).strip()[:200]} {stderr.strip()}".strip()
        )
    structured = data.get("structuredOutput")
    if isinstance(structured, dict):
        # Under tool use `text` concatenates the model's message with
        # the structured output (probed live), so the parsed object is
        # the only reliable carrier of the schema-constrained verdict.
        text = json.dumps(structured)
    elif not isinstance(text, str):
        raise AdapterProcessError(
            f"grok envelope text is not a string: {stdout.strip()[:200]}"
        )
    session = data.get("sessionId", "")
    if not isinstance(session, str):
        # A non-string id can never be resumed (and would poison a later
        # resume argv), so it degrades exactly like a missing one: the
        # template method raises ThreadContinuityError (D1).
        session = ""
    return text, session, telemetry(data)
