"""Agent protocol, normalized events, and shared dataclasses for runtime adapters.

The `Agent` protocol is the contract every runtime (Claude Code, Codex) must
implement. Methods cover: session-file discovery, transcript parsing into a
runtime-neutral `NormalizedEvent` stream, spawn-arg construction, and hook
installation.

See `doc/ontology.md` for the entities the API operates on (binding, window,
live process, thread, rollout) and the non-collapsing invariants every adapter
must respect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SessionSummary:
    """A persisted thread on disk, summarised for a picker UI.

    Runtime-neutral: both Claude and Codex map their per-runtime row/file shape
    into this struct.
    """

    session_id: str
    summary: str
    message_count: int
    file_path: str


class EventKind(str, Enum):
    """Coarse classification of a normalized rollout event.

    Both runtimes map their native message types into this set so downstream
    formatters can render without branching on runtime.
    """

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    LIFECYCLE = "lifecycle"
    SUMMARY = "summary"


@dataclass
class NormalizedEvent:
    """Runtime-neutral view of a single rollout entry.

    The fields are deliberately denormalised — easier for the formatter than a
    discriminated union. Empty/None fields are common: a USER_MESSAGE has no
    `tool_name`; a TOOL_RESULT has `tool_call_id` referencing the prior
    TOOL_CALL but no `text` until the impl decides to render the output.
    """

    kind: EventKind
    text: str = ""
    tool_name: str | None = None
    tool_call_id: str | None = None
    timestamp: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    """Original parsed line, kept for downstream consumers that need richer
    detail (e.g. expandable-quote formatters that want the raw command output).
    """


@dataclass(frozen=True)
class HookInstallResult:
    """Outcome of `Agent.install_hook` for telemetry / CLI output."""

    settings_path: Path
    installed: bool
    """True when the hook was newly written. False when it was already present."""
    message: str = ""


@runtime_checkable
class Agent(Protocol):
    """Runtime adapter contract.

    Implementations live in `agents/<name>.py`. The protocol is structural —
    consumers should depend on this type, not on the concrete classes.
    """

    name: str
    """Short identifier (e.g. ``"claude"``, ``"codex"``). Stable across versions."""

    projects_path: Path
    """Root directory the adapter reads session files from (informational for
    runtimes whose catalog is in SQLite)."""

    default_command: str
    """The CLI binary name used in tmux windows (overridable via env)."""

    hook_settings_path: Path
    """Where ``install_hook`` writes its registration."""

    # --- Session-file discovery -------------------------------------------------

    def encode_cwd(self, cwd: str) -> str:
        """Convert an absolute cwd into the adapter's on-disk directory name."""
        ...

    def build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        """Expected rollout/JSONL path for a session, or None when incomputable."""
        ...

    async def list_sessions_for_directory(self, cwd: str) -> list[SessionSummary]:
        """Enumerate persisted sessions rooted in ``cwd``, newest first."""
        ...

    # --- Transcript parsing -----------------------------------------------------

    def parse_rollout_line(self, line: str) -> NormalizedEvent | None:
        """Map one raw JSONL line into a runtime-neutral event.

        Returns None for lines that should be skipped (blank, malformed, or
        duplicate live-event envelopes).
        """
        ...

    def iter_rollout_events(
        self, path: Path, *, start_byte: int = 0
    ) -> AsyncIterator[NormalizedEvent]:
        """Yield events from ``path`` starting at ``start_byte``."""
        ...

    # --- Spawn-arg construction -------------------------------------------------

    def spawn_argv(
        self, *, resume_thread_id: str | None = None, extra: list[str] | None = None
    ) -> list[str]:
        """Argv tokens for launching the CLI in a tmux window.

        ``resume_thread_id`` switches to the runtime's resume invocation.
        ``extra`` is appended verbatim — used by callers passing flags like
        ``--dangerously-skip-permissions`` / ``--yolo``.
        """
        ...

    # --- Hook installation ------------------------------------------------------

    def is_hook_installed(self) -> bool:
        """True when ``ccbot hook`` is already wired into the runtime's settings."""
        ...

    def install_hook(self) -> HookInstallResult:
        """Idempotently add the ``ccbot hook`` entry to the runtime's settings."""
        ...

    @staticmethod
    def detect_hook_payload(payload: dict[str, Any]) -> bool:
        """True if the payload shape matches this runtime's hook stdin contract."""
        ...
