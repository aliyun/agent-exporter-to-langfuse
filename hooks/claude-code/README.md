# Langfuse Observability Plugin for Claude Code

Trace every Claude Code session to [Langfuse](https://langfuse.com) — turns, generations, tool calls, and token usage — with zero code changes.

## Install

```bash
# 1. Clone the repository
git clone https://github.com/aliyun/agent-exporter-to-langfuse.git

# 2. Add as a custom marketplace (path to the claude-code directory)
claude plugin marketplace add /path/to/agent-exporter-to-langfuse/claude-code

# 3. Install the plugin
claude plugin install langfuse@alibabacloud-database

# 4. Restart Claude Code
```

Configure via `/plugin` → Configure options:

| Field | Required | Description |
|-------|----------|-------------|
| `LANGFUSE_SECRET_KEY` | Yes | Your Langfuse secret key (sk-lf-...). Stored in your OS keychain. |
| `LANGFUSE_PUBLIC_KEY` | Yes | Your Langfuse public key (pk-lf-...). |
| `LANGFUSE_BASE_URL` | Yes | `https://us.cloud.langfuse.com` (default), `https://cloud.langfuse.com` for EU, or your self-hosted URL. |
| `LANGFUSE_USER_ID` | No | User identifier for Langfuse traces. Defaults to OS account username if not set. |
| `LANGFUSE_TAGS` | No | Comma-separated tags for Langfuse traces (e.g. `claude-code,production`). Default `claude-code`. |
| `LANGFUSE_MAX_CHARS` | No | Maximum characters per content field. Default 800000 (≈200K tokens). |
| `LANGFUSE_DEBUG` | No | Verbose logging to `~/.claude/state/langfuse_hook.log`. Default `true`. Set `false` to disable. |

Get keys from your Langfuse project settings → API Keys.

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (CLI or Desktop)
- [uv](https://docs.astral.sh/uv/) — Python package manager (auto-initializes on first run, installs Python and langfuse SDK automatically)

If `uv` is not initialized in the plugin hooks directory, the first run will automatically execute `uv init && uv add langfuse` to set up the environment.

## How it works

A Stop hook reads the session transcript incrementally on every turn and emits a Langfuse trace with one span per turn, nested generations per assistant message, and child tool spans for every tool call. Token usage is captured when present.

State is kept in `~/.claude/state/langfuse_state.json` so re-runs only emit new turns.

## Reconfigure

```bash
claude plugin disable langfuse
claude plugin enable langfuse
```

## Uninstall

```bash
claude plugin uninstall langfuse
```

## Troubleshooting

- **Nothing in Langfuse**: check `~/.claude/state/langfuse_hook.log` (enable `LANGFUSE_DEBUG`).
- **Hook not firing**: confirm with `claude plugin list` that langfuse is enabled; restart Claude Code.
- **uv errors**: ensure [uv](https://docs.astral.sh/uv/) is installed and available on your PATH.

## License

MIT
