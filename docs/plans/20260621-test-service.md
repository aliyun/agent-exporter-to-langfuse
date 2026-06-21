# Plan: langstash-tester E2E 测试服务

## Header
- source_spec: /Users/song/code/agent-exporter-to-langfuse/docs/specs/20260621-test-service.md
- risk: normal
- runtime_profile: normal/general
- runtime_profile_basis: greenfield service, no existing consumers or shared surfaces, no migration
- accepted_debt: none
- status: ready
- external_review_policy: none

## Requirements Covered
- R-1: HTTP API 提供 job 生命周期管理
- R-2: 实时进度追踪
- R-3: Git worktree 隔离执行
- R-4: 串行队列与同分支去重
- R-5: 超时与进程管理
- R-6: Webhook 回调通知
- R-7: 配置加载
- R-8: 结果持久化与清理
- R-9: 独立服务入口与生命周期管理
- R-10: E2E 脚本标准化输出格式

## Phases with Tasks

### phase-1: 项目骨架与配置
- commit_boundary: task
- gate: shell: cd test-service && uv run langstash-tester --help

#### task-1 [P]: 项目结构与配置加载
- requirements: [R-7, R-9]
- outputs: [test-service/pyproject.toml, test-service/src/config.py, test-service/src/main.py, test-service/src/__init__.py, test-service/src/__main__.py, test-service/VERSION]
- action: 创建 `test-service/` 独立 Python 包，包含 pyproject.toml（入口 `langstash-tester = "src.main:cli"`）、TOML 配置加载（所有 R-7 配置项及默认值，运行时基础目录 `~/.langstash-tester/`）、CLI 子命令骨架（run/start/stop/restart/status/install/uninstall）。`git.repo_url` 缺失时启动报错退出。版本号从 `test-service/VERSION` 读取（独立于 repo root VERSION）。运行时目录结构：`~/.langstash-tester/{config/, repo/, worktrees/, data/, logs/}`。
- constraints:
  - 不修改 exporter/pyproject.toml 或 deploy/ 下任何文件
  - CLI 入口名为 `langstash-tester`，与 `langstash` 完全独立
  - test-service/VERSION 是 langstash-tester 的独立版本号，与 repo root VERSION（langstash 版本）无关
  - 配置默认值全部基于 `~/.langstash-tester/` 基础目录：`repo/`、`worktrees/`、`data/langstash-tester.db`、`logs/`、`config/config.toml`
- verification:
  - shell: cd test-service && uv sync && uv run langstash-tester --help
  - inspect: pyproject.toml 的 [project.scripts] 指向 langstash-tester

### phase-2: 核心引擎（存储 + 队列 + worker + git）
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: cd test-service && uv run pytest -q

#### task-2: SQLite 存储层
- requirements: [R-1, R-8]
- outputs: [test-service/src/store.py]
- action: 实现 SQLite 存储层，提供 job CRUD 操作（create/get/list/update/delete），job_id 生成（`e2e-YYYYMMDD-6位随机`），过期清理方法（按 result_retention_days 删除终态 job 及对应日志文件）并注册定期调度（服务启动时和每小时触发一次）。schema 包含 job 的所有状态字段（status/branch/commit/mode/progress/summary/exit_code/timestamps/metadata）。
- constraints:
  - db_path（默认 `~/.langstash-tester/data/langstash-tester.db`）和 log_dir（默认 `~/.langstash-tester/logs/`）从配置读取
  - 清理不删除 running 或 pending 的 job
- verification:
  - planned_test: 创建/查询/列出/更新 job 的单元测试

#### task-3: 串行队列与同分支去重
- requirements: [R-4]
- outputs: [test-service/src/queue.py]
- action: 实现内存 FIFO 队列，支持 enqueue/dequeue/cancel/position 查询。实现同分支去重策略（replace/queue/reject），replace 模式下取消同分支的 pending 或 running 旧 job。队列保证同一时刻只有一个 job 在执行。
- constraints:
  - replace 取消 running job 时需要通过回调通知 worker 终止进程
  - 单元测试必须可独立运行（mock 外部依赖），不依赖 store 或 git_manager
- verification:
  - planned_test: replace/queue/reject 三种策略的行为测试

#### task-4: Git worktree 管理器
- requirements: [R-3]
- outputs: [test-service/src/git_manager.py]
- action: 实现 git 操作封装：启动时校验 bare repo 存在性、fetch origin、创建 worktree（checkout 指定 branch/commit）、integration 模式下 merge origin/main（失败时提取冲突文件列表）、清理 worktree。worktree 路径为 `<worktree_dir>/<job_id>/`。
- constraints:
  - 不修改 bare clone 的分支/ref
  - worktree 中不执行 git push 或 git commit（integration merge 除外）
  - 单元测试必须可独立运行（使用临时 git repo），不依赖 store 或 queue
- verification:
  - planned_test: worktree 创建/清理、integration merge 冲突检测

#### task-5: E2E 标记行解析器与 helper 脚本
- requirements: [R-10]
- outputs: [test-service/src/progress_parser.py, tests/e2e/e2e-helpers.sh]
- action: 实现 `##e2e##` 标记行解析器，逐行解析 stdout 中的 `suite`/`case`/`pass`/`fail`/`summary` 标记，输出结构化 progress（passed/failed/total/current_test）。提供 `tests/e2e/e2e-helpers.sh` bash helper，包含 `e2e_suite`/`e2e_case`/`e2e_pass`/`e2e_fail`/`e2e_summary` 函数，同时输出标记行和人类可读的彩色文本。
- constraints:
  - 标记行不含 ANSI 转义序列
  - 解析器对非标记行静默忽略，解析异常不抛错
- verification:
  - planned_test: 解析器对标准标记行、混合输出、无标记行的行为测试

#### task-6: Worker 执行引擎
- requirements: [R-2, R-3, R-5]
- outputs: [test-service/src/worker.py]
- action: 实现后台 worker 线程，从队列取 job 执行：git fetch → 创建 worktree → 若 worktree 中存在 `exporter/pyproject.toml` 则在 worktree 目录内运行 `uv sync` → 运行测试命令 → 使用 progress_parser 解析 stdout 中的 `##e2e##` 标记行更新 progress → 收集结果 → 清理 worktree。支持超时终止（SIGTERM→5s→SIGKILL）、graceful shutdown（SIGTERM/SIGINT 等待 30s）。日志实时写入 `<log_dir>/<job_id>.log`。
- depends_on: [task-2, task-3, task-4, task-5]
- constraints:
  - progress 解析失败不影响测试执行
  - 超时后不残留 zombie 进程或未清理的 worktree
  - uv sync 在 worktree 目录内执行，.venv 位于 worktree 下
- verification:
  - planned_test: 正常执行、超时终止、进度解析的测试

### phase-3: HTTP API 与回调
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: cd test-service && uv run pytest -q

#### task-7: FastAPI 路由
- requirements: [R-1, R-2]
- outputs: [test-service/src/server.py]
- action: 实现 FastAPI 应用，提供 `POST /e2e/jobs`、`GET /e2e/jobs/{job_id}`、`GET /e2e/jobs`、`POST /e2e/jobs/{job_id}/cancel`、`GET /e2e/jobs/{job_id}/logs`、`GET /health` 路由。创建 job 时异步入队返回 202。查询时从 store 读取状态，running job 附加实时 progress。不存在的 job_id 返回 404。使用 phase-2 产出的 store/queue/worker 模块。
- constraints:
  - 不包含认证/鉴权
  - POST /e2e/jobs 不同步执行，异步入队
  - GET /health 的 version 从 test-service/VERSION 读取
- verification:
  - planned_test: 各端点的请求/响应测试（含 404、409、202）

#### task-8: Webhook 回调
- requirements: [R-6]
- outputs: [test-service/src/webhook.py]
- action: 实现异步 webhook 通知器，job 进入终态时 POST 结果到 callback_url。重试 3 次（间隔 5s/15s/30s），超时 10 秒。回调不阻塞 worker 取下一个 job。callback_url 为空时不发送。
- verification:
  - planned_test: 回调成功、回调失败重试、无 callback_url 时不发送

### phase-4: 服务生命周期与集成测试
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: cd test-service && uv run pytest -q

#### task-9: 服务启动与 CLI 子命令实现
- requirements: [R-9]
- outputs: [test-service/src/main.py, test-service/install.sh, test-service/uninstall.sh]
- action: 完善 `langstash-tester run` 启动逻辑（组装 config → 校验 bare repo → 初始化 store → 启动 worker → 启动 FastAPI），组装 phase-2 和 phase-3 的所有模块。实现 start/stop/restart/status（systemd/launchd 服务管理）。实现 install/uninstall 脚本（幂等，普通卸载保留 config/data/logs，`--purge` 删除整个 `~/.langstash-tester/` 目录）。默认配置文件路径 `~/.langstash-tester/config/config.toml`。
- constraints:
  - install/uninstall 保持幂等
  - 普通 uninstall 删除：服务注册、CLI wrapper、repo/、worktrees/；保留：config/、data/、logs/
  - 不影响 langstash 的安装状态、进程或数据
- verification:
  - shell: cd test-service && uv run langstash-tester run --config test-config.toml &; sleep 2; curl -s http://127.0.0.1:5289/health; kill %1
  - inspect: install.sh 和 uninstall.sh 不引用 langstash 或 deploy/installer.sh

#### task-10: 端到端集成测试
- requirements: [R-1, R-2, R-3, R-4, R-5, R-6, R-8, R-9, R-10]
- outputs: [test-service/tests/test_e2e_integration.py]
- action: 编写集成测试：启动服务 → 创建 job（指向本仓库 main 分支）→ 轮询 progress 变化（验证 `##e2e##` 标记行解析）→ 等待终态 → 验证 summary 和 logs 端点 → 验证过期清理。覆盖 cancel、timeout、replace 去重场景。验证 webhook 回调（带 callback_url 创建 job，使用 mock HTTP server 验证回调到达）。验证 install.sh/uninstall.sh 不操作 langstash 相关路径（`~/.agent-exporter-to-langfuse/`、`~/.local/bin/langstash`）。
- depends_on: [task-9]
- verification:
  - shell: cd test-service && uv run pytest tests/test_e2e_integration.py -q

## Verification
- shell: cd test-service && uv run pytest -q
- shell: cd test-service && uv run langstash-tester --help
- inspect: test-service/ 目录独立于 exporter/，有自己的 pyproject.toml
- inspect: deploy/package.sh 和 deploy/installer.sh 未被修改
