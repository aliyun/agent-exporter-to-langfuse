# Agent Exporter to Langfuse

Export AI Agent session observability data (conversation turns, model calls, tool usage, token consumption) to [Langfuse](https://langfuse.com) with zero code changes.

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                         AI Agents                             │
│  Claude Code · Qoder · QoderWork · OpenCode · Codex · Cursor  │
└────────┬──────────────────────────────────────────────────────┘
         │ Plugin Hook (per-agent)
         ▼
┌───────────────────────────────────────────────────────────────┐
│                      langstash-deliver                        │
│  Three-tier delivery:                                         │
│    1. langstash (local buffer) ─► preferred                   │
│    2. Langfuse SDK (direct push) ─► fallback                  │
│    3. Failed log (~/.agent-exporter-to-langfuse/data/)        │
└────────┬────────────────────────┬─────────────────────────────┘
         │ POST /ingest         │ Direct SDK push
         ▼                      ▼
┌──────────────────┐   ┌──────────────────┐
│    langstash     │   │                  │
│  Local buffer &  │──►│     Langfuse     │
│  batch sender    │   │                  │
│  (macOS menubar) │   │                  │
└──────────────────┘   └──────────────────┘
```

- **hooks/** — per-agent plugin hooks that capture session data and hand off to `langstash-deliver`
- **langstash-deliver** — shared delivery library with three-tier fallback (langstash → direct push → local log)
- **langstash** (`exporter/`) — local HTTP buffer daemon that accepts traces, batches them, and reliably delivers to Langfuse; includes a macOS menubar app and web dashboard

## Supported Agents

| Agent | Directory | Description |
|-------|-----------|-------------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | [`hooks/claude-code/`](./hooks/claude-code/) | Collect Claude Code session data via Plugin Hook |
| [Qoder](https://qoder.com) | [`hooks/qoder/`](./hooks/qoder/) | Collect Qoder (CLI / Desktop / QoderWake) session data via Plugin Hook |
| [QoderWork](https://qoder.com/qoderwork) | [`hooks/qoderwork/`](./hooks/qoderwork/) | Collect QoderWork session data via Plugin Hook |
| [OpenCode](https://opencode.ai) | [`hooks/opencode/`](./hooks/opencode/) | Collect OpenCode session data via Plugin Hook |
| [Codex](https://developers.openai.com/codex) | [`hooks/codex/`](./hooks/codex/) | Collect OpenAI Codex CLI session data via Plugin Hook |
| [Cursor](https://cursor.com) | [`hooks/cursor/`](./hooks/cursor/) | Collect Cursor IDE Agent session data via Hooks |

See the README in each directory for detailed configuration and usage instructions.

## Install

> **Users in mainland China**: if PyPI downloads time out, set a mirror and timeout before installing:
>
> ```bash
> export UV_HTTP_TIMEOUT=1200
> export UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
> ```

### Install from GitHub Releases

```bash
curl -fsSL https://raw.githubusercontent.com/aliyun/agent-exporter-to-langfuse/main/deploy/installer.sh | bash -s -- install \
  --secret-key sk-lf-*** \
  --public-key pk-lf-*** \
  --base-url http://LANGFUSE_HOST:LANGFUSE_PORT \
  --user-id YOUR_USER_ID \
  --tags "team:my-team,env:prod"
```

### Install from local package

Build the package first:

```bash
git clone https://github.com/aliyun/agent-exporter-to-langfuse.git
cd agent-exporter-to-langfuse
bash deploy/package.sh --output-dir /tmp/pkg
```

Then install:

```bash
bash deploy/installer.sh install \
  --package-url "file:///tmp/pkg/agent-exporter-to-langfuse-0.1.0.tar.gz" \
  --secret-key sk-lf-*** \
  --public-key pk-lf-*** \
  --base-url http://LANGFUSE_HOST:LANGFUSE_PORT \
  --user-id YOUR_USER_ID \
  --tags "team:my-team,env:prod"
```

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `--public-key` | Yes | Langfuse public key (`pk-lf-...`) |
| `--secret-key` | Yes | Langfuse secret key (`sk-lf-...`) |
| `--base-url` | Yes | Langfuse server URL |
| `--user-id` | No | User identifier for traces. Defaults to OS username. |
| `--tags` | No | Extra tags (comma-separated, e.g. `team:olap,env:prod`). Agent name is always included automatically. |
| `--version` | No | Install a specific version (default: latest stable) |
| `--package-url` | No | Use a custom package URL (supports `file://` for local/offline install) |

All parameters can also be passed as environment variables: `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_BASE_URL`, `LANGFUSE_USER_ID`, `LANGFUSE_TAGS`.

## Upgrade

```bash
langstash upgrade --version 0.2.0
```

Or trigger from the web dashboard (`http://127.0.0.1:5288`).

## Rollback

```bash
langstash rollback
```

Swaps back to the previous version and restarts the service.

## Uninstall

```bash
langstash uninstall          # keep config/data/logs
langstash uninstall --purge  # remove everything
```

## Cursor

See [`hooks/cursor/README.md`](./hooks/cursor/README.md) for installation and configuration instructions.

## CLI Commands

After installation, `langstash` is available in your PATH (`~/.local/bin/langstash`):

| Command | Description |
|---------|-------------|
| `langstash run` | Start the server in foreground (default) |
| `langstash start` | Start the background service |
| `langstash stop` | Stop the background service |
| `langstash restart` | Restart the background service |
| `langstash status` | Show version and health status |
| `langstash upgrade` | Upgrade to a new version |
| `langstash rollback` | Rollback to the previous version |
| `langstash uninstall` | Uninstall (with optional `--purge`) |

## Directory Layout

```
~/.agent-exporter-to-langfuse/
├── current              ← active version pointer (e.g. "0.3.0")
├── previous             ← rollback version pointer
├── hook-state.json      ← per-agent hook status tracking
├── versions/
│   └── 0.3.0/           ← versioned package
│       ├── exporter/
│       ├── hooks/
│       └── deploy/
├── config/              ← shared config (survives upgrades)
├── data/                ← shared data (survives upgrades)
└── logs/                ← shared logs (survives upgrades)
```

## Web Dashboard

Access the built-in dashboard at `http://127.0.0.1:5288` to view:

- Trace counts, token usage, and delivery status
- Hook installation status per agent (with retry/install buttons)
- Version info and upgrade controls
- Pre-release update toggle

## Langfuse Backend

This project works with the open-source [Langfuse](https://langfuse.com) as well as [Alibaba Cloud Agent-Lens](https://help.aliyun.com/clickhouse/user-guide/agent-lens-overview), which is fully compatible with the Langfuse API.

## Development

Run all tests:

```bash
bash scripts/run-tests.sh
```

Build a release package:

```bash
bash deploy/package.sh --output-dir dist
```

## Platform Support

Currently supports Unix-like systems only: **macOS** and **Linux** distributions. Windows support is in progress.

## License

MIT

## Contact

Scan the QR code to join the DingTalk discussion group:

<img src="https://ck-langfuse-public.oss-cn-beijing.aliyuncs.com/agent-exporter-to-langfuse/dingtalk-qr-code.JPG" alt="QR Code" width="250" />

**DingTalk Group**: 180485008966
