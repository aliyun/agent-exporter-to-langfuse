"""Tests for langstash_deliver.schema module."""

from datetime import datetime, timezone

from langstash_deliver.schema import build_generation, build_span, build_trace_json


class TestBuildTraceJson:
    """Tests for build_trace_json."""

    def _make_trace(self, **overrides):
        defaults = dict(
            source="test-source",
            session_id="sess-1",
            user_id=None,
            tags=[],
            trace_name="my-trace",
            start_time=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2025, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
            user_input="hello",
            assistant_output="world",
            metadata={"key": "val"},
            generations=[],
            spans=[],
        )
        defaults.update(overrides)
        return build_trace_json(**defaults)

    def test_required_top_level_fields(self):
        result = self._make_trace()
        assert result["schema_version"] == "2"
        assert isinstance(result["id"], str) and len(result["id"]) > 0
        assert result["source"] == "test-source"
        assert result["session_id"] == "sess-1"

    def test_trace_block(self):
        t0 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2025, 6, 1, 12, 5, 0, tzinfo=timezone.utc)
        result = self._make_trace(
            trace_name="trace-x",
            start_time=t0,
            end_time=t1,
            user_input="in",
            assistant_output="out",
            metadata={"m": 1},
        )
        trace = result["trace"]
        assert trace["name"] == "trace-x"
        assert trace["start_time"] == t0.isoformat()
        assert trace["end_time"] == t1.isoformat()
        assert trace["input"] == "in"
        assert trace["output"] == "out"
        assert trace["metadata"] == {"m": 1}

    def test_generations_and_spans_present(self):
        gen = {"name": "g1"}
        sp = {"name": "s1"}
        result = self._make_trace(generations=[gen], spans=[sp])
        assert result["generations"] == [gen]
        assert result["spans"] == [sp]

    def test_user_id_included_when_set(self):
        result = self._make_trace(user_id="u-42")
        assert result["user_id"] == "u-42"

    def test_user_id_absent_when_none(self):
        result = self._make_trace(user_id=None)
        assert "user_id" not in result

    def test_tags_included_when_nonempty(self):
        result = self._make_trace(tags=["a", "b"])
        assert result["tags"] == ["a", "b"]

    def test_tags_absent_when_empty(self):
        result = self._make_trace(tags=[])
        assert "tags" not in result

    def test_none_datetime_yields_empty_string(self):
        result = self._make_trace(start_time=None, end_time=None)
        assert result["trace"]["start_time"] == ""
        assert result["trace"]["end_time"] == ""


class TestBuildGeneration:
    """Tests for build_generation."""

    def test_required_fields(self):
        t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        t1 = datetime(2025, 1, 1, 0, 1, tzinfo=timezone.utc)
        gen = build_generation(name="gen-1", model="gpt-4", start_time=t0, end_time=t1)
        assert gen["name"] == "gen-1"
        assert gen["model"] == "gpt-4"
        assert gen["start_time"] == t0.isoformat()
        assert gen["end_time"] == t1.isoformat()
        assert gen["output"] == {}

    def test_output_value(self):
        gen = build_generation(
            name="g", model="m", start_time=None, end_time=None, gen_output={"text": "hi"}
        )
        assert gen["output"] == {"text": "hi"}

    def test_optional_input_included(self):
        gen = build_generation(
            name="g", model="m", start_time=None, end_time=None, gen_input="prompt"
        )
        assert gen["input"] == "prompt"

    def test_input_absent_when_none(self):
        gen = build_generation(name="g", model="m", start_time=None, end_time=None)
        assert "input" not in gen

    def test_usage_included(self):
        gen = build_generation(
            name="g",
            model="m",
            start_time=None,
            end_time=None,
            usage={"input": 100, "output": 50},
        )
        assert gen["usage"] == {"input": 100, "output": 50}

    def test_usage_absent_when_none(self):
        gen = build_generation(name="g", model="m", start_time=None, end_time=None)
        assert "usage" not in gen

    def test_metadata_included(self):
        gen = build_generation(
            name="g", model="m", start_time=None, end_time=None, metadata={"k": "v"}
        )
        assert gen["metadata"] == {"k": "v"}

    def test_metadata_absent_when_none(self):
        gen = build_generation(name="g", model="m", start_time=None, end_time=None)
        assert "metadata" not in gen

    def test_none_datetime_yields_empty_string(self):
        gen = build_generation(name="g", model="m", start_time=None, end_time=None)
        assert gen["start_time"] == ""
        assert gen["end_time"] == ""

    def test_empty_model_becomes_unknown(self):
        gen = build_generation(name="g", model="", start_time=None, end_time=None)
        assert gen["model"] == "unknown"


class TestBuildSpan:
    """Tests for build_span."""

    def test_required_fields(self):
        t0 = datetime(2025, 3, 1, tzinfo=timezone.utc)
        t1 = datetime(2025, 3, 1, 0, 2, tzinfo=timezone.utc)
        span = build_span(name="sp-1", generation_index=0, start_time=t0, end_time=t1)
        assert span["name"] == "sp-1"
        assert span["generation_index"] == 0
        assert span["start_time"] == t0.isoformat()
        assert span["end_time"] == t1.isoformat()

    def test_optional_input_output(self):
        span = build_span(
            name="s",
            generation_index=1,
            start_time=None,
            end_time=None,
            span_input="in",
            span_output="out",
        )
        assert span["input"] == "in"
        assert span["output"] == "out"

    def test_input_output_absent_when_none(self):
        span = build_span(name="s", generation_index=0, start_time=None, end_time=None)
        assert "input" not in span
        assert "output" not in span

    def test_metadata_included(self):
        span = build_span(
            name="s",
            generation_index=0,
            start_time=None,
            end_time=None,
            metadata={"x": 1},
        )
        assert span["metadata"] == {"x": 1}

    def test_metadata_absent_when_none(self):
        span = build_span(name="s", generation_index=0, start_time=None, end_time=None)
        assert "metadata" not in span

    def test_none_datetime_yields_empty_string(self):
        span = build_span(name="s", generation_index=0, start_time=None, end_time=None)
        assert span["start_time"] == ""
        assert span["end_time"] == ""
