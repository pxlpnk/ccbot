"""Tests for the CodexAgent adapter.

Exercises the agent against the T1 fixtures in ``tests/fixtures/codex/`` so the
adapter stays honest about the on-disk shape that codex-cli 0.107.0 actually
emits.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ccbot.agents import Agent, CodexAgent, EventKind, NormalizedEvent

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "codex"
ROLLOUT_DIR = FIXTURE_DIR / "rollouts"
SCHEMA_FILE = FIXTURE_DIR / "sqlite" / "threads.schema.sql"


def test_codex_agent_satisfies_protocol(tmp_path: Path) -> None:
    agent = CodexAgent(codex_home=tmp_path)
    assert isinstance(agent, Agent)
    assert agent.name == "codex"
    assert agent.default_command == "codex"
    assert agent.projects_path == tmp_path / "sessions"
    assert agent.hook_settings_path == tmp_path / "config.toml"


def test_encode_cwd_is_noop(tmp_path: Path) -> None:
    agent = CodexAgent(codex_home=tmp_path)
    assert agent.encode_cwd("/home/user/project") == "/home/user/project"


def test_build_session_file_path_uses_catalog(tmp_path: Path) -> None:
    catalog = _make_catalog(
        tmp_path,
        [
            (
                "019e23f7-c20f-7390-8659-aa100c53092a",
                "/tmp/x",
                "/tmp/x/rollout-foo.jsonl",
            ),
        ],
    )
    agent = CodexAgent(codex_home=tmp_path, catalog_path=catalog)
    p = agent.build_session_file_path("019e23f7-c20f-7390-8659-aa100c53092a", "/tmp/x")
    assert p == Path("/tmp/x/rollout-foo.jsonl")


def test_build_session_file_path_none_when_unknown(tmp_path: Path) -> None:
    catalog = _make_catalog(tmp_path, [])
    agent = CodexAgent(codex_home=tmp_path, catalog_path=catalog)
    assert agent.build_session_file_path("missing", "/tmp/x") is None
    assert agent.build_session_file_path("", "/tmp/x") is None


def test_build_session_file_path_none_when_catalog_missing(tmp_path: Path) -> None:
    agent = CodexAgent(codex_home=tmp_path, catalog_path=tmp_path / "absent.sqlite")
    assert agent.build_session_file_path("any", "/tmp/x") is None


async def test_list_sessions_returns_all_same_cwd_candidates(tmp_path: Path) -> None:
    """Same-cwd ambiguity is preserved — the picker decides, not the catalog."""
    rollout_a = ROLLOUT_DIR / "01_fresh_then_resumed_alpha_beta.jsonl"
    rollout_b = ROLLOUT_DIR / "02_second_thread_same_cwd_gamma.jsonl"
    catalog = _make_catalog(
        tmp_path,
        [
            (
                "019e23f7-c20f-7390-8659-aa100c53092a",
                "/tmp/codex-fixture-work/workdir1",
                str(rollout_a),
                "alpha",
            ),
            (
                "019e23f7-ece6-7583-95a7-4563f451a187",
                "/tmp/codex-fixture-work/workdir1",
                str(rollout_b),
                "gamma",
            ),
        ],
    )
    agent = CodexAgent(codex_home=tmp_path, catalog_path=catalog)
    sessions = await agent.list_sessions_for_directory(
        "/tmp/codex-fixture-work/workdir1"
    )
    assert len(sessions) == 2
    ids = {s.session_id for s in sessions}
    assert "019e23f7-c20f-7390-8659-aa100c53092a" in ids
    assert "019e23f7-ece6-7583-95a7-4563f451a187" in ids


async def test_list_sessions_empty_when_no_catalog(tmp_path: Path) -> None:
    agent = CodexAgent(codex_home=tmp_path, catalog_path=tmp_path / "absent.sqlite")
    assert await agent.list_sessions_for_directory("/tmp/anywhere") == []


# ---------- Rollout parsing ---------------------------------------------------


@pytest.fixture
def agent() -> CodexAgent:
    return CodexAgent(codex_home=Path("/nonexistent"))


def test_parse_rollout_handles_blank_and_garbage(agent: CodexAgent) -> None:
    assert agent.parse_rollout_line("") is None
    assert agent.parse_rollout_line("\n") is None
    assert agent.parse_rollout_line("not json") is None


def test_parse_rollout_classifies_each_known_line_type(agent: CodexAgent) -> None:
    cases: list[tuple[dict, EventKind | None]] = [
        ({"type": "session_meta", "payload": {"id": "x"}}, EventKind.LIFECYCLE),
        ({"type": "turn_context", "payload": {"turn_id": "x"}}, EventKind.LIFECYCLE),
        (
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
            EventKind.USER_MESSAGE,
        ),
        (
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}],
                },
            },
            EventKind.ASSISTANT_MESSAGE,
        ),
        (
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "encrypted_content": "blob"},
            },
            EventKind.THINKING,
        ),
        (
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": "{}",
                    "call_id": "c1",
                },
            },
            EventKind.TOOL_CALL,
        ),
        (
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "Output:\nhello",
                },
            },
            EventKind.TOOL_RESULT,
        ),
        (
            {"type": "event_msg", "payload": {"type": "task_started"}},
            EventKind.LIFECYCLE,
        ),
        # Duplicate live-event envelopes are skipped.
        ({"type": "event_msg", "payload": {"type": "user_message"}}, None),
        ({"type": "event_msg", "payload": {"type": "agent_message"}}, None),
    ]
    for raw, expected in cases:
        line = json.dumps(raw)
        ev = agent.parse_rollout_line(line)
        if expected is None:
            assert ev is None, f"expected None for {raw['payload']}"
        else:
            assert ev is not None and ev.kind is expected, f"{raw} -> {ev}"


def test_parse_function_call_carries_name_and_id(agent: CodexAgent) -> None:
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"wc -l"}',
                "call_id": "call_xyz",
            },
        }
    )
    ev = agent.parse_rollout_line(line)
    assert ev is not None
    assert ev.tool_name == "exec_command"
    assert ev.tool_call_id == "call_xyz"


def test_parse_function_output_strips_codex_header(agent: CodexAgent) -> None:
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": (
                    "Chunk ID: 9f0126\n"
                    "Wall time: 0.05 seconds\n"
                    "Process exited with code 0\n"
                    "Output:\n"
                    "14 file.txt\n"
                ),
            },
        }
    )
    ev = agent.parse_rollout_line(line)
    assert ev is not None
    assert ev.text == "14 file.txt"


async def test_iter_rollout_events_against_fixture(agent: CodexAgent) -> None:
    """Smoke-test: real rollout file produces a sensible event stream."""
    fixture = ROLLOUT_DIR / "03_tool_use_function_call.jsonl"
    events: list[NormalizedEvent] = []
    async for ev in agent.iter_rollout_events(fixture):
        events.append(ev)

    kinds = [e.kind for e in events]
    # We expect at least one of each: user message, assistant message,
    # tool_call, tool_result.
    assert EventKind.USER_MESSAGE in kinds
    assert EventKind.ASSISTANT_MESSAGE in kinds
    assert EventKind.TOOL_CALL in kinds
    assert EventKind.TOOL_RESULT in kinds
    # No duplicate agent_message/user_message envelopes from event_msg lines.
    msg_texts = [e.text for e in events if e.kind == EventKind.ASSISTANT_MESSAGE]
    assert len(msg_texts) == len(set(msg_texts)) or len(msg_texts) <= 2


# ---------- Spawn argv --------------------------------------------------------


def test_spawn_argv_fresh(tmp_path: Path) -> None:
    agent = CodexAgent(codex_home=tmp_path)
    assert agent.spawn_argv() == ["codex"]


def test_spawn_argv_resume(tmp_path: Path) -> None:
    agent = CodexAgent(codex_home=tmp_path)
    tid = "019e23f7-c20f-7390-8659-aa100c53092a"
    assert agent.spawn_argv(resume_thread_id=tid) == ["codex", "resume", tid]


def test_spawn_argv_rejects_non_uuid(tmp_path: Path) -> None:
    agent = CodexAgent(codex_home=tmp_path)
    with pytest.raises(ValueError):
        agent.spawn_argv(resume_thread_id="not-a-uuid")


def test_spawn_argv_passes_extra(tmp_path: Path) -> None:
    agent = CodexAgent(codex_home=tmp_path)
    assert agent.spawn_argv(extra=["--yolo"]) == ["codex", "--yolo"]


# ---------- Hook installation -------------------------------------------------


def test_install_hook_writes_toml(tmp_path: Path) -> None:
    settings = tmp_path / "config.toml"
    agent = CodexAgent(codex_home=tmp_path, hook_settings_path=settings)
    assert not agent.is_hook_installed()
    result = agent.install_hook()
    assert result.installed
    assert settings.exists()
    text = settings.read_text()
    assert "hooks" in text
    assert "session_start" in text
    assert "ccbot hook" in text
    assert agent.is_hook_installed()


def test_install_hook_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / "config.toml"
    agent = CodexAgent(codex_home=tmp_path, hook_settings_path=settings)
    agent.install_hook()
    second = agent.install_hook()
    assert not second.installed
    assert "already" in second.message.lower()


def test_install_hook_preserves_existing_keys(tmp_path: Path) -> None:
    settings = tmp_path / "config.toml"
    settings.write_text('model = "gpt-5"\nsandbox_mode = "read-only"\n')
    agent = CodexAgent(codex_home=tmp_path, hook_settings_path=settings)
    agent.install_hook()
    text = settings.read_text()
    assert 'model = "gpt-5"' in text
    assert "sandbox_mode" in text
    assert "hooks" in text


def test_detect_hook_payload_codex_shape() -> None:
    assert CodexAgent.detect_hook_payload(
        {"conversation_id": "x", "hook_event_name": "SessionStart"}
    )
    assert CodexAgent.detect_hook_payload(
        {"thread_id": "x", "hook_event_name": "PreToolUse"}
    )
    # Claude shape is rejected.
    assert not CodexAgent.detect_hook_payload(
        {"session_id": "x", "source": "startup", "hook_event_name": "SessionStart"}
    )
    assert not CodexAgent.detect_hook_payload({"random": "data"})


# ---------- Helpers -----------------------------------------------------------


def _make_catalog(
    tmp_path: Path, rows: list[tuple[str, str, str] | tuple[str, str, str, str]]
) -> Path:
    """Build a minimal threads-table SQLite catalog for tests."""
    catalog = tmp_path / "state_5.sqlite"
    schema = SCHEMA_FILE.read_text()
    with sqlite3.connect(catalog) as conn:
        conn.executescript(schema)
        for row in rows:
            tid = row[0]
            cwd = row[1]
            rollout_path = row[2]
            title = row[3] if len(row) > 3 else "test thread"
            conn.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source,
                    model_provider, cwd, title, sandbox_policy, approval_mode,
                    cli_version, first_user_message
                ) VALUES (?, ?, ?, ?, 'exec', 'openai', ?, ?, 'read-only',
                          'never', '0.107.0', ?)
                """,
                (
                    tid,
                    rollout_path,
                    1_700_000_000,
                    1_700_000_000,
                    cwd,
                    title,
                    title,
                ),
            )
        conn.commit()
    return catalog
