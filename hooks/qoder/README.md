# Langfuse Observability Hook for Qoder

Trace every Qoder session to [Langfuse](https://langfuse.com) — turns, generations, tool calls, and token usage — with zero code changes.

## Supported Products

This hook works with all Qoder products that share the `~/.qoder/` configuration:

| Product | Type | Notes |
|---------|------|-------|
| **Qoder CLI** (`qodercli`) | CLI | `message.model`, `message.id`, subagent capture via `agent_transcript_path` (v1.0.10+). **No `message.usage`** — token data unavailable for CLI sessions. |
| **Qoder Desktop** | GUI (Electron) | Token/model enriched from SQLite DB; subagent internals not available (see [Known limitations](#known-limitations)) |
| **QoderWake** | CLI wrapper | Uses `qodercli` under the hood — fully supported, no separate configuration needed |

## Install

```bash
git clone https://github.com/aliyun/agent-exporter-to-langfuse.git
cd agent-exporter-to-langfuse/hooks/qoder
bash install.sh
```

The install script will interactively guide you through:
1. Entering Langfuse credentials (Base URL, Public Key, Secret Key)
2. Copying the hook script to `~/.qoder/hooks/langfuse/`
3. Initializing the uv environment and installing the langfuse SDK
4. Adding `Stop` and `SubagentStop` hook entries to `~/.qoder/settings.json`
5. Persisting environment variables for both shell and GUI apps, loaded at system startup

If the hook is already installed, the script will prompt before overwriting. Re-running the script updates the credentials automatically.

## Configuration

The install script configures the required environment variables. Full variable list:

| Variable | Required | Description |
|----------|----------|-------------|
| `LANGFUSE_SECRET_KEY` | Yes | Your Langfuse secret key (set during install). |
| `LANGFUSE_PUBLIC_KEY` | Yes | Your Langfuse public key (set during install). |
| `LANGFUSE_BASE_URL` | Yes | Your Langfuse host URL (set during install). |
| `LANGFUSE_USER_ID` | No | User identifier for Langfuse traces. Defaults to OS account username if not set. |
| `LANGFUSE_TAGS` | No | Comma-separated tags for Langfuse traces (e.g. `qoder,production`). Default `qoder`. |
| `LANGFUSE_MAX_CHARS` | No | Maximum characters per content field. Default 800000 (~200K tokens). |
| `LANGFUSE_DEBUG` | No | Verbose logging to `~/.qoder/state/langfuse_hook.log`. Default `true`. Set `false` to disable. |

Credentials are stored in a dedicated file `~/.qoder/langfuse.env`. The shell profile only adds a single `source` line, making it cleanly removable on uninstall.

| Platform | Shell | GUI Apps |
|----------|-------|----------|
| macOS | `~/.zshenv` sources `~/.qoder/langfuse.env` | LaunchAgent (`~/Library/LaunchAgents/com.qoder.langfuse-env.plist`) |
| Linux | `~/.profile` sources `~/.qoder/langfuse.env` | Inherited from shell profile |

## Requirements

- [Qoder](https://qoder.com) (CLI, Desktop, or QoderWake)
- [uv](https://docs.astral.sh/uv/) — Python package manager (auto-initializes on first run)
- Python 3.8+

## How it works

### Hook trigger and data flow

The `Stop` hook fires at the end of each main agent turn. The `SubagentStop` hook fires when a subagent completes, capturing its separate transcript. It receives a JSON payload via stdin:

```json
{
  "session_id": "26d0203c-...",
  "transcript_path": "/Users/.../.qoder/projects/.../26d0203c-....jsonl",
  "cwd": "/Users/.../project",
  "hook_event_name": "Stop"
}
```

The hook incrementally reads the JSONL transcript and assembles a turn → generation → tool hierarchy, then sends it to Langfuse. State is persisted in `~/.qoder/state/langfuse_state.json` to ensure only new turns are emitted.

### Data sources: CLI vs Desktop

Qoder CLI and Desktop share the hooks configuration in `~/.qoder/settings.json`, but their data sources differ:

| | CLI | Desktop |
|---|---|---|
| Transcript | `~/.qoder/projects/.../<session_id>.jsonl` | `~/.qoder/projects/.../transcript/<session_id>.jsonl` |
| `message.model` | Available (e.g. `"lite"`) | Not available (`None`) |
| Token usage in transcript | Not available | Not available |
| Session exists in SQLite DB | No | Yes |

### Token & model enrichment (SQLite)

JSONL transcripts do not contain token usage data. The hook attempts to enrich traces by querying the Qoder Desktop SQLite database:

- **DB path**: `~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db` (macOS)
- **Table**: `chat_message`, filtered by `session_id` + `role = 'assistant'`
- **Fields**: `prompt_tokens` / `completion_tokens` / `cached_tokens` from `token_info`, `model_key` from `model_info`
- **Matching**: Approximate timestamp matching (5-second window); JSONL and DB timestamps typically differ by < 5ms

Effective coverage:

| | CLI sessions | Desktop sessions |
|---|---|---|
| Model | From JSONL `message.model` | Enriched from DB `model_key` |
| Token usage | Not available (no `message.usage` in transcript, no DB) | Enriched from DB `token_info` |
| Subagent capture | Via `SubagentStop` + `agent_transcript_path` | Not available (no `agent_transcript_path` in payload) |

### Known limitations

The following differences exist between Qoder CLI and Desktop/IDE. We have reported these to the Qoder team and are waiting for alignment in a future release.

**Qoder Desktop/IDE: no subagent internals**

Desktop/IDE fires `SubagentStop` hooks but the payload contains only `transcript_path` (the main agent's transcript), not `agent_transcript_path` (the subagent's own transcript). The hook cannot locate or process the subagent's internal data.

The main transcript records subagent invocations as `Agent` tool_use/tool_result pairs. The tool_result contains the subagent's **final text output only** (e.g. a 30K-char markdown report) — no structured tool call data. So the subagent appears in Langfuse as a single `Tool: Agent` span with its final output and duration, but its internal LLM calls, tool chain, and token consumption are not captured.

Qoder CLI (v1.0.10+) provides `agent_transcript_path` in the SubagentStop payload, enabling full subagent observability including all internal tool calls and token usage.

**No token usage in transcript (CLI and Desktop)**

Neither CLI nor Desktop transcript assistant messages include `message.usage`. Token data is only available for Desktop sessions, enriched from the SQLite database (`chat_message.token_info` / `model_info`) via approximate timestamp matching (5-second window). CLI sessions have no token data source — the CLI does not write to the SQLite DB. We have reported this to the Qoder team and are waiting for `message.usage` support in a future CLI release.

CLI transcript does include `message.model` (e.g. `"performance"`); Desktop does not (enriched from DB `model_key`).

**Qoder Desktop/IDE: content blocks split across rows without message.id**

Desktop transcript writes each content block (thinking, text, tool_use) as a separate JSONL row without a shared `message.id`. The hook merges consecutive assistant rows using a gap-detection heuristic (non-assistant rows like `progress` do not break the merge). This works reliably in practice but is inherently fragile compared to CLI's explicit `message.id` grouping.

**Model names are aliases — manual cost configuration required**

Both CLI and Desktop use internal model aliases (e.g. `"lite"`, `"performance"`, `"auto"`, `"dfmodel"`) rather than underlying model IDs. These aliases do not map transparently to actual models or pricing, so Langfuse cannot calculate costs automatically.

To track costs in Langfuse, manually add pricing for these aliases in your Langfuse project settings under **Models** (e.g. set input/output token prices for `lite`, `performance`, `dfmodel`).

### Summary of CLI vs Desktop differences

| Feature | CLI | Desktop | Waiting for Qoder team |
|---------|-----|---------|----------------------|
| `message.usage` in transcript | No | No | Add `message.usage` to both CLI and Desktop |
| `message.model` in transcript | Yes | No (DB fallback) | Add `message.model` to Desktop |
| Token usage | No data source | DB enrichment | Add `message.usage` to CLI transcript |
| `message.id` for row grouping | Yes | No (heuristic merge) | Add `message.id` to Desktop rows |
| `SubagentStop` with `agent_transcript_path` | Yes | No (only `transcript_path`) | Add `agent_transcript_path` to Desktop payload |
| Subagent final output in main transcript | Yes | Yes | — |

### settings.json hook entry

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.qoder/hooks/langfuse/langfuse-entrypoint.sh",
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
            "command": "~/.qoder/hooks/langfuse/langfuse-entrypoint.sh",
            "timeout": 20
          }
        ]
      }
    ]
  }
}
```

## Uninstall

```bash
cd agent-exporter-to-langfuse/hooks/qoder
bash uninstall.sh
```

The uninstall script removes:
- Hook script and uv environment (`~/.qoder/hooks/langfuse/`)
- Stop hook entry from `~/.qoder/settings.json`
- Environment file (`~/.qoder/langfuse.env`) and the source line from the shell profile
- LaunchAgent (`~/Library/LaunchAgents/com.qoder.langfuse-env.plist`) on macOS
- State and log files (`~/.qoder/state/langfuse_*`)

## Troubleshooting

- **Nothing in Langfuse**: check `~/.qoder/state/langfuse_hook.log` (set `LANGFUSE_DEBUG=true`).
- **Hook not firing**: verify the `Stop` hook entry exists in `~/.qoder/settings.json`; restart Qoder.
- **uv errors**: ensure [uv](https://docs.astral.sh/uv/) is installed and available on your PATH.

## License

MIT
