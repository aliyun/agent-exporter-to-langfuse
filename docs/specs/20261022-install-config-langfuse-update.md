# 安装时已存在 config.toml 的 Langfuse 配置随安装提供值更新

## Purpose

安装流程（`deploy/installer.sh` 的 `install` 子命令，含对已装环境的重装）支持通过 `--public-key` / `--secret-key` / `--base-url` 参数或同名环境变量 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` 提供 Langfuse 凭据，并调用 `exporter/install-langstash.sh` 写 `~/.agent-exporter-to-langfuse/config/config.toml`。当前该脚本只在文件不存在时用这些值渲染整份模板；文件已存在时仅打印 `Config already exists` 并跳过写入，安装时提供的最新 Langfuse 值被静默丢弃。用户在已装环境上重装换凭据后，langstash 服务仍按文件中的旧 `[langfuse]` 值投递。本规格要求：安装时提供（非空）的 Langfuse 配置在 `config.toml` 已存在时同样写入并生效，同时不破坏该文件中其余内容。

## Diagnosis

症状：在 `config.toml` 已存在的环境上重新执行 `installer.sh install` 并传入新的 `--public-key` / `--secret-key` / `--base-url`，安装完成后文件中 `[langfuse]` 的 `public_key` / `secret_key` / `base_url` 仍为旧值；安装流程会重启 langstash，服务启动时加载该文件（`exporter/src/config.py` 的 `load_config`），`exporter/src/sender.py` 继续用旧凭据向旧 `base_url` 投递。而安装输出末尾打印的 `Langfuse:` 却是本次提供的新 `base_url`（`exporter/install-langstash.sh` 末段 `info "Langfuse: ${LANGFUSE_BASE_URL:-...}"`），输出与实际生效配置不一致。

根因：`exporter/install-langstash.sh` 的 "Write config.toml" 段只在文件缺失分支写入安装期提供的值，已存在分支不接收任何新值：

```bash
# exporter/install-langstash.sh（当前代码，示意摘录）
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" << EOF
[server]
host = "127.0.0.1"
port = 5288

[langfuse]
public_key = "${LANGFUSE_PUBLIC_KEY:-}"
secret_key = "${LANGFUSE_SECRET_KEY:-}"
base_url = "${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}"

...
EOF
    info "Created $CONFIG_FILE"
else
    info "Config already exists: $CONFIG_FILE"
fi
```

最短传播链：`deploy/installer.sh` 的 `cmd_install` 解析并导出 `LANGFUSE_*`（缺省为空字符串）→ 调用 `exporter/install-langstash.sh` → 文件已存在分支跳过写入 → `config.toml` 保留旧 `[langfuse]` → 安装后 `restart_langstash` 重启服务、启动时读入旧值 → sender 用旧凭据向旧地址投递。缺陷属于安装脚本的私有实现缺陷，不涉及运行时配置读写入口（Server API `GET/POST /settings`），也不影响 `langstash upgrade` 路径（`cmd_upgrade` 不接受 Langfuse 参数、不调用该写入逻辑，`config/` 跨版本共享保留）。

## Repair Direction

在安装期配置写入路径（`exporter/install-langstash.sh` 写 `config.toml` 的逻辑）中，当文件已存在时对 `[langfuse]` 的三个键做键级更新：仅当安装期为某键提供了非空值时替换该键；`[langfuse]` 段或个别键缺失时补齐；未提供（空值）的键保留既有值。该方向与仓库既有约定一致：各 agent hook 的 `install.sh`（如 `hooks/claude-code/install.sh`）对 `config/*.env` 同样按"提供值优先、未提供沿用旧值"解析并重写；`exporter/src/config.py` 的 `set_config_value` 已确立键级合并语义。不采用整文件模板重写已存在文件（会破坏用户自定义的其他配置，违背 `config/` 跨版本共享保留的不变量，`tests/e2e/test_versioned_upgrade.sh` 断言了 config 内容保留）；不扩展运行时 `/settings` API（安装期服务可能未运行，首装本就采用直接写文件的既有路径）。修复后，安装流程既有的重启动作使新值生效。

## Decisions

- design_source: `none`

## Requirements

### R-1: 安装时提供的 Langfuse 配置写入已存在的 config.toml 并生效

- context: 安装入口是 `deploy/installer.sh` 的 `install` 子命令（含重装），它将 `--public-key` / `--secret-key` / `--base-url`（或环境变量 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`，未提供时为空字符串）传入 `exporter/install-langstash.sh`；`~/.agent-exporter-to-langfuse/config/config.toml` 是 langstash 服务启动时加载的持久配置，其 `[langfuse]` 段含 `public_key` / `secret_key` / `base_url` 三个键。当前这些安装期值只在文件不存在时进入文件。
- must:
  - 安装流程写配置时，若 `config.toml` 已存在，且安装期为 `public_key` / `secret_key` / `base_url` 中某键提供了非空值，则该键在 `[langfuse]` 段中的值更新为安装期提供的值；键已存在则替换其值，`[langfuse]` 段或该键缺失时补齐。
  - 安装期未提供（值为空）的键保留文件中的既有值，包括既有值非默认值的情况（如用户自定义的私有化 Langfuse 地址）。
  - 除上述三个 `[langfuse]` 键外，文件其余内容——其他 section 的键值与未涉及的行——在更新后保持不变。
  - 安装流程完成后，langstash 服务按更新后的 `[langfuse]` 配置投递（安装流程在写配置后会重启服务，服务启动时重新加载该文件）。
  - `config.toml` 不存在时的首装行为保持现状：按现有模板创建，`base_url` 未提供时使用模板默认值 `https://us.cloud.langfuse.com`。
- must_not:
  - 不得在安装期未提供某键的值时清空或删除该键的既有值。
  - 不得以整份模板重写已存在的 `config.toml`，即不得破坏其他 section 的键值或用户自定义值。
  - 不得改变 `langstash upgrade` 路径的既有行为：upgrade 不更新 `config.toml`（config 跨版本保留）。
- verification:
  - 在 `config.toml` 已存在且三个键有旧值的场景下分别验证：三个值全部提供新值时三个键全部更新；仅部分键提供值时只有提供的键更新、未提供的键保持旧值；全部不提供时三个键全部保持旧值。
  - 在文件存在但缺 `[langfuse]` 段或缺个别键的场景下，提供的非空值仍写入对应位置。
  - 上述各场景安装完成后检查：文件其余内容与更新前一致；服务加载的 `[langfuse]` 配置与文件内容一致。
  - 文件不存在的首装场景，行为与现状模板一致。

#### Scenario: 重装换凭据

- 现有 `config.toml` 的 `[langfuse]` 是项目 A 的凭据与地址；用户重新执行 `installer.sh install --public-key pk-lf-new --secret-key sk-lf-new --base-url http://new-lf:3000`。安装完成后文件中三个键变为新值，服务向新地址投递；安装输出打印的 `Langfuse:` 地址与文件中 `base_url` 一致。
