"""Tests for langstash_deliver.deliver module."""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import langstash_deliver.deliver as deliver_mod
from langstash_deliver.deliver import append_failed_trace, deliver_trace

SAMPLE_TRACE = {"schema_version": "2", "id": "test-id", "source": "unit-test"}


class TestDeliverTraceTier1:
    """Tier 1: langstash enabled and POST succeeds."""

    def test_returns_true_on_langstash_success(self, monkeypatch):
        monkeypatch.setenv("LANGSTASH_ENABLED", "true")
        monkeypatch.setenv("LANGSTASH_URL", "http://127.0.0.1:9999")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(deliver_mod, "urlopen", return_value=mock_resp) as mock_urlopen:
            result = deliver_trace(SAMPLE_TRACE)

        assert result is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://127.0.0.1:9999/ingest"
        assert req.get_method() == "POST"


class TestDeliverTraceTier2:
    """Tier 2: langstash fails, direct_push_fn succeeds."""

    def test_falls_back_to_direct_push(self, monkeypatch):
        monkeypatch.setenv("LANGSTASH_ENABLED", "true")

        with patch.object(deliver_mod, "urlopen", side_effect=Exception("connection refused")):
            push_fn = MagicMock(return_value=True)
            result = deliver_trace(SAMPLE_TRACE, direct_push_fn=push_fn)

        assert result is True
        push_fn.assert_called_once_with(SAMPLE_TRACE)

    def test_direct_push_when_langstash_disabled(self, monkeypatch):
        monkeypatch.delenv("LANGSTASH_ENABLED", raising=False)

        push_fn = MagicMock(return_value=True)
        result = deliver_trace(SAMPLE_TRACE, direct_push_fn=push_fn)

        assert result is True
        push_fn.assert_called_once_with(SAMPLE_TRACE)


class TestDeliverTraceTier3:
    """Tier 3: both fail, writes to failed log and returns False."""

    def test_writes_failed_log(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LANGSTASH_ENABLED", "true")
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)

        with patch.object(deliver_mod, "urlopen", side_effect=Exception("down")):
            push_fn = MagicMock(return_value=False)
            result = deliver_trace(SAMPLE_TRACE, direct_push_fn=push_fn)

        assert result is False
        # Verify a .jsonl file was created under tmp_path
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1
        content = jsonl_files[0].read_text(encoding="utf-8")
        parsed = json.loads(content.strip())
        assert parsed["id"] == "test-id"

    def test_returns_false_no_push_fn_langstash_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LANGSTASH_ENABLED", raising=False)
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)

        result = deliver_trace(SAMPLE_TRACE)

        assert result is False
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1


class TestAppendFailedTrace:
    """Tests for append_failed_trace."""

    def test_writes_jsonl_line(self, monkeypatch, tmp_path):
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)
        trace = {"id": "abc", "data": "xyz"}
        append_failed_trace(trace)

        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1
        lines = jsonl_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == trace

    def test_appends_multiple_lines(self, monkeypatch, tmp_path):
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)
        append_failed_trace({"n": 1})
        append_failed_trace({"n": 2})

        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1
        lines = jsonl_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"n": 1}
        assert json.loads(lines[1]) == {"n": 2}

    def test_filename_is_date_based(self, monkeypatch, tmp_path):
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)
        append_failed_trace({"id": "x"})

        jsonl_files = list(tmp_path.glob("*.jsonl"))
        filename = jsonl_files[0].name
        # Filename should be YYYY-MM-DD.jsonl
        assert len(filename) == len("2025-01-01.jsonl")
        assert filename.endswith(".jsonl")
        # Date part should be parseable
        date_part = filename.removesuffix(".jsonl")
        parts = date_part.split("-")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)
