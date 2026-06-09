# Agent Exporter to Langfuse

Export AI Agent session observability data (conversation turns, model calls, tool usage, token consumption) to [Langfuse](https://langfuse.com) with zero code changes.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      AI Agents                          │
│  Claude Code · Qoder · QoderWork · OpenCode · ...       │
└────────┬────────────────────────────────────────────────┘
         │ Plugin Hook (per-agent)
         ▼
┌─────────────────────────────────────────────────────────┐
│                   langstash-deliver                     │
│  Three-tier delivery:                                   │
│    1. langstash (local buffer) ─► preferred             │
│    2. Langfuse SDK (direct push) ─► fallback            │
│    3. Failed log (~/.agent-exporter-to-langfuse/data/)  │
└────────┬──────────────────────┬─────────────────────────┘
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

See the README in each directory for detailed configuration and usage instructions.

## Install

> **Users in mainland China**: if PyPI downloads time out, set a mirror and timeout before installing:
>
> ```bash
> export UV_HTTP_TIMEOUT=1200
> export UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
> ```

One-line install (requires `git`):

```bash
curl -fsSL https://raw.githubusercontent.com/aliyun/agent-exporter-to-langfuse/main/install-remote.sh | bash -s -- \
  --public-key pk-lf-*** \
  --secret-key sk-lf-*** \
  --base-url http://LANGFUSE_HOST:LANGFUSE_PORT \
  --user-id USER_NAME \
  --tags TAGS
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `--public-key` | Yes | Langfuse public key (`pk-lf-...`) |
| `--secret-key` | Yes | Langfuse secret key (`sk-lf-...`) |
| `--base-url` | Yes | Langfuse server URL |
| `--user-id` | No | User identifier for traces. Defaults to OS username. |
| `--tags` | No | Extra tags (comma-separated, e.g. `team:olap,env:prod`). Agent name is always included automatically. |

## Uninstall

```bash
bash ~/.agent-exporter-to-langfuse/uninstall.sh
```

The uninstaller detects which agents have Langfuse hooks installed and lets you select which to remove. Use `-y` to remove all without prompting:

```bash
bash ~/.agent-exporter-to-langfuse/uninstall.sh -y
```

## Langfuse Backend

This project works with the open-source [Langfuse](https://langfuse.com) as well as [Alibaba Cloud Agent-Lens](https://help.aliyun.com/clickhouse/user-guide/agent-lens-overview), which is fully compatible with the Langfuse API.

## Platform Support

Currently supports Unix-like systems only: **macOS** and **Linux** distributions. Windows support is in progress.

## License

MIT

## Contact

Scan the QR code to join the DingTalk discussion group:

<img src="https://ck-langfuse-public.oss-cn-beijing.aliyuncs.com/agent-exporter-to-langfuse/dingtalk-qr-code.JPG" alt="QR Code" width="250" />

**DingTalk Group**: 180485008966
