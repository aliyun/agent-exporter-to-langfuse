# Sender Payload Splitting

## Purpose

Sender 将整个 batch（最多 `batch_size` 条 trace 展开的所有 items）一次性 POST 到 Langfuse ingestion API。当单条 trace 包含大量 generations/spans 时（实测 277 gen + 561 span = 5.5MB），请求体超过 Langfuse Cloud 的 4.5MB 限制，返回 HTTP 413 并无限重试，阻塞所有后续数据投递。需要在 sender 侧引入 payload 大小感知的投递策略。

## Non-Goals

- 不修改 ingestor 写入逻辑或 pending JSONL 文件格式。
- 不截断或压缩 generation/span 内容。
- 不修改 hook 数据收集逻辑。
- 不引入新模块或新依赖。
- 不解决 UUID 非幂等问题（`_build_ingestion_batch` 每次调用生成新 UUID 导致重试时 Langfuse 中产生重复观测项；记录为后续改进项）。

## Decisions

- design_source: `docs/designs/20260617-sender-payload-splitting.md`
- 投递策略优先以 trace 为单位累积，只有单条 trace 超限时才做 item 级别拆分。
- 413 fallback 采用 "转移到 failed/ + advance commit_id" 策略，不丢数据但不阻塞队列。
- `max_payload_bytes` 作为 `SenderConfig` 的新字段，默认值 3_500_000（为 Langfuse 4.5MB 限制留余量）。
- "序列化大小"统一定义为：对单条 trace 的 batch items 做 `json.dumps` 后取 UTF-8 字节长度，逐条累加。`{"batch": []}` 外层结构和逗号的常量开销作为安全余量包含在阈值与 Langfuse 限制的差值中（3.5MB vs 4.5MB），不要求精确到字节。

## Requirements

### R-1: Trace 级别累积发送，payload 大小不超过阈值

- context: 当前 `_send_batch` 将最多 `batch_size` 条 trace 的所有 items 合并为一个 `{"batch": [...]}` 发送，不检查序列化后的大小。单条超大 trace 或多条 trace 累积后超过 Langfuse 4.5MB 限制即触发 HTTP 413。`_read_pending_traces` 从 `commit_id` 之后开始读取，因此 `commit_id` 精确更新到已发送的最后一条 trace 的 `_seq_id` 即可确保未发送 trace 在下一轮被重新读取。`_read_pending_traces` 本身无需修改。
- must:
  - sender 逐条 trace 构建 ingestion batch items 并累积序列化大小（单条 trace items 的 `json.dumps` UTF-8 字节长度之和）。
  - 当加入下一条 trace 会使累积 payload 超过 `max_payload_bytes` 时（严格大于，等于时仍包含），停止累积，只发送已累积的 trace 的 items。未发送的 trace 留到下一轮。
  - 发送成功后 `commit_id` 更新到已发送的最后一条 trace 的 `_seq_id`，而非整批 trace 的 `max(_seq_id)`。
  - 对于未超过 `max_payload_bytes` 的正常批次，行为与当前一致（一次发送所有 trace）。
- must_not:
  - 不得将整批 trace 一次性序列化后才检查大小（必须逐条累积以支持精确截断）。
  - 不得丢弃因超限而未发送的 trace 数据——这些 trace 通过不更新 commit_id 的方式自然留到下一轮。
- verification:
  - 3 条 trace 各 1.5MB，`max_payload_bytes=3_500_000`：第一批发送 2 条（3MB），commit_id 更新到第 2 条的 seq_id；第二轮发送第 3 条。
  - 3 条 trace 各 100KB：一批全部发送，行为与当前一致。

#### Scenario: 多条 trace 累积超限
- given: pending 中有 3 条 trace（seq_id 分别为 1, 2, 3），各 1.5MB，`max_payload_bytes=3_500_000`
- when: sender 执行 `_send_batch`
- then: 发送包含前 2 条 trace items 的请求（约 3MB），commit_id 更新到 2；第 3 条留到下一轮。

### R-2: 单条超大 trace 的 item 级别拆分

- context: 当单条 trace 的 items 序列化后超过 `max_payload_bytes`（如 277 gen + 561 span = 5.5MB），trace 级别截断无法解决，因为这是批次中的第一条（也是唯一一条）。
- must:
  - 当第一条 trace 单条即超过 `max_payload_bytes` 时，将该 trace 的 batch items 拆分为多个子 batch，每个子 batch 的序列化大小不超过 `max_payload_bytes`。
  - trace-create item 必须包含在第一个子 batch 中。
  - 所有子 batch 逐个发送，全部成功后才 `record_commit`。
  - 任一子 batch 发送失败时，停止发送后续子 batch：若失败原因为 HTTP 413，适用 R-3 处理路径（写入 failed/ + advance commit_id）；若为其他错误（网络错误、5xx 等），走现有错误处理路径（record_error + 下一轮重试所有子 batch）。
  - 当单个 batch item 的序列化大小超过 `max_payload_bytes` 时，将该 item 单独作为一个子 batch 发送（不可再分），由服务端决定接受或拒绝。若服务端返回 413，适用 R-3 处理路径。
- must_not:
  - 不得拆分单个 batch item（一个 generation-create 或 span-create 不可再分）。
- verification:
  - 单条 trace 含 277 generations + 561 spans（约 5.5MB），`max_payload_bytes=3_500_000`：拆分为 2+ 个子 batch，全部发送成功后 commit。
  - 单条 trace 含 5 generations 共 200KB：不触发拆分，正常发送。
  - 拆分后子 batch 发送返回 413：适用 R-3 处理路径，trace 转移到 failed/，不无限重试。
  - 拆分后第 2 个子 batch 返回 500：停止发送第 3 个子 batch，不 commit，下一轮重试所有子 batch。

#### Scenario: 单条超大 trace 拆分成功
- given: pending 中有 1 条 trace，包含 200 个 generations 和 400 个 spans，总 items 序列化后为 5MB，`max_payload_bytes=3_500_000`
- when: sender 执行 `_send_batch`
- then: 生成 2 个子 batch（每个 ≤3.5MB），逐个 POST，全部成功后 commit_id 更新到该 trace 的 seq_id。

#### Scenario: 子 batch 部分成功后失败
- given: 单条超大 trace 拆分为 3 个子 batch
- when: 第 1 个子 batch 发送成功，第 2 个子 batch 返回 500
- then: 停止发送第 3 个子 batch，不 commit 该 trace，走 record_error + 下一轮重试路径。下一轮重试时所有子 batch 重新生成并发送。

### R-3: HTTP 413 不再无限重试

- context: 当前 sender 对 413 走通用 fallback 路径（record_error + raise RuntimeError），触发指数退避无限重试。由于 payload 大小不变，重试永远不会成功。R-1 和 R-2 的主动分片策略应在绝大多数场景下避免触发 413。如果分片后仍然收到 413，说明 Langfuse 侧限制比 `max_payload_bytes` 更严格，属于异常情况。当 R-3 在 R-2 部分子 batch 已发送成功后触发时，完整原始 trace JSONL 写入 `failed/`，后续 recover 会重新 ingest 整条 trace，对已发送的子 batch 中的 items 产生重复观测项。此行为与 Non-Goals 中记录的 UUID 非幂等问题一致，将一并在后续确定性 UUID 改进中解决。
- must:
  - HTTP 413 响应时，将触发 413 的 trace 原始 JSONL 数据写入 `failed/` 目录。
  - 写入 `failed/` 的文件为 JSONL 格式，每行是原始 pending 行（与 `pending/*.jsonl` 行格式一致），文件名为 `<ISO-date>-<trace_id>.jsonl`。`recover_failed` 会重新分配 `_seq_id`，原始 `_seq_id` 在恢复后不保留。
  - 写入 `failed/` 后 advance `commit_id` 跳过该 trace，避免队列死锁。
  - 记录 warning 级别日志，包含 trace id 和 payload 大小信息。
- must_not:
  - HTTP 413 不得触发指数退避重试循环。
  - 不得直接丢弃 trace 数据（必须写入 `failed/` 后才 advance commit_id）。
- verification:
  - 模拟 Langfuse 返回 413：trace 数据出现在 `failed/` 目录，commit_id 前进，sender 继续处理后续 trace。
  - `failed/` 中的文件为 JSONL 格式，文件名匹配 `<date>-<trace_id>.jsonl`，`recover_failed` 的 `glob("*.jsonl")` 能扫描到并成功恢复。
  - R-2 拆分后子 batch 返回 413 时，同样适用本处理路径。

### R-4: `max_payload_bytes` 配置项

- context: Langfuse Cloud 限制 4.5MB，self-hosted 可能不同。需要可配置的阈值。
- must:
  - `SenderConfig` 新增 `max_payload_bytes` 字段，类型 `int`，默认值 `3_500_000`。值低于 `100_000` 时在 `load_config` 中钳位到 `100_000` 并记录 warning 日志。
  - 通过 `config.toml` 的 `[sender]` section 读取，字段名 `max_payload_bytes`。
  - `load_config` 读取该字段的逻辑与现有 `SenderConfig` 字段一致（缺省时使用默认值）。
- must_not:
  - 不得新增配置 section 或配置文件。
- verification:
  - `config.toml` 中未设置 `max_payload_bytes` 时，使用默认值 3_500_000。
  - `config.toml` 中设置 `max_payload_bytes = 5000000` 时，sender 使用 5_000_000 作为阈值。
