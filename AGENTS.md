# AGENTS.md

## MUST NOT

- 随意修改软件路径和文件名。路径变更会破坏已安装用户的环境，必须有明确的兼容性方案后才可执行。
- 在日志、输出或代码中明文暴露完整的 API key。只允许输出前缀（如前 12 字符）。
- 使用 GNU 独有的命令参数。shell 脚本必须同时兼容 macOS 和 Linux。
- 自行执行 git commit / git push。提交代码前必须先询问用户确认，得到明确同意后才可执行。

## MUST

- install.sh / uninstall.sh 保持幂等。多次执行不能产生副作用（重复写入 profile、重复创建目录等）。

## Git Tag 与版本号规范

- Tag 格式：`v<major>.<minor>.<patch>`，例如 `v1.0.0`、`v2.3.4`。
- Pre-release 用连字符 `-` 分隔：`v<major>.<minor>.<patch>-<pre-release>`，例如 `v0.2.0-alpha`、`v5.9.0-beta.3`。
- 禁止使用 `.alpha`、`.beta` 等点号分隔的 pre-release 格式（如 ~~v0.1.0.alpha.1~~）。
- VERSION 文件内容不带 `v` 前缀，例如 `0.1.0-alpha.2`。
