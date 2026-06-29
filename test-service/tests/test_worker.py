import subprocess
import time
from pathlib import Path

import pytest

from src import worker as worker_mod
from src.config import Config
from src.queue import JobQueue
from src.store import Store
from src.worker import Worker


class FakeGit:
    def __init__(self, worktree_root, remove_raises=False):
        self._root = Path(worktree_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._remove_raises = remove_raises

    def fetch(self):
        pass

    def create_worktree(self, job_id, branch, commit=None):
        wt = self._root / job_id
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    def merge_main(self, worktree_path):
        pass

    def remove_worktree(self, job_id):
        if self._remove_raises:
            raise RuntimeError("forced remove failure")

    def cleanup_all_worktrees(self):
        pass


def _make_worker(tmp_path, remove_raises=False):
    db = str(tmp_path / "t.db")
    log_dir = str(tmp_path / "logs")
    config = Config()
    config.storage.log_dir = log_dir
    store = Store(db, log_dir)
    queue = JobQueue(same_branch_policy="replace")
    git = FakeGit(tmp_path / "wt", remove_raises=remove_raises)
    return Worker(config, store, queue, git)


def _wait_for_terminal(store, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = store.get_job(job_id)
        if result and result["status"] in ("success", "failed", "timeout", "cancelled", "conflict"):
            return result
        time.sleep(0.05)
    return store.get_job(job_id)


def test_job_stays_success_when_cleanup_fails(tmp_path):
    """A passing job must keep status=success even if worktree cleanup raises.
    Previously the cleanup exception propagated out of the `finally` block into
    the run loop's error handler, which overwrote a successful job to `failed`
    (R-1)."""
    worker = _make_worker(tmp_path, remove_raises=True)
    store = worker._store
    queue = worker._queue
    worker.start()
    try:
        job = store.create_job(branch="feat/a", mode="branch", test_command="true")
        queue.enqueue(job["job_id"], "feat/a")
        result = _wait_for_terminal(store, job["job_id"])
    finally:
        worker.stop(timeout=5)

    assert result["status"] == "success"
    assert "remove failure" not in (result.get("output_tail") or "")


def test_resolve_uv_via_which(monkeypatch):
    monkeypatch.setattr(worker_mod.shutil, "which", lambda name: "/bin/uv")
    assert Worker._resolve_uv() == "/bin/uv"


def test_resolve_uv_falls_back_to_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_mod.shutil, "which", lambda name: None)
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\n")
    fake_uv.chmod(0o755)
    monkeypatch.setattr(worker_mod, "_UV_CANDIDATE_PATHS", [fake_uv])
    assert Worker._resolve_uv() == str(fake_uv)


def test_resolve_uv_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(worker_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(worker_mod, "_UV_CANDIDATE_PATHS", [])
    assert Worker._resolve_uv() is None


def test_maybe_uv_sync_skips_without_crashing_when_uv_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Worker, "_resolve_uv", staticmethod(lambda: None))

    def boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when uv is missing")

    monkeypatch.setattr(worker_mod.subprocess, "run", boom)
    worktree = tmp_path / "wt"
    (worktree / "exporter").mkdir(parents=True)
    (worktree / "exporter" / "pyproject.toml").write_text("[project]\nname='x'\n")

    worker = _make_worker(tmp_path)
    worker._maybe_uv_sync(worktree)  # must not raise / must not spawn subprocess


def test_maybe_uv_sync_uses_resolved_uv_path_not_bare_uv(monkeypatch, tmp_path):
    """When uv is resolved, sync must be invoked with the absolute path, not a
    bare 'uv' that relies on PATH lookup (R-3)."""
    monkeypatch.setattr(Worker, "_resolve_uv", staticmethod(lambda: "/path/to/uv"))

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(worker_mod.subprocess, "run", fake_run)
    worktree = tmp_path / "wt"
    (worktree / "exporter").mkdir(parents=True)
    (worktree / "exporter" / "pyproject.toml").write_text("[project]\nname='x'\n")

    worker = _make_worker(tmp_path)
    worker._maybe_uv_sync(worktree)

    assert calls == [["/path/to/uv", "sync"]]
