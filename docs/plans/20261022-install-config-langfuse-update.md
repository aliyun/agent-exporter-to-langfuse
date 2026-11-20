# 实现计划：安装时更新已存在 config.toml 的 Langfuse 配置

## Header
- source_spec: ../specs/20261022-install-config-langfuse-update.md
- source_requirements_sha256: sha256:4e938ea016adda5669f2d10815880ec7454ee851fd15f3586b259a7d1cbbb747
- accepted_debt: none
- status: ready
- external_review_policy: none

## Requirements Covered
- R-1: 安装时提供的 Langfuse 配置写入已存在的 config.toml 并生效

## Planning Evidence
- 调用链与 owner：`deploy/installer.sh` `cmd_install` 解析 `--public-key` / `--secret-key` / `--base-url` 并导出 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`（未提供时为空字符串），以这些环境变量调用 `exporter/install-langstash.sh`（写 `$INSTALL_DIR/config/config.toml`），安装末尾 `restart_langstash` 重启服务；`cmd_upgrade` 不调用该脚本、不接受 Langfuse 参数，`config/` 跨版本保留。修复的 owner 是 `exporter/install-langstash.sh` 的 "Write config.toml" 已存在分支。
- 可复用实现：`exporter/src/config.py` 的 `set_config_value(section, key, value, path)` 已实现键级合并（替换既有键、向段内插入缺键、追加缺段），语义由 `exporter/tests/test_config.py` 的 `TestSetConfigValue` 覆盖；`load_config` 对 `[langfuse]` 三键的读取由 `TestLoadConfig` 覆盖。`exporter/install-langstash.sh` 第 1 段 `uv sync` 成功后才执行写配置段，此时 `.venv` 已存在且已安装项目本身（pyproject `packages = ["src"]`），可经 `.venv/bin/python` 导入 `src.config`。
- 服务生效链路（既有、不改动）：`exporter/src/main.py` 启动时 `load_config` → `server.py` `/health` 报 `langfuse_configured`；安装流程写配置后重启服务，文件新值经此链路生效。
- E2E 底座：`tests/e2e/test_versioned_upgrade.sh`（真实 HOME，先构建 package.sh tarball 再 `file://` 安装；E2E-1 首装、E2E-5 重装、E2E-6 升级 "config preserved" 断言已存在），`e2e-helpers.sh` 提供 `e2e_case` / `e2e_check` 结构化标记输出；新增重装场景复用该底座与标记输出形式，但断言与命令调用的诊断回显形式遵循 task 约束（现行 `e2e_check` 的 `eval ... 2>/dev/null` 不满足 AGENTS.md 诊断规范，属待改造点）。
- 文档位置：`README.md` 的安装参数表（`## Parameters` 一节）与 Upgrade 章节，是安装行为对用户的说明位置（AGENTS.md 要求安装逻辑变更同步文档）。

## Phases with Tasks

### phase-1: 安装期对已存在 config.toml 的 [langfuse] 键级更新

#### task-1: install-langstash.sh 已存在分支按非空提供值逐键更新并补齐测试与文档
- requirements: [R-1]
- outputs: [exporter/install-langstash.sh, tests/e2e/test_versioned_upgrade.sh, tests/e2e/e2e-helpers.sh, README.md]
- action: 在 `exporter/install-langstash.sh` "Write config.toml" 的已存在分支中，对安装期非空提供的 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` 对应的 `[langfuse]` `public_key` / `secret_key` / `base_url` 三键逐键做键级更新（键已存在则替换、`[langfuse]` 段或键缺失则补齐；提供值为空则跳过、保留既有值）；在 `tests/e2e/test_versioned_upgrade.sh` 新增重装换凭据场景断言更新/保留/补齐/其余内容不变四类边界；在 `README.md` 安装参数说明处补充"安装时提供的 Langfuse 值会更新已存在配置中的对应键，未提供的键保留旧值"。
- constraints:
  - Constraint: 键级更新必须复用 `exporter/src/config.py` 的 `set_config_value`（经 exporter venv 的 python 以显式 `path` 参数调用）；不得在 shell 中另写一套平行的 TOML 文本编辑实现。
  - Constraint: 新增 shell 逻辑必须同时兼容 macOS 与 Linux，不得使用 GNU 独有命令参数（AGENTS.md MUST NOT）。
  - Constraint: 首装分支（`config.toml` 不存在时的模板渲染，含 `base_url` 默认值 `https://us.cloud.langfuse.com`）保持现状不改；`deploy/installer.sh` 与 `cmd_upgrade` 路径不改（upgrade 不经 install-langstash.sh 写配置）。
  - Constraint: 安装输出与日志保持现状（仅打印 `base_url` 与 key 前缀），不得明文输出完整 secret key（AGENTS.md MUST NOT）。
  - Constraint: 新增 E2E 断言与场景内的安装命令调用必须捕获并在失败时回显被检命令的 stderr 与退出码（扩展 `e2e_check` 或使用合规检查形式均可），不得照抄现行 `e2e_check` 的 `eval ... 2>/dev/null` 与既有场景的 `>/dev/null 2>&1 || true` 形式（AGENTS.md 测试诊断规范明令禁止，失败输出不得退化为仅 `FAIL <name>`）。
- verification:
  - planned_test: `tests/e2e/test_versioned_upgrade.sh` 新增重装场景（真实 HOME 底座，复用 e2e_case/e2e_check 的结构化标记输出，断言形式遵循上一条诊断约束）：首装三值提供后 `[langfuse]` 三键为提供值；换新三值重装后三键全部为新值；仅提供 `LANGFUSE_BASE_URL` 重装后仅 `base_url` 更新、`public_key` / `secret_key` 保持旧值；三值均不提供重装后三键保持旧值。
  - planned_test: 同场景文件完整性边界：重装前自播种的其他 section 自定义值（如 `[storage]` 自定义行与注释）在重装后逐行保留；手工删除 `[langfuse]` 段（或缺个别键）后以提供值重装，段/键被补齐且值为提供值。
  - planned_test: 服务重载对照（`/health` 仅反映凭据非空，用信号翻转区分服务是否加载新文件）：首装不提供任何 Langfuse 值（模板写入空 `public_key` / `secret_key`）时 `/health` 为 `no_credentials`（503）；随后以三值重装，断言 `/health` 变为 `langfuse_configured: true`——若服务未重载新文件（仍持空凭据的旧内存配置）该断言失败；文件三键断言与该翻转断言共同覆盖 must 第 4 条（安装完成后服务按更新后 `[langfuse]` 配置生效）。
  - existing_test: `exporter/tests/test_config.py` 的 `TestSetConfigValue`（键级合并：替换既有键、段内插键、追加缺段）与 `TestLoadConfig`（`[langfuse]` 三键读取）覆盖安装脚本调用的同一实现路径，无需新增单测。
  - inspect: `exporter/install-langstash.sh` 已存在分支仅对非空提供值逐键调用键级更新、空值跳过；首装模板分支与 `deploy/installer.sh` / `cmd_upgrade` 无改动。

## Verification
- planned_test: `bash tests/e2e/test_versioned_upgrade.sh` 退出码 0——套件全量通过，含新增重装场景（文件键级断言 + `/health` 翻转对照）与既有 E2E-5 / E2E-6 "config preserved" 断言（安装路径新行为与 upgrade 路径 config 不变量同时成立）。
- existing_test: `bash scripts/run-tests.sh` 全量单元回归通过（含 `exporter/tests/test_config.py` 对 `set_config_value` / `load_config` 的语义回归）。
- shell: `bash deploy/package.sh --output-dir /tmp/pkg-<日期>` 退出码 0，且 tarball 内含修改后的 `exporter/install-langstash.sh`（打包产物与安装逻辑一致，AGENTS.md 打包适配义务）。
