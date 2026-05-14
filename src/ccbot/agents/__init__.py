"""Runtime adapters — pluggable per-CLI logic for Claude Code / Codex / ...

Each runtime (`claude`, `codex`, ...) is represented by an `Agent` implementation
that owns the bits of behavior that differ between CLIs: where session files
live, how to find one on disk, how to enumerate them for a working directory,
and (in later phases) transcript parsing, hook installation, and TUI state
detection.

See `doc/ontology.md` for the entities the adapter API operates on (binding,
window, live process, thread, rollout) and the non-collapsing invariants
every adapter must respect.
"""

from .base import Agent, SessionSummary
from .claude import ClaudeAgent

__all__ = ["Agent", "ClaudeAgent", "SessionSummary"]
