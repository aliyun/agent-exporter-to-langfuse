# Sender Payload Splitting — Implementation Plan

## Header
- source_spec: ../specs/20260617-sender-payload-splitting.md
- risk: high-risk
- runtime_profile: high-risk
- runtime_profile_basis: commit_id write-path semantics change, I/O boundary error handling (413), data integrity (partial success / failed recovery)
- accepted_debt: none
- status: ready
- external_review_policy: auto

## Requirements Covered
- R-1: Trace 级别累积发送
- R-2: 单条超大 trace 的 item 级别拆分
- R-3: HTTP 413 不再无限重试
- R-4: max_payload_bytes 配置项

## Planning Evidence

### surfaces
- `exporter/src/config.py`: `SenderConfig` dataclass, `load_config`
- `exporter/src/sender.py`: `_build_ingestion_batch`, `_read_pending_traces`, `Sender._send_batch`
- `exporter/src/ingestor.py`: `recover_failed` — reads `failed/*.jsonl`, calls `ingest()` per line
- `exporter/src/state.py`: `record_commit`, `record_error`

### consumers
- `Sender.__init__` reads `sender_cfg` fields — will read `max_payload_bytes`
- `_send_batch` is the only caller of `_build_ingestion_batch`
- `recover_failed` uses `glob("*.jsonl")` on `failed/` dir, parses each line as JSON, calls `ingest()`
- `_read_pending_traces` is called only from `_send_batch`
- Existing `TestBuildIngestionBatch` tests directly import and test `_build_ingestion_batch`

### coupling
- R-1/R-2/R-3 all modify `_send_batch` control flow but address distinct branches (accumulation, splitting, error handling)
- R-4 is a prerequisite (config field) consumed by R-1/R-2/R-3
- `_build_ingestion_batch` currently takes a list of traces; R-1 needs per-trace item building. Introduce a per-trace helper alongside existing function to preserve existing tests.

### unknowns
- None significant. All consumer paths verified.

## Phases with Tasks

### phase-1: payload-aware sending
- commit_boundary: task
- worker_dispatch: per-task
- gate: shell: cd exporter && python3 -m pytest tests/test_config.py tests/test_sender.py -v

#### task-1 [P]: add max_payload_bytes config field
- requirements: [R-4]
- outputs: [exporter/src/config.py, exporter/tests/test_config.py]
- action: Add `max_payload_bytes` field to `SenderConfig` with default `3_500_000`. Clamp values below `100_000` in `load_config` with a warning log. Add tests.
- verification:
  - planned_test: test_config.py — default value 3_500_000, custom toml value, clamping below 100_000

#### task-2: trace-level accumulation with commit_id precision
- requirements: [R-1]
- depends_on: [task-1]
- outputs: [exporter/src/sender.py, exporter/tests/test_sender.py]
- action: Restructure `_send_batch` to build items per-trace, accumulate serialized size, and stop when adding next trace would exceed `max_payload_bytes`. Update commit_id to last sent trace's seq_id only.
- constraints:
  - Constraint: commit_id must equal last sent trace's _seq_id, NOT batch max _seq_id
  - Constraint: introduce a per-trace item builder alongside existing `_build_ingestion_batch` to preserve existing tests
- verification:
  - planned_test: 3x1.5MB traces with 3.5MB limit — sends 2, commit_id == trace_2.seq_id (not trace_3.seq_id); next round reads trace_3
  - planned_test: 3x100KB traces — all sent in one batch, behavior unchanged
  - planned_test: negative — after partial send, commit_id != max(all seq_ids)

#### task-3: item-level splitting for single oversized trace
- requirements: [R-2]
- depends_on: [task-2]
- outputs: [exporter/src/sender.py, exporter/tests/test_sender.py]
- action: When first trace exceeds `max_payload_bytes`, split its items into sub-batches. trace-create in first sub-batch. All sub-batches succeed → commit; sub-batch failure → stop and retry or delegate to 413 handler.
- constraints:
  - Constraint: trace-create item must be in the first sub-batch
  - Constraint: single item exceeding threshold is sent alone as a sub-batch (not dropped or split further)
- verification:
  - planned_test: single trace 5MB with 3.5MB limit — splits into 2+ sub-batches, all succeed, commit
  - planned_test: single item exceeding threshold — sent alone, server decides
  - planned_test: sub-batch 2 returns 500 — stops, no commit, retries all next round

#### task-4: HTTP 413 handling with failed/ recovery
- requirements: [R-3]
- depends_on: [task-2]
- outputs: [exporter/src/sender.py, exporter/tests/test_sender.py]
- action: Add 413 handling branch: write original trace JSONL to `failed/` directory, advance commit_id, return from `_send_batch` without raising. Wire into R-2 sub-batch 413 path.
- constraints:
  - Constraint: 413 code path must return from _send_batch (not raise), so _run does not increase _backoff
  - Constraint: failed/ files: JSONL format, filename `<ISO-date>-<trace_id>.jsonl`, compatible with `recover_failed` glob
- verification:
  - planned_test: 413 response — trace written to failed/, commit_id advanced, sender continues without backoff
  - planned_test: failed/ file parseable by recover_failed (JSONL format, glob-compatible filename)
  - planned_test: R-2 sub-batch 413 — delegates to this handler, trace goes to failed/
  - source_scan: `rg '413' exporter/src/sender.py` — 413 not in the generic raise RuntimeError path

## Verification
- planned_test: all R-1/R-2/R-3/R-4 scenarios in test_sender.py and test_config.py
- shell: cd exporter && python3 -m pytest tests/test_config.py tests/test_sender.py -v
- source_scan: `rg 'raise RuntimeError' exporter/src/sender.py` — 413 must not reach the generic RuntimeError raise path
