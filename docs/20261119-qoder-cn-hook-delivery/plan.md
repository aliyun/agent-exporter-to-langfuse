# 实现计划：qoder-cn Hook 投递（Langfuse 观测）

## Header
- source_spec: spec.md
- source_requirements_sha256: sha256:a2c1b24449ff16846e9d765142fa0c6b34e5e9e15dde0f6f6aef3c021a31766c
- accepted_debt: none
- status: ready
- external_review_policy: none

## Requirements Covered
- R-1: qoder-cn 会话数据投递到 Langfuse 并包含 token/model 数据
- R-2: 幂等安装 qoder-cn hook 并注册到 settings.json
- R-3: 卸载 qoder-cn hook 并完整清理其注册与状态
- R-4: 统一安装器检测 qoder-cn 并随发布包分发
- R-5: 同步两层 README 文档

## Planning Evidence
- fork 基线为 `hooks/qoder/`（install.sh / uninstall.sh / hooks/langfuse_hook.py / hooks/langfuse-entrypoint.sh / hooks/pyproject.toml）；`hooks/qoderwork/` 证明了同模式 fork 仅需路径/命名/entrypoint 适配，但其 VM 双路径 entrypoint 与 env 硬链接是 QoderWork 特有机制，不引入 qoder-cn。
- `hooks/qoder/hooks/langfuse_hook.py` 的产品特定锚点：`_find_qoder_db()`（L32 起，候选含国际版 macOS App Support 与 `~/.qoder/shared_client/...` 路径）、`STATE_DIR = Path.home() / ".qoder" / "state"`（L125）、`tags_raw = _opt("LANGFUSE_TAGS") or "qoder"`（L950）、logger 名 `qoder_langfuse_hook`；其余逻辑（payload 解析、增量读取、turn 构建、`query_session_tokens`/`match_db_token`、OTLP 构建、`langstash_deliver.deliver_trace` 投递）产品无关。
- 行为级测试先例为 `hooks/claude-code/hooks/tests/`：pytest 直测 `build_otlp_json(...)` 在 OTLP JSON 输出边界断言（`test_trace_v2.py`），conftest 以 `sys.path` 注入 hook 目录与 `langstash-deliver/python`；`pyproject.toml` 用 `[project.optional-dependencies] dev` + `[dependency-groups]` 声明 pytest（生产依赖保持 `opentelemetry-sdk` 不变）。qoder hook 自身无测试。
- `deploy/installer.sh` 的 `install_detected_hooks <ver_dir>` 按目录发现并对检测到的产品执行 `install.sh --upgrade -y`，与 langstash 守护安装（`cmd_install`）解耦——以 sandbox HOME 直接驱动该函数可在不触碰 launchd/本机服务的前提下验证 R-4 的检测与派发行为；`bash -x` trace 断言是既有先例（`tests/e2e/test_installer_version_resolution.sh`）。
- `deploy/package.sh` 以 `git ls-files` 打包，未跟踪文件不会进入发布产物——发布包完整性验证需先 `git add hooks/qoder-cn/`（staging 允许；commit 依 AGENTS.md 需用户确认，计划自身不 commit）。
- `tests/e2e/test_pi_hook_install.sh` 是安装/卸载 E2E 先例：sandbox `HOME`（mktemp）、参数凭据非交互安装、幂等二次安装、secret 前缀（前 12 字符）不泄露断言、`--purge` 语义断言；qoder install.sh 接受 `--secret-key/--public-key/--base-url/--user-id/--tags/--upgrade` 参数与环境变量，`-y` 落入 `*) shift` 被忽略，已装 hook 时 `Overwrite? [y/N]` 提示经 stdin 驱动。
- `scripts/run-tests.sh` 是统一测试入口，但不在 spec R-1 must_not 允许修改的既有文件清单内（仅 `deploy/installer.sh` 与根 `README.md`）——qoder-cn 测试以直接调用方式执行（与 qoder 现状一致），统一入口集成留待后续独立变更。
- 投递链分层依据：qoder-cn hook 的新增可观测贡献止于"正确构建的 trace（含 tag/turns/token/model）到达 `langstash_deliver.deliver_trace` 投递边界"；三层投递与回退（langstash → 直推 → 失败日志）是共享的 `langstash-deliver` 既有已测行为（`hooks/langstash-deliver/python` pytest 套件）。真实 qoder-cn 产品触发的端到端确认作为补充性 manual_audit（spec R-1 verification 第 1 项的真实环境观察）。

## Phases with Tasks

### phase-1: qoder-cn hook 运行时与行为级验证
- gate: shell: 在 `hooks/qoder-cn/hooks` 下执行 `uv run pytest -q` 全部用例通过（覆盖 R-1 六个观察点：默认投递、tag 覆盖、回退执行、跨 DB 判别、DB 缺失降级、增量投递）

#### task-1: fork 并适配 qoder-cn 运行时文件
- requirements: [R-1]
- outputs: [hooks/qoder-cn/hooks/langfuse_hook.py, hooks/qoder-cn/hooks/langfuse-entrypoint.sh, hooks/qoder-cn/hooks/pyproject.toml]
- action: 以 `hooks/qoder/` 为基线逐文件 fork 出 `hooks/qoder-cn/hooks/` 三件套并完成产品适配：`langfuse_hook.py` 的 `_find_qoder_db()` 候选仅指向 qoder-cn 自身 SharedClientCache 路径（`~/.qoder-cn/shared_client/cache/db/local.db` 与 qoder-cn 的 macOS App Support `com.qodercn.app.stable` 对应路径），`STATE_DIR` 改为 `~/.qoder-cn/state`，默认 tag 改为 `qoder-cn`，logger 名改为 qoder-cn 自标识；`langfuse-entrypoint.sh` 采用 qoder 版形态（加载 `~/.agent-exporter-to-langfuse/config/qoder-cn.env` 后 `exec uv run python langfuse_hook.py`）；`pyproject.toml` 命名 `qoder-cn-langfuse-hook`，生产依赖与 qoder 一致，pytest 按 claude-code 先例入 dev 组。其余逻辑保持与 qoder 一致。
- constraints:
  - Constraint: `_find_qoder_db()` 候选列表不得包含任何国际版路径（`~/Library/Application Support/Qoder/...`、`~/.local/share/Qoder/...`、`~/.qoder/...`）——同机共存时读取错误产品的 session 数据直接违反 R-1 must_not。
  - Constraint: entrypoint 不得引入 qoderwork 的 VM 分支、`.venv-linux` 与 env 硬链接机制（QoderWork 特有，超出 qoder 基线）。
- verification:
  - inspect: `hooks/qoder-cn/hooks/` 三文件存在，`langfuse_hook.py` 中 `STATE_DIR` 指向 `~/.qoder-cn/state`、默认 tag 为 `qoder-cn`、logger 名含 qoder-cn；`langfuse-entrypoint.sh` 加载 `qoder-cn.env` 且无 `VENV_DIR`/`langfuse.env` 硬链接残留。
  - source_scan: 命令 `grep -n "Application Support/Qoder\|\.local/share/Qoder\|home / \"\.qoder\"" hooks/qoder-cn/hooks/langfuse_hook.py` 零命中（国际版路径禁止，无例外）。
  - build: 在 `hooks/qoder-cn/hooks` 下 `uv sync` 成功完成依赖安装。
  - shell: `python3 -c "import tomllib;d=tomllib.load(open('hooks/qoder-cn/hooks/pyproject.toml','rb'));assert d['project']['name']=='qoder-cn-langfuse-hook';assert d['project']['dependencies']==['opentelemetry-sdk>=1.20']"` 退出码 0（无例外）。

#### task-2: qoder-cn hook 行为级 pytest 套件
- requirements: [R-1]
- outputs: [hooks/qoder-cn/hooks/tests/]
- action: 在 `hooks/qoder-cn/hooks/tests/` 建 pytest 套件（conftest 按 claude-code 先例注入 hook 目录与 `langstash-deliver/python` 到 `sys.path`），以 fixture 驱动 hook 行为并在 OTLP 构建/投递边界断言，覆盖 R-1 全部 verification 观察点：①合成 Stop payload（transcript jsonl fixture，行格式按已验证的 qoder-cn transcript 同构布局）经 hook 处理产出包含 turns/generations/tool calls 与 DB token/model 的 trace；②`LANGFUSE_TAGS` 设额外标签时 tag 集合按 qoder 既有语义（默认 `qoder-cn` 与用户标签并存）；③langstash 不可用（disable/回退路径）时投递按回退语义执行；④跨 DB 判别：sandbox HOME 中同时构造国际版路径 DB 与 `~/.qoder-cn` DB（token/model 值不同），trace 数据须对应 qoder-cn DB；⑤DB 缺失时 trace 正常产出但缺 token/model；⑥同一会话多次触发仅投递新增内容（状态文件位于 `~/.qoder-cn/state/`）。
- constraints:
  - Constraint: 测试不得修改 `hooks/qoder/` 或其他既有产品目录的任何文件；fixture 仅置于 `hooks/qoder-cn/hooks/tests/` 内。
- verification:
  - planned_test: pytest 套件（`hooks/qoder-cn/hooks/tests/`，seam 为 `langfuse_hook` 的 payload→turn→OTLP 构建与 `deliver_trace` 投递调用边界，fixture 为合成 transcript jsonl + 临时 sqlite `chat_message` DB + 临时 HOME/状态目录）：R-1 六个观察点各有对应用例且断言区分违规实现（如硬编码 tag、读国际版 DB、全量重投递、DB 缺失时崩溃）。

### phase-2: 安装与卸载生命周期及 E2E
- gate: shell: `bash tests/e2e/test_qoder_cn_hook_install.sh` 退出码 0，全部 case 通过（安装工件、注册、env、幂等、覆盖确认、卸载清理、`--purge`、第三方注册保留、跨产品不变、secret 前缀）

#### task-3: fork 并适配 install.sh / uninstall.sh
- requirements: [R-2, R-3]
- outputs: [hooks/qoder-cn/install.sh, hooks/qoder-cn/uninstall.sh]
- action: 以 qoder 版 install.sh / uninstall.sh 为基线 fork 并适配：HOOK_DIR `~/.qoder-cn/hooks/langfuse`、SETTINGS_FILE `~/.qoder-cn/settings.json`、env 文件 `~/.agent-exporter-to-langfuse/config/qoder-cn.env`、默认 tag 追加 `qoder-cn`、状态清理目录 `~/.qoder-cn/state`、完成提示产品名；凭据收集（交互/参数/环境变量/`--upgrade` 复用）、`langstash_deliver` 拷贝、`uv sync`、settings.json 注册（`Stop` 与 `SubagentStop`、幂等跳过、命令 `~/.qoder-cn/hooks/langfuse/langfuse-entrypoint.sh`）与卸载清理（hook 目录、langfuse 注册项、状态文件、env 默认保留/`--purge` 删除、空 profile 目录与 shell loader 行清理）全部与 qoder 既有行为等价。
- constraints:
  - Constraint: 不得改变 qoder 基线的交互语义（`Overwrite? [y/N]` 确认、凭据提示默认值、`--upgrade` 复用 env 凭据），仅产品路径/命名/提示文案适配为 qoder-cn。
- verification:
  - inspect: `hooks/qoder-cn/install.sh` 与 `uninstall.sh` 中 HOOK_DIR/SETTINGS_FILE/ENV_FILE/STATE 清理路径均为 `~/.qoder-cn` 系，注册事件为 `Stop` 与 `SubagentStop`，卸载移除逻辑按命令含 `langfuse` 过滤且保留其他注册项。
  - source_scan: 命令 `grep -n "\.qoder/\|\.qoderwork/" hooks/qoder-cn/install.sh hooks/qoder-cn/uninstall.sh` 零命中（不得指向其他产品目录，无例外；`\.qoder-cn` 不匹配该模式）。

#### task-4: 安装/卸载 E2E 套件
- requirements: [R-2, R-3]
- outputs: [tests/e2e/test_qoder_cn_hook_install.sh]
- action: 新建 E2E 脚本（sandbox HOME 模式，参照 `test_pi_hook_install.sh`）：参数凭据非交互安装→断言 `~/.qoder-cn/hooks/langfuse/` 四类工件就位、settings.json 的 `Stop`/`SubagentStop` 各一条指向 entrypoint 的注册、env 文件含 `LANGFUSE_*`+`LANGSTASH_*` 且 tag 含 `qoder-cn`、安装输出仅含 secret 前 12 字符；重复安装→注册不重复、确认覆盖后文件更新且不残留旧版；已装状态下安装→`Overwrite` 确认行为与 qoder 一致（拒绝跳过拷贝/确认后更新）；预置 qoder/qoderwork sentinel（`~/.qoder/settings.json` 注册项、`~/.qoder/hooks/langfuse/` sentinel 文件、`config/qoder.env`）→安装前后不变；卸载→hook 目录、langfuse 注册项、`~/.qoder-cn/state` 清理且第三方注册保留、env 默认保留；`--purge`→env 删除且 config 目录空时目录与 shell loader 行清理；未安装状态卸载→无报错无副作用；卸载后重装→功能完整恢复。失败时回显被测命令 stderr 与退出码（AGENTS.md 诊断规范）。
- constraints:
  - Constraint: 全程 sandbox `HOME`（mktemp），凭据用占位符（`sk-lf-e2e-*`/`pk-lf-e2e-*`）与不可达 base URL，不得连接真实 Langfuse、不得触碰本机真实 `~/.qoder-cn` 与 launchd 服务。
- verification:
  - planned_test: `tests/e2e/test_qoder_cn_hook_install.sh`（seam 为 install.sh/uninstall.sh 在 sandbox HOME 的全流程执行）：R-2 四个观察点与 R-3 三个观察点各有 case 覆盖并区分违规（如静默覆盖无确认、注册重复、第三方注册被误删、`--purge` 语义缺失）。

### phase-3: 统一安装器集成、发布包与文档
- gate: shell: `bash tests/e2e/test_qoder_cn_installer_detection.sh` 退出码 0（存在 `~/.qoder-cn` → qoder-cn hook 安装且注册生效、第三方 sentinel 不变；不存在 → 跳过 qoder-cn 且其他产品行为不变），且发布包完整性检查（tarball 内 `hooks/qoder-cn/` 四类内容齐备 + `hooks/langstash-deliver/python/langstash_deliver` 拷贝源存在）通过

#### task-5: installer 检测分支与统一安装检测 E2E
- requirements: [R-4]
- outputs: [deploy/installer.sh, tests/e2e/test_qoder_cn_installer_detection.sh]
- action: 在 `deploy/installer.sh` 的 `is_agent_installed()` case 中新增 `qoder-cn)` 分支（判定 `[ -d "$HOME/.qoder-cn" ]`，不依赖命令探测），不改动其他分支；新建检测 E2E 脚本：sandbox HOME 下经 `install_detected_hooks`（函数级提取或 `bash -x` trace 断言，先例 `test_installer_version_resolution.sh`）验证双向检测行为——存在 `~/.qoder-cn`（含预置 settings.json）时执行 `hooks/qoder-cn/install.sh --upgrade -y` 且 hook 注册生效、预置的 qoder/qoderwork sentinel 不变；不存在该目录时输出跳过 qoder-cn 且其余产品检测分支行为不变。
- constraints:
  - Constraint: 只新增 `qoder-cn)` 分支，不修改其他 case 分支与统一安装流程（R-4 must_not）。
  - Constraint: 检测 E2E 不得触发 langstash 守护安装/launchd 操作（只驱动 `install_detected_hooks` 检测派发路径，不动 `cmd_install`）。
- verification:
  - inspect: `deploy/installer.sh` 的 `is_agent_installed()` 含 `qoder-cn)` 分支且其余分支与 main 基线一致（`git diff main -- deploy/installer.sh` 仅新增该分支）。
  - planned_test: `tests/e2e/test_qoder_cn_installer_detection.sh`（seam 为 installer 的检测派发函数）：存在/不存在 `~/.qoder-cn` 双向 case + 跨产品 sentinel 不变断言，区分“总是安装”或“从不安装”的违规实现。

#### task-6: 两层 README 文档同步
- requirements: [R-5]
- outputs: [README.md, hooks/qoder-cn/README.md]
- action: 根 `README.md` 的 Supported Agents 表新增 Qoder CN 行（指向 `hooks/qoder-cn/`，列结构与既有 Qoder/QoderWork 行一致）并在架构 banner 的 agent 列表加入 qoder-cn；新建 `hooks/qoder-cn/README.md` 按 qoder 产品 README 结构说明安装步骤、配置变量表、产品形态（国内版 Desktop、配置目录 `~/.qoder-cn/`、无 CLI 形态）与 Desktop 已知限制（subagent 不可用、token/model 来自 SharedClientCache DB）。
- constraints:
  - Constraint: 根 README 仅新增 Qoder CN 行与 banner 追加，不改动既有 agent 行语义（R-5 must_not 由变更面检查闭合）。
  - Constraint: 产品 README 中的安装路径、注册事件、默认 tag、配置目录描述须与 R-1/R-2 实现一致（AGENTS.md 文档同步 MUST）。
- verification:
  - inspect: 根 README 的表新增行含 `hooks/qoder-cn/` 链接且 banner 含 qoder-cn；`hooks/qoder-cn/README.md` 含安装、配置变量、产品形态与已知限制段落。
  - source_scan: 命令 `grep -n "hooks/qoder-cn" README.md` 至少 2 处命中（表行 + banner 或安装指引，无例外）。

#### task-7: 发布包完整性验证
- requirements: [R-4]
- depends_on: [task-6]
- outputs: [tests/e2e/test_qoder_cn_installer_detection.sh]
- action: 在检测 E2E 中加入发布包完整性 case：`git add hooks/qoder-cn/`（staging，不 commit）后执行 `deploy/package.sh --output-dir`（临时目录）构建 tarball，解包断言：①`hooks/qoder-cn/` 下完整包含 install.sh、uninstall.sh、README.md 与 `hooks/` 内的 langfuse_hook.py、langfuse-entrypoint.sh、pyproject.toml、tests/；②同级 `hooks/langstash-deliver/python/langstash_deliver` 存在于 tarball（它是 install.sh 的拷贝源，install 期才复制进用户 HOOK_DIR，本身不属于产品 hooks 目录）。
- constraints:
  - Constraint: 只允许 `git add`（staging）；`git commit` 依 AGENTS.md 须用户确认，本任务不得执行。
- verification:
  - shell: 构建发布包并检查 tarball 内上述清单完整（四类内容 + 拷贝源），失败时回显 package.sh stderr 与退出码。

## Verification
- shell: `git diff --name-only main` 输出仅包含：`hooks/qoder-cn/` 下新增文件、`tests/e2e/test_qoder_cn_hook_install.sh`、`tests/e2e/test_qoder_cn_installer_detection.sh`、`deploy/installer.sh`、`README.md`，不出现其他既有文件（R-1 must_not 变更面闭合；无例外）。
- shell: 组合运行三套验证全部通过：`cd hooks/qoder-cn/hooks && uv run pytest -q`、`bash tests/e2e/test_qoder_cn_hook_install.sh`、`bash tests/e2e/test_qoder_cn_installer_detection.sh`（计划级组合存活边界，区别于各 phase gate 的单套结果）。
- manual_audit: 补充性真机确认（非闭合必需）：在装有真实 qoder-cn 的环境完成一轮对话并停止，确认 Langfuse 收到该会话 trace（tag 含 `qoder-cn`、token/model 来自 qoder-cn DB）——对应 spec R-1 verification 第 1 项的真实环境观察，供用户验收时执行。
