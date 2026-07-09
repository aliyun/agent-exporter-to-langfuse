# Cursor Hook

Collect Cursor IDE Agent session data via Cursor Hooks and deliver to Langfuse.

## Prerequisites

- [Cursor IDE](https://cursor.com) installed
- Node.js >= 22
- Langfuse server or langstash daemon running

## Install

```bash
bash hooks/cursor/install.sh \
  --secret-key sk-lf-*** \
  --public-key pk-lf-*** \
  --base-url https://your-langfuse-host \
  --user-id YOUR_USER_ID \
  --tags "team:my-team,env:prod"
```

This registers hooks for 11 events (9 Agent events + `stop` + `sessionStart`) in `~/.cursor/hooks.json` and copies the hook bundle to `~/.cursor/hooks/langfuse/dist/`.

## Uninstall

```bash
bash hooks/cursor/uninstall.sh --purge
```

## Architecture

- **Event hooks**: Append JSONL records to per-conversation state file
- **Stop hook**: Read state file, split into turns, build per-turn OTLP JSON, deliver via `langstash-deliver`
- **Subagent hooks**: Deliver standalone traces immediately on `subagentStop`
- **SessionStart hook**: Recover orphaned state files (>6h old)
- **Fail-open**: All hooks return exit code 0 with valid JSON on any error
