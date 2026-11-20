# Design: qoder-cn Hook 投递（Langfuse 观测）

## Problem

Qoder 国内版（qoder-cn，Electron Desktop 应用 `com.qodercn.app.stable`，配置目录 `~/.qoder-cn/`）目前没有任何 Langfuse 观测能力：本仓库已为 claude-code、codex、cursor、opencode、pi、qoder、qoderwork 七个产品提供 hook 投递，但 qoder-cn 用户的会话（turns、generations、tool calls、token usage）无法投递到 Langfuse。本 Design 需要决定 qoder-cn hook 投递的实现方式与适配边界。

## Context

仓库既有模式（verified facts）：

- `hooks/` 下每个产品一个独立完整目录（install.sh / uninstall.sh / README.md / hooks/langfuse_hook.py / hooks/langfuse-entrypoint.sh / hooks/pyproject.toml），跨产品共享的只有 `hooks/langstash-deliver` 共享投递包（由各产品 install.sh 拷贝进产品 hook 目录）。
- qoder hook 的投递机制：install.sh 收集 Langfuse 凭据（交互或参数），拷贝 `langfuse_hook.py` + `langfuse-entrypoint.sh` + `langstash_deliver` 到 `~/.qoder/hooks/langfuse/`，`uv sync` 安装依赖，向 `~/.qoder/settings.json` 注册 `Stop` 与 `SubagentStop` 两个 hook 事件（命令 `~/.qoder/hooks/langfuse/langfuse-entrypoint.sh`，幂等：已含 `langfuse` 命令则跳过），写 env 文件 `~/.agent-exporter-to-langfuse/config/qoder.env`（`LANGFUSE_*` + `LANGSTASH_ENABLED=true` + `LANGSTASH_URL=http://127.0.0.1:5288`，tag 含 `qoder`）。hook 运行时经 `langstash_deliver.deliver_trace` 投递到本地 langstash 服务，由其转发 Langfuse。
- qoder hook 的 token/model enrichment 来自 Qoder Desktop 的 SharedClientCache SQLite DB：`_find_qoder_db()` 依次尝试 `~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db`（macOS）/ `~/.local/share/Qoder/...`（Linux）/ `~/.qoder/shared_client/cache/db/local.db`，查询 `chat_message` 表（`session_id`、`role`、`gmt_create`、`token_info`、`model_info`）。
- qoderwork 是同模式 fork：差异仅路径、env 文件名（`qoderwork.env`）、默认 tag（`qoderwork`）、entrypoint 的 VM 双路径特性和 env 硬链接。qoderwork 的 `langfuse_hook.py` 没有 SQLite enrichment（QoderWork hook 在 VM 内运行，无法访问宿主机 DB）。
- `deploy/package.sh` 用 `git ls-files` 打包整个仓库，新增 `hooks/qoder-cn/` 目录自动纳入，无需改动。
- `deploy/installer.sh` 通过 `is_agent_installed()` 的 case 分支检测产品是否已安装（如 `qoder)` 检测 `~/.qoder` 或 `qoder`/`qodercli` 命令），再对其执行 `install.sh --upgrade -y`。

qoder-cn 事实（本机 2026-11-19 现场验证，用户已安装 qoder-cn 并有真实使用数据）：

- 配置目录 `~/.qoder-cn/` 存在；产品为 Electron Desktop（`~/Library/Application Support/com.qodercn.app.stable`），无 CLI 形态（`~/.qoder-cn/bin` 只有 computer-use / office 工具）。
- `~/.qoder-cn/settings.json` 的 hooks schema 与 qoder 完全同族（`hooks.<Event>[].{matcher, hooks[].{type,command,timeout}}`），`Stop` 事件已注册多个第三方 hook（loongsuite-pilot、yunke 等）；不存在 `SubagentStop` key。
- `~/.qoder-cn/shared_client/cache/db/local.db` 存在，`chat_message` 表结构与 qoder 完全一致，含真实数据（6803 行、3369 条 assistant、2725 条非空 `token_info`；`model_info` 如 `{"model_key":"dashscope_qwen3_coder"}`）。`~/Library/Application Support/com.qodercn.app.stable/` 下无 SharedClientCache DB。
- 会话 transcript 位于 `~/.qoder-cn/projects/<project-dir>/<session>.jsonl`，行格式与 qoder 同族（`type` 为 `user`/`assistant`/`attachment`/`runtime-config` 等，`message.content[]` text blocks），hook 事件 payload 由产品自身的 hook 机制给出（含 transcript 路径），与 qoder 共用同一事件机制。
- 同一台机器上国际版 Qoder 的 DB（`~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db`）与 qoder-cn 的 DB 并存，两者 session 数据完全独立。

测试与文档基线：

- `tests/e2e/` 下已有产品安装 E2E 先例（`test_cursor_install.sh`、`test_pi_hook_install.sh`、`test_opencode_langfuse_delivery.sh`）；qoder/qoderwork 自身没有单元测试（claude-code 是唯一带 pytest 单测的 hook）。
- AGENTS.md 要求：新增产品目录属于代码结构变化，必须适配 `deploy/package.sh`、`deploy/installer.sh` 等打包/安装脚本；install/uninstall 保持幂等；修改后必须补充测试；文档与实际行为一致。

## Goals

- qoder-cn 用户的会话数据（turns、generations、tool calls、token/model usage）能通过既有 langstash 投递链路到达 Langfuse，行为与 qoder hook 等价（含 SQLite token/model enrichment）。
- qoder-cn hook 的安装、卸载、升级与统一安装器（`deploy/installer.sh`）的集成行为与既有产品 hook 一致（幂等、可重复执行）。
- 同机共存场景下 qoder-cn 与国际版 qoder 的投递互不串扰（状态、凭据、DB 数据各自独立）。

## Non-Goals

- 不为 qoder-cn 实现不存在的形态支持（CLI / QoderWake / SubagentStop subagent 捕获）；qoder-cn 未来出现新形态时另行扩展。
- 不改动 qoder / qoderwork 及其他既有产品 hook 的任何行为或路径。

## Options

### Option A: 完整 fork `hooks/qoder-cn/` 目录（遵循仓库既有模式）

以 qoder 为基线整体拷贝并适配（路径、DB 候选、tag、env 文件名、pyproject 名、README），新增 `deploy/installer.sh` 的 `qoder-cn` 检测分支。适配面小、风险低，但 `langfuse_hook.py` 出现第四份近似拷贝，后续修 bug 需多点同步。

### Option B: 抽取 qoder / qoderwork / qoder-cn 共享 hook 核心

将 `langfuse_hook.py` 参数化（产品名、路径、DB 候选），三产品共用一份实现。消除拷贝重复，但需要重构 qoder / qoderwork 已发布的 hook 逻辑与升级路径，改动已安装用户环境的行为基线，与"不随意修改既有路径、保持既有行为"的仓库约束冲突，收益是代码去重，无产品语义收益。

## Recommendation

采用 Option A：完整 fork `hooks/qoder-cn/`，以 **qoder**（非 qoderwork）为参考基线。

选择理由：`hooks/` 下"每产品一个独立完整目录"是仓库对全部七个产品的既有一致模式，fork 的适配边界清晰、不触碰任何已安装用户的既有环境；qoder-cn 的 hook 在宿主机执行且需要访问宿主机 SQLite DB，与 qoder 的运行形态一致，而 qoderwork 的 VM 双路径 entrypoint 与 env 硬链接是 QoderWork 特有机制，不适用。Option B 的重构风险与约束冲突超过去重收益。fork 带来的代码重复成本（与 qoder 的 `langfuse_hook.py` 约几千行近似重复）是本设计明确接受的 tradeoff。

### qoder-cn 的适配点（fork 内全部改动面）

1. **`hooks/qoder-cn/hooks/langfuse_hook.py`**（以 qoder 版为基线）：
   - `_find_qoder_db()` 的 DB 候选只指向 qoder-cn 自身的 SharedClientCache 路径（`~/.qoder-cn/shared_client/cache/db/local.db`，及 qoder-cn 的 macOS App Support 对应路径），**不得包含国际版路径**（`~/Library/Application Support/Qoder/...` 或 `~/.qoder/...`），否则同机共存时读取错误产品的 session 数据。
   - `STATE_DIR` 由 `~/.qoder/state` 改为 `~/.qoder-cn/state`（日志与 hook 状态文件随产品隔离）。
   - 默认 tag 由 `qoder` 改为 `qoder-cn`；logger 名等自标识同步改为 qoder-cn。
   - 其余逻辑（hook payload 解析、transcript 增量读取、turn 构建、SQLite enrichment 查询、OTLP 构建、langstash 投递）保持与 qoder 一致：qoder-cn 的 settings.json hooks schema、transcript 行格式、`chat_message` 表结构均已验证与 qoder 同族。
2. **`hooks/qoder-cn/hooks/langfuse-entrypoint.sh`**：采用 qoder 版形态（source `~/.agent-exporter-to-langfuse/config/qoder-cn.env` 后 `uv run`），不含 qoderwork 的 VM 分支。
3. **`hooks/qoder-cn/install.sh` / `uninstall.sh`**：基于 qoder 版，将 HOOK_DIR（`~/.qoder-cn/hooks/langfuse`）、SETTINGS_FILE（`~/.qoder-cn/settings.json`）、env 文件名（`qoder-cn.env`）、默认 tag（`qoder-cn`）、状态清理目录（`~/.qoder-cn/state`）、完成提示全部替换为 qoder-cn；凭据收集（交互/参数/`--upgrade` 复用）、`langstash_deliver` 拷贝、`uv sync`、settings.json 注册（`Stop` 与 `SubagentStop` 两事件、命令 `~/.qoder-cn/hooks/langfuse/langfuse-entrypoint.sh`、幂等跳过逻辑）与 qoder 保持一致。注册事件集沿用 qoder 的 `Stop` + `SubagentStop`，其权威依据是 Goal 1 的"行为与 qoder hook 等价"（qoder 即注册这两个事件）；"qoder-cn 当前不触发 `SubagentStop` 则该 hook 不执行、不影响既有行为"是本设计接受的非阻塞假设（未验证 qoder-cn 是否触发该事件），不作为必要性论据。
4. **`hooks/qoder-cn/hooks/pyproject.toml`**：project name 改为 `qoder-cn-langfuse-hook`（依赖与 qoder 一致）。
5. **文档同步（两层 README）**：新增 `hooks/qoder-cn/README.md`（按 qoder README 结构改写，说明 qoder-cn 为国内版 Desktop、配置目录 `~/.qoder-cn/`、无 CLI 形态、Desktop 形态的已知限制：subagent 不可用、token 来自 SharedClientCache DB）；同步更新仓库根 `README.md` 的 Supported Agents 表（新增 Qoder CN 行，指向 `hooks/qoder-cn/`，呈现方式与既有 Qoder / QoderWork 行一致）与架构 banner 的 agent 列表（AGENTS.md 文档同步 MUST）。
6. **`deploy/installer.sh`**：`is_agent_installed()` 增加 `qoder-cn)` 分支（检测 `~/.qoder-cn` 目录存在）。`install_detected_hooks` 经目录自动发现 `hooks/qoder-cn/install.sh`，无需其他改动；`deploy/package.sh` 基于 `git ls-files`，无需改动。
7. **验证方向**：补充 qoder-cn hook 的安装幂等与卸载清理的 E2E 验证（参照 `tests/e2e/` 既有产品安装测试模式），以及 hook 投递行为级验证（transcript 解析 / DB enrichment / langstash 投递，参照 claude-code hook 的 pytest 模式）；具体测试设计由下游 plan 决定。

### 兼容性与事实源

- qoder / qoderwork / 其他产品 hook 的行为与路径完全不变（Non-Goal 边界）；qoder-cn 是纯新增目录与一个 installer 检测分支，对既有用户零影响。
- 凭据与配置事实源沿用全局唯一模式：`~/.agent-exporter-to-langfuse/config/qoder-cn.env`（与 `qoder.env`、`qoderwork.env` 并列，产品间隔离）；hook 状态事实源为 `~/.qoder-cn/state/`（`langfuse_state.json` + FileLock，与 qoder 的状态机制一致）。
- 统一安装器的产品检测以 `~/.qoder-cn` 目录存在为准（与 qoderwork 以 `~/.qoderwork` 检测的先例一致），不依赖命令探测（qoder-cn 无 CLI 命令）。

### 后续（下游工作边界）

`hooks/qoder-cn/` 内部文件的产生方式（逐文件拷贝后最小改动，或脚本化生成）、精确的字面输出、测试文件清单与用例设计属于 spec / plan 层；本设计只固定上述行为边界与适配点。
