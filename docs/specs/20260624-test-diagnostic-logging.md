# 测试诊断信息规范：AGENTS.md 测试日志保留要求

## Purpose

项目没有 CI 自动化测试，单元测试与 E2E 测试主要由 AI agent 在执行实现计划（plan gate）或排查问题时反复运行。现状下测试代码广泛存在"丢失报错信息"的写法：`exporter/tests/test_sender.py` 有 7 处 `try/except: pass` 吞掉异常、全项目 500+ 处裸 `assert` 无业务语义 msg、E2E 脚本用 `2>/dev/null` 与 `>/dev/null 2>&1 || true` 丢弃 stderr。后果是当被测代码因缺陷抛出非预期异常（如 `AttributeError`）时，`except Exception: pass` 会使其静默通过、测试假绿、bug 被隐藏；当断言失败时，输出仅一行表达式、缺少业务语义，AI agent 无法在无上下文下定位失败原因。本 spec 在 `AGENTS.md` 中确立"测试诊断信息保留"规范（原则 + 具体 DO/DON'T 清单），使未来所有测试代码与现有代码的后续修改都按此规范执行，保证失败输出可直接被 AI agent 消费。

## Non-Goals

- 不修改任何现有反例代码（`test_sender.py` 的 7 处 `try/except: pass`、E2E 脚本的 `2>/dev/null` / `>/dev/null 2>&1 || true` 等）。这些列为后续治理项，由后续单独的修改计划处理，不在本 spec 边界内。
- 不引入 CI 自动化测试要求（`.github/workflows/release.yml` 当前不在 tag 触发时跑测试，该现状不在本 spec 改动范围）。
- 不强制要求所有 `assert` 一律带 `msg`（避免噪声与一刀切）。
- 不改变测试框架选型（pytest / vitest 维持现状）。
- 不新增测试日志专用工具或第三方库。
- 不为测试覆盖率设定指标。

## Decisions

- design_source: `none`（直接且边界清晰的规范修订请求，无需上游设计文档）。
- 规范在 `AGENTS.md` 中新增独立小节 `## 测试诊断信息规范`，与现有 `MUST NOT` / `MUST` / `架构约束` / `Git Tag` 小节并列；不拆散塞进现有 `MUST` 列表，因为内容自成体系（原则 + 多维度具体清单）。
- 适用范围覆盖三类测试代码：Python（pytest）、TypeScript（vitest）、shell E2E 脚本。
- 规范采用双层结构：一条总原则 + 多条具体 DO/DON'T 清单（异常捕获 / 断言上下文 / 子进程 stderr）。

## Requirements

### R-1: 确立测试诊断信息保留原则与适用范围

- context: 当前 `AGENTS.md` 没有任何关于测试日志或失败输出质量的约束，AI agent 编写测试时无统一指引，导致吞异常、丢 stderr、裸 assert 无语义等写法长期积累，而这些测试恰恰由 AI agent 在 plan gate 与排查中反复执行。
- must:
  - `AGENTS.md` 必须声明一条原则：所有测试代码（Python 单测、TypeScript 单测、shell E2E 脚本）在断言失败或异常抛出时，输出必须保留足够让"无额外上下文的 AI agent"直接定位失败原因的诊断信息，至少包含：异常类型与异常消息、与失败断言相关的实际值、（子进程场景下）被调用命令的 stderr 与退出码。
  - 原则须明确其服务对象与场景——主要服务于反复执行测试的 AI agent（plan gate 验证与失败排查），而非仅人类开发者。
- must_not:
  - 不得用"日志要详细""注意保留报错信息"这类不可执行、无法验收的泛泛表述作为唯一规则；原则必须可被具体清单落地。
- verification:
  - `AGENTS.md` 中存在该原则陈述，且其适用范围显式覆盖 Python、TypeScript、shell 三类测试。

### R-2: 预期异常须显式捕获并断言，禁止吞异常

- context: `exporter/tests/test_sender.py` 在多处用 `try: sender._send_batch() except RuntimeError: pass`（含一处 `except Exception: pass`）来表示"预期抛异常"，但 `except Exception: pass` 会让被测代码因缺陷抛出的非预期异常（`AttributeError`、`TypeError` 等）静默通过、测试假绿、bug 被隐藏。规范类同文件 `test_ingestor.py` 已有的正例是 `pytest.raises(<Type>, match="...")`。
- must:
  - 规范必须禁止以 `try/except: pass`（含 `except Exception: pass`、裸 `except: pass`）方式表示"预期异常已抛出"。
  - 规范必须要求预期异常用框架的显式机制捕获并断言：Python 用 `pytest.raises(<ExceptionType>)`，必要时配合 `match=` 断言异常消息、或经 `exc_info` 断言异常属性（如 `status`、`message`）；TypeScript 用 `await expect(...).rejects.toThrow(<message>)`。
- must_not:
  - 不得允许 `except Exception: pass` 使非预期异常静默通过、测试仍判"通过"。
  - 不得用宽泛的 `except Exception` 覆盖本应精确匹配的预期异常类型。
- verification:
  - 规范文本明确列出"禁止 `try/except: pass`"项，并给出 `pytest.raises` / `rejects.toThrow` 的推荐替代写法。

#### Scenario: 非预期异常不得被静默吞掉

- given: 一个被测函数在存在缺陷时会抛 `AttributeError` 而非测试预期的 `RuntimeError`
- when: 测试用 `except Exception: pass` 捕获预期异常
- then: 该非预期 `AttributeError` 被静默吞掉、测试假绿——规范须禁止此模式，要求改用 `pytest.raises(RuntimeError)`，使非预期异常直接暴露为测试失败、AI agent 能看到完整 traceback

### R-3: 关键断言须携带业务语义上下文

- context: 全项目有 500+ 处裸 `assert x == y` 无 msg，其中涉及业务语义或嵌套结构取值的断言（如 `assert data["tokens_today"]["input"] == 1000`）失败时，输出仅一行表达式、缺少业务语义，AI agent 难以在无上下文下判断失败含义。pytest 的 assertion rewrite 能显示变量值，但无法补充业务语义。
- must:
  - 规范必须要求涉及业务语义、嵌套结构取值、多步骤状态/结果的关键断言携带可读诊断信息：Python 用 `assert x == y, "<业务语义>"` 或通过 `exc_info` / `caplog` 补充上下文；TypeScript 用 `expect(x, "<业务语义>").toBe(y)` 或通过 `describe/it` 标题表达语义。
  - 规范必须区分"关键断言"与"简单断言"：纯类型判断、存在性、空值判断等简单断言不强制带 `msg`。
- must_not:
  - 不得要求所有 `assert` 一律带 `msg`（避免噪声、避免为简单断言强加冗余说明）。
- verification:
  - 规范文本明确给出"关键断言须带业务语义 msg"的要求，并显式豁免简单断言。

### R-4: 子进程与 E2E 命令不得丢弃 stderr

- context: E2E 脚本 `tests/e2e/e2e-helpers.sh` 的 `e2e_check` 用 `eval "$cmd" 2>/dev/null` 丢弃 stderr，失败时仅打印 `FAIL <name>`；`tests/e2e/test_versioned_upgrade.sh` 多处用 `>/dev/null 2>&1 || true` 完全静默安装步骤，导致 E2E 失败时 AI agent 拿不到任何诊断输出、必须手动重跑命令才能定位原因。
- must:
  - 规范必须要求测试中调用子进程或外部命令时，不得以 `2>/dev/null` 或 `>/dev/null 2>&1 || true` 静默丢弃输出。
  - 规范必须要求命令失败时回显其 stderr 与退出码作为诊断信息（如捕获 stderr 后随失败标记一并输出）。
  - 规范必须要求 E2E / 集成检查辅助函数在断言失败时输出被测命令的 stderr，而非仅打印 `FAIL <name>`。
- must_not:
  - 不得允许失败命令的 stderr 被完全丢弃后仅输出无诊断价值的 `FAIL <name>` 标记。
- verification:
  - 规范文本明确列出子进程 / E2E 场景下 stderr 与退出码的保留要求。

## Open Questions

（无）
