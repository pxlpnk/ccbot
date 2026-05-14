# Runtime ontology

This is the contract every runtime adapter must follow. Adopted from issue #71
(strato-space/ccbot planning doc) and verified empirically against Codex 0.107.0
in `doc/codex-rollout.md`. The contract holds for Claude Code today and Codex
when implemented.

## The chain

```
Telegram topic
     │
     ▼
   binding ─── persisted association (topic ↔ window, plus runtime metadata)
     │
     ▼
 tmux window ─── live terminal container
     │
     ▼
 live process ─── the running `claude` / `codex` CLI in that window
     │
     ▼
   thread ──── the persisted conversation identity (resumable)
     │
     ▼
 rollout log ─── the JSONL evidence appended to disk by the process
```

## Non-collapsing invariants

The implementation MUST NOT conflate these entities. Each rule below is a
maintenance trap a future refactor could re-introduce.

1. **A window is not a thread.** Same window can run multiple threads
   sequentially (after `/clear` or after restarting the CLI). Same thread can
   appear in different windows across bot restarts.

2. **A live process is not its persisted thread.** Killing the process does
   not destroy the thread — the rollout file is preserved and the thread is
   still resumable. Conversely, resuming a thread spawns a *new* live process
   that binds to the *existing* thread.

3. **A rollout log is evidence, not identity.** Multiple processes may append
   to the same rollout over time (resume case). The rollout is what we read;
   the thread is what we name.

4. **Resume does not rename.** `codex exec resume <id>` keeps the same thread
   id and the same rollout file. Same is true for `claude --resume <id>` (with
   the asymmetry noted below).

5. **Same cwd is not unique.** Multiple threads can be rooted in the same
   directory. The catalog MUST return all candidates and the UI MUST fail
   closed on ambiguity — never auto-pick.

## Direction of data flow

- **Outbound (user → process)**: the bot writes via tmux keystrokes to the
  live process. Tmux is the only authoritative input channel.
- **Inbound (process → user)**: the bot reads from the rollout log, not from
  the tmux pane. Pane reads are reserved for status/prompt-state detection.

This is a hard separation. Notifications must not be sourced from terminal
scraping, and input must not be inferred from rollout events.

## Per-runtime asymmetries (deliberate, documented)

| Concept       | Claude Code                                  | Codex 0.107.0                                |
| ------------- | -------------------------------------------- | -------------------------------------------- |
| Thread id     | `sessionId` in JSONL (UUID)                  | `id` in `session_meta` and `threads` row (UUID) |
| Catalog       | Filesystem (`~/.claude/projects/<cwd>/*.jsonl`) | SQLite (`~/.codex/state_5.sqlite`, table `threads`) |
| Rollout path  | `~/.claude/projects/<encoded_cwd>/<id>.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-{ts}-{id}.jsonl` (absolute path stored in `threads.rollout_path`) |
| Resume        | `claude --resume <id>` — assigns new session_id at the hook layer but **appends to the original JSONL** | `codex exec resume <id>` — keeps the same id and appends to the original rollout |
| Hook system   | `SessionStart` in `~/.claude/settings.json`  | `hooks.session_start` in `~/.codex/config.toml` (TOML) |
| Message schema| Top-level `type: user/assistant/summary` with `content: [{type:text|tool_use|tool_result}]` | `type: response_item|event_msg|turn_context|session_meta` with `payload.type ∈ {message, reasoning, function_call, function_call_output, ...}` |
| Tool naming   | Per-tool names (`Read`, `Bash`, `Edit`)     | `function_call.name == "exec_command"` for shell, others as the model declares |

## Adapter API surface (preview of T3)

Every runtime adapter (`ClaudeAgent`, `CodexAgent`) must answer to these,
typed in terms of the entities above:

- `thread_catalog(cwd) -> list[ThreadLocator]` — returns persisted threads
  rooted in `cwd`. Order: most-recently-updated first. **Never auto-picks.**
- `rollout_source(thread: ThreadLocator) -> RolloutSource` — the file to tail
  for incremental reads.
- `parse_rollout(line: bytes) -> NormalizedEvent | None` — codec; returns
  `None` for lines that should be ignored (e.g. duplicate live-event
  envelopes).
- `spawn_args(cwd, resume: ThreadLocator | None) -> list[str]` — argv for the
  live process. Caller wraps it in a tmux window.
- `hook_install_target() -> HookTarget` — where the `ccbot hook` registration
  is written; the launcher-side registry is primary truth for binding (see
  T6).
- `terminal_state(pane_text: str) -> TerminalState` — read-only TUI state
  classifier: one of `input_ready | busy | blocked_on_prompt | unknown`.
  Unknown states MUST NOT expose active controls.

## Out of scope for the Codex integration

Pinned by existing tests; refactor must not regress these:

- Voice transcription (`tests/ccbot/test_transcribe.py`)
- Slash-command passthrough (`tests/ccbot/test_forward_command.py`)
- Markdown → Telegram conversion (`tests/ccbot/test_markdown_v2.py`)
- Telegram send/split helpers (`tests/ccbot/test_telegram_sender.py`)
- Hook installer for Claude (`tests/ccbot/test_hook.py`)
- Per-window message queue + merging (`tests/ccbot/handlers/test_status_polling.py` and `test_response_builder.py`)

Approval-action UI for Codex prompts is *not* part of this scope (deferred to
post-MVP, see T10). The first Codex release exposes read-only state detection
only.
