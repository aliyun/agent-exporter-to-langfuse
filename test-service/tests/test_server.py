import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import Config, E2EConfig
from src.queue import JobQueue
from src.server import create_app
from src.store import Store


@pytest.fixture
def setup(tmp_path):
    db_path = str(tmp_path / "test.db")
    log_dir = str(tmp_path / "logs")
    config = Config()
    config.storage.log_dir = log_dir
    config.e2e = E2EConfig(same_branch_policy="replace")
    store = Store(db_path, log_dir)
    queue = JobQueue(same_branch_policy="replace")
    app = create_app(config, store=store, queue=queue)
    client = TestClient(app)
    return client, store, queue


def test_health(setup):
    client, _, _ = setup
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "version" in resp.json()


def test_create_job(setup):
    client, _, _ = setup
    resp = client.post("/e2e/jobs", json={"branch": "feat/test"})
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "pending"
    assert data["job_id"].startswith("e2e-")
    assert data["position"] == 0


def test_create_job_missing_branch(setup):
    client, _, _ = setup
    resp = client.post("/e2e/jobs", json={})
    assert resp.status_code == 400


def test_get_job(setup):
    client, _, _ = setup
    create_resp = client.post("/e2e/jobs", json={"branch": "feat/test"})
    job_id = create_resp.json()["job_id"]

    resp = client.get(f"/e2e/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["branch"] == "feat/test"


def test_get_job_not_found(setup):
    client, _, _ = setup
    resp = client.get("/e2e/jobs/no-such-id")
    assert resp.status_code == 404


def test_list_jobs(setup):
    client, _, _ = setup
    client.post("/e2e/jobs", json={"branch": "feat/a"})
    client.post("/e2e/jobs", json={"branch": "feat/b"})

    resp = client.get("/e2e/jobs")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_list_jobs_filter_branch(setup):
    client, _, _ = setup
    client.post("/e2e/jobs", json={"branch": "feat/a"})
    client.post("/e2e/jobs", json={"branch": "feat/b"})

    resp = client.get("/e2e/jobs?branch=feat/a")
    assert resp.status_code == 200
    for job in resp.json():
        assert job["branch"] == "feat/a"


def test_cancel_pending_job(setup):
    client, _, _ = setup
    create_resp = client.post("/e2e/jobs", json={"branch": "feat/test"})
    job_id = create_resp.json()["job_id"]

    resp = client.post(f"/e2e/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    get_resp = client.get(f"/e2e/jobs/{job_id}")
    assert get_resp.json()["status"] == "cancelled"


def test_cancel_not_found(setup):
    client, _, _ = setup
    resp = client.post("/e2e/jobs/no-such-id/cancel")
    assert resp.status_code == 404


def test_get_logs_empty(setup):
    client, store, _ = setup
    job = store.create_job(branch="feat/test")
    resp = client.get(f"/e2e/jobs/{job['job_id']}/logs")
    assert resp.status_code == 200
    assert resp.text == ""


def test_get_logs_not_found(setup):
    client, _, _ = setup
    resp = client.get("/e2e/jobs/no-such-id/logs")
    assert resp.status_code == 404


def test_reject_policy(tmp_path):
    db_path = str(tmp_path / "test.db")
    log_dir = str(tmp_path / "logs")
    config = Config()
    config.storage.log_dir = log_dir
    config.e2e.same_branch_policy = "reject"
    store = Store(db_path, log_dir)
    queue = JobQueue(same_branch_policy="reject")
    app = create_app(config, store=store, queue=queue)
    client = TestClient(app)

    resp1 = client.post("/e2e/jobs", json={"branch": "feat/x"})
    assert resp1.status_code == 202

    resp2 = client.post("/e2e/jobs", json={"branch": "feat/x"})
    assert resp2.status_code == 409


def test_metadata_roundtrip(setup):
    client, _, _ = setup
    meta = {"agent_id": "agent-001"}
    resp = client.post("/e2e/jobs", json={"branch": "feat/test", "metadata": meta})
    job_id = resp.json()["job_id"]

    get_resp = client.get(f"/e2e/jobs/{job_id}")
    assert get_resp.json()["metadata"] == meta


def test_get_logs_path_traversal(tmp_path):
    db_path = str(tmp_path / "test.db")
    log_dir = str(tmp_path / "logs")
    config = Config()
    config.storage.log_dir = log_dir
    config.e2e = E2EConfig(same_branch_policy="replace")
    store = Store(db_path, log_dir)
    queue = JobQueue(same_branch_policy="replace")
    app = create_app(config, store=store, queue=queue)

    handler = next(
        r.endpoint for r in app.routes
        if hasattr(r, "path") and r.path == "/e2e/jobs/{job_id}/logs"
    )
    result = asyncio.run(handler("../../secret"))
    assert result.status_code == 400
    assert result.body.decode() == "invalid job id"


@pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt",
    reason="symlink not available on this platform",
)
def test_get_logs_symlink_outside_base(tmp_path):
    db_path = str(tmp_path / "test.db")
    log_dir = tmp_path / "logs"
    config = Config()
    config.storage.log_dir = str(log_dir)
    config.e2e = E2EConfig(same_branch_policy="replace")
    store = Store(db_path, str(log_dir))
    queue = JobQueue(same_branch_policy="replace")
    app = create_app(config, store=store, queue=queue)
    client = TestClient(app)

    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("sensitive data")
    symlink_path = log_dir / "evil.log"
    os.symlink(str(secret_path), str(symlink_path))

    resp = client.get("/e2e/jobs/evil/logs")
    assert resp.status_code == 400
    assert resp.text == "invalid job id"
