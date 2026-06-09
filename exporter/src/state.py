import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("langstash.state")


@dataclass
class FileEntry:
    min_seq: int = 0
    max_seq: int = 0


@dataclass
class LastError:
    time: str = ""
    seq_id: int = 0
    error: str = ""
    retries: int = 0


@dataclass
class IngestState:
    next_seq_id: int = 1
    files: dict[str, FileEntry] = field(default_factory=dict)
    tokens_date: str = ""
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_creation: int = 0


@dataclass
class SenderState:
    commit_id: int = 0
    last_commit_at: str = ""
    last_error: LastError | None = None


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_ingest_state(path: Path) -> IngestState:
    if not path.exists():
        return IngestState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to read %s, starting fresh", path.name)
        return IngestState()

    s = IngestState()
    s.next_seq_id = int(raw.get("next_seq_id", 1))
    for name, entry in raw.get("files", {}).items():
        if isinstance(entry, dict):
            s.files[name] = FileEntry(
                min_seq=int(entry.get("min_seq", 0)),
                max_seq=int(entry.get("max_seq", 0)),
            )
    tokens = raw.get("tokens_today", {})
    if isinstance(tokens, dict):
        s.tokens_date = tokens.get("date", "")
        s.tokens_input = int(tokens.get("input", 0))
        s.tokens_output = int(tokens.get("output", 0))
        s.tokens_cache_read = int(tokens.get("cache_read", 0))
        s.tokens_cache_creation = int(tokens.get("cache_creation", 0))
    return s


def save_ingest_state(path: Path, state: IngestState) -> None:
    data: dict[str, Any] = {
        "next_seq_id": state.next_seq_id,
        "files": {},
        "tokens_today": {
            "date": state.tokens_date,
            "input": state.tokens_input,
            "output": state.tokens_output,
            "cache_read": state.tokens_cache_read,
            "cache_creation": state.tokens_cache_creation,
        },
    }
    for name, entry in state.files.items():
        data["files"][name] = {
            "min_seq": entry.min_seq,
            "max_seq": entry.max_seq,
        }
    _atomic_write(path, data)


def load_sender_state(path: Path) -> SenderState:
    if not path.exists():
        return SenderState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to read %s, starting fresh", path.name)
        return SenderState()

    s = SenderState()
    s.commit_id = int(raw.get("commit_id", 0))
    s.last_commit_at = raw.get("last_commit_at", "")

    le = raw.get("last_error")
    if isinstance(le, dict) and le:
        s.last_error = LastError(
            time=le.get("time", ""),
            seq_id=int(le.get("seq_id", 0)),
            error=le.get("error", ""),
            retries=int(le.get("retries", 0)),
        )
    return s


def save_sender_state(path: Path, state: SenderState) -> None:
    data: dict[str, Any] = {
        "commit_id": state.commit_id,
        "last_commit_at": state.last_commit_at,
        "last_error": None,
    }
    if state.last_error:
        data["last_error"] = {
            "time": state.last_error.time,
            "seq_id": state.last_error.seq_id,
            "error": state.last_error.error,
            "retries": state.last_error.retries,
        }
    _atomic_write(path, data)


def migrate_legacy_state(legacy_path: Path, ingest_path: Path, sender_path: Path) -> bool:
    if not legacy_path.exists():
        return False
    if ingest_path.exists() or sender_path.exists():
        return False

    try:
        raw = json.loads(legacy_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    ingest = IngestState()
    ingest.next_seq_id = int(raw.get("next_seq_id", 1))
    for name, entry in raw.get("files", {}).items():
        if isinstance(entry, dict):
            ingest.files[name] = FileEntry(
                min_seq=int(entry.get("min_seq", 0)),
                max_seq=int(entry.get("max_seq", 0)),
            )
    save_ingest_state(ingest_path, ingest)

    sender = SenderState()
    sender.commit_id = int(raw.get("commit_id", 0))
    sender.last_commit_at = raw.get("last_commit_at", "")
    le = raw.get("last_error")
    if isinstance(le, dict) and le:
        sender.last_error = LastError(
            time=le.get("time", ""),
            seq_id=int(le.get("seq_id", 0)),
            error=le.get("error", ""),
            retries=int(le.get("retries", 0)),
        )
    save_sender_state(sender_path, sender)

    logger.info("migrated legacy state.json → ingest.json + sender.json")
    return True


def allocate_seq_id(state: IngestState) -> int:
    seq_id = state.next_seq_id
    state.next_seq_id = seq_id + 1
    return seq_id


def record_commit(state: SenderState, seq_id: int) -> None:
    state.commit_id = seq_id
    state.last_commit_at = datetime.now(timezone.utc).isoformat()
    state.last_error = None


def record_error(state: SenderState, seq_id: int, error: str) -> None:
    if state.last_error and state.last_error.seq_id == seq_id:
        state.last_error.retries += 1
        state.last_error.time = datetime.now(timezone.utc).isoformat()
        state.last_error.error = error
    else:
        state.last_error = LastError(
            time=datetime.now(timezone.utc).isoformat(),
            seq_id=seq_id,
            error=error,
            retries=1,
        )


def update_file_entry(state: IngestState, filename: str, seq_id: int) -> None:
    if filename not in state.files:
        state.files[filename] = FileEntry(min_seq=seq_id, max_seq=seq_id)
    else:
        entry = state.files[filename]
        if seq_id < entry.min_seq or entry.min_seq == 0:
            entry.min_seq = seq_id
        if seq_id > entry.max_seq:
            entry.max_seq = seq_id
