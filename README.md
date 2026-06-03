# Agent Exporter to Langfuse

Export AI Agent session observability data (conversation turns, model calls, tool usage, token consumption) to [Langfuse](https://langfuse.com) with zero code changes.

## Supported Agents

| Agent | Directory | Description |
|-------|-----------|-------------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | [`claude-code/`](./claude-code/) | Collect Claude Code session data via Plugin Hook |
| [Qoder](https://qoder.com) | [`qoder/`](./qoder/) | Collect Qoder (CLI / Desktop / QoderWake) session data via Plugin Hook |
| [QoderWork](https://qoder.com/qoderwork) | [`qoderwork/`](./qoderwork/) | Collect QoderWork session data via Plugin Hook |
| [OpenCode](https://opencode.ai) | [`opencode/`](./opencode/) | Collect OpenCode session data via Plugin Hook |

See the README in each directory for installation, configuration, and usage instructions.

## Langfuse Backend

This project works with the open-source [Langfuse](https://langfuse.com) as well as [Alibaba Cloud Agent-Lens](https://help.aliyun.com/clickhouse/user-guide/agent-lens-overview), which is fully compatible with the Langfuse API.

## Platform Support

Currently supports Unix-like systems only: **macOS** and **Linux** distributions. Windows support is in progress.

## License

MIT
