"""CodexAgent — adapter for the OpenAI Codex CLI (`codex-cli`).

Codex differs from Claude in three ways that matter to ccbot:

* **Catalog** — threads are listed in ``~/.codex/state_5.sqlite`` (table
  ``threads``), not on the filesystem. Same-cwd ambiguity is the norm.
* **Rollout layout** — JSONL under ``~/.codex/sessions/YYYY/MM/DD/`` with a
  different schema (``session_meta`` / ``response_item`` / ``event_msg`` /
  ``turn_context``).
* **Hook config** — TOML, not JSON. Lives at ``~/.codex/config.toml`` under the
  ``[[hooks.session_start]]`` array.

Resume semantics, verified against codex-cli 0.107.0 in `doc/codex-rollout.md`:
``codex resume <thread_id>`` re-uses the same thread id and **appends to the
original rollout file** — process ≠ thread, see `doc/ontology.md`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiofiles
import tomlkit
from tomlkit.exceptions import TOMLKitError as _TOMLKitError

from .base import EventKind, HookInstallResult, NormalizedEvent, SessionSummary
from .claude import _find_ccbot_path

logger = logging.getLogger(__name__)


_DEFAULT_HOME = Path.home() / ".codex"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HOOK_COMMAND_SUFFIX = "ccbot hook"


class CodexAgent:
    """Adapter implementing the `Agent` protocol for Codex CLI."""

    name = "codex"

    def __init__(
        self,
        codex_home: Path | None = None,
        *,
        default_command: str = "codex",
        hook_settings_path: Path | None = None,
        catalog_path: Path | None = None,
    ) -> None:
        self.codex_home = codex_home if codex_home is not None else _DEFAULT_HOME
        self.projects_path = self.codex_home / "sessions"
        self.default_command = default_command
        self.hook_settings_path = (
            hook_settings_path
            if hook_settings_path is not None
            else self.codex_home / "config.toml"
        )
        # state_5.sqlite is the catalog. Allow override for tests.
        self._catalog_path = (
            catalog_path
            if catalog_path is not None
            else self.codex_home / "state_5.sqlite"
        )

    # --- Session-file discovery -------------------------------------------------

    def encode_cwd(self, cwd: str) -> str:
        """No-op for Codex (catalog is keyed by raw cwd in SQLite)."""
        return cwd

    def build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        """Look up the rollout path for ``session_id`` via the catalog.

        ``cwd`` is unused (kept for protocol compatibility). Returns None if
        the catalog is missing or the thread row has no ``rollout_path``.
        """
        if not session_id:
            return None
        if not self._catalog_path.exists():
            return None
        try:
            with sqlite3.connect(
                f"file:{self._catalog_path}?mode=ro", uri=True
            ) as conn:
                row = conn.execute(
                    "SELECT rollout_path FROM threads WHERE id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return Path(row[0])

    async def list_sessions_for_directory(self, cwd: str) -> list[SessionSummary]:
        """Read up to 10 non-archived threads whose cwd matches.

        Ordered by ``updated_at`` descending. Same-cwd ambiguity is preserved:
        all candidates are returned; the picker UI must surface them rather
        than auto-selecting.
        """
        if not self._catalog_path.exists():
            return []
        try:
            with sqlite3.connect(
                f"file:{self._catalog_path}?mode=ro", uri=True
            ) as conn:
                rows = conn.execute(
                    """
                    SELECT id, rollout_path, title, first_user_message
                    FROM threads
                    WHERE cwd = ? AND archived = 0
                    ORDER BY updated_at DESC
                    LIMIT 10
                    """,
                    (cwd,),
                ).fetchall()
        except sqlite3.Error as e:
            logger.warning("Failed to read codex thread catalog: %s", e)
            return []

        results: list[SessionSummary] = []
        for thread_id, rollout_path, title, first_msg in rows:
            summary_text = (title or first_msg or "Untitled").strip()
            if len(summary_text) > 80:
                summary_text = summary_text[:77] + "..."
            message_count = await self._count_rollout_messages(Path(rollout_path))
            if message_count == 0:
                continue
            results.append(
                SessionSummary(
                    session_id=thread_id,
                    summary=summary_text,
                    message_count=message_count,
                    file_path=rollout_path,
                )
            )
        return results

    @staticmethod
    async def _count_rollout_messages(path: Path) -> int:
        if not path.exists():
            return 0
        count = 0
        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                async for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Count user/assistant messages, not lifecycle envelopes.
                    if (
                        data.get("type") == "response_item"
                        and data.get("payload", {}).get("type") == "message"
                    ):
                        count += 1
        except OSError:
            return 0
        return count

    # --- Transcript parsing -----------------------------------------------------

    def parse_rollout_line(self, line: str) -> NormalizedEvent | None:
        """Map one Codex rollout JSONL line into a `NormalizedEvent`.

        Skips ``event_msg`` lines whose payload type duplicates a
        ``response_item`` (``user_message`` / ``agent_message``) to avoid
        emitting every message twice. Keeps ``task_started`` /
        ``task_complete`` / ``token_count`` as lifecycle events.
        """
        line = line.strip()
        if not line:
            return None
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        line_type = data.get("type", "")
        payload = data.get("payload") or {}
        timestamp = _parse_codex_timestamp(data.get("timestamp"))

        if line_type == "session_meta":
            return NormalizedEvent(
                kind=EventKind.LIFECYCLE,
                text="session_start",
                timestamp=timestamp,
                raw=data,
            )

        if line_type == "turn_context":
            return NormalizedEvent(
                kind=EventKind.LIFECYCLE,
                text="turn_context",
                timestamp=timestamp,
                raw=data,
            )

        if line_type == "event_msg":
            ptype = payload.get("type", "")
            # user_message / agent_message duplicate response_item — skip.
            if ptype in {"user_message", "agent_message"}:
                return None
            if ptype in {"task_started", "task_complete", "token_count"}:
                return NormalizedEvent(
                    kind=EventKind.LIFECYCLE,
                    text=ptype,
                    timestamp=timestamp,
                    raw=data,
                )
            return None

        if line_type != "response_item":
            return None

        ptype = payload.get("type", "")
        if ptype == "message":
            role = payload.get("role", "")
            text = _codex_message_text(payload.get("content"))
            if role == "user":
                # Codex injects AGENTS.md / permissions / environment context
                # as `user`-role messages before any real prompt. These leak
                # CWD, timezone, skill paths, and internal instructions if
                # surfaced to Telegram — skip them.
                if _is_codex_injected_context(text):
                    return None
                return NormalizedEvent(
                    kind=EventKind.USER_MESSAGE,
                    text=text,
                    timestamp=timestamp,
                    raw=data,
                )
            if role == "assistant":
                return NormalizedEvent(
                    kind=EventKind.ASSISTANT_MESSAGE,
                    text=text,
                    timestamp=timestamp,
                    raw=data,
                )
            # system / developer messages are not surfaced as conversation.
            return None

        if ptype == "reasoning":
            text = _codex_reasoning_text(payload)
            return NormalizedEvent(
                kind=EventKind.THINKING,
                text=text,
                timestamp=timestamp,
                raw=data,
            )

        if ptype == "function_call":
            return NormalizedEvent(
                kind=EventKind.TOOL_CALL,
                text=_codex_function_call_text(payload),
                tool_name=payload.get("name"),
                tool_call_id=payload.get("call_id"),
                timestamp=timestamp,
                raw=data,
            )

        if ptype == "function_call_output":
            return NormalizedEvent(
                kind=EventKind.TOOL_RESULT,
                text=_codex_function_output_text(payload.get("output", "")),
                tool_call_id=payload.get("call_id"),
                timestamp=timestamp,
                raw=data,
            )

        return None

    async def iter_rollout_events(
        self, path: Path, *, start_byte: int = 0
    ) -> AsyncIterator[NormalizedEvent]:
        try:
            async with aiofiles.open(path, "rb") as f:
                if start_byte:
                    await f.seek(start_byte)
                async for raw in f:
                    try:
                        decoded = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    event = self.parse_rollout_line(decoded)
                    if event is not None:
                        yield event
        except OSError:
            return

    # --- Spawn-arg construction -------------------------------------------------

    def spawn_argv(
        self,
        *,
        resume_thread_id: str | None = None,
        extra: list[str] | None = None,
    ) -> list[str]:
        """Construct argv for spawning Codex in a tmux window.

        Fresh thread:   ``codex``
        Resume thread:  ``codex resume <thread_id>``
        Caller is responsible for ``cd <cwd>`` before exec.
        """
        if resume_thread_id:
            if not _UUID_RE.match(resume_thread_id):
                # Defensive — the catalog returns UUIDs but the protocol allows
                # arbitrary strings. Refuse to pass an obviously wrong value.
                raise ValueError(
                    f"resume_thread_id is not a UUID: {resume_thread_id!r}"
                )
            argv = [self.default_command, "resume", resume_thread_id]
        else:
            argv = [self.default_command]
        if extra:
            argv += list(extra)
        return argv

    # --- Hook installation ------------------------------------------------------

    def is_hook_installed(self) -> bool:
        if not self.hook_settings_path.exists():
            return False
        try:
            doc = tomlkit.parse(self.hook_settings_path.read_text())
        except (_TOMLKitError, OSError):
            return False
        hooks = doc.get("hooks", {})
        if not isinstance(hooks, dict):
            return False
        session_start = hooks.get("session_start", [])
        if not isinstance(session_start, list):
            return False
        for entry in session_start:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks", [])
            if not isinstance(inner, list):
                continue
            for h in inner:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command", "")
                if isinstance(cmd, str) and (
                    cmd == _HOOK_COMMAND_SUFFIX
                    or cmd.endswith("/" + _HOOK_COMMAND_SUFFIX)
                ):
                    return True
        return False

    def install_hook(self) -> HookInstallResult:
        """Idempotently add ``ccbot hook`` to ``hooks.session_start`` in TOML."""
        settings_path = self.hook_settings_path
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        if settings_path.exists():
            try:
                doc = tomlkit.parse(settings_path.read_text())
            except (_TOMLKitError, OSError) as e:
                return HookInstallResult(
                    settings_path=settings_path,
                    installed=False,
                    message=f"Error reading {settings_path}: {e}",
                )
        else:
            doc = tomlkit.document()

        if self.is_hook_installed():
            return HookInstallResult(
                settings_path=settings_path,
                installed=False,
                message=f"Hook already installed in {settings_path}",
            )

        hooks = doc.get("hooks")
        if not isinstance(hooks, dict):
            hooks = tomlkit.table()
            doc["hooks"] = hooks
        session_start = hooks.get("session_start")
        if not isinstance(session_start, list):
            session_start = tomlkit.aot()  # array of tables
            hooks["session_start"] = session_start

        hook_cmd = f"{_find_ccbot_path()} hook"
        entry = tomlkit.table()
        entry["matcher"] = "*"
        inner_hook = tomlkit.inline_table()
        inner_hook["type"] = "command"
        inner_hook["command"] = hook_cmd
        inner_hook["timeout_sec"] = 5
        hooks_arr = tomlkit.array()
        hooks_arr.append(inner_hook)
        entry["hooks"] = hooks_arr
        session_start.append(entry)

        try:
            settings_path.write_text(tomlkit.dumps(doc))
        except OSError as e:
            return HookInstallResult(
                settings_path=settings_path,
                installed=False,
                message=f"Error writing {settings_path}: {e}",
            )
        return HookInstallResult(
            settings_path=settings_path,
            installed=True,
            message=f"Hook installed successfully in {settings_path}",
        )

    @staticmethod
    def detect_hook_payload(payload: dict[str, Any]) -> bool:
        """Heuristic: Codex hook payloads carry a ``conversation_id`` field.

        Codex's hook stdin shape (`codex-rs/hooks/schema/`) uses
        ``hook_event_name`` values like ``SessionStart``, ``PreToolUse``,
        ``PostToolUse``, etc., but the distinguishing field is
        ``conversation_id`` (Claude uses ``session_id``).
        """
        if not isinstance(payload, dict):
            return False
        if "conversation_id" in payload:
            return True
        # Fallback: presence of ``thread_id`` alongside Codex-shaped event names.
        event = payload.get("hook_event_name", "")
        if isinstance(event, str) and event in {
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PermissionRequest",
            "Stop",
        }:
            if "thread_id" in payload and "session_id" not in payload:
                return True
        return False


# ---------- Payload extraction helpers --------------------------------------


def _parse_codex_timestamp(ts: Any) -> float | None:
    """Parse an ISO-8601 string like ``2026-05-14T07:51:53.249Z`` into epoch seconds."""
    if not isinstance(ts, str):
        return None
    # Python's fromisoformat doesn't accept the trailing ``Z`` until 3.11; ccbot
    # requires 3.12+. Use it directly.
    iso = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    try:
        from datetime import datetime

        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, OverflowError):
        return None


_CODEX_INJECTED_USER_MARKERS = (
    "<INSTRUCTIONS>",
    "<environment_context>",
    "<permissions",  # `<permissions instructions>` and variants
    "# AGENTS.md",
    "<user_instructions>",
)


def _is_codex_injected_context(text: str) -> bool:
    """Heuristic: does this `user`-role message look like Codex's auto-injected context?

    Codex 0.107.0 prepends one or more `user` messages to each session
    containing wrappers like ``<INSTRUCTIONS>...</INSTRUCTIONS>``,
    ``<environment_context>...</environment_context>``, ``# AGENTS.md ...``,
    and ``<permissions instructions>...</permissions instructions>``. These
    aren't user prompts — they're internal context. Surfacing them to
    Telegram would leak the user's cwd, timezone, skill paths, and any
    AGENTS.md content.

    We match on the *start* of the message (after stripping whitespace) so a
    real user prompt that happens to mention these tags downstream still
    flows through. Also catch the case where the first non-blank line is one
    of the markers — Codex sometimes adds a blank line first.
    """
    if not text:
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(marker) for marker in _CODEX_INJECTED_USER_MARKERS)


def _codex_message_text(content: Any) -> str:
    """Flatten Codex message ``content`` blocks (input_text / output_text) into text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype in {"input_text", "output_text", "summary_text", "text"}:
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _codex_reasoning_text(payload: dict[str, Any]) -> str:
    """Best-effort reasoning text. Most rows have only ``encrypted_content``."""
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts = []
        for item in summary:
            if isinstance(item, dict):
                t = item.get("text") or item.get("summary")
                if isinstance(t, str) and t:
                    parts.append(t)
        if parts:
            return "\n".join(parts)
    content = payload.get("content")
    if isinstance(content, str) and content:
        return content
    return "(reasoning, encrypted)"


def _codex_function_call_text(payload: dict[str, Any]) -> str:
    name = payload.get("name", "tool")
    args = payload.get("arguments", "")
    if isinstance(args, str):
        # Codex serialises the args dict as a JSON string. Don't try to
        # pretty-print here; downstream formatters can decide.
        return f"{name}({args})"
    return name


def _codex_function_output_text(output: Any) -> str:
    """Extract the body after the Codex ``Output:`` separator.

    The shape is::

        Chunk ID: 9f0126
        Wall time: 0.0510 seconds
        Process exited with code 0
        Output:
        <body>
    """
    if not isinstance(output, str):
        return ""
    marker = "Output:"
    idx = output.find(marker)
    if idx == -1:
        return output.strip()
    return output[idx + len(marker) :].strip(os.linesep + "\r\n ")
