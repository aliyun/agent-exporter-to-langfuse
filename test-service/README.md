# langstash-tester

E2E 测试常驻服务，为自动编码系统（Coding Agent / Orchestrator）提供 HTTP API 触发、查询和管理 E2E 测试任务。

## 架构

```
Orchestrator / CI / curl
        │
        ▼  HTTP API (:5289)
┌──────────────────────────┐
│    langstash-tester       │
│                          │
│  Job Queue (FIFO, N=1)   │
│  Worker → git worktree   │
│  SQLite → 结果持久化      │
│  Webhook → 回调通知       │
└──────────────────────────┘
        │
        ▼  git fetch
   Git Repository
```

- 串行执行（同一时刻只运行一个 job）
- 每个 job 在独立的 git worktree 中执行，完成后自动清理
- 实时进度追踪（通过 `##e2e##` 标记行解析）
- 支持同分支去重（replace / queue / reject 策略）

## 安装

```bash
cd test-service
uv sync

# 一键安装（创建目录、生成配置、clone bare repo、注册服务、生成 CLI wrapper）
uv run langstash-tester install
```

## 启动

`langstash-tester install` 会在 `~/.local/bin/` 创建 wrapper 脚本，之后所有命令直接使用 `langstash-tester`，无需 `uv run` 前缀。

> 确保 `~/.local/bin` 在 `$PATH` 中。

### 前台启动（开发/调试）

```bash
langstash-tester run

# 指定配置文件
langstash-tester run --config ~/.langstash-tester/config/config.toml
```

### 后台启动（生产）

```bash
# 通过系统服务管理（install 已注册 systemd/launchd 服务）
langstash-tester start
langstash-tester stop
langstash-tester restart
```

### 查看状态

```bash
langstash-tester status
# langstash-tester v0.1.0-beta.2
# Status: ok
```

## CLI 命令

> 首次安装使用 `uv run langstash-tester install`，之后所有命令直接用 `langstash-tester`。

| 命令 | 说明 |
|------|------|
| `langstash-tester run` | 前台启动服务 |
| `langstash-tester start` | 启动后台服务 |
| `langstash-tester stop` | 停止后台服务 |
| `langstash-tester restart` | 重启后台服务 |
| `langstash-tester status` | 显示服务状态和版本 |
| `langstash-tester install` | 安装（创建目录、注册服务、生成 CLI wrapper） |
| `langstash-tester uninstall` | 卸载（保留 config/data/logs） |
| `langstash-tester uninstall --purge` | 卸载并删除所有数据 |

## API

### 创建 E2E 任务

```bash
curl -X POST http://127.0.0.1:5289/e2e/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "branch": "feat/my-feature",
    "mode": "branch",
    "callback_url": "http://your-host/webhook"
  }'
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `branch` | 是 | Git 分支名 |
| `commit` | 否 | 指定 commit（默认 branch HEAD） |
| `mode` | 否 | `branch`（默认）或 `integration`（临时 merge main） |
| `test_command` | 否 | 自定义测试命令（默认运行 `tests/e2e/` 下所有脚本） |
| `timeout_seconds` | 否 | 超时秒数（默认 1800） |
| `callback_url` | 否 | 完成后 POST 回调地址 |
| `metadata` | 否 | 透传到回调和查询结果的自定义数据 |

### 查询任务

```bash
# 查询单个任务（含实时进度）
curl http://127.0.0.1:5289/e2e/jobs/{job_id}

# 列出任务
curl "http://127.0.0.1:5289/e2e/jobs?status=running&limit=10"

# 获取完整日志
curl http://127.0.0.1:5289/e2e/jobs/{job_id}/logs
```

### 取消任务

```bash
curl -X POST http://127.0.0.1:5289/e2e/jobs/{job_id}/cancel
```

### 健康检查

```bash
curl http://127.0.0.1:5289/health
```

## 目录结构

```
~/.langstash-tester/
├── config/config.toml        ← 配置文件
├── repo/                     ← bare clone
├── worktrees/                ← 临时 worktree（自动管理）
├── data/langstash-tester.db  ← SQLite 数据库
└── logs/                     ← job 日志（<job_id>.log）
```

## E2E 脚本标记行

E2E 脚本可通过 `##e2e##` 标记行向 langstash-tester 报告进度：

```bash
source tests/e2e/e2e-helpers.sh

e2e_suite "my-tests" 3
e2e_case "test one"
e2e_pass "test one"
e2e_case "test two"
e2e_fail "test two"
e2e_case "test three"
e2e_pass "test three"
e2e_summary
```

不使用标记行的脚本仍可正常执行，进度信息为空。

## 配置参考

```toml
[server]
host = "0.0.0.0"
port = 5289

[git]
repo_url = "git@github.com:user/repo.git"    # 必填
# local_repo = "~/.langstash-tester/repo"
# worktree_dir = "~/.langstash-tester/worktrees"

[storage]
# db_path = "~/.langstash-tester/data/langstash-tester.db"
# log_dir = "~/.langstash-tester/logs"

[e2e]
# default_test_dir = "tests/e2e"
# max_concurrent = 1
# default_timeout_seconds = 1800
# result_retention_days = 30
# same_branch_policy = "replace"    # replace | queue | reject

[webhook]
# retry_count = 3
# retry_delays = [5, 15, 30]
# timeout_seconds = 10
```

## 与 langstash 的关系

langstash-tester 与采集服务 langstash **完全独立**：

- 独立安装/卸载/启停
- 独立目录（`~/.langstash-tester/` vs `~/.agent-exporter-to-langfuse/`）
- `langstash uninstall` 不影响 langstash-tester，反之亦然

## 开发

```bash
cd test-service
uv sync --group dev
uv run pytest -q
```
