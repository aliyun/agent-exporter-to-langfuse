# Sender Payload Splitting — 解决 Langfuse 4.5MB 请求体限制

## Problem

Sender 模块 (`sender.py:_build_ingestion_batch`) 将每个 trace 的所有 generations 和 spans 展开为 Langfuse ingestion batch items，一次性 POST 到 `/api/public/ingestion`。当一个对话产生大量观测项（实测案例：277 generations + 561 spans = 5.5MB），序列化后的 JSON 请求体超过 Langfuse Cloud 的 4.5MB 硬限制，返回 HTTP 413。

当前 sender 将 413 视为可重试的服务端错误（走 `_send_batch` 第 220 行的 fallback），导致无限指数退避重试（实测 113 次），阻塞该 trace 及其后所有 pending 数据的投递。

## Context

- 数据流：hook → POST `/ingest` → `ingestor.py` 写入 `pending/<date>.jsonl`（每条 JSONL 是一个完整 trace，含所有 generations/spans）→ `sender.py` 读取 pending 文件，按 `batch_size=10` 条 trace 组批，展开为 Langfuse batch items 后 POST。
- Langfuse `/api/public/ingestion` API 接受 `{"batch": [...]}` 格式，batch 内可以是 trace-create、generation-create、span-create 等独立 item，每个 item 通过 `traceId` 关联。一个 trace 的 items 分多次请求发送是合法的（Langfuse 会按 traceId 合并）。
- 当前 `batch_size` 控制的是 trace 数量，不是 payload 字节数。一个超大 trace 单独就能超限。
- Langfuse Cloud 的 4.5MB 限制是服务端硬限制，不可调整；self-hosted 可调但不应依赖。
- `ingestor.py` 已有 10MB 的单条 ingest 限制（`MAX_BODY_BYTES`），但该限制仅在接收端检查，与 sender 端发送大小无关。
- 每个 generation 和 span 是独立 item，平均 10–70KB，可以独立分批。
- 已知预存在问题：`_build_ingestion_batch` 每次调用对 generation/span 生成新 UUID（`uuid.uuid4()`），重试时会在 Langfuse 产生重复观测项。此问题不在本设计范围内，但 splitting 增加了部分成功的概率，从而放大该问题。记录为后续改进项：可通过确定性 UUID（如 UUID5(trace_id + generation_index)）解决。

## Goals

- sender 发出的每个 HTTP 请求的 JSON body 不超过可配置的大小阈值（默认 3.5MB，为 4.5MB 留安全余量）。
- 超大 trace 自动拆分为多个请求，保证数据完整投递。
- HTTP 413 不再导致无限重试；识别为不可恢复的 payload 错误并正确处理。
- 对正常大小的 trace（绝大多数场景）无性能影响。

## Non-Goals

- 不在 hook 侧或 ingestor 侧截断/过滤数据（数据完整性由上游保证）。
- 不修改 JSONL 存储格式（pending 文件结构不变）。
- 不增加新的配置文件或配置 section。
- 不做 generation/span 内容的截断。

## Options

### Option A: Sender 侧按字节大小分片发送

在 `_build_ingestion_batch` 之后、`httpx.post` 之前，检查序列化后的 payload 大小。如果超过阈值，将 batch items 拆分为多个子 batch，逐个发送。同时将 413 识别为不可跳过的 payload 错误。

- fit_to_existing_stack: 高 — 仅修改 sender.py 和 config.py，不引入新模块
- product_fit: 高 — 直接解决 413 问题，保证数据完整
- simplicity: 中 — 需要处理分片逻辑和部分成功场景
- correctness: 高 — Langfuse 支持按 traceId 跨请求合并 items，拆分语义正确
- operability: 高 — 添加日志记录拆分行为，可通过现有 stats/WebUI 监控
- ai_coding_fit: 高 — 变更集中在两个文件，边界清晰

### Option B: Ingestor 侧写入时拆分 trace

在 ingest 阶段，将超大 trace 的 generations/spans 拆成多条 JSONL 记录写入 pending。sender 不需要改动，因为每条 JSONL 自然更小。

- fit_to_existing_stack: 中 — 修改 ingestor 的写入语义，pending 文件中同一 trace 出现多条记录
- product_fit: 高 — 同样解决 413 问题
- simplicity: 低 — 需要在 ingestor 拆分 trace 时维护 generation_index→span 的父子关系，还需确保 sender 不会因同一 trace 出现多条而重复创建 trace-create
- correctness: 中 — 改变了 pending 文件的语义（从"一行一个完整 trace"变为"一行一个 trace 分片"），对 recover_failed、stats 等都有影响
- operability: 中 — 调试时 pending 文件不再是完整 trace，增加排查复杂度
- ai_coding_fit: 低 — 涉及 ingestor、sender、state 多个模块的协调变更

### Option C: gzip 压缩请求体

对序列化后的 JSON 进行 gzip 压缩后发送，设置 `Content-Encoding: gzip`。JSON 通常有 5–10x 的 gzip 压缩比，5.5MB payload 压缩后约 0.5–1.1MB，远低于 4.5MB 限制。

- fit_to_existing_stack: 高 — httpx 原生支持发送原始字节 + 自定义 header，约 5 行代码改动
- product_fit: 高 — 绝大多数超限场景可通过压缩解决
- simplicity: 极高 — 无分片逻辑、无部分成功问题、无多批次 commit 语义
- correctness: 取决于 Langfuse 服务端是否接受 gzip — Langfuse Cloud 基于 Vercel/Next.js，API 路由默认不自动解压 `Content-Encoding: gzip` 的请求体。Langfuse 官方 SDK 也不使用 gzip 压缩。**经验证，Langfuse ingestion API 不支持 gzip 编码的请求体。**
- operability: 高 — 对调试透明
- ai_coding_fit: 极高 — 最小变更

### Option D: 反应式 split-on-413

不做预检查。正常发送，仅在收到 413 时将 batch items 拆分为更小的子 batch 重试。

- fit_to_existing_stack: 高 — 仅修改 sender.py 的 413 处理路径
- product_fit: 高 — 直接解决 413 问题
- simplicity: 高 — 正常路径零开销，无需预序列化检查大小
- correctness: 中 — 每次超限都浪费一次 round-trip；拆分后如果仍然超限，需要递归拆分或放弃
- operability: 中 — 413 是预期行为的一部分，日志噪音增加
- ai_coding_fit: 高 — 变更集中在一个文件

## Recommendation

**选择 Option A（主动分片）作为主方案，Option C（gzip）不可行。**

Option C（gzip）是最简方案，但 Langfuse 的 ingestion API 不支持 gzip 编码的请求体（Vercel/Next.js API 路由不自动解压 `Content-Encoding: gzip`），因此排除。

Option D（反应式 split-on-413）看似更简单，但存在问题：(1) 每次超限浪费一次网络 round-trip + 服务端处理；(2) 413 变成常规日志，运维噪音增加；(3) 当超限是常态（长对话场景）时，性能退化明显。

**选择 Option A：Sender 侧主动分片发送。**采用 trace 级别投递策略：

1. 从 pending 读取最多 `batch_size` 条 trace。
2. 逐条 trace 构建 ingestion batch items，序列化后检查累积 payload 大小。
3. 若加入当前 trace 后未超过 `max_payload_bytes`，继续累加下一条。
4. 若加入当前 trace 后超过阈值：
   - **如果已有前序 trace 在 batch 中**：停止累加，只发送已累积的 trace，当前 trace 及后续 trace 留到下一轮发送。commit_id 更新到已成功发送的最后一条 trace 的 seq_id。
   - **如果这是第一条 trace（单条即超限）**：将该 trace 的 items 拆分为多个子 batch，逐个发送。所有子 batch 成功后才 commit。
5. HTTP 413 作为不可恢复错误：将该 trace 原始数据写入 `failed/` 目录，advance commit_id 跳过，记录告警日志。

**这个策略的优点**：绝大多数情况下只是 "少发几条 trace"，无需 item 级别拆分。只有单条 trace 本身超限（极少数超长对话）才需要 item 拆分。部分成功风险仅存在于单条超大 trace 的拆分场景，而非所有批次。

**413 fallback 语义**：splitting 后仍收到 413（理论上不应发生，因为单个 item 远小于阈值，实测最大 68KB），说明 Langfuse 侧限制比预期更严格。此时将该 trace 数据转移到 `failed/` 目录供运维排查，同时 advance commit_id 避免队列死锁。这是 "不丢数据但允许延迟手动恢复" 的策略。

## Decisions

- tech_stack_choice: 现有 Python + httpx 栈，不引入新依赖
- compatibility_policy: no_compatibility — 纯内部行为变更，不影响任何外部接口或存储格式
- refactor_or_rewrite: 在现有 `_build_ingestion_batch` 和 `_send_batch` 基础上适配，不重写
- state_and_data_source: pending JSONL 文件保持为唯一数据源；sender_state 的 commit_id 语义不变（一个 trace 的所有分片发送成功后才 commit）
- risk_notes:
  - 分片发送引入部分成功风险（trace-create 发送成功但部分 generation/span 分片失败），但 Langfuse 支持增量追加，下次重试会补发
  - 预存在的 UUID 非幂等问题：重试时相同 generation/span 获得新 UUID，Langfuse 中产生重复观测项。splitting 放大此问题但不引入。后续可通过确定性 UUID 解决（UUID5(trace_id + generation_index)）
  - 413 fallback：split 后仍超限时，trace 转移到 failed/ 目录，advance commit_id，记录告警
- overdesign_guard: 最小设计 = (1) `_send_batch` 在序列化后检查大小并拆分 (2) 413 转移到 failed/ + advance commit_id (3) `max_payload_bytes` 配置项。不做：gzip 压缩（Langfuse 不支持）、内容截断、adaptive batch_size、sender 侧内存限制、确定性 UUID（后续改进）。

## Design Review Accepted Minor Items

- **UUID 重试幂等性**：预存在问题，splitting 放大。记录为后续改进项，不阻塞本设计。
- **主动 vs 反应式**：选择主动检查而非 split-on-413，理由是避免预期内的网络浪费和日志噪音。代价是正常路径多一次 `len()` 检查（可忽略）。

## Handoff To `writing-specs`

- design_source: `docs/designs/20260617-sender-payload-splitting.md`
- requirement themes:
  - sender payload 大小限制与分片发送
  - HTTP 413 错误处理：split 后仍超限时转移到 failed/ 目录 + advance commit_id，不再无限重试
  - `max_payload_bytes` 配置项（SenderConfig 新增字段，默认 3_500_000）
- negative boundaries:
  - 不修改 ingestor 写入逻辑或 pending JSONL 格式
  - 不做内容截断
  - 不修改 hook 数据收集逻辑
  - 不引入新模块或新依赖
  - 不解决 UUID 非幂等问题（记录为后续改进）
- verification intent:
  - 多条正常 trace 累积超限时，自动截断批次只发送已累积部分，剩余留到下一轮
  - 单条超大 trace（>max_payload_bytes）自动拆分为多个子 batch 请求，全部发送成功
  - 正常大小 trace 行为无变化（正常路径只增加累积 len() 检查）
  - HTTP 413 不再导致无限重试：split 后仍超限时转移到 failed/ + advance commit_id
  - commit_id 精确更新到已成功发送的最后一条 trace 的 seq_id
  - failed/ 目录中的数据可通过 FailedRecovery 机制在后续恢复
- product-intent confirmations:
  - 数据完整性：所有 generations/spans 均投递，不丢弃不截断
  - 413 fallback：数据不丢失（转移到 failed/），但不阻塞队列
  - 默认阈值 3.5MB（4.5MB 的安全余量）可配置
