"""Tests for src.ingestor — validate_otlp, _accumulate_tokens, ingest, recover_failed."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.ingestor import IngestError, MAX_BODY_BYTES, _accumulate_tokens, ingest, validate_otlp, recover_failed
from src.state import IngestState


def _valid_otlp(**overrides):
    """Return a minimal valid OTLP JSON body."""
    base = {
        "resourceSpans": [{
            "scopeSpans": [{
                "scope": {"name": "agent-exporter-to-langfuse"},
                "spans": [{
                    "traceId": "a" * 32,
                    "spanId": "b" * 16,
                    "name": "test-trace",
                    "startTimeUnixNano": "1718000000000000000",
                    "endTimeUnixNano": "1718000001000000000",
                }],
            }],
        }],
    }
    base.update(overrides)
    return base


def _span_with_usage(input_tokens=100, output_tokens=50, cache_read=0, cache_creation=0):
    """Return a span with langfuse.observation.usage_details attribute."""
    usage = {"input": input_tokens, "output": output_tokens}
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    if cache_creation:
        usage["cache_creation_input_tokens"] = cache_creation
    return {
        "traceId": "a" * 32,
        "spanId": "c" * 16,
        "parentSpanId": "b" * 16,
        "name": "generation",
        "startTimeUnixNano": "1718000000000000000",
        "endTimeUnixNano": "1718000001000000000",
        "attributes": [
            {"key": "langfuse.observation.type", "value": {"stringValue": "generation"}},
            {"key": "langfuse.observation.usage_details", "value": {"stringValue": json.dumps(usage)}},
        ],
    }


class TestValidateOtlp:
    def test_valid_body_passes(self) -> None:
        validate_otlp(_valid_otlp())

    def test_missing_resource_spans(self) -> None:
        with pytest.raises(IngestError) as exc_info:
            validate_otlp({})
        assert exc_info.value.status == 422
        assert "resourceSpans" in exc_info.value.message

    def test_empty_resource_spans(self) -> None:
        with pytest.raises(IngestError, match="resourceSpans"):
            validate_otlp({"resourceSpans": []})

    def test_missing_scope_spans(self) -> None:
        with pytest.raises(IngestError, match="scopeSpans"):
            validate_otlp({"resourceSpans": [{}]})

    def test_empty_spans(self) -> None:
        with pytest.raises(IngestError, match="empty spans"):
            validate_otlp({"resourceSpans": [{"scopeSpans": [{"spans": []}]}]})

    def test_trace_id_not_hex(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "zzzz" * 8
        with pytest.raises(IngestError, match="traceId"):
            validate_otlp(body)

    def test_trace_id_wrong_length(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "a" * 16
        with pytest.raises(IngestError, match="traceId"):
            validate_otlp(body)

    def test_span_id_not_hex(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "ZZZZ" * 4
        with pytest.raises(IngestError, match="spanId"):
            validate_otlp(body)

    def test_span_id_wrong_length(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "b" * 8
        with pytest.raises(IngestError, match="spanId"):
            validate_otlp(body)

    def test_missing_name(self) -> None:
        body = _valid_otlp()
        del body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"]
        with pytest.raises(IngestError, match="name"):
            validate_otlp(body)

    def test_empty_name(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] = ""
        with pytest.raises(IngestError, match="name"):
            validate_otlp(body)

    def test_missing_start_time(self) -> None:
        body = _valid_otlp()
        del body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["startTimeUnixNano"]
        with pytest.raises(IngestError, match="startTimeUnixNano"):
            validate_otlp(body)

    def test_end_time_less_than_start(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["endTimeUnixNano"] = "1717000000000000000"
        with pytest.raises(IngestError, match="endTimeUnixNano < startTimeUnixNano"):
            validate_otlp(body)

    def test_no_root_span(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["parentSpanId"] = "c" * 16
        with pytest.raises(IngestError, match="no root span"):
            validate_otlp(body)

    def test_invalid_attributes_not_list(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = "not-a-list"
        with pytest.raises(IngestError, match="attributes"):
            validate_otlp(body)

    def test_invalid_attribute_missing_key(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [{"value": {}}]
        with pytest.raises(IngestError, match="key and value"):
            validate_otlp(body)

    def test_valid_with_attributes(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
            {"key": "session.id", "value": {"stringValue": "sess-1"}},
        ]
        validate_otlp(body)


class TestAccumulateTokens:
    def test_accumulates_from_span_attributes(self) -> None:
        from src.state import FileEntry
        state = IngestState(files={"2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=1)})
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"].append(
            _span_with_usage(input_tokens=100, output_tokens=50, cache_read=10)
        )
        _accumulate_tokens(state, body, "2024-01-01.jsonl")
        fe = state.files["2024-01-01.jsonl"]
        assert fe.input == 100
        assert fe.output == 50
        assert fe.cache_read == 10

    def test_accumulates_across_calls(self) -> None:
        from src.state import FileEntry
        state = IngestState(files={
            "2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=1, input=100, output=50),
        })
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"].append(
            _span_with_usage(input_tokens=10, output_tokens=5)
        )
        _accumulate_tokens(state, body, "2024-01-01.jsonl")
        fe = state.files["2024-01-01.jsonl"]
        assert fe.input == 110
        assert fe.output == 55

    def test_separate_days(self) -> None:
        from src.state import FileEntry
        state = IngestState(files={
            "2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=1, input=999),
            "2024-01-02.jsonl": FileEntry(min_seq=2, max_seq=2),
        })
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"].append(
            _span_with_usage(input_tokens=10, output_tokens=5)
        )
        _accumulate_tokens(state, body, "2024-01-02.jsonl")
        assert state.files["2024-01-01.jsonl"].input == 999
        assert state.files["2024-01-02.jsonl"].input == 10
        assert state.files["2024-01-02.jsonl"].output == 5

    def test_no_usage_attribute(self) -> None:
        from src.state import FileEntry
        state = IngestState(files={"2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=1, input=5)})
        body = _valid_otlp()
        _accumulate_tokens(state, body, "2024-01-01.jsonl")
        assert state.files["2024-01-01.jsonl"].input == 5

    def test_no_file_entry_does_nothing(self) -> None:
        state = IngestState()
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"].append(
            _span_with_usage(input_tokens=100, output_tokens=50)
        )
        _accumulate_tokens(state, body, "missing.jsonl")


class TestIngest:
    def test_writes_to_pending_file(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        fixed_dt = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        with patch("src.ingestor.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            seq_id = ingest(_valid_otlp(), state, data_dir, state_path)

        assert seq_id == 1
        pending_file = data_dir / "pending" / "2024-06-15.jsonl"
        assert pending_file.exists()
        line = pending_file.read_text().strip()
        record = json.loads(line)
        assert record["_seq_id"] == 1
        assert "_received_at" in record

    def test_rejects_invalid_otlp(self, tmp_path: Path) -> None:
        state = IngestState()
        with pytest.raises(IngestError) as exc_info:
            ingest({"not": "valid"}, state, tmp_path / "data", tmp_path / "ingest.json")
        assert exc_info.value.status == 422


class TestRecoverFailed:
    def test_skips_json_decode_error(self, tmp_path: Path, caplog) -> None:
        data_dir = tmp_path / "data"
        failed_dir = data_dir / "failed"
        failed_dir.mkdir(parents=True)
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        (failed_dir / "2024-01-01.jsonl").write_text("not valid json\n")

        with caplog.at_level(logging.WARNING):
            n = recover_failed(data_dir, state, state_path)

        assert n == 0
        assert any("JSONDecodeError" in r.message for r in caplog.records)

    def test_skips_old_format_trace_json_v2(self, tmp_path: Path, caplog) -> None:
        data_dir = tmp_path / "data"
        failed_dir = data_dir / "failed"
        failed_dir.mkdir(parents=True)
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        old_trace = json.dumps({
            "schema_version": "2", "source": "test",
            "session_id": "s1", "trace": {"name": "t", "start_time": "", "end_time": ""},
            "generations": [{"name": "g"}],
        })
        (failed_dir / "2024-01-01.jsonl").write_text(old_trace + "\n")

        with caplog.at_level(logging.WARNING):
            n = recover_failed(data_dir, state, state_path)

        assert n == 0
        assert any("recover skip" in r.message for r in caplog.records)

    def test_skips_validate_otlp_failure(self, tmp_path: Path, caplog) -> None:
        data_dir = tmp_path / "data"
        failed_dir = data_dir / "failed"
        failed_dir.mkdir(parents=True)
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        bad_otlp = json.dumps({"resourceSpans": []})
        (failed_dir / "2024-01-01.jsonl").write_text(bad_otlp + "\n")

        with caplog.at_level(logging.WARNING):
            n = recover_failed(data_dir, state, state_path)

        assert n == 0
        assert any("recover skip" in r.message for r in caplog.records)

    def test_recovers_valid_otlp(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        failed_dir = data_dir / "failed"
        failed_dir.mkdir(parents=True)
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        valid = json.dumps(_valid_otlp())
        (failed_dir / "2024-01-01.jsonl").write_text(valid + "\n")

        n = recover_failed(data_dir, state, state_path)
        assert n == 1
        assert not (failed_dir / "2024-01-01.jsonl").exists()
