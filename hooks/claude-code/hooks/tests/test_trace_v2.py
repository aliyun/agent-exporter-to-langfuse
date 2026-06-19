"""Tests for build_otlp_json from langfuse_hook."""

import json
from pathlib import Path

import langfuse_hook as hook
from langfuse_hook import Turn


def _user_msg(text="hello", ts="2024-01-01T00:00:00Z"):
    return {"type": "user", "content": text, "timestamp": ts}


def _assistant_msg(text="hi", msg_id="msg_001", ts="2024-01-01T00:00:01Z", model="claude-3.5-sonnet", usage=None):
    msg = {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": ts,
    }
    if usage:
        msg["message"]["usage"] = usage
    return msg


def _tool_use_assistant(tool_id="tu1", tool_name="Bash", tool_input=None,
                        msg_id="msg_002", ts="2024-01-01T00:00:02Z", model="claude-3.5-sonnet"):
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": [
                {"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input or {"cmd": "ls"}},
            ],
        },
        "timestamp": ts,
    }


def _get_spans(result):
    return result["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _get_attr(span, key):
    for attr in span.get("attributes", []):
        if attr["key"] == key:
            return attr["value"]
    return None


class TestBuildOtlpJsonBasic:
    def test_structure_has_otlp_format(self):
        turn = Turn(
            user_msg=_user_msg("what is 1+1?"),
            assistant_msgs=[_assistant_msg("2")],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="sess-1", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/transcript.jsonl"),
            user_id="testuser", tags=["claude-code"], is_subagent=False,
        )

        assert "resourceSpans" in result
        assert len(result["resourceSpans"]) == 1
        scope_spans = result["resourceSpans"][0]["scopeSpans"]
        assert len(scope_spans) == 1
        assert scope_spans[0]["scope"]["name"] == "agent-exporter-to-langfuse"
        spans = scope_spans[0]["spans"]
        assert len(spans) >= 1

    def test_trace_id_is_32_hex(self):
        turn = Turn(
            user_msg=_user_msg(), assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        for span in spans:
            assert len(span["traceId"]) == 32
            assert all(c in "0123456789abcdef" for c in span["traceId"])

    def test_span_id_is_16_hex(self):
        turn = Turn(
            user_msg=_user_msg(), assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        for span in spans:
            assert len(span["spanId"]) == 16
            assert all(c in "0123456789abcdef" for c in span["spanId"])

    def test_root_span_has_trace_name(self):
        turn = Turn(
            user_msg=_user_msg(), assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=3, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        root = [s for s in spans if "parentSpanId" not in s]
        assert len(root) == 1
        trace_name = _get_attr(root[0], "langfuse.trace.name")
        assert trace_name["stringValue"] == "Claude Code - Turn 3"

    def test_subagent_trace_name(self):
        turn = Turn(
            user_msg=_user_msg(), assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=2, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=True,
        )
        spans = _get_spans(result)
        root = [s for s in spans if "parentSpanId" not in s]
        trace_name = _get_attr(root[0], "langfuse.trace.name")
        assert trace_name["stringValue"] == "Claude Code - Subagent Turn 2"

    def test_session_id_attribute(self):
        turn = Turn(
            user_msg=_user_msg(), assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="sess-abc", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        root = [s for s in spans if "parentSpanId" not in s][0]
        session = _get_attr(root, "session.id")
        assert session["stringValue"] == "sess-abc"

    def test_generation_spans_count(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[
                _assistant_msg("first", msg_id="m1", ts="2024-01-01T00:00:01Z"),
                _assistant_msg("second", msg_id="m2", ts="2024-01-01T00:00:02Z"),
            ],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        gen_spans = [s for s in spans if _get_attr(s, "langfuse.observation.type") and
                     _get_attr(s, "langfuse.observation.type").get("stringValue") == "generation"]
        assert len(gen_spans) == 2


class TestBuildOtlpJsonWithTools:
    def test_tool_spans_nested_under_generation(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[_tool_use_assistant(tool_id="tu1", tool_name="Bash")],
            tool_results_by_id={
                "tu1": {"content": "file1.txt", "timestamp": "2024-01-01T00:00:03Z"},
            },
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        tool_spans = [s for s in spans if _get_attr(s, "langfuse.observation.type") and
                      _get_attr(s, "langfuse.observation.type").get("stringValue") == "tool"]
        assert len(tool_spans) == 1
        assert tool_spans[0]["name"] == "Tool: Bash"
        assert "parentSpanId" in tool_spans[0]


class TestBuildOtlpJsonWithUsage:
    def test_usage_in_generation_attributes(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[
                _assistant_msg("answer", usage={"input_tokens": 100, "output_tokens": 50}),
            ],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        gen_spans = [s for s in spans if _get_attr(s, "langfuse.observation.type") and
                     _get_attr(s, "langfuse.observation.type").get("stringValue") == "generation"]
        assert len(gen_spans) == 1
        usage_attr = _get_attr(gen_spans[0], "langfuse.observation.usage_details")
        assert usage_attr is not None
        usage = json.loads(usage_attr["stringValue"])
        assert usage["input"] == 100
        assert usage["output"] == 50


class TestBuildOtlpJsonUserIdOptional:
    def test_user_id_attribute_when_set(self):
        turn = Turn(
            user_msg=_user_msg(), assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id="testuser", tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        root = [s for s in spans if "parentSpanId" not in s][0]
        uid = _get_attr(root, "user.id")
        assert uid["stringValue"] == "testuser"

    def test_no_user_id_attribute_when_none(self):
        turn = Turn(
            user_msg=_user_msg(), assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook.build_otlp_json(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        spans = _get_spans(result)
        root = [s for s in spans if "parentSpanId" not in s][0]
        uid = _get_attr(root, "user.id")
        assert uid is None
