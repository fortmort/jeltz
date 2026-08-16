"""Codex adapter: drive `codex exec` as the reviewer backend (T8).

D2 is settled on the exec path: the installed skeptical-reviewer skill is
invoked by name, `--output-schema` pins the verdict shape natively, and
`codex exec resume <thread>` continues the reviewer thread for re-reviews
(D1). The JSONL event stream (verified live against codex-cli 0.147.0)
supplies the thread id via `thread.started` and the reviewer's output via
the final `agent_message` item. Schema-constrained output arrives as bare
JSON, so it is normalized into the fenced block form the T4 parser expects.
Transport failures - a dead, missing, or hung binary - raise
AdapterProcessError, never a verdict and never a hang.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from review.adapter import AdapterProcessError, ReviewerAdapter
from review.verdict import strict_schema

DEFAULT_TIMEOUT = 600.0


class CodexAdapter(ReviewerAdapter):
    """Reviewer backend running `codex exec` against the installed skill."""

    def __init__(
        self, codex_bin: str = "codex", timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        """Configure the backend invocation.

        Args:
            codex_bin: The codex executable to run.
            timeout: Seconds before a hung backend is killed.
        """
        self.codex_bin = codex_bin
        self.timeout = timeout

    def _send(
        self, prompt: str, worktree: Path, thread_id: str | None
    ) -> tuple[str, str]:
        """Run one codex exec turn and return its output and thread id.

        Args:
            prompt: The text to send; a fresh review is framed with the
                installed-skill invocation, an existing thread gets it raw.
            worktree: The review checkout; codex runs with it as cwd.
            thread_id: Existing reviewer thread to resume, or None.

        Returns:
            The final agent message (fenced if it arrived as bare verdict
            JSON) and the thread id from the event stream.

        Raises:
            AdapterProcessError: If the binary is missing, exits nonzero,
                or exceeds the timeout.
        """
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="jeltz-verdict-schema-"
        ) as schema_file:
            json.dump(strict_schema(), schema_file)
            schema_file.flush()
            if thread_id is None:
                argv = [
                    self.codex_bin,
                    "exec",
                    "--json",
                    "--sandbox",
                    "read-only",
                    "--output-schema",
                    schema_file.name,
                    f"$skeptical-reviewer HEAD\n\n{prompt}",
                ]
            else:
                # `codex exec resume` has no --sandbox flag (verified live
                # on 0.147.0); the config override is the supported spelling.
                argv = [
                    self.codex_bin,
                    "exec",
                    "resume",
                    thread_id,
                    "--json",
                    "-c",
                    'sandbox_mode="read-only"',
                    "--output-schema",
                    schema_file.name,
                    prompt,
                ]
            stdout = self._run(argv, worktree)
        return _parse_stream(stdout)

    def _run(self, argv: list[str], worktree: Path) -> str:
        """Execute codex, mapping every transport failure to a typed error."""
        try:
            proc = subprocess.run(
                argv,
                cwd=worktree,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except OSError as exc:
            # FileNotFoundError, PermissionError, and every other spawn
            # failure: the binary never ran, so this is transport, not
            # verdict (T7 typed-error contract).
            raise AdapterProcessError(
                f"codex could not be spawned ({self.codex_bin}): {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterProcessError(
                f"codex timed out after {self.timeout}s and was killed"
            ) from exc
        if proc.returncode != 0:
            raise AdapterProcessError(
                f"codex exited {proc.returncode}: {proc.stderr.strip()}"
            )
        return proc.stdout


def _parse_stream(stdout: str) -> tuple[str, str]:
    """Extract the final agent message and thread id from JSONL events.

    Non-JSON lines are skipped; a stream with no agent message yields an
    empty raw string (the template method's R1 assertion fails it) and a
    stream with no thread.started yields an empty thread id (the D1
    continuity check fails it).
    """
    thread_id = ""
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id", "")
        elif event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                texts.append(item.get("text", ""))
    raw = texts[-1] if texts else ""
    return _fence_bare_verdict(raw), thread_id


def _fence_bare_verdict(raw: str) -> str:
    """Wrap a bare schema-constrained verdict in the fence T4 parses.

    Output already carrying prose or a fence is returned untouched; only a
    message that is itself a verdict-shaped JSON object gets wrapped.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(data, dict) and "schema_version" in data:
        return f"```json\n{raw}\n```\n"
    return raw
