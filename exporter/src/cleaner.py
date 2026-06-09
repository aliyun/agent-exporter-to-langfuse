import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.config import StorageConfig
from src.state import IngestState, load_sender_state, save_ingest_state

logger = logging.getLogger("langstash.cleaner")


def _dir_size_mb(path: Path) -> float:
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total / (1024 * 1024)


def _cleanup_retention(data_dir: Path, ingest_state: IngestState,
                       commit_id: int, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed = 0
    pending_dir = data_dir / "pending"
    for filename, entry in list(ingest_state.files.items()):
        if entry.max_seq > commit_id:
            continue
        date_part = filename.replace(".jsonl", "")
        if date_part <= cutoff_str:
            fpath = pending_dir / filename
            if fpath.exists():
                fpath.unlink()
                logger.info("retention cleanup: removed %s", filename)
                removed += 1
            del ingest_state.files[filename]
    return removed


def _cleanup_size(data_dir: Path, ingest_state: IngestState,
                  commit_id: int, max_size_gb: float) -> int:
    max_mb = max_size_gb * 1024
    current_mb = _dir_size_mb(data_dir)
    if current_mb <= max_mb:
        return 0

    removed = 0
    pending_dir = data_dir / "pending"
    failed_dir = data_dir / "failed"

    committed = sorted(
        name for name, e in ingest_state.files.items() if e.max_seq <= commit_id
    )
    for filename in committed:
        if _dir_size_mb(data_dir) <= max_mb:
            break
        fpath = pending_dir / filename
        if fpath.exists():
            fpath.unlink()
            removed += 1
            logger.info("size cleanup: removed committed %s", filename)
        if filename in ingest_state.files:
            del ingest_state.files[filename]

    if _dir_size_mb(data_dir) > max_mb and failed_dir.exists():
        for fpath in sorted(failed_dir.glob("*.jsonl")):
            if _dir_size_mb(data_dir) <= max_mb:
                break
            fpath.unlink()
            removed += 1
            logger.info("size cleanup: removed failed %s", fpath.name)

    if _dir_size_mb(data_dir) > max_mb:
        uncommitted = sorted(
            name for name, e in ingest_state.files.items() if e.max_seq > commit_id
        )
        for filename in uncommitted:
            if _dir_size_mb(data_dir) <= max_mb:
                break
            fpath = pending_dir / filename
            if fpath.exists():
                fpath.unlink()
                removed += 1
                logger.warning("size cleanup: removed UNCOMMITTED %s (data loss)", filename)
            if filename in ingest_state.files:
                del ingest_state.files[filename]

    return removed


def run_cleanup(data_dir: Path, ingest_state: IngestState, ingest_state_path: Path,
                sender_state_path: Path, storage_cfg: StorageConfig) -> None:
    sender_state = load_sender_state(sender_state_path)
    commit_id = sender_state.commit_id

    r1 = _cleanup_retention(data_dir, ingest_state, commit_id, storage_cfg.retention_days)
    r2 = _cleanup_size(data_dir, ingest_state, commit_id, storage_cfg.max_size_gb)
    if r1 or r2:
        save_ingest_state(ingest_state_path, ingest_state)
        logger.info("cleanup done: %d retention + %d size removals", r1, r2)


class Cleaner:
    def __init__(self, data_dir: Path, ingest_state: IngestState,
                 ingest_state_path: Path, sender_state_path: Path,
                 storage_cfg: StorageConfig):
        self._data_dir = data_dir
        self._ingest_state = ingest_state
        self._ingest_state_path = ingest_state_path
        self._sender_state_path = sender_state_path
        self._cfg = storage_cfg
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        run_cleanup(self._data_dir, self._ingest_state, self._ingest_state_path,
                    self._sender_state_path, self._cfg)
        self._thread = threading.Thread(target=self._run, daemon=True, name="langstash-cleaner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(3600)
            if self._stop.is_set():
                break
            try:
                run_cleanup(self._data_dir, self._ingest_state, self._ingest_state_path,
                            self._sender_state_path, self._cfg)
            except Exception as e:
                logger.error("cleaner error: %s", e)
