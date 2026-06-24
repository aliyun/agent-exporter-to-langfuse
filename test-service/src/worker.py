import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.config import Config
from src.git_manager import GitManager, MergeConflictError
from src.progress_parser import ProgressParser
from src.queue import JobQueue
from src.store import Store

logger = logging.getLogger("langstash-tester.worker")


class Worker:
    def __init__(self, config: Config, store: Store, queue: JobQueue,
                 git: GitManager, on_complete: Callable[[str], None] | None = None):
        self._config = config
        self._store = store
        self._queue = queue
        self._git = git
        self._on_complete = on_complete
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._current_process: subprocess.Popen | None = None
        self._current_job_id: str | None = None
        self._cancel_requested: set[str] = set()

        self._queue.set_cancel_callback(self._handle_cancel)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="e2e-worker")
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop_event.set()
        if self._current_process:
            self._kill_process(self._current_process)
        if self._thread:
            self._thread.join(timeout=timeout)
        self._git.cleanup_all_worktrees()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            job_id = self._queue.dequeue(timeout=1.0)
            if job_id is None:
                continue
            if self._stop_event.is_set():
                break
            try:
                self._execute_job(job_id)
            except Exception as e:
                logger.error("job %s failed unexpectedly: %s", job_id, e)
                self._store.update_job(job_id, status="failed",
                                       finished_at=datetime.now(timezone.utc).isoformat(),
                                       output_tail=str(e))
            finally:
                self._current_job_id = None
                self._current_process = None
                self._queue.complete(job_id)
                if self._on_complete:
                    self._on_complete(job_id)

    def _execute_job(self, job_id: str) -> None:
        self._current_job_id = job_id
        job = self._store.get_job(job_id)
        if job is None:
            return

        if job_id in self._cancel_requested:
            self._cancel_requested.discard(job_id)
            self._store.update_job(job_id, status="cancelled",
                                   finished_at=datetime.now(timezone.utc).isoformat())
            return

        now = datetime.now(timezone.utc).isoformat()
        self._store.update_job(job_id, status="running", started_at=now,
                               progress={"phase": "preparing", "passed": 0, "failed": 0, "total": 0})

        self._store.update_job(job_id, progress={"phase": "preparing", "passed": 0, "failed": 0, "total": 0})
        self._git.fetch()

        worktree_path = self._git.create_worktree(job_id, job["branch"], job.get("commit"))
        actual_commit = self._get_head_commit(worktree_path)
        if actual_commit:
            self._store.update_job(job_id, commit_sha=actual_commit)

        try:
            if job["mode"] == "integration":
                self._store.update_job(job_id, progress={"phase": "merging", "passed": 0, "failed": 0, "total": 0})
                try:
                    self._git.merge_main(worktree_path)
                except MergeConflictError as e:
                    self._store.update_job(
                        job_id, status="conflict",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        summary={"conflict_files": e.conflict_files},
                    )
                    return

            self._maybe_uv_sync(worktree_path)

            test_command = job.get("test_command")
            if not test_command:
                test_dir = worktree_path / self._config.e2e.default_test_dir
                if test_dir.exists():
                    scripts = sorted(test_dir.glob("*.sh"))
                    if scripts:
                        test_command = " && ".join(f"bash {s}" for s in scripts)
                if not test_command:
                    self._store.update_job(
                        job_id, status="failed",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        output_tail="no E2E test scripts found",
                    )
                    return

            timeout = job.get("timeout_seconds") or self._config.e2e.default_timeout_seconds
            exit_code, parser = self._run_tests(job_id, worktree_path, test_command, timeout)

            finished_at = datetime.now(timezone.utc).isoformat()
            started_at = self._store.get_job(job_id).get("started_at", now)
            try:
                duration = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
            except Exception:
                duration = 0

            if job_id in self._cancel_requested:
                self._cancel_requested.discard(job_id)
                status = "cancelled"
            elif exit_code is None:
                status = "timeout"
            elif exit_code == 0:
                status = "success"
            else:
                status = "failed"

            progress = parser.progress.to_dict()
            progress["phase"] = "completed"

            log_path = Path(self._config.storage.log_dir) / f"{job_id}.log"
            output_tail = self._read_tail(log_path, lines=50)

            summary = {
                "total": parser.progress.passed + parser.progress.failed,
                "passed": parser.progress.passed,
                "failed": parser.progress.failed,
                "failed_tests": [],
            }

            self._store.update_job(
                job_id, status=status, finished_at=finished_at,
                duration_seconds=duration, exit_code=exit_code if exit_code is not None else -1,
                progress=progress, summary=summary, output_tail=output_tail,
            )

        finally:
            self._git.remove_worktree(job_id)

    def _run_tests(self, job_id: str, worktree_path: Path, command: str,
                   timeout: int) -> tuple[int | None, ProgressParser]:
        parser = ProgressParser()
        log_path = Path(self._config.storage.log_dir) / f"{job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        self._store.update_job(job_id, progress={"phase": "testing", "passed": 0, "failed": 0, "total": 0})

        env = os.environ.copy()
        env["LANGSTASH_TESTER_JOB_ID"] = job_id

        process = subprocess.Popen(
            ["bash", "-l", "-c", command],
            cwd=worktree_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        self._current_process = process

        start_time = time.monotonic()
        timed_out = False

        with open(log_path, "w", encoding="utf-8") as log_file:
            while True:
                if self._stop_event.is_set() or job_id in self._cancel_requested:
                    self._kill_process(process)
                    break

                elapsed = time.monotonic() - start_time
                if elapsed > timeout:
                    timed_out = True
                    self._kill_process(process)
                    break

                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    continue

                decoded = line.decode("utf-8", errors="replace")
                log_file.write(decoded)
                log_file.flush()

                parser.parse_line(decoded)
                parser.progress.elapsed_seconds = time.monotonic() - start_time

                self._store.update_job(job_id, progress=parser.progress.to_dict())

        process.wait(timeout=5)
        return (None if timed_out else process.returncode), parser

    def _handle_cancel(self, job_id: str) -> None:
        self._cancel_requested.add(job_id)
        if self._current_job_id == job_id and self._current_process:
            self._kill_process(self._current_process)

    def _kill_process(self, process: subprocess.Popen) -> None:
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _maybe_uv_sync(self, worktree_path: Path) -> None:
        pyproject = worktree_path / "exporter" / "pyproject.toml"
        if pyproject.exists():
            try:
                subprocess.run(
                    ["uv", "sync"], cwd=worktree_path / "exporter",
                    capture_output=True, timeout=120,
                )
            except Exception as e:
                logger.warning("uv sync failed: %s", e)

    @staticmethod
    def _get_head_commit(worktree_path: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_path, capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    @staticmethod
    def _read_tail(path: Path, lines: int = 50) -> str:
        if not path.exists():
            return ""
        try:
            all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(all_lines[-lines:])
        except Exception:
            return ""
