## Header
- source_spec: ../specs/20260623-e2e-opencode-langfuse-delivery.md
- risk: normal
- runtime_profile: normal/general
- runtime_profile_basis: cross-module E2E test addition with Docker orchestration and multi-module verification; no production code modification, no shared truth-source replacement, no migration, no public API change
- accepted_debt: none
- status: ready
- external_review_policy: none

## Requirements Covered
- R-1: E2E script framework compatible with test-service orchestration and cross-platform execution
- R-2: langstash health check as install verification supplement
- R-3: synthetic OTLP trace delivery to Docker Langfuse end-to-end verification
- R-4: OpenCode hook install/uninstall file and configuration integrity verification
- R-5: real OpenCode conversation data delivery verification (fully automated via opencode run)

## Planning Evidence

- surfaces: new file tests/e2e/test_opencode_langfuse_delivery.sh; reuses e2e-helpers.sh (sourced, not modified); invokes deploy/installer.sh, hooks/opencode/install.sh, hooks/opencode/uninstall.sh (not modified)
- consumers: test-service ProgressParser reads ##e2e## markers from stdout; CI/manual runs execute the script directly
- coupling: Module 4 reuses Module 2 Docker Langfuse setup and Module 3 hook install patterns within the same script; no cross-module runtime dependency (each module is a self-contained suite section)
- rejection_feedback incorporated: Module 4 is fully automated using `opencode run` (not manual-only baseline); Docker Langfuse uses `LANGFUSE_INIT_*` env vars to auto-seed org, project, user, and API keys

## Phases with Tasks

### phase-1: script framework and Module 1 (langstash health check)
- commit_boundary: task
- worker_dispatch: per-task

#### task-1: create script skeleton with framework and Module 1
- requirements: [R-1, R-2]
- outputs: [tests/e2e/test_opencode_langfuse_delivery.sh]
- action: Create E2E test script with --module selector, e2e-helpers sourcing, shared helpers including Docker Langfuse setup with LANGFUSE_INIT_* seeding, and Module 1 suite verifying langstash /health returns 200 and status "healthy" after install within 60s.
- constraints:
  - Source e2e-helpers.sh; do not modify it
  - Script must not use GNU-only parameters (compatible with macOS and Linux)
  - Module 1 is an independent E2E suite with its own suite name and case count
  - Suite cleanup must call purge_install regardless of pass/fail; suite summary must annotate manual-only module count when any module falls back
  - Docker Langfuse helpers use LANGFUSE_INIT_* env vars (LANGFUSE_INIT_ORG_ID, LANGFUSE_INIT_PROJECT_ID, LANGFUSE_INIT_PROJECT_PUBLIC_KEY, LANGFUSE_INIT_PROJECT_SECRET_KEY, LANGFUSE_INIT_USER_EMAIL, LANGFUSE_INIT_USER_PASSWORD) to auto-seed org/project/user/API keys; no manual key creation needed
- verification:
  - inspect: tests/e2e/test_opencode_langfuse_delivery.sh contains ##e2e## suite marker, --module parameter handler, wait_langstash_health function, Module 1 health-check suite section
  - shell: bash tests/e2e/test_opencode_langfuse_delivery.sh --module 1 outputs ##e2e## markers and Module 1 suite results

### phase-2: Module 2 (synthetic OTLP trace delivery to Docker Langfuse)
- commit_boundary: task
- worker_dispatch: per-task

#### task-2: implement Module 2 synthetic trace delivery
- requirements: [R-1, R-3]
- outputs: [tests/e2e/test_opencode_langfuse_delivery.sh]
- action: Add Module 2 suite that sends a minimal valid OTLP JSON trace (root span e2e-synthetic-test, generation span model e2e-model) through langstash /ingest, polls /stats for total_sent >= 1, then verifies the trace exists in Docker Langfuse API with matching attributes.
- constraints:
  - OTLP JSON must pass validate_otlp: resourceSpans[].scopeSpans[].spans[] with traceId (32-char hex), spanId (16-char hex), name (non-empty), startTimeUnixNano (string); must include at least one root span
  - API key obtained from LANGFUSE_INIT_* env vars (LANGFUSE_INIT_PROJECT_PUBLIC_KEY, LANGFUSE_INIT_PROJECT_SECRET_KEY); log output shows only key prefix (first 12 chars)
  - Docker container must be stopped and removed at suite cleanup regardless of pass/fail; /ingest non-202 and /stats timeout must produce fail marker with diagnostic output
  - Module 2 is an independent E2E suite; failure does not affect Module 1
- verification:
  - inspect: tests/e2e/test_opencode_langfuse_delivery.sh contains Module 2 suite section with Docker Langfuse setup, OTLP JSON construction, /ingest POST, /stats poll, Langfuse API trace query, and cleanup
  - shell: bash tests/e2e/test_opencode_langfuse_delivery.sh --module 2 outputs ##e2e## markers and Module 2 suite results (requires Docker)

### phase-3: Module 3 (OpenCode hook install/uninstall integrity)
- commit_boundary: task
- worker_dispatch: per-task

#### task-3: implement Module 3 hook install/uninstall integrity verification
- requirements: [R-1, R-4]
- outputs: [tests/e2e/test_opencode_langfuse_delivery.sh]
- action: Add Module 3 suite that verifies 5 file/config items exist after opencode hook install (with placeholder keys), then verifies the same 5 items are fully removed after hook uninstall, including logs directory.
- constraints:
  - Use placeholder API keys sk-e2e-test and pk-e2e-test (not real keys); use non-existent base URL to avoid real Langfuse connections
  - Module 3 is an independent E2E suite; failure does not affect Module 1 or Module 2
  - Suite cleanup must call purge_install regardless of pass/fail
  - Install verification must check: plugin file, langstash-deliver, opencode.json plugin entry, opencode.env with all 5 vars (LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_BASE_URL, LANGSTASH_ENABLED, LANGSTASH_URL)
  - Uninstall verification must check removal of: plugin file, langstash-deliver directory, opencode.json plugin entry, opencode.env, logs directory
- verification:
  - inspect: tests/e2e/test_opencode_langfuse_delivery.sh contains Module 3 suite with install verification (5 existence checks) and uninstall verification (5 absence checks)
  - shell: bash tests/e2e/test_opencode_langfuse_delivery.sh --module 3 outputs ##e2e## markers and Module 3 suite results

### phase-4: Module 4 (real OpenCode conversation delivery, fully automated)
- commit_boundary: task
- worker_dispatch: per-task

#### task-4: implement Module 4 real OpenCode conversation delivery
- requirements: [R-1, R-5]
- outputs: [tests/e2e/test_opencode_langfuse_delivery.sh]
- action: Add Module 4 suite that runs opencode run with a test prompt for non-interactive conversation and verifies trace delivery to Docker Langfuse (name contains OpenCode, non-empty model, input text). Falls back to manual-only if opencode is not available or opencode run fails (non-zero exit, timeout, no trace produced).
- constraints:
  - Module 4 is fully automated via opencode run; manual-only fallback is for binary-absence or runtime failure cases only
  - If opencode binary not found in PATH or opencode run exits non-zero or times out (120s) or produces no trace, Module 4 falls back to manual-only with ##e2e## pass marker and _manual_only case suffix; manual-only is expected baseline behavior, not a failure
  - Module 4 failure does not affect Module 1-3 suite summary results
  - API key from LANGFUSE_INIT_*; only prefix shown in logs
  - Docker container, hook, and agent-exporter-to-langfuse must all be cleaned up regardless of pass/fail
- verification:
  - inspect: tests/e2e/test_opencode_langfuse_delivery.sh contains Module 4 suite with Docker setup, hook install, opencode run invocation, Langfuse trace query, cleanup, and manual-only fallback when opencode not installed
  - shell: bash tests/e2e/test_opencode_langfuse_delivery.sh --module 4 outputs ##e2e## markers and Module 4 suite results (requires Docker + opencode CLI)

## Verification

- existing_test: bash tests/e2e/test_opencode_langfuse_delivery.sh --module 1 — Module 1 suite passes with health check marker output
- existing_test: bash tests/e2e/test_opencode_langfuse_delivery.sh --module 2 — Module 2 suite passes with trace delivery verified via Langfuse API (requires Docker)
- existing_test: bash tests/e2e/test_opencode_langfuse_delivery.sh --module 3 — Module 3 suite passes with install/uninstall integrity checks
- existing_test: bash tests/e2e/test_opencode_langfuse_delivery.sh --module 4 — Module 4 suite passes with real delivery or manual-only fallback (requires Docker + opencode CLI)
- existing_test: bash tests/e2e/test_opencode_langfuse_delivery.sh — all modules run in sequence; Module 4 failure does not affect Module 1-3 summary
- source_scan: rg ##e2e## tests/e2e/test_opencode_langfuse_delivery.sh scoped to marker output lines; allowed exceptions: none
- source_scan: rg LANGFUSE_INIT_ tests/e2e/test_opencode_langfuse_delivery.sh scoped to Docker Langfuse setup functions; allowed exceptions: none
- source_scan: rg GNU tests/e2e/test_opencode_langfuse_delivery.sh scoped to command invocations; allowed exceptions: none
- source_scan: rg purge_install tests/e2e/test_opencode_langfuse_delivery.sh scoped to module cleanup sections; allowed exceptions: none
- source_scan: rg docker tests/e2e/test_opencode_langfuse_delivery.sh scoped to container stop/rm cleanup commands; allowed exceptions: none
- source_scan: rg 'sk-lf-|pk-lf-' tests/e2e/test_opencode_langfuse_delivery.sh scoped to log/print statements; allowed exceptions: prefix truncation pattern (first 12 chars only)
