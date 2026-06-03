# Langfuse Observability Plugin for OpenCode

Trace every OpenCode session to [Langfuse](https://langfuse.com) — turns, generations, tool calls, and token usage — with zero code changes.

## Install

```bash
git clone https://github.com/aliyun/agent-exporter-to-langfuse.git
cd agent-exporter-to-langfuse/opencode
bash install.sh
```

The install script will interactively guide you through:
1. Entering Langfuse credentials (Base URL, Public Key, Secret Key)
2. Installing the `langfuse` npm package in `~/.config/opencode/`
3. Copying the plugin to `~/.config/opencode/plugins/langfuse-exporter.mjs`
4. Registering the plugin in `~/.config/opencode/opencode.json`
5. Persisting environment variables for both shell and GUI apps

If the plugin is already installed, the script will prompt before overwriting. Re-running the script updates the credentials automatically.

## Configuration

The install script configures the required environment variables. Full variable list:

| Variable | Required | Description |
|----------|----------|-------------|
| `LANGFUSE_SECRET_KEY` | Yes | Your Langfuse secret key (set during install). |
| `LANGFUSE_PUBLIC_KEY` | Yes | Your Langfuse public key (set during install). |
| `LANGFUSE_BASE_URL` | Yes | Your Langfuse host URL (set during install). |
| `OC_LANGFUSE_USER_ID` | No | User identifier for Langfuse traces. Defaults to OS username if not set. |
| `OC_LANGFUSE_TAGS` | No | Comma-separated tags for Langfuse traces (e.g. `opencode,production`). Default `opencode`. |
| `OC_LANGFUSE_MAX_CHARS` | No | Maximum characters per content field. Default 800000 (~200K tokens). |
| `OC_LANGFUSE_DEBUG` | No | Verbose logging to `~/.config/opencode/logs/langfuse-exporter/`. Default `true`. Set `false` to disable. |

Credentials are stored in a dedicated file `~/.config/opencode/langfuse.env`. The shell profile only adds a single `source` line, making it cleanly removable on uninstall.

| Platform | Shell | GUI Apps |
|----------|-------|----------|
| macOS | `~/.zshenv` sources `langfuse.env` | LaunchAgent (`~/Library/LaunchAgents/com.opencode.langfuse-env.plist`) |
| Linux | `~/.profile` sources `langfuse.env` | Inherited from shell profile |

## Requirements

- [OpenCode](https://opencode.ai) (CLI or TUI)
- [Node.js](https://nodejs.org/) 18+ (for npm and plugin runtime)

## How it works

### Plugin architecture

Unlike the other exporters in this repository (which use shell-hook scripts and read JSONL transcripts), OpenCode uses an **in-process JavaScript plugin** system. The plugin runs inside OpenCode's Node.js process and has direct access to:

- **Event stream** — real-time notification of session, message, and tool events
- **SDK Client API** — structured access to session messages, parts, and metadata
- **Langfuse JS SDK** — direct trace/generation/span creation with auto-batching

### Data flow

1. The `session.idle` event fires when OpenCode finishes processing a turn
2. The plugin calls `client.session.messages()` to fetch all messages for the session
3. Only new messages (since the last processing) are parsed into turns
4. Each user → assistant pair is emitted as a Langfuse trace with nested generation and tool spans
5. `langfuse.flushAsync()` sends the buffered data to Langfuse

### Langfuse mapping

```
OpenCode Session           → Langfuse session_id
User + Assistant message   → Langfuse Trace (name: "OpenCode - Turn N")
  └── Assistant response   → Langfuse Generation (model, tokens, cost)
      ├── Tool: edit       → Langfuse Span (input/output/timing)
      ├── Tool: bash       → Langfuse Span
      └── ...
```

### Data captured

| Data | Source | Notes |
|------|--------|-------|
| Model name | `AssistantMessage.providerID/modelID` | Full provider/model path (e.g. `anthropic/claude-sonnet-4-20250514`) |
| Token usage | `AssistantMessage.tokens` | input, output, reasoning, cache read/write |
| Cost | `AssistantMessage.cost` | Provider-reported cost |
| Tool calls | `ToolPart` with state | Tool name, input args, output, start/end time |
| User content | `UserMessage` parts | Text parts concatenated |
| Assistant content | `AssistantMessage` parts | Text parts concatenated (excluding synthetic/ignored) |

### opencode.json plugin entry

The install script adds the plugin reference to your `~/.config/opencode/opencode.json`:

```json
{
  "plugin": [
    "./plugins/langfuse-exporter.mjs"
  ]
}
```

## Known limitations

**In-memory state only**

The plugin tracks processed message counts in memory. If OpenCode restarts mid-session, already-emitted turns may be re-emitted on the next `session.idle` event (resulting in duplicate traces in Langfuse). This is harmless but may show duplicate entries.

**No subagent-level detail**

OpenCode's `general` subagent runs as a child session. The plugin captures the parent session's turns but does not currently follow child session events. Subagent work appears in the parent as an `Agent` tool call with the final result.

**Model cost depends on provider configuration**

Token costs are reported as-is from OpenCode's internal accounting. If your provider configuration has cost data, the values will be accurate. Otherwise, configure model pricing in Langfuse under **Models**.

## Uninstall

```bash
cd agent-exporter-to-langfuse/opencode
bash uninstall.sh
```

The uninstall script removes:
- Plugin reference from `~/.config/opencode/opencode.json`
- Plugin file (`~/.config/opencode/plugins/langfuse-exporter.mjs`)
- Environment file (`~/.config/opencode/langfuse.env`) and the source line from the shell profile
- LaunchAgent (`~/Library/LaunchAgents/com.opencode.langfuse-env.plist`) on macOS
- Log files (`~/.config/opencode/logs/langfuse-exporter/`)

## Troubleshooting

- **Nothing in Langfuse**: check logs at `~/.config/opencode/logs/langfuse-exporter/` (debug logging is on by default).
- **Plugin not loading**: verify `./plugins/langfuse-exporter.mjs` appears in `~/.config/opencode/opencode.json` `plugin` array; restart OpenCode.
- **npm errors**: ensure `langfuse` is installed in `~/.config/opencode/` (`cd ~/.config/opencode && npm ls langfuse`).

## License

MIT
