# Test Service 设计方案

> 日期：2026-06-19

## 1. 背景与目标

本项目当前有一套自动编码系统，多个 Coding Agent 并行开发不同需求。每个 Agent 在独立的 feature 分支上工作，编写代码和对应的 E2E 测试脚本并 commit 到仓库。

**需求：** 提供一个常驻测试服务（test-service），运行在独立的服务器/VM 上，当前聚焦 E2E 测试，后期可扩展其他测试类型。具备以下能力：

1. **发起** — 调用方通过 HTTP API 触发 E2E 测试
2. **进度** — 调用方可实时查询任务进度（当前阶段、已通过/失败用例数）
3. **状态与结果** — 调用方可查询任务最终状态和详细结果
4. **回调** — 任务完成时通过 webhook 主动通知（可选，调用方也可轮询）
5. **队列** — 多个请求串行执行（`max_concurrent = 1`），排队等待
6. **发布前验证** — 正式版本发布前可在 main 分支上运行完整 E2E

### 调用方说明

test-service 是一个纯 HTTP 服务，**不关心调用方是谁**。调用方可以是：

- **Orchestrator**（推荐）— auto-dev workflow 中的 `test` 节点由 orchestrator 直接调用 test-service API，而不是委托给 Coding Agent
- **CI/CD** — GitHub Actions 或其他 CI 系统在发布前触发全量 E2E
- **人工** — 开发者通过 curl 或 WebUI 手动触发
- **Coding Agent**（不推荐）— Agent 可以调用，但 Agent 的职责是写代码，HTTP 轮询不是其强项

### 与 auto-dev workflow 的集成模型

本服务设计为 orchestrator-agent 的 `workflow-auto-dev` 提供 E2E 测试能力。在 auto-dev DAG 中：

```
solution → spec → plan → coding → test → aone-submit-pr → aone-pr-review
```

`test` 节点的执行方式从「Coding Agent 本地跑测试」变为「orchestrator 调用 test-service」：

```
当前实现：
  coding (Agent 在 worktree 中写代码)
    → test (同一个 Agent 在同一个 worktree 中跑测试)

目标实现：
  coding (Agent 在 worktree 中写代码，完成后 push 分支)
    → test (orchestrator 调 test-service API，轮询结果)
```

**为什么 orchestrator 直接调用，而不是让 Coding Agent 调用：**

1. **职责分离** — Coding Agent 的能力是写代码和跑本地命令，不擅长做 HTTP 轮询和超时管理
2. **可靠性** — orchestrator 有成熟的重试、超时、状态持久化机制；Agent 的 HTTP 调用是临时的，进程退出即丢失
3. **可观测性** — orchestrator 管理 DAG 状态，test-service 的 job 状态可以直接映射到 DAG 节点状态
4. **解耦** — test-service 不依赖任何 Agent 平台的 webhook 能力，orchestrator 通过轮询即可

**orchestrator 执行 test 节点的流程：**

```
Orchestrator                    Git Repo                     test-service (VM)
    │                              │                              │
    │  (coding 节点完成，输出 BRANCH + PUSHED=true)                │
    │                              │                              │
    │── POST /e2e/jobs ───────────────────────────────────────────►│
    │   { branch: BRANCH,          │                              │
    │     mode: "branch" }         │                              │
    │◄─ 202 { job_id } ───────────────────────────────────────────│
    │                              │                              │
    │── GET /e2e/jobs/{id} ───────────────────────────────────────►│  (轮询)
    │◄─ { status: "running", progress: {...} } ───────────────────│
    │   ...                        │                              │
    │── GET /e2e/jobs/{id} ───────────────────────────────────────►│
    │◄─ { status: "success", summary: {...} } ────────────────────│
    │                              │                              │
    │  (status=success → 推进到 aone-submit-pr)                    │
    │  (status=failed  → 带 test report 回退到 coding)             │
```

**coding 节点的前置要求：** coding 完成后必须 `git push origin <branch>`，使 test-service 能通过 `git fetch` 获取最新代码。coding 的 output contract 需增加 `PUSHED=true` 确认字段。

**test 失败回退：** 当 test-service 返回 `status: failed` 时，orchestrator 将 test report（`output_tail` + `summary.failed_tests`）作为反馈注入 coding 节点的 `hitl_feedback`，让 Agent 修复代码后重新 push 和测试。

---

## 2. 核心决策：分支策略

### 2.1 推荐方案：Feature Branch E2E + Pre-release Integration E2E

| 阶段 | E2E 运行位置 | 目的 | 触发方式 |
|------|-------------|------|----------|
| **开发中** | Feature Branch | 快速反馈，验证 Agent 的代码+测试是否通过 | Agent 主动调 API |
| **合并前**（可选） | 临时 merge worktree（main + branch） | 检测与主干的集成冲突 | Agent 请求 integration check |
| **发布前** | main 分支 | 全量回归，确保主干稳定 | 手动或 CI 触发 |

### 2.2 为什么 E2E 应该在分支上做，而不是合并到总开发分支后做

| 考量 | 分支上做 E2E | 合并后做 E2E |
|------|-------------|-------------|
| **反馈速度** | ✅ 立即，Agent 不需要等合并 | ❌ 需要先合并成功才能测试 |
| **互相阻塞** | ✅ 无，各分支完全隔离 | ❌ 一个 Agent 合并失败/引入 bug 阻塞所有人 |
| **冲突处理** | ✅ 不存在（每分支独立） | ❌ 多 Agent 同时合并产生冲突 |
| **回滚成本** | ✅ 无需回滚（代码还没合并） | ❌ 需要 revert 或 force push |
| **集成验证** | ⚠️ 不验证跨分支集成 | ✅ 自然验证集成 |
| **适用场景** | 日常开发，快速迭代 | 发布前关门验证 |

**结论：** 日常开发阶段 E2E 在 feature branch 上运行；集成验证通过可选的 "integration check" 模式在合并前临时验证；发布前在 main 上运行全量。

### 2.3 代码冲突问题

由于每个 Agent 独占一个分支，开发阶段不存在代码冲突。冲突只在以下时机出现：

1. **合并到 main 时** — 这是正常的 git 合并流程，由 Agent 或人工解决
2. **Integration check 时** — test-service在临时 worktree 中尝试 merge，如果冲突则报告给 Agent（不实际修改任何分支）

test-service对冲突的处理策略：
- 如果 `git merge` 失败，job 状态标记为 `conflict`，返回冲突文件列表
- Agent 收到冲突通知后自行决定是否 rebase 并重新提交

---

## 3. 服务架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Test Service (VM)                      │
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌──────────────────┐    │
│  │  HTTP API   │───►│  Job Queue  │───►│  Worker            │    │
│  │  (FastAPI)  │    │  (FIFO)     │    │  (serial, N=1)    │    │
│  └─────────────┘    └─────────────┘    └────────┬─────────┘    │
│         │                                        │              │
│         │           ┌─────────────┐              │              │
│         └──────────►│ Result Store│◄─────────────┘              │
│                     │ (SQLite)    │                             │
│                     └──────┬──────┘                             │
│                            │                                    │
│                     ┌──────▼──────┐                             │
│                     │  Webhook    │──► Coding Agent callback    │
│                     │  Notifier   │                             │
│                     └─────────────┘                             │
│                                                                 │
│  ┌───────────────────────────────────────────────────────┐      │
│  │  Git Worktree Pool                                    │      │
│  │  ~/.langstash-tester/worktrees/                       │      │
│  │    ├── job-abc123/  (branch: feat/cursor-hook)        │      │
│  │    ├── job-def456/  (branch: feat/masking)            │      │
│  │    └── job-ghi789/  (branch: main, integration)       │      │
│  └───────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

### 3.1 组件职责

| 组件 | 职责 |
|------|------|
| **HTTP API** | 接收 E2E 请求、查询状态、取消任务 |
| **Job Queue** | FIFO 队列，管理待执行任务，控制并发上限 |
| **Worker** | 串行执行 E2E 任务（同一时刻只运行一个 job），使用临时 worktree |
| **Result Store** | 持久化任务状态、日志输出、测试结果 |
| **Webhook Notifier** | 任务完成时 POST 结果到 Agent 注册的回调 URL |
| **Git Worktree Pool** | 管理临时 worktree 的创建和清理 |

### 3.2 Job 生命周期

```
pending → running → [success | failed | conflict | cancelled | timeout]
```

- `pending` — 已入队，等待 worker 空闲
- `running` — worker 已开始执行（checkout + 运行测试）
- `success` — 所有测试通过
- `failed` — 存在测试失败
- `conflict` — integration check 模式下 merge 冲突
- `cancelled` — 被 Agent 主动取消
- `timeout` — 执行超时（默认 30 分钟）

---

## 4. HTTP API 设计

### 4.1 创建 E2E 任务

```http
POST /e2e/jobs
Content-Type: application/json

{
  "branch": "feat/cursor-hook",
  "commit": "a1b2c3d",                    // 可选，默认使用 branch HEAD
  "mode": "branch",                        // "branch" | "integration"
  "test_command": "bash tests/e2e/test_versioned_upgrade.sh",  // 可选，默认运行 tests/e2e/ 下所有测试
  "timeout_seconds": 1800,                 // 可选，默认 1800
  "callback_url": "http://agent-host:8080/webhook/e2e",  // 可选
  "metadata": {                            // 可选，透传给回调
    "agent_id": "agent-cursor-001",
    "task_id": "TASK-42"
  }
}
```

**Response (202 Accepted):**

```json
{
  "job_id": "e2e-20260619-abc123",
  "status": "pending",
  "created_at": "2026-06-19T15:30:00Z",
  "position": 0
}
```

**mode 说明：**
- `branch` — 直接 checkout 指定分支/commit 运行测试
- `integration` — 先在 worktree 中尝试将 branch merge 到 main，成功后运行测试

### 4.2 查询任务状态与结果

```http
GET /e2e/jobs/{job_id}
```

**Response（运行中）:**

```json
{
  "job_id": "e2e-20260619-abc123",
  "status": "running",
  "branch": "feat/cursor-hook",
  "commit": "a1b2c3d",
  "mode": "branch",
  "created_at": "2026-06-19T15:30:00Z",
  "started_at": "2026-06-19T15:30:05Z",
  "progress": {
    "phase": "testing",
    "passed": 5,
    "failed": 0,
    "total": 11,
    "current_test": "E2E-6: Legacy git layout migration",
    "elapsed_seconds": 68
  },
  "metadata": { "agent_id": "agent-cursor-001" }
}
```

**Response（已完成）:**

```json
{
  "job_id": "e2e-20260619-abc123",
  "status": "success",
  "branch": "feat/cursor-hook",
  "commit": "a1b2c3d",
  "mode": "branch",
  "created_at": "2026-06-19T15:30:00Z",
  "started_at": "2026-06-19T15:30:05Z",
  "finished_at": "2026-06-19T15:32:18Z",
  "duration_seconds": 133,
  "exit_code": 0,
  "progress": {
    "phase": "completed",
    "passed": 11,
    "failed": 0,
    "total": 11
  },
  "summary": {
    "total": 11,
    "passed": 11,
    "failed": 0,
    "failed_tests": []
  },
  "output_tail": "All test suites passed.\n",
  "metadata": { "agent_id": "agent-cursor-001" }
}
```

**progress.phase 取值：**
- `pending` — 排队等待中
- `preparing` — git fetch + worktree 创建中
- `merging` — integration 模式下正在合并
- `testing` — E2E 测试执行中
- `completed` — 已完成（查看 status 获取结果）

### 4.3 列出任务

```http
GET /e2e/jobs?status=running&branch=feat/cursor-hook&limit=20
```

### 4.4 取消任务

```http
POST /e2e/jobs/{job_id}/cancel
```

### 4.5 获取完整日志

```http
GET /e2e/jobs/{job_id}/logs
```

返回测试运行的完整 stdout/stderr 输出。

---

## 5. 执行与隔离策略

### 5.1 Git Worktree 隔离

每个 E2E job 在独立的 git worktree 中执行：

```bash
# Worker 执行流程
git fetch origin
git worktree add ~/.langstash-tester/worktrees/$JOB_ID origin/$BRANCH
cd ~/.langstash-tester/worktrees/$JOB_ID

# 如果是 integration 模式
git merge origin/main --no-edit  # 失败则报 conflict

# 运行 E2E 测试（不运行 scripts/run-tests.sh，那是单测）
bash tests/e2e/test_versioned_upgrade.sh

# 清理
cd /
git worktree remove ~/.langstash-tester/worktrees/$JOB_ID --force
```

### 5.2 串行队列

- **同一时刻只运行一个 job**（`max_concurrent = 1`），其余排队等待
- **队列策略** — FIFO，先到先服务
- **同分支去重** — 同一分支如果有 pending 的 job，新请求可选择：
  - `replace` — 取消旧 job，用新 job 替代（默认）
  - `queue` — 新旧都保留，排队执行
  - `reject` — 拒绝新请求

### 5.3 资源隔离

- 每个 worktree 使用独立的 Python venv（`uv sync` 在 worktree 内执行）
- 超时机制防止 zombie 进程

---

## 6. Webhook 回调

任务完成时，如果创建时指定了 `callback_url`，服务会 POST 结果：

```http
POST {callback_url}
Content-Type: application/json

{
  "event": "e2e.completed",
  "job_id": "e2e-20260619-abc123",
  "status": "success",
  "branch": "feat/cursor-hook",
  "commit": "a1b2c3d",
  "duration_seconds": 133,
  "exit_code": 0,
  "summary": { "total": 11, "passed": 11, "failed": 0 },
  "output_tail": "All test suites passed.\n",
  "metadata": { "agent_id": "agent-cursor-001", "task_id": "TASK-42" }
}
```

- 回调失败重试 3 次（间隔 5s/15s/30s）
- 回调超时 10 秒

---

## 7. 配置

服务通过环境变量或配置文件启动：

```toml
# ~/.langstash-tester/config/config.toml

[server]
host = "0.0.0.0"
port = 5289

[git]
repo_url = "git@github.com:aliyun/agent-exporter-to-langfuse.git"
# local_repo = "~/.langstash-tester/repo"           # 默认值
# worktree_dir = "~/.langstash-tester/worktrees"     # 默认值

[storage]
# db_path = "~/.langstash-tester/data/langstash-tester.db"  # 默认值
# log_dir = "~/.langstash-tester/logs"                       # 默认值

[e2e]
default_test_dir = "tests/e2e"              # 默认运行此目录下所有 E2E 脚本
max_concurrent = 1
default_timeout_seconds = 1800
result_retention_days = 30
same_branch_policy = "replace"              # replace | queue | reject

[webhook]
retry_count = 3
retry_delays = [5, 15, 30]
timeout_seconds = 10
```

运行时目录结构：

```
~/.langstash-tester/
├── config/config.toml        ← 配置文件
├── repo/                     ← bare clone（git clone --bare）
├── worktrees/                ← 临时 worktree（job 执行时创建，完成后删除）
├── data/langstash-tester.db  ← SQLite 持久化
└── logs/                     ← job 日志（<job_id>.log）
```

---

## 8. 部署

### 8.1 VM 要求

- Python 3.11+、uv、Node.js 18+、pnpm
- Git 2.x（支持 worktree）
- 磁盘 ≥20GB（worktree + 日志）
- 网络：可访问 Git 仓库，可被 Coding Agent 访问

### 8.2 启动方式

```bash
# 初始化 bare repo
git clone --bare git@github.com:aliyun/agent-exporter-to-langfuse.git ~/.langstash-tester/repo

# 启动服务（当前仅支持 E2E，后期可扩展）
langstash-tester run --config ~/.langstash-tester/config/config.toml
```

用 systemd/launchd 管理为常驻进程，或通过 `langstash-tester install` + `langstash-tester start` 自动注册。

---

## 9. 典型工作流

### 9.1 auto-dev workflow 中的 E2E 测试

```
Coding Agent (VM-A)        Orchestrator               Git Repo            test-service (VM-B)
     │                          │                        │                       │
     │── write code ────────────│                        │                       │
     │── git push ──────────────────────────────────────►│                       │
     │── output: BRANCH,        │                        │                       │
     │   PUSHED=true ──────────►│                        │                       │
     │                          │                        │                       │
     │                          │── POST /e2e/jobs ─────────────────────────────►│
     │                          │   { branch: BRANCH }   │                       │
     │                          │                        │◄── git fetch ─────────│
     │                          │◄─ 202 { job_id } ─────────────────────────────│
     │                          │                        │                       │── run E2E
     │                          │── GET /e2e/jobs/{id} ─────────────────────────►│
     │                          │◄─ { running, progress }───────────────────────│
     │                          │   ...轮询...            │                       │
     │                          │◄─ { success } ────────────────────────────────│
     │                          │                        │                       │
     │                          │── 推进到 aone-submit-pr │                       │
```

### 9.2 发布前全量验证

```bash
# 人工或 CI 触发，运行 main 分支上所有 E2E
curl -X POST http://test-vm:5289/e2e/jobs \
  -H "Content-Type: application/json" \
  -d '{"branch": "main", "mode": "branch"}'
```

### 9.3 合并前集成检查

```bash
# Agent 合并前检查是否和 main 有冲突
curl -X POST http://test-vm:5289/e2e/jobs \
  -H "Content-Type: application/json" \
  -d '{"branch": "feat/xxx", "mode": "integration"}'
```

---

## 10. 与现有架构的关系

| 组件 | 是否变动 | 说明 |
|------|---------|------|
| `exporter/` (langstash) | 不变 | test-service 是独立进程，不集成到 langstash |
| `tests/e2e/` | 不变 | 现有及新增的 E2E 脚本作为测试用例被执行 |
| `scripts/run-tests.sh` | 不变 | 单测脚本，test-service 不调用 |
| `deploy/` | 不变 | test-service 有自己的部署方式 |
| 新增 `test-service/` | **新增** | test-service 的代码目录 |

test-service 作为**独立的辅助工具**存在，不影响本项目的核心功能和部署流程。后期可在 test-service 中扩展其他测试类型（如 Contract 测试、性能测试等）。

---

## 11. 后续扩展

- **更多测试类型** — 在 `/contract/jobs`、`/perf/jobs` 等路径下扩展 Contract 测试、性能测试
- **测试报告** — 生成 HTML/Markdown 格式的测试报告，推送到 PR comment
- **历史趋势** — 追踪测试通过率、耗时趋势
- **资源监控** — 监控 VM 的 CPU/内存/磁盘使用
- **多仓库支持** — 支持多个 Git 仓库的测试
- **容器化** — 每个 job 在容器中执行，更强隔离
