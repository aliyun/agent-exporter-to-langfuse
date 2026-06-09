import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.state import IngestState, allocate_seq_id, save_ingest_state, update_file_entry

logger = logging.getLogger("langstash.ingestor")

MAX_BODY_BYTES = 10 * 1024 * 1024

REQUIRED_FIELDS_TRACE = ("name", "start_time", "end_time")


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
    save_ingest_state(state_path, state)

    logger.debug("ingested seq_id=%d to %s", seq_id, filename)
    return seq_id
