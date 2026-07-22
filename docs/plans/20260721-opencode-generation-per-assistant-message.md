# 实现计划：OpenCode Hook 每条 Assistant 消息独立 Generation

## Direct Execution Record

```yaml
direct_execution_record:
  source_spec: /mnt/Projects/agent-exporter-to-langfuse/docs/specs/20260721-opencode-generation-per-assistant-message.md
  source_requirements_sha256: sha256:a7f77b98ee33111b6a3bd0a3e2eeab78eb5665b586b6370ee1d02a7e5c296bcb
  requirements: [R-1, R-2, R-3, R-4]
  scope: One owner — hooks/opencode/hooks/langfuse-exporter.mjs (the OpenCode Hook plugin: refactor emitTurn and buildOtlpJson to iterate assistant messages and emit N generation spans with per-message metadata, correct tool span parentage, subsequent-generation input chain, and root span output = last assistant text). Direct consumers — deliverTrace from hooks/langstash-deliver (unchanged interface, accepts any OTLP JSON with multiple generation spans) and langstash/Langfuse OTLP endpoints. Verification boundary — unit tests on the OTLP JSON structure produced by the hook, aligned with hooks/claude-code/hooks/tests/test_trace_v2.py assertion patterns, plus existing E2E trace-name query remaining satisfied.
  verification:
    - planned_test: Unit test asserting N generation spans for N assistant messages, each with independent model/usage_details/metadata/output from its own assistant message (R-1)
    - planned_test: Unit test asserting tool span parentSpanId equals owning generation spanId, not cross-linked (R-2)
    - planned_test: Unit test asserting non-first generation input = previous tool results array with {name, output?, error?}, omitted when no previous tools (R-3)
    - planned_test: Unit test asserting root span trace.output content = last assistant message text only, not merged (R-4)
    - existing_test: E2E test tests/e2e/test_opencode_langfuse_delivery.sh M4-6 trace-name query remains satisfied after structural change
    - source_scan: Inspect buildOtlpJson output OTLP JSON to confirm no merged-generation fallback path or compatibility layer remains (R-1 must_not)
```

## 1. Per-Repository Changes

### 1.1 `hooks/opencode/hooks/langfuse-exporter.mjs`（核心重构）

这是唯一的行为变更文件。所有 4 个 R-N 均在此文件中闭合。

#### a) `buildOtlpJson` 函数重构（R-1, R-2, R-3, R-4）

当前签名（line 162）接收合并后的标量值：
```
buildOtlpJson(langfuseSessionID, sessionID, turnNum, userText, assistantText, modelName, tools, usage, userTime, assistantStartTime, assistantEndTime, isSubagent, meta)
```

重构为接收结构化的 per-message 数据：
- `langfuseSessionID`, `sessionID`, `turnNum`, `isSubagent`（不变）
- `userMsg`（用户消息 info，用于 root span input + agent + time 回退）
- `userParts`（用户消息 parts，用于提取 userText）
- `assistantEntries`（assistant 消息数组，每个含 `info` 和 `parts`）
- `sessionModels`（session 级模型回退 Map，用于 per-field model 回退）

函数内部：
1. 生成 1 个 root span（traceId, rootSpanId）。
2. **Root span 属性**（R-4）：
   - `langfuse.trace.input` = `{role: "user", content: userText}`（不变）
   - `langfuse.trace.output` = `{role: "assistant", content: lastAssistantText}`（**改为仅末条 assistant 文本**，不再合并）
3. **循环 `assistantEntries`**（R-1），为每条消息生成 1 个 generation span：
   - 每个 generation 有独立的 `spanId`（`randomBytes(8)`）
   - `parentSpanId` = `rootSpanId`
   - `langfuse.observation.model.name`：按字段独立回退（per-field session fallback，匹配现行 lines 420-422 逻辑）
   - `langfuse.observation.usage_details`：仅来自该消息自身的 `info.tokens`
   - `langfuse.observation.metadata`：`finish`, `mode`, `toolCount`（该消息自身 tool part 数量）, `agent`（用户消息 agent）, `cost`（当存在）
   - `langfuse.observation.output`：`{role: "assistant", content?, tool_calls?}`（该消息自身的文本与工具）
   - `langfuse.observation.input`（R-3）：
     - 首条（i=0）：`{role: "user", content: userText}`
     - 后续（i≥2）：上一步 assistant 消息的 tool results 数组 `[{name, output?, error?}]`，无 tool part 时省略该属性
   - 时间区间：`info.time.created`→`info.time.completed`，按 spec R-1 的回退链处理
4. **Tool spans**（R-2）：在每个 generation 循环内，为该 assistant 消息的每个 tool part 生成 1 个 tool span：
   - `parentSpanId` = **当前 generation 的 spanId**（不再指向单一合并 generation）
   - input/output/metadata 按 `ToolPart.state` 取值（不变）
   - 过滤条件不变：`!s.status || s.status === 'completed' || s.status === 'error'`
5. 返回 OTLP JSON：`{resourceSpans: [{scopeSpans: [{scope, spans: [rootSpan, ...genSpans, ...toolSpans]}]}]}`

#### b) `emitTurn` 函数重构（R-1 调用方）

当前 `emitTurn`（line 408）合并所有 assistant 消息后调用 `buildOtlpJson` 一次。重构为：
- 不再 `flatMap` 所有 assistant parts
- 不再聚合 token/cost（删除 lines 424-436 的聚合循环）
- 不再提取合并的 `assistantText` / `tools`
- 直接将 `userMsg`、`userParts`、`assistantEntries`、`sessionModels` 传给重构后的 `buildOtlpJson`
- `buildOtlpJson` 内部完成 per-message 迭代

#### c) 导出供测试

在文件末尾的 `export default` 之外，添加 named exports：
```javascript
export { buildOtlpJson, extractText, extractTools, truncate };
```
这使单元测试可以直接 import 并测试 `buildOtlpJson` 的 OTLP JSON 输出。模块加载时的 `deliverTrace` import 在测试环境中通过 try/catch 静默失败，不影响 `buildOtlpJson`（纯函数）。

### 1.2 `hooks/opencode/hooks/langfuse-exporter.test.mjs`（新增单元测试）

使用 Node.js 内置测试运行器 `node --test`（Node 18+，无需额外依赖）。

测试结构对齐 `hooks/claude-code/hooks/tests/test_trace_v2.py` 的断言模式：

- **R-1 测试**：构造 2 条 assistant 消息（不同 model/token），调用 `buildOtlpJson`，断言 generation span 数量 = 2，每个 span 的 `model.name`/`usage_details`/`metadata.toolCount`/`output` 取自对应消息。
- **R-2 测试**：构造 2 条 assistant 消息各含 1 个 tool part，断言 2 个 tool span 的 `parentSpanId` 分别等于 2 个 generation span 的 `spanId`，不交叉。
- **R-3 测试**：构造 2 条 assistant 消息（首条含 1 个已完成 tool part 有非空 output），断言第 2 个 generation 的 `input` = `[{name, output}]` 的 JSON；再构造无 tool part 的场景，断言第 2 个 generation 无 `input` 属性。
- **R-4 测试**：构造 2 条 assistant 消息（文本 "A" 和 "B"），断言 root span `langfuse.trace.output` 的 `content` 仅为 "B"。

### 1.3 `scripts/run-tests.sh`（添加 OpenCode 测试套件）

在 codex 测试块之后添加：
```bash
echo ""
echo "=== hooks/opencode ==="
(cd "$SCRIPT_DIR/hooks/opencode" && node --test hooks/langfuse-exporter.test.mjs) || EXIT_CODE=1
```

### 1.4 无需改动的文件

- `hooks/langstash-deliver/typescript/src/index.ts` — `deliverTrace` 接口不变
- `hooks/opencode/install.sh` / `uninstall.sh` — 仍拷贝单一 `langfuse-exporter.mjs` 文件（新增的 `.test.mjs` 不参与安装）
- `deploy/package.sh` / `deploy/installer.sh` — 无结构变更
- 其他 hook（claude-code、codex、qoder、qoderwork）— 不涉及
- `tests/e2e/test_opencode_langfuse_delivery.sh` — 现有 M4-6 trace-name 查询断言不受结构变更影响

## 2. Order of Changes

1. **重构 `buildOtlpJson`**：改签名，实现 per-message 迭代循环，生成 N 个 generation span + per-generation tool spans + 正确的 root span output。这是核心行为变更。
2. **重构 `emitTurn`**：移除合并逻辑，改为直接传递 per-message 数据给 `buildOtlpJson`。
3. **添加 named exports**：在文件末尾添加 `export { buildOtlpJson, ... }`。
4. **编写单元测试**：创建 `langfuse-exporter.test.mjs`，覆盖 R-1 到 R-4 的所有断言。
5. **更新 `scripts/run-tests.sh`**：添加 OpenCode 测试套件。
6. **运行全部测试**：`bash scripts/run-tests.sh` 确认所有测试通过。
7. **运行 E2E Module 3**（可选）：`bash tests/e2e/test_opencode_langfuse_delivery.sh --module 3` 确认 hook install/uninstall 完整性不受影响。

无 deploy-order 约束——所有变更在同一仓库内，不涉及跨服务部署顺序。

## 3. Risks and Rollback Strategy

### 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| `buildOtlpJson` 签名变更导致 `emitTurn` 调用点遗漏 | 运行时报错，trace 不投递 | 重构后立即运行单元测试，确认调用链完整 |
| `randomBytes` 在测试中产生不确定 spanId | 测试无法断言确切 ID | 测试只断言 span 数量、parentSpanId 关联关系、属性值，不断言确切 ID 值（与 Claude Code 测试一致） |
| 模块 side effects（env 文件读取、log 目录创建）在测试环境中触发 | 测试环境产生副作用 | try/catch 静默处理；测试仅 import `buildOtlpJson`（纯函数），不触发 `deliverTrace` 或插件 hook |
| OpenCode 实际运行时 assistant 消息缺少 `info.time` 或 `info.tokens` | 时间/usage 属性回退到默认值 | 按 spec R-1 的回退链处理，与现行逻辑一致 |
| E2E M4-4 opencode run 超时（依赖 AI 模型响应速度） | E2E 无法验证实际投递 | M4-4 已有 timeout 降级逻辑（exit 124 → skip 验证），不影响实现正确性判断 |

### 回滚策略

- 所有变更在 `opencode_generation_per_msg_20260721` worktree 分支上进行，trunk 不受影响。
- 如果发现问题，直接 `git checkout main`（或删除 worktree 分支）即可回滚。
- 已投递的历史 trace（合并 generation）无法回填，但新 trace 将正确产出多 generation——这是预期行为，无需回滚历史数据。

## 4. Verification Strategy

### 单元测试（planned_test，确定性证据）

```bash
cd hooks/opencode && node --test hooks/langfuse-exporter.test.mjs
```

覆盖：
- R-1：N assistant 消息 → N generation span；per-generation model/tokens/cost/metadata/output 独立
- R-2：tool span `parentSpanId` 归属正确 generation
- R-3：非首条 generation input = 上一步 tool results 数组；无 tool part 时省略
- R-4：root span output = 末条 assistant 文本，不合并

### 全量测试（existing_test + build）

```bash
bash scripts/run-tests.sh
```

确认所有测试套件通过（exporter、claude-code、langstash-deliver、codex、**opencode 新增**）。

### E2E 测试（existing_test）

```bash
bash tests/e2e/test_opencode_langfuse_delivery.sh --module 3
```

Module 3 验证 hook install/uninstall 完整性（不依赖 Docker 或 AI 模型），确认文件拷贝、配置、卸载不受影响。

### 源码扫描（source_scan）

检查重构后的 `buildOtlpJson` 输出 OTLP JSON 中：
- 不存在合并 generation 的旧代码路径或 fallback
- 不存在跨消息聚合 token/cost 的逻辑
- 不存在将所有 tool span 挂到单一 generation 的逻辑

## 5. Post-Implementation: Commit and Push

用户原始请求包含「提交远程」。按仓库 `AGENTS.md` MUST NOT 约束：

- 实现与测试完成后，**不得自行 `git commit` / `git push`**。
- 必须向用户展示变更摘要与测试结果，**明确询问用户确认**后，才执行 commit 和 push 到 `origin`。
