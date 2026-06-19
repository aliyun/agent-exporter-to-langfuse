"""Tests for langstash_deliver.deliver module — OTLP JSON three-tier delivery."""

import json
from unittest.mock import MagicMock, patch

import langstash_deliver.deliver as deliver_mod
from langstash_deliver.deliver import append_failed_trace, deliver_trace

SAMPLE_OTLP = {
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
            result = deliver_trace(SAMPLE_OTLP)

        assert result is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://127.0.0.1:9999/ingest"
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/json"


class TestDeliverTraceTier2:
    """Tier 2: langstash fails or disabled, direct POST to Langfuse OTel endpoint."""

    def test_falls_back_to_langfuse_otel(self, monkeypatch):
        monkeypatch.setenv("LANGSTASH_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example.com")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

        call_log = []

        def mock_urlopen(req, **kwargs):
            call_log.append(req)
            if "/ingest" in req.full_url:
                raise Exception("connection refused")
            resp = MagicMock()
            resp.status = 200
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch.object(deliver_mod, "urlopen", side_effect=mock_urlopen):
            result = deliver_trace(SAMPLE_OTLP)

        assert result is True
        assert len(call_log) == 2
        otel_req = call_log[1]
        assert otel_req.full_url == "https://langfuse.example.com/api/public/otel/v1/traces"
        assert otel_req.get_header("Content-type") == "application/json"
        assert otel_req.get_header("Authorization").startswith("Basic ")

    def test_direct_push_when_langstash_disabled(self, monkeypatch):
        monkeypatch.delenv("LANGSTASH_ENABLED", raising=False)
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example.com")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(deliver_mod, "urlopen", return_value=mock_resp) as mock_urlopen:
            result = deliver_trace(SAMPLE_OTLP)

        assert result is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "/api/public/otel/v1/traces" in req.full_url

    def test_skips_tier2_without_credentials(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LANGSTASH_ENABLED", raising=False)
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)

        result = deliver_trace(SAMPLE_OTLP)

        assert result is False
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1


class TestDeliverTraceTier3:
    """Tier 3: both fail, writes to failed log and returns False."""

    def test_writes_failed_log(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LANGSTASH_ENABLED", "true")
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)

        with patch.object(deliver_mod, "urlopen", side_effect=Exception("down")):
            result = deliver_trace(SAMPLE_OTLP)

        assert result is False
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1
        content = jsonl_files[0].read_text(encoding="utf-8")
        parsed = json.loads(content.strip())
        assert "resourceSpans" in parsed

    def test_returns_false_no_credentials_langstash_disabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LANGSTASH_ENABLED", raising=False)
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)

        result = deliver_trace(SAMPLE_OTLP)

        assert result is False
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1


class TestAppendFailedTrace:
    """Tests for append_failed_trace."""

    def test_writes_jsonl_line(self, monkeypatch, tmp_path):
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)
        trace = {"resourceSpans": []}
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

    def test_filename_is_date_based(self, monkeypatch, tmp_path):
        monkeypatch.setattr(deliver_mod, "FAILED_DIR", tmp_path)
        append_failed_trace({"id": "x"})

        jsonl_files = list(tmp_path.glob("*.jsonl"))
        filename = jsonl_files[0].name
        assert filename.endswith(".jsonl")
        date_part = filename.removesuffix(".jsonl")
        parts = date_part.split("-")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)
