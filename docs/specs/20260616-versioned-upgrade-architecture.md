# 版本化升级架构

## Purpose

当前安装和升级依赖 `git clone` + `git checkout`，存在非原子更新、无回滚、无完整性校验、需要 git 前置依赖、hook 与 langstash 版本无法独立追踪等问题。本需求将安装/升级机制重构为版本化目录布局 + tarball 分发 + pointer swap + hook 独立状态追踪，实现原子更新、一键回滚、SHA-256 校验和 hook 状态可视化。

## Non-Goals

- 可插拔后端（仅支持 Langfuse 作为目标）
- 数据脱敏（独立需求）
- Hook 看门狗自动修复（独立需求，但 hook-state.json 的 `error` 状态可被看门狗复用）
- Canary 发布策略
- 跨大版本自动迁移（如 0.x → 1.x 的 config schema 迁移）

## Decisions

- design_source: `docs/designs/20260616-versioned-upgrade-architecture.md`
- 发布渠道使用 GitHub Releases，版本信息（含 stable/pre-release 区分）由 GitHub 原生管理，不维护额外的 manifest 文件。
- `current`/`previous` pointer 采用纯文本文件格式，写入通过 write-to-temp + `mv`（rename）保证原子性。hook 版本通过 `hook-state.json` 独立追踪。两者分离是因为 hook 拷贝到 Agent 自身目录后独立于 langstash 版本目录，且各 agent hook 可能处于不同版本。
- 升级顺序为 langstash 先于 hook（接收端先就绪），由外部独立进程（`installer.sh`）驱动，跨越 langstash 重启存活。
- hook uninstall 调用 `hook-state.json` 中记录的该 agent 实际版本的 `uninstall.sh`（而非 langstash 的 `previous` pointer），因为各 agent hook 版本可能各不相同。
- hook 状态枚举仅四种：`undetected`、`not_installed`、`installed`、`error`。升级失败回装后的状态统一使用 `error`（附带 error 字段记录失败原因），不使用 `rollback` 作为独立状态。
- `deploy/` 目录只放统一安装入口（`installer.sh`）和 CI 打包脚本（`package.sh`），各组件的 install/uninstall 脚本保持在各自目录内。开发工具脚本（如 `run-tests.sh`）放在 `scripts/` 下。
- 根目录原有的 `install.sh`、`uninstall.sh`、`upgrade.sh` 保留为 thin wrapper 转发到 `deploy/installer.sh`，保持向后兼容。
- `deploy/package.sh` 从 `VERSION` 文件读取版本号，输出 tarball 和 `SHA256SUMS` 到当前目录（或通过 `--output-dir` 指定）。
- 所有 shell 脚本必须同时兼容 macOS 和 Linux（CLAUDE.md 约束）。
- install.sh / uninstall.sh 保持幂等（CLAUDE.md 约束）。

## Requirements

### R-1: 版本化目录布局替代 git clone

- context: 当前 `install-remote.sh` 将整个 git repo clone 到 `~/.agent-exporter-to-langfuse/`，升级通过 `git checkout` 切换 tag。整个安装目录即 git 工作树，无版本隔离。
- must:
  - 安装目录采用 `versions/<ver>/` 布局，每个版本解压到独立子目录
  - `config/`、`data/`、`logs/` 目录在 `versions/` 外，跨版本共享
  - `current` 文件为纯文本 pointer，内容为当前活跃版本号；写入通过 write-to-temp + `mv`（rename）保证原子性
  - `previous` 文件为纯文本 pointer，内容为上一版本号（用于回滚）；同样通过 write-to-temp + `mv` 写入
  - 首次安装时不需要 `git` 命令，仅需 `curl` 或 `wget`
- must_not:
  - 安装目录中不再包含 `.git/` 目录
  - `versions/<ver>/` 内不包含 `config/`、`data/`、`logs/`（这些在共享目录中）
- verification:
  - 首次安装后，`~/.agent-exporter-to-langfuse/current` 存在且内容为安装版本号，`versions/<ver>/` 包含完整版本包，`config/`、`data/` 在版本目录外
  - 在无 `git` 环境中执行 `installer.sh install` 成功完成

### R-2: GitHub Releases 分发与 SHA-256 校验

- context: 当前升级通过 `git ls-remote --tags` 检查版本，通过 `git fetch + checkout` 获取代码，无完整性校验。
- must:
  - CI 在 push tag `v*` 时自动构建 tarball 并上传为 GitHub Release asset
  - tag 含 `-` 时标记为 pre-release（`gh release create --prerelease`）
  - Release assets 包含 tarball 和 `SHA256SUMS` 校验文件
  - 安装器和升级器下载 tarball 后执行 SHA-256 校验，校验失败则中止并报错
  - 打包脚本排除 `.git/`、`.venv/`、`__pycache__/`、`node_modules/`
  - `deploy/package.sh` 从 `VERSION` 文件读取版本号（不带 `v` 前缀），输出 tarball 和 `SHA256SUMS` 到当前目录（可通过 `--output-dir` 指定输出目录）
- must_not:
  - 不维护额外的 manifest 文件（如 `latest.json`），版本信息完全由 GitHub Releases 管理
- verification:
  - `deploy/package.sh` 生成 tarball + `SHA256SUMS`，手动篡改 tarball 后安装器拒绝安装并输出校验错误

#### Scenario: pre-release 版本发布
- given: 开发者推送 tag `v0.4.0-beta.1`
- when: CI workflow 触发
- then: GitHub Release 创建且标记为 pre-release，包含 tarball 和 `SHA256SUMS` 两个 assets

### R-3: 升级器从 git tags 改为 GitHub Releases API

- context: 当前 `updater.py` 通过 `git ls-remote --tags` 检查新版本，通过调用 `upgrade.sh`（`git checkout`）执行升级。
- must:
  - 升级检查改为调用 GitHub Releases API（`releases/latest` 或 `releases` 列表）
  - `include_prerelease = false`（默认）时，查询 `releases/latest`（GitHub 自动排除 pre-release）
  - `include_prerelease = true` 时，查询 `releases` 列表取最新版本
  - 检测到新版本后记录到 `.update-check` 文件，通过 `/stats` API 和 WebUI 展示
  - 执行升级时，spawn `installer.sh upgrade` 为独立进程（`start_new_session=True`），返回 `{"status": "started"}`
- must_not:
  - 升级检查不再调用 `git ls-remote` 或任何 git 命令
  - 升级执行不再调用 `upgrade.sh` 中的 `git checkout`
- verification:
  - updater 定期检查 GitHub Releases API 并正确识别新版本（stable 和 pre-release 分别测试）
  - 无 git 环境中升级检查和执行均正常工作

### R-4: 安装器支持 install/upgrade/uninstall 子命令

- context: 当前 `install-remote.sh` 只做首次安装（git clone），升级和卸载分别由 `upgrade.sh` 和 `uninstall.sh` 独立处理。
- must:
  - `deploy/installer.sh` 支持三个子命令：`install`、`upgrade`、`uninstall [--purge]`
  - `install`：下载 tarball → SHA-256 校验 → 解压到 `versions/<ver>/` → 写 `current` pointer → 运行版本内 install.sh。安装目录下已存在 `config/`、`data/`、`logs/` 等残留数据时（如 uninstall 后未 purge 再重装），install 正常执行并复用已有配置和数据
  - `upgrade`：下载 tarball → 校验 → 解压 → pointer swap → 重启 langstash → 升级 hooks → GC
  - `uninstall`（不带 `--purge`）：停止 langstash 服务 → 卸载所有 hooks → 删除 `versions/`、pointer 文件、`hook-state.json` → 删除 `~/.local/bin/langstash` wrapper 脚本 → 注销 launchd/systemd 服务 → 保留 `config/`、`data/`、`logs/`
  - `uninstall --purge`：在上述基础上额外删除 `config/`、`data/`、`logs/`，完整清除 `~/.agent-exporter-to-langfuse/` 目录
  - 支持 `--package-url` 覆盖下载源（兼容 `file:///` 本地路径用于内网/离线场景）
  - 支持 `--version` 指定安装特定版本
  - 根目录原有的 `install.sh`、`uninstall.sh`、`upgrade.sh` 保留为 thin wrapper 转发到 `deploy/installer.sh`
- must_not:
  - `deploy/installer.sh` 不依赖 `git` 命令
- verification:
  - `installer.sh install` 在干净环境中完整安装成功
  - `installer.sh upgrade` 从旧版本升级到新版本成功
  - `installer.sh uninstall --purge` 清理所有文件和服务
  - `--package-url file:///path/to/local.tar.gz` 离线安装成功

### R-5: 升级流程 langstash 先于 hook 且跨重启存活

- context: langstash 和 hook 是独立升级的组件。新 hook 可能产出新 schema 格式数据，必须先升级接收端（langstash）。升级过程中 langstash 需要重启，驱动升级的进程必须存活。
- must:
  - 升级由 `installer.sh` 作为独立进程驱动（`start_new_session=True` / `nohup`），不随 langstash 重启而终止
  - 升级顺序：下载解压 → uv sync → pointer swap → 重启 langstash → 等待 `/health` 就绪 → 逐个升级 hook
  - langstash 重启后，installer.sh 轮询 `/health` 等待就绪，超时则标记升级失败
  - WebUI 触发升级时同理：`POST /upgrade` → spawn installer.sh → 返回 `{"status": "started"}`
  - installer.sh 中途被终止（kill/crash）后，`hook-state.json` 仍反映各 hook 的实际版本（尚未变更），可通过 `langstash upgrade --retry-hooks` 或下次 upgrade 恢复
- must_not:
  - 升级逻辑不在 langstash 进程内执行（避免被重启中断）
  - hook 升级不在 langstash 重启之前执行
  - installer.sh 中途被终止不导致系统进入不可恢复状态
- verification:
  - 升级过程中 langstash 被重启后，hook 升级仍继续执行并完成
  - langstash 重启超时时，升级标记为失败且不继续升级 hook
  - installer.sh 在 pointer swap 后、hook 升级前被 kill，系统可通过 `--retry-hooks` 恢复

#### Scenario: langstash 重启后继续升级 hook
- given: installer.sh 已完成 pointer swap 并重启了 langstash
- when: 新 langstash 启动并通过 `/health` 检查
- then: installer.sh 继续逐个执行 hook 升级

### R-6: Hook 升级 uninstall → install + 失败回装

- context: hook 升级需要先卸载旧版本再安装新版本。各 agent hook 可能处于不同版本。
- must:
  - 升级单个 hook 的流程：读取 `hook-state.json` 获取该 agent 实际版本 → 备份 hook 状态 → 调用实际版本的 `uninstall.sh` → 调用新版本的 `install.sh` → 更新 `hook-state.json`（成功时 version=新版本, status=`installed`）
  - uninstall 调用 `hook-state.json` 中记录的该 agent 实际版本的 `uninstall.sh`
  - install 调用 `current`（新版本）的 `install.sh`
  - install 失败时用该 agent 实际版本的 `install.sh` 回装旧版本，并更新 `hook-state.json`（status=error）
- must_not:
  - 不统一使用 langstash 的 `previous` pointer 作为 hook uninstall 版本
  - install 失败后不允许 hook 处于完全丢失状态（必须尝试回装）
- verification:
  - hook 升级成功后 `hook-state.json` 中 version 和 status 正确更新
  - hook 升级失败时旧版本被回装，`hook-state.json` 记录 error 状态和错误信息
  - 两个 agent hook 处于不同版本时，各自使用正确版本的 uninstall.sh

#### Scenario: 部分 hook 升级失败
- given: claude-code hook 版本为 0.3.0，qoder hook 版本为 0.2.0（上次升级失败）
- when: 升级到 0.4.0，claude-code 成功，qoder install 失败
- then: claude-code hook-state 为 `{"version": "0.4.0", "status": "installed"}`，qoder 被回装且 hook-state 为 `{"version": "0.2.0", "status": "error", "error": "..."}`

### R-7: hook-state.json 独立追踪 hook 版本和状态

- context: langstash 和 hook 版本独立，单一 pointer 无法反映各 agent hook 的实际状态。
- must:
  - `hook-state.json` 存放在 `~/.agent-exporter-to-langfuse/` 根目录（跨版本共享）
  - 每个 agent 记录 `version`（可选）、`status`、`error`（可选）字段
  - hook 状态定义为四种：`undetected`（Agent 未安装）、`not_installed`（Agent 已安装但 hook 未部署）、`installed`（正常运行）、`error`（异常）
  - hook 安装、升级、卸载、回装操作完成后必须更新 `hook-state.json`
- must_not:
  - hook 版本不记录在 `current`/`previous` pointer 中
- verification:
  - 各生命周期操作（安装/升级/卸载/回装/retry）后 `hook-state.json` 状态正确

### R-8: Hook 状态透出到 /stats API 和 WebUI

- must:
  - `/stats` API 返回 `hooks` 字段，包含每个 agent 的 version、status、error
  - `/stats` API 返回 `hook_version_mismatch`（布尔）和 `hook_mismatch_agents`（列表），标识哪些 hook 与 langstash 版本不一致
  - WebUI Dashboard 增加 Hooks 状态区块，按状态展示颜色标签（installed=绿色、error=红色、not_installed/undetected=灰色）
  - `error` 状态显示错误信息和 `Retry` 按钮
  - `not_installed` 状态显示 `Install` 按钮
  - 版本不一致时页面顶部显示告警条
  - langstash 启动时版本不一致记录 WARNING 日志
- must_not:
  - WebUI 不直接读取 `hook-state.json` 文件，通过 `/stats` API 获取
- verification:
  - 部分 hook 升级失败后，`/stats` 返回正确的 mismatch 信息，WebUI 显示红色 Error 标签和错误信息

### R-9: Hook 升级重试

- must:
  - `POST /upgrade/retry-hooks` 重试所有 status 为 `error` 的 hook 升级
  - `POST /upgrade/retry-hooks?agent=<name>` 重试指定 agent 的 hook 升级
  - CLI `langstash upgrade --retry-hooks` 提供相同功能
  - 重试流程与正常 hook 升级相同（uninstall 实际版本 → install current 版本）
- verification:
  - 通过 API 和 CLI 重试后，原 error 状态的 hook 成功升级为 installed

### R-10: 回滚

- must:
  - `langstash rollback` 命令 swap `current` ↔ `previous` pointer（通过 write-to-temp + `mv` 原子写入）
  - 重启 langstash 服务
  - 逐个对 hook 执行 uninstall → install 回装：uninstall 调用 `hook-state.json` 中该 agent 实际版本的 `uninstall.sh`，install 调用回滚后 `current` 版本的 `install.sh`
  - 回装成功的 hook 更新 `hook-state.json`（version=回滚后版本, status=installed）
  - 回装失败的 hook 更新 `hook-state.json`（status=error），不阻塞其余 hook 继续回装
- must_not:
  - 回滚后 `previous` 不指向比 `current` 更新的版本（swap 后 previous 变为原 current）
- verification:
  - 回滚后 langstash 运行旧版本，hook 回装到旧版本，`hook-state.json` 状态正确
  - 回滚过程中某个 hook 回装失败时，`hook-state.json` 记录 `error` 状态，其余 hook 继续回装

#### Scenario: 回滚
- given: current=0.3.0，previous=0.2.0，hooks 均为 0.3.0
- when: 执行 `langstash rollback`
- then: current=0.2.0，previous=0.3.0，langstash 运行 0.2.0，hooks 回装到 0.2.0

### R-11: 旧版本垃圾回收

- must:
  - 升级成功后 GC `versions/` 目录，只保留 `current` 和 `previous` 指向的版本
  - 其余版本目录删除
- must_not:
  - 不删除 `current` 或 `previous` 指向的版本目录
- verification:
  - 连续升级三次后 `versions/` 下只有两个目录

### R-12: Hook 自包含约束

- context: 部分 Agent（如 QoderWork）在容器/VM 中运行，仅挂载 Agent 自身目录，不挂载 `~/.agent-exporter-to-langfuse/`。
- must:
  - hook 脚本、依赖库（`langstash_deliver`）、Python venv（`.venv/`）必须完整存在于 Agent 自身目录内
  - 凭证通过 Agent 插件机制传递（如 `CLAUDE_PLUGIN_OPTION_*`），不依赖外部 env 文件
  - 外部 env source（`$HOME/.agent-exporter-to-langfuse/config/*.env`）仅为 fallback，失败时静默跳过
- must_not:
  - hook 运行时不引用 `~/.agent-exporter-to-langfuse/versions/` 下的文件
- verification:
  - 在 Agent 目录外的 `~/.agent-exporter-to-langfuse/` 不可访问时，hook 仍能正常执行

### R-13: 升级流程中的脚本定位规则

- must:
  - `deploy/` 目录包含 `installer.sh`（远程安装入口）和 `package.sh`（CI 打包）
  - 各 hook 的 `install.sh`/`uninstall.sh` 保持在 `hooks/<agent>/` 下
  - langstash 的 `install-langstash.sh`/`uninstall-langstash.sh` 保持在 `exporter/` 下
  - 升级流程中基于版本目录定位脚本：
    - hook uninstall：`versions/<hook-state 中该 agent 实际版本>/hooks/<agent>/uninstall.sh`
    - hook install：`versions/<current>/hooks/<agent>/install.sh`
    - langstash install：`versions/<current>/exporter/install-langstash.sh`
- must_not:
  - `deploy/` 不包含各组件的 install/uninstall 脚本（这些保持在组件目录内）
- verification:
  - 升级流程中 uninstall 和 install 调用正确版本目录下的脚本

### R-14: 存量用户从 git 布局迁移到版本化布局

- context: 已通过 `git clone` 安装的存量用户，安装目录下有 `.git/` 目录，升级到新版 installer 时需要迁移到版本化布局。
- must:
  - `installer.sh upgrade` 检测到旧布局（存在 `.git/` 目录）时，执行一次性迁移
  - 迁移步骤：读取当前 `VERSION` → 将当前目录内容打包为 `versions/<current_ver>/` → 创建 `current` pointer → 删除 `.git/` 目录 → 继续正常升级流程
  - `config/`、`data/`、`logs/` 如已在安装根目录下则保持原位（已是跨版本共享布局）
  - 迁移过程幂等，重复执行不产生副作用
- must_not:
  - 迁移不删除用户数据（`config/`、`data/`、`logs/`）
  - 迁移不中断正在运行的 langstash 服务（先迁移目录结构，再重启）
- verification:
  - 从 git 布局执行 `installer.sh upgrade` 后，目录结构符合版本化布局，langstash 和 hooks 正常运行

### R-15: langstash CLI 全局可执行

- context: 当前 `langstash` 命令仅存在于 `.venv/bin/langstash`，不在 PATH 中，用户无法直接执行 `langstash upgrade`、`langstash restart` 等命令。
- must:
  - 安装时将 `langstash` 命令注册到用户 PATH 可达的位置（如 `~/.local/bin/langstash`）
  - 注册的是一个 wrapper 脚本（而非 symlink），读取 `current` pointer 后调用对应版本的 `.venv/bin/langstash`
  - wrapper 脚本在版本升级后无需更新（因为它动态解析 current 版本）
  - 支持的子命令至少包括：`start`、`stop`、`restart`、`status`、`upgrade`、`upgrade --retry-hooks`、`rollback`
  - 安装时检测 `~/.local/bin` 是否在 PATH 中，不在则提示用户添加
- must_not:
  - wrapper 脚本不硬编码版本路径
  - 不要求 root 权限安装到 `/usr/local/bin`（用户级安装）
- verification:
  - 安装后在新终端中直接执行 `langstash status` 能正常返回
  - 升级版本后 `langstash status` 自动指向新版本，无需重新注册

#### Scenario: langstash CLI wrapper
- given: current pointer 内容为 "0.3.0"
- when: 用户执行 `langstash status`
- then: wrapper 读取 current → 调用 `~/.agent-exporter-to-langfuse/versions/0.3.0/exporter/.venv/bin/langstash status`

### R-16: Hook 状态获取方案

- context: `hook-state.json` 记录各 hook 的版本和状态，但状态的来源（写入时机和验证方式）需要明确定义，否则状态可能与实际不一致。
- must:
  - Hook 状态通过以下两种机制获取和维护：
  - **写入时更新**（installer 驱动）：
    - `install.sh` 执行成功后写入 `status: installed` + version
    - `install.sh` 执行失败后写入 `status: error` + error 信息
    - `uninstall.sh` 执行成功后写入 `status: not_installed`，移除 version
    - 回装旧版本后写入 `status: error` + 回装原因
  - **启动时探测**（langstash 启动驱动）：
    - langstash 启动时读取 `hook-state.json`，对每个状态为 `installed` 的 hook 执行验证：检查 Agent 的 settings/hooks 配置中是否存在预期的 hook 条目（通过 markers 关键词匹配）
    - 验证失败（hook 条目不存在或被覆盖）则将状态更新为 `error`，error 字段记录 "hook entry missing or overwritten in agent settings"
    - 对每个 `agents.d/*.json` 中定义的 agent，检测是否安装在系统上（检测路径/命令），更新 `undetected` ↔ `not_installed` 状态
  - `/stats` API 返回的 hook 状态来源为内存中加载的 `hook-state.json`（启动时探测后的结果），不每次请求都读文件
- must_not:
  - 不在每次 `/stats` 请求时都执行 hook 验证探测（性能考虑），仅启动时和升级/重试操作后执行
- verification:
  - 安装 hook 后 `hook-state.json` 中状态为 `installed`
  - 手动删除 Agent settings 中的 hook 条目后，重启 langstash，状态变为 `error`
  - 新安装一个 Agent（如 codex）后，重启 langstash，状态从 `undetected` 变为 `not_installed`

#### Scenario: 启动时探测发现 hook 被覆盖
- given: hook-state.json 中 claude-code status 为 `installed`，但 `~/.claude/settings.json` 中 hook 条目已被 Agent 更新覆盖
- when: langstash 启动
- then: 探测发现 hook 条目缺失，claude-code status 更新为 `error`，`/stats` API 返回 mismatch 告警

## Open Questions

（无）
