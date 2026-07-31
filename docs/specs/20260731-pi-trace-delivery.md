# 在 agent-exporter-to-langfuse 中支持 Pi 的 trace 投递

## Purpose

agent-exporter-to-langfuse 已为 claude-code、opencode、codex 等 agent 提供统一的 trace 采集：各 hook 构建 OTLP JSON，经共享包 langstash-deliver 三层投递（本地 exporter 缓冲 → Langfuse OTel 端点直推 → failed 日志）。Pi Coding Agent 目前不在支持列表中；独立仓库 pi-langfuse 虽已实现对 Pi 的完整 trace 跟踪，但它用 Langfuse SDK 直连 Langfuse，凭据与配置模式与本仓库不同。

本规范定义在本仓库新增 `hooks/pi/`：一个以 pi-langfuse 处理器逻辑为蓝本、重写为「事件驱动累积 + 一次性 OTLP JSON 投递」的 Pi extension，保留 pi-langfuse 已验证的观测建模与评分能力，并使配置、安装、检测、打包收敛到本仓库既有 hook 模式。

## Non-Goals

- 不修改 pi-langfuse 上游仓库与其 npm 包；它作为独立直连方案继续存在。
- 不为 exporter 的 ingest/sender/存储链路新增 Langfuse score 载荷类型。
- 不改变 exporter 现有的 OTLP JSON ingest 契约、pending/failed 存储结构与 Sender 转发机制。

## Decisions

- design_source: `docs/designs/20260731-pi-trace-delivery.md`

## Requirements

### R-1: 每次 Pi agent run 生成一条合法 OTLP JSON trace 并经 deliverTrace() 投递

- context: pi-langfuse 用 Langfuse SDK 实时创建观测对象；新 hook 改为在内存中累积 span 记录树，run 结束时一次性构建 OTLP JSON 并走本仓库统一的三层投递。exporter 的 `validate_otlp()` 要求每个 payload 含至少一个无 `parentSpanId` 的 root span、32 位 hex traceId、16 位 hex spanId 与纳秒字符串时间戳。
- must:
  - 新增 `hooks/pi/` Pi extension（`package.json` 经 `pi.extensions` 声明入口），监听 Pi 生命周期事件，在内存中按 session 累积当前 agent run 的观测记录。
  - 一次 agent run 对应一条 trace：root span 承载 agent 观测（trace 名 `pi-agent`，携带 prompt 输入与最终 assistant 输出）；每个 turn 为 root 的子 span；每次 provider 请求的 generation 与每次工具调用的 tool 观测为所属 turn 的子 span（无活跃 turn 时挂在 root 下）。
  - span 属性沿用仓库统一的 `langfuse.*` 映射约定：`langfuse.observation.type`（`generation`/`tool`/`span`）、`langfuse.observation.input`/`output`（JSON string）、`langfuse.observation.model.name`、`langfuse.observation.usage_details`（JSON string，键含 `input`/`output`/`cache_read_input_tokens`/`cache_creation_input_tokens`）、`langfuse.trace.name`、`session.id`（Pi session ID）、`user.id`（来自 `LANGFUSE_USER_ID`，缺省为 OS 用户名）、`langfuse.trace.tags`（来自 `LANGFUSE_TAGS`，含固定标签 `pi`）、`langfuse.trace.metadata`。
  - `agent_end` 时构建完整 OTLP JSON（`resourceSpans`→`scopeSpans`→`spans`，traceId/spanId 直接以 hex 字符串构造，时间戳为纳秒字符串），调用 langstash-deliver 的 `deliverTrace()` 完成三层投递；构建结果必须能通过 exporter 现行 `validate_otlp()` 校验。
  - hook 的任何内部异常（构建失败、投递失败、配置缺失）只允许记录日志并静默降级，不得中断或影响 Pi 的 agent 运行。
- must_not:
  - hook 不得依赖 Langfuse SDK（`langfuse`、`@langfuse/*`）或在 trace 投递上绕过 `deliverTrace()` 自建通道（R-5 的 score 直连通道除外）。
  - 不得沿用 pi-langfuse 的 trace 可见性轮询与 REST fallback 机制；投递失败的兜底由三层投递的 failed 日志与 exporter 恢复机制承担。
- verification:
  - 在 Pi 中执行一次含工具调用的 agent run，exporter `/ingest` 接受该 payload（202）且 pending 中出现一条 trace，其 span 层级为 root→turn→generation/tool、属性含上述 `langfuse.*` 键；构造非法输入（如缺 root span）时 hook 不影响 Pi 运行。

### R-2: 保留 pi-langfuse 的生命周期跟踪语义

- context: pi-langfuse 通过全套 Pi 事件实现了细粒度跟踪；新 hook 改为累积模型后这些语义必须等价保留，否则违背「保留原有跟踪能力」的目标。
- must:
  - generation 记录 TTFT：首次 `message_update` 时刻写入 `langfuse.observation.completion_start_time`；`message_end`/`turn_end` 时补全输出、model、usage、cost（如有）与 finishReason。
  - `after_provider_response` 携带错误（HTTP ≥400、error 字段）时，对应 generation 标记 level `ERROR` 并记录 statusMessage。
  - tool 观测按 `toolCallId` 关联 start/end 事件对，支持并发工具调用；工具失败时该观测标记 level `ERROR`、记录错误消息与执行时长。
  - `turn_end` 时若该 turn 内没有任何正常 generation 完成且存在 assistant 消息，合成一条 fallback generation（含输入、输出、usage）。
  - `session_compact` 事件在当前 trace 中生成一个标记 span。
  - 多 session 并发时状态相互隔离：一个 session 的观测、计数与配置状态不得泄漏到另一 session 的 trace 中。
  - `before_agent_start` 与 `agent_start`、tool/generation 的 start/end 事件对可能重复触发，处理必须幂等（不产生重复观测）。
  - `agent_end` 时若仍存在未关闭的 tool/generation 观测，先将其以 level `WARNING` 收尾后再进入 trace 发射流程（沿用 pi-langfuse 在正常结束路径上的悬挂观测收尾行为）。
- verification:
  - 构造含正常 generation、失败工具调用、无 generation turn 的 run，产出 trace 中分别可见 completion_start_time、ERROR level 的 tool 观测、fallback generation；并发两个 session 各自产出独立 trace 且计数互不污染。

### R-3: payload 截断、脱敏与隐私策略随 trace 构建生效

- context: pi-langfuse 在发送前对 payload 做整形（字符串截断、深度/数组/键数限制）、敏感信息脱敏（私钥、Bearer token、已知 token 模式、敏感字段名）与隐私策略过滤；新 hook 输出进入本地缓冲与 Langfuse，必须保留同等保护。
- must:
  - 所有写入 span 属性的 input/output/metadata 在构建 OTLP JSON 前经过整形与脱敏：字符串截断、嵌套深度/数组元素/对象键数/总节点数限制，默认限额沿用 pi-langfuse 现行常量，可经环境变量覆盖（随 `pi.env` 下发）。
  - 脱敏覆盖 pi-langfuse 现行模式：私钥块、Bearer token、已知 API token 格式（`sk-*`、`pk-lf-*` 等）、形如密钥赋值的字符串、敏感字段名（authorization/token/secret/password 等），替换为占位符。
- must_not:
  - 任何完整的 Langfuse API key 或其他被脱敏规则命中的凭据不得出现在投递的 trace 内容或 hook 日志中。
- verification:
  - 构造含超长字符串与伪造凭据（如 `sk-lf-` 前缀串）的工具输入，产出 trace 中该字符串被截断、凭据被替换为占位符。

### R-4: 中断与进程崩溃场景补发部分 trace

- context: 一次性发射模型下，run 未走到 `agent_end` 就终止会丢失整条 trace。Pi 的 `session_shutdown` 可覆盖优雅中断，但进程 crash/kill 没有任何事件通知，需要 checkpoint 机制兜底。
- must:
  - session 中断或 `session_shutdown` 时若存在未完成 run：关闭全部悬挂观测（tool/generation/turn 标记 cancelled 或 WARNING），root 观测 metadata 标记 `completed: false, cancelled: true`，随后立即构建并投递这条部分 trace。
  - 每次 `turn_end` 后将当前 run 的累积观测状态（含 run 开始即创建的 root span 记录）checkpoint 到 hook 自有的本地文件；`agent_end` 或中断发射成功后清除该 checkpoint。
  - extension 加载时若发现遗留 checkpoint，将其重建为标记 cancelled 的部分 trace 经 `deliverTrace()` 补发，补发后清除 checkpoint；重建的 trace 必须包含 root span（满足 `validate_otlp()`）。
- must_not:
  - 同一 run 不得既正常发射又被 checkpoint 补发为第二条 trace；checkpoint 读写失败不得影响正常 trace 流程。
- verification:
  - 在 turn 完成后强制终止 Pi 进程，重新启动 Pi 后 exporter 收到一条标记 cancelled 的部分 trace，且再次重启不产生重复补发；正常完成的 run 结束后 checkpoint 文件不存在。

### R-5: 评分能力经直连通道保留

- context: pi-langfuse 将聚合评分经 Langfuse `/api/public/ingestion` 的 score-create 接口发送，score 不是 OTel span 概念、无法进入 OTLP 投递链。设计决定 score 由 hook 直连发送，不经 exporter 缓冲。
- must:
  - run 结束（含中断发射）后，hook 将 trace 级评分 `tool_call_count`、`turn_count`、`total_tool_errors`、`tool_success_rate`（NUMERIC）与 `session_had_errors`（BOOLEAN）关联 traceId 批量 POST 到 `{LANGFUSE_BASE_URL}/api/public/ingestion`（score-create、Basic Auth，凭据取自与 Tier 2 相同的环境变量）；工具失败时追加 tool 级 `tool_is_error`（BOOLEAN）关联对应观测。
  - 上述聚合值同时镜像写入 root 观测的 metadata（沿用 pi-langfuse 既有行为），作为 score 发送失败时的兜底。
  - score 发送为 best-effort：网络层抛错（如 stale keep-alive socket 导致的 `connect EBADF`）以新连接原地重试一次；仍失败或收到 HTTP 错误时，仅以单行摘要日志（不含堆栈）记录后丢弃，不缓冲；hook README 明示该降级语义（score 可能缺失，metadata 镜像兜底）。
- must_not:
  - score 载荷不得进入 exporter 的 `/ingest`、pending/failed 存储或 Sender 转发路径；score 发送的失败或延迟不得阻塞、延后或影响 trace 投递结果。
- verification:
  - 正常 run 结束后 Langfuse 中该 trace 附有全部五项 trace 级 score，工具失败时对应观测附 `tool_is_error`；模拟 score 接口不可达时 trace 仍正常送达且 root 观测 metadata 含聚合值。

### R-6: 运行时配置以 pi.env 为唯一入口

- context: pi-langfuse 有 config.json、首跑交互配置与 `/langfuse-setup` 命令三种配置入口；本仓库 hook 统一用 install.sh 写入的 env 文件。双配置源会导致行为不可预测。
- must:
  - hook 启动时读取 `~/.agent-exporter-to-langfuse/config/pi.env`（格式与既有 `<agent>.env` 一致：每行 `export KEY="value"`），仅对 process.env 中尚未存在的变量注入（已有环境变量优先），支持的键为 `LANGFUSE_BASE_URL`/`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_USER_ID`/`LANGFUSE_TAGS`/`LANGSTASH_ENABLED`/`LANGSTASH_URL` 及 R-3 的限额覆盖变量。
  - env 文件缺失或凭据不全时 hook 静默降级（`deliverTrace()` 按其三层规则处理，最终落 failed 日志），不提示用户输入。
- must_not:
  - hook 不得读取 `~/.pi/agent/pi-langfuse/config.json`，不得注册 `/langfuse-setup`、`/langfuse-test` 等交互式配置命令，不得写入任何配置文件——运行时配置不得出现 pi.env 之外的第二来源。
- verification:
  - 仅存在 pi.env 时 hook 正常投递；删除 pi.env 且无环境变量时 Pi 正常运行、trace 落入 failed 日志；确认 hook 不注册任何 `/langfuse-*` 命令。

### R-7: install.sh / uninstall.sh 幂等安装与 pi-langfuse 互斥保护

- context: Pi 经 `~/.pi/agent/settings.json` 的 `packages` 字符串数组注册 extension 包（`pi install <source>` 写入、`pi remove` 移除，已实机验证）。新 hook 与 npm 版 pi-langfuse 同时启用会对同一 run 产生重复 trace。
- must:
  - `hooks/pi/install.sh`：交互采集凭据（或 `--upgrade` 模式复用既有 `pi.env`、命令行参数直传）写入 `~/.agent-exporter-to-langfuse/config/pi.env`；将构建产物 bundle 与声明 `pi.extensions` 入口的 `package.json` 拷贝到 `~/.pi/hooks/langfuse/`；以该目录绝对路径执行 `pi install` 完成注册。
  - install.sh 在注册前检查 `settings.json` 的 `packages` 中是否存在 `npm:pi-langfuse` 条目：交互模式下警告并要求确认后才继续；非交互模式（`-y`/`--upgrade`）下输出警告后继续安装。
  - `hooks/pi/uninstall.sh`：经 `pi remove` 反注册并删除 `~/.pi/hooks/langfuse/`；保留 `pi.env` 以供重装复用。
  - 两个脚本幂等：重复执行不产生重复 `packages` 条目、重复文件或残留状态；输出凭据时只显示前缀（前 12 字符）。
  - hook README 说明安装方式、与 npm 版 pi-langfuse 二选一的互斥关系及 R-5 的 score 降级语义。
- must_not:
  - 脚本不得在日志或终端输出完整 API key，不得使用 GNU 独有命令参数（须兼容 macOS 与 Linux）。
- verification:
  - 连续执行两次 install.sh 后 `packages` 中该路径条目恰有一条、目标目录文件完整；uninstall.sh 后条目与目录消失而 pi.env 保留；预置 `npm:pi-langfuse` 条目时安装输出互斥警告。

### R-8: exporter 检测、统一安装器与打包纳入 Pi

- context: exporter 通过 `_builtin_agent_definitions()` 探测各 agent 及其 hook 状态；`deploy/installer.sh` 统一批量安装 hook；`deploy/package.sh` 预构建 TS hook 产物。新增 agent 必须同步纳入这三处，否则 WebUI/Menubar 不显示 Pi、统一升级不覆盖 Pi。
- must:
  - `exporter/src/hook_state.py` 的 `_builtin_agent_definitions()` 新增 `pi` 条目：detection 为 `~/.pi` 路径与 `pi` 命令；hook 检测为 settingsPath `~/.pi/agent/settings.json` + markers 含安装路径稳定标识 `hooks/langfuse`。
  - `deploy/installer.sh` 的 `is_agent_installed()` 新增 `pi` 分支（`~/.pi` 目录或 `pi` 命令存在）；`hooks/pi/` 含 install.sh 使其自动进入 `list_known_agents()` 的批量安装与版本化升级流程。
  - `deploy/package.sh` 预构建 pi hook bundle（tsdown 内联 langstash-deliver，与 codex 方式一致），构建失败或产物缺失时打包报错退出。
  - 仓库顶层 `README.md` 的支持 agent 列表同步加入 Pi。
- must_not:
  - `pi` 条目不得配置 fileCheck——避免 bundle 文件残留但未注册时误报 installed；hook 状态判定的唯一依据是 settings.json 中的注册条目。
- verification:
  - 安装 pi hook 后 exporter `/stats` 的 hooks 中 `pi` 为 installed；手动从 `settings.json` 移除注册条目后探测转为 error；`package.sh` 产出的 tarball 含 pi hook 的 bundle 产物。
