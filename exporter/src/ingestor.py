import fcntl
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.state import IngestState, allocate_seq_id, save_ingest_state, update_file_entry

logger = logging.getLogger("langstash.ingestor")

MAX_BODY_BYTES = 10 * 1024 * 1024

REQUIRED_FIELDS_TRACE = ("name", "start_time", "end_time")

RECOVER_INTERVAL = 60


class IngestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def validate_trace(body: dict[str, Any]) -> None:
    if not body.get("schema_version"):
        raise IngestError(422, "missing required field: schema_version")
    if not body.get("source"):
        raise IngestError(422, "missing required field: source")
    if not body.get("session_id"):
        raise IngestError(422, "missing required field: session_id")

    trace = body.get("trace")
    if not isinstance(trace, dict):
        raise IngestError(422, "missing required field: trace")
    for f in REQUIRED_FIELDS_TRACE:
        if not trace.get(f):
            raise IngestError(422, f"missing required field: trace.{f}")

    generations = body.get("generations")
    if not isinstance(generations, list) or len(generations) == 0:
        raise IngestError(422, "generations must be a non-empty array")


def _accumulate_tokens(state: IngestState, body: dict[str, Any], today: str) -> None:
    if state.tokens_date != today:
        state.tokens_date = today
        state.tokens_input = 0
        state.tokens_output = 0
        state.tokens_cache_read = 0
        state.tokens_cache_creation = 0
    for gen in body.get("generations", []):
        usage = gen.get("usage")
        if isinstance(usage, dict):
            state.tokens_input += int(usage.get("input", 0))
            state.tokens_output += int(usage.get("output", 0))
            state.tokens_cache_read += int(usage.get("cache_read_input_tokens", 0))
            state.tokens_cache_creation += int(usage.get("cache_creation_input_tokens", 0))


def ingest(body: dict[str, Any], state: IngestState, data_dir: Path, state_path: Path) -> int:
    validate_trace(body)

    seq_id = allocate_seq_id(state)
    now = datetime.now(timezone.utc)

    body["_seq_id"] = seq_id
    body["_received_at"] = now.isoformat()

    line = json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"

    if len(line.encode("utf-8")) > MAX_BODY_BYTES:
        state.next_seq_id -= 1
        raise IngestError(413, "payload exceeds 10MB limit")

    today = now.strftime("%Y-%m-%d")
    filename = f"{today}.jsonl"
    pending_dir = data_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    filepath = pending_dir / filename

    with open(filepath, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    update_file_entry(state, filename, seq_id)
    _accumulate_tokens(state, body, today)
    save_ingest_state(state_path, state)

    logger.debug("ingested seq_id=%d to %s", seq_id, filename)
    return seq_id


def recover_failed(data_dir: Path, state: IngestState, state_path: Path) -> int:
    failed_dir = data_dir / "failed"
    if not failed_dir.exists():
        return 0

    recovered = 0
    for fpath in sorted(failed_dir.glob("*.jsonl")):
        ok = True
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    body = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    ingest(body, state, data_dir, state_path)
                    recovered += 1
                except IngestError as e:
                    logger.warning("recover skip (%s): %s", fpath.name, e.message)
                    ok = False
        if ok:
            fpath.unlink(missing_ok=True)
            logger.info("recovered %s", fpath.name)
    return recovered


class FailedRecovery:
    def __init__(self, data_dir: Path, state: IngestState, state_path: Path):
        self._data_dir = data_dir
        self._state = state
        self._state_path = state_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="langstash-recovery")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(RECOVER_INTERVAL)
            if self._stop.is_set():
                break
            try:
                n = recover_failed(self._data_dir, self._state, self._state_path)
                if n:
                    logger.info("recovered %d failed traces to pending", n)
            except Exception as e:
                logger.error("recovery error: %s", e)
