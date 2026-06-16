# 版本化升级架构设计

## 背景

当前 agent-exporter-to-langfuse 的安装和升级依赖 `git clone` + `git checkout`，存在以下问题：

- `install-remote.sh` 将整个 git repo clone 到 `~/.agent-exporter-to-langfuse/`
- `upgrade.sh` 用 `git fetch` + `git checkout <tag>` 切换版本
- 升级过程非原子性 — git checkout 中途失败可能留下混合状态
- 无回滚机制 — 新版本有问题只能手动 `git checkout` 回退
- 升级需要 git 命令可用
- 无 SHA-256 完整性校验
- langstash 和 hook 版本无法独立追踪，部分 hook 升级失败时状态不明

本文档设计版本化目录布局 + 原子更新 + 回滚 + 完整性校验的升级架构。

## 核心概念

系统包含两个独立升级的组件：

- **langstash**（exporter）：常驻服务，通过版本化目录 + pointer swap 升级
- **hooks**：写入 Agent 配置的采集脚本 + deliver 库，拷贝到 Agent 自身目录内（自包含）

两者版本独立追踪，升级顺序为 langstash 先于 hook（接收端先就绪）。

---

## 目标目录布局

```
~/.agent-exporter-to-langfuse/
├── current              ← langstash 版本 pointer（如 "0.3.0"）
├── previous             ← langstash 回滚 pointer（如 "0.2.0"）
├── hook-state.json      ← 各 agent hook 的实际版本和状态
├── versions/
│   ├── 0.2.0/           ← 完整版本包
│   │   ├── VERSION
│   │   ├── hooks/
│   │   ├── exporter/
│   │   ├── install.sh
│   │   └── ...
│   └── 0.3.0/
│       └── ...
├── config/              ← 配置目录（跨版本共享）
│   ├── config.toml
│   └── *.env
├── data/                ← 数据目录（跨版本共享）
└── logs/                ← 日志目录（跨版本共享）
```

---

## 版本追踪

### langstash 版本：pointer 文件

`current`/`previous` 是纯文本文件，内容为版本号，管理 langstash 的版本目录切换。

### hook 版本：hook-state.json

各 Agent hook 的实际版本和状态通过 `hook-state.json` 独立追踪。

**Hook 状态定义**：

| 状态 | 含义 | 触发条件 |
|------|------|---------|
| `undetected` | 未探测到 | Agent 未安装在系统上（检测路径不存在、命令不可用） |
| `not_installed` | 未安装 | Agent 已安装但 hook 尚未部署 |
| `installed` | 已安装 | hook 正常安装并注册到 Agent settings |
| `error` | 异常 | hook 安装/升级失败、被覆盖、运行时异常等 |

**`hook-state.json` 示例**：

```json
{
  "claude-code": { "version": "0.3.0", "status": "installed" },
  "qoder":       { "version": "0.2.0", "status": "error", "error": "uv sync failed: network timeout" },
  "codex":       { "status": "not_installed" },
  "opencode":    { "status": "undetected" }
}
```

**状态流转**：

```
                 检测到 Agent
  undetected ──────────────────► not_installed
                                      │
                              install 成功
                                      │
                                      ▼
               install/upgrade   installed
               失败或被覆盖      ▲       │
                    ┌───────────┘       │ 升级失败 / watchdog 检测到被覆盖
                    │                   ▼
                  error ◄──────────── error
                    │
                    │ retry 成功
                    ▼
                installed
```

### Hook 状态透出（API + WebUI）

langstash 启动时加载 `hook-state.json`，通过 `/stats` API 和 WebUI 完整透出每个 hook 的安装状态和异常信息。

**`/stats` API 返回**：
```json
{
  "hooks": {
    "claude-code": { "version": "0.3.0", "status": "installed" },
    "qoder":       { "version": "0.2.0", "status": "error", "error": "uv sync failed: network timeout" },
    "codex":       { "status": "not_installed" },
    "opencode":    { "status": "undetected" }
  },
  "hook_version_mismatch": true,
  "hook_mismatch_agents": ["qoder"]
}
```

**WebUI 展示**：

在 Dashboard 中增加 Hooks 状态区块，展示每个 Agent hook 的状态：

| 状态 | 版本号 | 状态标签 | 错误信息 | 操作按钮 |
|------|--------|---------|---------|---------|
| `installed` | v0.3.0 | 绿色 `Installed` | — | — |
| `error` | v0.2.0 ⚠️ | 红色 `Error` | `uv sync failed: network timeout` | `Retry` |
| `not_installed` | — | 灰色 `Not Installed` | — | `Install` |
| `undetected` | — | 灰色 `Not Detected` | — | — |

版本不一致时，在页面顶部显示醒目告警条：
- "⚠️ qoder hook 版本 (0.2.0) 与 langstash (0.3.0) 不一致，点击重试升级"

**Retry API**：
- `POST /upgrade/retry-hooks` — 重试所有 status 非 ok 的 hook 升级
- `POST /upgrade/retry-hooks?agent=qoder` — 重试指定 agent 的 hook 升级

**日志**：版本不一致时记录 WARNING 级别告警

### 各场景处理

| 场景 | 处理 |
|------|------|
| 全部成功 | 正常运行，无告警 |
| 部分 hook 失败 | 失败的 hook 保持旧版本 + WebUI 红色告警 + Retry 按钮，可通过 API 或 CLI `langstash upgrade --retry-hooks` 重试 |
| langstash 回滚 | `langstash rollback` swap pointer 后，逐个用 hook-state.json 中版本回装 hook |
| 手动重试 | `POST /upgrade/retry-hooks` 或 CLI，只对 status 非 ok 的 hook 重新执行 uninstall → install |

---

## 发布渠道：GitHub Releases

版本信息（包括 stable 和 pre-release 区分）完全由 GitHub Releases 管理，不维护额外的 manifest 文件。

### 打包脚本（`deploy/package.sh`）

本地/CI 均可调用：
- 从 git tag 构建 release tarball
- 排除 `.git/`、`.venv/`、`__pycache__/`、`node_modules/` 等
- 输出 `agent-exporter-to-langfuse-<version>.tar.gz`
- 计算 SHA-256 并输出 `SHA256SUMS` 校验文件

### CI 发布（`.github/workflows/release.yml`）

- 触发条件：push tag `v*`
- 步骤：调用 `deploy/package.sh` → 通过 `gh release create` 上传 Release assets
- tag 含 `-`（如 `v0.4.0-beta.1`）时标记为 pre-release（`gh release create --prerelease`）
- Release assets：
  - `agent-exporter-to-langfuse-<version>.tar.gz`（版本包）
  - `SHA256SUMS`（校验文件）

### GitHub Releases 版本查询

| 场景 | 方式 |
|------|------|
| 最新稳定版 tarball | `https://github.com/{owner}/{repo}/releases/latest/download/agent-exporter-to-langfuse.tar.gz`（GitHub 自动指向最新非 pre-release） |
| 指定版本 tarball | `https://github.com/{owner}/{repo}/releases/download/v{version}/agent-exporter-to-langfuse-{version}.tar.gz` |
| 查询所有版本（含 pre-release） | GitHub API: `GET https://api.github.com/repos/{owner}/{repo}/releases` |
| 查询最新稳定版信息 | GitHub API: `GET https://api.github.com/repos/{owner}/{repo}/releases/latest` |

### Pre-release 过滤

由 GitHub Releases 的 `prerelease` 字段原生管理：
- `include_prerelease = false`（默认）→ updater 只检查 `releases/latest`（GitHub 自动排除 pre-release）
- `include_prerelease = true` → updater 查询 `releases` 列表，取第一个（最新，含 pre-release）

---

## 安装器（`deploy/installer.sh`）

重构 `install-remote.sh` 为子命令模式：`install | upgrade | uninstall [--purge]`。不再需要 `git` 作为前置依赖（仅需 `curl` 或 `wget`）。支持 `--package-url` 覆盖下载源（兼容内网/离线场景：`--package-url file:///path/to/local.tar.gz`）。

### 首次安装流程

```
install:
  1. 从 GitHub Releases 下载 tarball
     - 默认：releases/latest/download/agent-exporter-to-langfuse.tar.gz（最新稳定版）
     - 指定版本：releases/download/v<ver>/agent-exporter-to-langfuse-<ver>.tar.gz
     - 自定义：--package-url file:///path/to/local.tar.gz
  2. 下载 SHA256SUMS，校验 tarball 完整性
  3. 解压到 versions/<ver>/
  4. 写 current pointer
  5. 运行版本内 install.sh（安装 hooks + uv sync + 注册服务）
```

### 升级流程

```
upgrade:
  1. 同 install 步骤 1-3
  2. 写 previous ← current，写 current ← 新版本（原子 rename）
  3. 重新安装 hooks + uv sync
  4. 重启 langstash 服务
  5. GC 旧版本（保留 current + previous）
```

---

## 升级器（`exporter/src/updater.py`）

langstash 内置的后台升级检查器，定期轮询 GitHub Releases。

### 检测新版本

```
include_prerelease = false（默认）:
  1. GET https://api.github.com/repos/aliyun/agent-exporter-to-langfuse/releases/latest
  2. 从响应中提取 tag_name（如 "v0.3.0"）
  3. 对比 tag version vs 本地 VERSION
  4. 如有新版本，记录到 .update-check 文件
  5. WebUI / /stats API 展示 "有新版本可用"

include_prerelease = true:
  1. GET https://api.github.com/repos/aliyun/agent-exporter-to-langfuse/releases
  2. 取列表中第一个（最新，含 pre-release）
  3. 后续同上
```

### 执行升级

用户在 WebUI 点击 Upgrade 或执行 `langstash upgrade` 时触发。

---

## Hook 自包含约束

### 容器场景

部分 Agent（如 QoderWork）在容器/VM 中运行，仅挂载 Agent 自身目录（如 `~/.qoderwork/`），不挂载 `~/.agent-exporter-to-langfuse/`。因此 hook 脚本 + 依赖 + 配置必须完整存在于 Agent 自身目录内，不能引用外部路径。

### 自包含要求

- 凭证通过 Agent 插件机制传递（如 `CLAUDE_PLUGIN_OPTION_*`），不依赖外部 env 文件
- `langstash_deliver` 库 copy 进 hook 目录（install.sh 已实现）
- `.venv/` 在 hook 目录内创建（install.sh 已实现）
- 外部 env source 仅为 fallback（容器内失败时静默跳过）

---

## Hook 升级策略

### uninstall → install + 失败回装

```
升级单个 Agent hook 的流程:

  1. 读取 hook-state.json，获取该 agent 当前实际版本（如 "0.2.0"）
  2. 备份当前 hook 状态（记录 settings.json 中的 hook 条目）
  3. 调用该 agent 实际版本的 uninstall.sh 卸载旧 hook
  4. 调用 current（新版本）的 install.sh 安装新 hook
     ├─ 成功 → 更新 hook-state.json（version=新版本, status=ok）
     └─ 失败 → 调用该 agent 实际版本的 install.sh 回装 → 更新 hook-state.json（status=rollback, error=...）
```

### 脚本版本规则

- **uninstall 调用 hook-state.json 中记录的版本的 uninstall.sh** — 该 hook 实际运行的版本知道自己装了什么、怎么卸
- **install 调用 current（新版本）的 install.sh** — 新版本知道新的安装方式
- **失败回装调用 hook-state.json 中记录的版本的 install.sh** — 恢复到该 hook 之前的实际版本

注意：每个 agent hook 的实际版本可能不同（例如 claude-code 在 0.3.0，而 qoder 因上次升级失败仍在 0.2.0），因此不能统一使用 langstash 的 `previous` pointer，必须从 `hook-state.json` 逐个读取。

### 注意事项

- uninstall 和 install 之间存在短暂**空窗期**（秒级），此间 Agent 事件不会被采集
- install 失败时必须尝试回装旧版本，避免 hook 完全丢失

---

## 完整升级流程

langstash 先于 hook 升级（接收端先就绪），由外部脚本 `installer.sh` 驱动，以独立进程运行（`start_new_session=True` / `nohup`），不随 langstash 重启而终止：

```
langstash updater（内部线程）          installer.sh（独立进程）

  检测到新版本                               
  spawn installer.sh ──────────►  1. 下载新版 tarball → SHA-256 校验 → 解压
  返回 "started"                  2. 在新版本目录中预装 langstash 依赖（uv sync）
        │                         3. 原子 pointer swap（previous ← current，current ← 新版本）
        │                         4. 重启 langstash 服务
        ×（langstash 被重启）         │
                                   5. 轮询 /health 等待新 langstash 就绪（超时 → 标记失败）
  新 langstash 启动 ◄────────────  6. 确认 langstash 就绪
        │                         7. 逐个 Agent 执行 hook 升级：
        │                            a. 从 hook-state.json 读取该 agent 当前实际版本
        │                            b. 备份当前 hook 状态
        │                            c. 调用该 agent 实际版本的 uninstall.sh 卸载旧 hook
        │                            d. 调用 current（新版本）的 install.sh 安装新 hook
        │                               （install.sh 内部完成依赖安装 + 注册到 Agent settings）
        │                            e. 成功 → 更新 hook-state.json（version=新版本, status=ok）
        │                               失败 → 用实际版本的 install.sh 回装 + 更新 hook-state.json（status=rollback）
        │                         9. GC 旧版本目录
        │                        10. 退出
  正常运行
```

### 设计保证

- langstash 重启不中断升级流程 — installer.sh 作为独立进程存活
- 新 langstash 就绪后才升级 hook，确保新数据不被旧 langstash 拒绝
- 部分 hook 升级失败时，旧格式数据仍被新 langstash 接受（向后兼容）
- WebUI 触发升级时同理：`POST /upgrade` → spawn installer.sh → 返回 `{"status": "started"}`

---

## 回滚

`langstash rollback`：

1. swap current ↔ previous pointer
2. 重启 langstash 服务
3. 逐个用 previous（现在是 current）版本的 install.sh 回装 hook
4. 更新 hook-state.json

---

## 脚本目录管理

`deploy/` 只放统一安装入口和 CI 打包脚本。各 hook 和 langstash 的 install/uninstall 脚本保持在各自目录内，与组件代码同级管理。

### 目标结构

```
deploy/
├── installer.sh                    ← 远程安装入口（替代 install-remote.sh）
│                                      支持 install/upgrade/uninstall 子命令
├── package.sh                      ← CI 打包脚本（新增）
└── .github/workflows/release.yml   ← CI 发布（实际放在项目根 .github/ 下）

hooks/
├── claude-code/
│   ├── install.sh                  ← claude-code hook 安装/卸载（自包含）
│   ├── uninstall.sh
│   └── hooks/                      ← 运行时 hook 脚本
│       ├── langfuse_hook.py
│       └── ...
├── qoder/
│   ├── install.sh
│   ├── uninstall.sh
│   └── hooks/
├── qoderwork/
│   ├── install.sh
│   ├── uninstall.sh
│   └── hooks/
├── opencode/
│   ├── install.sh
│   ├── uninstall.sh
│   └── hooks/
└── codex/
    ├── install.sh
    ├── uninstall.sh
    └── src/

exporter/
├── install-langstash.sh            ← langstash 服务安装（保持原位）
├── uninstall-langstash.sh          ← langstash 服务卸载（保持原位）
└── src/

scripts/
└── run-tests.sh                    ← 开发工具（从根目录迁入）
```

### 职责划分

| 目录 | 职责 | 包含脚本 |
|------|------|---------|
| `deploy/` | 统一安装入口 + CI 打包 | `installer.sh`, `package.sh` |
| `hooks/<agent>/` | 各 agent hook 的安装/卸载/运行时脚本 | `install.sh`, `uninstall.sh`, `hooks/*` |
| `exporter/` | langstash 服务的安装/卸载 | `install-langstash.sh`, `uninstall-langstash.sh` |
| `scripts/` | 开发工具 | `run-tests.sh` |

### 向后兼容

根目录原有的 `install.sh`、`uninstall.sh`、`upgrade.sh` 保留为 thin wrapper，转发到 `deploy/installer.sh` 对应子命令：

```bash
#!/usr/bin/env bash
# upgrade.sh — backward-compat wrapper
exec "$(dirname "$0")/deploy/installer.sh" upgrade "$@"
```

### 升级流程中的路径引用

升级流程中调用各脚本时，基于版本目录定位：
- hook uninstall：`versions/<hook实际版本>/hooks/<agent>/uninstall.sh`
- hook install：`versions/<current>/hooks/<agent>/install.sh`
- langstash install：`versions/<current>/exporter/install-langstash.sh`

---

## 关键文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `deploy/installer.sh` | 新增（替代 `install-remote.sh`） | 远程安装入口（install/upgrade/uninstall） |
| `deploy/package.sh` | 新增 | CI 打包脚本 |
| `.github/workflows/release.yml` | 新增 | CI 自动发布 Release |
| `exporter/src/updater.py` | 重构 | GitHub API + tarball + pointer swap |
| `hooks/<agent>/install.sh` | 修改 | 适配版本化目录布局 |
| `hooks/<agent>/uninstall.sh` | 修改 | 适配版本化目录布局 |
| `exporter/install-langstash.sh` | 修改 | 适配版本化目录布局 |
| `install.sh` / `upgrade.sh` / `uninstall.sh` | 修改 | 改为 thin wrapper 转发到 `deploy/installer.sh` |

---

## 验证计划

- `deploy/package.sh` 生成合法 tarball + SHA256SUMS
- `deploy/installer.sh install` 完整走通首次安装
- `deploy/installer.sh upgrade` 从旧版本升级，验证 pointer swap + 服务重启
- `langstash rollback` 回退到上一版本，验证服务正常
- SHA-256 校验：篡改 tarball 后升级应失败
- GC：升级两次后，只保留 current + previous 两个版本目录
- 部分 hook 升级失败：验证 hook-state.json 记录正确 + WebUI 告警 + `--retry-hooks` 可重试
- 版本不一致告警：hook 与 langstash 版本不同时，`/stats` 返回 mismatch 信息
- 离线安装：`--package-url file:///path/to/local.tar.gz` 正常工作
