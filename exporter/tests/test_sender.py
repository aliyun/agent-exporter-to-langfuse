"""Tests for src.sender — _build_ingestion_batch, _read_pending_traces."""

import json
from pathlib import Path

from src.sender import _build_ingestion_batch, _read_pending_traces
from src.state import FileEntry, IngestState


def _make_trace(trace_id="t1", session_id="s1", name="test"):
    return {
        "id": trace_id,
        "schema_version": "1",
        "source": "test",
        "session_id": session_id,
        "trace": {
            "name": name,
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


class TestBuildIngestionBatch:
    def test_single_trace_produces_trace_and_generation(self) -> None:
        trace = _make_trace()
        result = _build_ingestion_batch([trace])
        batch = result["batch"]

        types = [item["type"] for item in batch]
        assert types.count("trace-create") == 1
        assert types.count("generation-create") == 1

    def test_trace_body_structure(self) -> None:
        trace = _make_trace(trace_id="abc", session_id="sess-1", name="my-trace")
        result = _build_ingestion_batch([trace])
        batch = result["batch"]

        trace_item = [b for b in batch if b["type"] == "trace-create"][0]
        assert trace_item["body"]["id"] == "abc"
        assert trace_item["body"]["sessionId"] == "sess-1"
        assert trace_item["body"]["name"] == "my-trace"

    def test_generation_body_structure(self) -> None:
        trace = _make_trace()
        result = _build_ingestion_batch([trace])
        batch = result["batch"]

        gen_item = [b for b in batch if b["type"] == "generation-create"][0]
        assert gen_item["body"]["model"] == "gpt-4"
        assert gen_item["body"]["traceId"] == "t1"
        assert gen_item["body"]["usage"] == {"input": 100, "output": 50}

    def test_multiple_traces(self) -> None:
        traces = [_make_trace(trace_id=f"t{i}") for i in range(3)]
        result = _build_ingestion_batch(traces)
        batch = result["batch"]
        trace_items = [b for b in batch if b["type"] == "trace-create"]
        assert len(trace_items) == 3

    def test_span_linked_to_generation(self) -> None:
        trace = _make_trace()
        trace["spans"] = [
            {
                "generation_index": 0,
                "name": "tool-call",
                "start_time": "2024-01-01T00:00:00Z",
                "end_time": "2024-01-01T00:00:01Z",
            },
        ]
        result = _build_ingestion_batch([trace])
        batch = result["batch"]
        span_items = [b for b in batch if b["type"] == "span-create"]
        assert len(span_items) == 1
        assert span_items[0]["body"]["name"] == "tool-call"
        # parent should be the generation id
        gen_item = [b for b in batch if b["type"] == "generation-create"][0]
        assert span_items[0]["body"]["parentObservationId"] == gen_item["body"]["id"]

    def test_empty_input(self) -> None:
        result = _build_ingestion_batch([])
        assert result == {"batch": []}


class TestReadPendingTraces:
    def test_reads_uncommitted_traces(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        filename = "2024-01-01.jsonl"
        lines = []
        for i in range(1, 4):
            lines.append(json.dumps({"_seq_id": i, "data": f"trace-{i}"}))
        (pending_dir / filename).write_text("\n".join(lines) + "\n")

        state = IngestState(
            next_seq_id=4,
            files={filename: FileEntry(min_seq=1, max_seq=3)},
        )
        # commit_id=1 means seq 2 and 3 are pending
        traces = _read_pending_traces(tmp_path, state, commit_id=1, batch_size=10)
        seq_ids = [t["_seq_id"] for t in traces]
        assert seq_ids == [2, 3]

    def test_respects_batch_size(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        filename = "2024-01-01.jsonl"
        lines = [json.dumps({"_seq_id": i}) for i in range(1, 11)]
        (pending_dir / filename).write_text("\n".join(lines) + "\n")

        state = IngestState(
            next_seq_id=11,
            files={filename: FileEntry(min_seq=1, max_seq=10)},
        )
        traces = _read_pending_traces(tmp_path, state, commit_id=0, batch_size=3)
        assert len(traces) == 3

    def test_filters_committed(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        filename = "2024-01-01.jsonl"
        lines = [json.dumps({"_seq_id": i}) for i in range(1, 6)]
        (pending_dir / filename).write_text("\n".join(lines) + "\n")

        state = IngestState(
            next_seq_id=6,
            files={filename: FileEntry(min_seq=1, max_seq=5)},
        )
        # Everything committed
        traces = _read_pending_traces(tmp_path, state, commit_id=5, batch_size=10)
        assert traces == []

    def test_no_pending_dir(self, tmp_path: Path) -> None:
        state = IngestState()
        traces = _read_pending_traces(tmp_path, state, commit_id=0, batch_size=10)
        assert traces == []

    def test_skips_missing_files(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        # File in state but not on disk
        state = IngestState(
            next_seq_id=5,
            files={"2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=4)},
        )
        traces = _read_pending_traces(tmp_path, state, commit_id=0, batch_size=10)
        assert traces == []
