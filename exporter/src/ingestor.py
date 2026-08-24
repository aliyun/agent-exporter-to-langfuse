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

# Max serialized bytes per OTLP chunk written when recovering an oversized
# failed trace. Kept under the 10MB server/Langfuse acceptance threshold so each
# chunk is deliverable as a single Langfuse OTel POST. A failed trace larger than
# OTLP_CHUNK_BYTES is split into multiple chunks sharing one traceId; Langfuse
# reassembles the trace by traceId/spanId.
OTLP_CHUNK_BYTES = 9 * 1024 * 1024

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


def _accumulate_tokens(state: IngestState, body: dict[str, Any], filename: str) -> None:
    entry = state.files.get(filename)
    if not entry:
        return

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
                    entry.input += int(usage.get("input", 0))
                    entry.output += int(usage.get("output", 0))
                    entry.cache_read += int(usage.get("cache_read_input_tokens", 0))
                    entry.cache_creation += int(usage.get("cache_creation_input_tokens", 0))


def ingest(body: dict[str, Any], state: IngestState, data_dir: Path, state_path: Path) -> int:
    validate_otlp(body)

    seq_id = allocate_seq_id(state)
    now = datetime.now(timezone.utc)

    body["_seq_id"] = seq_id
    body["_received_at"] = now.isoformat()

    line = json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"

    if len(line.encode("utf-8")) > MAX_BODY_BYTES:
        state.next_seq_id -= 1
        raise IngestError(413, f"payload exceeds {MAX_BODY_BYTES // (1024 * 1024)}MB limit")

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
    _accumulate_tokens(state, body, filename)
    save_ingest_state(state_path, state)

    logger.debug("ingested seq_id=%d to %s", seq_id, filename)
    return seq_id


def _otlp_body_bytes(body: dict[str, Any]) -> int:
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _split_otlp(body: dict[str, Any], max_bytes: int) -> list[dict[str, Any]]:
    """Split a single-resource OTLP body into chunks each <= max_bytes.

    Each chunk duplicates the original root span so it still passes
    validate_otlp's root-span requirement. Returns [] when the body shape is
    not safely splittable (multi-resource, zero/multiple roots) or when a
    single span alone exceeds max_bytes; the caller then drops the line.
    """
    rs = body.get("resourceSpans")
    if not isinstance(rs, list) or len(rs) != 1:
        return []
    r = rs[0]
    if not isinstance(r, dict):
        return []
    ss_list = r.get("scopeSpans")
    if not isinstance(ss_list, list) or len(ss_list) != 1:
        return []
    scope_obj = ss_list[0]
    if not isinstance(scope_obj, dict):
        return []
    scope = scope_obj.get("scope", {})
    spans = scope_obj.get("spans", [])
    if not isinstance(spans, list):
        return []
    resource = r.get("resource", {})

    roots = [s for s in spans if isinstance(s, dict) and not s.get("parentSpanId")]
    if len(roots) != 1:
        return []
    root = roots[0]
    children = [s for s in spans if s is not root]

    def make_chunk(batch: list[dict[str, Any]]) -> dict[str, Any]:
        return {"resourceSpans": [{
            "resource": resource,
            "scopeSpans": [{"scope": scope, "spans": [root, *batch]}],
        }]}

    def chunk_bytes(batch: list[dict[str, Any]]) -> int:
        return len(json.dumps(make_chunk(batch), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    if chunk_bytes([]) > max_bytes:
        return []

    chunks: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
    for sp in children:
        if chunk_bytes([*batch, sp]) <= max_bytes:
            batch.append(sp)
            continue
        if batch:
            chunks.append(make_chunk(batch))
            batch = []
        if chunk_bytes([sp]) <= max_bytes:
            batch.append(sp)
        else:
            return []
    if batch:
        chunks.append(make_chunk(batch))
    return chunks if chunks else [make_chunk([])]


def _recover_line(body: dict[str, Any], state: IngestState, data_dir: Path,
                   state_path: Path, fname: str) -> int | None:
    """Re-ingest one recovered trace, splitting oversized bodies.

    Returns the number of pending lines written (>=0, 0 when dropped), or None
    when a transient error means the line should be retried next cycle.
    """
    if _otlp_body_bytes(body) <= OTLP_CHUNK_BYTES:
        try:
            ingest(body, state, data_dir, state_path)
            return 1
        except IngestError as e:
            logger.warning("recover drop (%s): %s", fname, e.message)
            return 0
        except OSError as e:
            logger.error("recover retry (%s): %s", fname, e)
            return None

    chunks = _split_otlp(body, OTLP_CHUNK_BYTES)
    if not chunks:
        logger.warning(
            "recover drop (%s): payload exceeds %dMB and cannot be split",
            fname, OTLP_CHUNK_BYTES // (1024 * 1024),
        )
        return 0
    count = 0
    try:
        for chunk in chunks:
            ingest(chunk, state, data_dir, state_path)
            count += 1
    except IngestError as e:
        logger.warning("recover drop (%s): chunk ingest failed: %s", fname, e.message)
    except OSError as e:
        logger.error("recover retry (%s): %s", fname, e)
        return None if count == 0 else count
    return count


def recover_failed(data_dir: Path, state: IngestState, state_path: Path) -> int:
    failed_dir = data_dir / "failed"
    if not failed_dir.exists():
        return 0

    recovered = 0
    for fpath in sorted(failed_dir.glob("*.jsonl")):
        kept: list[str] = []
        with open(fpath, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                lines = [ln.strip() for ln in f if ln.strip()]
                for line in lines:
                    try:
                        body = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("recover drop (%s): JSONDecodeError", fpath.name)
                        continue
                    result = _recover_line(body, state, data_dir, state_path, fpath.name)
                    if result is None:
                        kept.append(line)
                    else:
                        recovered += result
                if kept:
                    f.seek(0)
                    f.truncate()
                    f.write("\n".join(kept) + "\n")
                    f.flush()
                else:
                    f.seek(0)
                    f.truncate()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        if kept:
            logger.info("recover partial (%s): %d line(s) deferred for retry",
                        fpath.name, len(kept))
        else:
            try:
                fpath.unlink(missing_ok=True)
                logger.info("recovered %s", fpath.name)
            except OSError:
                # a hook may have appended between unlock and unlink; leave it
                pass
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
