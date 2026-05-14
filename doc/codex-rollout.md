# Codex rollout format and thread catalog

Captured against `codex-cli 0.107.0`. Source files in `tests/fixtures/codex/`.

## Storage layout

```
$CODEX_HOME/                       # default ~/.codex
├── state_5.sqlite                 # thread catalog (SQLite, WAL mode)
├── sessions/
│   └── YYYY/MM/DD/
│       └── rollout-{TS}-{UUID}.jsonl
├── config.toml
└── auth.json
```

`CODEX_HOME` env var overrides the default. No CLI flag exposes it; setting the
env is the only knob.

## Thread catalog (`state_5.sqlite`, table `threads`)

Authoritative listing of every Codex thread, regardless of which day's directory
the rollout lives in. The `rollout_path` column is an absolute path to the JSONL
file. Full schema is in `tests/fixtures/codex/sqlite/threads.schema.sql`.

Columns the ccbot adapter needs:

| Column              | Purpose                                              |
| ------------------- | ---------------------------------------------------- |
| `id`                | Thread UUID — same id appears in rollout filename and in `session_meta.payload.id` |
| `rollout_path`      | Absolute path to the JSONL                           |
| `cwd`               | Working directory the thread was launched in        |
| `created_at`        | Unix seconds                                         |
| `updated_at`        | Unix seconds (touched on resume)                     |
| `source`            | `exec` for non-interactive, other values for TUI    |
| `archived`          | 0/1 — exclude archived from picker by default       |
| `title`             | First user prompt (often long; truncate for picker) |
| `first_user_message`| Same content, kept as separate column                |
| `git_branch`        | Optional — for display only                          |
| `tokens_used`       | Optional — for display only                          |

## Resume semantics (verified empirically)

`codex exec resume <thread_id> <prompt>`:
- **Same thread id** in the new process's stdout (`session id: <same id>`)
- **No new rollout file** — new turns append to the original `rollout_path`
- **No new `threads` row** — only `updated_at` advances

This is the ontology critical invariant from issue #71: a resumed live process
binds to an existing thread + rollout, it does not create new ones. ccbot's
binding layer must track `(window_id, live_pid, thread_id)` independently.

## Rollout JSONL format

One JSON object per line. Top level:

```json
{ "timestamp": "ISO-8601", "type": "<line_type>", "payload": { ... } }
```

### Line types and what to do with them

| `type`          | `payload.type` (where present)             | Meaning                                              |
| --------------- | ------------------------------------------ | ---------------------------------------------------- |
| `session_meta`  | —                                          | Once per file, at top. Has `id`, `cwd`, `cli_version`, `originator`, `base_instructions` (full system prompt) |
| `turn_context`  | —                                          | Once per turn. `cwd`, `approval_policy`, `sandbox_policy`, `model`, `user_instructions`. Boundary marker for turn N |
| `response_item` | `message`                                  | A message with `role` ∈ {`user`, `assistant`, `system`, `developer`} and `content[]` of `{type: input_text/output_text, text}` |
| `response_item` | `reasoning`                                | Thinking content. Often `content: null` with only `encrypted_content` (opaque blob). Render as "Codex reasoned" placeholder unless `summary` is non-empty |
| `response_item` | `function_call`                            | Tool call. `name`, `arguments` (JSON-encoded string), `call_id` |
| `response_item` | `function_call_output`                     | Tool result, paired by `call_id`. `output` is a multi-line string with `Chunk ID`, `Wall time`, `Process exited with code N`, `Output:` sections |
| `event_msg`     | `task_started` / `task_complete`           | Turn lifecycle markers                               |
| `event_msg`     | `user_message`                             | Duplicate of the corresponding `response_item.message` for live streaming. **Skip for history reconstruction.** |
| `event_msg`     | `agent_message`                            | Duplicate of assistant `response_item.message`. **Skip for history reconstruction.** |
| `event_msg`     | `token_count`                              | Token usage stats. Optional display |

### History reconstruction rule

For the `/history` view, **read `response_item` lines only**. `event_msg` lines
are a parallel live-event stream and would duplicate every assistant message.

### Tool name in v0.107.0

The current binary emits `function_call.name == "exec_command"` for shell
execution (not `local_shell_call` as the protocol docs suggested for an earlier
version). The `arguments` JSON is `{"cmd": "...", "workdir": "..."}`. Parser
should match on `name == "exec_command"` and gracefully fall through for other
tool names.

### Function call output structure

`function_call_output.output` looks like:

```
Chunk ID: 9f0126
Wall time: 0.0510 seconds
Process exited with code 0
Original token count: 3
Output:
<actual stdout/stderr>
```

The adapter should extract `Process exited with code` for the status badge and
the section after `Output:` for the body.

## Fixture inventory

| File                                                | What it covers                                                                                          |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `rollouts/01_fresh_then_resumed_alpha_beta.jsonl`   | Fresh `exec` thread (ALPHA) followed by `exec resume` (BETA) — verifies append-on-resume                |
| `rollouts/02_second_thread_same_cwd_gamma.jsonl`    | Independent thread in the same cwd as #01 — verifies same-cwd ambiguity (catalog returns two candidates) |
| `rollouts/03_tool_use_function_call.jsonl`          | `reasoning` + `function_call` (`exec_command`) + `function_call_output` round-trip                       |
| `sqlite/threads.schema.sql`                         | DDL for `threads` (the catalog table)                                                                   |
| `sqlite/threads.sample.tsv`                         | Tab-separated rows for the three synthetic threads (no personal data)                                   |

## Outstanding

- TUI pane snapshots (idle, busy, approval prompt) — TODO before T10
- Hook payload sample (`SessionStart` from Codex side) — TODO when T7 lands
- Interrupted-turn rollout (`task_started` without matching `task_complete`) — TODO
- Confirm behaviour of `codex fork <id>` (creates new thread + rollout, references parent?) — not yet captured
