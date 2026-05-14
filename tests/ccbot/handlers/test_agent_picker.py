"""Tests for the agent-picker UI (T8)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ccbot.handlers.callback_data import (
    CB_AGENT_CANCEL,
    CB_AGENT_CLAUDE,
    CB_AGENT_CODEX,
)
from ccbot.handlers.directory_browser import (
    PENDING_AGENT_KEY,
    STATE_KEY,
    STATE_PICKING_AGENT,
    build_agent_picker,
    clear_agent_picker_state,
)


def test_build_agent_picker_returns_text_and_keyboard() -> None:
    text, kb = build_agent_picker()
    assert "agent" in text.lower()
    # 2 rows: agent buttons + cancel
    assert len(kb.inline_keyboard) == 2
    first_row = kb.inline_keyboard[0]
    assert len(first_row) == 2
    callbacks = {btn.callback_data for btn in first_row}
    assert callbacks == {CB_AGENT_CLAUDE, CB_AGENT_CODEX}
    # Cancel row
    assert kb.inline_keyboard[1][0].callback_data == CB_AGENT_CANCEL


def test_agent_picker_default_appears_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default agent (from CCBOT_DEFAULT_AGENT) is shown first with a marker."""
    import ccbot.handlers.directory_browser as mod

    fake_cfg = MagicMock()
    fake_cfg.default_agent_kind = "codex"
    monkeypatch.setattr(mod, "config", fake_cfg)

    _, kb = build_agent_picker()
    labels = [btn.text for btn in kb.inline_keyboard[0]]
    # Codex is the default → first
    assert labels[0].startswith("Codex")
    assert "•" in labels[0]  # default marker
    assert "•" not in labels[1]


def test_clear_agent_picker_state_clears_only_when_in_picker_state() -> None:
    """clear_agent_picker_state shouldn't nuke state if we've moved on."""
    ud: dict = {STATE_KEY: STATE_PICKING_AGENT, PENDING_AGENT_KEY: "codex"}
    clear_agent_picker_state(ud)
    assert STATE_KEY not in ud
    # PENDING_AGENT_KEY survives — it's read downstream by _create_and_bind_window
    assert ud[PENDING_AGENT_KEY] == "codex"


def test_clear_agent_picker_state_noop_outside_picker_state() -> None:
    ud: dict = {STATE_KEY: "browsing_directory"}
    clear_agent_picker_state(ud)
    # State unrelated to the picker stays put
    assert ud[STATE_KEY] == "browsing_directory"


def test_clear_agent_picker_state_handles_none() -> None:
    # Telegram occasionally hands us None instead of a dict
    clear_agent_picker_state(None)  # must not raise
