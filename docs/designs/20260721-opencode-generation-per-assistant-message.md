# 修复 OpenCode Hook：每条 Assistant 消息独立 Generation

## Problem

OpenCode Hook（`hooks/opencode/hooks/langfuse-exporter.mjs`）在处理一个用户回合时，会把用户消息之后**所有连续的 assistant 消息合并成单个 Langfuse generation span**。一次 agentic 回合通常包含多条 assistant 消息（LLM 调用 → 工具调用 → LLM 再调用 → … → 最终回复），每条对应一次独立的模型生成。当前实现把这些独立生成压成一条，导致：

- Langfuse 面板只看到一个 generation，无法观察每次 LLM 调用的输入/输出、模型、token 用量与成本。
- 中间含工具调用的 assistant 消息的 per-call 元数据（`modelID`、`tokens`、`cost`、`finish`、`time`）在聚合后被平均化/求和，丢失单次调用粒度。
- 与仓库内其他两个参考实现（Claude Code、Codex）行为不一致——它们均为「每条 assistant 消息/step 一个 generation span」。

需要决定：是否将 OpenCode Hook 改为按 assistant 消息逐条产出 generation span，使每条 assistant 消息对应一个独立的 Langfuse generation observation。

## Context

**当前行为（已核实）**：

- `processSession`（`hooks/opencode/hooks/langfuse-exporter.mjs:329`）在用户消息后收集所有连续 assistant 消息到 `assistantEntries`（第 366-395 行）。
- `emitTurn`（第 408 行）将所有 assistant 消息的 parts 拍平合并：`assistantEntries.flatMap(e => e.parts || [])`，对 token 与 cost 在所有 entry 上求和（第 424-436 行），然后**只调用一次** `buildOtlpJson`（第 464 行）。
- `buildOtlpJson`（第 162 行）只构造 **1 个 generation span**（`genSpan`，第 222 行）+ 其下的所有 tool span；trace 顶层 input/output 也只反映合并后的文本。

**每条 assistant 消息携带独立的 per-call 元数据（已核实）**：`info.modelID` / `info.providerID` / `info.tokens`（input、output、reasoning、cacheRead、cacheWrite）/ `info.cost` / `info.time.created` / `info.time.completed` / `info.finish` / `info.mode`。这些字段在合并后被求和或取首/尾值，无法还原单次调用。

**仓库内参考实现（已核实，均「每 assistant 消息/step 一个 generation」）**：

- Claude Code Hook（`hooks/claude-code/hooks/langfuse_hook.py:598`）：`for idx, am in enumerate(turn.assistant_msgs)` 为每条 assistant 消息创建一个 generation span；对应测试 `hooks/claude-code/hooks/tests/test_trace_v2.py:154` `test_generation_spans_count` 显式断言「2 条 assistant 消息 → 2 个 generation span」。
- Codex Hook（`hooks/codex/src/trace.ts:245`）：`for (let i = 0; i < turn.steps.length; i++)` 为每个 step 创建一个 generation span；非首步 generation 的 input 为上一步 tool 结果（第 264-269 行）。

**OpenCode Hook 自身文档已声明此意图**：`hooks/opencode/README.md:71`「Assistant response → Langfuse Generation (model, tokens, cost)」，第 63 行数据流描述「nested generation and tool spans」隐含多个 generation。

**投递层无需改动（已核实）**：`deliverTrace(otlpJson, options)`（`hooks/langstash-deliver/typescript/src/index.ts:109`）只把 OTLP JSON 透传到 langstash `/ingest` 或 Langfuse OTLP 端点；单个 OTLP JSON 包含多个 generation span 是标准用法，参考实现已验证可行。

**用户请求中的「提交远程」**是交付动作（推送到 `origin`），非设计决策；按仓库 `AGENTS.md` MUST NOT，提交/推送前需用户确认。此约束不在设计边界内，留给实现阶段处理。

## Goals

- 每条 OpenCode assistant 消息产出一个独立的 Langfuse generation span，承载该次 LLM 调用的模型、token 用量、成本、输入、输出与时间区间。
- 一个用户回合对应的 trace 内含 N 个 generation span（N = 该回合内 assistant 消息数），按时间顺序作为 root span 的兄弟 span 排列。
- 工具调用 span 归属到其所属的 generation span（即由产生该工具调用的 assistant 消息对应的 generation 持有），不再全部挂到单一合并 generation 下。
- trace 顶层 input 反映用户消息、顶层 output 反映最终 assistant 消息内容，与参考实现一致。

## Non-Goals

- 不改 `langstash-deliver` 包接口或投递通道。
- 不改其他 4 个 Hook（claude-code、codex、qoder、qoderwork）——它们已是正确实现。
- 不回填已投递的历史 trace（历史 trace 的合并 generation 无法事后拆分）。
- 不在本设计中处理「提交远程」——那是交付动作，由实现阶段按 `AGENTS.md` 流程在用户确认后执行。
- 不引入新的 OTel GenAI 语义约定字段；继续使用 Langfuse 原生 `langfuse.*` 属性，与现有约定一致。

## Recommendation

采用「每条 assistant 消息一个 generation span」方案。核心原因：仓库内 Claude Code 与 Codex 两个参考实现已建立此结构为唯一约定，OpenCode Hook 自身 README 也已声明此意图，且每条 assistant 消息携带独立的 per-call 元数据——合并即丢失。不存在与之竞争的可信替代路径（保留合并行为直接违反文档意图与参考实现，不可信）。

按 owner 组织的未来状态：

- **turn 解析与 generation 分裂**（owner：OpenCode Hook 的 `emitTurn`/`buildOtlpJson`）：`emitTurn` 不再聚合所有 assistant 消息为一个输入；改为按 assistant 消息迭代，每条产出独立 generation span。`buildOtlpJson` 的签名从「接收合并后的 assistantText/tools/usage」改为「接收 assistant 消息数组 + user 消息」，在内部循环构造 N 个 generation span。
- **per-generation 元数据归属**（owner：每个 generation span）：每条 generation 携带该 assistant 消息自身的 `modelID`/`providerID`（合成 `provider/model` 形式）、`tokens`（独立计算，不跨消息求和）、`cost`、`finish`、`mode`、`time.created`→`time.completed` 作为起止时间。
- **generation input 链路**（owner：`buildOtlpJson` 内循环）：首条 generation 的 input 为用户消息文本（与现行为一致）；后续 generation 的 input 反映上一步工具结果（与 Codex `trace.ts:264-269` 约定一致）。确切 input 结构（如 `{role:"tool", content:...}` vs 工具结果数组）属于 exact contract，留给 `writing-specs`。
- **tool span 归属**（owner：每个 generation span）：`ToolPart` 挂到其所属 assistant 消息对应的 generation 下（`parentSpanId` = 该 generation 的 spanId），而非单一合并 generation。一条 assistant 消息可含多个 tool part，每个独立成 span。
- **trace 顶层 input/output**（owner：root span）：顶层 input = 用户消息文本；顶层 output = 最后一条 assistant 消息的文本（与 Claude Code `final_text` 约定一致），不再合并所有 assistant 文本。
- **turn 边界与计数**（owner：`processSession`）：turn 仍按用户消息切分（一个 user 起始到下一个 user 之前为一个 turn），`turnNum` 计数逻辑不变；变更只影响 turn 内 generation 数量，不影响 turn 划分。

**兼容性**：旧「合并 generation」行为到此结束，不保留兼容层——合并行为从未被任何下游消费者依赖，参考实现也从未提供过合并语义。已投递历史 trace 不回填。

**测试覆盖**：当前 OpenCode Hook 无单元测试（仅 E2E），与 Claude Code Hook 有 `test_trace_v2.py` 单测形成对比。本设计要求新增 OpenCode Hook 的单元测试，至少覆盖「N 条 assistant 消息 → N 个 generation span」「tool span 归属正确 generation」「per-generation token/cost 独立」三项断言，与 `test_trace_v2.py:154` 对齐。E2E 测试 `tests/e2e/test_opencode_langfuse_delivery.sh` 已有的「Trace in Langfuse with OpenCode attributes」断言不受结构变更影响（仍按 trace name 查询），但可考虑在 E2E 层增加多 generation 的可观测断言——是否新增 E2E 断言属于测试覆盖决策，留给 `writing-specs`/`spec-to-plan`。

## Handoff To `writing-specs`
- review_route: `folded-design-review`
