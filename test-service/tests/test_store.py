import tempfile
from pathlib import Path

import pytest

from src.store import Store, generate_job_id


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    log_dir = str(tmp_path / "logs")
    return Store(db_path, log_dir)


def test_generate_job_id_format():
    jid = generate_job_id()
    assert jid.startswith("e2e-")
    parts = jid.split("-")
    assert len(parts) == 3
    assert len(parts[1]) == 8  # YYYYMMDD
    assert len(parts[2]) == 6  # random suffix


def test_generate_job_id_unique():
    ids = {generate_job_id() for _ in range(100)}
    assert len(ids) == 100


def test_create_and_get(store):
    job = store.create_job(branch="feat/test")
    assert job["status"] == "pending"
    assert job["branch"] == "feat/test"
    assert job["mode"] == "branch"
    assert job["job_id"].startswith("e2e-")

    fetched = store.get_job(job["job_id"])
    assert fetched is not None
    assert fetched["branch"] == "feat/test"


def test_get_nonexistent(store):
    assert store.get_job("no-such-id") is None


def test_list_jobs(store):
    store.create_job(branch="feat/a")
    store.create_job(branch="feat/b")
    store.create_job(branch="feat/a")

    all_jobs = store.list_jobs()
    assert len(all_jobs) == 3

    a_jobs = store.list_jobs(branch="feat/a")
    assert len(a_jobs) == 2

    limited = store.list_jobs(limit=1)
    assert len(limited) == 1


def test_list_jobs_by_status(store):
    job = store.create_job(branch="feat/x")
    store.update_job(job["job_id"], status="running")

    pending = store.list_jobs(status="pending")
    assert len(pending) == 0

    running = store.list_jobs(status="running")
    assert len(running) == 1


def test_update_job(store):
    job = store.create_job(branch="feat/test")
    store.update_job(job["job_id"], status="running", started_at="2026-06-21T00:00:00Z")

    updated = store.get_job(job["job_id"])
    assert updated["status"] == "running"
    assert updated["started_at"] == "2026-06-21T00:00:00Z"


def test_update_job_progress_json(store):
    job = store.create_job(branch="feat/test")
    progress = {"phase": "testing", "passed": 3, "failed": 0, "total": 10}
    store.update_job(job["job_id"], progress=progress)

    updated = store.get_job(job["job_id"])
    assert updated["progress"]["passed"] == 3


def test_find_pending_or_running_by_branch(store):
    j1 = store.create_job(branch="feat/x")
    j2 = store.create_job(branch="feat/x")
    j3 = store.create_job(branch="feat/y")
    store.update_job(j1["job_id"], status="success")

    results = store.find_pending_or_running_by_branch("feat/x")
    assert len(results) == 1
    assert results[0]["job_id"] == j2["job_id"]


def test_count_pending(store):
    store.create_job(branch="feat/a")
    store.create_job(branch="feat/b")
    assert store.count_pending() == 2


def test_cleanup_expired(store):
    job = store.create_job(branch="feat/old")
    store.update_job(job["job_id"], status="success")

    log_file = Path(store._log_dir) / f"{job['job_id']}.log"
    log_file.write_text("test log")

    deleted = store.cleanup_expired(retention_days=0)
    assert deleted == 1
    assert store.get_job(job["job_id"]) is None
    assert not log_file.exists()


def test_cleanup_skips_running(store):
    job = store.create_job(branch="feat/active")
    store.update_job(job["job_id"], status="running")

    deleted = store.cleanup_expired(retention_days=0)
    assert deleted == 0
    assert store.get_job(job["job_id"]) is not None


def test_metadata_roundtrip(store):
    meta = {"agent_id": "agent-001", "task_id": "TASK-42"}
    job = store.create_job(branch="feat/test", metadata=meta)

    fetched = store.get_job(job["job_id"])
    assert fetched["metadata"] == meta
