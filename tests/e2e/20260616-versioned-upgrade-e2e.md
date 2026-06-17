# E2E 测试用例：版本化升级架构

## 测试环境准备

每个测试用例使用隔离的 `$HOME` 目录（如 `/tmp/e2e-test-NNN`），避免影响真实安装。

```bash
export HOME=/tmp/e2e-test-$$
mkdir -p "$HOME"
```

测试前先构建一个本地 tarball：

```bash
bash deploy/package.sh --output-dir /tmp/e2e-pkg
```

---

## E2E-1: 首次安装（本地包）

**覆盖需求**: R-1, R-2, R-4, R-15

**步骤**:
```bash
bash deploy/installer.sh install \
  --package-url "file:///tmp/e2e-pkg/agent-exporter-to-langfuse-$(cat VERSION).tar.gz"
```

**验证**:
- [ ] `~/.agent-exporter-to-langfuse/current` 存在且内容为版本号
- [ ] `~/.agent-exporter-to-langfuse/versions/<ver>/` 包含 VERSION、exporter/、hooks/
- [ ] `~/.agent-exporter-to-langfuse/config/` 目录存在
- [ ] `~/.agent-exporter-to-langfuse/data/` 目录存在
- [ ] `~/.agent-exporter-to-langfuse/logs/` 目录存在
- [ ] `~/.local/bin/langstash` wrapper 存在且可执行
- [ ] 安装目录无 `.git/` 目录

```bash
test -f "$HOME/.agent-exporter-to-langfuse/current"
test -d "$HOME/.agent-exporter-to-langfuse/versions/$(cat VERSION)"
test -f "$HOME/.local/bin/langstash"
test ! -d "$HOME/.agent-exporter-to-langfuse/.git"
```

---

## E2E-2: SHA-256 校验拒绝篡改包

**覆盖需求**: R-2

**步骤**:
```bash
# 篡改 tarball
cp /tmp/e2e-pkg/agent-exporter-to-langfuse-*.tar.gz /tmp/e2e-tampered.tar.gz
echo "tampered" >> /tmp/e2e-tampered.tar.gz
cp /tmp/e2e-pkg/SHA256SUMS /tmp/e2e-tampered-SHA256SUMS

bash deploy/installer.sh install \
  --package-url "file:///tmp/e2e-tampered.tar.gz"
```

**验证**:
- [ ] 安装失败并输出 "SHA-256 mismatch"
- [ ] `~/.agent-exporter-to-langfuse/current` 不存在

---

## E2E-3: 指定版本安装

**覆盖需求**: R-4

**步骤**:
```bash
bash deploy/installer.sh install \
  --version "0.1.0-alpha.3" \
  --package-url "file:///tmp/e2e-pkg/agent-exporter-to-langfuse-0.1.0-alpha.3.tar.gz"
```

**验证**:
- [ ] `current` pointer 内容为 `0.1.0-alpha.3`
- [ ] `versions/0.1.0-alpha.3/` 存在

---

## E2E-4: Uninstall 不带 --purge

**覆盖需求**: R-4

**前置**: E2E-1 或 E2E-3 已执行

**步骤**:
```bash
bash deploy/installer.sh uninstall
```

**验证**:
- [ ] `versions/` 目录已删除
- [ ] `current` 和 `previous` pointer 已删除
- [ ] `hook-state.json` 已删除
- [ ] `~/.local/bin/langstash` wrapper 已删除
- [ ] `config/` 目录保留（如果之前有数据）
- [ ] `data/` 目录保留
- [ ] `logs/` 目录保留

---

## E2E-5: Uninstall --purge

**覆盖需求**: R-4

**前置**: E2E-1 已执行

**步骤**:
```bash
bash deploy/installer.sh uninstall --purge
```

**验证**:
- [ ] `~/.agent-exporter-to-langfuse/` 整个目录已删除
- [ ] `~/.local/bin/langstash` 已删除

```bash
test ! -d "$HOME/.agent-exporter-to-langfuse"
test ! -f "$HOME/.local/bin/langstash"
```

---

## E2E-6: Uninstall 后重装复用残留数据

**覆盖需求**: R-4

**步骤**:
```bash
# 安装
bash deploy/installer.sh install --package-url "file:///tmp/e2e-pkg/..."
# 创建一些数据
echo "test" > "$HOME/.agent-exporter-to-langfuse/data/test.txt"
# Uninstall 不 purge
bash deploy/installer.sh uninstall
# 重装
bash deploy/installer.sh install --package-url "file:///tmp/e2e-pkg/..."
```

**验证**:
- [ ] 安装成功
- [ ] `data/test.txt` 仍然存在

---

## E2E-7: 升级流程 + Pointer Swap

**覆盖需求**: R-4, R-5, R-11

**步骤**:
```bash
# 准备两个版本的包（修改 VERSION 文件模拟不同版本）
# 安装 v0.1.0
bash deploy/installer.sh install --package-url "file:///tmp/v1-pkg/..."

# 升级到 v0.2.0
bash deploy/installer.sh upgrade --package-url "file:///tmp/v2-pkg/..."
```

**验证**:
- [ ] `current` 指向新版本
- [ ] `previous` 指向旧版本
- [ ] `versions/` 下只有 current 和 previous 两个目录（GC 生效）
- [ ] hook-state.json 中的 version 更新为新版本

---

## E2E-8: 回滚

**覆盖需求**: R-10

**前置**: E2E-7 已执行（有 current 和 previous）

**步骤**:
```bash
bash deploy/installer.sh rollback
```

**验证**:
- [ ] `current` 和 `previous` 互换
- [ ] langstash 服务配置更新为回滚版本的路径

---

## E2E-9: 旧 Git 布局迁移

**覆盖需求**: R-14

**步骤**:
```bash
# 模拟旧的 git 布局
mkdir -p "$HOME/.agent-exporter-to-langfuse/.git"
mkdir -p "$HOME/.agent-exporter-to-langfuse/exporter/src"
mkdir -p "$HOME/.agent-exporter-to-langfuse/hooks/claude-code"
mkdir -p "$HOME/.agent-exporter-to-langfuse/config"
mkdir -p "$HOME/.agent-exporter-to-langfuse/data"
echo "0.1.0-alpha.2" > "$HOME/.agent-exporter-to-langfuse/VERSION"
echo "test-config" > "$HOME/.agent-exporter-to-langfuse/config/config.toml"

# 执行升级，触发迁移
bash deploy/installer.sh upgrade \
  --package-url "file:///tmp/e2e-pkg/agent-exporter-to-langfuse-0.1.0-alpha.3.tar.gz"
```

**验证**:
- [ ] `.git/` 已删除
- [ ] `versions/0.1.0-alpha.2/` 存在（旧版本被打包迁移）
- [ ] `current` pointer 指向新版本
- [ ] `config/config.toml` 保留且内容不变
- [ ] `data/` 保留
- [ ] 重复执行不产生副作用（幂等）

---

## E2E-10: Hook 升级失败回装

**覆盖需求**: R-6, R-7

**步骤**:
模拟场景：hook install.sh 失败（可通过临时替换 install.sh 为返回 exit 1 的脚本）

```bash
# 安装 v1
bash deploy/installer.sh install --package-url "file:///tmp/v1-pkg/..."
# 标记 hook 为已安装
echo '{"claude-code":{"version":"0.1.0","status":"installed"}}' > \
  "$HOME/.agent-exporter-to-langfuse/hook-state.json"

# 准备 v2 的包，其中 hooks/claude-code/install.sh 返回 exit 1
# ...修改 v2 包...

# 升级
bash deploy/installer.sh upgrade --package-url "file:///tmp/v2-pkg-bad/..."
```

**验证**:
- [ ] hook-state.json 中 claude-code status 为 "error"
- [ ] hook-state.json 中 claude-code version 保持旧版本号
- [ ] error 字段包含失败原因

---

## E2E-11: --retry-hooks 恢复

**覆盖需求**: R-5, R-9

**前置**: E2E-10 执行后有 error 状态的 hook

**步骤**:
```bash
# 修复 v2 的 install.sh（使其可以成功）
# 重试
bash deploy/installer.sh upgrade --retry-hooks
```

**验证**:
- [ ] 之前 error 状态的 hook 重试安装
- [ ] 成功后 hook-state.json 中 status 变为 "installed"

---

## E2E-12: langstash CLI wrapper 动态版本解析

**覆盖需求**: R-15

**前置**: 安装完成

**步骤**:
```bash
# 检查 wrapper 存在
which langstash  # (需要 ~/.local/bin 在 PATH)

# 验证 wrapper 内容是动态解析的
cat ~/.local/bin/langstash | grep -q "current"

# 升级后 wrapper 无需更新
langstash status  # 应指向新版本
```

**验证**:
- [ ] wrapper 内容包含对 `current` pointer 的读取
- [ ] wrapper 不硬编码版本路径
- [ ] 升级版本后 wrapper 自动指向新版本

---

## E2E-13: hook-state.json 状态探测

**覆盖需求**: R-7, R-16

**步骤**:
```bash
# 准备：设置 hook-state.json 中 claude-code 为 installed
echo '{"claude-code":{"version":"0.3.0","status":"installed"}}' > \
  "$HOME/.agent-exporter-to-langfuse/hook-state.json"

# 模拟 hook 被覆盖：删除 settings.json 中的 hook 条目
echo '{"hooks":{}}' > "$HOME/.claude/settings.json"

# 启动 langstash（触发启动时探测）
# 或直接调用 probe 逻辑
```

**验证**:
- [ ] 探测后 hook-state.json 中 claude-code status 变为 "error"
- [ ] error 字段包含 "hook entry missing or overwritten"

---

## E2E-14: /stats API 返回 hooks 信息

**覆盖需求**: R-8

**步骤**:
```bash
# 启动 langstash 后
curl -s http://127.0.0.1:5288/stats | python3 -m json.tool
```

**验证**:
- [ ] 响应包含 `hooks` 字段
- [ ] 响应包含 `hook_version_mismatch` 布尔值
- [ ] 响应包含 `hook_mismatch_agents` 列表
- [ ] 各 hook 的 version、status、error 字段正确

---

## E2E-15: POST /upgrade/retry-hooks

**覆盖需求**: R-9

**步骤**:
```bash
# 有 error 状态的 hook 时
curl -X POST http://127.0.0.1:5288/upgrade/retry-hooks

# 指定 agent 重试
curl -X POST "http://127.0.0.1:5288/upgrade/retry-hooks?agent=qoder"
```

**验证**:
- [ ] 返回 `{"status": "started"}`
- [ ] 实际触发 installer.sh upgrade --retry-hooks

---

## E2E-16: GC 只保留 current + previous

**覆盖需求**: R-11

**步骤**:
```bash
# 安装三个版本（手动放置目录模拟）
mkdir -p "$HOME/.agent-exporter-to-langfuse/versions/0.1.0"
mkdir -p "$HOME/.agent-exporter-to-langfuse/versions/0.2.0"
echo "0.2.0" > "$HOME/.agent-exporter-to-langfuse/current"

# 升级到 0.3.0
bash deploy/installer.sh upgrade --package-url "file:///tmp/v3-pkg/..."
```

**验证**:
- [ ] `versions/` 下只有两个目录（current 和 previous）
- [ ] `versions/0.1.0/` 已被 GC 删除

---

## E2E-17: langstash CLI 子命令

**覆盖需求**: R-15

**步骤**:
```bash
langstash --help
langstash status
langstash upgrade --help
```

**验证**:
- [ ] `--help` 列出所有子命令：run, start, stop, restart, status, upgrade, rollback
- [ ] `status` 显示版本号和运行状态
- [ ] `upgrade --help` 显示 --version 和 --retry-hooks 选项

---

## E2E-18: updater.py 使用 GitHub Releases API

**覆盖需求**: R-3

**验证**:
- [ ] `rg 'git ls-remote|git fetch|git checkout' exporter/src/` — 0 hits
- [ ] updater.py 中使用 `api.github.com/repos/.../releases`
- [ ] include_prerelease=false 时查询 `releases/latest`
- [ ] include_prerelease=true 时查询 `releases` 列表

```bash
rg 'git ls-remote|git fetch|git checkout' exporter/src/ --count-matches
# 预期：无输出
```

---

## E2E-19: Hook 自包含验证

**覆盖需求**: R-12, R-13

**验证**:
```bash
rg '\.agent-exporter-to-langfuse/versions/' hooks/ --glob '*.sh' --glob '*.py'
# 预期：0 hits
```

- [ ] hooks 目录下无任何脚本引用 `versions/` 路径
- [ ] 各 hook 的 install.sh 将依赖拷贝到 Agent 自身目录

---

## E2E-20: CI Release Workflow

**覆盖需求**: R-2

**验证**（CI 环境或手动模拟）:
- [ ] `.github/workflows/release.yml` 存在
- [ ] push tag `v0.4.0-beta.1` 时，Release 标记为 pre-release
- [ ] push tag `v1.0.0` 时，Release 标记为 stable
- [ ] Release assets 包含 tarball 和 SHA256SUMS

---

## E2E-21: package.sh 跨平台兼容

**覆盖需求**: R-2

**步骤**:
```bash
# macOS
bash deploy/package.sh && test -f SHA256SUMS && echo PASS

# Linux
bash deploy/package.sh && test -f SHA256SUMS && echo PASS
```

**验证**:
- [ ] macOS 上使用 `shasum -a 256` 生成 SHA256SUMS
- [ ] Linux 上使用 `sha256sum` 生成 SHA256SUMS
- [ ] 两个平台生成的校验和格式一致

---

## E2E-22: 幂等性验证

**覆盖需求**: R-1, R-4, R-14

**步骤**:
```bash
# 执行两次安装
bash deploy/installer.sh install --package-url "file:///tmp/..."
bash deploy/installer.sh install --package-url "file:///tmp/..."

# 执行两次迁移
bash deploy/installer.sh upgrade --package-url "file:///tmp/..."
bash deploy/installer.sh upgrade --package-url "file:///tmp/..."
```

**验证**:
- [ ] 两次安装不产生错误
- [ ] 目录结构正确
- [ ] pointer 内容正确
