# 修复 OpenCode Hook：每条 Assistant 消息产出独立 Generation

## Problem

OpenCode Hook（`hooks/opencode/hooks/langfuse-exporter.mjs`）在一个用户回合内，把用户消息之后所有连续的 assistant 消息合并成**单个** Langfuse generation span。一次 agentic 回合通常包含多条 assistant 消息（LLM 调用 → 工具执行 → LLM 再调用 → … → 最终回复），每条对应一次独立的模型生成。合并后 Langfuse 面板只看到一个 generation，无法分别观察每次 LLM 调用的模型、token 用量、成本、输入输出与时间区间；中间步骤的 per-call 元数据（`modelID`、`tokens`、`cost`、`finish`、`time`）在求和/取首尾后被抹平，丢失单次调用粒度。

仓库内已存在一份 20260721 的设计/spec/plan 三件套（`docs/designs/20260721-opencode-generation-per-assistant-message.md` 等），完成了完整的 ground 与 R-N 拆分，但尚未实现——当前 hook 源码仍为旧合并行为。本次方案在已有设计基础上重新确认事实、收束为可执行的 fresh brief。

## Context

**当前合并行为（已核实）**：

- `processSession`（`hooks/opencode/hooks/langfuse-exporter.mjs:329`）在用户消息后收集所有连续 assistant 消息到 `assistantEntries`（第 366-395 行）。
- `emitTurn`（第 408 行）将所有 assistant 消息的 parts 拍平：`assistantEntries.flatMap(e => e.parts || [])`，对 token 与 cost 跨消息求和（第 424-436 行），然后只调用一次 `buildOtlpJson`（第 464 行）。
- `buildOtlpJson`（第 162 行）只构造 1 个 generation span（`genSpan`，第 222 行），其下挂所有 tool span；root span 的 `langfuse.trace.output` 也只反映合并后的文本。

**每条 assistant 消息携带独立的 per-call 元数据（已核实）**：`info.modelID` / `info.providerID` / `info.tokens`（input、output、reasoning、cacheRead、cacheWrite）/ `info.cost` / `info.time.created` / `info.time.completed` / `info.finish` / `info.mode`。合并后这些 per-call 值丢失。

**仓库内参考实现（已核实，均为「每 assistant 消息/step 一个 generation」）**：

- Claude Code Hook（`hooks/claude-code/hooks/langfuse_hook.py:598`）：`for idx, am in enumerate(turn.assistant_msgs)` 为每条 assistant 消息创建一个 generation span；对应测试 `hooks/claude-code/hooks/tests/test_trace_v2.py:154` `test_generation_spans_count` 显式断言「2 条 assistant 消息 → 2 个 generation span」。
- Codex Hook（`hooks/codex/src/trace.ts:245`）：`for (let i = 0; i < turn.steps.length; i++)` 为每个 step 创建一个 generation span；非首步 generation 的 input 为上一步 tool 结果（第 264-269 行）。
- Qoder Hook（`hooks/qoder/hooks/langfuse_hook.py:808`）与 QoderWork Hook（`hooks/qoderwork/hooks/langfuse_hook.py:662`）同样按 assistant 消息逐条产出 generation。

OpenCode Hook 是仓库内唯一合并的 hook。其自身 README（`hooks/opencode/README.md:71`）已声明「Assistant response → Langfuse Generation (model, tokens, cost)」，即每条 assistant 响应对应一个 generation。

**投递层无需改动（已核实）**：`deliverTrace(otlpJson, options)`（`hooks/langstash-deliver/typescript/src/index.ts:109`）签名接收 `Record<string, unknown>`，不检查 span 数量或类型，单个 OTLP JSON 包含多个 generation span 是标准用法。

**测试缺口（已核实）**：OpenCode Hook 的 `buildOtlpJson` / `emitTurn` / `processSession` 无任何单元测试。仅有一条 E2E（`tests/e2e/test_opencode_langfuse_delivery.sh`）按 trace name 断言 trace 存在性，不断言 generation 结构。对比 Claude Code 有 `test_trace_v2.py`、Codex 有 `trace.test.ts`、Cursor 有 `trace.test.ts`。

## Goals

- 每条 OpenCode assistant 消息产出一个独立的 Langfuse generation span，承载该次 LLM 调用的模型、token 用量、成本、输入、输出与时间区间。
- 一个用户回合对应的 trace 内含 N 个 generation span（N = 该回合内 assistant 消息数），按时间顺序排列为 root span 的子 span。
- 工具调用 span 归属到产生它的 assistant 消息对应的 generation（`parentSpanId` = 该 generation 的 `spanId`），不再全部挂到单一合并 generation。
- trace 顶层 input 反映用户消息、顶层 output 反映最终 assistant 消息文本，与参考实现一致。
- 为 OpenCode Hook 补齐单元测试，覆盖 generation 数量、tool span 归属、per-generation 元数据独立性、root output 末条文本四项断言。

## Non-Goals

- 不改 `langstash-deliver` 包接口或投递通道——已核实其透传 OTLP JSON，无需改动。
- 不改其他 4 个 Hook——它们已是正确实现。
- 不回填已投递的历史 trace——合并 generation 无法事后拆分。
- 不引入 OTel GenAI 语义约定字段；继续使用 Langfuse 原生 `langfuse.*` 属性，与现有约定一致。
- 不处理 git 提交/推送——按仓库 `AGENTS.md` MUST NOT 约束，提交前需用户确认，属交付动作非设计边界。

## Recommendation

采用「每条 assistant 消息一个 generation span」方案。核心原因：仓库内 Claude Code、Codex、Qoder、QoderWork 四个参考实现已建立此结构为唯一约定，OpenCode Hook 自身 README 也已声明此意图，且每条 assistant 消息携带独立的 per-call 元数据——合并即丢失。不存在与之竞争的可信替代路径（保留合并行为直接违反文档意图与参考实现，不可信）。已有 20260721 设计/spec/plan 三件套完成了 ground 与 R-N 拆分，本次方案确认其设计决策仍然成立，可直接进入 spec 阶段。

按 owner 组织的未来状态：

- **turn 解析与 generation 分裂**（owner：OpenCode Hook 的 `emitTurn` / `buildOtlpJson`）：`emitTurn` 不再聚合所有 assistant 消息为一个输入；改为将 `assistantEntries` 数组直接传给 `buildOtlpJson`。`buildOtlpJson` 签名从「接收合并后的标量值」改为「接收 assistant 消息数组 + user 消息 + session 模型回退」，在内部循环构造 N 个 generation span。`processSession` 的 turn 划分逻辑不变（仍按用户消息切分）。

- **per-generation 元数据归属**（owner：每个 generation span）：每条 generation 携带该 assistant 消息自身的 `modelID` / `providerID`（按字段独立回退合成 `provider/model`）、`tokens`（独立计算，不跨消息求和）、`cost`、`finish`、`mode`、`toolCount`（该消息自身 tool part 数量）、`time.created` → `time.completed` 作为起止时间。

- **generation input 链路**（owner：`buildOtlpJson` 内循环）：首条 generation 的 input 为用户消息文本（与现行为一致）；后续 generation 的 input 反映上一步工具结果（与 Codex `trace.ts:264-269` 约定一致）。确切 input 结构（如工具结果数组 `[{name, output?, error?}]`）属于 exact contract，留给 `writing-specs`。

- **tool span 归属**（owner：每个 generation span）：`ToolPart` 挂到其所属 assistant 消息对应的 generation 下（`parentSpanId` = 该 generation 的 `spanId`）。一条 assistant 消息可含多个 tool part，每个独立成 span。tool span 的 input / output / metadata 取值逻辑不变。

- **trace 顶层 input/output**（owner：root span）：顶层 input = 用户消息文本；顶层 output = 最后一条 assistant 消息的文本（与 Claude Code `final_text` 约定一致），不再合并所有 assistant 文本。

- **测试覆盖**（owner：新增单元测试 `hooks/opencode/hooks/langfuse-exporter.test.mjs`）：使用 Node.js 内置 `node --test`，对齐 `test_trace_v2.py` 断言模式，覆盖 R-1 到 R-4 四项断言。在 `scripts/run-tests.sh` 添加 opencode 测试块。测试仅 import `buildOtlpJson` 纯函数，不触发 `deliverTrace` 或插件 hook side effects。

**兼容性**：旧「合并 generation」行为到此结束，不保留兼容层——合并行为从未被任何下游消费者依赖，参考实现也从未提供过合并语义。已投递历史 trace 不回填。

## Handoff To `writing-specs`
- review_route: `folded-design-review`
