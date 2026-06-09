import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx

from src.config import LangfuseConfig, SenderConfig
from src.state import (
    IngestState, SenderState,
    load_ingest_state, record_commit, record_error, save_sender_state,
)
from src.stats import Stats

logger = logging.getLogger("langstash.sender")


def _build_ingestion_batch(traces: list[dict[str, Any]]) -> dict[str, Any]:
    batch: list[dict[str, Any]] = []
    for trace_json in traces:
        trace_id = trace_json.get("id") or str(uuid.uuid4())
        trace_data = trace_json.get("trace", {})

        batch.append({
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
            batch.append({
                "id": str(uuid.uuid4()),
                "type": "generation-create",
                "timestamp": gen.get("start_time", ""),
                "body": gen_body,
            })

            for span in trace_json.get("spans", []):
                if span.get("generation_index") != idx:
                    continue
                batch.append({
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

    return {"batch": batch}


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
                 ingest_state_path: Path, stats: Stats):
        self._langfuse = langfuse_cfg
        self._cfg = sender_cfg
        self._data_dir = data_dir
        self._state = sender_state
        self._state_path = sender_state_path
        self._ingest_state_path = ingest_state_path
        self._stats = stats
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

    def _send_batch(self) -> bool:
        ingest_state = load_ingest_state(self._ingest_state_path)
        traces = _read_pending_traces(
            self._data_dir, ingest_state, self._state.commit_id, self._cfg.batch_size,
        )
        if not traces:
            return False

        batch_payload = _build_ingestion_batch(traces)
        max_seq = max(t.get("_seq_id", 0) for t in traces)

        url = f"{self._langfuse.base_url.rstrip('/')}/api/public/ingestion"
        auth = (self._langfuse.public_key, self._langfuse.secret_key)

        try:
            resp = httpx.post(
                url,
                json=batch_payload,
                auth=auth,
                timeout=self._cfg.timeout_seconds,
            )
        except Exception as e:
            record_error(self._state, max_seq, f"network error: {e}")
            save_sender_state(self._state_path, self._state)
            raise

        if 200 <= resp.status_code < 300:
            record_commit(self._state, max_seq)
            save_sender_state(self._state_path, self._state)
            self._stats.record_sent(len(traces))
            logger.info("sent %d traces (commit_id=%d)", len(traces), max_seq)
            return True

        error_msg = f"HTTP {resp.status_code}"
        try:
            error_msg += f": {resp.text[:200]}"
        except Exception:
            pass

        if resp.status_code == 400:
            logger.warning("skipping bad batch: %s", error_msg)
            record_commit(self._state, max_seq)
            save_sender_state(self._state_path, self._state)
            return True

        if resp.status_code in (401, 403):
            logger.error("auth error, pausing sender: %s", error_msg)
            record_error(self._state, max_seq, error_msg)
            save_sender_state(self._state_path, self._state)
            self._stop.set()
            return False

        record_error(self._state, max_seq, error_msg)
        save_sender_state(self._state_path, self._state)
        self._backoff = min(self._backoff * 2, self._cfg.max_backoff_seconds)
        raise RuntimeError(error_msg)
