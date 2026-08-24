"""Tests for src.sender — OTLP JSON relay, _read_pending_traces, Sender."""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import httpx

from src.config import LangfuseConfig, SenderConfig
from src.sender import (
    Sender, _fmt_time, _fmt_trace, _read_pending_traces, _trace_time_range,
)
from src.state import FileEntry, IngestState, SenderState, save_sender_state


def _make_otlp(seq_id: int = 1) -> dict:
    return {
        "_seq_id": seq_id,
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


class TestReadPendingTraces:
    def test_reads_uncommitted_traces(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        filename = "2024-01-01.jsonl"
        lines = [json.dumps({"_seq_id": i, "data": f"trace-{i}"}) for i in range(1, 4)]
        (pending_dir / filename).write_text("\n".join(lines) + "\n")

        state = IngestState(
            next_seq_id=4,
            files={filename: FileEntry(min_seq=1, max_seq=3)},
        )
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

    def test_no_pending_dir(self, tmp_path: Path) -> None:
        state = IngestState()
        traces = _read_pending_traces(tmp_path, state, commit_id=0, batch_size=10)
        assert traces == []


def _setup_sender(tmp_path: Path) -> Sender:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_path = tmp_path / "sender.json"
    ingest_state_path = tmp_path / "ingest.json"

    langfuse_cfg = LangfuseConfig(
        public_key="pk-test", secret_key="sk-test", base_url="https://test.langfuse.com",
    )
    sender_cfg = SenderConfig()
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


class TestSenderOtlpRelay:
    def test_posts_to_otel_endpoint(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(200)) as mock:
            sender._send_batch()

        mock.assert_called_once()
        posted = mock.call_args[0][0]
        assert "resourceSpans" in posted

    def test_otel_endpoint_url(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)

        with patch("httpx.post", return_value=httpx.Response(200)) as mock:
            sender._post_otlp(_make_otlp())

        url = mock.call_args[1].get("url") or mock.call_args[0][0]
        assert url == "https://test.langfuse.com/api/public/otel/v1/traces"

    def test_2xx_advances_commit_id(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(200)):
            sender._send_batch()

        assert sender._state.commit_id == 1

    def test_400_skips_and_advances(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(400)):
            sender._send_batch()

        assert sender._state.commit_id == 1

    def test_401_does_not_advance_retries(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(401)):
            try:
                sender._send_batch()
            except RuntimeError:
                pass

        assert sender._state.commit_id == 0
        assert sender._state.last_error is not None
        assert not sender._stop.is_set()

    def test_403_does_not_stop_sender(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(403)):
            try:
                sender._send_batch()
            except RuntimeError:
                pass

        assert not sender._stop.is_set()

    def test_404_retries_with_backoff(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)
        original_backoff = sender._backoff

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(404)):
            try:
                sender._send_batch()
            except RuntimeError:
                pass

        assert sender._backoff > original_backoff

    def test_405_retries_with_backoff(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(405)):
            try:
                sender._send_batch()
            except RuntimeError:
                pass

        assert sender._state.commit_id == 0
        assert not sender._stop.is_set()

    def test_5xx_retries(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(500)):
            try:
                sender._send_batch()
            except RuntimeError:
                pass

        assert sender._state.commit_id == 0

    def test_partial_failure_commits_last_success(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        sender._cfg.batch_size = 3
        traces = [_make_otlp(i) for i in range(1, 4)]
        _write_pending(sender._data_dir, traces, sender._ingest_state_path)

        call_idx = 0
        def fake_post(otlp):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                return httpx.Response(500)
            return httpx.Response(200)

        with patch.object(sender, "_post_otlp", side_effect=fake_post):
            try:
                sender._send_batch()
            except RuntimeError:
                pass

        assert sender._state.commit_id == 1

    def test_batch_size_default_is_10(self) -> None:
        cfg = SenderConfig()
        assert cfg.batch_size == 10

    def test_network_error_retries(self, tmp_path: Path) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(1)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", side_effect=Exception("connection refused")):
            try:
                sender._send_batch()
            except Exception:
                pass

        assert sender._state.commit_id == 0
        assert sender._state.last_error is not None


class TestSenderLogContext:
    """Rich logging: per-POST line carries seq + trace time + status, and the
    batch summary carries the seq range + trace-time range."""

    def test_trace_time_range_from_spans(self) -> None:
        # _make_otlp uses start=1718000000000000000 end=1718000001000000000 ns
        s, e = _trace_time_range(_make_otlp(1))
        assert s == 1718000000000000000
        assert e == 1718000001000000000

    def test_trace_time_range_missing_times(self) -> None:
        body = {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "x"}]}]}]}
        assert _trace_time_range(body) == (None, None)

    def test_fmt_time_human_friendly(self) -> None:
        s = 1718000000000000000  # 2024-06-10 08:53:20 UTC
        e = 1718000001000000000  # +1s
        out = _fmt_time(s, e)
        assert out.startswith("time=")
        assert "~" in out  # a range, not a point
        # same instant collapses to a point (no ~)
        assert "~" not in _fmt_time(s, s)
        # unknown
        assert _fmt_time(None, None) == "time=unknown"

    def test_fmt_trace_single_trace_start_plus_duration_ms(self) -> None:
        s = 1718000000000000000  # 2024-06-10 08:53:20 UTC
        e = 1718000001000000000  # +1s = 1000ms
        out = _fmt_trace(s, e)
        assert out.startswith("time="), out
        assert "+1000ms" in out, f"expected +1000ms duration, got: {out}"
        assert "~" not in out, "single trace must not show a range"
        # zero duration when end missing or equal -> +0ms
        assert _fmt_trace(s, s) == _fmt_trace(s, None)
        assert "+0ms" in _fmt_trace(s, None)
        assert _fmt_trace(None, None) == "time=unknown"

    def test_posted_line_carries_seq_and_trace_time(self, tmp_path: Path, caplog) -> None:
        sender = _setup_sender(tmp_path)
        _write_pending(sender._data_dir, [_make_otlp(42)], sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(200)):
            with caplog.at_level(logging.INFO, logger="langstash.sender"):
                sender._send_batch()

        posted = [r for r in caplog.records if "posted seq=" in r.message]
        assert posted, "expected a per-POST 'posted seq=...' log line"
        assert "seq=42" in posted[0].message
        assert "-> 200" in posted[0].message
        assert "time=" in posted[0].message
        # single trace shows start + duration in ms, never a range (~)
        assert "+1000ms" in posted[0].message, f"got: {posted[0].message}"
        assert "~" not in posted[0].message

    def test_batch_summary_carries_seq_range_and_trace_time(self, tmp_path: Path, caplog) -> None:
        sender = _setup_sender(tmp_path)
        sender._cfg.batch_size = 5
        traces = [_make_otlp(i) for i in range(100, 105)]
        _write_pending(sender._data_dir, traces, sender._ingest_state_path)

        with patch.object(sender, "_post_otlp", return_value=httpx.Response(200)):
            with caplog.at_level(logging.INFO, logger="langstash.sender"):
                sender._send_batch()

        summary = [r for r in caplog.records if "sent 5 traces" in r.message]
        assert summary, "expected a 'sent N traces' summary line"
        msg = summary[0].message
        assert "commit_id=" not in msg, "commit_id is redundant now that seq range is shown"
        assert "seq=100~104" in msg, f"expected seq range in summary, got: {msg}"
        assert "time=" in msg
