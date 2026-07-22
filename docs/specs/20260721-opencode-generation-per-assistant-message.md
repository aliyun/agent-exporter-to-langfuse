# 修复 OpenCode Hook：每条 Assistant 消息独立 Generation

## Purpose

OpenCode Hook（`hooks/opencode/hooks/langfuse-exporter.mjs`）当前将一个用户回合内所有连续 assistant 消息合并成单个 Langfuse generation span。在 agentic 回合中，一次用户消息通常触发多次 LLM 调用（LLM 调用 → 工具执行 → LLM 再调用 → … → 最终回复），每条 assistant 消息对应一次独立生成。合并后 Langfuse 只能看到一个 generation，无法分别观察每次 LLM 调用的模型、token 用量、成本、输入输出与时间区间。

仓库内 Claude Code Hook（`hooks/claude-code/hooks/langfuse_hook.py`）与 Codex Hook（`hooks/codex/src/trace.ts`）均已实现「每条 assistant 消息/step 一个 generation span」，OpenCode Hook 自身 README（`hooks/opencode/README.md`）也声明此意图。本 spec 将 OpenCode Hook 对齐此约定，使每条 assistant 消息产出一个独立 generation span。

## Decisions
- design_source: `/mnt/Projects/agent-exporter-to-langfuse/docs/designs/20260721-opencode-generation-per-assistant-message.md`

## Requirements

### R-1: 每条 assistant 消息产出一个独立 generation span 并承载该次调用的元数据

- context: 当前 `emitTurn`（`hooks/opencode/hooks/langfuse-exporter.mjs`）将一个 turn 内所有连续 assistant 消息的 parts 拍平合并，对 token 与 cost 跨消息求和，然后只调用一次 `buildOtlpJson` 构造 1 个 generation span。每条 assistant 消息携带独立的 `info.modelID`/`info.providerID`/`info.tokens`/`info.cost`/`info.time.created`/`info.time.completed`/`info.finish`/`info.mode`，合并后这些 per-call 值丢失。本要求改为按 assistant 消息逐条产出 generation span。
- must:
  - 对一个 turn 内的 N 条连续 assistant 消息（N ≥ 1），OpenCode Hook 产出的 OTLP JSON 中包含恰好 N 个 `langfuse.observation.type` 为 `generation` 的 span，作为 root span 的子 span，按对应 assistant 消息的时间顺序排列。
  - 每个 generation span 的 `langfuse.observation.model.name` 属性值按字段独立回退合成：`modelID` 取对应 assistant 消息自身的 `info.modelID`，缺失时回退到 `chat.params` hook 记录的 session 级 `modelID`，再缺失时为 `unknown`；`providerID` 取对应 assistant 消息自身的 `info.providerID`，缺失时回退到 session 级 `providerID`，再缺失时为空字符串；当 `providerID` 非空时合成 `providerID/modelID`，否则仅用 `modelID`。
  - 每个 generation span 的 `langfuse.observation.usage_details` 属性值仅来自其对应 assistant 消息自身的 `info.tokens`（input、output、reasoning、cacheRead、cacheWrite），不得跨 assistant 消息求和。
  - 每个 generation span 的起始时间为对应 assistant 消息的 `info.time.created`，缺失时回退到用户消息时间，再缺失时回退到当前时间；结束时间为 `info.time.completed`，缺失时回退到该 generation 自身的起始时间。
  - 每个 generation span 的 `langfuse.observation.metadata` 包含该 assistant 消息自身的 `finish`、`mode`、`toolCount`（该消息自身的 tool part 数量，不跨消息求和）与 `agent`（取自用户消息的 `agent` 值）；当 `info.cost` 存在时还包含 `cost`，不得跨消息求和 cost。
  - 每个 generation span 的 `langfuse.observation.output` 属性值为 JSON 序列化的对象，`role` 为 `assistant`；当对应 assistant 消息含文本 part 时包含 `content`（文本拼接，排除 `synthetic` 与 `ignored` part），当含 tool part 时包含 `tool_calls`（每个含 `id`、`name`、`input`），两者可同时存在或其一为空。
- must_not:
  - 不得将多条 assistant 消息合并为单个 generation span。
  - 不得在 generation span 之间聚合 token、cost 或合并文本作为单个 generation 的属性值。
  - 不得保留兼容旧合并行为的回退路径或兼容层；旧「合并 generation」语义到此结束。
- verification:
  - 对包含 2 条 assistant 消息的 turn，断言 OpenCode Hook 产出的 OTLP JSON 中 generation span 数量恰好为 2，且每个 span 的 model、usage_details、metadata（含 per-message `toolCount`）、output 取自对应 assistant 消息自身的值，而非聚合值。

#### Scenario: 单条 assistant 消息的 turn

一个 turn 只含 1 条 assistant 消息时，产出 1 个 generation span，行为与改造前单条场景一致，仅由新代码路径产出。

### R-2: 工具调用 span 归属到其所属 assistant 消息的 generation span

- context: 当前所有 tool span 的 `parentSpanId` 都指向唯一的合并 generation span。改为每条 assistant 消息独立 generation 后，一条 assistant 消息内的 tool part 应挂到该消息对应的 generation 下，使工具调用与其触发的 LLM 生成正确关联。
- must:
  - 每个 `langfuse.observation.type` 为 `tool` 的 span 的 `parentSpanId` 必须等于其所属 assistant 消息对应的 generation span 的 `spanId`。
  - tool span 的 input/output/metadata 属性按现有逻辑取自 `ToolPart.state`（`input`、`output`/`error`、`status`、`time.start`/`time.end`、`tool`、`callID`），不受 generation 分裂影响。
- must_not:
  - 不得将一个 assistant 消息的 tool span 挂到其他 assistant 消息的 generation span 下。
- verification:
  - 对含 2 条 assistant 消息、每条各含 1 个 tool part 的 turn，断言 2 个 tool span 的 `parentSpanId` 分别等于 2 个 generation span 的 `spanId`，不交叉。

### R-3: 非首条 generation 的 input 反映上一步工具结果

- context: 在 agentic 回合中，后续 LLM 调用的输入是上一步工具执行的结果。当前合并行为无此概念。改为独立 generation 后，首条 generation 的 input 为用户消息文本，后续 generation 的 input 为上一步 assistant 消息的工具结果，与 Codex Hook（`hooks/codex/src/trace.ts`）约定一致。
- must:
  - 一个 turn 内首条 generation span 的 `langfuse.observation.input` 属性值为 JSON 序列化的 `{role: "user", content: <用户消息文本>}`，与现行首条行为一致。
  - 一个 turn 内第 i 条 generation（i ≥ 2）的 `langfuse.observation.input` 属性值：当第 i−1 条 assistant 消息含已完成或出错的 tool part（即 `state.status` 缺失、`'completed'` 或 `'error'`，与 R-2 tool span 的过滤条件一致）时，为 JSON 序列化的工具结果数组，每个元素含 `name`（tool 名）、`output`（截断后的 `state.output`，当 `state.output` 缺失时省略 `output` 字段）和（当 `state.error` 存在时）`error`；当第 i−1 条 assistant 消息不含此类 tool part 时，该属性省略。
- must_not:
  - 非首条 generation 的 input 不得为用户消息文本或前一条 assistant 消息的文本。
- verification:
  - 对含 2 条 assistant 消息、首条含 1 个已完成 tool part（有非空 `state.output`）的 turn，断言第 2 个 generation span 的 `langfuse.observation.input` 为包含该 tool 名与 output 的数组 JSON。

### R-4: trace root span 的 input 为用户消息文本，output 为末条 assistant 消息文本

- context: 当前 root span 的 `langfuse.trace.output` 为所有 assistant 消息文本的拼接。改为独立 generation 后，trace 层应反映用户输入与最终回复，与 Claude Code Hook 的 `final_text` 约定一致。
- must:
  - root span 的 `langfuse.trace.input` 属性值为 JSON 序列化的 `{role: "user", content: <用户消息文本>}`，与现行行为一致。
  - root span 的 `langfuse.trace.output` 属性值为 JSON 序列化的 `{role: "assistant", content: <最后一条 assistant 消息的文本>}`，不得拼接多条 assistant 消息的文本。
  - 当最后一条 assistant 消息无文本 part 时，`content` 为空字符串。
- must_not:
  - root span 的 output 不得为多条 assistant 消息文本的拼接或聚合。
- verification:
  - 对含 2 条 assistant 消息（文本分别为 "A" 和 "B"）的 turn，断言 root span 的 `langfuse.trace.output` 中 `content` 仅为 "B"，不含 "A"。

