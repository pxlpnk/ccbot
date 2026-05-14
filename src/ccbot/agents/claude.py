"""ClaudeAgent — adapter wrapping Claude Code's on-disk session layout.

Implements the full `Agent` protocol. Logic is lifted from the existing
top-level modules (`SessionManager`, `hook.py`, `TranscriptParser`) so the
existing pipelines keep their current behavior; this adapter is the
runtime-neutral entry point that consumers will migrate to.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiofiles

from .base import EventKind, HookInstallResult, NormalizedEvent, SessionSummary

logger = logging.getLogger(__name__)

_HOOK_COMMAND_SUFFIX = "ccbot hook"


def _find_ccbot_path() -> str:
    """Mirror of `hook.py._find_ccbot_path` for use inside the adapter."""
    path = shutil.which("ccbot")
    if path:
        return path
    python_dir = Path(sys.executable).parent
    candidate = python_dir / "ccbot"
    if candidate.exists():
        return str(candidate)
    return "ccbot"


class ClaudeAgent:
    """Adapter implementing the `Agent` protocol for Claude Code."""

    name = "claude"

    def __init__(
        self,
        projects_path: Path,
        *,
        default_command: str = "claude",
        hook_settings_path: Path | None = None,
    ) -> None:
        self.projects_path = projects_path
        self.default_command = default_command
        self.hook_settings_path = (
            hook_settings_path
            if hook_settings_path is not None
            else Path.home() / ".claude" / "settings.json"
        )

    # --- Session-file discovery -------------------------------------------------

    def encode_cwd(self, cwd: str) -> str:
        """Replace non-alnum-or-dash characters with dashes (Claude convention)."""
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)

    def build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        if not session_id or not cwd:
            return None
        return self.projects_path / self.encode_cwd(cwd) / f"{session_id}.jsonl"

    async def list_sessions_for_directory(self, cwd: str) -> list[SessionSummary]:
        encoded_cwd = self.encode_cwd(cwd)
        project_dir = self.projects_path / encoded_cwd
        if not project_dir.is_dir():
            return []

        jsonl_files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        sessions: list[SessionSummary] = []
        for f in jsonl_files:
            if f.stem == "sessions-index":
                continue
            if len(sessions) >= 10:
                break
            summary = await self._summarise_session_file(f)
            if summary is not None and summary.message_count > 0:
                sessions.append(summary)
        return sessions

    async def _summarise_session_file(self, file_path: Path) -> SessionSummary | None:
        from ..transcript_parser import TranscriptParser

        summary = ""
        last_user_msg = ""
        message_count = 0
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "summary":
                        s = data.get("summary", "")
                        if s:
                            summary = s
                    elif TranscriptParser.is_user_message(data):
                        parsed = TranscriptParser.parse_message(data)
                        if parsed and parsed.text.strip():
                            last_user_msg = parsed.text.strip()
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return SessionSummary(
            session_id=file_path.stem,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    # --- Transcript parsing -----------------------------------------------------

    def parse_rollout_line(self, line: str) -> NormalizedEvent | None:
        """Map one Claude JSONL line into a `NormalizedEvent`.

        Minimal coverage suitable for the new event-based pipeline. The
        existing `TranscriptParser` continues to power the legacy notification
        path until that consumer is migrated.
        """
        line = line.strip()
        if not line:
            return None
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        entry_type = data.get("type", "")
        message = data.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None

        if entry_type == "summary":
            return NormalizedEvent(
                kind=EventKind.SUMMARY,
                text=data.get("summary", ""),
                raw=data,
            )
        if entry_type == "user":
            text = _claude_blocks_to_text(content)
            # Distinguish tool_result (which Claude packs inside user messages)
            # from a real user prompt via the `tool_use_id` field.
            if _claude_has_tool_result(content):
                tool_use_id = _claude_tool_result_id(content)
                return NormalizedEvent(
                    kind=EventKind.TOOL_RESULT,
                    text=text,
                    tool_call_id=tool_use_id,
                    raw=data,
                )
            return NormalizedEvent(kind=EventKind.USER_MESSAGE, text=text, raw=data)
        if entry_type == "assistant":
            tool_use = _claude_first_tool_use(content)
            if tool_use is not None:
                return NormalizedEvent(
                    kind=EventKind.TOOL_CALL,
                    text=_claude_blocks_to_text(content),
                    tool_name=tool_use.get("name"),
                    tool_call_id=tool_use.get("id"),
                    raw=data,
                )
            if _claude_has_thinking(content):
                return NormalizedEvent(
                    kind=EventKind.THINKING,
                    text=_claude_blocks_to_text(content, kinds={"thinking"}),
                    raw=data,
                )
            return NormalizedEvent(
                kind=EventKind.ASSISTANT_MESSAGE,
                text=_claude_blocks_to_text(content),
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
        argv = [self.default_command]
        if resume_thread_id:
            argv += ["--resume", resume_thread_id]
        if extra:
            argv += list(extra)
        return argv

    # --- Hook installation ------------------------------------------------------

    def is_hook_installed(self) -> bool:
        if not self.hook_settings_path.exists():
            return False
        try:
            settings = json.loads(self.hook_settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        hooks = settings.get("hooks", {})
        session_start = hooks.get("SessionStart", [])
        for entry in session_start:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks", []):
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command", "")
                if cmd == _HOOK_COMMAND_SUFFIX or cmd.endswith(
                    "/" + _HOOK_COMMAND_SUFFIX
                ):
                    return True
        return False

    def install_hook(self) -> HookInstallResult:
        settings_path = self.hook_settings_path
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        settings: dict[str, Any] = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                return HookInstallResult(
                    settings_path=settings_path,
                    installed=False,
                    message=f"Error reading {settings_path}: {e}",
                )

        if self.is_hook_installed():
            return HookInstallResult(
                settings_path=settings_path,
                installed=False,
                message=f"Hook already installed in {settings_path}",
            )

        hook_cmd = f"{_find_ccbot_path()} hook"
        hook_config = {"type": "command", "command": hook_cmd, "timeout": 5}
        settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(
            {"hooks": [hook_config]}
        )

        try:
            settings_path.write_text(
                json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
            )
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
        """Heuristic: Claude SessionStart payloads carry `hook_event_name`.

        Also accepts payloads with the Claude-specific `source` + `session_id`
        pair so misrouted variants still classify correctly.
        """
        if not isinstance(payload, dict):
            return False
        event = payload.get("hook_event_name", "")
        if isinstance(event, str) and event.startswith("Session"):
            return True
        if "source" in payload and "session_id" in payload:
            return True
        return False


def _claude_blocks_to_text(content: Any, *, kinds: set[str] | None = None) -> str:
    """Flatten Claude `message.content` (string or list of blocks) into text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if kinds is not None and btype not in kinds:
            continue
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            parts.append(block.get("thinking", "") or block.get("text", ""))
        elif btype == "tool_use":
            parts.append(f"{block.get('name', 'tool')}({block.get('input', '')})")
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                parts.append(_claude_blocks_to_text(inner))
    return "\n".join(p for p in parts if p)


def _claude_first_tool_use(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block
    return None


def _claude_has_tool_result(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _claude_tool_result_id(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tid = block.get("tool_use_id")
            return tid if isinstance(tid, str) else None
    return None


def _claude_has_thinking(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "thinking" for b in content)
