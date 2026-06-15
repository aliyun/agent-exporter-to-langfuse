"""Tests for src.ingestor — validate_trace, _accumulate_tokens, ingest."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.ingestor import IngestError, MAX_BODY_BYTES, _accumulate_tokens, ingest, validate_trace
from src.state import IngestState


def _valid_body(**overrides):
    """Return a minimal valid trace body, with optional overrides."""
    base = {
        "schema_version": "1",
        "source": "test-agent",
        "session_id": "sess-001",
        "trace": {
            "name": "test-trace",
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-01T00:00:01Z",
        },
        "generations": [
            {
                "name": "gen-1",
                "model": "gpt-4",
                "start_time": "2024-01-01T00:00:00Z",
                "end_time": "2024-01-01T00:00:01Z",
                "usage": {"input": 100, "output": 50},
            },
        ],
    }
    base.update(overrides)
    return base


class TestValidateTrace:
    def test_valid_body_passes(self) -> None:
        validate_trace(_valid_body())  # should not raise

    @pytest.mark.parametrize("field", ["schema_version", "source", "session_id"])
    def test_missing_top_level_field(self, field: str) -> None:
        body = _valid_body()
        del body[field]
        with pytest.raises(IngestError) as exc_info:
            validate_trace(body)
        assert exc_info.value.status == 422
        assert field in exc_info.value.message

    def test_missing_trace(self) -> None:
        body = _valid_body()
        del body["trace"]
        with pytest.raises(IngestError, match="trace"):
            validate_trace(body)

    def test_missing_trace_name(self) -> None:
        body = _valid_body()
        del body["trace"]["name"]
        with pytest.raises(IngestError, match="trace.name"):
            validate_trace(body)

    def test_empty_generations(self) -> None:
        body = _valid_body(generations=[])
        with pytest.raises(IngestError, match="generations"):
            validate_trace(body)

    def test_generations_not_list(self) -> None:
        body = _valid_body(generations="not-a-list")
        with pytest.raises(IngestError, match="generations"):
            validate_trace(body)


class TestAccumulateTokens:
    def test_accumulates_on_same_day(self) -> None:
        state = IngestState(tokens_date="2024-01-01", tokens_input=50, tokens_output=20)
        body = {
            "generations": [
                {"usage": {"input": 10, "output": 5, "cache_read_input_tokens": 3}},
                {"usage": {"input": 20, "output": 10}},
            ],
        }
        _accumulate_tokens(state, body, "2024-01-01")
        assert state.tokens_input == 80   # 50 + 10 + 20
        assert state.tokens_output == 35  # 20 + 5 + 10
        assert state.tokens_cache_read == 3

    def test_resets_on_new_day(self) -> None:
        state = IngestState(
            tokens_date="2024-01-01",
            tokens_input=999,
            tokens_output=999,
            tokens_cache_read=999,
            tokens_cache_creation=999,
        )
        body = {
            "generations": [
                {"usage": {"input": 10, "output": 5}},
            ],
        }
        _accumulate_tokens(state, body, "2024-01-02")
        assert state.tokens_date == "2024-01-02"
        assert state.tokens_input == 10
        assert state.tokens_output == 5
        assert state.tokens_cache_read == 0
        assert state.tokens_cache_creation == 0

    def test_no_usage_key(self) -> None:
        state = IngestState(tokens_date="2024-01-01", tokens_input=5)
        body = {"generations": [{"model": "gpt-4"}]}
        _accumulate_tokens(state, body, "2024-01-01")
        assert state.tokens_input == 5  # unchanged


class TestIngest:
    def test_writes_to_pending_file(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        fixed_dt = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        with patch("src.ingestor.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            seq_id = ingest(_valid_body(), state, data_dir, state_path)

        assert seq_id == 1
        pending_file = data_dir / "pending" / "2024-06-15.jsonl"
        assert pending_file.exists()
        line = pending_file.read_text().strip()
        record = json.loads(line)
        assert record["_seq_id"] == 1
        assert "_received_at" in record

    def test_updates_state(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        fixed_dt = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        with patch("src.ingestor.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            ingest(_valid_body(), state, data_dir, state_path)

        assert state.next_seq_id == 2
        assert "2024-06-15.jsonl" in state.files
        assert state.tokens_input == 100
        assert state.tokens_output == 50

    def test_rejects_oversized_payload(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        body = _valid_body()
        # Make payload exceed MAX_BODY_BYTES
        body["big_field"] = "x" * (MAX_BODY_BYTES + 1)

        with pytest.raises(IngestError) as exc_info:
            ingest(body, state, data_dir, state_path)
        assert exc_info.value.status == 413
        # seq_id should be rolled back
        assert state.next_seq_id == 1

    def test_rejects_invalid_trace(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        body = {"not": "valid"}
        with pytest.raises(IngestError) as exc_info:
            ingest(body, state, data_dir, state_path)
        assert exc_info.value.status == 422
