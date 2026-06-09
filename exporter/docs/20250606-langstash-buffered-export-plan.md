# 实现计划：langstash 缓冲投递 + 生命周期管理

## Context

根据 `exporter/docs/20250606-langstash-buffered-export-spec.md`，实现 langstash 本地缓冲服务和相关生命周期脚本。不修改 hook 的 transcript 解析 / Turn 组装 / Langfuse SDK 调用逻辑，但在 hook 的 main() 中加入 deliver_trace() 投递层实现三级投递链路。

## 实现分 5 个 Packet

---

### P1: langstash 核心服务

**目标**：实现 langstash FastAPI 服务，能接收 trace JSON、JSONL 持久化、sender 异步推送 Langfuse。

**新建文件**（全部在 `exporter/` 下）：

```
exporter/
├── pyproject.toml            # 包名 langstash, Python ≥3.11, fastapi/uvicorn/httpx
├── src/
│   ├── __init__.py
│   ├── __main__.py           # python -m langstash
│   ├── main.py               # CLI 入口: --server-only / 默认带 menubar
│   ├── config.py             # 加载 config/config.toml (tomllib)
│   ├── server.py             # FastAPI app: POST /ingest, GET /stats, GET /health, GET /
│   ├── ingestor.py           # 接收 trace JSON → 分配 seq_id → append JSONL (fcntl.flock)
│   ├── sender.py             # 后台线程: 按 seq_id 读取 pending → POST Langfuse ingestion API
│   ├── state.py              # state.json 读写 (next_seq_id, commit_id, files, last_error)
│   ├── stats.py              # 内存计数器 + 持久化 stats
│   ├── cleaner.py            # 存储清理 (max_size_gb + retention_days)
│   └── updater.py            # 更新检测 (GitHub releases API, .update-check)
```

**关键实现点**：

- `config.py`：用 `tomllib`（Python 3.11+）读取 `~/.agent-exporter-to-langfuse/config/config.toml`，合并默认值
- `ingestor.py`：
  - 分配 int64 seq_id（进程内计数器，从 state.json 恢复）
  - 注入 `_seq_id` + `_received_at`
  - `fcntl.flock` + append 写入 `data/pending/{YYYY-MM-DD}.jsonl`
  - 校验必填字段：schema_version, source, session_id, trace.name, trace.start_time, trace.end_time, generations(≥1)
  - body 上限 10MB
- `sender.py`：
  - 后台线程，间隔 `interval_seconds`
  - 从 commit_id+1 定位文件 → 逐行扫描 → 跳过 ≤ commit_id → 收集 batch_size 条
  - POST `{base_url}/api/public/ingestion` (Basic Auth)
  - 将 Trace Schema v2 转换为 Langfuse ingestion batch 格式 (trace-create + generation-create + span-create)
  - 退避策略：5s → 10s → ... → 300s，成功重置
  - at-least-once 保证
- `state.py`：
  - atomic write（tmp + rename）
  - 启动校验 next_seq_id ≥ commit_id
  - last_error 维护
- `cleaner.py`：
  - 启动 + 每小时定时
  - retention_days 超龄清理
  - max_size_gb 超限清理（优先级：committed pending → failed → uncommitted pending）
- `server.py`：
  - `POST /ingest` → ingestor
  - `POST /ingest/batch` → 循环调用 ingestor（≤100）
  - `GET /stats` → stats + state.last_error + updater 状态
  - `GET /health` → {status, version, langfuse_reachable}
  - `POST /update` → 触发 upgrade.sh
  - `GET /` → 内嵌 HTML 单页（Web UI）

---

### P2: Web UI + Menubar

**目标**：实现 Web UI 单页和 macOS menubar。

**Web UI**（`exporter/src/webui.py` 或内嵌在 `server.py`）：
- 单个 HTML 字符串，内嵌 CSS + vanilla JS
- 轮询 `/stats`（10s），动态更新 4 个指标卡片 + token 统计 + 投递状态 + 存储进度条
- 深色主题，响应式
- Favicon 内嵌 SVG（复用 menubar-icon 设计）

**Menubar**（`exporter/src/menubar.py`）：
- 仅 macOS，`rumps` 可选依赖
- `main.py` 判断平台和 `--server-only` 参数决定是否启动
- 4 种状态 PNG 图标（`exporter/assets/menubar-icon*.png`）
- 轮询 `/stats`（10s），更新图标 + 菜单项
- "Open Web UI" 菜单项 → `webbrowser.open()`

---

### P3: 生命周期脚本

**目标**：实现 VERSION、upgrade.sh、install-remote.sh，改造 install.sh / uninstall.sh。

**新建文件**（项目根目录）：
- `VERSION` — 内容 `0.2.0`
- `upgrade.sh` — git fetch tags → GitHub API 获取 latest → checkout → install.sh --upgrade
- `install-remote.sh` — curl|bash 引导脚本

**修改文件**：
- `install.sh`：
  - 顶部打印版本（读 VERSION）
  - 新增自动 Relocate 逻辑（非标准路径 → clone → exec）
  - 新增 `--upgrade` 参数处理（跳过凭据收集，从 config/*.env 推断 agent）
  - Step 末尾新增：安装 langstash（`cd exporter && uv sync`）、写 config.toml、安装 launchd/systemd service
  - env 文件新增 `LANGSTASH_ENABLED=true` 和 `LANGSTASH_URL`
- `uninstall.sh`：
  - 新增：stop langstash 服务 → 移除 plist/systemd service → 清理 data/ logs/
- `.gitignore`：
  - 新增 `config/`、`data/`、`logs/`、`.update-check`

**新建**（`lib/`）：
- `lib/install-langstash.sh` — langstash 安装子脚本（uv sync + config.toml + launchd/systemd）
- `lib/uninstall-langstash.sh` — langstash 卸载子脚本

---

### P4: Hook 投递层

**目标**：在各 hook 的 main() 中加入 deliver_trace() 封装，实现三级投递链路。不动 transcript 解析 / Turn 组装 / emit_turn 内部逻辑。

**新建文件**：
- `hooks/lib/deliver.py` — Python hooks 共享的投递层：
  ```python
  def deliver_trace(trace_json: dict) -> bool:
      """langstash → 直推 Langfuse → 写 failed 日志"""
  ```
  - 读 `LANGSTASH_ENABLED` / `LANGSTASH_URL` / `LANGSTASH_TIMEOUT`
  - 优先 POST langstash /ingest
  - fallback: 调用传入的 `direct_push_fn`（保持现有 emit_turn 逻辑）
  - 最终 fallback: `append_failed_trace()`（fcntl.flock 写 data/failed/）
  - 所有异常 catch + exit 0

- `hooks/lib/schema.py` — 构建 Trace Schema v2 JSON 的辅助函数：
  ```python
  def build_trace_json(source, session_id, user_id, tags, turn_num, turn, ...) -> dict:
      """从 Turn 数据构建 Schema v2 JSON"""
  ```

**修改文件**（最小改动，在 main() 末尾加投递逻辑）：
- `hooks/claude-code/hooks/langfuse_hook.py`：main() 中 emit_turn 调用外层包装 deliver_trace
- `hooks/qoder/hooks/langfuse_hook.py`：同上
- `hooks/qoderwork/hooks/langfuse_hook.py`：同上
- `hooks/opencode/hooks/langfuse-exporter.mjs`：JS 版 deliver（fetch langstash → fallback 现有逻辑 → fallback writeFileSync）

---

### P5: 各 Agent install.sh 更新

**目标**：各 agent 的 env 文件新增 `LANGSTASH_ENABLED` 和 `LANGSTASH_URL`，LaunchAgent plist 新增 `LANGSTASH_*` 环境变量注入。

**修改文件**（模式相同，4 个 agent）：
- `hooks/claude-code/install.sh` — env 文件写入 + plist grep 模式
- `hooks/qoder/install.sh` — 同上
- `hooks/qoderwork/install.sh` — 同上
- `hooks/opencode/install.sh` — 同上
- 对应的 `uninstall.sh` 不需要改（卸载删除整个 env 文件）

---

## 实现顺序与依赖

```
P1 (langstash 核心) ← 无依赖，独立开发
P2 (Web UI + Menubar) ← 依赖 P1 的 server.py / stats
P3 (生命周期脚本) ← 依赖 P1 的 exporter/pyproject.toml
P4 (Hook 投递层) ← 依赖 P1 的 /ingest API 可用
P5 (Agent install 更新) ← 依赖 P3 的 install.sh 改造
```

推荐：P1 → P3 → P5 → P4 → P2

---

## 验证方式

### P1 验证
```bash
cd exporter && uv sync && uv run langstash --server-only
# 另一终端：
curl -X POST http://127.0.0.1:5288/ingest -H 'Content-Type: application/json' \
  -d '{"schema_version":"2","id":"test","source":"claude-code","session_id":"s1","trace":{"name":"Test","start_time":"2025-01-01T00:00:00Z","end_time":"2025-01-01T00:01:00Z","input":{"role":"user","content":"hi"},"output":{"role":"assistant","content":"hello"}},"generations":[{"name":"Gen 1","model":"test","start_time":"2025-01-01T00:00:00Z","end_time":"2025-01-01T00:01:00Z","output":{"role":"assistant","content":"hello"}}]}'
# 期望：HTTP 202, data/pending/{today}.jsonl 有一行
curl http://127.0.0.1:5288/stats
# 期望：traces_today=1, pending_count=1
curl http://127.0.0.1:5288/health
```

### P2 验证
- 浏览器打开 http://127.0.0.1:5288 → 看到 Web UI 页面
- macOS: menubar 出现图标

### P3 验证
```bash
# 从非标准路径测试 relocate
cd /tmp && git clone ~/.agent-exporter-to-langfuse test-repo
bash test-repo/install.sh  # 期望：自动 relocate 回标准路径
# 测试 upgrade
bash ~/.agent-exporter-to-langfuse/upgrade.sh
```

### P4 验证
- 启动 langstash → 触发 hook → 检查 data/pending/ 有 JSONL 行
- 停止 langstash → 触发 hook → 检查直推 Langfuse 成功或 data/failed/ 有 JSONL 行

### P5 验证
```bash
bash install.sh -y  # 检查 env 文件包含 LANGSTASH_ENABLED=true
grep LANGSTASH config/claude-code.env
```
