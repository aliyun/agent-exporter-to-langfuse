import threading
import time
from collections import deque
from typing import Callable


class JobQueue:
    def __init__(self, same_branch_policy: str = "replace"):
        self._queue: deque[str] = deque()
        self._running: str | None = None
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._policy = same_branch_policy
        self._cancel_callback: Callable[[str], None] | None = None
        self._branch_map: dict[str, str] = {}

    def set_cancel_callback(self, callback: Callable[[str], None]) -> None:
        self._cancel_callback = callback

    def enqueue(self, job_id: str, branch: str) -> int | None:
        with self._not_empty:
            existing = self._find_same_branch(branch)

            if existing and self._policy == "reject":
                return None

            if existing and self._policy == "replace":
                for eid in existing:
                    if eid == self._running:
                        if self._cancel_callback:
                            self._cancel_callback(eid)
                        self._running = None
                    elif eid in self._queue:
                        self._queue.remove(eid)
                    self._branch_map.pop(eid, None)

            self._queue.append(job_id)
            self._branch_map[job_id] = branch
            self._not_empty.notify()
            return self._position_unlocked(job_id)

    def dequeue(self, timeout: float | None = None) -> str | None:
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._not_empty:
            while not self._queue:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._not_empty.wait(timeout=remaining)
                else:
                    self._not_empty.wait()
            job_id = self._queue.popleft()
            self._running = job_id
            return job_id

    def complete(self, job_id: str) -> None:
        with self._not_empty:
            if self._running == job_id:
                self._running = None
            self._branch_map.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        with self._not_empty:
            if job_id in self._queue:
                self._queue.remove(job_id)
                self._branch_map.pop(job_id, None)
                return True
            if job_id == self._running:
                if self._cancel_callback:
                    self._cancel_callback(job_id)
                return True
        return False

    def position(self, job_id: str) -> int:
        with self._lock:
            return self._position_unlocked(job_id)

    def _position_unlocked(self, job_id: str) -> int:
        try:
            return list(self._queue).index(job_id)
        except ValueError:
            return -1

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def running_job_id(self) -> str | None:
        with self._lock:
            return self._running

    def _find_same_branch(self, branch: str) -> list[str]:
        return [jid for jid, b in self._branch_map.items() if b == branch]
