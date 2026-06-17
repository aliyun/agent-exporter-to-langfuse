"""Tests for src.sender — _build_ingestion_batch, _read_pending_traces, Sender."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from src.config import LangfuseConfig, SenderConfig
from src.sender import (
    Sender,
    _build_ingestion_batch,
    _build_trace_items,
    _items_byte_size,
    _read_pending_traces,
    _split_into_batches,
    _write_to_failed,
)
from src.state import FileEntry, IngestState, SenderState, save_sender_state


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


def _make_large_trace(trace_id: str, seq_id: int, target_bytes: int) -> dict:
    padding = "x" * (target_bytes // 2)
    return {
        "id": trace_id,
        "schema_version": "1",
        "source": "test",
        "session_id": "s1",
        "_seq_id": seq_id,
        "trace": {
            "name": "test",
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-01T00:00:01Z",
            "input": padding,
        },
        "generations": [
            {
                "name": "gen-1",
                "model": "gpt-4",
                "start_time": "2024-01-01T00:00:00Z",
                "end_time": "2024-01-01T00:00:01Z",
                "input": padding,
            },
        ],
    }


def _setup_sender(tmp_path: Path, max_payload_bytes: int = 3_500_000) -> Sender:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_path = tmp_path / "sender.json"
    ingest_state_path = tmp_path / "ingest.json"

    langfuse_cfg = LangfuseConfig(
        public_key="pk-test", secret_key="sk-test", base_url="https://test.langfuse.com",
    )
    sender_cfg = SenderConfig(max_payload_bytes=max_payload_bytes)
    sender_state = SenderState()
    save_sender_state(state_path, sender_state)

    return Sender(langfuse_cfg, sender_cfg, data_dir, sender_state, state_path, ingest_state_path)


def _write_pending(data_dir: Path, traces: list[dict], ingest_state_path: Path) -> None:
    from src.state import save_ingest_state

    pending_dir = data_dir / "pending"
    pending_dir.mkdir(exist_ok=True)
    filename = "2024-01-01.jsonl"
    lines = [json.dumps(t, ensure_ascii=False, separators=(",", ":")) for t in traces]
    (pending_dir / filename).write_text("\n".join(lines) + "\n")

    max_seq = max(t.get("_seq_id", 0) for t in traces)
    min_seq = min(t.get("_seq_id", 0) for t in traces)
    state = IngestState(
        next_seq_id=max_seq + 1,
        files={filename: FileEntry(min_seq=min_seq, max_seq=max_seq)},
    )
    save_ingest_state(ingest_state_path, state)


class TestBuildTraceItems:
    def test_returns_items_for_single_trace(self) -> None:
        trace = _make_trace()
        items = _build_trace_items(trace)
        types = [i["type"] for i in items]
        assert "trace-create" in types
        assert "generation-create" in types

    def test_consistent_with_build_ingestion_batch(self) -> None:
        trace = _make_trace()
        items = _build_trace_items(trace)
        batch = _build_ingestion_batch([trace])
        assert len(items) == len(batch["batch"])


class TestSplitIntoBatches:
    def test_single_batch_when_under_limit(self) -> None:
        items = [{"id": str(i), "data": "small"} for i in range(3)]
        batches = _split_into_batches(items, 1_000_000)
        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_splits_when_over_limit(self) -> None:
        items = [{"id": str(i), "data": "x" * 1000} for i in range(10)]
        item_size = _items_byte_size([items[0]])
        limit = item_size * 3 + 10
        batches = _split_into_batches(items, limit)
        assert len(batches) > 1
        for batch in batches:
            assert _items_byte_size(batch) <= limit or len(batch) == 1

    def test_single_oversized_item_in_own_batch(self) -> None:
        small = {"id": "1", "data": "small"}
        big = {"id": "2", "data": "x" * 10000}
        items = [small, big]
        batches = _split_into_batches(items, 100)
        assert len(batches) == 2
        assert batches[0] == [small]
        assert batches[1] == [big]


class TestWriteToFailed:
    def test_creates_failed_file(self, tmp_path: Path) -> None:
        trace = _make_trace(trace_id="abc-123")
        trace["_seq_id"] = 1
        _write_to_failed(tmp_path, trace, 5000)
        failed_dir = tmp_path / "failed"
        assert failed_dir.exists()
        files = list(failed_dir.glob("*.jsonl"))
        assert len(files) == 1
        assert "abc-123" in files[0].name
        content = files[0].read_text()
        parsed = json.loads(content.strip())
        assert parsed["id"] == "abc-123"


class TestTraceAccumulation:
    """R-1: trace-level accumulation with commit_id precision."""

    def test_sends_all_traces_when_under_limit(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=3_500_000)
        traces = [_make_trace(trace_id=f"t{i}") for i in range(3)]
        for i, t in enumerate(traces, 1):
            t["_seq_id"] = i
        _write_pending(sender._data_dir, traces, sender._ingest_state_path)

        responses = [httpx.Response(200)]
        with patch.object(sender, "_post_batch", side_effect=responses):
            result = sender._send_batch()

        assert result is True
        assert sender._state.commit_id == 3

    def test_stops_accumulation_when_exceeding_limit(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=50_000)
        traces = [_make_large_trace(f"t{i}", i, 30_000) for i in range(1, 4)]
        _write_pending(sender._data_dir, traces, sender._ingest_state_path)

        responses = [httpx.Response(200)]
        with patch.object(sender, "_post_batch", side_effect=responses) as mock_post:
            result = sender._send_batch()

        assert result is True
        assert sender._state.commit_id < 3
        assert sender._state.commit_id >= 1

    def test_commit_id_equals_last_sent_not_batch_max(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=50_000)
        t1 = _make_large_trace("t1", 1, 30_000)
        t2 = _make_large_trace("t2", 2, 30_000)
        t3 = _make_large_trace("t3", 3, 30_000)
        _write_pending(sender._data_dir, [t1, t2, t3], sender._ingest_state_path)

        call_count = 0
        def fake_post(items):
            nonlocal call_count
            call_count += 1
            return httpx.Response(200)

        with patch.object(sender, "_post_batch", side_effect=fake_post):
            sender._send_batch()

        assert call_count == 1
        assert sender._state.commit_id != 3

    def test_unsent_traces_available_next_round(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=50_000)
        t1 = _make_large_trace("t1", 1, 30_000)
        t2 = _make_large_trace("t2", 2, 30_000)
        t3 = _make_large_trace("t3", 3, 30_000)
        _write_pending(sender._data_dir, [t1, t2, t3], sender._ingest_state_path)

        with patch.object(sender, "_post_batch", return_value=httpx.Response(200)):
            sender._send_batch()

        first_commit = sender._state.commit_id
        assert first_commit < 3

        with patch.object(sender, "_post_batch", return_value=httpx.Response(200)):
            sender._send_batch()

        assert sender._state.commit_id > first_commit

    def test_all_small_traces_sent_at_once(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=3_500_000)
        traces = [_make_trace(trace_id=f"t{i}") for i in range(5)]
        for i, t in enumerate(traces, 1):
            t["_seq_id"] = i
        _write_pending(sender._data_dir, traces, sender._ingest_state_path)

        with patch.object(sender, "_post_batch", return_value=httpx.Response(200)) as mock:
            sender._send_batch()

        assert mock.call_count == 1
        assert sender._state.commit_id == 5


class TestItemSplitting:
    """R-2: single oversized trace item-level splitting."""

    def test_oversized_trace_splits_into_sub_batches(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=100_000)
        trace = _make_large_trace("big-trace", 1, 200_000)
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        calls = []
        def fake_post(items):
            calls.append(len(items))
            return httpx.Response(200)

        with patch.object(sender, "_post_batch", side_effect=fake_post):
            result = sender._send_batch()

        assert result is True
        assert len(calls) >= 2
        assert sender._state.commit_id == 1

    def test_trace_create_in_first_sub_batch(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=100_000)
        trace = _make_large_trace("big-trace", 1, 200_000)
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        first_batch_items = []
        call_idx = 0
        def fake_post(items):
            nonlocal call_idx
            if call_idx == 0:
                first_batch_items.extend(items)
            call_idx += 1
            return httpx.Response(200)

        with patch.object(sender, "_post_batch", side_effect=fake_post):
            sender._send_batch()

        types = [i["type"] for i in first_batch_items]
        assert "trace-create" in types

    def test_sub_batch_500_stops_and_retries(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=100_000)
        trace = _make_large_trace("big-trace", 1, 200_000)
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        call_idx = 0
        def fake_post(items):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                return httpx.Response(500)
            return httpx.Response(200)

        with patch.object(sender, "_post_batch", side_effect=fake_post):
            try:
                sender._send_batch()
            except RuntimeError:
                pass

        assert sender._state.commit_id == 0
        assert sender._state.last_error is not None

    def test_sub_batch_413_goes_to_failed(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=100_000)
        trace = _make_large_trace("big-trace", 1, 200_000)
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        call_idx = 0
        def fake_post(items):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                return httpx.Response(413)
            return httpx.Response(200)

        with patch.object(sender, "_post_batch", side_effect=fake_post):
            result = sender._send_batch()

        assert result is True
        assert sender._state.commit_id == 1
        failed_files = list((sender._data_dir / "failed").glob("*.jsonl"))
        assert len(failed_files) == 1

    def test_small_trace_no_splitting(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=3_500_000)
        trace = _make_trace(trace_id="small")
        trace["_seq_id"] = 1
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        with patch.object(sender, "_post_batch", return_value=httpx.Response(200)) as mock:
            sender._send_batch()

        assert mock.call_count == 1
        assert sender._state.commit_id == 1


class TestHttp413Handling:
    """R-3: HTTP 413 handling with failed/ recovery."""

    def test_413_writes_to_failed_and_advances_commit(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=3_500_000)
        trace = _make_trace(trace_id="fail-trace")
        trace["_seq_id"] = 1
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        with patch.object(sender, "_post_batch", return_value=httpx.Response(413)):
            result = sender._send_batch()

        assert result is True
        assert sender._state.commit_id == 1
        failed_files = list((sender._data_dir / "failed").glob("*.jsonl"))
        assert len(failed_files) == 1

    def test_413_does_not_increase_backoff(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=3_500_000)
        original_backoff = sender._backoff
        trace = _make_trace(trace_id="fail-trace")
        trace["_seq_id"] = 1
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        with patch.object(sender, "_post_batch", return_value=httpx.Response(413)):
            result = sender._send_batch()

        assert result is True
        assert sender._backoff == original_backoff

    def test_413_returns_not_raises(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=3_500_000)
        trace = _make_trace(trace_id="fail-trace")
        trace["_seq_id"] = 1
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        with patch.object(sender, "_post_batch", return_value=httpx.Response(413)):
            result = sender._send_batch()
        assert result is True

    def test_failed_file_compatible_with_recover(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=3_500_000)
        trace = _make_trace(trace_id="recover-test")
        trace["_seq_id"] = 1
        _write_pending(sender._data_dir, [trace], sender._ingest_state_path)

        with patch.object(sender, "_post_batch", return_value=httpx.Response(413)):
            sender._send_batch()

        failed_dir = sender._data_dir / "failed"
        files = list(failed_dir.glob("*.jsonl"))
        assert len(files) == 1
        assert files[0].name.endswith(".jsonl")
        content = files[0].read_text().strip()
        parsed = json.loads(content)
        assert parsed["id"] == "recover-test"
        assert "trace" in parsed
        assert "generations" in parsed

    def test_sender_continues_after_413(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path, max_payload_bytes=3_500_000)
        t1 = _make_trace(trace_id="fail-1")
        t1["_seq_id"] = 1
        t2 = _make_trace(trace_id="ok-2")
        t2["_seq_id"] = 2
        _write_pending(sender._data_dir, [t1, t2], sender._ingest_state_path)

        call_idx = 0
        def fake_post(items):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return httpx.Response(413)
            return httpx.Response(200)

        with patch.object(sender, "_post_batch", side_effect=fake_post):
            sender._send_batch()

        assert sender._state.commit_id >= 1

        with patch.object(sender, "_post_batch", return_value=httpx.Response(200)):
            sender._send_batch()

        assert sender._state.commit_id == 2
