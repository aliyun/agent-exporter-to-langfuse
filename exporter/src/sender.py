import fcntl
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.config import LangfuseConfig, SenderConfig
from src.state import (
    IngestState, SenderState,
    load_ingest_state, record_commit, record_error, save_sender_state,
)

logger = logging.getLogger("langstash.sender")


def _build_trace_items(trace_json: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    trace_id = trace_json.get("id") or str(uuid.uuid4())
    trace_data = trace_json.get("trace", {})

    items.append({
        "id": str(uuid.uuid4()),
        "type": "trace-create",
        "timestamp": trace_data.get("start_time", ""),
        "body": {
            "id": trace_id,
            "name": trace_data.get("name", ""),
            "sessionId": trace_json.get("session_id", ""),
            "userId": trace_json.get("user_id"),
            "input": trace_data.get("input"),
            "output": trace_data.get("output"),
            "metadata": trace_data.get("metadata"),
            "tags": trace_json.get("tags"),
        },
    })

    for idx, gen in enumerate(trace_json.get("generations", [])):
        gen_id = str(uuid.uuid4())
        gen_body: dict[str, Any] = {
            "id": gen_id,
            "traceId": trace_id,
            "name": gen.get("name", f"Generation {idx + 1}"),
            "model": gen.get("model", ""),
            "startTime": gen.get("start_time", ""),
            "endTime": gen.get("end_time", ""),
            "input": gen.get("input"),
            "output": gen.get("output"),
            "metadata": gen.get("metadata"),
        }
        usage = gen.get("usage")
        if isinstance(usage, dict):
            gen_body["usage"] = usage
        items.append({
            "id": str(uuid.uuid4()),
            "type": "generation-create",
            "timestamp": gen.get("start_time", ""),
            "body": gen_body,
        })

        for span in trace_json.get("spans", []):
            if span.get("generation_index") != idx:
                continue
            items.append({
                "id": str(uuid.uuid4()),
                "type": "span-create",
                "timestamp": span.get("start_time", ""),
                "body": {
                    "id": str(uuid.uuid4()),
                    "traceId": trace_id,
                    "parentObservationId": gen_id,
                    "name": span.get("name", ""),
                    "startTime": span.get("start_time", ""),
                    "endTime": span.get("end_time", ""),
                    "input": span.get("input"),
                    "output": span.get("output"),
                    "metadata": span.get("metadata"),
                },
            })

    return items


def _build_ingestion_batch(traces: list[dict[str, Any]]) -> dict[str, Any]:
    batch: list[dict[str, Any]] = []
    for trace_json in traces:
        batch.extend(_build_trace_items(trace_json))
    return {"batch": batch}


def _items_byte_size(items: list[dict[str, Any]]) -> int:
    return len(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _split_into_batches(
    items: list[dict[str, Any]], max_bytes: int,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0

    for item in items:
        item_size = _items_byte_size([item])
        if current and current_size + item_size > max_bytes:
            batches.append(current)
            current = [item]
            current_size = item_size
        else:
            current.append(item)
            current_size += item_size

    if current:
        batches.append(current)
    return batches


def _write_to_failed(data_dir: Path, trace_json: dict[str, Any], payload_size: int) -> None:
    failed_dir = data_dir / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    trace_id = trace_json.get("id", "unknown")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{today}-{trace_id}.jsonl"
    filepath = failed_dir / filename

    line = json.dumps(trace_json, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(filepath, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    logger.warning(
        "trace %s (%d bytes) moved to failed/: %s",
        trace_id, payload_size, filename,
    )


def _read_pending_traces(
    data_dir: Path, ingest_state: IngestState, commit_id: int, batch_size: int,
) -> list[dict[str, Any]]:
    pending_dir = data_dir / "pending"
    if not pending_dir.exists():
        return []

    files_with_pending = sorted(
        ((name, entry) for name, entry in ingest_state.files.items()
         if entry.max_seq > commit_id),
        key=lambda x: x[0],
    )

    traces: list[dict[str, Any]] = []
    for filename, _entry in files_with_pending:
        filepath = pending_dir / filename
        if not filepath.exists():
            continue
        try:
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seq = row.get("_seq_id", 0)
                    if seq <= commit_id:
                        continue
                    traces.append(row)
                    if len(traces) >= batch_size:
                        return traces
        except OSError:
            continue
    return traces


class Sender:
    def __init__(self, langfuse_cfg: LangfuseConfig, sender_cfg: SenderConfig,
                 data_dir: Path, sender_state: SenderState, sender_state_path: Path,
                 ingest_state_path: Path):
        self._langfuse = langfuse_cfg
        self._cfg = sender_cfg
        self._data_dir = data_dir
        self._state = sender_state
        self._state_path = sender_state_path
        self._ingest_state_path = ingest_state_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff = sender_cfg.interval_seconds

    def start(self) -> None:
        if not self._langfuse.public_key or not self._langfuse.secret_key:
            logger.warning("langfuse credentials not configured, sender disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="langstash-sender")
        self._thread.start()
        logger.info("sender started (interval=%ds)", self._cfg.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sent = self._send_batch()
                if sent:
                    self._backoff = self._cfg.interval_seconds
                else:
                    self._stop.wait(self._backoff)
                    continue
            except Exception as e:
                logger.error("sender error: %s", e)
                self._backoff = min(self._backoff * 2, self._cfg.max_backoff_seconds)
                self._stop.wait(self._backoff)
                continue
            self._stop.wait(self._cfg.interval_seconds)

    def _post_batch(self, batch_items: list[dict[str, Any]]) -> httpx.Response:
        url = f"{self._langfuse.base_url.rstrip('/')}/api/public/ingestion"
        auth = (self._langfuse.public_key, self._langfuse.secret_key)
        return httpx.post(
            url,
            json={"batch": batch_items},
            auth=auth,
            timeout=self._cfg.timeout_seconds,
        )

    def _handle_413(self, trace_json: dict[str, Any], payload_size: int) -> None:
        _write_to_failed(self._data_dir, trace_json, payload_size)
        seq = trace_json.get("_seq_id", 0)
        record_commit(self._state, seq)
        save_sender_state(self._state_path, self._state)

    def _send_batch(self) -> bool:
        ingest_state = load_ingest_state(self._ingest_state_path)
        traces = _read_pending_traces(
            self._data_dir, ingest_state, self._state.commit_id, self._cfg.batch_size,
        )
        if not traces:
            return False

        max_bytes = self._cfg.max_payload_bytes

        accumulated_items: list[dict[str, Any]] = []
        accumulated_size = 0
        included_traces: list[dict[str, Any]] = []

        for trace_json in traces:
            trace_items = _build_trace_items(trace_json)
            trace_size = _items_byte_size(trace_items)

            if accumulated_items and accumulated_size + trace_size > max_bytes:
                break

            if not accumulated_items and trace_size > max_bytes:
                return self._send_oversized_trace(trace_json, trace_items)

            accumulated_items.extend(trace_items)
            accumulated_size += trace_size
            included_traces.append(trace_json)

        if not included_traces:
            return False

        commit_seq = max(t.get("_seq_id", 0) for t in included_traces)

        try:
            resp = self._post_batch(accumulated_items)
        except Exception as e:
            record_error(self._state, commit_seq, f"network error: {e}")
            save_sender_state(self._state_path, self._state)
            raise

        if 200 <= resp.status_code < 300:
            record_commit(self._state, commit_seq)
            save_sender_state(self._state_path, self._state)
            logger.info("sent %d traces (commit_id=%d)", len(included_traces), commit_seq)
            return True

        error_msg = f"HTTP {resp.status_code}"
        try:
            error_msg += f": {resp.text[:200]}"
        except Exception:
            pass

        if resp.status_code == 413:
            for t in included_traces:
                self._handle_413(t, accumulated_size)
            return True

        if resp.status_code == 400:
            logger.warning("skipping bad batch: %s", error_msg)
            record_commit(self._state, commit_seq)
            save_sender_state(self._state_path, self._state)
            return True

        if resp.status_code in (401, 403):
            logger.error("auth error, pausing sender: %s", error_msg)
            record_error(self._state, commit_seq, error_msg)
            save_sender_state(self._state_path, self._state)
            self._stop.set()
            return False

        record_error(self._state, commit_seq, error_msg)
        save_sender_state(self._state_path, self._state)
        self._backoff = min(self._backoff * 2, self._cfg.max_backoff_seconds)
        raise RuntimeError(error_msg)

    def _send_oversized_trace(
        self, trace_json: dict[str, Any], trace_items: list[dict[str, Any]],
    ) -> bool:
        max_bytes = self._cfg.max_payload_bytes
        seq = trace_json.get("_seq_id", 0)
        sub_batches = _split_into_batches(trace_items, max_bytes)

        logger.info(
            "splitting oversized trace %s into %d sub-batches",
            trace_json.get("id", "?"), len(sub_batches),
        )

        for i, batch_items in enumerate(sub_batches):
            try:
                resp = self._post_batch(batch_items)
            except Exception as e:
                record_error(self._state, seq, f"network error on sub-batch {i}: {e}")
                save_sender_state(self._state_path, self._state)
                raise

            if 200 <= resp.status_code < 300:
                continue

            if resp.status_code == 413:
                self._handle_413(trace_json, _items_byte_size(batch_items))
                return True

            if resp.status_code in (401, 403):
                error_msg = f"HTTP {resp.status_code}"
                try:
                    error_msg += f": {resp.text[:200]}"
                except Exception:
                    pass
                logger.error("auth error on sub-batch %d, pausing sender: %s", i, error_msg)
                record_error(self._state, seq, error_msg)
                save_sender_state(self._state_path, self._state)
                self._stop.set()
                return False

            error_msg = f"HTTP {resp.status_code} on sub-batch {i}"
            try:
                error_msg += f": {resp.text[:200]}"
            except Exception:
                pass
            record_error(self._state, seq, error_msg)
            save_sender_state(self._state_path, self._state)
            self._backoff = min(self._backoff * 2, self._cfg.max_backoff_seconds)
            raise RuntimeError(error_msg)

        record_commit(self._state, seq)
        save_sender_state(self._state_path, self._state)
        logger.info("sent oversized trace (commit_id=%d, %d sub-batches)", seq, len(sub_batches))
        return True
