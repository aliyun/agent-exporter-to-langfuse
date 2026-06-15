# Unit Test Infrastructure Design

## Problem

项目当前没有任何单元测试文件和测试基础设施。需要为跨两种语言（Python、TypeScript）、四个独立包的项目建立统一的单元测试方案，使关键逻辑可验证、可回归。

## Context

- 项目由四个独立包组成，各自有独立的依赖管理：
  - `exporter/`（Python, FastAPI 服务，venv + pyproject.toml）
  - `hooks/claude-code/hooks/`（Python, 依赖 langfuse SDK，venv + pyproject.toml）
  - `hooks/langstash-deliver/python/`（Python, 零外部依赖，pyproject.toml）
  - `hooks/codex/`（TypeScript, ESM + tsdown, package.json）
- `exporter/` 和 `hooks/claude-code/hooks/` 各自有独立的 `.venv`；`hooks/langstash-deliver/python/` 无外部依赖也无 `.venv`，可直接用系统 Python 或临时 venv 运行测试
- TypeScript 包使用 ESM（`tsdown` 构建，`"module": "Node16"`）
- 外部依赖包括 Langfuse SDK (OTel)、httpx、fcntl（Unix only）、subprocess
- 大量函数是纯逻辑或仅依赖文件 I/O，可测试性高
- 非阻塞假设：CI 环境为 Linux，测试不需要 macOS 特定依赖（rumps）

## Goals

- 为每个包建立可独立运行的测试基础设施
- 优先覆盖纯逻辑和数据转换函数（最高 ROI）
- 测试可在 CI 中无外部服务依赖地运行
- 建立 mock 策略，使涉及文件 I/O、HTTP、外部 SDK 的代码可测

## Non-Goals

- 集成测试或端到端测试（不在本次范围）
- 覆盖 macOS 专属模块（`menubar.py`）
- 对 Langfuse SDK OTel 内部实现的深度 mock（`emit_turn`、`emitTurnOtel`、`convertRollout` 中的 OTel 调用等重度 SDK 耦合函数暂不覆盖）
- 测试覆盖率指标要求

## Options

### Option A: vitest (TS) + pytest (Python)，per-package 测试目录

每个包在自己的目录下建立 `tests/` 子目录，各自独立的测试配置：

- Python：pytest + pytest-asyncio，每个包的 `pyproject.toml` 添加 `[project.optional-dependencies] dev = ["pytest", ...]`
- TypeScript：vitest，添加到 `devDependencies`，`vitest.config.ts` 放在 `hooks/codex/`
- 根目录提供一个 `Makefile` 或 shell script 统一调用所有包的测试

评估：
- fit_to_existing_stack: 高 — pytest 是 Python 标准，vitest 是 ESM 原生且与 tsdown 生态匹配
- product_fit: 高 — 直接满足需求
- simplicity: 高 — 每个包独立，无跨包依赖冲突
- correctness: 高 — 各包用各自 venv 的依赖运行，避免导入冲突
- operability: 高 — 各包可单独运行也可统一运行
- ai_coding_fit: 高 — 每个测试文件独立、有界，适合逐模块编写

### Option B: 根级统一 pytest + vitest 配置

在项目根目录放置 `pytest.ini` / `pyproject.toml` 的 pytest 配置，发现所有 Python 测试；TypeScript 同理用一个 vitest 配置。

评估：
- fit_to_existing_stack: 低 — Python 各包有独立 venv，根级 pytest 无法同时使用多个 venv 的依赖
- product_fit: 中 — 表面上简化 CI，但实际需要复杂的 venv 切换
- simplicity: 低 — 需要解决跨 venv 导入问题、conftest 冲突
- correctness: 低 — 依赖隔离被打破，可能导致导入错误的包版本
- operability: 中 — 一条命令但出错时排查困难
- ai_coding_fit: 中 — 配置复杂度高，容易出错

### Option C: monorepo 工具统一管理（nx / turborepo）

引入 monorepo 工具来统一管理 Python + TypeScript 包的测试任务。

评估：
- fit_to_existing_stack: 低 — 项目未使用 monorepo 工具，引入重量级新依赖
- product_fit: 中 — 功能过剩
- simplicity: 低 — 需要大量配置和学习成本
- correctness: 中 — 工具本身可靠但引入不必要的复杂度
- operability: 中 — 强大但过度
- ai_coding_fit: 低 — 配置面大，不适合当前项目规模

## Recommendation

**选择 Option A：vitest (TS) + pytest (Python)，per-package 测试目录。**

理由：
1. 各包已有独立的依赖管理，per-package 测试是自然延伸
2. pytest + vitest 分别是各语言的主流测试框架，无学习成本
3. vitest 原生支持 ESM，与项目的 tsdown + Node16 module 完美匹配
4. 配置最小化，每个包只需少量新增文件

### 具体技术选型

**Python 测试栈：**
- pytest >= 8.0（测试框架）
- pytest-asyncio >= 0.24（FastAPI 异步路由测试）
- httpx（FastAPI TestClient 底层依赖，exporter 已有）
- 标准库 `unittest.mock`（mock 外部依赖）
- pytest 内置 `tmp_path` fixture（临时文件/目录）

**TypeScript 测试栈：**
- vitest >= 3.0（测试框架，ESM 原生）
- vitest 内置 `vi.mock` / `vi.fn`（模块和函数 mock）

### 目录结构

```
exporter/
  tests/
    test_config.py
    test_state.py
    test_ingestor.py
    test_sender.py
    test_cleaner.py
    test_server.py
    test_updater.py
    conftest.py

hooks/claude-code/hooks/
  tests/
    test_helpers.py        # extract_text, truncate_text, get_role 等
    test_build_turns.py    # build_turns 逻辑
    test_read_jsonl.py     # read_new_jsonl 增量读取
    test_trace_v2.py       # _build_trace_v2
    conftest.py

hooks/langstash-deliver/python/
  tests/
    test_schema.py         # build_trace_json, build_generation, build_span
    test_deliver.py        # deliver_trace（mock HTTP + 文件）
    conftest.py

hooks/codex/
  tests/
    parse.test.ts          # parseSession
    config.test.ts         # getConfig
    utils.test.ts          # truncate, toText, isPrimitive
    langstash.test.ts      # buildTraceV2
    sidecar.test.ts        # loadUploadedTurnIds, markTurnUploaded
```

### 模块优先级

| 优先级 | 模块 | 理由 |
|--------|------|------|
| P0 | `state.py`, `config.py`, `schema.py` | 纯数据/文件操作，核心基础，最高 ROI |
| P0 | `parse.ts`, `config.ts`, `utils.ts` | 纯逻辑，解析器是 codex hook 的核心 |
| P1 | `ingestor.py`（validate_trace, _accumulate_tokens, ingest） | 数据入口的验证和写入 |
| P1 | `langstash.ts`（buildTraceV2） | 纯数据转换 |
| P1 | `langfuse_hook.py`（helpers + build_turns） | 纯逻辑函数和核心 turn 组装 |
| P2 | `sender.py`（_build_ingestion_batch, _read_pending_traces） | 批量构建是纯逻辑，读取需文件 mock |
| P2 | `cleaner.py`, `server.py` | 需文件系统 mock 或 TestClient |
| P2 | `deliver.py` | 需 HTTP + 文件 mock |
| P3 | `updater.py`（_parse_semver） | 纯函数部分可测，subprocess 部分延后 |
| P3 | `stats.py` | 极简 dataclass，uptime_seconds 属性 |
| P3 | `sidecar.ts` | 简单文件 I/O |

### Mock 策略

**Python:**
- 文件系统：pytest `tmp_path` fixture 创建真实临时文件和目录
- HTTP（httpx）：`unittest.mock.patch` mock `httpx.post`，或使用 `httpx.MockTransport`
- FastAPI 路由：`fastapi.testclient.TestClient`（基于 httpx，同步调用异步端点）
- Langfuse SDK：不直接测试 `emit_turn`；测试 `_build_trace_v2` 等纯构建函数
- subprocess：`unittest.mock.patch("subprocess.run")` mock `git ls-remote`
- fcntl：测试中使用 `tmp_path` 的真实文件，fcntl 在 Linux/macOS 上可用
- 时间：`unittest.mock.patch` mock `datetime.now` 或 `time.time` 当需要固定时间

**TypeScript:**
- 模块 mock：`vi.mock("node:fs/promises")` mock 文件操作
- 函数 mock：`vi.fn()` 创建 mock 函数
- 环境变量：直接传入 env 对象（`getConfig(env)` 已支持参数注入）
- fetch：`vi.fn()` mock global fetch
- 时间：`vi.useFakeTimers()` 当需要固定时间

## Decisions

- tech_stack_choice: pytest（Python）+ vitest（TypeScript），均为各语言的主流选择，与现有技术栈一致
- compatibility_policy: no_compatibility — 新增测试基础设施，不涉及已有行为变更
- refactor_or_rewrite: 不涉及 — 纯新增
- state_and_data_source: 不涉及 — 测试不引入新的运行时状态
- risk_notes: 
  - `hooks/claude-code/hooks/langfuse_hook.py` 中 `emit_turn` 依赖 Langfuse SDK 4.x 内部 API（`_otel_tracer`、`_create_observation_from_otel_span`），不建议 mock 这些内部实现
  - `fcntl` 仅 Unix 可用，Windows CI 不支持（当前项目 target 为 macOS + Linux，非问题）
  - `hooks/claude-code/hooks/` 的 `langfuse_hook.py` import 了 `langstash_deliver` 包，测试时需确保该包在 Python path 中可用
- overdesign_guard: 最小设计 = 每个包添加 tests/ 目录 + 测试依赖 + 测试文件。不引入 coverage 工具、CI 配置、monorepo 工具、测试数据库、或测试容器。不为暂不测试的模块（menubar、emit_turn、emitTurnOtel）预设 mock 基础设施。

## Accepted Review Findings (minor)

1. `stats.py` 已补充到优先级表（P3）
2. 已修正 Context 中关于 `langstash-deliver/python/` 无独立 `.venv` 的描述
3. `trace.ts` 的 `convertRollout` 已明确列入 Non-Goals（重度 SDK 耦合）

## Handoff To `writing-specs`

- design_source: `docs/designs/unit-test-infrastructure.md`
- requirement themes:
  - Python 测试基础设施搭建（pytest 配置、依赖声明）
  - TypeScript 测试基础设施搭建（vitest 配置、依赖声明）
  - P0 模块单元测试实现
  - P1 模块单元测试实现
  - P2 模块单元测试实现
  - 统一测试入口（根目录 Makefile 或 script）
- negative boundaries:
  - 不实现集成测试、E2E 测试
  - 不覆盖 menubar.py、emit_turn、emitTurnOtel 等重度 SDK 耦合函数
  - 不引入 coverage 工具或覆盖率要求
  - 不修改任何现有源代码（仅新增测试文件和配置）
  - 不引入 monorepo 管理工具
- verification intent:
  - 每个包的测试可通过 `pytest` / `vitest run` 独立运行并全部通过
  - 根目录统一命令可运行所有包的测试
  - 所有测试不依赖外部服务（Langfuse、网络）
- product-intent confirmations:
  - 测试优先级按模块可测试性和业务价值排序
  - per-package 隔离而非统一配置
