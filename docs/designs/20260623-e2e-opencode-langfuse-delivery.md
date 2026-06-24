# E2E 案例：安装 → OpenCode 对话 → Langfuse 数据投递验证

## Problem

当前 E2E 测试套件 (`tests/e2e/test_versioned_upgrade.sh`) 仅覆盖安装/卸载/升级/回滚等运维操作，**未覆盖核心数据投递流程**：即安装 agent-exporter-to-langfuse 后，通过 AI Agent 产生对话数据，验证数据成功到达 Langfuse 后端。

Aone 需求 #83454976 要求补充 E2E 案例，具体流程为：**安装 agent-exporter-to-langfuse → 和 opencode 对话 → 检查数据被投递到 langfuse**。

这意味着 E2E 必须验证从 OpenCode hook 捕获 session 数据 → langstash-deliver 投递 → langstash 本地缓冲 → Langfuse 后端接收的完整链路。

## Context

- **数据链路**：OpenCode hook (`langfuse-exporter.mjs`) 在 `session.idle` 事件时构建 OTLP JSON，通过 `langstash-deliver` 投递；`langstash-deliver` 三级回退：langstash `/ingest` → 直接 Langfuse OTel `/api/public/otel/v1/traces` → 本地 failed log
- **langstash 服务**：FastAPI 服务，监听 `http://127.0.0.1:5288`，`/ingest` 接口接受 OTLP JSON，`/stats` 返回投递统计
- **现有 E2E**：仅覆盖 installer.sh 的运维操作（安装、卸载、升级、回滚、SHA-256 校验等），不涉及数据投递
- **test-service**：已设计完成（`docs/designs/20260619-e2e-testing-service.md`），提供 job 编排基础设施，但不含具体 E2E 测试内容
- **OpenCode CLI**：基于 Bun 运行时的交互式 CLI/TUI 工具，hook 以 JavaScript plugin 方式运行在 OpenCode 进程内，访问 `session.idle` 事件和 SDK Client API
- **非阻塞假设**：
  - E2E 测试环境有 Docker 可用（用于启动自托管 Langfuse）
  - E2E 测试环境有 Node.js 18+、Python 3.11+、uv、pnpm
  - OpenCode CLI 可通过某种方式接受非交互式输入（stdin pipe 或 `--prompt` 参数）完成至少一轮对话
  - Module 4 预期为 manual-only；若 OpenCode CLI 支持非交互式对话则升级为全自动，否则保持 manual-only

## Goals

- 验证完整数据投递链路：OpenCode hook → langstash-deliver → langstash → Langfuse
- E2E 测试可被 test-service 编排执行，也可独立手动运行
- 每个测试模块独立运行判定：Module 1–3 完全独立，Module 4 为复合模块（依赖 Module 2+3 前置），但 Module 4 失败不阻塞其他模块
- 测试脚本兼容 macOS 和 Linux

## Non-Goals

- **产品行为**：不测试 Langfuse UI 上的 trace 展示效果，只验证 API 层面数据存在
- **兼容/迁移**：不覆盖旧 git 布局迁移到新版本后的数据投递（已有 E2E-6 覆盖迁移本身）
- **Runtime**：不测试 langstash 的高并发、大批量投递性能
- **相邻 workflow**：不修改 test-service 的 job 编排逻辑；test-service 只负责调度本 E2E 脚本的执行
- **未来扩展**：不覆盖其他 Agent（claude-code、qoder 等）的数据投递 E2E；本设计仅聚焦 OpenCode，其他 Agent 的 E2E 可后续按同样模式补充

## Options

### Option A: Docker 全链路 E2E

- best_when: 需要验证从 hook 到 Langfuse 的完整真实投递
- rejected_because: 需要 Docker 基础设施；OpenCode 非交互式对话能力不确定；链路过长，一处失败全部失败

### Option B: 合成 trace 注入 langstash 验证

- best_when: 只需验证 langstash 的投递能力，无需外部依赖
- rejected_because: 不覆盖 OpenCode hook → langstash-deliver 这段链路；不验证数据实际到达 Langfuse

### Option C: 模块化混合方案（推荐）

- best_when: 各模块独立可验证，失败不互相阻塞；合成 trace 覆盖 langstash→Langfuse 链路；hook 安装验证覆盖 hook→langstash-deliver 配置链路；可选的真实 OpenCode 对话覆盖完整链路
- rejected_because: Module 4（真实 OpenCode 对话）可能因 CLI 不支持非交互而降级为 manual-only

## Recommendation

选择 **Option C：模块化混合方案**。理由：

1. **最小充分覆盖**：Module 2（合成 trace → langstash → Docker Langfuse）验证了数据投递的核心路径；Module 3 验证了 OpenCode hook 安装正确性；两者组合已覆盖"安装 → hook 配置 → 数据可投递"的关键链路
2. **模块独立性**：Module 1–3 独立运行、独立判定，任一失败不影响其他模块通过；Module 4 是复合模块，依赖 Module 2 的 Docker Langfuse 和 Module 3 的 hook 安装作为前置，但 Module 4 失败不影响 Module 1–3 的判定
3. **渐进增强**：Module 4 以 manual-only 为预期基线；若后续发现 OpenCode CLI 支持非交互式输入则升级为全自动
4. **明确不做什么**：不做 Langfuse UI 验证、不做性能测试、不做其他 Agent 的 E2E

### 模块划分

| 模块 | 覆盖链路 | 外部依赖 | 自动化程度 |
|------|---------|----------|-----------|
| Module 1: 安装 + langstash 健康 | installer → langstash 启动 | 无 | 全自动（已有 E2E-1 覆盖大部分，补充 langstash health check） |
| Module 2: 合成 trace → Langfuse 投递 | langstash `/ingest` → sender → Docker Langfuse API | Docker | 全自动 |
| Module 3: OpenCode hook 安装验证 | hook install → plugin 注册 → env 配置 → langstash-deliver 拷贝 | 无 | 全自动 |
| Module 4: 真实 OpenCode 对话 → Langfuse | OpenCode hook → langstash-deliver → langstash → Langfuse | Docker + OpenCode CLI | manual-only（预期）；若 CLI 支持非交互则可全自动 |

### Module 2 详细设计

1. 启动 Docker Langfuse 容器（`langfuse/langfuse:latest`），暴露端口，创建测试 project
2. 安装 agent-exporter-to-langfuse，配置指向 Docker Langfuse 的 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`
3. 启动 langstash 服务，等待 health check 通过
4. 构造一个最小合法 OTLP JSON trace（包含 root span + generation span），通过 `curl POST /ingest` 发送给 langstash
5. 等待 langstash sender 将数据投递到 Langfuse（轮询 `/stats` 直到 `total_sent >= 1`）
6. 通过 Langfuse API `/api/traces` 查询，验证 trace 存在且包含预期属性（trace name、model、token usage）
7. 清理：停止 langstash，停止 Docker Langfuse，purge 安装

### Module 3 详细设计

1. 安装 agent-exporter-to-langfuse（使用本地 package）
2. 执行 `hooks/opencode/install.sh --secret-key --public-key --base-url -y`
3. 验证：
   - `~/.config/opencode/plugins/langfuse-exporter.mjs` 存在
   - `~/.config/opencode/plugins/langstash-deliver/index.js` 存在
   - `~/.config/opencode/opencode.json` 的 `plugin` 数组包含 `"./plugins/langfuse-exporter.mjs"`
   - `~/.agent-exporter-to-langfuse/config/opencode.env` 包含 `LANGFUSE_SECRET_KEY`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_BASE_URL`、`LANGSTASH_ENABLED`、`LANGSTASH_URL`
4. 执行 `hooks/opencode/uninstall.sh`
5. 验证：
   - `~/.config/opencode/plugins/langfuse-exporter.mjs` 不存在
   - `~/.config/opencode/opencode.json` 的 `plugin` 数组不包含 langfuse-exporter
   - `~/.agent-exporter-to-langfuse/config/opencode.env` 不存在
6. 清理

### Module 4 详细设计

前置条件：Module 2（Docker Langfuse 运行中）+ Module 3（OpenCode hook 已安装）。Module 4 失败不影响 Module 1–3 的通过判定。

1. 启动 Docker Langfuse + 安装 exporter + 启动 langstash（同 Module 2）
2. 安装 OpenCode hook（同 Module 3）
3. 尝试通过 OpenCode CLI 发送一轮对话：
   - 方案 A：`opencode --prompt "hello"` 或类似非交互模式
   - 方案 B：通过 stdin pipe 输入：`echo "hello" | opencode`
   - 方案 C：使用 opencode session API（如果存在）
   - 方案 D（fallback）：标记为 manual-only，在测试输出中打印手动操作指引
4. 等待 `session.idle` 事件触发 hook 处理（等待若干秒）
5. 通过 Langfuse API 查询 traces，验证至少有一条 trace，且包含：
   - trace name 包含 "OpenCode"
   - 有 generation span，model 字段非空
   - input 包含用户发送的文本
6. 清理

## Decisions

- tech_stack_choice: Bash shell scripts（与现有 E2E 一致）+ Docker（Langfuse 容器）+ curl（HTTP 请求）+ jq（JSON 解析）; reason: 与现有 `e2e-helpers.sh` 和 `test_versioned_upgrade.sh` 的技术栈完全一致，不引入新语言或框架
- compatibility_policy: no_compatibility; reason: 这是新增 E2E 测试脚本，不修改任何现有代码或 API
- refactor_or_rewrite: 新增; reason: 完全是新增的测试脚本，不重构现有代码
- state_and_data_source: Docker Langfuse 容器为 E2E 测试的临时数据源，测试结束后销毁; reason: 需要真实的 Langfuse API 来验证数据到达，Docker 是最可控的方式
- risk_notes: OpenCode CLI 非交互式对话能力不确定，Module 4 可能降级为 manual-only; Docker Langfuse 启动时间可能较长（~30s），需要合理的等待超时; reason: 这两个风险分别影响 Module 4 的自动化程度和 Module 2 的执行时间
- overdesign_guard: 最小设计是 Module 2 + Module 3；Module 1 大部分已由现有 E2E 覆盖，仅补充 langstash health check；Module 4 是可选增强；不做的：不做 Langfuse UI 测试、不做多 Agent E2E、不做性能/并发测试

## Handoff To `writing-specs`

- design_source: `docs/designs/20260623-e2e-opencode-langfuse-delivery.md`
- must_carry_to_spec:
  - E2E 必须覆盖完整数据投递链路（hook → langstash-deliver → langstash → Langfuse）
  - Module 1–3 独立通过/失败，不互相阻塞；Module 4 是复合模块，依赖 Module 2+3 的基础设施，但 Module 4 失败不阻塞 Module 1–3
  - Module 2 必须使用 Docker Langfuse 进行真实 API 级别的数据验证
  - Module 3 必须验证 opencode hook 安装/卸载的文件、配置、环境变量完整性
- Module 4 以 manual-only 为预期基线；若 OpenCode CLI 不支持非交互式对话，保持 manual-only 并在报告中标注
  - 测试脚本必须兼容 macOS 和 Linux（不使用 GNU 独有参数）
  - 测试脚本使用现有 `e2e-helpers.sh` 的 marker 输出格式
- advisory_context:
  - Docker Langfuse 可用 `langfuse/langfuse:latest` 镜像，默认端口 3000，首次启动需创建 project 和 API key
  - 合成 OTLP JSON 可参考 `hooks/opencode/hooks/langfuse-exporter.mjs` 中 `buildOtlpJson` 函数的输出格式
  - Langfuse trace 查询 API 参考 Langfuse v2.x 文档（https://langfuse.com/docs/api/traces），实际端点需在 spec 阶段根据 Docker 镜像版本确认
  - OpenCode CLI 可能支持 `--prompt` 参数或 stdin pipe，需实际测试确认
  - 现有 `test_versioned_upgrade.sh` 的 `purge_install()` 函数可复用
  - test-service 的 job 执行流程：fetch → worktree → run E2E → cleanup，本 E2E 脚本需在 worktree 内可执行

## Open Questions

- OpenCode CLI 是否支持非交互式对话模式（stdin pipe 或 `--prompt` 参数）？若不支持，Module 4 自动化程度受影响，但不阻塞其他模块的实现
