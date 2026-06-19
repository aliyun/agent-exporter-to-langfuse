# 使用 OTLP JSON 统一 Hooks-Langstash 通信协议与投递通道 — 实现计划

## Header
- source_spec: ../specs/20260618-otlp-json-unified-delivery.md
- risk: high-risk
- runtime_profile: high-risk
- runtime_profile_basis: wire format replacement (trace JSON v2 → OTLP JSON), no-compat migration, producer-consumer chain (hooks → ingestor → pending/ → sender → Langfuse), persistence format change, cross-language multi-package blast radius
- accepted_debt: none
- status: ready
- external_review_policy: required

## Requirements Covered
- R-1: Hooks 构建 OTLP JSON span 树替代 trace JSON v2
- R-2: langstash-deliver 包完整封装三层投递（Python + TS/JS）
- R-3: Sender 作为 OTLP JSON relay 投递到 Langfuse OTel 端点
- R-4: Ingestor 校验 OTLP JSON 结构合规性并提取 token usage
- R-5: codex Hook 改造为使用 OTel SDK 和 TS langstash-deliver
- R-6: opencode Hook 改造为使用 TS langstash-deliver
- R-7: 移除全部 trace JSON v2 相关代码
- R-8: 旧格式 failed 文件优雅跳过
- R-9: 依赖和打包适配

## Planning Evidence

### surfaces
- `hooks/langstash-deliver/python/langstash_deliver/deliver.py` — deliver_trace(trace_json, direct_push_fn) 签名需重写
- `hooks/langstash-deliver/python/langstash_deliver/schema.py` — 整个文件删除（build_trace_json, build_generation, build_span, Usage）
- `exporter/src/ingestor.py` — validate_trace → validate_otlp; _accumulate_tokens 从 generations[].usage → span attributes
- `exporter/src/sender.py` — 9 个函数删除（_build_trace_items 等），新增 _post_otlp; endpoint /api/public/ingestion → /api/public/otel/v1/traces
- `exporter/src/config.py` — SenderConfig.batch_size 10→1, max_payload_bytes 删除, load_config 中 max_payload_bytes clamp 逻辑删除
- `hooks/claude-code/hooks/langfuse_hook.py`（935 行）— _build_trace_v2 + emit_turn 双路径 → build_otlp_json
- `hooks/qoder/hooks/langfuse_hook.py`（1219 行）— 同上 + DB token enrichment
- `hooks/qoderwork/hooks/langfuse_hook.py`（1057 行）— 同上 + VM-specific state
- `hooks/codex/src/langstash.ts` — buildTraceV2 + postLangstash + appendFailedTrace → 整文件删除
- `hooks/codex/src/instrumentation.ts` — LangfuseSpanProcessor → 整文件删除
- `hooks/codex/src/trace.ts` — emitTurnOtel → buildOtlpJson; convertRollout 内联 Tier 1/2/3 → deliverTrace
- `hooks/opencode/hooks/langfuse-exporter.mjs` — 内联 buildTraceV2 + postLangstash + appendFailedTrace + Langfuse SDK → buildOtlpJson + deliverTrace

### consumers
- `deliver_trace()` 被 3 个 Python hooks 的 main() 调用
- `schema.py` 的 build_trace_json/build_generation/build_span 被 3 个 Python hooks 的 _build_trace_v2 调用
- `validate_trace()` 被 `ingest()` 调用，`ingest()` 被 server.py `/ingest` endpoint 和 `recover_failed()` 调用
- `_accumulate_tokens()` 被 `ingest()` 调用
- `_build_trace_items()` 被 sender._send_batch() 调用
- `_post_batch()` 被 sender._send_batch() 和 sender._send_oversized_trace() 调用
- `SenderConfig.max_payload_bytes` 被 sender._send_batch(), sender._send_oversized_trace(), config.load_config() 使用

### coupling
- ingestor validate 和 hooks produce 必须对齐同一 OTLP JSON schema — 由 spec R-1/R-4 定义
- sender POST 和 ingestor 存储的 pending/ 格式一致 — 都是 OTLP JSON JSONL
- Python langstash-deliver 被 3 个 Python hooks import — 签名变更需同步
- TS langstash-deliver 被 codex 和 opencode 共用 — 新建包
- deploy/package.sh 使用 git ls-files 打包 — 新 TS 包自动包含无需额外适配
- installer.sh 保证同版本部署 — atomic upgrade，中间状态不影响生产

### unknowns
- 无

### negative_surfaces
- `schema_version`, `build_trace_json`, `build_generation`, `build_span` — schema.py 中定义，3 个 Python hooks import
- `_build_trace_v2` — 3 个 Python hooks 中定义
- `emit_turn` — 3 个 Python hooks 中定义（Langfuse SDK direct push）
- `_build_trace_items`, `_build_ingestion_batch`, `_post_batch`, `_split_into_batches`, `_items_byte_size`, `_write_to_failed`, `_send_oversized_trace`, `_handle_413` — sender.py 中定义
- `buildTraceV2` — codex langstash.ts 和 opencode langfuse-exporter.mjs 中定义
- `postLangstash`, `appendFailedTrace` — codex langstash.ts 和 opencode 内联
- `direct_push_fn` — deliver.py 参数
- `max_payload_bytes` — config.py SenderConfig 字段
- `@langfuse/otel`, `@langfuse/tracing`, `langfuse` JS SDK — package.json / npm 依赖
- `langfuse>=4.7.0` — pyproject.toml 依赖
- `from langfuse import Langfuse` — Python hooks import
- `LangfuseSpanProcessor` — codex instrumentation.ts

### tests
- `exporter/tests/test_ingestor.py` — TestValidateTrace, TestAccumulateTokens, TestIngest 全部需要重写
- `exporter/tests/test_sender.py` — TestBuildIngestionBatch, imports of deleted functions 全部需要重写
- `hooks/langstash-deliver/python/tests/test_deliver.py` — deliver_trace 签名变更
- `hooks/langstash-deliver/python/tests/test_schema.py` — schema.py 删除后此文件删除
- `hooks/codex/tests/langstash.test.ts` — buildTraceV2 等删除后需重写或删除

## Phases with Tasks

### phase-1: OTLP JSON 基础设施
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: cd exporter && python -m pytest tests/test_ingestor.py tests/test_sender.py -v

#### task-1 [P]: 重写 Python langstash-deliver 为 OTLP JSON 三层投递
- requirements: [R-2, R-7]
- outputs: [hooks/langstash-deliver/python/langstash_deliver/deliver.py, hooks/langstash-deliver/python/tests/]
- action: 重写 deliver.py，签名从 deliver_trace(trace_json, direct_push_fn=None) 改为 deliver_trace(otlp_json: dict) -> bool。包内完整实现 Tier 1（POST /ingest）、Tier 2（POST Langfuse OTel 端点 + Basic Auth）、Tier 3（failed/ JSONL）。删除 schema.py 及 tests/test_schema.py。
- constraints:
  - Tier 2 的 LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL 由 deliver.py 自行从环境变量读取
  - LANGSTASH_ENABLED=false 时跳过 Tier 1 直接 Tier 2
  - schema.py 中 build_trace_json/build_generation/build_span/Usage 的语义由 OTLP JSON 中 langfuse.* span attributes 承载（R-1 负责构建）
- verification:
  - planned_test: deliver_trace(otlp_json) Tier 1 成功返回 True
  - planned_test: LANGSTASH_ENABLED=false 跳过 Tier 1，Tier 2 成功返回 True
  - planned_test: Tier 1 和 Tier 2 均失败时写入 failed/ 返回 False
  - planned_test: Tier 2 请求包含 Content-Type: application/json 和 Basic Auth
  - source_scan: rg "direct_push_fn|build_trace_json|build_generation|build_span" hooks/langstash-deliver/python/ — allowed exceptions: none

#### task-2 [P]: 创建 TS/JS langstash-deliver 包
- requirements: [R-2]
- outputs: [hooks/langstash-deliver/typescript/]
- action: 新建 hooks/langstash-deliver/typescript/ 包，提供 deliverTrace(otlpJson, options?) 函数。行为与 Python 版对齐：Tier 1/2/3 三层投递。HTTP 层默认 fetch，支持注入自定义 adapter（curlFetch）。零外部 npm 依赖。
- constraints:
  - 环境变量名与 Python 版一致：LANGSTASH_ENABLED, LANGSTASH_URL, LANGSTASH_TIMEOUT, LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
  - failed/ 目录路径与 Python 版一致：~/.agent-exporter-to-langfuse/data/failed/
  - 输出 ESM 格式（.mjs），opencode 通过 install.sh 拷贝编译产物后 import 使用
  - package.json 包含 build 脚本（tsc 或等效），type: module
- verification:
  - planned_test: deliverTrace 三层投递行为与 Python 版一致
  - planned_test: 自定义 HTTP adapter 注入后用于 Tier 1 和 Tier 2
  - inspect: 零外部 npm 依赖（package.json dependencies 为空或不存在）

#### task-3 [P]: Ingestor OTLP JSON 校验 + recover_failed 鲁棒性
- requirements: [R-4, R-8]
- outputs: [exporter/src/ingestor.py, exporter/tests/test_ingestor.py]
- action: 将 validate_trace(body) 改为 validate_otlp(body)，校验 OTLP JSON 结构（resourceSpans/scopeSpans/spans 层级、traceId 32 hex、spanId 16 hex、name 非空、时间戳合法、root span 存在、attributes KeyValue 格式）。_accumulate_tokens 改为从 span attributes 中查找 langfuse.observation.usage_details。recover_failed 对 JSONDecodeError、旧格式、校验失败跳过并记录 warning。
- constraints:
  - 不校验 langfuse.* 或 gen_ai.* 属性语义
  - 校验失败返回 IngestError(422, "...")
- verification:
  - planned_test: 合法 OTLP JSON 通过校验
  - planned_test: 缺少 resourceSpans → 422
  - planned_test: traceId 非 hex 或长度不为 32 → 422
  - planned_test: spanId 非 hex 或长度不为 16 → 422
  - planned_test: 缺少 name 或 startTimeUnixNano → 422
  - planned_test: endTimeUnixNano < startTimeUnixNano → 422
  - planned_test: 无 root span → 422
  - planned_test: 正确从 langfuse.observation.usage_details 提取 token 计数
  - planned_test: recover_failed 跳过 JSONDecodeError 损坏行并记录 warning
  - planned_test: recover_failed 跳过含 schema_version 的旧格式行并记录 warning
  - planned_test: recover_failed 跳过 validate_otlp 校验失败行并记录 warning

#### task-4 [P]: Sender 改为 OTLP JSON relay + 配置调整
- requirements: [R-3, R-9]
- outputs: [exporter/src/sender.py, exporter/src/config.py, exporter/tests/test_sender.py, exporter/tests/test_config.py]
- action: 移除全部 REST batch 逻辑（含 _write_to_failed/_handle_413/_send_oversized_trace），改为逐条 POST OTLP JSON 到 Langfuse OTel 端点。Sender 不再写 failed/。SenderConfig.batch_size 默认 1，移除 max_payload_bytes。
- constraints:
  - 不做 payload 大小预检
  - 单条失败停止本轮，commit_id 推进到最后一条成功的 _seq_id
  - 401/403/404/405 不停止 Sender（与旧逻辑不同），退避重试
  - Sender 不再写入 failed/ — 投递失败时不推进 commit_id，下轮重试
- verification:
  - planned_test: POST 到 /api/public/otel/v1/traces 而非 /api/public/ingestion
  - planned_test: 2xx → commit_id 推进
  - planned_test: 400 → 跳过并推进
  - planned_test: 401/403/404/405 → 不推进，退避重试，不停止 Sender
  - planned_test: 5xx → 退避重试
  - planned_test: 逐条 POST 中某条失败时 commit_id 推进到最后成功的 _seq_id
  - planned_test: batch_size 默认为 1
  - source_scan: rg "max_payload_bytes|_write_to_failed|_handle_413|_send_oversized_trace" exporter/src/ — allowed exceptions: none

### phase-2: Hook 迁移到 OTLP JSON
- commit_boundary: task
- worker_dispatch: per-task
- gate: source_scan: rg -l "_build_trace_v2|emit_turn|buildTraceV2|emitTurnOtel" hooks/ — allowed exceptions: none

#### task-5 [P]: Python hooks OTel SDK 重写
- requirements: [R-1, R-7, R-9]
- outputs: [hooks/claude-code/hooks/langfuse_hook.py, hooks/qoder/hooks/langfuse_hook.py, hooks/qoderwork/hooks/langfuse_hook.py, hooks/claude-code/hooks/pyproject.toml, hooks/qoder/hooks/pyproject.toml, hooks/qoderwork/hooks/pyproject.toml]
- action: 三个 Python hooks 从 _build_trace_v2()+emit_turn() 双路径统一为 build_otlp_json(turn)。使用标准 OTel SDK（TracerProvider + InMemorySpanExporter）构建 span 树，手工序列化为 OTLP JSON dict。调用 deliver_trace(otlp_json) 替代 deliver_trace(trace_json, direct_push_fn)。移除 Langfuse SDK 依赖。pyproject.toml 添加 opentelemetry-sdk>=1.20，移除 langfuse>=4.7.0。
- constraints:
  - traceId 32 字符 hex，spanId 16 字符 hex，不使用 MessageToDict
  - instrumentation scope name 为 "agent-exporter-to-langfuse"
  - langfuse.* 属性键名和值格式与 Langfuse SDK 4.x attributes.py 一致
  - 保留各 hook 特有逻辑（qoder DB token enrichment、qoderwork VM state）
  - transcript 解析逻辑（read_new_jsonl、build_turns、state 管理）保持不变
- verification:
  - planned_test: build_otlp_json 输出包含 resourceSpans[].scopeSpans[].spans[] 三层结构
  - planned_test: traceId 32 hex, spanId 16 hex
  - planned_test: span 树层级正确（root → generation → tool）
  - planned_test: langfuse.* 属性设置正确
  - planned_test: scope name 为 "agent-exporter-to-langfuse"
  - source_scan: rg "from langfuse|import Langfuse|_build_trace_v2|emit_turn" hooks/claude-code/ hooks/qoder/ hooks/qoderwork/ — allowed exceptions: none
  - inspect: read_new_jsonl、build_turns、SessionState、load_session_state 函数体在 refactor 前后保持不变

#### task-6 [P]: codex Hook OTel SDK 重写
- requirements: [R-5, R-7, R-9]
- outputs: [hooks/codex/src/trace.ts, hooks/codex/package.json, hooks/codex/tests/]
- action: emitTurnOtel 改为 buildOtlpJson（使用 @opentelemetry/sdk-trace-base）。convertRollout 调用 buildOtlpJson + TS langstash-deliver 的 deliverTrace。删除 src/langstash.ts 和 src/instrumentation.ts。package.json 添加 @opentelemetry/sdk-trace-base，移除 @langfuse/otel + @langfuse/tracing + @opentelemetry/sdk-trace-node。
- constraints:
  - convertRollout 不内联 Tier 1/2/3 投递逻辑
  - 若 TS langstash-deliver API 需要调整，修改 hooks/langstash-deliver/typescript/ 源文件并与 task-7 协调
- verification:
  - planned_test: buildOtlpJson 输出包含正确的 span 树和 langfuse.* 属性
  - source_scan: rg "@langfuse/otel|@langfuse/tracing|LangfuseSpanProcessor|buildTraceV2|postLangstash|appendFailedTrace" hooks/codex/ — allowed exceptions: none
  - shell: cd hooks/codex && pnpm run lint:tsc

#### task-7 [P]: opencode Hook 重写
- requirements: [R-6, R-7, R-9]
- outputs: [hooks/opencode/hooks/langfuse-exporter.mjs]
- action: 移除内联的 postLangstash/appendFailedTrace/buildTraceV2 和 Langfuse JS SDK 直推逻辑。新增 buildOtlpJson 构建 OTLP JSON。投递改为调用 TS langstash-deliver 的 deliverTrace，通过 curlFetch adapter 解决 Bun 限制。
- constraints:
  - 通过 TS langstash-deliver 的 HTTP adapter 机制注入 curlFetch
  - 不依赖 langfuse JS SDK
- verification:
  - source_scan: rg "from 'langfuse'|require.*langfuse|buildTraceV2|postLangstash|appendFailedTrace" hooks/opencode/ — allowed exceptions: none
  - inspect: 确认 curlFetch adapter 注入方式

### phase-3: 打包和安装脚本适配
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: bash deploy/package.sh --output-dir /tmp/test-pkg

#### task-8: 安装和打包脚本适配
- requirements: [R-9]
- outputs: [hooks/codex/install.sh, hooks/codex/uninstall.sh, hooks/opencode/install.sh, hooks/opencode/uninstall.sh, deploy/package.sh, deploy/installer.sh]
- action: 适配 codex install.sh 以分发 TS langstash-deliver 编译产物。适配 opencode install.sh 移除 npm install langfuse，改为拷贝 TS langstash-deliver。适配 uninstall.sh。确认 package.sh 和 installer.sh 无需额外修改（package.sh 基于 git ls-files 自动包含新包）。
- constraints:
  - install.sh 写入的环境变量文件内容不变
  - install.sh/uninstall.sh 保持幂等
- verification:
  - shell: bash deploy/package.sh --output-dir /tmp/test-pkg
  - inspect: 安装后环境变量文件内容与升级前一致
  - inspect: uninstall.sh 清理 TS langstash-deliver 文件

## Verification
- source_scan: rg "schema_version|build_trace_json|build_generation\b|build_span\b|_build_trace_items|_build_ingestion_batch|buildTraceV2|direct_push_fn|max_payload_bytes" exporter/src/ hooks/ — allowed exceptions: none
- source_scan: rg "from langfuse|@langfuse/otel|@langfuse/tracing|langfuse import Langfuse" hooks/ — allowed exceptions: none
- shell: cd exporter && python -m pytest tests/ -v
- shell: cd hooks/langstash-deliver/python && python -m pytest tests/ -v
- shell: cd hooks/codex && pnpm run lint:tsc && pnpm test
- shell: cd hooks/langstash-deliver/typescript && npm test
- shell: bash deploy/package.sh --output-dir /tmp/test-pkg
- inspect: SenderConfig.batch_size 默认值为 1，无 max_payload_bytes 字段
- inspect: Sender POST 到 /api/public/otel/v1/traces，Content-Type: application/json + Basic Auth
- inspect: 各 Python hook pyproject.toml 包含 opentelemetry-sdk>=1.20，不包含 langfuse
- inspect: codex package.json 包含 @opentelemetry/sdk-trace-base，不包含 @langfuse/*
