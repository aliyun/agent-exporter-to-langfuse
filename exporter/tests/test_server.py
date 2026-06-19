"""Tests for src.server — FastAPI endpoints via TestClient."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from src.config import Config, LangfuseConfig, UpdateConfig
from src.server import create_app
from src.state import IngestState, LastError, SenderState
from src.stats import Stats


@pytest.fixture()
def app_env(tmp_path: Path):
    """Create a minimal app environment and return (client, config, ingest_state, sender_state)."""
    config = Config()
    config.storage.data_dir = str(tmp_path / "data")
    config.langfuse.public_key = "pk-test"
    config.langfuse.secret_key = "sk-test"

    ingest_state = IngestState()
    sender_state = SenderState()
    stats = Stats()

    ingest_state_path = tmp_path / "ingest.json"
    sender_state_path = tmp_path / "sender.json"

    app = create_app(
        config=config,
        ingest_state=ingest_state,
        ingest_state_path=ingest_state_path,
        sender_state=sender_state,
        sender_state_path=sender_state_path,
        stats=stats,
    )
    client = TestClient(app, raise_server_exceptions=False)
    return client, config, ingest_state, sender_state


def _valid_trace():
    return {
        "resourceSpans": [{
            "scopeSpans": [{
                "scope": {"name": "agent-exporter-to-langfuse"},
                "spans": [{
                    "traceId": "a" * 32,
                    "spanId": "b" * 16,
                    "name": "test",
                    "startTimeUnixNano": "1718000000000000000",
                    "endTimeUnixNano": "1718000001000000000",
                }],
            }],
        }],
    }


class TestPostIngest:
    def test_accepts_valid_trace(self, app_env) -> None:
        client, _, ingest_state, _ = app_env
        resp = client.post("/ingest", json=_valid_trace())
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["seq_id"] == 1
        assert ingest_state.next_seq_id == 2

    def test_rejects_invalid_json(self, app_env) -> None:
        client, *_ = app_env
        resp = client.post(
            "/ingest",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_rejects_missing_fields(self, app_env) -> None:
        client, *_ = app_env
        resp = client.post("/ingest", json={"foo": "bar"})
        assert resp.status_code == 422
        assert resp.json()["status"] == "rejected"

    def test_rejects_oversized_body(self, app_env) -> None:
        client, *_ = app_env
        huge = b"x" * (10 * 1024 * 1024 + 1)
        resp = client.post(
            "/ingest",
            content=huge,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413


class TestPostIngestBatch:
    def test_processes_multiple_traces(self, app_env) -> None:
        client, _, ingest_state, _ = app_env
        traces = [_valid_trace() for _ in range(3)]
        resp = client.post("/ingest/batch", json={"traces": traces})
        assert resp.status_code == 202
        data = resp.json()
        assert data["count"] == 3
        assert len(data["seq_ids"]) == 3

    def test_rejects_non_list(self, app_env) -> None:
        client, *_ = app_env
        resp = client.post("/ingest/batch", json={"traces": "not a list"})
        assert resp.status_code == 422

    def test_skips_invalid_in_batch(self, app_env) -> None:
        client, *_ = app_env
        traces = [_valid_trace(), {"bad": "trace"}, _valid_trace()]
        resp = client.post("/ingest/batch", json={"traces": traces})
        assert resp.status_code == 202
        data = resp.json()
        assert data["count"] == 2  # one skipped


class TestGetStats:
    def test_returns_correct_structure(self, app_env) -> None:
        client, _, ingest_state, sender_state = app_env
        ingest_state.next_seq_id = 11  # 10 traces
        sender_state.commit_id = 7

        resp = client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_traces"] == 10
        assert data["total_sent"] == 7
        assert data["pending_count"] == 3
        assert "tokens_today" in data
        assert "storage_used_mb" in data
        assert "uptime_seconds" in data

    def test_handles_error_in_stats(self, app_env) -> None:
        client, _, _, sender_state = app_env
        sender_state.last_error = LastError(
            time="2024-01-01T00:00:00Z", seq_id=5, error="HTTP 500", retries=2,
        )
        resp = client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["last_error"]["error"] == "HTTP 500"
        assert data["last_error"]["retries"] == 2


class TestGetHealth:
    def test_healthy_with_credentials(self, app_env) -> None:
        client, *_ = app_env
        with patch("src.updater._read_local_version", return_value="1.0.0"):
            resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["langfuse_configured"] is True

    def test_unhealthy_without_credentials(self, tmp_path: Path) -> None:
        config = Config()
        config.storage.data_dir = str(tmp_path / "data")
        # No langfuse keys
        app = create_app(
            config=config,
            ingest_state=IngestState(),
            ingest_state_path=tmp_path / "i.json",
            sender_state=SenderState(),
            sender_state_path=tmp_path / "s.json",
            stats=Stats(),
        )
        client = TestClient(app, raise_server_exceptions=False)
        with patch("src.updater._read_local_version", return_value="0.0.0"):
            resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "no_credentials"


class TestSettings:
    def test_get_settings(self, app_env) -> None:
        client, config, *_ = app_env
        config.update.include_prerelease = True
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert resp.json()["include_prerelease"] is True

    def test_post_settings_updates_config(self, app_env) -> None:
        client, config, *_ = app_env
        assert config.update.include_prerelease is False
        with patch("src.server.set_config_value"):
            resp = client.post("/settings", json={"include_prerelease": True})
        assert resp.status_code == 200
        assert config.update.include_prerelease is True

    def test_post_settings_invalid_json(self, app_env) -> None:
        client, *_ = app_env
        resp = client.post(
            "/settings",
            content=b"bad",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422


class TestRetryHooks:
    def test_retry_all_agents(self, app_env) -> None:
        client, *_ = app_env
        with patch("src.server.start_upgrade", return_value=True) as mock:
            resp = client.post("/upgrade/retry-hooks")
        assert resp.status_code == 200
        assert resp.json()["scope"] == "all"
        mock.assert_called_once_with(retry_hooks=True, retry_agent=None)

    def test_retry_specific_agent(self, app_env) -> None:
        client, *_ = app_env
        with patch("src.server.start_upgrade", return_value=True) as mock:
            resp = client.post("/upgrade/retry-hooks?agent=cursor")
        assert resp.status_code == 200
        assert resp.json()["scope"] == "cursor"
        mock.assert_called_once_with(retry_hooks=True, retry_agent="cursor")

    def test_retry_hooks_installer_not_found(self, app_env) -> None:
        client, *_ = app_env
        with patch("src.server.start_upgrade", return_value=False):
            resp = client.post("/upgrade/retry-hooks?agent=cursor")
        assert resp.status_code == 500
        assert resp.json()["status"] == "error"
