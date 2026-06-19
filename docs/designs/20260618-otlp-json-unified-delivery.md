# 使用 OTLP JSON 统一 Hooks-Langstash 通信协议与投递通道

## Problem

当前 hooks ↔ langstash 使用自定义的 **trace JSON schema v2** 作为中间数据格式，存在以下问题：

1. **自定义格式无互操作性**：trace JSON v2 是项目私有 schema（`langstash_deliver.schema`），无法被标准 OTel 工具链解析
2. **双重转换**：Hooks 先将 transcript 数据转换为 trace JSON v2，Sender 再将 trace JSON v2 转换为 Langfuse REST API batch items。两次转换增加了维护负担和 bug 面
3. **Sender 使用非官方推荐路径**：手工 POST 到 REST API `/api/public/ingestion`（已被标记为 Legacy），而非官方推荐的 OTel 端点 `/api/public/otel/v1/traces`
4. **格式与传输紧耦合**：trace JSON v2 是面向 Langfuse REST API 设计的中间格式，如果要切换传输通道必须同时改中间格式

## Context

### 当前数据流

```
Python Hooks (claude-code / qoder / qoderwork)
  ├─ 解析 transcript JSONL → build_turns() → Turn 对象
  ├─ _build_trace_v2(turn) → trace JSON schema v2 ──→ langstash_deliver
  │   (自定义格式: {schema_version, source, session_id, trace, generations[], spans[]})
  │                                                       ├─ Tier 1: POST /ingest → langstash
  │                                                       │    Ingestor → pending/
  │                                                       │    Sender → _build_trace_items() → httpx POST /api/public/ingestion
  │                                                       ├─ Tier 2: emit_turn() → Langfuse SDK OTel → Langfuse
  │                                                       └─ Tier 3: failed/
  └─ emit_turn(turn) ← Tier 2 direct push fallback
```

### 涉及组件

| 组件 | 语言 | 使用 trace JSON v2 | 说明 |
|------|-----|-------------------|------|
| `hooks/claude-code/hooks/langfuse_hook.py` | Python | `_build_trace_v2()` + `emit_turn()` | |
| `hooks/qoder/hooks/langfuse_hook.py` | Python | `_build_trace_v2()` + `emit_turn()` | |
| `hooks/qoderwork/hooks/langfuse_hook.py` | Python | `_build_trace_v2()` + `emit_turn()` | |
| `hooks/langstash-deliver/python/langstash_deliver/schema.py` | Python | `build_trace_json()`, `build_generation()`, `build_span()` | |
| `hooks/langstash-deliver/python/langstash_deliver/deliver.py` | Python | `deliver_trace()` | |
| `exporter/src/sender.py` | Python | `_build_trace_items()`, `_post_batch()` | |
| `exporter/src/ingestor.py` | Python | `validate_trace()` | |
| `hooks/codex/src/langstash.ts` | TypeScript | `buildTraceV2()` + `postLangstash()` + `appendFailedTrace()` | 自行实现 Tier 1/3，Tier 2 用 `@langfuse/otel` |
| `hooks/codex/src/trace.ts` | TypeScript | Tier 1/2/3 编排在 `convertRollout()` 内联 | |
| `hooks/opencode/hooks/langfuse-exporter.mjs` | JavaScript | `buildTraceV2()` + `postLangstash()` + `appendFailedTrace()` 内联 | Tier 2 用 Langfuse JS SDK |

### Langfuse OTel 端点确认

Langfuse SDK 的 `RawOpentelemetryClient.export_traces` 文档确认 OTLP 端点 `/api/public/otel/v1/traces` 支持两种格式：
- **Binary Protobuf**: `Content-Type: application/x-protobuf`
- **JSON Protobuf**: `Content-Type: application/json`

SDK 自身通过 `Content-Type: application/json` 调用此端点。因此 **Sender 可直接 POST OTLP JSON，无需 protobuf 转换**。

### Langfuse OTel 属性体系

Langfuse SDK 4.x 使用 `langfuse.*` 前缀的私有 OTel span attributes。Langfuse 服务端接受含 `langfuse.*` 或 `gen_ai.*` 属性的 span。SDK 使用的 instrumentation scope name 常量为 `"langfuse-sdk"`（`LANGFUSE_TRACER_NAME`）。

### OTLP JSON 格式与序列化注意事项

OTLP JSON 是 `ExportTraceServiceRequest` 的 JSON 表示。**关键注意**：protobuf 的 `MessageToDict()` 会将 `bytes` 字段（如 traceId、spanId）编码为 **base64**，不符合 OTLP JSON 规范（要求 **hex 编码**）。Hooks 构建 OTLP JSON 时必须直接使用 hex 字符串构造 traceId/spanId，不能依赖 protobuf JSON 序列化。

## Goals

- 用 **OTLP JSON** 替代自定义 trace JSON schema v2，作为 hooks ↔ langstash 的标准中间数据格式
- Hooks 直接构建 OTel span 树并序列化为 OTLP JSON
- langstash Sender **直接 POST OTLP JSON**（`Content-Type: application/json`）到 Langfuse OTel 端点，**无需 protobuf 转换**
- 消除全部中间格式转换逻辑

## Non-Goals

- 不改变 langstash 的 pending/ / failed/ 磁盘存储结构
- 不改变 Sender 的后台线程轮询 + commit_id 状态跟踪机制
- 不引入格式版本协商或 trace v2/OTLP 混合模式

## Recommendation

**OTLP JSON 全链路 — Hooks 构建 OTLP JSON dict → langstash 存储 → Sender 直接 POST JSON**

### 新数据流

```
Python Hooks
  ├─ 解析 transcript JSONL → build_turns() → Turn (不变)
  ├─ build_otlp_json(turn) → OTLP JSON dict  ──→ langstash_deliver
  │   (标准格式: ExportTraceServiceRequest JSON)
  │                                               ├─ Tier 1: POST /ingest → langstash
  │                                               │    Ingestor → pending/
  │                                               │    Sender → POST JSON (Content-Type: application/json) → OTel endpoint
  │                                               ├─ Tier 2: direct POST JSON → Langfuse OTel endpoint
  │                                               └─ Tier 3: failed/
  └─ (不再需要 emit_turn() 和 _build_trace_v2() 双路径)
```

核心优势：

1. **零格式转换的 Sender**：读取 OTLP JSON → 直接 POST 到 `/api/public/otel/v1/traces`（`Content-Type: application/json` + Basic Auth）。核心投递逻辑约 20 行
2. **消除 Langfuse SDK 依赖**：Hooks 只需标准 `opentelemetry-sdk` 构建 span。OTel 的 `Tracer.start_span(start_time=ns)` 原生支持历史时间戳，不需要 Langfuse SDK 的 `_start_backdated` hack
3. **统一 Hooks 输出**：从 `_build_trace_v2()` + `emit_turn()` 双路径合并为一套 OTel span builder
4. **标准化中间格式**：OTLP JSON 可被 Jaeger、Grafana Tempo、任何 OTel collector 消费

### 改动范围

**1. Hooks（claude-code、qoder、qoderwork 的 `langfuse_hook.py`）**

- **移除** `_build_trace_v2()` 和 `emit_turn()` 双路径
- **新增** `build_otlp_json(turn)` 统一构建函数：
  - 创建临时 `TracerProvider` + `InMemorySpanExporter`
  - 使用 OTel `Tracer.start_span(start_time=ns)` 构建 span 树（原生支持历史时间戳）
  - 设置 `langfuse.*` 属性（参见下方属性映射表）
  - 从 `InMemorySpanExporter.get_finished_spans()` 获取完成的 spans
  - **手工构建** OTLP JSON dict（traceId/spanId 使用 hex 编码，不依赖 `MessageToDict`）
- **移除** `from langfuse import Langfuse` — 不再需要 Langfuse SDK
- **新增** `opentelemetry-sdk` 依赖
- instrumentation scope name：使用 `"agent-exporter-to-langfuse"`（项目全名，Langfuse 服务端通过 `langfuse.*` 属性识别 span，不依赖 scope name）

**2. `hooks/langstash-deliver/python/langstash_deliver/`**

- **移除** `schema.py`（`build_trace_json`、`build_generation`、`build_span`、`Usage`）
- **改写** `deliver.py`：三层投递完全封装在包内，Hook 只需调用 `deliver_trace(otlp_json)`，不再传入 `direct_push_fn` 回调
  - **Tier 1**（langstash buffer）：仅当 `LANGSTASH_ENABLED=true` 时，POST OTLP JSON 到 `LANGSTASH_URL`（默认 `http://127.0.0.1:5288`）的 `/ingest`，超时由 `LANGSTASH_TIMEOUT`（默认 10s）控制。与现有行为一致
  - **Tier 2**（direct push）：POST OTLP JSON 到 `{LANGFUSE_BASE_URL}/api/public/otel/v1/traces`，`Content-Type: application/json` + Basic Auth（`LANGFUSE_PUBLIC_KEY:LANGFUSE_SECRET_KEY`）。**新增**：`deliver.py` 自行读取这三个环境变量，不再通过回调委托给 Hook
  - **Tier 3**（failed log）：写入 `~/.agent-exporter-to-langfuse/data/failed/` JSONL 文件（与现有行为一致）
  - `deliver_trace()` 签名简化：`deliver_trace(otlp_json: dict) -> bool`（移除 `direct_push_fn` 参数）

  **配置兼容性**：
  - `LANGSTASH_ENABLED`、`LANGSTASH_URL`、`LANGSTASH_TIMEOUT`：保留，语义不变，控制 Tier 1
  - `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`：deliver.py **新增读取**（之前由 Hook 持有），用于 Tier 2 Basic Auth
  - install.sh 写入的 env 文件内容**不需要修改**
  - 部署模式兼容：`LANGSTASH_ENABLED=false` 时跳过 Tier 1 直接走 Tier 2（仅 hooks 不装 daemon 的场景仍可用）

  旧签名：`deliver_trace(trace_json, direct_push_fn=None)`
  新签名：`deliver_trace(otlp_json)`

  Hook 调用从：
  ```python
  def _direct_push(_tj, _t=t, _turn_num=turn_num):
      emit_turn(langfuse, ctx, _turn_num, _t, ...)
      return True
  deliver_trace(trace_json, direct_push_fn=_direct_push)
  ```
  简化为：
  ```python
  deliver_trace(otlp_json)
  ```

**3. `exporter/src/sender.py`**

- **移除** `_build_trace_items`、`_build_ingestion_batch`、`_post_batch`、`_split_into_batches`、`_items_byte_size`
- **新增** `_post_otlp(otlp_json_bytes)` 方法：
  - POST JSON body 到 `{base_url}/api/public/otel/v1/traces`
  - `Content-Type: application/json` + Basic Auth (`public_key:secret_key`)
  - 检查 HTTP 响应状态
- **保留** commit_id 状态跟踪、backoff、failed/ fallback、per-HTTP-status 错误处理
- **移除** `import httpx` → 改用 `requests`（或保留 `httpx`，视 exporter 已有依赖而定）

**4. `exporter/src/ingestor.py`**

- **改写** `validate_trace()` 为 `validate_otlp(body)` 校验 OTLP JSON 结构：

  **结构校验**（基于 OTLP proto `ExportTraceServiceRequest` 定义）：
  - `resourceSpans` 必须存在且为非空数组
  - 每个 resourceSpan 必须包含 `scopeSpans` 数组
  - 每个 scopeSpan 必须包含 `spans` 非空数组

  **span 必填字段校验**（基于 proto Span 定义的 required/semantically required 字段）：
  - `traceId`：hex string，恰好 32 字符（16 字节）。全零或长度不对视为无效
  - `spanId`：hex string，恰好 16 字符（8 字节）。全零或长度不对视为无效
  - `name`：非空字符串（proto 规范："semantically required to be set to non-empty string"）
  - `startTimeUnixNano`：非空字符串，合法纳秒时间戳
  - `endTimeUnixNano`：非空字符串，且 `>= startTimeUnixNano`（proto 规范："it is expected that end_time >= start_time"）

  **语义校验**：
  - root span（`parentSpanId` 为空或不存在）至少存在一个
  - `attributes` 如果存在，必须是 OTel KeyValue 数组格式（`[{"key": "...", "value": {"stringValue": "..."}}]`）

  **不校验的字段**（optional per proto）：
  - `kind`、`status`、`events`、`links`、`traceState`、`flags`

  **注意**：校验仅验证 OTLP 结构合规性，**不校验 `langfuse.*` 或 `gen_ai.*` 属性语义**——属性语义由 Langfuse 服务端在接收后处理。Ingestor 的职责是确保存入 pending/ 的数据是合法的 OTLP JSON，Sender 可以安全转发。

- 校验失败返回 `IngestError(422, "...")`，与现有行为一致
- `_accumulate_tokens()` 改为从 span attributes 中提取 `langfuse.observation.usage_details`（JSON string → parse → 累加 token 计数）。遍历所有 spans 的 attributes，查找 `key == "langfuse.observation.usage_details"` 的条目

### 当前属性策略说明

设计选择使用 **`langfuse.*` 私有属性**而非 `gen_ai.*` 标准属性，原因是：
- Langfuse 服务端解析 `langfuse.observation.type` 来区分 generation/span/tool 类型——标准 GenAI semconv 无此概念
- `langfuse.observation.usage_details` 是 JSON string（单个属性包含所有 token 类型），而 `gen_ai.usage.*` 是每种 token 类型一个独立 int 属性——Langfuse 服务端期望前者
- `session.id`、`user.id`、`langfuse.trace.name`、`langfuse.trace.tags` 等属性在标准 GenAI semconv 中没有对应项

未来 Langfuse 服务端如果增加对 `gen_ai.*` 属性的原生解析支持，可以在 Hooks 的 `build_otlp_json()` 中同时设置两套属性（`langfuse.*` + `gen_ai.*`），Ingestor 校验逻辑不需要变化

**5. `hooks/langstash-deliver/typescript/`（新建 TS/JS 通用包）**

当前 codex（`src/langstash.ts`）和 opencode（`langfuse-exporter.mjs` 内联）各自重复实现了 `postLangstash()` + `appendFailedTrace()` + `buildTraceV2()`。新建 TS/JS 版 langstash-deliver 包，与 Python 版功能对齐：

- **三层投递**：`deliverTrace(otlpJson)` — Tier 1 (POST langstash) → Tier 2 (POST Langfuse OTel endpoint) → Tier 3 (failed log)
- **配置**：从环境变量读取 `LANGSTASH_ENABLED`、`LANGSTASH_URL`、`LANGSTASH_TIMEOUT`、`LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`
- **HTTP**：使用 `fetch`（Node.js 18+ 内置）。opencode 在 Bun 环境中 `fetch` 受限——保留 `curlFetch` 作为可注入的 HTTP adapter
- **无外部依赖**：纯 Node.js API（`fs`、`path`、`os`、`crypto`），不依赖 `langfuse` 或 `@langfuse/otel`

**6. `hooks/codex/`（TypeScript hook 改造）**

- **移除** `src/langstash.ts`（`buildTraceV2`、`postLangstash`、`appendFailedTrace`）
- **移除** `src/instrumentation.ts`（`@langfuse/otel` 的 `LangfuseSpanProcessor` 初始化）
- **改造** `src/trace.ts`：
  - `emitTurnOtel()` 改为 `buildOtlpJson()`——使用 `@opentelemetry/sdk-trace-base` 构建 span 树（`startSpan(name, {startTime})` 原生支持历史时间戳）
  - `convertRollout()` 调用 `buildOtlpJson()` 后传给 `deliverTrace()`（来自 TS langstash-deliver）
  - 三层编排逻辑从 `convertRollout()` 内联移出到 langstash-deliver
- **依赖变更**：移除 `@langfuse/otel`、`@langfuse/tracing`；添加 `@opentelemetry/sdk-trace-base`

**7. `hooks/opencode/`（JavaScript hook 改造）**

- **移除** `langfuse-exporter.mjs` 中内联的 `postLangstash()`、`appendFailedTrace()`、`buildTraceV2()`、Langfuse JS SDK 直接推送逻辑
- **改造** 投递部分：
  - 新增 `buildOtlpJson()` 构建 OTLP JSON
  - 调用 TS langstash-deliver 的 `deliverTrace()`（编译为 JS 后使用）
  - 通过 `curlFetch` adapter 解决 Bun 网络限制
- **依赖变更**：移除 `langfuse` JS SDK

**8. 依赖变更**

| 组件 | 添加 | 移除 |
|------|-----|------|
| hooks/claude-code `pyproject.toml` | `opentelemetry-sdk>=1.20` | `langfuse>=4.7.0` |
| hooks/qoder `pyproject.toml` | `opentelemetry-sdk>=1.20` | `langfuse>=4.7.0` |
| hooks/qoderwork `pyproject.toml` | `opentelemetry-sdk>=1.20` | `langfuse>=4.7.0` |
| hooks/codex `package.json` | `@opentelemetry/sdk-trace-base` | `@langfuse/otel`, `@langfuse/tracing` |
| hooks/opencode | （无新 npm 依赖） | `langfuse` JS SDK |
| hooks/langstash-deliver/typescript | 新建包（零外部依赖） | — |
| exporter `pyproject.toml` | （无新增） | （无移除） |

**不需要** `opentelemetry-proto` 和 `opentelemetry-exporter-otlp-proto-http`——Sender 直接 POST JSON，不做 protobuf 转换。

### OTel span 属性映射

| Langfuse 概念 | OTel span attribute | 值格式 |
|-------------|--------------------|----|
| trace | root span + `langfuse.trace.name` | span with no parent |
| generation | child span + `langfuse.observation.type` = `"generation"` | |
| tool span | child span + `langfuse.observation.type` = `"tool"` | nested under generation |
| session_id | `session.id` | string |
| user_id | `user.id` | string |
| tags | `langfuse.trace.tags` | JSON array string |
| input | `langfuse.observation.input` | JSON string |
| output | `langfuse.observation.output` | JSON string |
| model | `langfuse.observation.model.name` | string |
| usage | `langfuse.observation.usage_details` | JSON string, e.g. `'{"input":100,"output":50}'` |
| metadata | `langfuse.observation.metadata.{key}` | per-key string or JSON string |

### OTLP JSON 序列化规范

Hooks 的 `build_otlp_json()` 必须遵循 OTLP JSON 编码规则（不能使用 `MessageToDict`）：
- `traceId`、`spanId`、`parentSpanId`：**hex 编码**字符串（32/16 字符）
- `startTimeUnixNano`、`endTimeUnixNano`：**字符串格式的纳秒 epoch**（如 `"1718000000000000000"`）
- `attributes`：键值对数组 `[{"key": "...", "value": {"stringValue": "..."}}]`

### 错误处理

| 场景 | HTTP status | 行为 |
|------|-------------|------|
| 成功 | 200 | 推进 commit_id |
| 认证错误 | 401/403 | 停止 Sender |
| 格式错误 | 400 | 跳过并推进 commit_id |
| 服务端错误 | 5xx | 退避重试 |
| 网络错误 | N/A | 退避重试 |

### 升级与迁移

- **同步升级要求**：hooks 和 exporter 必须同版本部署。installer.sh 已保证这一点
- **旧 `failed/` 文件处理**：升级后 `failed/` 中的 trace JSON v2 文件无法被新的 `validate_trace()` 接受。`recover_failed()` 应检测旧格式（`schema_version` 字段存在）并跳过或丢弃，记录 warning 日志

### Design Review accepted findings

- **High** (traceId 编码): 不使用 `MessageToDict()`，手工构建 OTLP JSON dict 时直接使用 hex 编码
- **Medium** (JSON 直接 POST): Langfuse 服务端确认支持 `application/json`，不再需要 JSON→protobuf 转换，移除 `opentelemetry-proto` 依赖
- **Medium** (旧 failed/ 文件): `recover_failed()` 增加旧格式检测和跳过逻辑
- **Low** (scope name): 使用 `"agent-exporter-to-langfuse"` 作为 instrumentation scope name，不混用 `"langfuse-sdk"`
- **Low** (InMemorySpanExporter 导入路径): 使用 `from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter`（或验证版本稳定的 re-export）

## Decisions

- **tech_stack_choice**: 标准 OTel SDK（`opentelemetry-sdk>=1.20`）用于 Hooks 构建 span。Sender 使用 `httpx`（已有依赖）直接 POST OTLP JSON。**不需要** `opentelemetry-proto`、`protobuf`、`langfuse` SDK
- **compatibility_policy**: `no_compatibility` — 中间格式从 trace JSON v2 直接切换到 OTLP JSON。`recover_failed()` 优雅跳过旧格式文件
- **refactor_or_rewrite**:
  - Hooks：rewrite span 构建逻辑
  - Sender：rewrite 投递逻辑（从 REST batch → OTLP JSON relay）
  - Ingestor：refactor 校验逻辑
- **state_and_data_source**: `pending/` 目录为唯一 trace 数据源（格式从 trace JSON v2 变为 OTLP JSON），`sender.json` 的 `commit_id` 不变
- **risk_notes**:
  - 同时升级是 atomic 要求——installer.sh 已保证
  - OTLP JSON 中属性值的序列化格式需与 Langfuse 服务端期望一致（JSON string for complex types）
  - Hooks 移除 Langfuse SDK 后 Tier 2 direct push 需自行处理 Basic Auth
- **overdesign_guard**: 最小设计。不引入格式版本协商、不引入 protobuf 转换、不引入 OTel Collector、不引入自定义 schema

## Handoff To `writing-specs`

- **design_source**: `docs/designs/20260618-otlp-json-unified-delivery.md`
- **requirement themes**:
  - R-1: Python Hooks 使用标准 OTel SDK 构建 span 树，输出 OTLP JSON（hex-encoded traceId/spanId）
  - R-2: `langstash_deliver`（Python + TS/JS 双语言包）完整封装三层投递（Tier 1 langstash buffer / Tier 2 direct POST to Langfuse OTel endpoint / Tier 3 failed log），移除 `direct_push_fn` 回调参数，移除 `schema.py`
  - R-3: langstash Sender 作为 OTLP JSON relay：读取 → POST `Content-Type: application/json` + Basic Auth
  - R-4: langstash Ingestor 校验 OTLP JSON 结构合规性（resourceSpans → scopeSpans → spans 层级完整、traceId 32 字符 hex、spanId 16 字符 hex、name 非空、startTimeUnixNano 合法、endTimeUnixNano >= startTimeUnixNano、root span 存在、attributes KeyValue 数组格式）。不校验属性语义。从 span attributes 提取 token usage 累计
  - R-5: 保持 at-least-once delivery、commit_id 状态跟踪、per-HTTP-status 错误处理
  - R-6: 依赖变更：hooks 移除 `langfuse`，添加 `opentelemetry-sdk`；exporter 无新增
  - R-7: 移除全部 trace JSON v2 代码（Python: `schema.py`、`_build_trace_v2`、`emit_turn`、`_build_trace_items`；TS: `langstash.ts` 的 `buildTraceV2`；JS: 内联 `buildTraceV2`）
  - R-8: codex hook 改造：移除 `@langfuse/otel`，使用 `@opentelemetry/sdk-trace-base` + TS langstash-deliver
  - R-9: opencode hook 改造：移除 Langfuse JS SDK，使用 TS langstash-deliver（通过 `curlFetch` adapter）
  - R-10: `recover_failed()` 优雅跳过旧格式 failed/ 文件
- **negative boundaries**:
  - 不改变 pending/ / failed/ 目录结构
  - 不引入格式版本协商或混合模式
  - 不引入 protobuf 转换
  - 不改变 Sender 线程模型
- **verification intent**:
  - 验证：Hooks OTLP JSON 包含正确的 `langfuse.*` 属性
  - 验证：traceId/spanId 为 hex 编码（32/16 字符）
  - 验证：span 树结构正确（trace root → generation → tool，parent-child 通过 parentSpanId 链接）
  - 验证：startTimeUnixNano/endTimeUnixNano 反映原始 transcript 时间戳
  - 验证：instrumentation scope name 为 `"agent-exporter-to-langfuse"`
  - 验证：Ingestor 对合法 OTLP JSON 返回 202 accepted
  - 验证：Ingestor 对缺少 resourceSpans 的请求返回 422
  - 验证：Ingestor 对 traceId 非 hex 或长度不为 32 的 span 返回 422
  - 验证：Ingestor 对 spanId 非 hex 或长度不为 16 的 span 返回 422
  - 验证：Ingestor 对缺少 name 或 startTimeUnixNano 的 span 返回 422
  - 验证：Ingestor 对 endTimeUnixNano < startTimeUnixNano 的 span 返回 422
  - 验证：Ingestor 对无 root span 的请求返回 422
  - 验证：Ingestor 正确从 span attributes 提取 `langfuse.observation.usage_details` 并累计 token
  - 验证：Sender POST OTLP JSON 到 Langfuse → 数据正确显示
  - 验证：Sender 错误处理（401→stop、400→skip、5xx→retry）
  - 验证：commit_id 在成功后推进
  - 验证：超大 trace OTLP JSON 写入 failed/
  - 验证：langstash_deliver Tier 2 direct push 成功投递
  - 验证：旧格式 failed/ 文件被优雅跳过
  - 验证：OTLP JSON round-trip 完整性（序列化 → pending/ 存储 → 读取 → POST → Langfuse 接受）

## Open Questions

（无阻塞问题）
