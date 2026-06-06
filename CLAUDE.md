# CLAUDE.md

## MUST NOT

- 随意修改软件路径和文件名。路径变更会破坏已安装用户的环境，必须有明确的兼容性方案后才可执行。
- 在日志、输出或代码中明文暴露完整的 API key。只允许输出前缀（如前 12 字符）。
- 使用 GNU 独有的命令参数。shell 脚本必须同时兼容 macOS 和 Linux。

## MUST

- install.sh / uninstall.sh 保持幂等。多次执行不能产生副作用（重复写入 profile、重复创建目录等）。
