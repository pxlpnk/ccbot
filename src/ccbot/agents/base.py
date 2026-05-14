"""Agent protocol and shared dataclasses for runtime adapters.

The `Agent` protocol is the contract every runtime (Claude Code, Codex) must
implement. This is the minimal slice introduced in T3a — it covers the
session-file discovery surface only. Subsequent phases will extend the protocol
with transcript parsing, hook installation, terminal-state detection, and
spawn-argument construction (see `doc/ontology.md` § "Adapter API surface").

The intentional small surface here is so we can route one consumer through the
agent (`SessionManager.list_sessions_for_directory`) and prove the pattern
without breaking anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SessionSummary:
    """A persisted thread on disk, summarised for a picker UI.

    Runtime-neutral: both Claude and Codex map their per-runtime row/file shape
    into this struct. The fields are exactly what the directory-browser session
    picker needs — no more.

    Note: `file_path` is the absolute path to the rollout JSONL (Claude) or
    rollout file (Codex). `message_count` may be approximate for very large
    files; consumers must not rely on it for correctness.
    """

    session_id: str
    summary: str
    message_count: int
    file_path: str


@runtime_checkable
class Agent(Protocol):
    """Runtime adapter contract.

    The protocol is intentionally narrow in T3a. Methods added later (transcript
    parsing, hook install, spawn args, terminal state) will extend it in step
    with the consumers they unblock.
    """

    name: str
    """Short identifier (e.g. ``"claude"``, ``"codex"``). Stable across versions."""

    projects_path: Path
    """Root directory the adapter reads session files from.

    For Claude: ``~/.claude/projects/`` (or override via env). For Codex this is
    informational only — the catalog lives in SQLite — but the attribute exists
    so legacy code paths keep compiling during the migration.
    """

    def encode_cwd(self, cwd: str) -> str:
        """Convert an absolute cwd into the adapter's on-disk directory name.

        For Claude this replaces non-alnum-or-dash characters with dashes.
        For Codex it is a no-op placeholder (Codex catalogs threads in SQLite,
        keyed by cwd directly), but the method must exist so that callers can
        continue to round-trip paths through the adapter without branching.
        """
        ...

    def build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        """Return the expected rollout/JSONL path for a session, if computable.

        Returns ``None`` when the inputs are insufficient. Callers must still
        verify the path exists before reading.
        """
        ...

    async def list_sessions_for_directory(self, cwd: str) -> list[SessionSummary]:
        """Enumerate persisted sessions rooted in ``cwd``.

        Ordering: most-recently-updated first. Empty list if none exist.
        Implementations MUST NOT auto-select among ambiguous candidates —
        callers (the picker UI) are responsible for surfacing ambiguity.
        """
        ...
