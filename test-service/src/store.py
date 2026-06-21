import json
import random
import sqlite3
import string
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def generate_job_id() -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"e2e-{today}-{suffix}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    branch TEXT NOT NULL,
    commit_sha TEXT,
    mode TEXT NOT NULL DEFAULT 'branch',
    test_command TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 1800,
    callback_url TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_seconds REAL,
    exit_code INTEGER,
    progress TEXT,
    summary TEXT,
    output_tail TEXT,
    same_branch_policy TEXT
);
"""


class Store:
    def __init__(self, db_path: str, log_dir: str):
        self._db_path = db_path
        self._log_dir = log_dir
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def create_job(self, *, branch: str, commit: str | None = None,
                   mode: str = "branch", test_command: str | None = None,
                   timeout_seconds: int = 1800, callback_url: str | None = None,
                   metadata: dict | None = None) -> dict[str, Any]:
        job_id = generate_job_id()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (job_id, status, branch, commit_sha, mode, test_command,
                    timeout_seconds, callback_url, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, "pending", branch, commit, mode, test_command,
                 timeout_seconds, callback_url,
                 json.dumps(metadata) if metadata else None, now),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_jobs(self, *, status: str | None = None, branch: str | None = None,
                  limit: int = 20) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if branch:
            clauses.append("branch = ?")
            params.append(branch)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs{where} ORDER BY created_at DESC LIMIT ?", params,
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status", "started_at", "finished_at", "duration_seconds",
            "exit_code", "progress", "summary", "output_tail", "commit_sha",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        for k in ("progress", "summary", "metadata"):
            if k in updates and isinstance(updates[k], (dict, list)):
                updates[k] = json.dumps(updates[k])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [job_id]
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {set_clause} WHERE job_id = ?", values)

    def find_pending_or_running_by_branch(self, branch: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE branch = ? AND status IN ('pending', 'running') ORDER BY created_at",
                (branch,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_pending(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'").fetchone()
        return row[0] if row else 0

    def cleanup_expired(self, retention_days: int) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT job_id FROM jobs
                   WHERE status NOT IN ('pending', 'running')
                   AND created_at < ?""",
                (cutoff_iso,),
            ).fetchall()
            deleted = 0
            for row in rows:
                jid = row[0]
                conn.execute("DELETE FROM jobs WHERE job_id = ?", (jid,))
                log_file = Path(self._log_dir) / f"{jid}.log"
                if log_file.exists():
                    log_file.unlink(missing_ok=True)
                deleted += 1
        return deleted

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("progress", "summary", "metadata"):
            if k in d and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        if "commit_sha" in d:
            d["commit"] = d.pop("commit_sha")
        return d
