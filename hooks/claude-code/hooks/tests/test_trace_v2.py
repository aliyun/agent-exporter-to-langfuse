"""Tests for _build_trace_v2 from langfuse_hook."""

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


class TestBuildTraceV2Basic:
    def test_structure_has_required_keys(self):
        turn = Turn(
            user_msg=_user_msg("what is 1+1?"),
            assistant_msgs=[_assistant_msg("2")],
            tool_results_by_id={},
        )
        result = hook._build_trace_v2(
            session_id="sess-1",
            turn_num=1,
            turn=turn,
            transcript_path=Path("/tmp/transcript.jsonl"),
            user_id="testuser",
            tags=["claude-code"],
            is_subagent=False,
        )

        assert result["schema_version"] == "2"
        assert result["source"] == "claude-code"
        assert result["session_id"] == "sess-1"
        assert result["user_id"] == "testuser"
        assert result["tags"] == ["claude-code"]
        assert "trace" in result
        assert "generations" in result
        assert "spans" in result

    def test_trace_name_normal(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook._build_trace_v2(
            session_id="s", turn_num=3, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        assert result["trace"]["name"] == "Claude Code - Turn 3"

    def test_trace_name_subagent(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook._build_trace_v2(
            session_id="s", turn_num=2, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=True,
        )
        assert result["trace"]["name"] == "Claude Code - Subagent Turn 2"

    def test_generation_count_matches_assistant_msgs(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[
                _assistant_msg("first", msg_id="m1", ts="2024-01-01T00:00:01Z"),
                _assistant_msg("second", msg_id="m2", ts="2024-01-01T00:00:02Z"),
            ],
            tool_results_by_id={},
        )
        result = hook._build_trace_v2(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        assert len(result["generations"]) == 2
        assert result["generations"][0]["name"] == "Claude Generation 1"
        assert result["generations"][1]["name"] == "Claude Generation 2"


class TestBuildTraceV2WithTools:
    def test_tool_uses_produce_spans(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[
                _tool_use_assistant(tool_id="tu1", tool_name="Bash"),
            ],
            tool_results_by_id={
                "tu1": {"content": "file1.txt", "timestamp": "2024-01-01T00:00:03Z"},
            },
        )
        result = hook._build_trace_v2(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        assert len(result["spans"]) == 1
        span = result["spans"][0]
        assert span["name"] == "Tool: Bash"
        assert span["generation_index"] == 0
        assert span["output"] == "file1.txt"
        assert span["metadata"]["tool_id"] == "tu1"

    def test_generation_output_includes_tool_calls(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[
                _tool_use_assistant(tool_id="tu1", tool_name="Read"),
            ],
            tool_results_by_id={},
        )
        result = hook._build_trace_v2(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        gen = result["generations"][0]
        assert "tool_calls" in gen["output"]
        assert gen["output"]["tool_calls"][0]["name"] == "Read"

    def test_multiple_tools_in_one_assistant(self):
        am = {
            "type": "assistant",
            "message": {
                "id": "msg_multi",
                "role": "assistant",
                "model": "claude-3.5-sonnet",
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"cmd": "ls"}},
                    {"type": "tool_use", "id": "tu2", "name": "Read", "input": {"path": "/tmp/x"}},
                ],
            },
            "timestamp": "2024-01-01T00:00:02Z",
        }
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[am],
            tool_results_by_id={
                "tu1": {"content": "output1", "timestamp": "2024-01-01T00:00:03Z"},
                "tu2": {"content": "output2", "timestamp": "2024-01-01T00:00:04Z"},
            },
        )
        result = hook._build_trace_v2(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        assert len(result["spans"]) == 2
        assert result["spans"][0]["name"] == "Tool: Bash"
        assert result["spans"][1]["name"] == "Tool: Read"


class TestBuildTraceV2WithUsage:
    def test_usage_included_in_generation(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[
                _assistant_msg("answer", usage={"input_tokens": 100, "output_tokens": 50}),
            ],
            tool_results_by_id={},
        )
        result = hook._build_trace_v2(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        gen = result["generations"][0]
        assert "usage" in gen
        assert gen["usage"]["input"] == 100
        assert gen["usage"]["output"] == 50


class TestBuildTraceV2UserIdOptional:
    def test_no_user_id_key_when_none(self):
        turn = Turn(
            user_msg=_user_msg(),
            assistant_msgs=[_assistant_msg()],
            tool_results_by_id={},
        )
        result = hook._build_trace_v2(
            session_id="s", turn_num=1, turn=turn,
            transcript_path=Path("/tmp/t.jsonl"),
            user_id=None, tags=[], is_subagent=False,
        )
        assert "user_id" not in result
