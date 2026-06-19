import json
import logging
import threading
from pathlib import Path
from typing import Any

import httpx

from src.config import LangfuseConfig, SenderConfig
from src.state import (
    IngestState, SenderState,
    load_ingest_state, record_commit, record_error, save_sender_state,
)

logger = logging.getLogger("langstash.sender")


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

    def _post_otlp(self, otlp_json: dict[str, Any]) -> httpx.Response:
        url = f"{self._langfuse.base_url.rstrip('/')}/api/public/otel/v1/traces"
        auth = (self._langfuse.public_key, self._langfuse.secret_key)
        return httpx.post(
            url,
            json=otlp_json,
            auth=auth,
            headers={"Content-Type": "application/json"},
            timeout=self._cfg.timeout_seconds,
        )

    def _send_batch(self) -> bool:
        ingest_state = load_ingest_state(self._ingest_state_path)
        traces = _read_pending_traces(
            self._data_dir, ingest_state, self._state.commit_id, self._cfg.batch_size,
        )
        if not traces:
            return False

        last_success_seq = self._state.commit_id

        for trace in traces:
            seq = trace.get("_seq_id", 0)

            try:
                resp = self._post_otlp(trace)
            except Exception as e:
                record_error(self._state, seq, f"network error: {e}")
                save_sender_state(self._state_path, self._state)
                raise

            if 200 <= resp.status_code < 300:
                last_success_seq = seq
                continue

            error_msg = f"HTTP {resp.status_code}"
            try:
                error_msg += f": {resp.text[:200]}"
            except Exception:
                pass

            if resp.status_code == 400:
                logger.warning("skipping bad OTLP JSON (seq=%d): %s", seq, error_msg)
                last_success_seq = seq
                continue

            if last_success_seq > self._state.commit_id:
                record_commit(self._state, last_success_seq)

            record_error(self._state, seq, error_msg)
            save_sender_state(self._state_path, self._state)
            self._backoff = min(self._backoff * 2, self._cfg.max_backoff_seconds)
            raise RuntimeError(error_msg)

        if last_success_seq > self._state.commit_id:
            record_commit(self._state, last_success_seq)
            save_sender_state(self._state_path, self._state)
            logger.info("sent %d traces (commit_id=%d)", len(traces), last_success_seq)

        return True
