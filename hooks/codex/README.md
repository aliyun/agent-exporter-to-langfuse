# Codex Trace Hook

Trace OpenAI Codex CLI sessions to Langfuse.

## Features

- **Automatic trace recording**: uploads full interaction traces after each Codex session
- **Three-tier delivery**: langstash → OTel direct push → failed log
- **Dual-field user input**: correctly separates real user messages from system-injected context
- **Deduplication**: sidecar files track uploaded turn_ids to prevent duplicates
- **Subagent support**: recursively traces subagent threads nested under parent turns

## Data Model

Each Codex turn is parsed into:

- **Agent span** (trace root): the entire turn with user input and final output
- **Generations**: each model step (with model name, usage, reasoning, tool calls)
- **Tool spans**: each tool invocation (with input, output, duration, errors)

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
1. Install pnpm (if not present, via corepack or npm)
2. Build `dist/index.mjs` from source via `pnpm install && pnpm run build`
3. Copy built artifact to `~/.codex/hooks/langfuse/`
4. Register Stop hook in `~/.codex/hooks.json`
5. Configure environment variables

**Prerequisites**: Node.js >= 22, Codex CLI installed. pnpm is auto-installed if missing.

## Uninstallation

### Option 1: Unified Uninstaller

```bash
bash uninstall.sh
```

### Option 2: Standalone Uninstall

```bash
bash hooks/codex/uninstall.sh
```

## Configuration

Credentials stored in `~/.agent-exporter-to-langfuse/config/codex.env`:

```bash
LANGFUSE_PUBLIC_KEY="pk-xxx"
LANGFUSE_SECRET_KEY="sk-xxx"
LANGFUSE_BASE_URL="https://app.langfuse.com"
LANGFUSE_USER_ID="your-user-id"      # optional
LANGFUSE_TAGS="codex,project-name"   # optional, default: codex
LANGFUSE_MAX_CHARS="800000"          # optional, max characters per content field (~200K tokens)
LANGFUSE_DEBUG="true"                # optional, verbose logging. Set "false" to disable
LANGSTASH_ENABLED="true"             # enable langstash delivery
LANGSTASH_URL="http://127.0.0.1:5288"
```

## How It Works

1. **Hook trigger**: Codex invokes the hook on every `Stop` event
2. **Read rollout**: reads session data from `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
3. **Parse**: reconstructs turns with dual-field user input strategy (event_msg > response_item fallback)
4. **Deliver**: langstash HTTP POST → OTel+Langfuse direct push → failed log
5. **Dedup**: sidecar file tracks completed turn_ids

## Development

```bash
# Install dependencies
pnpm install

# Build bundled output
pnpm run build

# Type check
pnpm run lint:tsc
```

The build produces `dist/index.mjs` — a self-contained ESM bundle with all dependencies inlined. The `dist/` directory is gitignored; it is built on the user's machine during `install.sh`.

## File Structure

```
hooks/codex/
├── src/                    # TypeScript source
│   ├── index.ts            # Entry point
│   ├── parse.ts            # Rollout JSONL parser
│   ├── trace.ts            # Trace builder + delivery orchestration
│   ├── langstash.ts        # Langstash HTTP delivery + trace_v2 builder
│   ├── sidecar.ts          # Turn dedup via sidecar files
│   ├── config.ts           # Environment-based configuration
│   ├── instrumentation.ts  # OTel provider setup
│   ├── types.ts            # Codex rollout type definitions
│   └── utils.ts            # Utilities (stdin, truncate, debug)
├── dist/                   # Build output (gitignored, built at install time)
│   └── index.mjs
├── hooks/
│   └── hooks.json          # Codex hook registration template
├── tsdown.config.ts        # Bundle configuration
├── package.json
├── install.sh
├── uninstall.sh
└── README.md
```

## Limitations

- Subagent and main agent session IDs cannot be correlated. Codex rollout files do not contain a mapping between subagent threads and the parent session, so they appear as independent sessions in Langfuse. Only in OTel direct-push mode are subagent traces nested as child spans under the parent turn.

## References

- Based on [codex-observability-plugin](https://github.com/langfuse/codex-observability-plugin)
