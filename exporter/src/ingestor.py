import fcntl
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.state import IngestState, allocate_seq_id, save_ingest_state, update_file_entry

logger = logging.getLogger("langstash.ingestor")

MAX_BODY_BYTES = 10 * 1024 * 1024

RECOVER_INTERVAL = 60

_HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX_16_RE = re.compile(r"^[0-9a-f]{16}$")


class IngestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def validate_otlp(body: dict[str, Any]) -> None:
    resource_spans = body.get("resourceSpans")
    if not isinstance(resource_spans, list) or len(resource_spans) == 0:
        raise IngestError(422, "missing or empty resourceSpans")

    has_root = False

    for rs_idx, rs in enumerate(resource_spans):
        scope_spans = rs.get("scopeSpans")
        if not isinstance(scope_spans, list):
            raise IngestError(422, f"resourceSpans[{rs_idx}]: missing scopeSpans")

        for ss_idx, ss in enumerate(scope_spans):
            spans = ss.get("spans")
            if not isinstance(spans, list) or len(spans) == 0:
                raise IngestError(422, f"resourceSpans[{rs_idx}].scopeSpans[{ss_idx}]: missing or empty spans")

            for sp_idx, span in enumerate(spans):
                prefix = f"resourceSpans[{rs_idx}].scopeSpans[{ss_idx}].spans[{sp_idx}]"

                trace_id = span.get("traceId", "")
                if not isinstance(trace_id, str) or not _HEX_32_RE.match(trace_id):
                    raise IngestError(422, f"{prefix}: traceId must be 32-char hex string")

                span_id = span.get("spanId", "")
                if not isinstance(span_id, str) or not _HEX_16_RE.match(span_id):
                    raise IngestError(422, f"{prefix}: spanId must be 16-char hex string")

                name = span.get("name")
                if not isinstance(name, str) or not name:
                    raise IngestError(422, f"{prefix}: name must be non-empty string")

                start_ns = span.get("startTimeUnixNano")
                if not isinstance(start_ns, str) or not start_ns:
                    raise IngestError(422, f"{prefix}: startTimeUnixNano must be non-empty string")
                try:
                    start_val = int(start_ns)
                except (ValueError, TypeError):
                    raise IngestError(422, f"{prefix}: startTimeUnixNano is not a valid nanosecond timestamp")

                end_ns = span.get("endTimeUnixNano")
                if end_ns is not None and end_ns != "":
                    try:
                        end_val = int(end_ns)
                    except (ValueError, TypeError):
                        raise IngestError(422, f"{prefix}: endTimeUnixNano is not a valid nanosecond timestamp")
                    if end_val < start_val:
                        raise IngestError(422, f"{prefix}: endTimeUnixNano < startTimeUnixNano")

                parent_span_id = span.get("parentSpanId", "")
                if not parent_span_id:
                    has_root = True

                attributes = span.get("attributes")
                if attributes is not None:
                    if not isinstance(attributes, list):
                        raise IngestError(422, f"{prefix}: attributes must be a KeyValue array")
                    for attr in attributes:
                        if not isinstance(attr, dict) or "key" not in attr or "value" not in attr:
                            raise IngestError(422, f"{prefix}: each attribute must have key and value")

    if not has_root:
        raise IngestError(422, "no root span found (all spans have parentSpanId)")


def _accumulate_tokens(state: IngestState, body: dict[str, Any], today: str) -> None:
    if state.tokens_date != today:
        state.tokens_date = today
        state.tokens_input = 0
        state.tokens_output = 0
        state.tokens_cache_read = 0
        state.tokens_cache_creation = 0

    for rs in body.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for attr in span.get("attributes", []):
                    if attr.get("key") != "langfuse.observation.usage_details":
                        continue
                    value = attr.get("value", {})
                    raw = value.get("stringValue", "")
                    if not raw:
                        continue
                    try:
                        usage = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(usage, dict):
                        continue
                    state.tokens_input += int(usage.get("input", 0))
                    state.tokens_output += int(usage.get("output", 0))
                    state.tokens_cache_read += int(usage.get("cache_read_input_tokens", 0))
                    state.tokens_cache_creation += int(usage.get("cache_creation_input_tokens", 0))


def ingest(body: dict[str, Any], state: IngestState, data_dir: Path, state_path: Path) -> int:
    validate_otlp(body)

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
                    logger.warning("recover skip (%s): JSONDecodeError", fpath.name)
                    ok = False
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
