# 实现计划：在 agent-exporter-to-langfuse 中支持 Pi 的 trace 投递

## Header
- source_spec: ../specs/20260731-pi-trace-delivery.md
- source_requirements_sha256: sha256:513de86d3d67619397b053419827c009054ecbae749406009d77f62fdee0a6fb
- accepted_debt: none
- status: ready
- external_review_policy: none

## Requirements Covered
- R-1: 每次 Pi agent run 生成一条合法 OTLP JSON trace 并经 deliverTrace() 投递
- R-2: 保留 pi-langfuse 的生命周期跟踪语义
- R-3: payload 截断、脱敏与隐私策略随 trace 构建生效
- R-4: 中断与进程崩溃场景补发部分 trace
- R-5: 评分能力经直连通道保留
- R-6: 运行时配置以 pi.env 为唯一入口
- R-7: install.sh / uninstall.sh 幂等安装与 pi-langfuse 互斥保护
- R-8: exporter 检测、统一安装器与打包纳入 Pi

## Planning Evidence
- codex hook 是既有的 TS bundle 范式：`hooks/codex/tsdown.config.ts` 用 `noExternal` 内联依赖、`target: node22`、产物 `dist/index.mjs`，`deploy/package.sh` 已有对应预构建段落——pi hook 复用同一构建方式，决定 phase-1 的产出形态与 phase-5 的打包任务写法。
- `scripts/run-tests.sh` 是仓库统一测试入口，按目录逐个执行各 hook 测试——新增 `hooks/pi` 的 vitest 段落属于 AGENTS.md「补充测试」义务，归入 phase-1。
- `hooks/opencode/hooks/langfuse-exporter.mjs` L73-L81 是 env 文件兜底注入的既有实现（`export KEY="value"` 正则、process.env 已有值优先），R-6 的 hook 侧行为按此模式实现。
- `exporter/src/hook_state.py` 的 `_check_hook_markers()` 中 fileCheck 命中即返回 True、优先于 markers——决定 R-8 任务中 `pi` 条目必须省略 fileCheck 字段（而非置空）。
- pi-langfuse 的 `src/state.ts`（AsyncLocalStorage session 隔离）、`src/redaction.ts`、`src/limits.ts`、`src/capture-policy.ts`、`src/utils.ts` 是 R-2/R-3 语义的参照实现，处理器逻辑在 `index.ts` 与 `src/handlers/*`；新 hook 移植其语义但以内存 span 记录替代 SDK 观测对象。
- 本机已验证：`~/.pi/agent/settings.json` 的 `packages` 为字符串数组（支持本地绝对路径条目），`pi install <source>`/`pi remove <source>` 增删条目——R-7 安装脚本据此实现，测试中以 PATH 注入的 pi stub 模拟。

## Phases with Tasks

### phase-1: Pi hook 运行时——观测累积、OTLP 构建与配置加载
- gate: shell: 在 `hooks/pi` 执行 `npx vitest run` 全部通过，且 `npm run build` 产出 `dist/index.mjs`

#### task-1: 建立 hooks/pi 包骨架、pi.env 配置加载与 session 隔离状态
- requirements: [R-1, R-6]
- outputs: [hooks/pi/package.json, hooks/pi/tsdown.config.ts, hooks/pi/index.ts, hooks/pi/src/config.ts, hooks/pi/src/state.ts]
- action: 新建 `hooks/pi/` TypeScript 包：入口 extension 默认导出函数、开发用 `package.json`（vitest + tsdown + typescript，无任何 `langfuse`/`@langfuse/*` 依赖）、tsdown 配置内联 langstash-deliver 源码产出单文件 `dist/index.mjs`。实现 pi.env 加载（仅注入 process.env 中不存在的键，支持 R-6 列出的键与 `PI_LANGFUSE_MAX_*` 限额变量；文件缺失静默跳过）与按 Pi sessionId 隔离的运行状态容器（移植 pi-langfuse `src/state.ts` 的 AsyncLocalStorage 模式）。extension 不注册任何命令。
- constraints:
  - Constraint: pi.env 路径固定为 `~/.agent-exporter-to-langfuse/config/pi.env`，解析正则与 opencode hook 一致（`^export\s+([A-Za-z_][A-Za-z0-9_]*)="(.*)"\s*$`）；此为全 hook 唯一运行时配置读取点，后续任务经此获取配置，不得另行读取配置文件。
  - Constraint: langstash-deliver 经源码相对路径 `../langstash-deliver/typescript/src/index.ts` 引入并由 tsdown 内联，不作为 npm 依赖声明。
- verification:
  - planned_test: vitest 覆盖：pi.env 存在时注入缺失变量且不覆盖已有环境变量；文件缺失时静默降级；两个并发 session 的状态互不可见。
  - source_scan: 命令 `grep -rn "registerCommand" hooks/pi/src hooks/pi/index.ts` 零命中（无例外）；命令 `python3 -c "import json;d=json.load(open('hooks/pi/package.json'));deps={**d.get('dependencies',{}),**d.get('devDependencies',{})};import sys;sys.exit(1 if any('langfuse' in k for k in deps) else 0)"` 退出码 0（无例外）。

#### task-2: payload 整形、脱敏与限额
- requirements: [R-3]
- outputs: [hooks/pi/src/redaction.ts, hooks/pi/src/limits.ts, hooks/pi/src/shape.ts]
- action: 移植 pi-langfuse 的整形与脱敏能力：字符串截断、深度/数组/键数/总节点数限制（默认常量沿用 pi-langfuse `src/constants.ts` 现值，`PI_LANGFUSE_MAX_*` 环境变量可覆盖），脱敏模式覆盖私钥块、Bearer token、已知 token 格式（`sk-*`、`pk-lf-*` 等）、密钥赋值形式与敏感字段名，命中替换为占位符。所有后续写入 span 属性的 input/output/metadata 必须经过该模块。
- verification:
  - planned_test: vitest 覆盖：超长字符串被截断带标记；`sk-lf-` 伪凭据、Bearer token、敏感字段名（authorization/secret 等）被替换为占位符；深度与节点数超限时安全降级；环境变量覆盖限额生效。

#### task-3: 生命周期事件处理器与观测记录累积
- requirements: [R-2]
- outputs: [hooks/pi/index.ts, hooks/pi/src/handlers/]
- action: 以 pi-langfuse `index.ts` 事件清单为准注册处理器，在内存 span 记录树上实现全部生命周期语义：`before_agent_start`/`agent_start` 幂等创建 root 记录（run 一开始即存在）；`turn_start`/`turn_end` 开闭 turn 记录；`before_provider_request` 开 generation、首次 `message_update` 记录 TTFT 时刻、`after_provider_response` 错误时标记 ERROR+statusMessage、`message_end`/`turn_end` 补全输出/model/usage/cost/finishReason；tool 记录按 toolCallId 关联 start/end 且并发安全，失败标记 ERROR、错误消息与时长；turn 内无正常 generation 且有 assistant 消息时合成 fallback generation；`session_compact` 生成标记 span 记录；`agent_end` 时将未关闭的 tool/generation 记录以 WARNING 收尾。所有事件处理异常仅记日志，不向 Pi 抛出。
- constraints:
  - Constraint: 事件对重复触发（before_agent_start+agent_start、tool_execution_start+tool_call、tool_result+tool_execution_end）必须幂等，判定键分别为「当前 run 已有 root」与 toolCallId。
- verification:
  - planned_test: vitest 以合成事件序列驱动：正常 run 产出含 TTFT 的 generation 记录；provider 4xx 使 generation 记为 ERROR；失败工具记录 ERROR+时长；无 generation turn 合成 fallback generation；重复事件对不产生重复记录；两个并发 session 的计数与记录互不污染；处理器抛错被吞掉且后续事件仍被处理。

#### task-4: OTLP JSON 构建与 deliverTrace 三层投递
- requirements: [R-1, R-2]
- outputs: [hooks/pi/src/otlp.ts, hooks/pi/src/emit.ts]
- action: `agent_end` 时把当前 run 的记录树构建为一条 OTLP JSON（`resourceSpans`→`scopeSpans`→`spans`；traceId 32 位 hex、spanId 16 位 hex 直接以随机字节 hex 构造；时间戳纳秒字符串；scope name `agent-exporter-to-langfuse`），span 层级为 root→turn→generation/tool（无活跃 turn 时挂 root），属性按 R-1 列出的 `langfuse.*` 键写入（含 `langfuse.observation.completion_start_time`、`langfuse.observation.usage_details`、trace 级 name/`session.id`/`user.id`/tags/metadata；tags 含固定 `pi`；user.id 缺省 OS 用户名），构建后调用内联的 `deliverTrace()`。构建或投递失败仅记日志。
- constraints:
  - Constraint: 构建结果必须满足 exporter `validate_otlp()` 的全部结构约束（root span 无 parentSpanId、hex 长度、纳秒字符串、attributes 为 KeyValue 数组、endTime≥startTime）；这是 pending 缓冲与 Sender 转发的前置契约。
  - Constraint: 不实现任何 trace 可见性轮询或 REST fallback；投递失败的最终兜底是 deliverTrace 内部的 failed 日志。
- verification:
  - planned_test: vitest 断言构建产物：含且仅含一个无 parentSpanId 的 root span；层级 parentSpanId 链正确；`langfuse.observation.usage_details` 为含 input/output/cache 键的 JSON string；traceId/spanId 满足 `^[0-9a-f]{32}$`/`^[0-9a-f]{16}$`；时间戳为纯数字字符串且 end≥start；deliverTrace 被以该产物调用（注入 fetchFn 桩验证 Tier 1 POST 到 `/ingest`）。
  - source_scan: 命令 `grep -rn "api/public/traces\|/api/public/ingestion" hooks/pi/src hooks/pi/index.ts` 允许的唯一例外是 R-5 score 模块文件（`hooks/pi/src/score.ts`），其余零命中。

### phase-2: 中断与进程崩溃的部分 trace 补发（R-4）

#### task-5: shutdown 发射、turn_end checkpoint 与启动补发
- requirements: [R-4]
- outputs: [hooks/pi/src/checkpoint.ts, hooks/pi/index.ts]
- action: session 中断/`session_shutdown` 且存在未完成 run 时：悬挂 tool/generation/turn 记录标记 cancelled 或 WARNING、root metadata 标记 `completed: false, cancelled: true`，随即构建并投递部分 trace，成功后清除 checkpoint。每次 `turn_end` 后将当前 run 记录树（含 root）序列化到 checkpoint 文件；`agent_end` 正常发射后删除。extension 加载时若发现遗留 checkpoint，重建为标记 cancelled 的部分 trace 经 `deliverTrace()` 补发并删除文件。checkpoint 读写任何异常仅记日志，不影响正常 trace 流程。
- constraints:
  - Constraint: checkpoint 文件位于 `~/.agent-exporter-to-langfuse/data/pi-checkpoints/<sessionId>.json`（不放 `~/.pi/hooks/langfuse/`，避免 uninstall 删目录时连带丢数据）；写入采用临时文件+rename，防止半写文件被补发。
  - Constraint: 防双发的判定：正常/中断发射成功后立即删除 checkpoint 且在同一 run 内不再补发；补发只发生在 extension 加载时刻，补发前先读后删。
- verification:
  - planned_test: vitest 覆盖：turn_end 后 checkpoint 文件存在且含 root 记录；agent_end 发射后文件被删除；模拟遗留 checkpoint 时加载即补发一条 cancelled 部分 trace（deliverTrace 桩收到含 root span 的合法 OTLP JSON）且文件被删、二次加载不再补发；checkpoint 目录不可写时正常 trace 流程不受影响；shutdown 中断发射的 trace 中悬挂记录带 cancelled/WARNING 标记。

### phase-3: score 直连通道（R-5）

#### task-6: run 结束后 best-effort 发送评分并镜像 metadata
- requirements: [R-5]
- outputs: [hooks/pi/src/score.ts, hooks/pi/src/emit.ts]
- action: trace 发射（含中断发射）后，计算 `tool_call_count`、`turn_count`、`total_tool_errors`、`tool_success_rate`（NUMERIC）与 `session_had_errors`（BOOLEAN），连同每个失败工具的 `tool_is_error`（BOOLEAN，关联对应观测 spanId）组成 score-create 批量，POST 到 `{LANGFUSE_BASE_URL}/api/public/ingestion`（Basic Auth，凭据取自与 Tier 2 相同的环境变量）。同一组聚合值镜像写入 root 记录 metadata（在 OTLP 构建前合入）。发送失败仅记日志丢弃，不重试不缓冲；发送在 deliverTrace 返回后进行，不阻塞 trace 投递结果。
- constraints:
  - Constraint: score 关联的 traceId/observationId 必须与已投递 OTLP trace 的 traceId/spanId 一致（Langfuse OTel 端点以 span hex ID 作为观测 ID）。
  - Constraint: score 通道是 R-1 must_not「不绕过 deliverTrace」的唯一豁免面；凭据缺失时直接跳过发送。
- verification:
  - planned_test: vitest 覆盖：正常 run 后 fetch 桩收到 5 项 trace 级 score（dataType 正确、traceId 与投递 trace 一致）；含失败工具时追加 tool_is_error 且 observationId 等于该 tool span 的 spanId；fetch 桩抛错时 deliverTrace 结果不受影响且 root metadata 含全部聚合值；凭据缺失时无 score 请求发出。

### phase-4: 安装、卸载与互斥保护（R-7）

#### task-7: install.sh / uninstall.sh 与 hook README
- requirements: [R-7]
- outputs: [hooks/pi/install.sh, hooks/pi/uninstall.sh, hooks/pi/README.md, tests/e2e/test_pi_hook_install.sh]
- action: install.sh 按 opencode 模式交互采集凭据（支持 `--secret-key/--public-key/--base-url/--user-id/--tags/-y/--upgrade`，`--upgrade` 复用既有 pi.env）写入 `~/.agent-exporter-to-langfuse/config/pi.env`（`export KEY="value"` 格式，含 LANGSTASH_ENABLED/URL，tags 确保含 `pi`）；将 `dist/index.mjs` 与声明 `pi.extensions` 入口的安装用 package.json 拷贝到 `~/.pi/hooks/langfuse/`；注册前用 python3 解析 `~/.pi/agent/settings.json` 检查 `packages` 中的 `npm:pi-langfuse` 条目，交互模式要求确认、非交互模式警告后继续；以目录绝对路径执行 `pi install`（已注册则跳过）。uninstall.sh 执行 `pi remove` 并删除 `~/.pi/hooks/langfuse/`，保留 pi.env。README 说明安装方式、与 npm 版 pi-langfuse 二选一及 score best-effort 降级语义。新增 shell E2E 在沙箱 HOME（PATH 注入 pi stub：install/remove 读写 stub settings.json）验证全流程。
- constraints:
  - Constraint: 凭据回显只允许前 12 字符前缀；脚本不使用 GNU 独有参数（macOS/Linux 兼容）；dist 缺失时按 opencode 模式现场构建。
  - Constraint: 幂等判定以 settings.json `packages` 中该绝对路径条目是否已存在为准；重复 install 不追加条目、不重复写 pi.env 之外的任何 profile。
- verification:
  - planned_test: shell E2E `tests/e2e/test_pi_hook_install.sh`：连续两次 install 后 stub settings.json 中该路径条目恰一条、`~/.pi/hooks/langfuse/` 含 bundle 与 package.json、pi.env 内容正确；uninstall 后条目与目录消失而 pi.env 保留；预置 `npm:pi-langfuse` 条目时 `-y` 模式输出互斥警告并继续；输出中不含完整 secret key（对捕获输出 grep 完整 key 零命中）。

### phase-5: exporter 检测、统一安装器与打包（R-8）
- gate: shell: `bash scripts/run-tests.sh` 全部通过（含新增 hooks/pi vitest 段落），且 `bash deploy/package.sh --output-dir /tmp/pi-plan-pkg` 成功并且 tarball 内含 `hooks/pi/dist/index.mjs`

#### task-8: hook_state 新增 pi 条目与 exporter 回归
- requirements: [R-8]
- outputs: [exporter/src/hook_state.py, exporter/tests/test_hook_state.py]
- action: `_builtin_agent_definitions()` 新增 `pi` 条目：`detection` 为 `~/.pi` 路径与 `pi` 命令；`hook` 为 settingsPath `~/.pi/agent/settings.json` + markers `["hooks/langfuse"]`；不含 fileCheck 键。补充 pytest 覆盖新条目的探测行为。
- constraints:
  - Constraint: 不设 fileCheck 是规范 must_not——`_check_hook_markers()` 中 fileCheck 命中即返回 True，会使 bundle 残留但未注册时误报 installed。
- verification:
  - planned_test: pytest：settings.json 含 `hooks/langfuse` 路径条目时 `_check_hook_markers` 为 True，移除条目后为 False（即使 bundle 文件存在）；`probe_hook_states` 对 installed→条目缺失转 error。
  - existing_test: `cd exporter && uv run pytest -q` 全部通过（无回归）。

#### task-9: installer.sh、package.sh、run-tests.sh 与顶层 README 纳入 pi
- requirements: [R-8]
- outputs: [deploy/installer.sh, deploy/package.sh, scripts/run-tests.sh, README.md]
- action: `is_agent_installed()` 新增 `pi` 分支（`~/.pi` 目录或 `pi` 命令存在）；`package.sh` 仿照 codex 段落预构建 `hooks/pi`（npm install + build，校验 `dist/index.mjs` 存在，失败即退出非零）；`scripts/run-tests.sh` 加入 hooks/pi 的 vitest 段落；顶层 README 支持 agent 列表加入 Pi（含安装说明入口）。`hooks/pi/` 已含 install.sh，自动进入 `list_known_agents()` 流程，无需改动该函数。
- verification:
  - shell: `bash deploy/package.sh --output-dir /tmp/pi-plan-pkg` 退出码 0，`tar tzf` 输出含 `hooks/pi/dist/index.mjs` 与 `hooks/pi/install.sh`。
  - inspect: `is_agent_installed` 的 `pi` 分支与 README agent 列表更新符合规范表述。

## Verification
- shell: `bash scripts/run-tests.sh` 全部通过（覆盖 exporter pytest 回归与 hooks/pi vitest 全量）。
- planned_test: shell E2E `tests/e2e/test_pi_hook_install.sh`：连续两次 install 后 stub settings.json 中该路径条目恰一条、`~/.pi/hooks/langfuse/` 含 bundle 与 package.json、pi.env 内容正确；uninstall 后条目与目录消失而 pi.env 保留；预置 `npm:pi-langfuse` 条目时 `-y` 模式输出互斥警告并继续；输出中不含完整 secret key（对捕获输出 grep 完整 key 零命中）。
- planned_test: 集成检查：vitest 产出的样例 OTLP JSON 经 `python3 -c` 调用 `exporter/src/ingestor.py` 的 `validate_otlp()` 校验通过（hook 构建产物满足 ingest 契约的跨语言证明）。
- shell: `bash deploy/package.sh --output-dir /tmp/pi-plan-pkg` 退出码 0，tarball 含 pi hook 构建产物。
