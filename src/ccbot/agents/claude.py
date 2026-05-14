"""ClaudeAgent — adapter wrapping Claude Code's on-disk session layout.

This is the current production behavior of ccbot, lifted verbatim from
`SessionManager._encode_cwd`, `_build_session_file_path`, and
`list_sessions_for_directory` so consumers can route through the `Agent`
protocol with zero behavior change.

Subsequent T3 phases will absorb transcript parsing, hook install, and
spawn-arg construction into this adapter. For now it owns path-building and
session enumeration only.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import aiofiles

from .base import SessionSummary

logger = logging.getLogger(__name__)


class ClaudeAgent:
    """Adapter implementing the `Agent` protocol for Claude Code."""

    name = "claude"

    def __init__(self, projects_path: Path) -> None:
        self.projects_path = projects_path

    def encode_cwd(self, cwd: str) -> str:
        """Encode an absolute cwd into Claude's project directory name.

        Claude replaces every non-alnum-or-dash character with a dash, so
        ``/home/user_name/Code/project`` becomes
        ``-home-user-name-Code-project``.
        """
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)

    def build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        """Direct path to ``{projects_path}/{encoded_cwd}/{session_id}.jsonl``.

        Returns ``None`` when either input is empty. The caller must still
        check ``Path.exists()`` — Claude may have moved the file or the cwd
        encoding may have changed across versions.
        """
        if not session_id or not cwd:
            return None
        return self.projects_path / self.encode_cwd(cwd) / f"{session_id}.jsonl"

    async def list_sessions_for_directory(self, cwd: str) -> list[SessionSummary]:
        """Enumerate Claude sessions under ``cwd``, newest first, capped at 10.

        Skips the ``sessions-index`` artefact and any zero-message JSONL files.
        Summary extraction mirrors the previous in-line implementation.
        """
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
        """Single-pass scan: count messages, extract summary or last user msg."""
        # Local import to avoid cycle (transcript_parser imports config which
        # will eventually import this module's package).
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
