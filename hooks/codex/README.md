# Codex Trace Hook

Trace OpenAI Codex CLI sessions to Langfuse.

## Features

- **Automatic trace recording**: uploads full interaction traces after each Codex session
- **Three-tier delivery**: langstash → direct push → failed log, ensuring no data is lost
- **Incremental processing**: byte offset tracking, only processes new rollout data
- **Deduplication**: sidecar files track uploaded turn_ids to prevent duplicates
- **Direct hook integration**: registers Stop hook in `~/.codex/hooks.json`

## Data Model

Each Codex turn is parsed into:

- **Trace**: the entire turn (containing user input and assistant output)
- **Generations**: each model step (containing model, usage, reasoning, tool calls)
- **Spans**: each tool call (containing input, output, duration)

## Installation

### Option 1: Unified Installer (Recommended)

From the project root:

```bash
bash install.sh
```

Interactively select agents to install (including codex).

### Option 2: Standalone Install

```bash
bash hooks/codex/install.sh \
  --public-key pk-xxx \
  --secret-key sk-xxx \
  --base-url https://app.langfuse.com
```

The installer will:
1. Copy hook files to `~/.codex/hooks/langfuse/`
2. Copy `langstash_deliver` library
3. Initialize uv environment
4. Register Stop hook in `~/.codex/hooks.json`
5. Configure environment variables

## Uninstallation

### Option 1: Unified Uninstaller

```bash
bash uninstall.sh
```

### Option 2: Standalone Uninstall

```bash
bash hooks/codex/uninstall.sh
```

The uninstaller will:
1. Remove Stop hook from `~/.codex/hooks.json`
2. Delete `~/.codex/hooks/langfuse/` directory
3. Remove environment configuration from `~/.agent-exporter-to-langfuse/config/codex.env`

## Configuration

Credentials stored in `~/.agent-exporter-to-langfuse/config/codex.env`:

```bash
LANGFUSE_PUBLIC_KEY="pk-xxx"
LANGFUSE_SECRET_KEY="sk-xxx"
LANGFUSE_BASE_URL="https://app.langfuse.com"
LANGFUSE_USER_ID="your-user-id"  # optional
LANGFUSE_TAGS="codex,project-name"  # optional
```

## How It Works

1. **Hook trigger**: Codex invokes `langfuse-entrypoint.sh` on every `Stop` event
2. **Read rollout**: reads session data from `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
3. **State machine parsing**: parses JSONL lines into Turn → Step → ToolCall structures
4. **Build trace**: constructs Trace Schema v2 via `langstash_deliver`
5. **Three-tier delivery**:
   - First: langstash daemon (http://127.0.0.1:5288/ingest)
   - Fallback: direct push to Langfuse API
   - Last resort: write to failed log for retry
6. **Update state**: records byte offset and sidecar file for next incremental pass

## File Structure

```
hooks/codex/
├── hooks/
│   ├── langfuse_hook.py              # Main script (rollout parsing + delivery)
│   └── langfuse-entrypoint.sh        # Shell entrypoint
├── install.sh                        # Installation script
├── uninstall.sh                      # Uninstallation script
└── README.md                         # This document

After installation:
~/.codex/hooks/langfuse/
├── langfuse_hook.py                  # Copied from hooks/langfuse_hook.py
├── langfuse-entrypoint.sh            # Copied from hooks/langfuse-entrypoint.sh
├── langstash_deliver/                # Copied dependency library
└── .venv/                            # Python venv (created during install)
```

## Debugging

View logs:

```bash
tail -f ~/.codex/state/langfuse_hook.log
```

Enable verbose logging, add to `codex.env`:

```bash
LANGFUSE_DEBUG="true"
LANGSTASH_LOG_LEVEL="DEBUG"
```

## Limitations

- **No SubagentStop**: Codex sub-agents have independent rollout files, handled by their own Stop hooks
- **Sidecar dedup**: completed turns are not re-uploaded, but incomplete turns may be re-processed on the next hook invocation

## References

- Technical spec: `docs/2026-06-10-spec-codex-trace.md`
- Implementation plan: `docs/2026-06-10-plan-codex-trace.md`
- Codex Observability Plugin: `/Users/song/code/codex-observability-plugin`
