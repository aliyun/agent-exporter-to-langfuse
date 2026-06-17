## Header
- source_spec: /Users/song/code/agent-exporter-to-langfuse/docs/specs/20260616-versioned-upgrade-architecture.md
- risk: high-risk
- runtime_profile: high-risk
- runtime_profile_basis: 公共 CLI 合约（langstash 子命令）、持久化格式（pointer 文件 + hook-state.json）、服务生命周期管理（launchd/systemd）、跨进程协调（installer.sh 独立进程跨 langstash 重启）、存量用户迁移
- accepted_debt: none
- status: ready
- external_review_policy: required

## Requirements Covered
- R-1: 版本化目录布局替代 git clone
- R-2: GitHub Releases 分发与 SHA-256 校验
- R-3: 升级器从 git tags 改为 GitHub Releases API
- R-4: 安装器支持 install/upgrade/uninstall 子命令
- R-5: 升级流程 langstash 先于 hook 且跨重启存活
- R-6: Hook 升级 uninstall → install + 失败回装
- R-7: hook-state.json 独立追踪 hook 版本和状态
- R-8: Hook 状态透出到 /stats API 和 WebUI
- R-9: Hook 升级重试
- R-10: 回滚
- R-11: 旧版本垃圾回收
- R-12: Hook 自包含约束
- R-13: 升级流程中的脚本定位规则
- R-14: 存量用户从 git 布局迁移到版本化布局
- R-15: langstash CLI 全局可执行
- R-16: Hook 状态获取方案

## Planning Evidence

### surfaces
- `install-remote.sh`(67L): git clone 入口，被 `deploy/installer.sh` 替代
- `install.sh`(498L): 本地统一安装器，agent 检测 + per-agent install 调用
- `upgrade.sh`(125L): git fetch + checkout 升级，被 installer.sh upgrade 替代
- `uninstall.sh`(311L): 统一卸载器
- `exporter/src/updater.py`(149L): 版本检查（git ls-remote）+ 触发升级（spawn upgrade.sh）
- `exporter/src/server.py`(327L): `/stats`、`/health`、`/upgrade`、`/settings` API + 内嵌 WebUI HTML
- `exporter/src/main.py`(104L): CLI 入口（argparse）+ 服务启动编排
- `exporter/install-langstash.sh`: langstash venv + service 注册
- `hooks/<agent>/install.sh`: per-agent hook 安装
- `hooks/<agent>/uninstall.sh`: per-agent hook 卸载

### consumers
- `exporter/src/server.py` 的 `/upgrade` 端点调用 `updater.start_upgrade()`
- `exporter/src/server.py` 的 `/stats` 端点调用 `updater.get_update_info()`
- WebUI HTML 内嵌在 `server.py` 中，通过 `/stats` 和 `/upgrade` 交互
- `exporter/src/main.py` 构造 `Updater` 并传入 `create_app()`
- launchd plist / systemd service 引用 `.venv/bin/langstash` 路径

### coupling
- updater.py 与 upgrade.sh 强耦合（`start_upgrade()` spawn `upgrade.sh`）
- server.py 的 `/stats` 返回结构被 WebUI JavaScript 直接消费
- install.sh 根据 agent 类型调用 `hooks/<agent>/install.sh`，参数传递靠环境变量
- installer.sh 的 upgrade 子命令实现升级编排（pointer swap → 重启 → hook 升级），updater.py 仅负责检测 + spawn

### unknowns
- per-agent install.sh/uninstall.sh 的接口差异（各 agent 参数和行为不完全统一）
- 存量用户目录下实际文件布局变体（可能有手动修改）

## Phases with Tasks

### phase-1: 打包脚本
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: bash deploy/package.sh && test -f SHA256SUMS

#### task-1: 创建打包脚本和 CI 发布流程
- requirements: [R-2]
- outputs: [deploy/package.sh, .github/workflows/release.yml]
- action: 创建打包脚本（从 VERSION 读取版本号，构建 tarball，生成 SHA256SUMS）和 CI workflow（push tag v* 时上传 Release assets，含 pre-release 标记）。
- constraints:
  - package.sh 兼容 macOS 和 Linux（sha256sum vs shasum）
  - VERSION 文件内容不带 `v` 前缀（CLAUDE.md 约束）
- verification:
  - shell: bash deploy/package.sh && test -f agent-exporter-to-langfuse-*.tar.gz && test -f SHA256SUMS

### phase-2: 安装器核心（install + uninstall）
- commit_boundary: phase
- worker_dispatch: per-task
- gate: shell: bash deploy/installer.sh install --package-url file://$(pwd)/agent-exporter-to-langfuse-*.tar.gz && cat ~/.agent-exporter-to-langfuse/current && test -f ~/.local/bin/langstash && bash deploy/installer.sh uninstall --purge

#### task-2: installer.sh install 子命令 + 版本化目录布局 + CLI wrapper
- requirements: [R-1, R-4, R-15]
- outputs: [deploy/installer.sh]
- action: 创建 installer.sh 的 install 子命令：下载 tarball + SHA-256 校验 + 解压到 versions/<ver>/ + 写 current pointer + 调用版本内 install.sh。安装 ~/.local/bin/langstash wrapper 脚本。支持 --package-url 和 --version 参数。残留 config/data 时正常复用。
- constraints:
  - 不依赖 git，仅需 curl 或 wget
  - pointer 写入通过 write-to-temp + mv 原子性
  - 幂等，兼容 macOS 和 Linux
  - wrapper 脚本动态读取 current pointer，不硬编码版本路径
- verification:
  - shell: bash deploy/installer.sh install --package-url file:///path/to/test.tar.gz && test -f ~/.agent-exporter-to-langfuse/current && test -d ~/.agent-exporter-to-langfuse/versions/
  - shell: which langstash && langstash --help

#### task-3: installer.sh uninstall 子命令
- requirements: [R-4]
- outputs: [deploy/installer.sh]
- action: 实现 uninstall 子命令：停止服务 → 卸载 hooks → 清理 versions/pointer/hook-state/wrapper/服务注册，--purge 额外删除 config/data/logs。
- depends_on: [task-2]
- constraints:
  - uninstall 清理 ~/.local/bin/langstash wrapper 和 launchd/systemd 服务
  - 幂等，兼容 macOS 和 Linux
- verification:
  - shell: bash deploy/installer.sh uninstall --purge && test ! -d ~/.agent-exporter-to-langfuse

### phase-3: hook-state.json 与状态探测
- commit_boundary: phase
- worker_dispatch: per-task
- gate: shell: cd exporter && uv run pytest tests/test_hook_state.py tests/test_server.py -v

#### task-4: 实现 hook-state.json 读写和启动时探测
- requirements: [R-7, R-16]
- outputs: [exporter/src/hook_state.py]
- action: 新增 hook_state.py：hook-state.json 加载/保存/更新，四种状态枚举，启动时探测（验证 installed hook 是否被覆盖、检测新 agent 安装状态）。
- constraints:
  - hook-state.json 在安装根目录，跨版本共享
  - 状态枚举仅四种，无 rollback 状态
  - 启动时探测加载到内存，/stats 不重复探测
- verification:
  - planned_test: test_hook_state.py 覆盖四种状态转换、启动探测发现被覆盖标记 error、新 agent 标记 not_installed

#### task-5: 集成 hook-state 到 /stats API 和 WebUI
- requirements: [R-8, R-9]
- outputs: [exporter/src/server.py, exporter/src/main.py]
- action: 扩展 /stats 返回 hooks 字段和 mismatch 信息。新增 POST /upgrade/retry-hooks 端点。WebUI 增加 Hooks 状态区块。main.py 启动时调用探测。
- depends_on: [task-4]
- constraints:
  - WebUI 通过 /stats API 获取，不直接读文件
  - 运行时配置读写通过 Server API（CLAUDE.md 架构约束）
- verification:
  - planned_test: test_server.py 覆盖 /stats hooks 字段和 /upgrade/retry-hooks 端点
  - inspect: WebUI HTML 中 hooks 区块包含状态标签、Retry 按钮、告警条

### phase-4: 升级与回滚流程
- commit_boundary: phase
- worker_dispatch: grouped
- worker_dispatch_basis: installer.sh upgrade/rollback 子命令与 updater.py 共享 hook-state 写入路径、pointer 读取、installer.sh spawn 接口，分 worker 会导致接口不匹配
- gate: shell: cd exporter && uv run pytest tests/test_updater.py -v && rg 'git ls-remote|git fetch|git checkout' exporter/src/ --count-matches | grep -v ':0$' | wc -l | grep -q '^0$' && rg 'rollback' deploy/installer.sh exporter/src/updater.py --count-matches | grep -v ':0$' | wc -l | test $(cat) -ge 2

#### task-6: installer.sh upgrade 子命令（升级编排 + hook 升级 + GC + 存量迁移）
- requirements: [R-4, R-5, R-6, R-11, R-13, R-14]
- outputs: [deploy/installer.sh]
- action: 实现 upgrade 子命令：检测旧 git 布局并迁移（R-14），然后执行完整升级编排：pointer swap → 重启 langstash → 轮询 /health → 逐个 hook 升级（从 hook-state.json 读实际版本调 uninstall.sh，用 current 版本调 install.sh，失败回装）→ GC 旧版本。
- constraints:
  - 升级顺序：langstash 先于 hook
  - installer.sh 作为独立进程运行（start_new_session），跨 langstash 重启存活
  - 旧布局迁移不删除用户数据（config/data/logs），幂等，不中断运行中的 langstash
  - hook uninstall 调用 hook-state.json 中该 agent 实际版本的脚本
  - GC 只保留 current + previous 指向的版本目录
  - 中途终止后可通过 --retry-hooks 恢复
- verification:
  - shell: 模拟升级流程验证 pointer swap + hook-state.json 更新正确
  - shell: 模拟 /health 超时验证 installer.sh 退出并标记失败
  - shell: 模拟 installer.sh 在 pointer swap 后被 kill，验证 hook-state.json 反映各 hook 实际版本未变更，随后执行 --retry-hooks 验证恢复
  - shell: 准备带 .git/ 和 VERSION 的模拟旧布局，执行 installer.sh upgrade 后验证 versions/ 存在、current 正确、.git/ 已删除、config/data 保留；重复执行验证幂等
  - planned_test: test_updater.py 覆盖 --retry-hooks 恢复场景

#### task-7: installer.sh rollback + updater.py 重构
- requirements: [R-3, R-10]
- outputs: [deploy/installer.sh, exporter/src/updater.py, exporter/src/server.py]
- action: 实现 rollback 子命令（swap pointer → 重启 langstash → 逐个 hook uninstall→install 回装）。重写 updater.py：版本检查改为 GitHub Releases API，start_upgrade() 通过 start_new_session=True spawn installer.sh upgrade 为独立进程。适配 server.py /upgrade 端点调用新 start_upgrade()。
- depends_on: [task-6]
- constraints:
  - updater.start_upgrade() 必须以 start_new_session=True spawn installer.sh，确保跨 langstash 重启存活
  - 升级检查和执行不调用 git 命令
  - 回滚失败的 hook 标记 error 状态，不阻塞其余 hook
- verification:
  - planned_test: test_updater.py 覆盖 GitHub API 版本检查（stable/pre-release）、start_upgrade spawn 独立进程验证 start_new_session
  - shell: 模拟回滚验证 pointer swap + hook 回装 + hook-state.json 更新
  - source_scan: rg 'git ls-remote|git fetch|git checkout' exporter/src/ — 0 hits

### phase-5: 向后兼容 wrapper 与 CLI 子命令
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: head -2 upgrade.sh | grep -q installer.sh

#### task-8: 根目录 thin wrapper + langstash CLI 子命令
- requirements: [R-4, R-15]
- outputs: [install.sh, upgrade.sh, uninstall.sh, exporter/src/main.py]
- action: 根目录脚本改为 thin wrapper 转发到 installer.sh。扩展 main.py argparse 增加 start/stop/restart/status/upgrade/rollback 子命令。
- constraints:
  - wrapper 幂等，兼容 macOS 和 Linux
  - phase-1~4 期间根目录脚本保持原样不影响测试（测试直接调用 installer.sh）
- verification:
  - shell: bash upgrade.sh --help 2>&1 | grep -q installer
  - inspect: main.py argparse 包含 start/stop/restart/status/upgrade/rollback

### phase-6: Hook 自包含约束验证
- commit_boundary: task
- worker_dispatch: per-task
- gate: source_scan: rg '\.agent-exporter-to-langfuse/versions/' hooks/ --glob '*.sh' --glob '*.py' — 0 hits

#### task-9: 确保 hook 自包含并适配版本化布局
- requirements: [R-12, R-13]
- outputs: [hooks/claude-code/install.sh, hooks/qoder/install.sh, hooks/qoderwork/install.sh, hooks/opencode/install.sh, hooks/codex/install.sh]
- action: 审查修改各 agent 的 install.sh/uninstall.sh，确保 hook 和依赖完整拷贝到 Agent 目录内，运行时不引用 versions/ 下的文件。
- constraints:
  - hook 运行时不引用 ~/.agent-exporter-to-langfuse/versions/
  - 各 install.sh/uninstall.sh 保持幂等
- verification:
  - source_scan: rg '\.agent-exporter-to-langfuse/versions/' hooks/ --glob '*.sh' --glob '*.py' — 0 hits
  - inspect: 各 hook install.sh 中 langstash_deliver 和 .venv 拷贝到 Agent 目录

## Verification
- shell: bash deploy/package.sh && test -f SHA256SUMS
- shell: bash deploy/installer.sh install --package-url file://$(pwd)/agent-exporter-to-langfuse-*.tar.gz && cat ~/.agent-exporter-to-langfuse/current
- shell: langstash status
- shell: cd exporter && uv run pytest tests/ -v
- source_scan: rg 'git ls-remote|git fetch|git checkout|git clone' exporter/src/ deploy/ — 0 hits
- inspect: 目录结构符合版本化布局（versions/ + current + previous + hook-state.json + config/ + data/）
- inspect: /stats API 返回 hooks 字段和 hook_version_mismatch
