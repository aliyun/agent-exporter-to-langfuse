# Unit Test Infrastructure — Implementation Plan

## Header
- source_spec: ../specs/20260614-unit-test-infrastructure.md
- risk: normal
- runtime_profile: normal/general
- runtime_profile_basis: additive-only work across 4 independent packages, no existing code modification, no shared truth source or runtime state
- accepted_debt: none
- status: ready
- external_review_policy: none

## Requirements Covered
- R-1: 为每个包建立可独立运行的测试基础设施
- R-2: 为纯逻辑和数据转换模块编写单元测试
- R-3: 为 I/O 和 HTTP 依赖模块编写 mock 驱动的单元测试
- R-4: 提供根目录统一测试入口

## Phases with Tasks

### phase-1: test infrastructure setup
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: (cd exporter && uv run pytest --co -q) && (cd hooks/claude-code/hooks && uv run pytest --co -q) && (cd hooks/langstash-deliver/python && uv run pytest --co -q) && (cd hooks/codex && pnpm vitest run --passWithNoTests)

#### task-1 [P]: Python packages test infrastructure
- requirements: [R-1]
- outputs: [exporter/pyproject.toml, exporter/tests/conftest.py, hooks/claude-code/hooks/pyproject.toml, hooks/claude-code/hooks/tests/conftest.py, hooks/langstash-deliver/python/pyproject.toml, hooks/langstash-deliver/python/tests/conftest.py]
- action: Add pytest (and pytest-asyncio for exporter) as optional dev dependencies in each Python package's pyproject.toml. Create tests/ directory with conftest.py in each package. Install dev dependencies in each venv.
- constraints:
  - Constraint: do not create root-level pytest.ini or pyproject.toml pytest config
  - Constraint: do not modify any existing source code files
- verification:
  - shell: cd exporter && uv run pytest --co -q
  - shell: cd hooks/claude-code/hooks && uv run pytest --co -q
  - shell: cd hooks/langstash-deliver/python && uv run pytest --co -q

#### task-2 [P]: TypeScript codex test infrastructure
- requirements: [R-1]
- outputs: [hooks/codex/package.json, hooks/codex/vitest.config.ts, hooks/codex/tests/]
- action: Add vitest as devDependency and a "test" script to package.json. Create vitest.config.ts and tests/ directory. Install dependencies.
- constraints:
  - Constraint: do not modify existing source files in hooks/codex/src/
- verification:
  - shell: cd hooks/codex && pnpm vitest run --passWithNoTests

### phase-2: unit test implementation
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: (cd exporter && uv run pytest -q) && (cd hooks/claude-code/hooks && uv run pytest -q) && (cd hooks/langstash-deliver/python && uv run pytest -q) && (cd hooks/codex && pnpm vitest run)

#### task-3 [P]: exporter package unit tests
- requirements: [R-2, R-3]
- outputs: [exporter/tests/]
- action: Write unit tests for all exporter modules: config, state, ingestor, sender, cleaner, server (via TestClient), and updater. Use tmp_path for file I/O, unittest.mock for httpx/subprocess.
- constraints:
  - Constraint: do not test emit_turn or _start_backdated (SDK-coupled)
  - Constraint: mock httpx.post for sender HTTP tests, mock subprocess.run for updater
  - Constraint: use deterministic timestamps via mock datetime.now/time.time
- verification:
  - shell: cd exporter && uv run pytest -q
  - inspect: tests cover functions enumerated in R-2/R-3 for exporter, each with normal path and at least one error/edge case; mock-driven modules (sender, updater) include I/O failure scenario

#### task-4 [P]: claude-code hooks unit tests
- requirements: [R-2]
- outputs: [hooks/claude-code/hooks/tests/]
- action: Write unit tests for langfuse_hook.py: pure helper functions, build_turns (turn assembly with dedup), read_new_jsonl (incremental read), and _build_trace_v2 (output structure).
- constraints:
  - Constraint: do not test emit_turn, _start_backdated, or main() (SDK/integration-coupled)
  - Constraint: _build_trace_v2 tests require langstash_deliver.schema on Python path
- verification:
  - shell: cd hooks/claude-code/hooks && uv run pytest -q
  - inspect: build_turns tests include multi-message dedup and tool_result association scenarios

#### task-5 [P]: langstash-deliver package unit tests
- requirements: [R-2, R-3]
- outputs: [hooks/langstash-deliver/python/tests/]
- action: Write unit tests for schema.py (build_trace_json, build_generation, build_span structure validation) and deliver.py (three-tier delivery: langstash success, langstash fail with direct push fallback, all-fail writes to failed log). Mock urllib for HTTP and use tmp_path for failed log file.
- constraints:
  - Constraint: mock urllib.request.urlopen, not real HTTP
- verification:
  - shell: cd hooks/langstash-deliver/python && uv run pytest -q
  - inspect: deliver_trace tests cover all three tiers

#### task-6 [P]: codex package unit tests
- requirements: [R-2, R-3]
- outputs: [hooks/codex/tests/]
- action: Write unit tests for all codex modules: utils, config, parse (parseSession), langstash (buildTraceV2), and sidecar. Use vi.mock for fs and vi.fn for fetch.
- constraints:
  - Constraint: pass env object to getConfig() for deterministic config tests
  - Constraint: mock node:fs/promises for sidecar tests
- verification:
  - shell: cd hooks/codex && pnpm vitest run
  - inspect: parseSession tests include session_meta extraction, multi-turn, and tool_call output association

### phase-3: unified test runner
- commit_boundary: task
- gate: shell: bash run-tests.sh

#### task-7: root-level test runner script
- requirements: [R-4]
- outputs: [run-tests.sh]
- action: Create a shell script that runs pytest in each Python package and vitest in the codex package, propagating exit codes so any failure results in non-zero overall exit.
- constraints:
  - Constraint: compatible with macOS and Linux (no GNU-only flags)
  - Constraint: no additional test orchestration dependencies
- verification:
  - shell: bash run-tests.sh
  - shell: (echo 'def test_fail(): assert False' >> exporter/tests/test_smoke_fail.py && bash run-tests.sh; rc=$?; rm -f exporter/tests/test_smoke_fail.py; exit $rc) && exit 1 || true
  - inspect: script runs all 4 package test suites and exits non-zero on any failure

## Verification
- shell: bash run-tests.sh (all 4 packages pass)
- inspect: no existing source files modified (only new test files, config changes, and run-tests.sh)
- inspect: no tests import or call emit_turn, emitTurnOtel, convertRollout, _start_backdated, or menubar
- inspect: no tests make real HTTP requests or depend on external services
