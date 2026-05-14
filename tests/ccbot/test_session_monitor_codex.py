"""Tests for the Codex side of `SessionMonitor` (T9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccbot.agents.base import EventKind, NormalizedEvent
from ccbot.session_monitor import (
    NewMessage,
    SessionMonitor,
    _codex_event_to_new_message,
)


def test_lifecycle_events_drop() -> None:
    ev = NormalizedEvent(kind=EventKind.LIFECYCLE, text="task_started")
    assert _codex_event_to_new_message("t", ev) is None


def test_summary_events_drop() -> None:
    ev = NormalizedEvent(kind=EventKind.SUMMARY, text="summary")
    assert _codex_event_to_new_message("t", ev) is None


def test_assistant_message_round_trips() -> None:
    ev = NormalizedEvent(kind=EventKind.ASSISTANT_MESSAGE, text="hello")
    msg = _codex_event_to_new_message("tid", ev)
    assert msg is not None
    assert msg.session_id == "tid"
    assert msg.role == "assistant"
    assert msg.content_type == "text"
    assert msg.text == "hello"
    assert msg.is_complete


def test_thinking_maps_to_thinking_content_type() -> None:
    ev = NormalizedEvent(kind=EventKind.THINKING, text="hmm")
    msg = _codex_event_to_new_message("tid", ev)
    assert msg is not None
    assert msg.content_type == "thinking"
    assert msg.role == "assistant"


def test_tool_call_carries_id_and_name() -> None:
    ev = NormalizedEvent(
        kind=EventKind.TOOL_CALL,
        text='exec_command({"cmd":"ls"})',
        tool_name="exec_command",
        tool_call_id="c1",
    )
    msg = _codex_event_to_new_message("tid", ev)
    assert msg is not None
    assert msg.content_type == "tool_use"
    assert msg.tool_name == "exec_command"
    assert msg.tool_use_id == "c1"


def test_tool_result_carries_id() -> None:
    ev = NormalizedEvent(
        kind=EventKind.TOOL_RESULT, text="14 file.txt", tool_call_id="c1"
    )
    msg = _codex_event_to_new_message("tid", ev)
    assert msg is not None
    assert msg.content_type == "tool_result"
    assert msg.role == "user"
    assert msg.tool_use_id == "c1"


def test_user_message_suppressed_when_show_user_messages_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot import session_monitor as mod

    monkeypatch.setattr(mod.config, "show_user_messages", False)
    ev = NormalizedEvent(kind=EventKind.USER_MESSAGE, text="hi")
    assert _codex_event_to_new_message("tid", ev) is None


def test_tool_call_suppressed_when_show_tool_calls_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot import session_monitor as mod

    monkeypatch.setattr(mod.config, "show_tool_calls", False)
    ev = NormalizedEvent(
        kind=EventKind.TOOL_CALL,
        text="x",
        tool_name="exec_command",
        tool_call_id="c1",
    )
    assert _codex_event_to_new_message("tid", ev) is None


# ---------- Integration: end-to-end against a real Codex fixture ------------


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "codex"


@pytest.fixture
def codex_session_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand up a session_map.json + state.sqlite catalog pointing at our fixture rollout."""
    from ccbot import session_monitor as mod

    # 1. session_map.json with one codex entry
    session_map = {
        f"{mod.config.tmux_session_name}:@0": {
            "session_id": "019e23f7-c20f-7390-8659-aa100c53092a",
            "cwd": "/tmp/codex-fixture-work/workdir1",
            "window_name": "workdir1",
            "runtime_kind": "codex",
        }
    }
    map_path = tmp_path / "session_map.json"
    map_path.write_text(json.dumps(session_map))

    # 2. Patch config to point at it + at a codex agent rooted in a fake home
    #    whose build_session_file_path resolves the fixture.
    monkeypatch.setattr(mod.config, "session_map_file", map_path)

    from ccbot.agents.codex import CodexAgent

    fixture_rollout = (
        FIXTURE_DIR / "rollouts" / "01_fresh_then_resumed_alpha_beta.jsonl"
    )

    # Override the agent so build_session_file_path returns our fixture path
    # regardless of what's in any catalog.
    class _StubCodex(CodexAgent):
        def build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
            return fixture_rollout

    agents: dict[str, Any] = dict(mod.config.agents)
    agents["codex"] = _StubCodex(codex_home=tmp_path)
    monkeypatch.setattr(mod.config, "agents", agents)

    return map_path


async def test_check_codex_updates_emits_messages_after_offset_init(
    tmp_path: Path,
    codex_session_map: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First poll seeds the offset at EOF (no replay). After we rewind the
    tracked offset to 0, the next poll should emit the conversation events."""
    state_file = tmp_path / "monitor_state.json"
    monitor = SessionMonitor(state_file=state_file)

    # First poll: starts tracking, no messages emitted (initial offset = EOF).
    first = await monitor.check_codex_updates()
    assert first == []

    # Rewind so the next poll re-reads the whole fixture.
    tracked = monitor.state.get_session("019e23f7-c20f-7390-8659-aa100c53092a")
    assert tracked is not None
    tracked.last_byte_offset = 0
    monitor._file_mtimes.pop("019e23f7-c20f-7390-8659-aa100c53092a", None)

    messages = await monitor.check_codex_updates()
    assert len(messages) > 0
    # The injected AGENTS.md user message must be filtered; first surfaced
    # user message must be the actual prompt.
    user_msgs = [m for m in messages if m.role == "user" and m.content_type == "text"]
    assert user_msgs, "expected at least one user message"
    assert user_msgs[0].text == "Reply with the single word: ALPHA"
    # And we should see at least one assistant message in there.
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    assert any("ALPHA" in m.text for m in assistant_msgs)


async def test_check_codex_updates_no_op_without_codex_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session map with only Claude entries must not emit Codex messages."""
    from ccbot import session_monitor as mod

    session_map = {
        f"{mod.config.tmux_session_name}:@0": {
            "session_id": "claude-session-id",
            "cwd": "/tmp/claude",
            "window_name": "x",
        }
    }
    map_path = tmp_path / "session_map.json"
    map_path.write_text(json.dumps(session_map))
    monkeypatch.setattr(mod.config, "session_map_file", map_path)

    monitor = SessionMonitor(state_file=tmp_path / "monitor_state.json")
    assert await monitor.check_codex_updates() == []


async def test_check_codex_updates_no_op_when_session_map_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccbot import session_monitor as mod

    monkeypatch.setattr(mod.config, "session_map_file", tmp_path / "absent.json")
    monitor = SessionMonitor(state_file=tmp_path / "monitor_state.json")
    assert await monitor.check_codex_updates() == []


# Surface check on NewMessage shape so a future refactor doesn't silently
# drop fields the renderer needs.
def test_new_message_fields_we_rely_on() -> None:
    msg = NewMessage(session_id="t", text="x", is_complete=True)
    assert hasattr(msg, "role")
    assert hasattr(msg, "content_type")
    assert hasattr(msg, "tool_use_id")
    assert hasattr(msg, "tool_name")
