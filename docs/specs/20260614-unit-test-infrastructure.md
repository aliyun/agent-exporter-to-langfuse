# Unit Test Infrastructure

## Purpose

项目当前没有任何测试文件和测试基础设施。为四个独立包（三个 Python + 一个 TypeScript）建立单元测试方案，覆盖核心逻辑和数据转换函数，使关键行为可验证、可回归，且所有测试可在 CI 中无外部服务依赖地运行。

## Non-Goals

- 集成测试或端到端测试
- 覆盖 macOS 专属模块（`menubar.py`）
- 对 Langfuse SDK OTel 内部实现的深度 mock（`emit_turn`、`emitTurnOtel`、`convertRollout` 中的 OTel 调用）
- 测试覆盖率指标或 coverage 工具
- `stats.py` 单独测试（极简 dataclass，仅通过 `/stats` 端点间接覆盖）
- CI pipeline 配置
- 修改任何现有源代码
- monorepo 管理工具

## Decisions

- design_source: `docs/designs/20260614-unit-test-infrastructure.md`
- Python 测试框架：pytest + pytest-asyncio，各包通过 `[project.optional-dependencies]` 声明测试依赖
- TypeScript 测试框架：vitest（ESM 原生，与 tsdown + Node16 module 匹配）
- 测试目录：每个包内建立 `tests/` 子目录，与源码平行
- Mock 策略：Python 用 `tmp_path` + `unittest.mock`；TypeScript 用 `vi.mock` / `vi.fn`

## Requirements

### R-1: 为每个包建立可独立运行的测试基础设施

- must:
  - `exporter/` 包的 `pyproject.toml` 声明 pytest、pytest-asyncio 为测试依赖，`exporter/tests/` 目录包含 `conftest.py`
  - `hooks/claude-code/hooks/` 包的 `pyproject.toml` 声明 pytest 为测试依赖，`hooks/claude-code/hooks/tests/` 目录包含 `conftest.py`
  - `hooks/langstash-deliver/python/` 包的 `pyproject.toml` 声明 pytest 为测试依赖，`hooks/langstash-deliver/python/tests/` 目录包含 `conftest.py`
  - `hooks/codex/` 包的 `package.json` 声明 vitest 为 devDependency，添加 `vitest.config.ts` 和 `"test"` script，`hooks/codex/tests/` 目录存在
  - 每个包的测试可在其自身目录内通过 `pytest` 或 `vitest run` 独立运行
- must_not:
  - 不在项目根目录放置 pytest.ini 或统一的 pytest 配置
  - 不引入 monorepo 测试编排工具
  - 不修改现有源代码文件
- verification:
  - 在每个包目录内分别执行 `pytest`（Python）或 `vitest run`（TypeScript），命令成功退出且无报错

### R-2: 为纯逻辑和数据转换模块编写单元测试

- context: 项目中大量函数是无副作用的纯逻辑（验证、解析、构建、转换），是测试 ROI 最高的部分。
- must:
  - **exporter** 包测试覆盖：
    - `config.py`：`load_config` 从 TOML 文件加载配置、缺失文件返回默认值、`set_config_value` 写入和更新 TOML section/key
    - `state.py`：`load_ingest_state` / `save_ingest_state` 序列化往返一致、`load_sender_state` / `save_sender_state` 序列化往返一致、`migrate_legacy_state` 从旧格式拆分为两个状态文件、`allocate_seq_id` 递增、`record_commit` / `record_error` / `update_file_entry` 状态更新正确
    - `ingestor.py`：`validate_trace` 对缺失字段返回正确错误、`_accumulate_tokens` 按日期累加 token、`ingest` 写入 pending 文件并更新状态
  - **langstash-deliver** 包测试覆盖：
    - `schema.py`：`build_trace_json` / `build_generation` / `build_span` 输出结构符合 Trace Schema v2
  - **claude-code hooks** 包测试覆盖：
    - `langfuse_hook.py` 的纯函数：`extract_text`、`truncate_text`、`get_role`、`is_tool_result`、`iter_tool_results`、`iter_tool_uses`、`get_model`、`get_usage`、`parse_ts`
    - `langfuse_hook.py` 的 `build_turns`：从 transcript 消息列表正确组装 Turn 结构（含 dedup、tool_result 关联）
    - `langfuse_hook.py` 的 `read_new_jsonl`：增量读取、partial line buffer、文件截断重置
    - `langfuse_hook.py` 的 `_build_trace_v2`：输出结构正确（依赖 `langstash_deliver.schema`）
  - **codex** 包测试覆盖：
    - `utils.ts`：`truncate`、`toText`、`isPrimitive`
    - `config.ts`：`getConfig` 从环境变量解析配置、缺失变量使用默认值、布尔值/tags/整数解析
    - `parse.ts`：`parseSession` 从 rollout JSONL 行组装 turns/steps/toolCalls，含 session_meta、turn 生命周期、tool_call 关联
    - `langstash.ts`：`buildTraceV2` 输出结构符合 Trace Schema v2
  - 所有纯逻辑测试使用确定性输入和断言，不依赖外部服务或网络
  - 涉及文件操作的测试使用 pytest `tmp_path`（Python）或 vitest 临时目录（TypeScript）
  - 涉及时间判断的测试使用固定时间（mock `datetime.now` / `time.time` 或 `vi.useFakeTimers()`），不依赖系统时钟
- must_not:
  - 不测试 `emit_turn`、`_start_backdated`、`emitTurnOtel`、`convertRollout` 等 Langfuse SDK OTel 耦合函数
  - 不测试 `menubar.py`
  - 测试不发起真实 HTTP 请求或连接外部服务
- verification:
  - 每个测试模块的测试用例覆盖正常路径和至少一个边界/错误路径
  - 所有测试在无网络环境下通过

### R-3: 为 I/O 和 HTTP 依赖模块编写 mock 驱动的单元测试

- context: sender、cleaner、server、deliver、updater 等模块涉及文件系统操作、HTTP 请求或子进程调用，需要 mock 策略才能单元测试。
- must:
  - **exporter** 包测试覆盖：
    - `sender.py`：`_build_ingestion_batch` 纯逻辑构建正确的 Langfuse batch 结构；`_read_pending_traces` 从 pending 目录正确读取并过滤已提交的 trace
    - `cleaner.py`：`_cleanup_retention` 按日期清理已提交文件；`_cleanup_size` 按大小上限清理
    - `server.py`：通过 FastAPI `TestClient` 测试 `/ingest`、`/stats`、`/health`、`/settings` 端点的请求响应
    - `updater.py`：`_parse_semver` 版本号解析
  - **langstash-deliver** 包测试覆盖：
    - `deliver.py`：`deliver_trace` 三层投递逻辑（langstash 成功 / langstash 失败回退 direct push / 全部失败写 failed log）
  - **codex** 包测试覆盖：
    - `sidecar.ts`：`loadUploadedTurnIds` 和 `markTurnUploaded` 文件操作
  - Python HTTP mock 使用 `unittest.mock.patch` 或 `httpx.MockTransport`
  - Python subprocess mock 使用 `unittest.mock.patch("subprocess.run")`
  - TypeScript fetch mock 使用 `vi.fn()` 替换 global fetch
  - TypeScript 文件 mock 使用 `vi.mock("node:fs/promises")`
- must_not:
  - 不发起真实 HTTP 请求
  - 不调用真实的 `git ls-remote` 或其他子进程
  - 不依赖 Langfuse 服务可用
- verification:
  - mock 驱动的测试覆盖成功路径和至少一个失败路径（如网络错误、认证失败）
  - 所有测试在无网络、无 Langfuse 服务环境下通过

### R-4: 提供根目录统一测试入口

- must:
  - 项目根目录提供一个 shell script 或 Makefile target，一条命令依次运行所有四个包的测试
  - 该命令在所有包的测试全部通过时返回 exit code 0，任一失败时返回非零
  - 脚本兼容 macOS 和 Linux（不使用 GNU 独有参数）
- must_not:
  - 不引入额外的测试编排依赖
- verification:
  - 在根目录执行统一命令，四个包的测试均被执行并通过
  - 故意使某包测试失败后，命令返回非零退出码（验证失败传播）
