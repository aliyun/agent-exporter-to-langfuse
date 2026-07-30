# 实现计划：OpenCode Hook 每条 Assistant 消息产出独立 Generation

## Header
- source_spec: ../specs/20260727-opencode-generation-per-assistant-message.md
- source_requirements_sha256: sha256:34da536abb2f5941f935b980b2023940f39adbf61c45327d5b42ea2a8792fcb6
- accepted_debt: none
- status: draft
- external_review_policy: none

## Requirements Covered
- R-1: 每条 assistant 消息产出一个独立 generation span
- R-2: 每个 generation span 承载该次调用的独立元数据
- R-3: 工具调用 span 归属到其所属 assistant 消息的 generation span
- R-4: 非首条 generation 的 input 反映上一步工具结果
- R-5: trace root span 的 input 为用户消息文本，output 为末条 assistant 消息文本
- R-6: OTLP JSON 构造为可独立导入的无副作用纯函数

## Phases with Tasks
### phase-1: 每条 assistant 消息独立 generation 的 OTLP 构造与单元测试

#### task-1: 重写 buildOtlpJson 为按消息产出 generation 的纯函数并补齐单元测试
- requirements: [R-1, R-2, R-3, R-4, R-5, R-6]
- outputs:
  - hooks/opencode/hooks/langfuse-exporter.mjs
  - hooks/opencode/hooks/langfuse-exporter.test.mjs
  - scripts/run-tests.sh
- action: 将 `buildOtlpJson` 由「接收合并后标量值、构造单个 generation span」重写为「接收 assistant 消息数组 + 用户消息 + session 级模型回退、循环构造恰好 N 个 generation span」的纯函数并对外 `export`（R-6）；循环内每条 assistant 消息对应一个 generation span，作为 root span 子 span（parentSpanId = root spanId），按消息时间顺序排列（R-1）；每个 generation 的 model.name 按该消息自身 modelID/providerID 独立回退合成、usage_details 仅取该消息自身 tokens（零值省略，不跨消息求和）、metadata 含该消息自身 finish/mode/toolCount/agent 与（存在时）cost、起止时间取该消息自身 info.time.created/completed（R-1、R-2）；每个 generation 下挂其所属 assistant 消息的 tool span（parentSpanId 指向该 generation spanId，tool span 过滤条件与 input/output/metadata 取值不变）（R-3）；首条 generation 的 input 为 `{role:"user",content:<用户消息文本>}`，第 i 条（i≥2）当第 i−1 条消息含已完成/出错 tool part 时 input 为工具结果数组 `[{name,output?,error?}]`，否则该属性省略（R-4）；root span 的 trace.input 为用户消息文本、trace.output 为最后一条 assistant 消息文本（无文本时为空串），不拼接多条（R-5）；同步改造 `emitTurn` 直接将 `assistantEntries` 数组与 session 级模型回退传入新 `buildOtlpJson`，移除旧 `flatMap` 拍平、token 跨消息求和、cost 累加、finish 取末条/mode 取首条的合并逻辑（R-1 must_not：旧合并语义结束，不保留兼容层或回退路径）；新增 `langfuse-exporter.test.mjs` 使用 Node 内置 `node --test`（零外部依赖，对齐仓库内 claude-code `test_trace_v2.py` 断言模式），仅 import `buildOtlpJson` 纯函数、不触发 `deliverTrace`/插件 hook 副作用，覆盖 R-1 到 R-6 断言；在 `scripts/run-tests.sh` 追加 opencode 测试块。
- verification:
  - planned_test: 含 2 条 assistant 消息的 turn → OTLP JSON 恰好 2 个 `langfuse.observation.type=generation` span，parentSpanId 均等于 root span 的 spanId，按 startTimeUnixNano 升序与 assistant 消息顺序一致（R-1）
  - planned_test: tokens 分别为 `{input:10,output:5}` 与 `{input:20,output:8}` 的 2 条 assistant 消息 → 2 个 generation 的 usage_details 各只含自身值，input 分别为 10 与 20、不得出现求和值 30；metadata 各含自身 finish/mode/toolCount/agent，cost 不跨消息累加（R-2）
  - planned_test: 每条 assistant 消息各含 1 个 tool part 的 turn → 2 个 tool span 的 parentSpanId 分别等于 2 个 generation span 的 spanId，不交叉（R-3）
  - planned_test: 首条 assistant 消息含 1 个已完成 tool part（非空 state.output）的 turn → 第 2 个 generation 的 `langfuse.observation.input` 为含该 tool 名与 output 的数组 JSON，非用户消息文本（R-4）
  - planned_test: 文本分别为 "A" 与 "B" 的 2 条 assistant 消息 → root span 的 `langfuse.trace.output` 中 content 仅为 "B"、不含 "A"；末条无文本 part 时 content 为空字符串（R-5）
  - planned_test: 直接 `import` `buildOtlpJson` 并构造含 2 条 assistant 消息的 turn 输入，断言返回的 OTLP JSON 满足 R-1 到 R-5，且函数执行期间不触发任何外发请求或文件写入（不调用 deliverTrace/curlFetch/writeLogFile）（R-6）
  - shell: `node --test hooks/opencode/hooks/langfuse-exporter.test.mjs` 全部通过，且 `scripts/run-tests.sh` 追加 opencode 块后整体退出码 0
