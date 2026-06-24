# E2E 测试：OpenCode 数据投递到 Langfuse 的端到端验证

## Purpose

当前 E2E 测试套件仅覆盖安装/卸载/升级等运维操作，未覆盖核心数据投递链路（OpenCode hook → langstash-deliver → langstash → Langfuse）。本 spec 定义四个独立 E2E 测试模块，从合成 trace 投递到真实 OpenCode 对话，分层验证数据投递链路的完整性，每模块独立通过/失败，不互相阻塞。

## Non-Goals

- 产品行为边界：
  - 不验证 Langfuse UI 上 trace 的展示效果，只验证 API 层面数据存在且属性正确
- 兼容与迁移边界：
  - 不覆盖旧 git 布局迁移后的数据投递行为（已有 E2E-6 覆盖迁移本身）
  - 不验证 install.sh / uninstall.sh 的幂等性（重复安装不产生重复 plugin 注册、重复卸载不报错）；幂等性由单独的测试覆盖
- Runtime 边界：
  - 不测试 langstash 的高并发或大批量投递性能
- 相邻 workflow 边界：
  - 不修改 test-service 的 job 编排逻辑；test-service 只负责调度本 E2E 脚本的执行
  - 不修改现有 `test_versioned_upgrade.sh` 的测试内容或结构
- 未来扩展边界：
  - 不覆盖其他 Agent（claude-code、qoder 等）的数据投递 E2E；其他 Agent 可后续按同样模式补充

## Decisions

- design_source: `docs/designs/20260623-e2e-opencode-langfuse-delivery.md`
- tech_stack: Bash shell scripts + Docker（Langfuse 容器）+ curl + jq，与现有 `e2e-helpers.sh` 和 `test_versioned_upgrade.sh` 技术栈一致，不引入新语言或框架; applies_to: R-1, R-2, R-3, R-4, R-5
- module_independence: Module 1–3 独立运行、独立判定通过/失败，不互相阻塞；Module 4 是复合模块，依赖 Module 2 的 Docker Langfuse 和 Module 3 的 hook 安装作为前置，但 Module 4 失败不阻塞 Module 1–3 的判定; applies_to: R-1, R-5
- module4_baseline: Module 4 以 manual-only 为预期基线；若 OpenCode CLI 支持非交互式对话（stdin pipe 或 `--prompt` 参数）则升级为全自动，否则保持 manual-only 并在测试报告中标注; applies_to: R-5
- docker_langfuse_data_source: Docker Langfuse 容器是 E2E 测试的临时数据接收端，测试结束后销毁，不持久化; applies_to: R-2, R-3, R-5

## Requirements

R-N 与模块映射：R-1→脚本框架（跨所有模块），R-2→Module 1，R-3→Module 2，R-4→Module 3，R-5→Module 4

### R-1: E2E 脚本兼容 test-service 编排与跨平台执行

- context:
  - 现有 E2E 使用 `e2e-helpers.sh` 的 `##e2e##` marker 输出格式；test-service 的 `ProgressParser` 解析该格式来跟踪进度
  - 本要求确保新增的 E2E 脚本兼容现有 marker 格式和 test-service 的 worktree 执行环境

- must:
  - 使用 `e2e-helpers.sh` 的 marker 输出格式（`##e2e## suite/case/pass/fail/summary`）
  - 每个模块（Module 1–4）作为独立的 E2E suite 运行，有自己的 suite name 和 case 计数
  - 各模块可在 test-service 的 git worktree 内独立执行（不依赖 worktree 外的路径）
  - 脚本兼容 macOS 和 Linux（不使用 GNU 独有参数）
  - 提供 `--module` 参数选择运行特定模块（如 `--module 2` 只运行 Module 2），无参数时按顺序运行所有模块
  - 各模块的通过/失败判定独立：Module 4 失败不影响 Module 1–3 的 suite summary 结果
  - manual-only marker 映射：当 Module 4 降级为 manual-only 时，使用 `##e2e## pass` 标记（manual-only 是预期基线行为，不是失败），case name 添加 `_manual_only` 后缀（如 `opencode_delivery_manual_only`），suite summary 中注明 manual-only 模块数量

- must_not:
  - 不修改 `e2e-helpers.sh` 或 `test_versioned_upgrade.sh` 的现有内容
  - 不引入 Python、Node.js 或其他脚本语言作为 E2E 脚本本身的运行时（curl/jq/docker 为 shell 外部命令，不算引入新语言）
  - `--module 4` 在前置缺失时不自动搭建 Module 2/3 的环境（保持模块独立性）

- verification:
  - 脚本输出包含 `##e2e##` marker 且可被 test-service 的 `ProgressParser` 正确解析
  - `--module 2` 只运行 Module 2 的 suite，不执行 Module 1/3/4 的 case
  - 脚本在 macOS 和 Linux 上均可执行
  - `--module 4` 在无 Docker Langfuse 和 hook 安装前置时输出 fail marker 并提示前置缺失

### R-2: langstash 健康检查补充到安装验证

- context:
  - 现有 E2E-1（`test_versioned_upgrade.sh` 的 `e2e_install` case）验证了安装后文件结构，但不验证 langstash 服务是否健康启动
  - 本要求补充 langstash `/health` endpoint 的 200 响应验证，作为安装完整性的一部分
  - `/health` 的 `"healthy"` 状态仅表示 Langfuse credentials 已写入配置（`bool(public_key and secret_key)`），不验证 Langfuse 后端可达性；后端连通验证由 Module 2 覆盖

- must:
  - 在安装 agent-exporter-to-langfuse 后，等待 langstash `/health` endpoint 返回 HTTP 200 且 `status` 字段为 `"healthy"`
  - 等待超时不超过 60 秒（langstash 启动可能需要时间）
  - 健康检查失败时 E2E case 标记为 fail，输出 langstash 的错误信息辅助诊断
  - suite 结束时（无论 pass/fail）执行 `purge_install` 清理，不留 langstash 进程或安装产物

- must_not:
  - 不修改现有 E2E-1 的测试内容；本模块作为独立的 E2E suite（Module 1），包含安装验证 + 健康检查
  - 不在 suite 结束后遗留 langstash 进程或 agent-exporter-to-langfuse 安装产物

- verification:
  - 安装后 langstash `/health` 返回 200，E2E case pass；`/health` 返回非 200 或超时，E2E case fail

### R-3: 合成 OTLP trace 投递到 Docker Langfuse 的端到端验证

- context:
  - 数据投递链路：langstash `/ingest` 接收 OTLP JSON → sender 投递到 Langfuse `/api/public/otel/v1/traces`
  - 本要求用合成 trace 验证该链路，不依赖 OpenCode hook，只需 langstash 服务和 Langfuse 后端
  - Langfuse trace 查询端点基于 Langfuse v2.x；若 Docker 镜像版本变更，端点路径可能需适配

- must:
  - 环境准备：
    - 启动 Docker Langfuse 容器（`langfuse/langfuse:latest`，暴露端口 3000）
    - 创建 Langfuse project 和 API key（通过 Langfuse API 或初始化脚本）
    - 安装 agent-exporter-to-langfuse，配置 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 指向 Docker Langfuse
    - 启动 langstash 服务，等待 `/health` 返回 200
  - trace 构造与发送：
    - 构造最小合法 OTLP JSON trace（包含 root span + generation span），格式符合 langstash `validate_otlp()` 的校验规则（`resourceSpans[].scopeSpans[].spans[]`，每个 span 有 `traceId`/`spanId`/`name`/`startTimeUnixNano`）
    - root span name 为 `"e2e-synthetic-test"`，model 属性为 `"e2e-model"`
    - 通过 `curl POST /ingest` 发送给 langstash，验证返回 HTTP 202 和 `seq_id`
  - 投递验证：
    - 轮询 langstash `/stats` endpoint，等待 `total_sent >= 1`，超时不超过 30 秒
    - 通过 Langfuse trace 查询 API（具体端点随 Docker 镜像版本确定）查询，验证 trace 存在且属性匹配：
      - trace name 包含 `"e2e-synthetic-test"`
      - 有 generation span 且 model 字段包含 `"e2e-model"`
  - 清理：
    - 停止 langstash 服务
    - 停止并删除 Docker Langfuse 容器及相关资源
    - purge agent-exporter-to-langfuse 安装

- must_not:
  - 不在测试脚本中硬编码 Langfuse API key 明文（使用 Docker Langfuse 初始化时动态获取的 key）
  - 不在日志输出中暴露完整 API key（只输出前缀，如前 12 字符）
  - Docker Langfuse 容器在测试结束后必须被停止并删除，不留残余容器

- verification:
  - 合成 trace 经 langstash 投递后在 Langfuse API 中可查询到且属性匹配
  - langstash `/stats` 的 `total_sent` 增长
  - 测试结束后无残余 Docker 容器

#### Scenario: 合成 trace 投递验证

- given: Docker Langfuse 运行在端口 3000，langstash 运行在端口 5288 并指向 Docker Langfuse
- when: 发送一个包含 root span（name=`e2e-synthetic-test`）和 generation span（model=`e2e-model`）的 OTLP JSON 到 langstash `/ingest`
- then: langstash `/stats` 显示 `total_sent >= 1`；Langfuse trace 查询 API 返回结果包含 name 含 `e2e-synthetic-test` 的 trace，且该 trace 有 model 含 `e2e-model` 的 generation span

### R-4: OpenCode hook 安装与卸载的文件和配置完整性验证

- context:
  - OpenCode hook 安装由 `hooks/opencode/install.sh` 执行，拷贝 plugin 文件、langstash-deliver、注册到 `opencode.json`、写入 env 文件
  - 卸载由 `hooks/opencode/uninstall.sh` 执行，移除上述所有文件和配置
  - 本要求验证安装后文件/配置/环境变量的完整性，以及卸载后这些项的彻底清除

- must:
  - 安装验证（执行 `hooks/opencode/install.sh --secret-key sk-e2e-test --public-key pk-e2e-test --base-url http://127.0.0.1:9999 -y` 后）：
    - `~/.config/opencode/plugins/langfuse-exporter.mjs` 文件存在
    - `~/.config/opencode/plugins/langstash-deliver/index.js` 文件存在
    - `~/.config/opencode/opencode.json` 的 `plugin` 数组包含 `"./plugins/langfuse-exporter.mjs"`
    - `~/.agent-exporter-to-langfuse/config/opencode.env` 包含 `LANGFUSE_SECRET_KEY`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_BASE_URL`、`LANGSTASH_ENABLED`、`LANGSTASH_URL` 五个变量且值非空
  - 卸载验证（执行 `hooks/opencode/uninstall.sh` 后）：
    - `~/.config/opencode/plugins/langfuse-exporter.mjs` 不存在
    - `~/.config/opencode/plugins/langstash-deliver/` 目录不存在
    - `~/.config/opencode/opencode.json` 的 `plugin` 数组不包含 langfuse-exporter 相关条目
    - `~/.agent-exporter-to-langfuse/config/opencode.env` 不存在
    - `~/.config/opencode/logs/langfuse-exporter/` 目录不存在
  - suite 结束时（无论 pass/fail）执行 `purge_install` 清理，不留 agent-exporter-to-langfuse 安装产物

- must_not:
  - 卸载后不存在残留的 langfuse-exporter plugin 文件、langstash-deliver 目录、langfuse-exporter 日志目录、opencode.env 文件、或 opencode.json 中的 plugin 注册
  - 测试中不使用真实的 Langfuse API key（使用占位符值 `sk-e2e-test`、`pk-e2e-test`）

- verification:
  - 安装后逐一检查上述文件/配置项存在且内容正确
  - 卸载后逐一检查上述文件/配置项不存在

#### Scenario: hook 安装完整性验证

- given: agent-exporter-to-langfuse 已安装，OpenCode 配置目录 `~/.config/opencode` 存在
- when: 执行 `hooks/opencode/install.sh --secret-key sk-e2e-test --public-key pk-e2e-test --base-url http://127.0.0.1:9999 -y`
- then: `~/.config/opencode/plugins/langfuse-exporter.mjs` 存在；`~/.config/opencode/plugins/langstash-deliver/index.js` 存在；`~/.config/opencode/opencode.json` 的 `plugin` 数组包含 `"./plugins/langfuse-exporter.mjs"`；`opencode.env` 包含所有五个必需变量

#### Scenario: hook 卸载彻底性验证

- given: OpenCode hook 已安装（上述安装验证通过）
- when: 执行 `hooks/opencode/uninstall.sh`
- then: `~/.config/opencode/plugins/langfuse-exporter.mjs` 不存在；`~/.config/opencode/plugins/langstash-deliver/` 目录不存在；`~/.config/opencode/opencode.json` 的 `plugin` 数组不含 langfuse-exporter；`opencode.env` 不存在；`~/.config/opencode/logs/langfuse-exporter/` 目录不存在

### R-5: 真实 OpenCode 对话数据投递验证（manual-only 基线）

- context:
  - Module 4 是复合模块，前置条件为 Module 2 的 Docker Langfuse 和 Module 3 的 hook 安装
  - OpenCode CLI 是否支持非交互式对话（stdin pipe 或 `--prompt` 参数）不确定，本模块以 manual-only 为预期基线
  - Module 4 失败不影响 Module 1–3 的判定

- must:
  - 前置条件准备：
    - 启动 Docker Langfuse（同 Module 2 的环境准备）
    - 安装 agent-exporter-to-langfuse 并配置指向 Docker Langfuse
    - 启动 langstash 并等待健康
    - 安装 OpenCode hook（同 Module 3 的安装验证）
  - 对话触发尝试：
    - 尝试 OpenCode CLI 的非交互式对话方式（`--prompt` 参数或 stdin pipe）
    - 若 CLI 不支持非交互式对话，将 Module 4 标记为 manual-only，在测试输出中打印手动操作指引（包括启动 OpenCode、发送对话、等待 session.idle 事件、检查 Langfuse 的步骤）
  - 数据验证（仅在成功触发对话后执行）：
    - 等待 `session.idle` 事件触发（等待若干秒，超时不超过 120 秒）
    - 通过 Langfuse trace 查询 API（具体端点随 Docker 镜像版本确定）查询，验证至少有一条 trace，且包含：
      - trace name 包含 `"OpenCode"`
      - 有 generation span 且 model 字段非空
      - input 包含用户发送的文本
  - 清理（同 Module 2/3 的清理步骤）
  - Module 4 的 pass/fail 状态独立于 Module 1–3；Module 4 fail 不影响整体 E2E 脚本对 Module 1–3 的通过判定

- must_not:
  - Module 4 的失败不导致 Module 1–3 的 suite summary 报告为 fail
  - manual-only 模式下不在测试输出中暴露完整 Langfuse API key
  - 不在 manual-only 输出中包含需要用户手动创建 project/key 的步骤（Docker Langfuse 的 project 和 key 应在 Module 2 的前置中已创建）

- verification:
  - 若 CLI 支持非交互：对话后在 Langfuse API 中可查到包含 OpenCode 属性的 trace
  - 若 CLI 不支持非交互：Module 4 标记为 manual-only 且输出包含完整的手动操作指引

#### Scenario: Module 4 manual-only fallback

- given: Docker Langfuse 和 OpenCode hook 前置已就绪，OpenCode CLI 不支持非交互式对话
- when: Module 4 检测到 CLI 不支持非交互模式
- then: Module 4 suite 输出标记为 manual-only，打印手动操作指引；Module 1–3 的 suite summary 不受影响

## Open Questions

- OpenCode CLI 是否支持非交互式对话模式（stdin pipe 或 `--prompt` 参数）？若不支持，Module 4 保持 manual-only，不阻塞其他模块的实现
