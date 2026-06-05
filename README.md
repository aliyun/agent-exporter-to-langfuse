# Agent Exporter to Langfuse

Export AI Agent session observability data (conversation turns, model calls, tool usage, token consumption) to [Langfuse](https://langfuse.com) with zero code changes.

## Supported Agents

| Agent | Directory | Description |
|-------|-----------|-------------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | [`hooks/claude-code/`](./hooks/claude-code/) | Collect Claude Code session data via Plugin Hook |
| [Qoder](https://qoder.com) | [`hooks/qoder/`](./hooks/qoder/) | Collect Qoder (CLI / Desktop / QoderWake) session data via Plugin Hook |
| [QoderWork](https://qoder.com/qoderwork) | [`hooks/qoderwork/`](./hooks/qoderwork/) | Collect QoderWork session data via Plugin Hook |
| [OpenCode](https://opencode.ai) | [`hooks/opencode/`](./hooks/opencode/) | Collect OpenCode session data via Plugin Hook |

See the README in each directory for detailed configuration and usage instructions.

## Install

The unified installer auto-detects installed agents and configures them all at once.

```bash
git clone https://github.com/aliyun/agent-exporter-to-langfuse.git
cd agent-exporter-to-langfuse
bash install.sh
```

Or pass all parameters for non-interactive installation:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-*** \
LANGFUSE_SECRET_KEY=sk-lf-*** \
LANGFUSE_BASE_URL=http://LANGFUSE_HOST:LANGFUSE_PORT \
LANGFUSE_USER_ID=USER_ID \
LANGFUSE_TAGS=team:clickhouse,env:personal \
bash install.sh
```

You can also select specific agents:

```bash
bash install.sh --agents claude-code,qoder
```

### Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `LANGFUSE_PUBLIC_KEY` | Yes | Your Langfuse public key (pk-lf-...) |
| `LANGFUSE_SECRET_KEY` | Yes | Your Langfuse secret key (sk-lf-...) |
| `LANGFUSE_BASE_URL` | Yes | Langfuse host URL |
| `LANGFUSE_USER_ID` | No | User identifier for traces. Defaults to OS username. |
| `LANGFUSE_TAGS` | No | Extra tags (comma-separated). Agent name is always included automatically. |

## Uninstall

```bash
bash uninstall.sh
```

The uninstaller detects which agents have Langfuse hooks installed and lets you select which to remove. Use `-y` to remove all without prompting:

```bash
bash uninstall.sh -y
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
