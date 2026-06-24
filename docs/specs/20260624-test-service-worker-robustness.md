# test-service：Worker 清理与 uv 解析健壮性

## Purpose

修复 test-service 在真实服务环境（launchd / systemd 最小 PATH 环境）下暴露的两个 worker 健壮性问题，避免把已成功的 E2E job 错误标记为失败，并让 worktree 清理与依赖安装真正可靠完成。这两个问题都源自同一类根因：worker 把"善后/环境准备"步骤的失败当作了 job 本身的失败，从而污染了 job 的最终结果语义。

具体故障（见线上日志）：

1. `job e2e-20260624-zmh34m failed unexpectedly: Command ['git', 'worktree', 'remove', ... '--force'] returned non-zero exit status 255`，随后服务退出时 `failed to remove worktree /root/.langstash-tester/worktrees/e2e-20260624-zmh34m`。
2. `uv sync failed: [Errno 2] No such file or directory: 'uv'`。

## Non-Goals

- 不改变 job 的成功/失败判定来源：job 终态仍由测试进程退出码、超时、取消、merge 冲突决定，不由 worktree 清理或 `uv sync` 决定。
- 不引入 `uv` 路径的持久化配置项或新的 Server API 配置面（`e2e.uv_bin` 之类）。uv 二进制路径仅在运行时解析。
- 不重构 worker 的整体执行流程、队列、webhook 或进度解析逻辑。
- 不改变 `uv sync` 失败时的现有语义：当前 `_maybe_uv_sync` 未设 `check=True`，故非零退出码被静默忽略、不中止 job；而 `FileNotFoundError` / `TimeoutExpired` 等异常才走 `except` 记录 warning。本次仅修复"找不到 uv"根因，保留"`uv sync` 失败不中止 job"这一不变量，不改写该异常/非零退出的现有处理语义。
- 不改动 langstash 采集业务、exporter 核心代码、deploy/installer 脚本。

## Decisions

- design_source: `none`（直接的线上故障修复，无上游设计文档）。
- uv 二进制解析为运行时解析：优先 `shutil.which("uv")`，命中则用；未命中时按已知候选路径列表探测第一个可执行文件；全部失败则视为 uv 不可用，跳过 `uv sync` 并记录 warning（保留当前降级行为）。不持久化、不写入配置文件。
- worktree 清理降级链固定为：`git worktree remove --force`（注意 `git worktree remove` 仅接受单一布尔 `--force`，重复传入是幂等的、无分级 force 语义）→ 失败则 `shutil.rmtree(path, ignore_errors=True)` 兜底删除目录（忽略占用/权限类错误，避免中断）→ 然后 `git worktree prune` 清理 bare repo 中悬空的 worktree 注册项（prune 只清理目录已不存在的注册项，故必须在 rmtree 之后）。全程 best-effort，任何阶段失败均记录 warning，不向上抛出。
- uv 候选路径范围（运行时读源）须覆盖 uv 的 common 安装位置，与 `main.py` `cmd_install` 中 `which uv`（main.py:193-195）解析到的目录族一致，至少包含：`~/.local/bin/uv`、`~/.cargo/bin/uv`、`/usr/local/bin/uv`、`/opt/homebrew/bin/uv`。该集合为指引性范围，非持久化配置。

## Requirements

### R-1: Job 终态不受善后清理失败影响

- context: 当前 `_execute_job` 在 `try` 中已把终态（success/failed/conflict/timeout/cancelled）写入 store，随后在 `finally` 中调用 `remove_worktree`；该调用走 `_run_git(..., check=True)`，一旦 `git worktree remove --force` 返回非零（如 exit 255）即抛出 `CalledProcessError`，异常从 `finally` 传播到 `_run_loop` 的 `except Exception`，把一个测试已通过的 job 覆盖写成 `failed`，`output_tail` 变成 git 命令错误信息。
- must:
  - job 的终态以测试进程结果（退出码/超时/取消/merge 冲突）为准，一旦据此写入 store 即为最终值。
  - worktree 清理属于 best-effort 善后操作，其失败不得改写已写入的 job 终态、不得改写 `output_tail` / `exit_code` / `duration_seconds` / `summary`。
  - 清理失败时记录一条包含 job_id 与失败原因的 warning 日志。
- must_not:
  - 清理异常不得传播到 `_run_loop` 的 job 异常处理路径从而把终态覆盖为 `failed`。
  - 不得为了让清理"不报错"而吞掉真实需要的 job 失败信号（job 失败仍由测试结果如实反映）。
- verification:
  - 构造一个测试通过（退出码 0）但 worktree 清理必然失败的 job（例如清理前在 worktree 制造使 `git worktree remove --force` 返回非零的状态），job 最终状态仍为 `success`，`output_tail` 反映测试输出而非 git 清理错误。

### R-2: Worktree 清理最终一定回收目录

- context: `git worktree remove --force` 在 worktree 含 `.venv`、locked、或有进程占用文件时常返回非零（exit 255），导致残留 worktree 目录，并在服务退出时 `cleanup_all_worktrees` 同样无法删除、只能 warning，长期累积磁盘与 bare repo 的 worktree 注册项。`git worktree remove` 仅接受单一 `--force`，无法通过叠加 force 标志增强删除能力，故对这类失败必须用 `shutil.rmtree` + `git worktree prune` 兜底。
- must:
  - 单个 job 完成后，其 worktree 目录最终被回收：目录不再存在，且 bare clone 的 `git worktree list` 中不再列出该 worktree。
  - 单 `--force` 删除失败时，按降级链继续尝试（`shutil.rmtree` 删目录 → `git worktree prune` 清理注册项）直到目录被删除或所有手段用尽。
  - 服务退出清理（`cleanup_all_worktrees`）使用与单 job 相同的 best-effort 降级链。
- must_not:
  - 不得残留孤立 worktree 目录或 bare repo 中悬空的 worktree 注册项（删除目录后需 `git worktree prune` 同步注册状态）。
  - 清理不得阻塞服务退出流程（沿用已有的 best-effort、超时后继续的语义）。
- verification:
  - 在 worktree 中创建 `exporter/.venv`（模拟 uv sync 产物 / 大量未跟踪文件）后结束 job，job 结束后该 worktree 目录不存在，`git -C <bare_repo> worktree list` 不再包含该 job 的路径。

#### Scenario: 清理降级到 rmtree + prune

- given: 一个 job 的 worktree 含 `.venv`，且 `git worktree remove --force` 因文件占用返回非零
- when: job 执行完毕进入清理阶段
- then: 清理流程以 `shutil.rmtree` 兜底删除目录、再 `git worktree prune` 同步注册状态，最终 worktree 目录不存在、`git worktree list` 不含该路径，job 终态不受影响

### R-3: 服务环境中最小 PATH 下仍能定位 uv

- context: 服务经 launchd / systemd 以 `ExecStart={uv_bin} run langstash-tester run` 启动，该运行环境 PATH 极简，通常不含 `~/.local/bin` / `~/.cargo/bin`。worker 内 `subprocess.run(["uv", "sync"], ...)` 依赖 PATH 查找 `uv`，于是抛 `FileNotFoundError: [Errno 2] No such file or directory: 'uv'`，被 `except Exception` 捕获后仅留一条 warning，但其后依赖该 venv 的测试会因依赖缺失而失败，根因被掩盖。
- must:
  - worker 在调用 `uv sync` 前先解析 `uv` 的可执行路径：优先 `shutil.which("uv")`，未命中时探测一组已知候选路径并取第一个存在的可执行文件，最终用解析得到的绝对路径调用 `uv sync`。
  - 当 `uv` 在 PATH 与所有候选路径均不存在时，跳过 `uv sync`、记录 warning 并继续 job（保留当前降级行为），不得因 uv 缺失而使 job 进程崩溃或卡死。
- must_not:
  - 不得新增持久化的 uv 路径配置项或新的 Server API 配置面（uv 解析仅在运行时）。
  - 不得在解析到 uv 后仍使用裸 `"uv"` 依赖 PATH 查找。
  - uv 缺失不得静默——必须留下可观测的 warning 日志。
- verification:
  - 在一个 PATH 不含 uv、但 uv 存在于某已知候选路径的环境中，job 能用解析到的绝对 uv 路径成功执行 `uv sync`，不再出现 `[Errno 2] No such file or directory: 'uv'`。
  - 在 uv 完全不可用的环境中，job 不崩溃、记录 warning 并按现有语义继续。

## Open Questions

（无）
