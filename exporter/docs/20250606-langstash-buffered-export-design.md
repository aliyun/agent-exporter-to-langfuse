# Agent Exporter to Langfuse — 设计文档

## 1. 项目定位

将 AI Coding Agent 的会话可观测性数据（对话轮次、模型调用、工具使用、Token 消耗）零代码侵入地导出到 Langfuse，支持开源 Langfuse 和阿里云 Agent-Lens（Langfuse API 兼容）。

---

## 2. 架构总览

采用 oh-my-zsh / nvm 的 **Git Clone 模式** — `~/.agent-exporter-to-langfuse/` 本身就是 git clone，同时作为源码仓库和运行时主目录。

```
┌──────────────────────────────────────────────────────────────────┐
│  install.sh / uninstall.sh / upgrade.sh                          │  生命周期管理
├──────────────────────────────────────────────────────────────────┤
│  hooks/claude-code  │  hooks/qoder  │  hooks/qoderwork  │  hooks/opencode  │
│  (Plugin Hook)      │  (Stop Hook)  │  (Stop Hook)      │  (Plugin .mjs)   │
│                                                                  │
│  hook 构建 Trace Schema v2 JSON → 三级投递链路                    │
│  ┌─────────┐    ┌──────────────┐    ┌────────────────────┐       │
│  │langstash│ →→ │直推 Langfuse │ →→ │写入本地失败日志     │       │
│  │ :5288   │    │  SDK         │    │ data/failed/*.jsonl│       │
│  └────┬────┘    └──────────────┘    └────────────────────┘       │
│       │                                                          │
│  ┌────▼──────────────────────────────────┐                       │
│  │  langstash 本地常驻服务                │                       │
│  │  JSONL append → seq_id 自增 → sender  │                       │
│  │  → 异步批量推送 Langfuse API           │                       │
│  └───────────────────────────────────────┘                       │
├──────────────────────────────────────────────────────────────────┤
│  Langfuse API (self-hosted / cloud / Agent-Lens)                 │
└──────────────────────────────────────────────────────────────────┘
```

**目录结构：**

```
~/.agent-exporter-to-langfuse/                   ← git clone = 运行时主目录
├── .git/
├── VERSION                                       ← 语义化版本号
├── install.sh / uninstall.sh / upgrade.sh
├── install-remote.sh                             ← curl|bash 远程安装引导
├── config/                                       ← .gitignore，运行时凭据
│   ├── claude-code.env
│   ├── qoder.env / qoderwork.env / opencode.env
│   └── config.toml                               ← langstash 配置
├── hooks/                                        ← 各 agent hook 源码 & 安装脚本
│   ├── claude-code/ / qoder/ / qoderwork/ / opencode/
├── lib/                                          ← 共享工具脚本
├── exporter/                                     ← langstash 服务源码
├── data/                                         ← langstash 运行时数据
│   ├── pending/*.jsonl                           ← 待推送 trace
│   ├── failed/*.jsonl                            ← 投递失败兜底
│   └── state.json                                ← seq_id / commit_id 进度
└── logs/                                         ← langstash 日志
```

---

## 3. 核心设计原则

### 3.1 Fail-Open
所有 hook 在任何异常情况下（SDK import 失败、网络超时、文件不存在）均 exit 0，绝不阻塞宿主 Agent。

### 3.2 增量处理
Python hooks 通过 file offset + JSONL line buffer 实现增量读取 transcript，跨 hook 调用持久化进度（`langfuse_state.json`），避免重复推送。

### 3.3 适配器隔离
每个 Agent 有独立的 hook 实现（适配各自的插件系统、transcript 格式、数据获取方式），但输出统一的 Trace Schema v2 JSON。

### 3.4 凭据集中
所有 Agent 共享 `~/.agent-exporter-to-langfuse/config/{agent}.env` 凭据文件，通过 shell profile loader 统一注入环境变量。macOS GUI 应用通过 LaunchAgent 注入 `launchctl setenv`。

### 3.5 幂等安装
install.sh 和 uninstall.sh 可重复执行且无副作用。

---

## 4. 支持的 Agent 及其适配方式

| Agent            | Hook 机制                    | Transcript 来源                          | 运行时         | 特殊处理                                     |
|------------------|------------------------------|------------------------------------------|---------------|----------------------------------------------|
| Claude Code      | Plugin Hook (hooks.json)     | session JSONL (stdin payload 提供 path)   | Python + uv   | SDK 4.x OTel backdating                      |
| Qoder (CLI/Desktop) | settings.json Stop hook  | session JSONL (stdin payload)             | Python + uv   | SQLite DB token enrichment, content-block merge |
| QoderWork        | settings.json Stop hook      | 同 Qoder 格式                             | Python + venv | 无 SQLite enrichment，VM 环境适配             |
| OpenCode         | plugin .mjs (opencode.json)  | SDK client API (`session.messages()`)     | Bun/Node.js   | curl-based fetch bypass                       |

---

## 5. 数据模型

每个 hook 产出的 Langfuse observability 结构：

```
Trace (per turn)
├── input: user message
├── output: final assistant text
├── metadata: source, session_id, turn_number, is_subagent, ...
│
├── Generation 1 (first LLM call in this turn)
│   ├── model, input, output (text + tool_calls), usage
│   ├── Tool: Bash        (span, nested under generation)
│   ├── Tool: Read        (span)
│   └── Tool: Edit        (span)
│
├── Generation 2 (second LLM call, after tool results returned)
│   ├── input: tool_results from Gen 1
│   ├── output: text + tool_calls
│   └── Tool: Write       (span)
│
└── ... (as many generations as the agentic loop requires)
```

**Generation 粒度**：各 hook 保持原有粒度。Claude Code/Qoder 每个 assistant message 对应一个 Generation（多 Generation），OpenCode 将多个 assistant entries 合并为单个 Generation。统一 Trace Schema v2 的 `generations` 数组长度不限。

**时间线重建**：每个 span/generation 带有从 transcript 解析的精确 start/end time，Python hooks 通过 OTel `start_time` 参数实现 backdated creation。

---

## 6. 投递架构

### 6.1 三级投递链路

hook 构建 Trace Schema v2 JSON 后，按优先级依次尝试：

```
① langstash 投递（默认）
   POST http://127.0.0.1:5288/ingest, timeout 10s
   成功 → 返回，hook 退出
   失败 → 进入 ②

② Langfuse SDK 直推（fallback）
   使用现有 Langfuse SDK 逻辑直接推送
   成功 → 返回，hook 退出
   失败 → 进入 ③

③ 本地日志保存（兜底）
   append 到 ~/.agent-exporter-to-langfuse/data/failed/{date}.jsonl
   确保数据不丢，后续可手动重推

所有阶段失败均 exit 0，绝不阻塞 agent。
```

### 6.2 langstash 默认安装

统一 install.sh 默认安装 langstash 服务并设置 `LANGSTASH_ENABLED=true`：
- 各 agent env 文件写入 `LANGSTASH_ENABLED=true` 和 `LANGSTASH_URL=http://127.0.0.1:5288`
- macOS 通过 launchd 常驻 langstash 进程
- 用户可通过 `LANGSTASH_ENABLED=false` 关闭 langstash 投递，退回纯直推模式

### 6.3 langstash 核心机制

langstash 作为本地单进程常驻服务，提供两种状态展示界面：

- **Web UI**（所有平台）：内置于 FastAPI 服务，通过浏览器访问 `http://127.0.0.1:5288`，展示推送状态、统计、更新通知
- **macOS Menubar**（仅 macOS）：通过 rumps 在系统菜单栏展示摘要状态，点击可跳转 Web UI

核心流程：

1. **接收**：hook POST `/ingest` → langstash 分配 int64 自增 `_seq_id` → append 写入当日 JSONL 文件
2. **存储**：按日期分片 JSONL（`data/pending/{date}.jsonl`），fcntl.flock 保护并发写入
3. **推送**：sender 后台线程按 `_seq_id` 顺序读取 → 批量调用 Langfuse ingestion REST API
4. **进度**：`state.json` 记录 `commit_id`（最后成功推送的 seq_id），at-least-once 保证
5. **清理**：两种清理策略协同 — 已 committed 文件超过 `retention_days`（默认 30 天）自动清理；存储总量超过 `max_size_gb`（默认 20 GB）时按日期从旧到新删除（优先已 committed → failed → 未 committed，未 committed 被删除时记录 warning）

### 6.4 投递保证

- **At-least-once**：`commit_id` 之前的 trace 已推送成功，之后的可能因重启/失败而重推，但不丢
- **断网恢复**：网络恢复后 sender 自动从 `commit_id + 1` 继续推送积压数据
- **退避重试**：连续失败时间隔倍增（5s → 10s → ... → 300s），成功一次重置

---

## 7. 生命周期管理

### 7.1 安装

**两种安装方式：**

| 方式 | 命令 | 说明 |
|------|------|------|
| Git Clone（推荐） | `git clone <repo> ~/.agent-exporter-to-langfuse && bash ~/.agent-exporter-to-langfuse/install.sh` | 用户直接控制版本 |
| curl One-Liner | `curl -sSf <url>/install-remote.sh \| bash` | 自动 clone 最新 release tag 后执行 install.sh |

**自动 Relocate**：当 install.sh 从非标准路径运行时，自动 clone 到 `~/.agent-exporter-to-langfuse/` 并切换。

**安装流程：**
1. 自动检测已安装的 Agent
2. 安装运行时依赖（uv / npm）
3. 交互式或非交互式收集 Langfuse 凭据
4. 委托各 agent `hooks/{agent}/install.sh` 安装 hook
5. 写入凭据到 `config/{agent}.env`（含 `LANGSTASH_ENABLED=true`）
6. 安装 langstash 服务（`cd exporter && uv sync`）
7. 安装 launchd plist（langstash 常驻 + env 注入）
8. 添加 shell profile loader

### 7.2 版本追踪

- 根目录 `VERSION` 文件维护语义化版本号（SemVer）
- 发版流程：更新 VERSION → git commit → git tag vX.Y.Z → push

### 7.3 升级

```bash
bash ~/.agent-exporter-to-langfuse/upgrade.sh
```

基于 release tag：git fetch → 获取最新 tag → checkout → `install.sh --upgrade`（复用已有 config/*.env，无交互）。

### 7.4 自动更新检测

由 langstash 后台每 24h 检查 GitHub releases API，结果缓存到 `.update-check`，通过 `/stats` 端点、Web UI 和 menubar（macOS）通知用户。

### 7.5 卸载

```bash
bash ~/.agent-exporter-to-langfuse/uninstall.sh
```

流程：各 agent uninstall → 清理 shell profile → 卸载 langstash/launchd → `rm -rf ~/.agent-exporter-to-langfuse`。

---

## 8. 安装器共同行为

### 8.1 各 Agent 安装器共同步骤
1. 拷贝/注册 hook 脚本到 Agent 的插件/hooks 目录
2. 写入凭据到 `~/.agent-exporter-to-langfuse/config/{agent}.env`
3. 添加 shell profile loader（一次性，所有 agent 共享）
4. macOS: 创建 LaunchAgent plist 注入环境变量到 GUI 进程

### 8.2 环境变量注入

**Shell 场景**（终端启动的 CLI Agent）：
```bash
# ~/.zshenv (or ~/.profile)
for f in "$HOME"/.agent-exporter-to-langfuse/config/*.env; do [ -f "$f" ] && . "$f"; done
```

**GUI 场景**（macOS 桌面应用）：
```
LaunchAgent plist → source env → launchctl setenv LANGFUSE_*
```

---

## 9. 各 Agent Hook 实现细节

### 9.1 Claude Code (`hooks/claude-code/`)

- **触发方式**: Plugin Hook，hooks.json 注册 Stop / SubagentStop 事件
- **入口**: `uv run python langfuse_hook.py`
- **数据流**:
  1. 从 stdin 读取 JSON payload（含 sessionId, transcriptPath）
  2. 增量读取 transcript JSONL → 组装 Turn（user → assistant msgs → tool results）
  3. 对每个 Turn 构建 Trace Schema v2 JSON → 三级投递
- **凭据来源**: `CLAUDE_PLUGIN_OPTION_*` 环境变量（Plugin userConfig）+ fallback 到普通 env
- **状态存储**: `~/.claude/state/langfuse_state.json`
- **特殊处理**:
  - SubagentStop 事件使用 `agent_transcript_path`
  - 使用 Langfuse SDK 4.x `_otel_tracer.start_span(start_time=ns)` 实现 backdated observation（直推 fallback 时）

### 9.2 Qoder (`hooks/qoder/`)

- **触发方式**: settings.json 配置的 Stop/SubagentStop hook command
- **入口**: `langfuse-entrypoint.sh` → source env → `uv run python langfuse_hook.py`
- **凭据来源**: 环境变量（entrypoint.sh source env 文件）
- **状态存储**: `~/.qoder/state/langfuse_state.json`
- **特殊处理**:
  - Content-block merge：同一 message.id 的多行 JSONL 合并为单条 assistant message
  - SQLite DB enrichment：从 SharedClientCache DB 补充 token/model
  - Desktop payload 额外上下文（branch, repo, email, org）写入 trace metadata
  - SubagentStop 无 agent_transcript_path 时（Desktop）静默跳过

### 9.3 QoderWork (`hooks/qoderwork/`)

- **触发方式**: 同 Qoder
- **入口**: `langfuse-entrypoint.sh` → 多路径 env 查找 → venv python（非 uv）
- **与 Qoder 的差异**:
  - 无 SQLite DB enrichment（暂无本地 DB）
  - 使用 `python3 -m venv` 而非 `uv`（VM 环境适配）
  - entrypoint.sh 多路径 env 查找（本地 hook 目录优先）
  - Trace label 前缀 `QoderWork`
- **状态存储**: `~/.qoderwork/state/langfuse_state.json`

### 9.4 OpenCode (`hooks/opencode/`)

- **触发方式**: opencode.json plugin 配置
- **入口**: Bun 加载 `plugins/langfuse-exporter.mjs`
- **数据流**: 通过 `ctx.client.session.messages()` SDK API 获取消息，监听 `session.idle` 事件触发
- **特殊处理**:
  - Bun 网络沙箱 → 重写 `langfuse.fetch` 为 curl-based
  - 子 agent session 通过 `client.session.get()` 查询 parentID
  - `chat.params` hook 捕获 model 信息
  - 单 Generation（多 assistant entries 合并 token usage）

---

## 10. 配置参数

### 10.1 Langfuse 凭据

| 环境变量 | 必填 | 说明 |
|----------|------|------|
| `LANGFUSE_PUBLIC_KEY` | Yes | Langfuse 项目公钥 |
| `LANGFUSE_SECRET_KEY` | Yes | Langfuse 项目私钥 |
| `LANGFUSE_BASE_URL` | Yes | Langfuse 服务地址 |
| `LANGFUSE_USER_ID` | No | 用户标识（默认 OS username） |
| `LANGFUSE_TAGS` | No | 逗号分隔的 tag 列表 |
| `LANGFUSE_MAX_CHARS` | No | 单字段最大字符数（默认 800000） |
| `LANGFUSE_DEBUG` | No | 调试日志开关（默认 true） |

### 10.2 投递控制

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `LANGSTASH_ENABLED` | `true` | 是否通过 langstash 投递（`false` 退回直推） |
| `LANGSTASH_URL` | `http://127.0.0.1:5288` | langstash 服务地址 |
| `LANGSTASH_TIMEOUT` | `10` | HTTP 超时秒数 |

### 10.3 更新检测

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `AGENT_EXPORTER_UPDATE_MODE` | `prompt` | `prompt` / `auto` / `disabled` |
| `AGENT_EXPORTER_UPDATE_FREQUENCY` | `24` | 检查间隔（小时） |

---

## 11. 平台支持

- macOS: 全功能（shell env + LaunchAgent GUI inject + launchd 常驻 langstash + menubar + Web UI）
- Linux: shell env + systemd user service + langstash Web UI
- Windows: 计划中

---

## 12. 依赖关系

### Python hooks (claude-code, qoder)
- Python ≥ 3.13（由 uv 管理）
- `langfuse` ≥ 4.7.0（SDK 4.x OTel-based，直推 fallback 使用）

### Python hooks (qoderwork)
- Python 3（系统 python3，使用 venv）
- `langfuse`（pip install）

### JS hook (opencode)
- Node.js / Bun
- `langfuse` npm package
- 系统 `curl`（绕过 Bun 网络限制）

### langstash 服务
- Python ≥ 3.11
- `fastapi`, `uvicorn`, `httpx`
- `rumps`（macOS menubar，可选依赖）
- 不依赖 `langfuse` SDK — 直接调用 Langfuse ingestion REST API

### 生命周期管理
- `git`（安装和升级必需）
- GitHub API（自动更新检测，超时静默跳过）
