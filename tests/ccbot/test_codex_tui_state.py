"""Tests for the read-only Codex TUI state classifier (T10).

These pin the heuristic. When a real Codex tmux snapshot lands in fixtures,
add an integration test that asserts the classifier returns the expected
state for that snapshot.
"""

from __future__ import annotations

import pytest

from ccbot.terminal_parser import classify_codex_state


def test_unknown_on_empty_or_blank() -> None:
    assert classify_codex_state("") == "unknown"
    assert classify_codex_state("   \n  \n") == "unknown"


def test_approval_detected() -> None:
    samples = [
        "shell> rm -rf /tmp/foo\n\n[y / n / a] Approve and run?",
        "Codex wants to run a command.\nAllow command? [y · n]",
        "Run this command?\n\n  rm -rf .git\n\n  y / n",
        "Always allow this tool?\nyes / no",
    ]
    for pane in samples:
        assert classify_codex_state(pane) == "approval", pane


def test_busy_detected() -> None:
    samples = [
        "Some scrollback\nThinking...\n",
        "Reasoning for 3s\n",
        "Working on your request",
        "Streaming response from gpt-5.3-codex",
    ]
    for pane in samples:
        assert classify_codex_state(pane) == "busy", pane


def test_input_ready_detected() -> None:
    samples = [
        "Some scrollback\nfoo bar\n\n▌\n",
        "Some scrollback\n\n❯ \n",
    ]
    for pane in samples:
        assert classify_codex_state(pane) == "input_ready", pane


def test_unknown_when_no_marker() -> None:
    samples = [
        "Just some random output\nNo prompt, no spinner, no modal",
        "$ ls\nfile.txt\n",
    ]
    for pane in samples:
        assert classify_codex_state(pane) == "unknown", pane


def test_approval_wins_over_busy() -> None:
    """If both 'Thinking' scrollback and an approval prompt appear, the
    foreground action (approval) wins so we don't mis-classify a stalled
    turn as 'busy' and silently forward keystrokes."""
    pane = "Thinking...\nCodex wants to run: ls\nApprove and run? [y / n]\n"
    assert classify_codex_state(pane) == "approval"


@pytest.mark.parametrize("state", ["approval", "busy", "input_ready", "unknown"])
def test_returns_string_literal(state: str) -> None:
    """Sanity check the contract — callers branch on string values."""
    assert isinstance(state, str)
