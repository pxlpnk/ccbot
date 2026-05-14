"""Tests for the unified `ccbot hook` autodetect routing."""

from __future__ import annotations

from ccbot.hook import _detect_agent_kind


def test_detects_codex_via_conversation_id() -> None:
    assert (
        _detect_agent_kind({"conversation_id": "x", "hook_event_name": "SessionStart"})
        == "codex"
    )


def test_detects_codex_via_pretooluse_thread_id() -> None:
    assert (
        _detect_agent_kind({"thread_id": "x", "hook_event_name": "PreToolUse"})
        == "codex"
    )


def test_detects_claude_via_session_id_source() -> None:
    assert (
        _detect_agent_kind(
            {
                "session_id": "x",
                "source": "startup",
                "hook_event_name": "SessionStart",
                "cwd": "/tmp/x",
            }
        )
        == "claude"
    )


def test_detects_claude_via_session_event_name() -> None:
    assert (
        _detect_agent_kind({"session_id": "x", "hook_event_name": "SessionStart"})
        == "claude"
    )


def test_returns_none_for_unrecognised_payload() -> None:
    assert _detect_agent_kind({"random": "thing"}) is None
    assert _detect_agent_kind({}) is None
