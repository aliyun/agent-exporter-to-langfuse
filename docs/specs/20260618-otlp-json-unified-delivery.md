# 使用 OTLP JSON 统一 Hooks-Langstash 通信协议与投递通道

## Purpose

将 hooks ↔ langstash 的中间数据格式从项目私有的 trace JSON schema v2 替换为标准 OTLP JSON（`ExportTraceServiceRequest` JSON 序列化），同时将 Sender 的投递端点从 Langfuse REST API（`/api/public/ingestion`，已标记 Legacy）切换到 Langfuse 官方推荐的 OTel 端点（`/api/public/otel/v1/traces`）。统一全部 5 个 Hook（Python × 3 + TypeScript + JavaScript）的投递路径到 langstash-deliver 包（Python + TS/JS 双语言），消除各 Hook 中重复的三层投递实现和 Langfuse SDK 依赖。

## Non-Goals

- 不改变 langstash 的 `pending/` / `failed/` 磁盘目录结构和 JSONL 行格式。
- 不改变 Sender 的后台线程轮询模型和 `commit_id` / `seq_id` 状态跟踪机制。
- 不引入格式版本协商、trace v2 / OTLP JSON 混合模式、或 OTel Collector。
- 不引入 `opentelemetry-proto` 或 protobuf 转换——Sender 直接 POST OTLP JSON。
- 不改变 `install.sh` 写入的环境变量文件内容（`LANGSTASH_ENABLED`、`LANGSTASH_URL` 等已有变量保留原值）。

## Decisions

- design_source: `docs/designs/20260618-otlp-json-unified-delivery.md`
- 中间格式：trace JSON schema v2 → OTLP JSON（`ExportTraceServiceRequest` JSON），clean cut，不保留旧格式兼容。
- 投递端点：`/api/public/ingestion`（REST, Legacy）→ `/api/public/otel/v1/traces`（OTel, `Content-Type: application/json`）。
- Langfuse 属性命名空间：使用 `langfuse.*` 私有属性（`langfuse.observation.type`、`langfuse.observation.model.name` 等），因为 Langfuse 服务端依赖这些属性进行 observation 类型分类。不使用 `gen_ai.*` 标准属性（未来可叠加）。
- instrumentation scope name：`"agent-exporter-to-langfuse"`。
- OTLP JSON 中 `traceId` / `spanId` 使用 hex 编码（不使用 protobuf `MessageToDict` 的 base64 编码）。
- Python hooks 依赖从 `langfuse>=4.7.0` 改为 `opentelemetry-sdk>=1.20`。codex 从 `@langfuse/otel` + `@langfuse/tracing` 改为 `@opentelemetry/sdk-trace-base`。opencode 移除 `langfuse` JS SDK。

## Requirements

### R-1: Hooks 构建 OTLP JSON span 树替代 trace JSON v2 和 SDK direct push 双路径

- context: 当前每个 Python Hook 维护 `_build_trace_v2()`（构建 trace JSON v2 用于 langstash）和 `emit_turn()`（使用 Langfuse SDK OTel API 直接推送）两套几乎相同的逻辑。codex 和 opencode 同样各自实现 `buildTraceV2()` + OTel/SDK 直推。
- must:
  - 每个 Python Hook（claude-code、qoder、qoderwork）提供 `build_otlp_json(turn)` 函数，使用标准 OTel SDK（`TracerProvider` + `InMemorySpanExporter`）构建 span 树，序列化为 OTLP JSON dict。
  - OTel span 使用 `Tracer.start_span(name, start_time=ns)` 设置历史时间戳（纳秒 epoch），使用 `span.end(end_time=ns)` 设置结束时间。时间戳来源于 transcript 原始 ISO 8601 时间。
  - span 树结构：root span（trace 级）→ generation child spans → tool child spans（嵌套在 generation 下），通过 OTel parent span context 建立父子关系。
  - root span 设置 `langfuse.trace.name`、`session.id`、`user.id`、`langfuse.trace.tags`、`langfuse.observation.input`、`langfuse.observation.output` 属性。
  - generation span 设置 `langfuse.observation.type=generation`、`langfuse.observation.model.name`、`langfuse.observation.usage_details`（JSON string）、`langfuse.observation.input`、`langfuse.observation.output` 属性。
  - tool span 设置 `langfuse.observation.type=tool`、`langfuse.observation.input`、`langfuse.observation.output`、`langfuse.observation.metadata.*` 属性。
  - OTLP JSON 中 `traceId` 为 32 字符 hex string，`spanId` 为 16 字符 hex string，`startTimeUnixNano` / `endTimeUnixNano` 为字符串格式纳秒 epoch。手工构建 OTLP JSON dict，不使用 protobuf `MessageToDict()`。
  - instrumentation scope name 为 `"agent-exporter-to-langfuse"`。
  - codex Hook 提供等价的 `buildOtlpJson(turn)` 函数（TypeScript，使用 `@opentelemetry/sdk-trace-base`）。
  - opencode Hook 提供等价的 `buildOtlpJson()` 函数（JavaScript）。
  - Hook 的 transcript 解析逻辑（`read_new_jsonl`、`build_turns`、增量读取状态管理）保持不变。
- must_not:
  - 不保留 `_build_trace_v2()` 或 `emit_turn()` 函数。
  - 不保留 `buildTraceV2()` 函数（codex langstash.ts、opencode langfuse-exporter.mjs）。
  - 不依赖 `langfuse` Python SDK、`@langfuse/otel`、`@langfuse/tracing`、或 `langfuse` JS SDK。
  - 不使用 Langfuse SDK 内部 API（`_otel_tracer`、`_create_observation_from_otel_span`、`_start_backdated`）。
  - `traceId` / `spanId` 不使用 base64 编码。
- verification:
  - 构建的 OTLP JSON 包含 `resourceSpans[].scopeSpans[].spans[]` 三层嵌套结构。
  - `traceId` 为 32 字符 hex，`spanId` 为 16 字符 hex。
  - span 树层级正确：root（无 parentSpanId）→ generation（parentSpanId = root spanId）→ tool（parentSpanId = generation spanId）。
  - `startTimeUnixNano` / `endTimeUnixNano` 反映 transcript 原始时间戳。
  - `langfuse.*` 属性键名和值格式与 Langfuse SDK 4.x `attributes.py` 中定义的一致。
  - scope name 为 `"agent-exporter-to-langfuse"`。

### R-2: langstash-deliver 包完整封装三层投递（Python + TS/JS 双语言）

- context: 当前 Python langstash-deliver 的 `deliver_trace(trace_json, direct_push_fn=None)` 只负责 Tier 1 和 Tier 3，Tier 2 通过 Hook 提供的 `direct_push_fn` 回调委托给 Langfuse SDK。codex 和 opencode 各自内联实现了 `postLangstash()` + `appendFailedTrace()`。
- must:
  - Python `langstash_deliver.deliver.deliver_trace(otlp_json: dict) -> bool` 完整封装三层投递，不接受 `direct_push_fn` 参数。
  - Tier 1：当 `LANGSTASH_ENABLED=true`（环境变量）时，POST OTLP JSON 到 `LANGSTASH_URL`（默认 `http://127.0.0.1:5288`）的 `/ingest`，超时由 `LANGSTASH_TIMEOUT`（默认 10s）控制。
  - Tier 2：POST OTLP JSON 到 `{LANGFUSE_BASE_URL}/api/public/otel/v1/traces`，`Content-Type: application/json`，Basic Auth 使用 `LANGFUSE_PUBLIC_KEY:LANGFUSE_SECRET_KEY`。`deliver.py` 自行从环境变量读取这三个凭证。
  - Tier 3：写入 `~/.agent-exporter-to-langfuse/data/failed/{date}.jsonl`，每行一个 OTLP JSON。
  - 当 `LANGSTASH_ENABLED` 不为 `true` 时跳过 Tier 1 直接尝试 Tier 2（仅 hooks 不装 daemon 的部署模式）。
  - 新建 `hooks/langstash-deliver/typescript/` 包，提供 `deliverTrace(otlpJson)` 函数，行为与 Python 版对齐。
  - TS/JS 版 HTTP 层默认使用 `fetch`（Node.js 18+），并支持注入自定义 HTTP adapter（opencode 的 Bun 环境需要 `curlFetch`）。
  - TS/JS 版零外部 npm 依赖（纯 Node.js API）。
- must_not:
  - 不保留 `deliver_trace` 的 `direct_push_fn` 参数。
  - 不保留 `schema.py`（`build_trace_json`、`build_generation`、`build_span`、`Usage`）。
  - 不保留 codex `src/langstash.ts` 中的 `postLangstash()`、`appendFailedTrace()`、`buildTraceV2()`。
  - 不保留 opencode `langfuse-exporter.mjs` 中内联的 `postLangstash()`、`appendFailedTrace()`、`buildTraceV2()`。
- verification:
  - Tier 1 成功投递时 `deliver_trace` 返回 `True`。
  - `LANGSTASH_ENABLED=false` 时跳过 Tier 1，Tier 2 成功投递时返回 `True`。
  - Tier 1 和 Tier 2 均失败时写入 `failed/` 目录并返回 `False`。
  - Tier 2 POST 的请求包含正确的 `Content-Type: application/json` header 和 Basic Auth。

### R-3: Sender 作为 OTLP JSON relay 投递到 Langfuse OTel 端点

- context: 当前 Sender 从 `pending/` 读取 trace JSON v2，调用 `_build_trace_items()` 转换为 REST API batch items，通过 `httpx.post` 发送到 `/api/public/ingestion`。
- must:
  - Sender 从 `pending/` 读取 OTLP JSON 行，直接 POST JSON body 到 `{base_url}/api/public/otel/v1/traces`，`Content-Type: application/json`，Basic Auth（`public_key:secret_key`）。
  - HTTP 2xx：推进 `commit_id`。
  - HTTP 400：请求体无法解析（整个 OTLP JSON 结构不合法），记录 error 含 response body（此情况不应发生，表示 OTLP JSON 构建有 bug），跳过并推进 `commit_id`（重试无意义）。
  - HTTP 401/403/404/405：记录 error 含 status code 和 response body，不推进 `commit_id`，退避重试。不停止 Sender——凭证可能被修正、Langfuse 可能临时异常，保持重试以自动恢复。
  - HTTP 5xx：不推进 `commit_id`，退避重试。
  - 网络异常：不推进 `commit_id`，退避重试。
  - 不做 payload 大小预检——Langfuse OTel 端点无文档化的大小限制，任何大小的 OTLP JSON 都直接投递。
  - `batch_size` 新语义：每次轮询从 `pending/` 读取最多 `batch_size` 条 OTLP JSON 行，逐条独立 POST，全部成功后推进 `commit_id` 到最后一条的 `_seq_id`。单条失败时停止本轮，`commit_id` 推进到最后一条成功的 `_seq_id`。`batch_size` 默认值从 10 改为 1。
  - `commit_id` / `seq_id` 状态跟踪机制、`SenderState` 持久化、`SenderConfig` 其余参数（`interval_seconds`、`max_backoff_seconds`、`timeout_seconds`）保持不变。`max_payload_bytes` 不再使用，可从 `SenderConfig` 中移除。
- must_not:
  - 不保留 `_build_trace_items()`、`_build_ingestion_batch()`、`_post_batch()`、`_split_into_batches()`、`_items_byte_size()`。
  - 不做 payload 大小预检或 payload splitting。
  - 不 POST 到 `/api/public/ingestion`。
  - 不依赖 `opentelemetry-proto` 或 protobuf 转换。
- verification:
  - Sender POST OTLP JSON 到 Langfuse OTel 端点后，数据在 Langfuse 中正确显示为 trace → generation → tool 层级。
  - 400 → 跳过并 commit；401/403/404/405/5xx → 不推进 commit_id，退避重试。
  - `commit_id` 在成功投递后推进。
  - 逐条 POST 中某条失败时 `commit_id` 推进到最后一条成功的 `_seq_id`。

### R-4: Ingestor 校验 OTLP JSON 结构合规性并提取 token usage

- context: 当前 `validate_trace()` 校验 trace JSON v2 的 `schema_version`、`source`、`session_id`、`trace.name/start_time/end_time`、`generations` 非空。`_accumulate_tokens()` 从 `generations[].usage` 提取 token 计数。
- must:
  - 将 `validate_trace(body)` 改为 `validate_otlp(body)` 校验 OTLP JSON 结构：
    - `resourceSpans` 存在且为非空数组。
    - 每个 resourceSpan 包含 `scopeSpans` 数组。
    - 每个 scopeSpan 包含 `spans` 非空数组。
    - 每个 span 的 `traceId` 为 32 字符 hex string（非全零）。
    - 每个 span 的 `spanId` 为 16 字符 hex string（非全零）。
    - 每个 span 的 `name` 为非空字符串。
    - 每个 span 的 `startTimeUnixNano` 为非空字符串且为合法纳秒时间戳。
    - 每个 span 的 `endTimeUnixNano`（如果存在）`>= startTimeUnixNano`。
    - 至少存在一个 root span（`parentSpanId` 为空或不存在）。
    - `attributes`（如果存在）为 OTel KeyValue 数组格式。
  - 校验失败返回 `IngestError(422, "...")`。
  - 不校验 `langfuse.*` 或 `gen_ai.*` 属性语义。
  - `_accumulate_tokens()` 从 span attributes 中查找 `key == "langfuse.observation.usage_details"` 的条目，解析 JSON string 值，累加 `input`、`output`、`cache_read_input_tokens`、`cache_creation_input_tokens` 到 `IngestState`。
- must_not:
  - 不保留 trace JSON v2 的 `schema_version`、`source`、`session_id`、`trace`、`generations` 等校验逻辑。
  - 不校验 `kind`、`status`、`events`、`links`、`traceState`、`flags` 等 OTLP optional 字段。
- verification:
  - 合法 OTLP JSON → 202 accepted。
  - 缺少 `resourceSpans` → 422。
  - `traceId` 非 hex 或长度不为 32 → 422。
  - `spanId` 非 hex 或长度不为 16 → 422。
  - 缺少 `name` 或 `startTimeUnixNano` → 422。
  - `endTimeUnixNano < startTimeUnixNano` → 422。
  - 无 root span → 422。
  - 正确从 `langfuse.observation.usage_details` 属性提取并累计 token 计数。

### R-5: codex Hook 改造为使用 OTel SDK 和 TS langstash-deliver

- context: codex 当前使用 `@langfuse/otel` 的 `LangfuseSpanProcessor` 进行 Tier 2 OTel 直推，`src/langstash.ts` 实现 Tier 1/3，`src/trace.ts` 的 `convertRollout()` 内联编排三层投递。
- must:
  - `src/trace.ts` 中 `emitTurnOtel()` 改为 `buildOtlpJson()`，使用 `@opentelemetry/sdk-trace-base` 构建 span 树，输出 OTLP JSON dict。
  - `convertRollout()` 调用 `buildOtlpJson()` 后传给 TS langstash-deliver 的 `deliverTrace()`。三层投递编排从 `convertRollout()` 内联移出到 langstash-deliver。
  - 移除 `src/instrumentation.ts` 文件（`LangfuseSpanProcessor` 初始化）。
  - 移除 `src/langstash.ts` 文件（`buildTraceV2()`、`postLangstash()`、`appendFailedTrace()` 及辅助类型，全部功能由 TS langstash-deliver 替代）。
- must_not:
  - 不依赖 `@langfuse/otel` 或 `@langfuse/tracing`。
  - `convertRollout()` 不内联实现 Tier 1/2/3 投递逻辑。
- verification:
  - codex 构建的 OTLP JSON 包含正确的 span 树和 `langfuse.*` 属性。
  - 通过 `deliverTrace()` 三层投递成功到达 Langfuse。

### R-6: opencode Hook 改造为使用 TS langstash-deliver

- context: opencode 当前在 `langfuse-exporter.mjs` 中内联实现 `buildTraceV2()`、`postLangstash()`、`appendFailedTrace()`、以及 Langfuse JS SDK 直推。Bun 运行时限制 `fetch` 出站 HTTP，需使用 `curlFetch` 绕过。
- must:
  - `langfuse-exporter.mjs` 的投递部分改为调用 TS langstash-deliver 的 `deliverTrace()`。
  - 通过 TS langstash-deliver 的 HTTP adapter 机制注入 `curlFetch`，解决 Bun 网络限制。
  - 新增 `buildOtlpJson()` 构建 OTLP JSON。
- must_not:
  - 不保留内联的 `postLangstash()`、`appendFailedTrace()`、`buildTraceV2()`。
  - 不依赖 `langfuse` JS SDK。
  - 不内联实现 Tier 1/2/3 投递逻辑。
- verification:
  - opencode 构建的 OTLP JSON 包含正确的 span 树和 `langfuse.*` 属性。
  - 通过 `deliverTrace()` + `curlFetch` adapter 三层投递成功。

### R-7: 移除全部 trace JSON v2 相关代码

- context: trace JSON v2 schema 分布在 `langstash_deliver/schema.py`、各 Hook 的 `_build_trace_v2()` / `buildTraceV2()`、Sender 的 `_build_trace_items()` / `_build_ingestion_batch()` 中。
- must:
  - 移除 Python `langstash_deliver/schema.py` 整个文件。
  - 移除各 Python Hook 中的 `_build_trace_v2()` 函数。
  - 移除各 Python Hook 中的 `emit_turn()` 函数。
  - 移除 Sender 中的 `_build_trace_items()`、`_build_ingestion_batch()`、`_post_batch()`、`_split_into_batches()`、`_items_byte_size()`、`_send_oversized_trace()`、`_handle_413()`、`_write_to_failed()` 函数。Sender 不再做 payload 预检和写 `failed/`——投递失败时不推进 `commit_id`，下轮重试。
  - 移除 codex `src/langstash.ts` 中的 `buildTraceV2()` 函数。
  - 移除 opencode `langfuse-exporter.mjs` 中内联的 `buildTraceV2()` 函数。
  - trace JSON v2 的语义（trace → generation → tool span 层级、session_id、user_id、tags、usage）由 OTLP JSON 中的 `langfuse.*` span attributes 承载。
- must_not:
  - 不保留任何 trace JSON v2 构建函数或 schema 定义。
  - 不保留 REST API `/api/public/ingestion` 的调用代码。
- verification:
  - 代码库中不存在 `schema_version`、`build_trace_json`、`build_generation`、`build_span`、`_build_trace_items`、`_build_ingestion_batch`、`buildTraceV2` 等符号。

### R-8: 旧格式 failed 文件优雅跳过

- context: 升级后 `failed/` 目录可能残留 trace JSON v2 格式的文件。新的 `validate_otlp()` 会拒绝这些文件，`recover_failed()` 会反复尝试恢复并失败。
- must:
  - `recover_failed()` 在调用 `ingest()` 前检测旧格式（body 包含 `schema_version` 字段），跳过该行并记录 warning 日志。
  - `recover_failed()` 对单行 JSON 解析失败（`JSONDecodeError`）、`validate_otlp` 校验失败、或 `ingest()` 抛出的任何 `IngestError`，都跳过该行并记录 warning，继续处理后续行。
- must_not:
  - 不因旧格式、损坏行、或校验失败导致 `recover_failed()` 抛出未处理异常、中断后续行处理、或无限循环。
- verification:
  - `failed/` 中包含无法解析的损坏行时，跳过并继续。
  - `failed/` 中包含不符合 OTLP 校验的行时（含旧格式 trace JSON v2），跳过并继续。
  - 上述跳过均记录 warning 日志。

### R-9: 依赖和打包适配

- context: 代码结构变化（新增 TS langstash-deliver 包、移除 Langfuse SDK 依赖、移除 schema.py）需要适配打包和安装脚本。
- must:
  - Python hooks（claude-code、qoder、qoderwork）的 `pyproject.toml` 添加 `opentelemetry-sdk>=1.20`，移除 `langfuse>=4.7.0`。
  - codex `package.json` 添加 `@opentelemetry/sdk-trace-base`，移除 `@langfuse/otel`、`@langfuse/tracing`、`@opentelemetry/sdk-trace-node`。
  - opencode 移除 `langfuse` JS SDK 依赖。
  - `exporter/src/config.py` 中 `SenderConfig.batch_size` 默认值从 10 改为 1，`max_payload_bytes` 字段移除。
  - 各 hook 的 `install.sh` 适配新的文件拷贝路径（TS langstash-deliver 包的分发）。
  - 各 hook 的 `uninstall.sh` 适配新增的 TS langstash-deliver 文件清理。
  - `deploy/package.sh` 适配新增的 TS langstash-deliver 包打包。
  - `deploy/installer.sh` 适配（如需要）。
- must_not:
  - `install.sh` 写入的环境变量文件内容不变（`LANGSTASH_ENABLED`、`LANGSTASH_URL`、`LANGFUSE_*` 等变量名和值不改）。
- verification:
  - 各 hook 安装后能正确导入新依赖（Python: `opentelemetry.sdk`；TS: `@opentelemetry/sdk-trace-base`；JS: TS langstash-deliver 编译产物）。
  - `deploy/package.sh` 构建产物包含 TS langstash-deliver。
  - 安装后环境变量文件内容与升级前一致。

## Open Questions

（无阻塞问题）
