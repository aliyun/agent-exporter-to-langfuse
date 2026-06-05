# Langfuse Observability Hook for QoderWork

Trace every QoderWork session to [Langfuse](https://langfuse.com) — turns, generations, tool calls, and token usage — with zero code changes.

## Install

```bash
git clone https://github.com/aliyun/agent-exporter-to-langfuse.git
cd agent-exporter-to-langfuse/hooks/qoderwork
bash install.sh
```

The install script will interactively guide you through:
1. Entering Langfuse credentials (Base URL, Public Key, Secret Key)
2. Copying the hook script to `~/.qoderwork/hooks/langfuse/`
3. Initializing the uv environment and installing the langfuse SDK
4. Adding `Stop` and `SubagentStop` hook entries to `~/.qoderwork/settings.json`
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
| `LANGFUSE_TAGS` | No | Comma-separated tags for Langfuse traces (e.g. `qoderwork,production`). Default `qoderwork`. |
| `LANGFUSE_MAX_CHARS` | No | Maximum characters per content field. Default 800000 (~200K tokens). |
| `LANGFUSE_DEBUG` | No | Verbose logging to `~/.qoderwork/state/langfuse_hook.log`. Default `true`. Set `false` to disable. |

Credentials are stored in a dedicated file `~/.qoderwork/langfuse.env`. The shell profile only adds a single `source` line, making it cleanly removable on uninstall.

| Platform | Shell | GUI Apps |
|----------|-------|----------|
| macOS | `~/.zshenv` sources `~/.qoderwork/langfuse.env` | LaunchAgent (`~/Library/LaunchAgents/com.qoderwork.langfuse-env.plist`) |
| Linux | `~/.profile` sources `~/.qoderwork/langfuse.env` | Inherited from shell profile |

## Requirements

- [QoderWork](https://qoder.com/qoderwork) (CLI or Desktop)
- [uv](https://docs.astral.sh/uv/) — Python package manager (auto-initializes on first run)
- Python 3.8+

## How it works

### Hook trigger and data flow

The `Stop` hook fires at the end of each main agent turn. The `SubagentStop` hook fires when a subagent completes, capturing its separate transcript. It receives a JSON payload via stdin:

```json
{
  "session_id": "26d0203c-...",
  "transcript_path": "/Users/.../.qoderwork/projects/.../26d0203c-....jsonl",
  "cwd": "/Users/.../project",
  "hook_event_name": "Stop"
}
```

The hook incrementally reads the JSONL transcript and assembles a turn → generation → tool hierarchy, then sends it to Langfuse. State is persisted in `~/.qoderwork/state/langfuse_state.json` to ensure only new turns are emitted.

### Data sources: CLI vs Desktop

QoderWork CLI and Desktop share the hooks configuration in `~/.qoderwork/settings.json`, but their data sources differ:

| | CLI | Desktop |
|---|---|---|
| Transcript | `~/.qoderwork/projects/.../<session_id>.jsonl` | `~/.qoderwork/projects/.../transcript/<session_id>.jsonl` |
| `message.model` | Available (e.g. `"lite"`) | Not available (`None`) |
| Token usage in transcript | Not available | Not available |
| Session exists in SQLite DB | No | Yes |

### Token usage

QoderWork does not currently provide token usage data:

- **Transcript**: No `message.usage` field in assistant messages
- **SQLite DB**: Not accessible (DB resides on macOS host, outside the VM's filesystem mount)

Token usage will be captured automatically once the QoderWork team adds `message.usage` to the transcript format. The hook code already supports reading `message.usage` when it becomes available.

### Linux VM architecture

QoderWork Desktop executes all hook commands inside a Linux VM (Ubuntu aarch64, via Apple Virtualization framework / `hvkit`). This introduces several differences compared to native macOS execution:

| Aspect | macOS (native) | Linux VM (QoderWork) |
|--------|---------------|---------------------|
| `$HOME` | `/Users/<user>` | `/root` |
| Shell | `/bin/zsh` | `/usr/bin/bash` |
| Python venv | macOS arm64 binaries | Linux aarch64 binaries |
| Environment variables | From shell profile / LaunchAgent | Must be sourced from `langfuse.env` by entrypoint |

The `langfuse-entrypoint.sh` wrapper script handles these differences:

1. **Sources `~/.qoderwork/langfuse.env`** to inject Langfuse credentials into the VM environment (shell profile and LaunchAgent settings do not propagate into the VM)
2. **Auto-rebuilds the Python venv** on first run — the macOS `.venv` created by `install.sh` contains Mach-O binaries that cannot execute on Linux; the entrypoint detects this and recreates the venv using the VM's system `python3`
3. **Uses `~` paths in `settings.json`** — absolute macOS paths (`/Users/...`) do not exist inside the VM; tilde expands to `$HOME` at runtime on both platforms

### Known limitations

**QoderWork Desktop: no subagent internals**

Desktop fires `SubagentStop` hooks but the payload contains only `transcript_path` (the main agent's transcript), not `agent_transcript_path` (the subagent's own transcript). The hook cannot locate or process the subagent's internal data.

The main transcript records subagent invocations as `Agent` tool_use/tool_result pairs. The tool_result contains the subagent's **final text output only** — no structured tool call data. So the subagent appears in Langfuse as a single `Tool: Agent` span with its final output and duration, but its internal LLM calls, tool chain, and token consumption are not captured.

**QoderWork: no token usage in transcript**

Transcript assistant messages do not include `message.usage` or `message.model`. Token usage is not captured. We are waiting for the QoderWork team to add `message.usage` support.

**QoderWork Desktop: content blocks split across rows without message.id**

Desktop transcript writes each content block (thinking, text, tool_use) as a separate JSONL row without a shared `message.id`. The hook merges consecutive assistant rows using a gap-detection heuristic. This works reliably in practice but is inherently fragile compared to explicit `message.id` grouping.

**Model names are aliases — manual cost configuration required**

QoderWork uses internal model aliases (e.g. `"lite"`, `"performance"`, `"auto"`, `"dfmodel"`) rather than underlying model IDs. These aliases do not map transparently to actual models or pricing, so Langfuse cannot calculate costs automatically.

To track costs in Langfuse, manually add pricing for these aliases in your Langfuse project settings under **Models**.

### settings.json hook entry

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.qoderwork/hooks/langfuse/langfuse-entrypoint.sh",
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
            "command": "~/.qoderwork/hooks/langfuse/langfuse-entrypoint.sh",
            "timeout": 20
          }
        ]
      }
    ]
  }
}
```

QoderWork executes hook commands inside a Linux VM. A wrapper script (`langfuse-entrypoint.sh`) is used instead of a compound `cd && uv run` command to ensure proper PATH resolution and working directory setup within the VM environment.

## Uninstall

```bash
cd agent-exporter-to-langfuse/hooks/qoderwork
bash uninstall.sh
```

The uninstall script removes:
- Hook script and uv environment (`~/.qoderwork/hooks/langfuse/`)
- Stop hook entry from `~/.qoderwork/settings.json`
- Environment file (`~/.qoderwork/langfuse.env`) and the source line from the shell profile
- LaunchAgent (`~/Library/LaunchAgents/com.qoderwork.langfuse-env.plist`) on macOS
- State and log files (`~/.qoderwork/state/langfuse_*`)

## Troubleshooting

- **Nothing in Langfuse**: check `~/.qoderwork/state/langfuse_hook.log` (set `LANGFUSE_DEBUG=true`).
- **Hook not firing**: verify the `Stop` hook entry exists in `~/.qoderwork/settings.json`; restart QoderWork.
- **uv errors**: ensure [uv](https://docs.astral.sh/uv/) is installed and available on your PATH.

## License

MIT
