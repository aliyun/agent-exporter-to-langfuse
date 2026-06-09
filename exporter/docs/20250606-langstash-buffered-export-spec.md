# Agent Exporter to Langfuse — 技术规格

## 1. 目录结构

```
~/.agent-exporter-to-langfuse/                   ← git clone = $AGENT_EXPORTER_HOME
├── .git/
├── VERSION                                       ← 语义化版本号（如 0.2.0）
├── .update-check                                 ← 更新检测缓存（.gitignore）
├── install.sh / uninstall.sh / upgrade.sh
├── install-remote.sh
├── config/                                       ← .gitignore，运行时凭据
│   ├── {agent}.env                               ← 各 agent 凭据 + 投递配置
│   └── config.toml                               ← langstash 服务配置
├── hooks/                                        ← 各 agent 源码和安装脚本
│   ├── claude-code/ / qoder/ / qoderwork/ / opencode/
├── lib/                                          ← 共享工具
├── exporter/                                     ← langstash 服务源码
├── data/                                         ← langstash 运行时数据
│   ├── pending/{date}.jsonl                      ← 待推送 trace（每行一条）
│   ├── failed/{date}.jsonl                       ← 投递失败兜底日志
│   └── state.json                                ← seq_id / commit_id 进度
└── logs/                                         ← langstash 日志
```

---

## 2. Hook 数据处理规格

### 2.1 Transcript 增量读取

所有 Python hook 共享的增量读取机制：

```python
@dataclass
class SessionState:
    offset: int = 0       # 文件字节偏移
    buffer: str = ""      # 未完成的行尾缓冲
    turn_count: int = 0   # 已处理 turn 数（用于编号续接）
```

- 从 `offset` 位置读取新增字节，按 `\n` 分行
- 最后一行（可能不完整）保留在 `buffer`
- 文件 size < offset（轮转）时重置为 0
- 状态持久化到 `langfuse_state.json`，key = `sha256(session_id::transcript_path)`
- 自动清理 30 天前条目
- `fcntl.flock` 文件锁 + atomic rename（`.tmp` → `.json`）

### 2.2 Turn 组装

```
Turn = {
  user_msg: Dict          # 第一条非 tool_result 的 user 行
  assistant_msgs: List    # 该 turn 内所有 assistant 行（按 message.id 去重/合并）
  tool_results_by_id: Dict  # tool_use_id → {content, timestamp}
}
```

1. role=user（非 tool_result）→ flush 上一个 turn，开始新 turn
2. role=user + content[].type=tool_result → 收集到 tool_results_by_id
3. role=assistant → 按 message.id 追加；Qoder/QoderWork 同 id 做 content-block merge

### 2.3 Backdated Observation

Langfuse SDK 4.x `start_observation()` 不支持 `start_time`，直推 fallback 时使用底层 OTel API：

```python
otel_span = langfuse._otel_tracer.start_span(name=name, start_time=ns)
langfuse._create_observation_from_otel_span(otel_span=otel_span, as_type=as_type, **kwargs)
```

依赖 SDK 4.x 内部接口 `_otel_tracer` / `_create_observation_from_otel_span`。

### 2.4 内容截断

```python
MAX_CHARS = 800_000  # 可通过 LANGFUSE_MAX_CHARS 配置
```

截断后附带 metadata：`{truncated, orig_len, kept_len, sha256}`。

---

## 3. 各 Agent Hook 规格

### 3.1 Claude Code

**触发**：hooks.json 注册 Stop / SubagentStop，`uv run python langfuse_hook.py`，async=true, timeout=20s。

**Stdin payload**：
```json
{
  "sessionId": "...",
  "transcriptPath": "/path/to/transcript.jsonl",
  "hookEventName": "Stop" | "SubagentStop",
  "agent_transcript_path": "..."
}
```

**凭据**：`CLAUDE_PLUGIN_OPTION_{name}` → fallback `{name}` 环境变量。

**Plugin 注册**：`.claude-plugin/plugin.json` + `hooks.json`，通过 `claude plugin marketplace add` + `claude plugin install` 安装。

### 3.2 Qoder

**触发**：`~/.qoder/settings.json` hooks 配置 Stop / SubagentStop。

**Entrypoint**：
```bash
ENV_FILE="$HOME/.agent-exporter-to-langfuse/config/qoder.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
cd "$(dirname "$0")" && exec uv run python langfuse_hook.py
```

**Stdin payload**：
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "hook_event_name": "Stop" | "SubagentStop",
  "cwd": "...",
  "agent_transcript_path": "...",
  "agent_id": "...",
  "agent_type": "...",
  "extra": {"branch": "...", "repo": "...", "email": "...", "user": {"name": "...", "uid": "...", "org_name": "..."}}
}
```

**Content-block merge**：同一 message.id 的 thinking/text/tool_use 多行合并，usage/model/stop_reason 取最后一行。

**SQLite DB enrichment**：从 Qoder SharedClientCache DB 按时间戳匹配（5s 窗口）补充 token/model。DB 路径：
- macOS: `~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db`
- Linux: `~/.local/share/Qoder/SharedClientCache/cache/db/local.db`
- Fallback: `~/.qoder/shared_client/cache/db/local.db`

### 3.3 QoderWork

基于 Qoder hook 代码，关键差异：

| 维度 | Qoder | QoderWork |
|------|-------|-----------|
| 运行时 | `uv run python` | `python3 -m venv` + `pip install` |
| SQLite DB | 有（SharedClientCache） | 无 |
| Token usage | transcript + DB 补充 | 暂不可用 |
| Env 查找 | 单路径 | 多路径（`$SCRIPT_DIR/langfuse.env` 优先） |
| 状态存储 | `~/.qoder/state/` | `~/.qoderwork/state/` |
| Env 硬链接 | 无 | 硬链接到 `$HOOK_DIR/langfuse.env`（VM 场景） |

### 3.4 OpenCode

**注册**：`~/.config/opencode/opencode.json` → `"plugin": ["./plugins/langfuse-exporter.mjs"]`

**数据获取**：通过 `ctx.client.session.messages()` SDK API（非 transcript 文件），监听 `session.idle` 事件触发。

**Curl transport**：Bun 运行时阻断出站 HTTP，`langfuse.fetch = curlFetch`（`execSync('curl ...')`）。

**子 agent**：`client.session.get()` 查询 `parentID`，trace 使用 parent session ID。

**单 Generation**：多个 assistant entries 合并 token usage / cost。

---

## 4. 安装/卸载规格

### 4.1 Env 文件格式

```bash
export LANGFUSE_BASE_URL="https://..."
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."
export LANGFUSE_USER_ID="username"
export LANGFUSE_TAGS="agent-name,team:x"
export LANGSTASH_ENABLED="true"
export LANGSTASH_URL="http://127.0.0.1:5288"
```

### 4.2 Shell Profile Loader

```bash
# ~/.zshenv (or ~/.profile)
for f in "$HOME"/.agent-exporter-to-langfuse/config/*.env; do [ -f "$f" ] && . "$f"; done
```

### 4.3 macOS LaunchAgent

各 Agent 独立 plist（`~/Library/LaunchAgents/com.{agent}.langfuse-env.plist`），RunAtLoad：
```bash
for f in "$HOME"/.agent-exporter-to-langfuse/config/*.env; do [ -f "$f" ] && . "$f"; done
env | grep '^LANGFUSE_\|^LANGSTASH_' | while IFS='=' read -r k v; do launchctl setenv "$k" "$v"; done
```

### 4.4 Agent 检测

| Agent | 检测条件 |
|-------|----------|
| Claude Code | `command -v claude` 或 `~/.claude` 存在 |
| Qoder | `~/.qoder` 存在 或 `command -v qoder/qodercli` |
| QoderWork | `~/.qoderwork` 存在 |
| OpenCode | `~/.config/opencode` 存在 或 `command -v opencode` |

### 4.5 卸载检测

| Agent | 已安装判定 |
|-------|-----------|
| Claude Code | `claude plugin list` 含 langfuse 或 `~/.claude/plugins/langfuse/plugin.json` |
| Qoder | `~/.qoder/hooks/langfuse/` 存在 |
| QoderWork | `~/.qoderwork/hooks/langfuse/` 存在 |
| OpenCode | `~/.config/opencode/plugins/langfuse-exporter.mjs` 存在 |

### 4.6 langstash 安装

install.sh 在安装各 agent hook 之后，自动安装 langstash 服务：

```
1. cd ~/.agent-exporter-to-langfuse/exporter && uv sync
2. 写入 config/config.toml（Langfuse 凭据从收集的 env 中复用）
3. 创建 data/ 和 logs/ 目录
4. macOS: 安装 launchd plist（com.langstash.plist）并 load
5. Linux: 安装 systemd user service 并 enable
```

卸载时反向操作：stop 服务 → 移除 plist/service → 由主 uninstall.sh 清理整个安装目录。

---

## 5. 版本管理规格

### 5.1 VERSION 文件

根目录单行语义化版本号（如 `0.2.0`），install.sh 启动时打印。

### 5.2 install-remote.sh

1. `~/.agent-exporter-to-langfuse/.git` 已存在 → 拒绝，提示 upgrade.sh
2. GitHub API 获取最新 release tag（timeout 5s）→ `git clone --depth 1 --branch $TAG`
3. 失败 → fallback `git clone --depth 1`（main）
4. 支持 `--version vX.Y.Z`
5. `exec bash $INSTALL_DIR/install.sh "$@"`

### 5.3 install.sh Relocate

非标准路径运行 → 标准路径已存在则报错，否则 `git clone` 到标准路径 → `exec` 切换。

### 5.4 --upgrade 模式

| 行为 | 普通模式 | --upgrade |
|------|---------|-----------|
| 凭据收集 | 交互式 | 跳过，复用 config/*.env |
| Agent 选择 | 交互式 | 从已有 env 文件推断 |
| Relocate | 执行 | 跳过 |

### 5.5 upgrade.sh

```
cd ~/.agent-exporter-to-langfuse
git fetch --tags → 获取 LATEST_TAG（GitHub API → fallback git tag）
当前 tag == LATEST_TAG → "Already up to date" → exit
git checkout $LATEST_TAG → bash install.sh --upgrade
```

### 5.6 .update-check

```
LAST_CHECK_EPOCH=19900          # int(time.time() / 86400)
REMOTE_VERSION=0.3.0
LOCAL_VERSION=0.2.0
UPDATE_AVAILABLE=true
```

由 langstash 后台写入（24h 周期），`/stats` 端点暴露更新状态。

---

## 6. 统一 Trace Schema v2

hook 构建的标准 JSON 格式。用于 langstash `/ingest` 接收，JSONL append 存储。

### 6.1 完整结构

```jsonc
{
  // --- 标识 ---
  "schema_version": "2",
  "id": "<uuid>",                            // hook 生成，trace 唯一标识
  "source": "claude-code",                   // 必填，enum: claude-code | qoder | qoderwork | opencode
  "session_id": "abc123",                    // 必填，agent 会话 ID
  "user_id": "username",                     // 可选
  "tags": ["claude-code", "team:x"],         // 可选

  // --- Trace ---
  "trace": {
    "name": "Claude Code - Turn 3",          // 必填
    "start_time": "2025-06-04T10:00:00.000Z",// 必填，ISO 8601 UTC
    "end_time": "2025-06-04T10:01:30.000Z",  // 必填
    "input": {                               // 必填
      "role": "user",
      "content": "..."
    },
    "output": {                              // 必填
      "role": "assistant",
      "content": "..."
    },
    "metadata": {                            // 可选，自由结构
      // --- 公共字段 ---
      "source": "claude-code",               // 冗余，方便查询
      "turn_number": 3,
      "is_subagent": false,
      "assistant_message_count": 2,

      // --- Claude Code / Qoder / QoderWork 特有（optional） ---
      "transcript_path": "/path/to/transcript.jsonl",

      // --- Qoder / QoderWork Desktop 特有（optional） ---
      "cwd": "/working/dir",
      "repo": "my-project",
      "branch": "main",
      "email": "user@example.com",
      "org_name": "...",
      "user_uid": "...",
      "agent_id": "...",                     // SubagentStop
      "agent_type": "...",                   // SubagentStop

      // --- OpenCode 特有（optional） ---
      "directory": "/project/root",
      "parent_session_id": "..."             // 子 agent
    }
  },

  // --- Generations ---
  "generations": [                           // 必填，≥1 个
    {
      "name": "Generation 1",               // 必填
      "model": "claude-opus-4-6",            // 必填
      "start_time": "ISO8601",              // 必填
      "end_time": "ISO8601",                // 必填
      "input": {                             // 可选
        "role": "user",                      // 第一个 gen: user message
        "content": "..."                     // 后续 gen: tool results
      },
      "output": {                            // 必填
        "role": "assistant",
        "content": "...",                    // 可选，文本回复
        "tool_calls": [                      // 可选
          {"id": "toolu_xxx", "name": "Bash", "input": "..."}
        ]
      },
      "usage": {                             // 可选
        "input": 1000,
        "output": 500,
        "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 0,
        "reasoning": 0                      // OpenCode 特有
      },
      "metadata": {                          // 可选
        "assistant_index": 0,
        "tool_count": 3,
        "cost": 0.05                         // OpenCode 特有
      }
    }
  ],

  // --- Tool Spans ---
  "spans": [                                 // 可选
    {
      "name": "Tool: Bash",                 // 必填，格式 "Tool: {name}"
      "generation_index": 0,                 // 必填，对应 generations 数组索引
      "start_time": "ISO8601",              // 必填
      "end_time": "ISO8601",                // 必填
      "input": "ls -la",                    // 可选，string 或 object
      "output": "total 0\n...",             // 可选，string
      "metadata": {                          // 可选
        "tool_name": "Bash",
        "tool_id": "toolu_xxx",
        "status": "completed"                // OpenCode 特有: completed | error
      }
    }
  ]
}
```

### 6.2 字段约束

| 字段 | 最大长度 | 说明 |
|------|---------|------|
| `trace.input.content` | 800,000 chars | hook 侧截断 |
| `trace.output.content` | 800,000 chars | hook 侧截断 |
| `generation.output.content` | 800,000 chars | hook 侧截断 |
| `span.input` | 800,000 chars | hook 侧截断 |
| `span.output` | 800,000 chars | hook 侧截断 |
| `tags` 数组 | 20 个元素 | |
| `metadata` 任意字段 | 10,000 chars | |
| 整个 JSON body | 10 MB | langstash 拒绝超限请求 |

### 6.3 Generation 粒度

各 hook 保持原有粒度：
- Claude Code / Qoder / QoderWork：每个 assistant message 对应一个 Generation（多 Generation）
- OpenCode：多个 assistant entries 合并为单个 Generation

`generations` 数组长度不限。

---

## 7. langstash 服务规格

### 7.1 HTTP API

| Endpoint | 方法 | 说明 |
|----------|------|------|
| `POST /ingest` | POST | 接收单条 Trace Schema v2 JSON |
| `POST /ingest/batch` | POST | 批量接收（≤100 条） |
| `GET /stats` | GET | 统计信息 + 更新状态 |
| `GET /health` | GET | 健康检查 |
| `POST /update` | POST | 触发升级 |
| `GET /` | GET | Web UI 首页（内置 HTML，无需额外前端构建） |

### 7.2 Pending 文件规格

#### 文件命名

```
data/pending/{date}.jsonl
```

- `{date}` = 接收时的 UTC 日期，格式 `YYYY-MM-DD`（如 `2025-06-06.jsonl`）
- 按天分片，一天一个文件，便于按日期清理和定位
- 文件名中不含 seq_id 信息（seq_id 在文件内容中）

#### JSONL 行格式

每行是一个完整 JSON 对象，由 langstash 在 hook 提交的 Trace Schema v2 JSON 基础上注入内部字段：

```jsonc
{"_seq_id":35,"_received_at":"2025-06-06T10:00:00.123Z","schema_version":"2","id":"<uuid>","source":"claude-code","session_id":"...","trace":{...},"generations":[...],"spans":[...]}
```

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `_seq_id` | int64 | langstash 注入 | 全局单调递增序列号，用于投递进度管理 |
| `_received_at` | string | langstash 注入 | 接收时间 ISO 8601 UTC |
| 其余字段 | — | hook 提交 | Trace Schema v2 原始内容（见第 6 节） |

`_seq_id` 是行级唯一标识，全局跨文件严格递增。同一文件内行按 `_seq_id` 升序排列。

### 7.3 POST /ingest 流程

```
1. 接收 JSON body
2. 校验必填字段：schema_version, source, session_id, trace.name, trace.start_time, trace.end_time, generations(≥1)
3. 分配 _seq_id = next_seq_id++
4. 注入 _seq_id 和 _received_at 到 JSON 中
5. 序列化为单行 JSON（无换行）
6. fcntl.flock 加锁 → append 写入 data/pending/{today}.jsonl → 解锁
7. 更新 state.json 中当前文件的 max_seq
8. 返回 HTTP 202 {"status": "accepted", "seq_id": N}
```

**错误响应**：
- 422: schema 校验失败（缺少必填字段）
- 413: body 超 10 MB
- 500: 内部错误

### 7.4 seq_id 与投递索引

#### seq_id 分配

- langstash 单进程维护 int64 计数器 `next_seq_id`，每次 `/ingest` 成功后递增
- 持久化到 `state.json`，重启时从 `next_seq_id` 恢复
- 同一进程内严格单调递增，跨重启不回退
- int64 上限 2⁶³−1（≈9.2×10¹⁸），按每秒 1000 条需约 2.9 亿年溢出，不做回卷处理
- 启动校验：`next_seq_id` 必须 ≥ `commit_id`，否则修正为 `commit_id + 1` 并记录 warning

#### Sender 如何通过 seq_id 定位数据

Sender 需要从 `commit_id + 1` 开始读取未推送数据。定位流程：

```
1. 读取 state.json，获取 commit_id 和 files 索引
2. 在 files 中找到 commit_id + 1 所在的文件：
   遍历 files（按日期升序），找到 min_seq ≤ (commit_id+1) ≤ max_seq 的文件
3. 打开该文件，逐行读取 JSON，解析 _seq_id 字段
4. 跳过 _seq_id ≤ commit_id 的行
5. 收集 batch_size 条 _seq_id > commit_id 的行
6. 如果当前文件读完仍不足 batch_size，继续打开下一个日期文件
```

**注意**：Sender 不维护文件内 byte offset，而是每次从文件头逐行扫描并按 `_seq_id` 过滤。原因：
- pending 文件按天分片，单文件不会过大（典型 < 100MB/天）
- 逐行扫描简单可靠，无需维护额外的行偏移索引
- committed 文件被清理后，Sender 自然跳过

### 7.5 state.json 格式

```json
{
  "next_seq_id": 43,
  "commit_id": 39,
  "last_commit_at": "2025-06-06T10:30:00Z",
  "last_error": null,
  "files": {
    "2025-06-06.jsonl": {
      "min_seq": 35,
      "max_seq": 42
    },
    "2025-06-05.jsonl": {
      "min_seq": 1,
      "max_seq": 34,
      "committed": true
    }
  }
}
```

投递异常时 `last_error` 示例：
```json
{
  "last_error": {
    "time": "2025-06-06T10:31:00Z",
    "seq_id": 40,
    "error": "HTTP 503 Service Unavailable",
    "retries": 3
  }
}
```

| 字段 | 说明 |
|------|------|
| `next_seq_id` | 下一个可分配的 seq_id |
| `commit_id` | 最后成功推送到 Langfuse 的 seq_id。`_seq_id ≤ commit_id` 的行已推送 |
| `last_commit_at` | 最后一次推送成功的时间 |
| `last_error` | 最近一次投递异常信息，推送成功后置为 `null` |
| `last_error.time` | 异常发生时间 |
| `last_error.seq_id` | 发生异常时正在推送的 seq_id |
| `last_error.error` | 异常描述（HTTP 状态码 / 网络错误 / 超时等） |
| `last_error.retries` | 当前连续失败次数（成功后重置为 0 并清空 last_error） |
| `files.{name}.min_seq` | 该文件中最小的 _seq_id |
| `files.{name}.max_seq` | 该文件中最大的 _seq_id |
| `files.{name}.committed` | true = 该文件所有行都已推送（`max_seq ≤ commit_id`） |

`files` 条目在以下时机维护：
- `/ingest` 写入时：创建或更新 `max_seq`，首行写入时设置 `min_seq`
- Sender 推送成功后：当 `commit_id ≥ max_seq` 时标记 `committed: true`
- 存储清理时：删除文件后移除对应条目

`last_error` 在以下时机维护：
- Sender 推送失败时：写入异常信息，`retries` 递增
- Sender 推送成功后：置为 `null`

### 7.6 Sender 行为

**投递保证**：at-least-once。`_seq_id ≤ commit_id` 的 trace 已推送成功；`_seq_id > commit_id` 的可能因重启/失败而重推，但不丢。断网恢复后 sender 自动从 `commit_id + 1` 继续推送积压数据。

**流程**：

1. 后台线程，间隔 `interval_seconds`（默认 5s）
2. 从 `commit_id + 1` 开始，按 seq_id 顺序读取 pending JSONL（见 7.4 定位流程）
3. 每批收集 `batch_size` 条（默认 10），转换为 Langfuse ingestion batch request
4. POST `{base_url}/api/public/ingestion`（Basic Auth: public_key / secret_key）
5. 成功：`commit_id` 更新为本批最大 `_seq_id`，清空 `last_error`，更新 stats
6. 检查 files：`max_seq ≤ commit_id` 的文件标记 `committed: true`

### 7.7 Sender 错误处理

| 场景 | 处理 |
|------|------|
| HTTP 200-299 | 成功，更新 commit_id |
| HTTP 400 | 数据错误，跳过该行（记录 error） |
| HTTP 401/403 | 凭据错误，暂停 sender |
| HTTP 429 | 限流，退避 |
| HTTP 5xx | 服务端错误，退避 |
| 超时/网络错误 | 退避 |

**退避策略**：5s → 10s → 20s → 40s → 80s → 160s → 300s（上限），成功一次重置。

### 7.8 存储清理

**触发时机**：启动时 + 每小时定期检查。

**清理流程**：
1. 计算 `data/`（pending/ + failed/）总大小
2. 如未超过 `max_size_gb` → 跳过
3. 超限时按日期从旧到新依次删除：
   - 优先删除 `committed: true` 的 pending 文件（数据已推送，安全删除）
   - 其次删除 failed 文件（兜底日志，优先级低于未推送数据）
   - 仍超限则删除最老的未 committed pending 文件（数据丢失，记录 warning）
4. 每删一个文件后重新计算总大小，不超限即停止
5. 删除后同步更新 `state.json`

**配置**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `storage.max_size_gb` | `20.0` | data/ 目录总大小上限（GB） |
| `storage.retention_days` | `30` | 已 committed 文件最大保留天数，超龄自动清理（不受大小限制触发） |

**Stats 暴露**：`GET /stats` 返回 `storage_used_mb` 字段，Web UI 展示已用/上限。

### 7.9 配置

```toml
# ~/.agent-exporter-to-langfuse/config/config.toml
[server]
host = "127.0.0.1"
port = 5288

[langfuse]
public_key = ""
secret_key = ""
base_url = "https://us.cloud.langfuse.com"

[storage]
data_dir = "~/.agent-exporter-to-langfuse/data"
max_size_gb = 20.0           # data/ 目录总大小上限
retention_days = 30          # 已 committed 文件最大保留天数

[sender]
interval_seconds = 5
max_backoff_seconds = 300
batch_size = 10
timeout_seconds = 30
```

### 7.10 Stats

`GET /stats` 响应：

```json
{
  "traces_today": 42,
  "tokens_today": {"input": 150000, "output": 30000, "total": 180000},
  "pending_count": 3,
  "failed_count": 0,
  "sent_today": 39,
  "last_success_at": "2025-06-06T10:30:00Z",
  "last_error": null,
  "storage_used_mb": 12.5,
  "uptime_seconds": 3600,
  "update_available": true,
  "current_version": "0.2.0",
  "latest_version": "0.3.0"
}
```

### 7.11 Menubar UI（仅 macOS）

通过 rumps 在系统菜单栏展示 langstash 状态摘要。

#### 图标设计

**概念**：圆角矩形框 + 三个圆点（`•••`）— box 代表 stash 暂存容器，圆点代表缓冲中的数据。纯图标，不显示数字。

```
  正常态          pending 态        异常态           离线态
 ┌───────┐      ┌───────┐ ●      ┌───────┐ ●      ┌ ─ ─ ─ ┐
 │ • • • │      │ • • • │        │ • • • │        │ • • • │
 └───────┘      └───────┘        └───────┘        └ ─ ─ ─ ┘
                 (黄色badge)      (整体变黄)        (虚线灰色)
```

效果图见 `exporter/assets/menubar-icon.svg`。

**文件格式**：16x16 PNG template image（单色黑，macOS 自动适配深/浅模式），路径 `exporter/assets/menubar-icon*.png`。

**状态切换**：通过 rumps 的 `icon` 属性动态切换：

| 状态 | 图标文件 | 说明 |
|------|---------|------|
| 正常 | `menubar-icon.png` | 白色边框 + 白色圆点 |
| pending | `menubar-icon-pending.png` | 白色边框 + 右上角黄色 badge 圆点 |
| 异常 | `menubar-icon-error.png` | 黄色边框 + 黄色圆点 + 黄色 badge |
| 离线 | `menubar-icon-offline.png` | 灰色虚线边框 + 灰色圆点 |

Menubar 仅展示图标，不显示文字和数字。详细数据通过下拉菜单和 Web UI 查看。

#### 菜单结构

```
[•••]
├── Today: 42 traces, 180k tokens
├── Sent: 39 | Pending: 3 | Failed: 0
├── ─────────────────────
├── Last success: 2m ago
├── Last error: (none)
├── ─────────────────────
├── Open Web UI              → 浏览器打开 http://127.0.0.1:5288
├── ─────────────────────
├── v0.2.0 — Update available (v0.3.0)
├── Upgrade Now              → 执行 upgrade.sh
├── ─────────────────────
└── Quit langstash
```

#### 轮询

- 间隔 10s，通过 `GET http://127.0.0.1:{port}/stats`
- 超时 3s，失败时切换为离线图标，菜单首项显示 "Server unreachable"

### 7.12 Web UI

langstash 内置 Web UI，通过 `GET /` 提供，适用于所有平台（尤其 Linux 无 menubar 环境）。

#### 页面布局

单页应用，深色主题，响应式布局。

```
┌─────────────────────────────────────────────────────────┐
│  langstash                              v0.2.0  ⬆ 可更新 │  ← 顶部栏
├──────────┬──────────┬──────────┬────────────────────────┤
│  ◆ 42   │  ↑ 39    │  ⏳ 3    │  ⚠ 0                  │  ← 指标卡片
│  今日    │  已推送   │  等待中   │  失败                  │
│  traces  │  sent    │  pending │  failed               │
├──────────┴──────────┴──────────┴────────────────────────┤
│                                                         │
│  Tokens Today                                           │  ← Token 统计
│  Input: 150,000  Output: 30,000  Total: 180,000         │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  投递状态                                                │  ← 状态详情
│  ┌─────────────────────────────────────────────────┐    │
│  │ Last success: 2025-06-06 10:30:00 (2m ago)      │    │
│  │ Last error:   (none)                            │    │
│  │ Uptime:       1h 0m                             │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  存储                                                    │  ← 存储用量
│  ████████░░░░░░░░░░░░  12.5 MB / 20.0 GB (0.06%)       │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

#### 指标卡片

| 卡片 | 数据源 (`/stats`) | 图标 | 正常色 | 异常色 |
|------|------------------|------|--------|--------|
| 今日 traces | `traces_today` | ◆ | 蓝色 | — |
| 已推送 | `sent_today` | ↑ | 绿色 | — |
| 等待中 | `pending_count` | ⏳ | 灰色 | 黄色（> 0） |
| 失败 | `failed_count` | ⚠ | 灰色 | 红色（> 0） |

#### 投递状态区

- `Last success`：`last_success_at` 转为人类可读时间 + "(Xm ago)" 相对时间
- `Last error`：`last_error` 为 null 显示 "(none)"；非 null 显示 `last_error.error`（红色）+ retry 次数
- `Uptime`：`uptime_seconds` 转为 "Xh Xm" 格式

#### 存储用量条

- 进度条：`storage_used_mb / (max_size_gb * 1024)` 百分比
- 正常：蓝色；超过 80% 黄色；超过 95% 红色

#### 版本信息

- 顶部栏右侧显示当前版本 `current_version`
- `update_available == true` 时显示 "⬆ 可更新 (vX.Y.Z)" 徽标（绿色）

#### 实现方式

- FastAPI 静态路由 `GET /`，返回内嵌 HTML（单文件，无需 npm build）
- 前端 JS 定时轮询 `GET /stats`（10s 间隔），动态更新 DOM
- 无第三方前端框架依赖（纯 HTML + CSS + vanilla JS）
- Favicon 复用 menubar 图标的"火焰+托盘"设计（SVG 内嵌，橙红色）
- macOS menubar 点击 "Open Web UI" 跳转浏览器打开同一地址

### 7.13 进程管理

**macOS**：`~/Library/LaunchAgents/com.langstash.plist`
- KeepAlive: true, ThrottleInterval: 5s

**Linux**：`~/.config/systemd/user/langstash.service`
```ini
[Unit]
Description=langstash - Agent Exporter to Langfuse

[Service]
ExecStart=%h/.agent-exporter-to-langfuse/exporter/.venv/bin/langstash --server-only
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```
- `--server-only`：不启动 menubar（Linux 无 rumps）
- 启用：`systemctl --user enable --now langstash`

### 7.14 Python 依赖

```toml
[project]
name = "langstash"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["fastapi>=0.100", "uvicorn[standard]>=0.20", "httpx>=0.24"]

[project.optional-dependencies]
macos = ["rumps>=0.4"]
```

不依赖 `langfuse` SDK — 直接调用 Langfuse ingestion REST API。

---

## 8. Hook 投递链路规格

### 8.1 投递流程

```
1. hook 完成 transcript 解析，构建 Trace Schema v2 JSON
2. if LANGSTASH_ENABLED == "true":
     POST $LANGSTASH_URL/ingest (timeout: $LANGSTASH_TIMEOUT)
     if HTTP 202: return success
3. fallback: Langfuse SDK 直推（现有 emit_turn 逻辑）
     if success: return
4. fallback: append JSON 到 ~/.agent-exporter-to-langfuse/data/failed/{date}.jsonl
5. exit 0（无论结果如何）
```

### 8.2 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LANGSTASH_ENABLED` | `true` | 设为 `false` 关闭 langstash 投递 |
| `LANGSTASH_URL` | `http://127.0.0.1:5288` | langstash 地址 |
| `LANGSTASH_TIMEOUT` | `10` | HTTP 超时秒数 |

### 8.3 失败日志格式与并发写入

`data/failed/{date}.jsonl`：每行一条 Trace Schema v2 JSON（与 pending 格式相同，但无 `_seq_id`）。

**并发问题**：多个 hook 进程（claude-code Stop、qoder Stop、opencode session.idle 等）可能同时触发 fallback，并发 append 同一文件。

**解决方案**：`fcntl.flock` 文件锁（与 langstash pending 写入相同策略）。`fcntl.flock` 是进程级锁，绑定在文件描述符上，进程终止时内核自动释放，不会残留。

```python
def append_failed_trace(trace_json: dict) -> None:
    data_dir = Path.home() / ".agent-exporter-to-langfuse" / "data" / "failed"
    data_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = data_dir / f"{today}.jsonl"
    line = json.dumps(trace_json, ensure_ascii=False) + "\n"

    fd = open(path, "a", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # 阻塞等锁，超时由 hook timeout 兜底
        fd.write(line)
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
```

**JS hook（OpenCode）**：使用 `execSync('flock ...')` 或 append-only `writeFileSync` + `O_APPEND` flag（POSIX 保证单次 ≤ PIPE_BUF 的 write 原子性，单行 JSON 通常远小于 PIPE_BUF 4096 字节上限；超大 trace 走 flock）。

### 8.4 关键约束

- hook 总执行时间不超过 20s（agent hook timeout）
- langstash 投递超时 10s，超时后进入直推 fallback
- 所有 fallback 均 exit 0（fail-open，不阻塞 agent）

---

## 9. 运行约束

| 约束 | 值 | 说明 |
|------|------|------|
| Hook timeout | 20s | Agent 插件系统限制 |
| Langfuse flush timeout | 5s | 直推 fallback 时防止慢网络阻塞 |
| langstash 投递 timeout | 10s | 超时进入下一级 fallback |
| MAX_CHARS 默认值 | 800,000 | 单字段截断上限 |
| 状态清理周期 | 30 days | hook 侧自动删除过期 session state |
| FileLock timeout | 2s | 超时则跳过本次处理 |
| GitHub API timeout | 5s connect | 更新检测超时静默跳过 |
| config/ 不入 git | .gitignore | env 文件含凭据 |
| Shell 脚本兼容性 | macOS + Linux | 不使用 GNU 专有参数 |
| 幂等性 | install/uninstall | 可重复执行无副作用 |
| Trace JSON 上限 | 10 MB | langstash 拒绝超限 body |
