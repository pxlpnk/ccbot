"""Tests for the ClaudeAgent adapter (T3a).

These tests pin the adapter's behavior to the *current* Claude on-disk layout.
Existing `SessionManager` tests already cover the round-trip through
`_encode_cwd` / `_build_session_file_path` / `list_sessions_for_directory`;
the cases here focus on the agent's own surface so future refactors don't
quietly drift away from it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccbot.agents import Agent, ClaudeAgent, SessionSummary


def test_claude_agent_satisfies_protocol(tmp_path: Path) -> None:
    agent = ClaudeAgent(projects_path=tmp_path)
    # Protocol membership is structural, but runtime_checkable lets us assert it.
    assert isinstance(agent, Agent)
    assert agent.name == "claude"
    assert agent.projects_path == tmp_path


def test_encode_cwd_replaces_non_alnum_with_dash(tmp_path: Path) -> None:
    agent = ClaudeAgent(projects_path=tmp_path)
    assert agent.encode_cwd("/home/user_name/Code/project") == (
        "-home-user-name-Code-project"
    )
    assert agent.encode_cwd("/a.b/c") == "-a-b-c"
    assert agent.encode_cwd("") == ""


def test_build_session_file_path_returns_none_on_empty_inputs(tmp_path: Path) -> None:
    agent = ClaudeAgent(projects_path=tmp_path)
    assert agent.build_session_file_path("", "/cwd") is None
    assert agent.build_session_file_path("sid", "") is None


def test_build_session_file_path_round_trips_encoded_cwd(tmp_path: Path) -> None:
    agent = ClaudeAgent(projects_path=tmp_path)
    path = agent.build_session_file_path("abc-123", "/home/foo/bar")
    assert path == tmp_path / "-home-foo-bar" / "abc-123.jsonl"


async def test_list_sessions_for_directory_empty_when_dir_missing(
    tmp_path: Path,
) -> None:
    agent = ClaudeAgent(projects_path=tmp_path)
    assert await agent.list_sessions_for_directory("/nowhere") == []


async def test_list_sessions_for_directory_summarises_and_orders(
    tmp_path: Path,
) -> None:
    agent = ClaudeAgent(projects_path=tmp_path)
    cwd = "/home/u/proj"
    project_dir = tmp_path / agent.encode_cwd(cwd)
    project_dir.mkdir(parents=True)

    # Synthesise two JSONL session files. Each line is a Claude-shaped entry —
    # one a `summary`, the rest user/assistant messages so `message_count > 0`.
    older = project_dir / "older.jsonl"
    older.write_text(
        json.dumps({"type": "summary", "summary": "older session"})
        + "\n"
        + json.dumps({"type": "user", "message": {"content": "hi"}})
        + "\n"
    )
    newer = project_dir / "newer.jsonl"
    newer.write_text(
        json.dumps({"type": "summary", "summary": "newer session"})
        + "\n"
        + json.dumps({"type": "user", "message": {"content": "yo"}})
        + "\n"
    )
    # Force newer.mtime to be later than older.mtime.
    import os
    import time

    os.utime(older, (time.time() - 100, time.time() - 100))

    sessions = await agent.list_sessions_for_directory(cwd)
    assert [s.session_id for s in sessions] == ["newer", "older"]
    assert all(isinstance(s, SessionSummary) for s in sessions)
    assert sessions[0].summary == "newer session"


async def test_list_sessions_skips_sessions_index_artifact(tmp_path: Path) -> None:
    agent = ClaudeAgent(projects_path=tmp_path)
    cwd = "/home/u/proj"
    project_dir = tmp_path / agent.encode_cwd(cwd)
    project_dir.mkdir(parents=True)
    (project_dir / "sessions-index.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "ignored"}}) + "\n"
    )
    (project_dir / "real.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "kept"}}) + "\n"
    )

    sessions = await agent.list_sessions_for_directory(cwd)
    assert [s.session_id for s in sessions] == ["real"]


async def test_list_sessions_skips_zero_message_files(tmp_path: Path) -> None:
    agent = ClaudeAgent(projects_path=tmp_path)
    cwd = "/home/u/proj"
    project_dir = tmp_path / agent.encode_cwd(cwd)
    project_dir.mkdir(parents=True)
    (project_dir / "empty.jsonl").write_text("")
    (project_dir / "real.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "kept"}}) + "\n"
    )

    sessions = await agent.list_sessions_for_directory(cwd)
    assert [s.session_id for s in sessions] == ["real"]


def test_claude_session_alias_is_session_summary() -> None:
    # session.py keeps a backward-compat re-export for older callers.
    from ccbot.session import ClaudeSession

    assert ClaudeSession is SessionSummary


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive: tests must not depend on CLAUDE_CONFIG_DIR / etc.
    for var in (
        "CCBOT_CLAUDE_PROJECTS_PATH",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
