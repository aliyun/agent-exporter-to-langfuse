# AGENTS.md

## MUST NOT

- 随意修改软件路径和文件名。路径变更会破坏已安装用户的环境，必须有明确的兼容性方案后才可执行。
- 在日志、输出或代码中明文暴露完整的 API key。只允许输出前缀（如前 12 字符）。
- 使用 GNU 独有的命令参数。shell 脚本必须同时兼容 macOS 和 Linux。
- 自行执行 git commit / git push。提交代码前必须先询问用户确认，得到明确同意后才可执行。
- 将任何敏感信息提交到代码仓库。包括但不限于：用户名/密码、API Key（AK/SK）、Auth Token、私钥、证书、数据库连接串中的凭据、`.env` 文件中的真实密钥值。配置文件和文档中只允许使用占位符（如 `sk-xxx`、`<your-api-key>`）或环境变量引用（如 `$LANGFUSE_SECRET_KEY`），不得包含可直接使用的真实凭据。

## MUST

- install.sh / uninstall.sh 保持幂等。多次执行不能产生副作用（重复写入 profile、重复创建目录等）。
- 生成方案（设计文档、实现计划、规范等）时，文件名必须携带日期前缀，格式为 `YYYYMMDD`，例如 `20260614-feature-design.md`。
- 开发过程中形成的新规则（约束、规范、架构决策等），必须提醒用户将其同步写入 `CLAUDE.md` 和 `AGENTS.md`。
- 如果更新了部署、安装、运行逻辑，必须同步修改 `README.md` 等用户指导文件，保持文档与实际行为一致。
- 修改代码逻辑后，必须通过相关的单元测试和 E2E 测试。如果现有测试不覆盖改动，需要补充测试。
- 如果代码结构发生变化（新增/移除/重命名模块、包、文件），必须检查并适配 `deploy/package.sh`、`deploy/installer.sh`、各 hook 的 `install.sh` / `uninstall.sh`、`pyproject.toml` / `package.json` 等打包和安装脚本，确保构建产物、文件拷贝路径、依赖声明与新结构一致。
- Hooks 会被拷贝到各 agent 的工作目录下（如 `~/.codex/hooks/langfuse/`、`~/.config/opencode/plugins/`）。hooks 依赖的共享包（如 `langstash-deliver`）必须通过 `install.sh` 一并拷贝到对应 agent 目录，或在打包构建时内联到 bundle 产物中（如 codex 的 `tsdown` 已内联）。

## 架构约束

- 运行时配置的读写必须通过 Server API（`GET/POST /settings`）作为唯一入口。UI 客户端（WebUI、Menubar）不得直接调用 `set_config_value` 或持有 `updater`/`config` 等内部对象引用。
- 本项目的唯一后端是 Langfuse。数据模型、传输协议、字段设计只需满足 Langfuse 的接口和分析需求，不强制要求与 OpenTelemetry GenAI Semantic Conventions 完全对齐。在 Langfuse 已有等价能力的场景下，优先使用 Langfuse 原生概念（如 trace/generation/span、`langfuse.*` 属性），而非引入 OTel GenAI 标准字段。

## Git Tag 与版本号规范

- Tag 格式：`v<major>.<minor>.<patch>`，例如 `v1.0.0`、`v2.3.4`。
- Pre-release 用连字符 `-` 分隔：`v<major>.<minor>.<patch>-<pre-release>`，例如 `v0.2.0-alpha`、`v5.9.0-beta.3`。
- 禁止使用 `.alpha`、`.beta` 等点号分隔的 pre-release 格式（如 ~~v0.1.0.alpha.1~~）。
- VERSION 文件内容不带 `v` 前缀，例如 `0.1.0-alpha.2`。
