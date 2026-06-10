# Implementation Plan: Codex Trace 投递

## 实现清单

### Phase 1: 核心 Hook 实现

| # | 文件 | 描述 |
|---|------|------|
| 1 | `hooks/codex/hooks/langfuse_hook.py` | 主 hook 脚本 — rollout 解析 + 三层投递 |
| 2 | `hooks/codex/hooks/langfuse-entrypoint.sh` | Shell 入口 — source env + uv run |
| 3 | `hooks/codex/hooks/hooks.json` | Codex hook 注册 (Stop 事件) |
| 4 | `hooks/codex/.codex-plugin/plugin.json` | Codex plugin 元数据 |

### Phase 2: 安装器

| # | 文件 | 描述 |
|---|------|------|
| 5 | `hooks/codex/install.sh` | Codex 专用安装器 |
| 6 | `install.sh` (修改) | 统一安装器加入 codex 检测和安装 |
| 7 | `uninstall.sh` (修改) | 统一卸载器加入 codex 清理 |

### Phase 3: 文档

| # | 文件 | 描述 |
|---|------|------|
| 8 | `hooks/codex/README.md` | Codex hook 文档 |

## 关键实现细节

### 1. langfuse_hook.py 核心流程

```
stdin JSON → session_id + transcript_path
           → 增量读取 JSONL (byte offset state)
           → 状态机组装 Turn/Step/ToolCall
           → 构建 Trace Schema v2
           → deliver_trace() 三层投递
           → 更新 state + sidecar 去重
```

### 2. 文件安装路径

```
~/.codex/plugins/cache/agent-exporter-to-langfuse/langfuse/<version>/
├── .codex-plugin/plugin.json
├── hooks/
│   ├── hooks.json
│   ├── langfuse_hook.py
│   └── langfuse-entrypoint.sh
└── pyproject.toml (uv init 生成)
```

### 3. hooks.json command 路径

```bash
bash "${CODEX_HOME:-$HOME/.codex}/plugins/cache/agent-exporter-to-langfuse/langfuse/<version>/hooks/langfuse-entrypoint.sh"
```

### 4. Rollout 解析状态机

按 codex-observability-plugin 的 parse.ts 逻辑移植到 Python:
- `session_meta` → session 级元数据
- `turn_context` → 当前 turn 的 model
- `response_item.message` → userInput / step.text
- `response_item.function_call` → ToolCall 创建
- `response_item.function_call_output` → ToolCall 匹配
- `response_item.reasoning` → step.reasoning
- `event_msg.task_started` → 开新 turn
- `event_msg.token_count` → 关 step + usage
- `event_msg.task_complete` / `turn_aborted` → 关 turn
- `event_msg.*_end` + call_id → ToolCall 完成

### 5. 去重机制

- Sidecar 文件: `<rollout_path>.langfuse`
- 每行一个已上传的 turn_id
- 已完成的 turn 且 ID 在 sidecar 中 → 跳过
- 未完成的 trailing turn → 允许重处理

### 6. install.sh 修改点

1. `detect_codex()`: 检查 `~/.codex` 或 `codex` 命令
2. `install_codex()` 函数
3. agent 列表 case 中加入 `codex`
4. `--agents` 帮助文本加入 codex

### 7. uninstall.sh 修改点

1. 检查 `~/.codex/plugins/cache/agent-exporter-to-langfuse/` 是否存在
2. 删除 plugin cache 目录
3. 删除 env 文件
