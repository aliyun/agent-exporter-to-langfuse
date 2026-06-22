# test-service：E2E 测试常驻服务

## Purpose

提供一个独立的 HTTP 常驻服务（test-service），运行在专用 VM 上，接受外部调用方（orchestrator、CI/CD、人工）通过 HTTP API 发起 E2E 测试任务。服务从 Git 仓库获取指定分支代码，在隔离的 worktree 中执行 `tests/e2e/` 下的测试脚本，实时暴露任务进度，并在完成后返回结构化结果。当前聚焦 E2E 测试，服务命名为 test-service 以预留后续扩展其他测试类型的空间。

## Non-Goals

- 不执行单元测试（`scripts/run-tests.sh`）。test-service 仅运行 `tests/e2e/` 目录下的 E2E 脚本。
- 不提供 Web UI 仪表盘。调用方通过 HTTP API 查询即可。
- 不支持并发执行多个 job（`max_concurrent = 1`）。多个请求串行排队。
- 不修改本项目的核心功能（exporter / hooks / deploy）。test-service 是独立辅助工具。
- 不负责 orchestrator-agent workflow DAG 的改造。workflow 侧的 `test` 节点适配由 orchestrator-agent 项目完成。
- 不支持多仓库。当前仅服务于 agent-exporter-to-langfuse 单仓库。

## Decisions

- design_source: `docs/designs/20260619-e2e-testing-service.md`
- 服务命名：CLI 命令名为 `langstash-tester`，与采集服务 `langstash` 完全独立。
- 技术栈：Python + FastAPI，与本项目 exporter 保持一致。
- 存储：SQLite 持久化 job 状态和结果。日志按 job 落盘到独立文件。
- Git 隔离：使用 git worktree 为每个 job 提供独立工作目录，job 完成后清理。
- 代码位置：`test-service/` 目录，独立于 `exporter/`。
- 调用方无关：HTTP API 不感知调用方身份，orchestrator、CI、curl 均可调用。

## Requirements

### R-1: HTTP API 提供 job 生命周期管理

- context: 调用方需要通过标准 HTTP 接口创建 E2E 任务、查询进度和状态、获取日志、取消任务、列出历史任务。API 路径以 `/e2e/` 为前缀，为后续扩展其他测试类型（如 `/contract/`）预留命名空间。
- must:
  - `POST /e2e/jobs` 创建 E2E 任务，接受 `branch`（必填）、`commit`（可选，默认 branch HEAD）、`mode`（`branch` 或 `integration`，默认 `branch`）、`test_command`（可选，默认运行 `tests/e2e/` 下所有可执行脚本）、`timeout_seconds`（可选，默认 1800）、`callback_url`（可选）、`metadata`（可选，透传给回调和查询结果）。`same_branch_policy` 不支持 per-request 覆盖，统一使用全局配置值。返回 202 + `job_id`、`status`、`created_at`、`position`（队列位置）。`job_id` 格式为 `e2e-<YYYYMMDD>-<6位随机后缀>`，全局唯一。
  - `GET /e2e/jobs/{job_id}` 返回 job 的完整状态，包含 `status`、`branch`、`commit`、`mode`、`created_at`、`started_at`、`finished_at`、`duration_seconds`、`exit_code`、`progress`、`summary`、`output_tail`（最后 N 行输出）、`metadata`。
  - `GET /e2e/jobs` 列出任务，支持 `status`、`branch`、`limit` 查询参数过滤。
  - `POST /e2e/jobs/{job_id}/cancel` 取消 pending 或 running 的 job。pending job 直接移出队列；running job 终止子进程后标记 `cancelled`。
  - `GET /e2e/jobs/{job_id}/logs` 返回该 job 的完整 stdout/stderr 输出文本。
  - 不存在的 `job_id` 返回 404。
- must_not:
  - API 不包含认证/鉴权机制（服务运行在内网 VM，由网络隔离保障安全）。
  - `POST /e2e/jobs` 不同步执行测试，必须异步入队后立即返回 202。
- verification:
  - 创建 job 后立即查询返回 `status: pending`；job 执行中查询返回 `status: running` + `progress` 字段；完成后查询返回终态 + `summary`。
  - 取消 pending job 后查询返回 `status: cancelled`。
  - 对不存在的 job_id 查询返回 404。

### R-2: 实时进度追踪

- context: 调用方（特别是 orchestrator）需要在测试运行期间了解执行进度，以便向用户展示状态或判断是否超时。
- must:
  - job 状态查询（`GET /e2e/jobs/{job_id}`）在 `status: running` 时返回 `progress` 对象，包含：`phase`（当前阶段）、`passed`（已通过用例数）、`failed`（已失败用例数）、`total`（总用例数，已知时填充）、`current_test`（当前正在执行的测试名）、`elapsed_seconds`（已用时间）。
  - `progress.phase` 取值为 `pending`、`preparing`、`merging`、`testing`、`completed` 之一。
  - Worker 解析 E2E 脚本 stdout 中的 `##e2e##` 标记行来更新 `passed` / `failed` / `total` 计数（格式定义见 R-10）。
- must_not:
  - 进度解析失败不影响测试执行。解析异常时 `progress` 中的计数保持上次已知值。
- verification:
  - 启动一个耗时较长的 E2E job，在执行期间多次查询，`progress.passed` 随测试推进递增。

### R-3: Git worktree 隔离执行

- context: test-service 需要从远程仓库获取指定分支的代码并在隔离环境中执行测试，不能影响其他 job 或基础仓库。
- must:
  - 服务启动时使用配置的 `git.repo_url` 维护一个 bare clone 在 `git.local_repo` 路径。
  - 每个 job 执行前 `git fetch origin` 获取最新远程状态。
  - 每个 job 创建独立的 git worktree 在 `git.worktree_dir/<job_id>/`，checkout 到指定 branch（或 commit）。
  - `mode: integration` 时，在 worktree 中执行 `git merge origin/main --no-edit`。merge 失败（冲突）时 job 状态标记为 `conflict`，结果中包含冲突文件列表。
  - job 完成（无论成败）后删除 worktree（`git worktree remove --force`）。
  - worktree 内执行 E2E 测试前，如果仓库根目录存在 `exporter/pyproject.toml`，在 worktree 目录内运行 `uv sync` 初始化 Python 虚拟环境。生成的 `.venv` 位于 worktree 目录下，job 清理时随 worktree 一并删除。
- must_not:
  - 不修改 bare clone 的任何分支或 ref（只 fetch，不 push/merge/commit）。
  - worktree 中不执行 `git push` 或 `git commit`（integration 模式的临时 merge 除外）。
  - integration 模式的临时 merge 不影响远程仓库的任何分支。
- verification:
  - 同一分支连续创建两个 job，两者在不同 worktree 目录中执行，互不影响。
  - integration 模式下制造一个与 main 冲突的分支，job 返回 `status: conflict` 和冲突文件列表。

#### Scenario: integration 模式下 merge 冲突
- given: feature 分支修改了 `README.md` 第一行，main 分支也修改了 `README.md` 第一行
- when: 创建 `mode: integration` 的 E2E job
- then: job 状态为 `conflict`，`summary` 中包含 `conflict_files: ["README.md"]`

### R-4: 串行队列与同分支去重

- context: 服务同一时刻只执行一个 job（`max_concurrent = 1`）。多个请求需要排队。同一分支的重复请求需要去重策略避免队列堆积。
- must:
  - job 按 FIFO 顺序执行。新 job 入队时 `position` 字段反映当前队列位置（0 = 下一个执行）。
  - 同分支去重策略由配置 `e2e.same_branch_policy` 控制，取值为 `replace`（默认）、`queue`、`reject`：
    - `replace`：同分支有 pending 或 running 的旧 job 时，取消旧 job（running 的先终止进程），新 job 入队。
    - `queue`：新旧都保留，按 FIFO 排队。
    - `reject`：同分支有 pending 或 running 的 job 时，拒绝新请求，返回 409。
- must_not:
  - 不同时执行两个 job。Worker 必须等当前 job 完全结束（含 worktree 清理）后才取下一个。
- verification:
  - 快速连续创建同一分支的两个 job（`replace` 模式），第一个变为 `cancelled`，第二个正常执行。

### R-5: 超时与进程管理

- context: E2E 测试可能因环境问题挂起，需要超时保护。
- must:
  - 每个 job 有执行超时，默认 1800 秒，可在创建时通过 `timeout_seconds` 覆盖。
  - 超时时终止测试子进程树（SIGTERM → 等待 5 秒 → SIGKILL），job 状态标记为 `timeout`。
  - 服务正常退出（SIGTERM/SIGINT）时，等待当前 running job 完成（最多 30 秒），然后清理所有 worktree。
- must_not:
  - 超时后不残留 zombie 子进程或未清理的 worktree。
- verification:
  - 创建一个 `timeout_seconds: 5` 的 job 运行一个耗时脚本，5 秒后 job 状态变为 `timeout`。
  - 服务收到 SIGTERM 时，等待 running job 完成或 30 秒超时后退出，退出后 worktree 目录已清理。

### R-6: Webhook 回调通知

- context: 部分调用方（如支持 webhook 的 orchestrator）希望在 job 完成时被动通知，避免持续轮询。
- must:
  - 创建 job 时如果指定了 `callback_url`，job 进入终态（success/failed/conflict/timeout/cancelled）后 POST 结果到该 URL。
  - 回调 payload 包含 `event`（`e2e.completed`）、`job_id`、`status`、`branch`、`commit`、`duration_seconds`、`exit_code`、`summary`、`output_tail`、`metadata`。
  - 回调失败重试 3 次，间隔 5s/15s/30s。回调超时 10 秒。
  - 回调失败不影响 job 的最终状态。
- must_not:
  - `callback_url` 为空时不发送任何回调。
  - 回调不阻塞 Worker 取下一个 job。回调在后台异步执行。
- verification:
  - 创建带 `callback_url` 的 job，job 完成后目标 URL 收到 POST 请求，payload 包含完整的 job 结果。

### R-7: 配置加载

- context: 服务需要通过 TOML 配置文件和环境变量控制运行参数。
- must:
  - 通过 `--config` 参数指定 TOML 配置文件路径。
  - 运行时基础目录为 `~/.langstash-tester/`，跨平台友好（Linux/macOS/Windows 均支持 `~` 展开）。
  - 配置项包含：`server.host`、`server.port`（默认 5289）、`git.repo_url`、`git.local_repo`（默认 `~/.langstash-tester/repo`）、`git.worktree_dir`（默认 `~/.langstash-tester/worktrees`）、`storage.db_path`（默认 `~/.langstash-tester/data/langstash-tester.db`）、`storage.log_dir`（默认 `~/.langstash-tester/logs`）、`e2e.default_test_dir`（默认 `tests/e2e`）、`e2e.max_concurrent`（默认 1）、`e2e.default_timeout_seconds`（默认 1800）、`e2e.result_retention_days`（默认 30）、`e2e.same_branch_policy`（默认 `replace`）、`webhook.retry_count`、`webhook.retry_delays`、`webhook.timeout_seconds`。
  - `git.repo_url` 必填，无默认值。缺失时服务启动报错并退出。
- must_not:
  - 配置文件中不包含敏感信息（API key、密码）。Git SSH 认证通过 VM 的 SSH agent 完成。
- verification:
  - 不提供 `git.repo_url` 时服务启动失败并输出清晰的错误信息。

### R-8: 结果持久化与清理

- context: job 结果需要持久化以支持查询，过期数据需要清理避免磁盘占满。
- must:
  - job 状态和结果存储在 SQLite 数据库中，路径由配置项 `storage.db_path` 控制，默认 `~/.langstash-tester/data/langstash-tester.db`。
  - 每个 job 的完整日志（stdout/stderr）存储为独立文件，目录由配置项 `storage.log_dir` 控制，默认 `~/.langstash-tester/logs/`，文件名为 `<job_id>.log`。
  - 后台定期清理超过 `e2e.result_retention_days` 的 job 记录和对应日志文件。
- must_not:
  - 清理不删除 running 或 pending 的 job。
- verification:
  - 创建并完成一个 job 后，通过 `GET /e2e/jobs/{job_id}/logs` 可获取完整日志。

### R-9: 独立服务入口与生命周期管理

- context: langstash-tester 是一个完全独立于采集业务（langstash）的服务，拥有独立的安装、卸载、启停命令和部署流程。采集业务的 `install.sh` / `uninstall.sh` / `langstash start|stop|restart` 对 langstash-tester 无任何影响，反之亦然。
- must:
  - CLI 命令名为 `langstash-tester`，提供以下子命令：
    - `langstash-tester run --config <path>` — 前台启动服务。
    - `langstash-tester start` — 启动后台服务（systemd/launchd）。
    - `langstash-tester stop` — 停止后台服务。
    - `langstash-tester restart` — 重启后台服务。
    - `langstash-tester status` — 显示服务状态和版本。
    - `langstash-tester install` — 安装服务（部署文件、注册 systemd/launchd 服务）。
    - `langstash-tester uninstall [--purge]` — 卸载服务。
  - `langstash-tester uninstall`（普通卸载）删除以下内容：服务注册（systemd/launchd）、CLI wrapper（`~/.local/bin/langstash-tester`）、`~/.langstash-tester/repo/`（bare clone，可重建）、`~/.langstash-tester/worktrees/`（临时文件）。
  - `langstash-tester uninstall`（普通卸载）保留以下用户数据：`~/.langstash-tester/config/`、`~/.langstash-tester/data/`（含 `langstash-tester.db`）、`~/.langstash-tester/logs/`。
  - `langstash-tester uninstall --purge` 删除整个 `~/.langstash-tester/` 目录及 CLI wrapper。
  - 服务启动时校验 bare repo（`git.local_repo`，默认 `~/.langstash-tester/repo`）是否存在，不存在则报错退出并提示手动执行 `git clone --bare <repo_url> <local_repo>`。
  - 提供 `GET /health` 端点返回 `{"status": "ok", "version": "<version>"}`。
  - 代码位于 `test-service/` 目录下，独立于 `exporter/`，有自己的 `pyproject.toml`。
  - 默认配置文件路径为 `~/.langstash-tester/config/config.toml`。
  - install / uninstall 脚本保持幂等。
- must_not:
  - 不集成到 langstash CLI（`langstash run` / `langstash start` 等命令不启动 langstash-tester）。
  - langstash 的 `deploy/installer.sh install` / `uninstall` 不影响 langstash-tester 的安装状态、进程、配置或数据。
  - langstash-tester 的 `install` / `uninstall` 不影响 langstash 的安装状态、进程、配置或数据。
  - 不修改 `deploy/package.sh` 或 `deploy/installer.sh`。langstash-tester 有独立的部署脚本。
- verification:
  - `langstash-tester run --config config.toml` 启动后 `GET /health` 返回 200。
  - 执行 `langstash uninstall --purge` 后，langstash-tester 进程仍在运行，数据未受影响。
  - 执行 `langstash-tester uninstall` 后，langstash 进程仍在运行，数据未受影响。
  - 执行 `langstash-tester uninstall` 后，`~/.langstash-tester/config/`、`~/.langstash-tester/data/`、`~/.langstash-tester/logs/` 仍存在；`~/.langstash-tester/repo/` 和 `~/.langstash-tester/worktrees/` 已删除。
  - 执行 `langstash-tester uninstall --purge` 后，`~/.langstash-tester/` 目录不存在。

### R-10: E2E 脚本标准化输出格式

- context: 当前 E2E 脚本（如 `test_versioned_upgrade.sh`）使用带 ANSI 颜色码的自由文本输出 `PASS` / `FAIL`，worker 需要正则匹配来解析进度，容易因颜色码、格式变化或用户自定义输出而误判。定义一个结构化的标记行格式，使 worker 能可靠地解析进度，同时保持 bash 脚本的编写简便性。
- must:
  - E2E 脚本在 stdout 中输出以 `##e2e##` 为前缀的标记行，worker 只解析这些标记行，忽略其他输出。
  - 标记行格式（每行一条，纯文本，无 ANSI 颜色码）：
    - `##e2e## suite <suite_name> <total_count>` — 测试套件开始，声明套件名和预期用例总数（total 未知时填 0）。
    - `##e2e## case <case_name>` — 开始执行一个用例。
    - `##e2e## pass <case_name>` — 用例通过。
    - `##e2e## fail <case_name>` — 用例失败。
    - `##e2e## summary <passed> <failed> <total>` — 测试结束，最终统计。
  - E2E 脚本可以在标记行之外自由输出任何内容（人类可读的日志、颜色、分隔线等），worker 不解析这些行。
  - worker 根据标记行实时更新 `progress` 字段：`suite` 行设置 `total`，`case` 行设置 `current_test`，`pass`/`fail` 行递增 `passed`/`failed` 计数，`summary` 行作为最终确认值。
  - 提供一个 bash helper 文件 `tests/e2e/e2e-helpers.sh`，脚本通过 `source` 引入后可直接使用 `e2e_suite`、`e2e_case`、`e2e_pass`、`e2e_fail`、`e2e_summary` 函数，这些函数同时输出标记行和人类可读的彩色输出。
- must_not:
  - 标记行中不包含 ANSI 转义序列。
  - 缺少标记行的旧脚本不会导致 worker 报错或 job 失败——worker 退化为无进度信息（`progress.passed`/`failed`/`total` 为 0）。
- verification:
  - 使用 `e2e-helpers.sh` 的脚本输出同时包含人类可读格式和 `##e2e##` 标记行。
  - worker 解析标记行后，`progress` 中的 `passed`/`failed`/`total` 与脚本实际结果一致。
  - 不使用 `e2e-helpers.sh` 的旧脚本仍可正常执行，`progress` 计数为 0。

#### Scenario: helper 函数输出示例
- given: 脚本调用 `e2e_suite "versioned-upgrade" 11` 和 `e2e_pass "Fresh install"`
- when: worker 解析 stdout
- then: stdout 包含 `##e2e## suite versioned-upgrade 11` 和 `##e2e## pass Fresh install`，`progress.total` = 11，`progress.passed` >= 1

## Open Questions

（无）
