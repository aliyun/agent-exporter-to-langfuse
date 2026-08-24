"""Tests for src.ingestor — validate_otlp, _accumulate_tokens, ingest, recover_failed."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.ingestor import (
    IngestError, MAX_BODY_BYTES, OTLP_CHUNK_BYTES,
    _accumulate_tokens, _otlp_body_bytes, _split_otlp, ingest, recover_failed, validate_otlp,
)
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


def _otlp_with_many_spans(n_children: int = 20) -> dict:
    """Return a valid single-resource OTLP body with one root + n child spans.

    Each child carries a ~200-byte attribute so a modest n exceeds a small
    chunk budget without building multi-megabyte test data.
    """
    root = {
        "traceId": "a" * 32,
        "spanId": "b" * 16,
        "name": "root",
        "startTimeUnixNano": "1718000000000000000",
        "endTimeUnixNano": "1718000001000000000",
    }
    children = []
    for i in range(n_children):
        children.append({
            "traceId": "a" * 32,
            "spanId": f"{(i + 1):016x}",
            "parentSpanId": "b" * 16,
            "name": f"gen-{i}",
            "startTimeUnixNano": "1718000000000000000",
            "endTimeUnixNano": "1718000001000000000",
            "attributes": [
                {"key": "langfuse.observation.type", "value": {"stringValue": "generation"}},
                {"key": "langfuse.observation.output", "value": {"stringValue": "z" * 200}},
            ],
        })
    return {
        "resourceSpans": [{
            "scopeSpans": [{
                "scope": {"name": "agent-exporter-to-langfuse"},
                "spans": [root, *children],
            }],
        }],
    }


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
        assert not (failed_dir / "2024-01-01.jsonl").exists()

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
        assert any("recover drop" in r.message for r in caplog.records)
        # invalid traces are dropped (file removed) so they don't spam forever
        assert not (failed_dir / "2024-01-01.jsonl").exists()

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
        assert any("recover drop" in r.message for r in caplog.records)
        assert not (failed_dir / "2024-01-01.jsonl").exists()

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

    def test_does_not_re_recover_handled_lines(self, tmp_path: Path) -> None:
        """A recovered line must be removed so it is not re-ingested next cycle.

        Regression for the duplicate re-ingestion bug: previously, any
        permanently-failed line in a file kept ok=False, so every recoverable
        line in that file was re-ingested each cycle -> duplicate pending lines
        and duplicate Langfuse deliveries.
        """
        data_dir = tmp_path / "data"
        failed_dir = data_dir / "failed"
        failed_dir.mkdir(parents=True)
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        (failed_dir / "2024-01-01.jsonl").write_text(json.dumps(_valid_otlp()) + "\n")

        n1 = recover_failed(data_dir, state, state_path)
        assert n1 == 1
        assert not (failed_dir / "2024-01-01.jsonl").exists()
        # second cycle: nothing left to recover, no duplicates produced
        n2 = recover_failed(data_dir, state, state_path)
        assert n2 == 0

    def test_mixed_file_recovers_all_and_removes_file(self, tmp_path: Path, monkeypatch) -> None:
        """A file with one small trace + one oversized trace: the small one is
        ingested whole and the oversized one is split, then the file is removed
        (no permanent skip, no duplicate re-ingestion next cycle).
        """
        monkeypatch.setattr("src.ingestor.OTLP_CHUNK_BYTES", 4000)
        data_dir = tmp_path / "data"
        failed_dir = data_dir / "failed"
        failed_dir.mkdir(parents=True)
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        small = json.dumps(_valid_otlp())
        big = json.dumps(_otlp_with_many_spans(n_children=30))
        (failed_dir / "2024-01-01.jsonl").write_text(small + "\n" + big + "\n")

        n = recover_failed(data_dir, state, state_path)
        # small -> 1 line; big -> multiple chunks; total > 1
        assert n > 1
        assert not (failed_dir / "2024-01-01.jsonl").exists()
        # each written pending line is a valid OTLP body with a root span
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pending = data_dir / "pending" / f"{today}.jsonl"
        assert pending.exists()
        for line in pending.read_text(encoding="utf-8").splitlines():
            body = json.loads(line)
            roots = [
                s for rs in body.get("resourceSpans", [])
                for ss in rs.get("scopeSpans", [])
                for s in ss.get("spans", [])
                if not s.get("parentSpanId")
            ]
            assert len(roots) == 1, "each split chunk must carry exactly one root span"
        # second cycle: file gone, no duplicates
        assert recover_failed(data_dir, state, state_path) == 0

    def test_split_otlp_chunks_each_under_budget_with_root(self) -> None:
        body = _otlp_with_many_spans(n_children=20)
        budget = 3000
        chunks = _split_otlp(body, budget)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert _otlp_body_bytes(chunk) <= budget
            roots = [
                s for rs in chunk["resourceSpans"]
                for ss in rs["scopeSpans"]
                for s in ss["spans"]
                if not s.get("parentSpanId")
            ]
            assert len(roots) == 1
            validate_otlp(chunk)
        # every original child span appears exactly once across chunks
        original_child_ids = {
            s["spanId"] for s in body["resourceSpans"][0]["scopeSpans"][0]["spans"]
            if s.get("parentSpanId")
        }
        chunk_child_ids: list[str] = []
        for c in chunks:
            for s in c["resourceSpans"][0]["scopeSpans"][0]["spans"]:
                if s.get("parentSpanId"):
                    chunk_child_ids.append(s["spanId"])
        assert set(chunk_child_ids) == original_child_ids
        assert len(chunk_child_ids) == len(original_child_ids)

    def test_split_returns_empty_for_unsplittable_root_too_big(self) -> None:
        """A body whose root span alone exceeds the budget cannot be split."""
        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
            {"key": "big", "value": {"stringValue": "x" * 5000}}
        ]
        assert _split_otlp(body, 1000) == []

    def test_split_returns_empty_for_multi_resource(self) -> None:
        body = _valid_otlp()
        body["resourceSpans"] = body["resourceSpans"] + body["resourceSpans"]
        assert _split_otlp(body, 1_000_000) == []

    def test_recovers_oversized_without_split_returns_empty_handled_as_drop(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """An oversized trace whose root alone exceeds the chunk budget is dropped
        once (not retried forever) and the file is removed."""
        monkeypatch.setattr("src.ingestor.OTLP_CHUNK_BYTES", 1000)
        data_dir = tmp_path / "data"
        failed_dir = data_dir / "failed"
        failed_dir.mkdir(parents=True)
        state_path = tmp_path / "ingest.json"
        state = IngestState()

        body = _valid_otlp()
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
            {"key": "big", "value": {"stringValue": "x" * 5000}}
        ]
        (failed_dir / "2024-01-01.jsonl").write_text(json.dumps(body) + "\n")

        with caplog.at_level(logging.WARNING):
            n = recover_failed(data_dir, state, state_path)

        assert n == 0
        assert any("cannot be split" in r.message for r in caplog.records)
        assert not (failed_dir / "2024-01-01.jsonl").exists()
