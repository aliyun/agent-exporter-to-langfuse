# Spec: Codex Trace 投递

## 1. 目标

在 agent-exporter-to-langfuse 项目中新增 Codex agent 支持，将 Codex CLI 的会话 trace 投递到 Langfuse，与现有 Claude Code / Qoder / QoderWork / OpenCode 四个 agent 对齐，复用共享的三层投递架构（langstash → direct push → failed log）。

## 2. 参考实现分析

codex-observability-plugin（TypeScript）的核心机制：

| 环节 | 做法 |
|------|------|
| Hook 触发 | Codex 的 `Stop` 事件 hook，stdin 接收 `{session_id, turn_id, transcript_path, hook_event_name}` |
| 数据来源 | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<threadId>.jsonl`，每行 `{timestamp, type, payload}` |
| 行类型 | `session_meta`（会话元数据）、`turn_context`（模型+参数）、`response_item`（模型 I/O）、`event_msg`（生命周期事件） |
| Turn 组装 | 状态机：`task_started` 开 turn → `response_item` 填充 step/tool → `token_count` 关 step → `task_complete`/`turn_aborted` 关 turn |
| 去重 | sidecar 文件 `<rollout>.langfuse` 记录已上传的 turn_id |
| 子 agent | `collab_agent_spawn_end` 事件记录子线程 ID，递归查找子 rollout 文件并嵌套 |
| 投递 | OTel + LangfuseSpanProcessor，batched 模式 |

## 3. 架构决策

### 3.1 语言：Python

**选择 Python**，理由：
- 项目其余四个 hook（claude-code, qoder, qoderwork, opencode 除外）均用 Python
- 共享投递层 `langstash-deliver` 是 Python 库
- Trace Schema v2 builder 已在 Python 中实现
- Codex rollout JSONL 解析用 Python 实现没有技术障碍

### 3.2 读取策略：增量 vs 全量

**选择增量读取**，与 claude-code/qoder hook 保持一致：
- 维护 byte offset，每次只读新增字节
- 避免大 session 重复处理
- state 文件存放在 `~/.codex/state/langfuse_state.json`

### 3.3 去重策略

参考 codex-observability-plugin 的 sidecar 方案：
- 在 rollout 文件旁创建 `<rollout>.langfuse` 文件，记录已完成的 turn_id
- 已完成的 turn 不重复投递；未完成的 trailing turn 允许重新处理

### 3.4 Hook 注册方式

使用 Codex 原生 plugin 机制：
- `.codex-plugin/plugin.json` — plugin 元数据
- `hooks/hooks.json` — 注册 `Stop` 事件 hook

## 4. 数据模型映射

### 4.1 Codex Rollout → Trace Schema v2

| Codex 概念 | Trace Schema v2 字段 | 说明 |
|------------|---------------------|------|
| session (thread) | `session_id` | Codex thread ID，作为 Langfuse session |
| Turn (task_started → task_complete) | `trace` | 每个 Turn 生成一个 trace |
| ModelStep (两次 token_count 之间) | `generations[]` | 每个 step 一个 generation |
| ToolCall (function_call/custom_tool_call) | `spans[]` | 每个 tool call 一个 span |
| reasoning text | generation `output.reasoning` | 附在 generation output 中 |

### 4.2 字段映射细节

**Trace:**
```
name: "Codex - Turn {N}"
start_time: turn.startTime (ISO 8601)
end_time: turn.endTime
input: {role: "user", content: turn.userInput}
output: {role: "assistant", content: turn.finalOutput}
metadata: {source: "codex", turn_number, turn_id, model, model_provider, cli_version, aborted, tool_call_count}
```

**Generation (per ModelStep):**
```
name: "Codex Generation {idx+1}"
model: turn.model
start_time: step.startTime
end_time: step.endTime
input: 第一个 step → user input; 后续 → 上一步 tool results
output: {content: step.text, reasoning: step.reasoning, tool_calls: [...]}
usage: {input: input_tokens, output: output_tokens, cache_read_input_tokens: cached_input_tokens}
metadata: {step_index}
```

**Span (per ToolCall):**
```
name: "Tool: {tc.name}"
generation_index: 所属 step 的 index
start_time: tc.startTime
end_time: tc.endTime
input: tc.args
output: tc.output
metadata: {call_id, error (如有)}
```

### 4.3 Token 使用映射

| Codex TokenUsage 字段 | Trace Schema v2 Usage 字段 |
|----------------------|---------------------------|
| `input_tokens` | `input` |
| `output_tokens` | `output` |
| `cached_input_tokens` | `cache_read_input_tokens` |
| `reasoning_output_tokens` | 不映射（Langfuse 无标准字段），记录在 metadata |

## 5. 文件结构

```
hooks/codex/
├── .codex-plugin/
│   └── plugin.json              # Codex plugin 元数据
├── hooks/
│   ├── hooks.json               # Hook 注册 (Stop 事件)
│   ├── langfuse_hook.py         # 主 hook 脚本
│   └── pyproject.toml           # uv 项目配置
├── install.sh                   # Codex 专用安装器
└── README.md
```

## 6. 核心模块设计

### 6.1 `langfuse_hook.py` 主要流程

```
1. 读 stdin JSON → 提取 session_id, transcript_path
2. 获取增量 state (byte offset)
3. 读新增 JSONL 行 → 解析为 RolloutLine 列表
4. 状态机组装 Turn（含 ModelStep + ToolCall）
5. 对每个 Turn:
   a. 构建 Trace Schema v2 JSON
   b. 调用 deliver_trace() 三层投递
6. 更新 state (offset, turn_count)
7. 更新 sidecar 去重文件
```

### 6.2 Rollout 解析器 (parse 模块)

内联在 `langfuse_hook.py` 中，核心数据结构：

```python
@dataclass
class ToolCall:
    call_id: str
    name: str
    args: Any
    start_time: datetime
    end_time: Optional[datetime]
    output: Any
    error: Optional[str]

@dataclass
class ModelStep:
    start_time: datetime
    end_time: datetime
    reasoning: Optional[str]
    text: Optional[str]
    tool_calls: list[ToolCall]
    usage: Optional[dict]

@dataclass
class Turn:
    turn_id: Optional[str]
    start_time: datetime
    end_time: datetime
    model: Optional[str]
    user_input: Optional[str]
    final_output: Optional[str]
    steps: list[ModelStep]
    subagent_thread_ids: list[str]
    completed: bool
    aborted: bool
    total_usage: Optional[dict]
```

### 6.3 状态机逻辑

按行类型分发：
- `session_meta` → 提取 session 级元数据
- `turn_context` → 设置当前 turn 的 model 和参数
- `response_item` → 按 payload.type 分发：
  - `message` (role=user) → 记录 userInput
  - `message` (role=assistant) → 追加到当前 step.text
  - `function_call` / `custom_tool_call` → 创建 ToolCall
  - `function_call_output` / `custom_tool_call_output` → 匹配 ToolCall 并填充 output
  - `reasoning` → 追加到当前 step.reasoning
- `event_msg` → 按 payload.type 分发：
  - `task_started` → 开新 turn
  - `user_message` → 设置 userInput
  - `agent_message` → 记录最后 agent 消息
  - `token_count` → 关闭当前 step，记录 usage
  - `task_complete` → 关闭 turn
  - `turn_aborted` → 关闭 turn（标记 aborted）
  - `collab_agent_spawn_end` → 记录子线程 ID
  - `*_end` + call_id → 匹配 ToolCall 填充 end_time/output/error

## 7. 配置

### 7.1 凭据来源

优先级（低 → 高）：
1. 环境变量 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`
2. 环境文件 `~/.agent-exporter-to-langfuse/config/codex.env`（由安装器写入）
3. Codex plugin userConfig（如果 Codex 支持，通过 `CODEX_PLUGIN_OPTION_*` 环境变量传入）

### 7.2 可选参数

| 参数 | 环境变量 | 默认值 |
|------|---------|--------|
| debug | `LANGFUSE_DEBUG` | true |
| max_chars | `LANGFUSE_MAX_CHARS` | 800000 |
| tags | `LANGFUSE_TAGS` | "codex" |
| user_id | `LANGFUSE_USER_ID` | OS username |

## 8. 安装器

### 8.1 `install.sh` 修改

- 新增 `detect_codex()`：检查 `~/.codex` 目录或 `codex` 命令
- 新增 `install_codex()` 函数，调用 `hooks/codex/install.sh`
- 在 agent 列表和 case 分支中加入 `codex`

### 8.2 `hooks/codex/install.sh`

职责：
1. 创建 `~/.codex/hooks/langfuse/` 目录
2. 复制 `langfuse_hook.py` 到该目录
3. 初始化 uv 环境（`uv init && uv pip install langstash-deliver langfuse`）
4. 写入 `~/.agent-exporter-to-langfuse/config/codex.env`
5. 注册 Codex plugin：
   - 复制 `.codex-plugin/` 到 `~/.codex/plugins/cache/agent-exporter-to-langfuse/codex/<version>/`
   - 复制 `hooks/hooks.json` 到对应位置
6. 注入环境变量到 shell profile

### 8.3 `uninstall.sh` 修改

- 新增 Codex hook 清理逻辑

## 9. 子 Agent 支持

初版不实现子 agent 嵌套（codex-observability-plugin 中通过递归查找子 rollout 文件实现，复杂度较高）。

策略：
- 解析 `collab_agent_spawn_end` 事件记录子线程 ID
- 在 trace metadata 中标记 `subagent_thread_ids`
- 子 agent 的 rollout 文件会在其自身的 Stop hook 中独立处理
- 后续版本可增加嵌套支持

## 10. 边界与约束

- Hook 必须 fail-open：任何异常 exit 0，不阻塞 Codex 会话
- Hook 超时：Codex 默认 30 秒，需在此窗口内完成解析+投递
- Shell 脚本兼容 macOS + Linux（不使用 GNU 独有参数）
- install.sh / uninstall.sh 保持幂等
- 不在日志/输出中暴露完整 API key

## 11. 验证计划

1. 单元测试：rollout JSONL 解析器 → 验证 Turn/Step/ToolCall 组装正确
2. 集成测试：用 fixture rollout 文件模拟 hook 调用 → 验证 Trace Schema v2 输出
3. 端到端测试：在真实 Codex 环境中触发 Stop hook → 验证 Langfuse 收到 trace
4. 增量测试：多次调用 hook → 验证不重复投递已完成的 turn
