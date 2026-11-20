# Langfuse Observability Hook for Qoder CN

Trace every Qoder CN session to [Langfuse](https://langfuse.com) — turns, generations, tool calls, and token usage — with zero code changes.

## Supported Products

Qoder CN is the mainland-China edition (国内版) of Qoder. This hook supports its Desktop form:

| Product | Type | Notes |
|---------|------|-------|
| **Qoder CN Desktop** | GUI (Electron `com.qodercn.app.stable`) | Token/model data enriched from the SharedClientCache SQLite DB; subagent internals not available (see [Known limitations](#known-limitations)) |

Qoder CN has no CLI form. All of its data (config, transcripts, DB, hook state) lives under `~/.qoder-cn/` and is fully independent from the international Qoder's `~/.qoder/` — on a machine where both are installed, this hook only ever reads `~/.qoder-cn/`.

## Install

```bash
git clone https://github.com/aliyun/agent-exporter-to-langfuse.git
cd agent-exporter-to-langfuse/hooks/qoder-cn
bash install.sh
```

The install script will interactively guide you through:
1. Entering Langfuse credentials (Base URL, Public Key, Secret Key)
2. Copying the hook script to `~/.qoder-cn/hooks/langfuse/`
3. Initializing the uv environment and installing the SDK dependencies
4. Adding `Stop` and `SubagentStop` hook entries to `~/.qoder-cn/settings.json`
5. Writing environment variables to `~/.agent-exporter-to-langfuse/config/qoder-cn.env` (loaded by the hook entrypoint at runtime)

If the hook is already installed, the script will prompt before overwriting. Re-running the script updates the credentials automatically.

The unified installer (`deploy/installer.sh`) auto-detects `~/.qoder-cn` and installs/updates this hook non-interactively (`install.sh --upgrade -y`).

## Configuration

The install script configures the required environment variables. Full variable list:

| Variable | Required | Description |
|----------|----------|-------------|
| `LANGFUSE_SECRET_KEY` | Yes | Your Langfuse secret key (set during install). |
| `LANGFUSE_PUBLIC_KEY` | Yes | Your Langfuse public key (set during install). |
| `LANGFUSE_BASE_URL` | Yes | Your Langfuse host URL (set during install). |
| `LANGFUSE_USER_ID` | No | User identifier for Langfuse traces. Defaults to the Qoder CN account name from the hook payload, then the OS account username. |
| `LANGFUSE_TAGS` | No | Comma-separated tags for Langfuse traces (e.g. `team:olap,env:prod`). Default `qoder-cn`. |
| `LANGFUSE_MAX_CHARS` | No | Maximum characters per content field. Default 800000 (~200K tokens). |
| `LANGFUSE_DEBUG` | No | Verbose logging to `~/.qoder-cn/state/langfuse_hook.log`. Default `true`. Set `false` to disable. |
| `LANGSTASH_ENABLED` | No | Deliver through the local langstash buffer (`true`) or push directly to Langfuse. Install writes `true`. |
| `LANGSTASH_URL` | No | URL of the local langstash buffer. Install writes `http://127.0.0.1:5288`. |

Credentials are stored in a dedicated file `~/.agent-exporter-to-langfuse/config/qoder-cn.env` (one file per product, shared config directory). The hook entrypoint sources this file at runtime, so GUI sessions pick it up without any shell profile change; the file is cleanly removable on uninstall.

## Requirements

- Qoder CN Desktop (国内版)
- [uv](https://docs.astral.sh/uv/) — Python package manager (auto-initializes on first run)
- Python 3.11+

## How it works

### Hook trigger and data flow

The `Stop` hook fires at the end of each agent turn. `SubagentStop` is registered so subagent completions can be captured as well; on the Desktop form its payload lacks the subagent's own transcript, so those events are skipped gracefully (see [Known limitations](#known-limitations)). Both events receive a JSON payload via stdin:

```json
{
  "session_id": "26d0203c-...",
  "transcript_path": "/Users/.../.qoder-cn/projects/.../26d0203c-....jsonl",
  "cwd": "/Users/.../project",
  "hook_event_name": "Stop"
}
```

The hook incrementally reads the JSONL transcript and assembles a turn → generation → tool hierarchy, then sends it to Langfuse via `langstash-deliver` (langstash local buffer → direct push → failed-log fallback). State is persisted in `~/.qoder-cn/state/langfuse_state.json` to ensure only new turns are emitted.

### Token & model enrichment (SQLite)

JSONL transcripts do not contain token usage data. The hook enriches traces by querying the Qoder CN SharedClientCache SQLite database:

- **DB paths** (first match wins): `~/Library/Application Support/com.qodercn.app.stable/SharedClientCache/cache/db/local.db` (macOS App Support, probed first on macOS), then `~/.qoder-cn/shared_client/cache/db/local.db`
- **Table**: `chat_message`, filtered by `session_id` + `role = 'assistant'`
- **Fields**: `prompt_tokens` / `completion_tokens` / `cached_tokens` from `token_info`, model name from `model_info.model_key` (e.g. `dashscope_qwen3_coder`)
- **Matching**: Approximate timestamp matching (5-second window)

If the DB is missing or has no matching rows, the trace is still delivered — just without token/model data.

Only the qoder-cn paths above are consulted; the international Qoder's DB (`~/Library/Application Support/Qoder/...`, `~/.qoder/...`) is never read, so coexisting installations never mix session data.

### Known limitations

**Subagent internals unavailable (Desktop)**

Desktop fires `SubagentStop` hooks but the payload contains only `transcript_path` (the main agent's transcript), not `agent_transcript_path` (the subagent's own transcript), so the subagent's internal data cannot be located. The hook skips these events gracefully. Subagent invocations still appear in the main transcript as `Agent` tool_use/tool_result pairs — the subagent shows up in Langfuse as a single `Tool: Agent` span with its final output and duration, but its internal LLM calls, tool chain, and token consumption are not captured.

**Token/model data comes from the SharedClientCache DB**

Transcript assistant messages include no `message.usage`, and there is no CLI data source — token counts and model names are only available through the SQLite DB enrichment described above. Sessions with no DB rows are delivered without token/model data.

**Desktop transcript rows have no `message.id`**

Desktop writes each content block (thinking, text, tool_use) as a separate JSONL row without a shared `message.id`. The hook merges consecutive assistant rows using a gap-detection heuristic. This works reliably in practice but is inherently fragile.

**Model names may require manual cost configuration**

Model names come from the DB `model_key` (e.g. `dashscope_qwen3_coder`). If Langfuse does not know the model's pricing, add it manually in your Langfuse project settings under **Models** to get cost calculations.

### settings.json hook entry

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.qoder-cn/hooks/langfuse/langfuse-entrypoint.sh",
            "timeout": 20
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.qoder-cn/hooks/langfuse/langfuse-entrypoint.sh",
            "timeout": 20
          }
        ]
      }
    ]
  }
}
```

Registration is idempotent: if the event already contains a hook entry whose command mentions `langfuse`, the installer does not add another one. Third-party hook entries (e.g. `loongsuite-pilot`) are preserved.

## Uninstall

```bash
cd agent-exporter-to-langfuse/hooks/qoder-cn
bash uninstall.sh
```

The uninstall script removes:
- Hook script and uv environment (`~/.qoder-cn/hooks/langfuse/`)
- Langfuse `Stop` / `SubagentStop` entries from `~/.qoder-cn/settings.json` (other entries preserved)
- State and log files (`~/.qoder-cn/state/langfuse_*`)
- Environment file (`~/.agent-exporter-to-langfuse/config/qoder-cn.env`) — preserved by default, removed with `bash uninstall.sh --purge`

## Troubleshooting

- **Nothing in Langfuse**: check `~/.qoder-cn/state/langfuse_hook.log` (set `LANGFUSE_DEBUG=true`).
- **Hook not firing**: verify the `Stop` hook entry exists in `~/.qoder-cn/settings.json`; restart Qoder CN.
- **uv errors**: ensure [uv](https://docs.astral.sh/uv/) is installed and available on your PATH.

## License

MIT
