# Spec: qoder-cn Hook 投递（Langfuse 观测）

## Purpose

本仓库已为 claude-code、codex、cursor、opencode、pi、qoder、qoderwork 七个产品提供 Langfuse hook 投递：每个产品在 `hooks/<产品名>/` 下拥有独立的 install.sh / uninstall.sh / README.md / hooks/langfuse_hook.py / hooks/langfuse-entrypoint.sh / hooks/pyproject.toml，安装后由产品的 hook 事件机制触发，会话数据经 `langstash-deliver` 共享投递链路（langstash 本地缓冲 → 直推 Langfuse → 失败日志回退）送达 Langfuse。

qoder-cn 是 Qoder 国内版 Desktop 应用（Electron `com.qodercn.app.stable`，配置目录 `~/.qoder-cn/`），当前没有任何 Langfuse 观测能力：其会话数据（turns、generations、tool calls、token/model usage）无法投递。经设计评审（见 `design_source`）选定方案：以 qoder 的实现为基线完整 fork 出 `hooks/qoder-cn/`，仅适配 qoder-cn 的路径、命名与数据源。本 Spec 将该方案落为可验收的行为契约：运行时投递行为、安装/卸载生命周期、统一安装器集成与文档同步。

qoder-cn 与 qoder 的既有事实（设计阶段已验证，支撑上述契约的适配面）：两者共用同一套 hook 事件机制（settings.json 的 hooks schema 同族，`Stop` 事件均可注册 hook）、同一 transcript 布局（`~/.qoder-cn/projects/<project-dir>/<session>.jsonl`，行为与 qoder 的 `~/.qoder/projects/` 同构）、以及同一 `chat_message` 表结构的 SharedClientCache SQLite DB（qoder-cn 位于 `~/.qoder-cn/shared_client/cache/db/local.db`）。qoder-cn 当前无 CLI 形态、settings.json 中不存在 `SubagentStop` 事件。

## Non-Goals

- qoder-cn 的 CLI / QoderWake 形态支持与 subagent（`SubagentStop` 转录）捕获：qoder-cn 当前仅有 Desktop 形态，这些能力不在本 Spec 范围内；产品未来出现新形态时另行扩展。

## Decisions

- design_source: `docs/20261119-qoder-cn-hook-delivery/design.md`

## Requirements

### R-1: qoder-cn 会话数据投递到 Langfuse 并包含 token/model 数据

- context: qoder-cn hook 以 qoder 版 `langfuse_hook.py` 为基线 fork，运行于宿主机并由 qoder-cn 的 hook 事件触发；其数据源（transcript、SharedClientCache DB）与状态文件均为 qoder-cn 自有路径。同机可能并存国际版 Qoder 的同名数据结构，两者 session 数据完全独立。
- must:
  - 当 qoder-cn 触发已注册的 `Stop` hook 时，hook 读取事件 payload 中的 transcript 路径，增量解析该会话 transcript，构建 turns / generations / tool calls 并经 `langstash-deliver` 既有投递链路投递到 Langfuse；解析、turn 构建、投递与回退行为与 qoder hook 一致。
  - token/model enrichment 从 qoder-cn 自身的 SharedClientCache SQLite DB 匹配（候选路径为 `~/.qoder-cn/shared_client/cache/db/local.db` 与 qoder-cn 的 macOS App Support 对应路径）；DB 不存在或无匹配数据时投递正常完成，token/model 数据缺失。
  - 投递的 trace 默认携带 tag `qoder-cn`（`LANGFUSE_TAGS` 环境变量的覆盖与追加行为与 qoder 一致）。
  - 增量投递状态与日志写入 `~/.qoder-cn/state/`（状态文件 + 文件锁机制与 qoder 一致，hook 日志自标识为 qoder-cn）。
- must_not:
  - DB 候选不得包含国际版 qoder 的任何路径（`~/Library/Application Support/Qoder/...`、`~/.local/share/Qoder/...`、`~/.qoder/...`）；hook 不得读取或写入国际版 qoder 的状态目录（`~/.qoder/state`）。
  - 实现不得修改 `hooks/qoder/`、`hooks/qoderwork/` 及其他既有产品目录的任何文件；本变更对既有文件的修改仅限 `deploy/installer.sh` 新增 `qoder-cn` 检测分支（R-4）与根 `README.md` 的文档新增（R-5）。
- verification:
  - 在装有 qoder-cn（存在真实 transcript 与 `chat_message` 数据）的环境触发 `Stop`：Langfuse 收到该会话的 trace，包含 turns / generations / tool calls 与来自 qoder-cn DB 的 token/model 数据，tag 含 `qoder-cn`。
  - 在 `LANGFUSE_TAGS` 设有额外标签的环境触发 `Stop`：trace 的 tag 集合按 qoder 既有语义包含默认 tag `qoder-cn` 与用户标签。
  - 在 langstash 服务不可用的环境触发 `Stop`：trace 仍按 qoder 既有回退语义投递到达 Langfuse。
  - 同机存在国际版 Qoder DB 且两 DB 的 session 数据不同时，qoder-cn trace 的 token/model 数据须与 qoder-cn DB 的内容对应，而非国际版 DB。
  - DB 缺失的环境触发 `Stop`：trace 正常投递但缺 token/model 数据。
  - 同一会话多次触发 `Stop`：仅投递新增内容，状态文件位于 `~/.qoder-cn/state/`。

#### Scenario: 一轮对话结束后的投递

用户在 qoder-cn 中完成一轮包含工具调用的对话并停止；`Stop` hook 触发，Langfuse 中出现该轮的 trace（tag 含 `qoder-cn`），其中 assistant 消息的 token 用量与模型名来自 qoder-cn 的 SharedClientCache DB 中该 session 的记录。

### R-2: 幂等安装 qoder-cn hook 并注册到 settings.json

- context: 安装机制完全沿用 qoder install.sh 的既有流程（凭据收集、文件拷贝、依赖安装、settings.json 注册、env 文件写入），仅将产品路径与命名适配为 qoder-cn；仓库约束要求 install.sh 幂等、可重复执行。
- must:
  - install.sh 将 `langfuse_hook.py`、`langfuse-entrypoint.sh`、`langstash_deliver` 共享包与 `pyproject.toml`（project name `qoder-cn-langfuse-hook`，依赖与 qoder 一致）安装到 `~/.qoder-cn/hooks/langfuse/` 并完成 Python 依赖安装；entrypoint 采用 qoder 版形态（加载 `~/.agent-exporter-to-langfuse/config/qoder-cn.env` 后以 uv 运行 hook，不含 VM 分支）。
  - 凭据与可选项（base URL、public key、secret key、user id、tags）的收集行为与 qoder 既有安装一致（交互默认值 / 命令行参数 / 环境变量 / `--upgrade` 模式复用既有 env 文件），并写入 `~/.agent-exporter-to-langfuse/config/qoder-cn.env`（`LANGFUSE_*` + `LANGSTASH_ENABLED` + `LANGSTASH_URL`，默认 tag 追加 `qoder-cn`）。
  - 向 `~/.qoder-cn/settings.json` 注册 `Stop` 与 `SubagentStop` 两个 hook 事件（命令指向 `~/.qoder-cn/hooks/langfuse/langfuse-entrypoint.sh`）；注册幂等：目标事件中已存在命令含 `langfuse` 的注册项时不重复添加。
  - 已存在旧版 hook 文件时的覆盖确认行为，以及安装过程用户可见输出中的产品名与路径，均与 qoder 既有安装行为一致（相应为 qoder-cn）。
- must_not:
  - 安装不得读取、覆盖或删除 qoder、qoderwork 等其他产品的 hook 文件、settings 注册项、env 文件或状态目录。
- verification:
  - 全新环境执行安装：`~/.qoder-cn/hooks/langfuse/` 下 hook 脚本、entrypoint、`langstash_deliver` 与 `pyproject.toml` 就位且依赖可用；`~/.qoder-cn/settings.json` 的 `Stop` 与 `SubagentStop` 各存在一条指向 entrypoint 的注册项；env 文件生成且默认 tag 含 `qoder-cn`。
  - 连续重复执行安装（含 `--upgrade` 模式）：注册项不重复；确认覆盖后已安装文件更新为当前版本、不残留旧版本内容。
  - 在已存在旧版 hook 文件的环境执行安装：覆盖确认行为与 qoder 既有安装一致（拒绝确认则跳过 hook 文件拷贝，确认后拷贝更新）。
  - 安装前后对照：qoder 与 qoderwork 的用户侧 hook、settings 注册、env 文件与状态完全不变。

### R-3: 卸载 qoder-cn hook 并完整清理其注册与状态

- context: 卸载机制沿用 qoder uninstall.sh 的既有流程，仅将清理目标适配为 qoder-cn 路径；仓库约束要求 uninstall.sh 幂等。
- must:
  - 删除 `~/.qoder-cn/hooks/langfuse/` 目录，并从 `~/.qoder-cn/settings.json` 移除命令含 `langfuse` 的 `Stop` / `SubagentStop` 注册项，保留其他注册项。
  - 清理 `~/.qoder-cn/state/` 下的 langfuse 日志与状态文件；env 文件 `~/.agent-exporter-to-langfuse/config/qoder-cn.env` 默认保留，`--purge` 时删除。
  - 其余清理行为（空 profile 目录与 shell loader 行清理等）与 qoder 既有卸载机制一致；卸载在未安装状态下执行不报错、无副作用，卸载后重装可恢复完整功能。
- must_not:
  - 卸载不得移除或修改 qoder、qoderwork 等其他产品与 qoder-cn 内第三方 hook 的任何注册项、文件或 env 文件。
- verification:
  - 安装后卸载：hook 目录、settings.json 中的 langfuse 注册项、`~/.qoder-cn/state/` 状态文件均被清除；qoder-cn 内第三方 hook 注册项保留；env 文件默认保留。
  - `--purge` 卸载：env 文件被删除；且当 `~/.agent-exporter-to-langfuse/config/` 再无其他产品 env 文件时，配置目录被移除、shell rc 中的 agent-exporter-to-langfuse loader 行被清理（与 qoder 既有卸载行为一致）。
  - 未安装状态执行卸载：无报错、无副作用；卸载后重装，R-1 / R-2 的行为完整恢复。

### R-4: 统一安装器检测 qoder-cn 并随发布包分发

- context: `deploy/installer.sh` 经 `is_agent_installed()` 的 case 分支判断各产品是否已安装，再对 `hooks/<产品名>/install.sh` 目录自动发现并执行安装；`deploy/package.sh` 以 `git ls-files` 打包仓库。qoder-cn 无 CLI 命令，检测只能依赖配置目录。
- must:
  - `is_agent_installed()` 对 `qoder-cn` 以 `~/.qoder-cn` 目录存在判定为已安装。
  - 统一安装 / 升级流程对检测到 qoder-cn 的机器执行 `hooks/qoder-cn/install.sh --upgrade -y`，与其他产品的处理方式一致。
  - 发布产物中包含完整的 `hooks/qoder-cn/` 目录（install.sh、uninstall.sh、README.md 与 hooks 内容）。
- must_not:
  - 不得改变其他产品的检测分支与统一安装行为。
- verification:
  - 存在 `~/.qoder-cn` 的环境运行统一安装：qoder-cn hook 被安装且注册生效；不存在该目录的环境：qoder-cn 被跳过，其他产品的检测与安装行为不变。
  - 发布产物中 `hooks/qoder-cn/` 目录完整存在。

### R-5: 同步两层 README 文档

- context: 仓库约束要求文档与实际行为一致；根 `README.md` 的 Supported Agents 表与架构 banner 列举全部受支持 agent，各产品目录下有独立 README。
- must:
  - 根 `README.md` 的 Supported Agents 表新增 Qoder CN 行（指向 `hooks/qoder-cn/`，呈现方式与既有 Qoder / QoderWork 行一致），架构 banner 的 agent 列表同步加入 qoder-cn。
  - `hooks/qoder-cn/README.md` 按 qoder 产品 README 的既有结构说明：安装步骤、配置变量、产品形态（国内版 Desktop、配置目录 `~/.qoder-cn/`、无 CLI 形态）与 Desktop 形态的已知限制（subagent 不可用、token/model 来自 SharedClientCache DB）。
- verification:
  - 两层 README 的描述与实际行为一致：安装路径、注册事件、默认 tag、产品形态与已知限制的说明与 R-1–R-4 的契约对应；根 README 新增行与 banner 与 `hooks/qoder-cn/` 目录的存在对应。
