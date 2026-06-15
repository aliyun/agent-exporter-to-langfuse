"""Tests for pure helper functions in langfuse_hook."""

import hashlib
from datetime import datetime, timezone

import langfuse_hook as hook


# ---- extract_text ----

class TestExtractText:
    def test_string_input(self):
        assert hook.extract_text("hello") == "hello"

    def test_list_of_text_blocks(self):
        content = [
            {"type": "text", "text": "line1"},
            {"type": "text", "text": "line2"},
        ]
        assert hook.extract_text(content) == "line1\nline2"

    def test_mixed_content_skips_non_text(self):
        content = [
            {"type": "text", "text": "visible"},
            {"type": "image", "data": "..."},
            {"type": "text", "text": "also visible"},
        ]
        assert hook.extract_text(content) == "visible\nalso visible"

    def test_list_with_plain_strings(self):
        content = ["alpha", "beta"]
        assert hook.extract_text(content) == "alpha\nbeta"

    def test_empty_list(self):
        assert hook.extract_text([]) == ""

    def test_empty_string(self):
        assert hook.extract_text("") == ""

    def test_none_returns_empty(self):
        assert hook.extract_text(None) == ""

    def test_integer_returns_empty(self):
        assert hook.extract_text(42) == ""

    def test_text_block_with_empty_text(self):
        content = [{"type": "text", "text": ""}]
        # empty strings are filtered by the "if p" guard
        assert hook.extract_text(content) == ""


# ---- truncate_text ----

class TestTruncateText:
    def test_within_limit(self):
        s = "short"
        result, meta = hook.truncate_text(s, max_chars=100)
        assert result == "short"
        assert meta["truncated"] is False
        assert meta["orig_len"] == 5

    def test_exact_limit(self):
        s = "abcde"
        result, meta = hook.truncate_text(s, max_chars=5)
        assert result == "abcde"
        assert meta["truncated"] is False

    def test_over_limit_truncates(self):
        s = "abcdefghij"  # 10 chars
        result, meta = hook.truncate_text(s, max_chars=4)
        assert result == "abcd"
        assert meta["truncated"] is True
        assert meta["orig_len"] == 10
        assert meta["kept_len"] == 4
        assert meta["sha256"] == hashlib.sha256(s.encode("utf-8")).hexdigest()

    def test_none_input(self):
        result, meta = hook.truncate_text(None, max_chars=100)
        assert result == ""
        assert meta["truncated"] is False
        assert meta["orig_len"] == 0


# ---- get_role ----

class TestGetRole:
    def test_type_user(self):
        assert hook.get_role({"type": "user"}) == "user"

    def test_type_assistant(self):
        assert hook.get_role({"type": "assistant"}) == "assistant"

    def test_message_role(self):
        msg = {"message": {"role": "assistant"}}
        assert hook.get_role(msg) == "assistant"

    def test_unknown_type(self):
        assert hook.get_role({"type": "system"}) is None

    def test_no_type_no_message(self):
        assert hook.get_role({}) is None

    def test_type_takes_precedence_over_message_role(self):
        msg = {"type": "user", "message": {"role": "assistant"}}
        assert hook.get_role(msg) == "user"


# ---- is_tool_result ----

class TestIsToolResult:
    def test_true_for_tool_result_content(self):
        msg = {
            "type": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        }
        assert hook.is_tool_result(msg) is True

    def test_false_for_assistant(self):
        msg = {
            "type": "assistant",
            "content": [{"type": "tool_result", "tool_use_id": "t1"}],
        }
        assert hook.is_tool_result(msg) is False

    def test_false_for_text_content(self):
        msg = {"type": "user", "content": [{"type": "text", "text": "hi"}]}
        assert hook.is_tool_result(msg) is False

    def test_false_for_string_content(self):
        msg = {"type": "user", "content": "just a string"}
        assert hook.is_tool_result(msg) is False


# ---- iter_tool_results / iter_tool_uses ----

class TestIterToolResults:
    def test_extracts_tool_results(self):
        content = [
            {"type": "tool_result", "tool_use_id": "a", "content": "r1"},
            {"type": "text", "text": "ignore"},
            {"type": "tool_result", "tool_use_id": "b", "content": "r2"},
        ]
        results = hook.iter_tool_results(content)
        assert len(results) == 2
        assert results[0]["tool_use_id"] == "a"
        assert results[1]["tool_use_id"] == "b"

    def test_empty_list(self):
        assert hook.iter_tool_results([]) == []

    def test_non_list(self):
        assert hook.iter_tool_results("not a list") == []
        assert hook.iter_tool_results(None) == []


class TestIterToolUses:
    def test_extracts_tool_uses(self):
        content = [
            {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"cmd": "ls"}},
            {"type": "text", "text": "some text"},
        ]
        uses = hook.iter_tool_uses(content)
        assert len(uses) == 1
        assert uses[0]["id"] == "tu1"

    def test_empty_list(self):
        assert hook.iter_tool_uses([]) == []

    def test_non_list(self):
        assert hook.iter_tool_uses(None) == []


# ---- get_model ----

class TestGetModel:
    def test_from_message(self):
        msg = {"message": {"model": "claude-3.5-sonnet"}}
        assert hook.get_model(msg) == "claude-3.5-sonnet"

    def test_missing_model_defaults_claude(self):
        msg = {"message": {}}
        assert hook.get_model(msg) == "claude"

    def test_no_message_key(self):
        assert hook.get_model({}) == "claude"


# ---- get_usage ----

class TestGetUsage:
    def test_full_usage(self):
        msg = {
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                }
            }
        }
        usage = hook.get_usage(msg)
        assert usage == {
            "input": 100,
            "output": 50,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 10,
        }

    def test_partial_usage(self):
        msg = {"message": {"usage": {"input_tokens": 100, "output_tokens": 50}}}
        usage = hook.get_usage(msg)
        assert usage == {"input": 100, "output": 50}

    def test_zero_values_skipped(self):
        msg = {"message": {"usage": {"input_tokens": 0, "output_tokens": 0}}}
        assert hook.get_usage(msg) is None

    def test_no_usage(self):
        msg = {"message": {}}
        assert hook.get_usage(msg) is None

    def test_no_message(self):
        assert hook.get_usage({}) is None


# ---- parse_ts ----

class TestParseTs:
    def test_iso_with_trailing_z(self):
        result = hook.parse_ts("2024-01-15T10:30:00Z")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 10
        assert result.minute == 30

    def test_iso_with_offset(self):
        result = hook.parse_ts("2024-01-15T10:30:00+08:00")
        assert isinstance(result, datetime)

    def test_dict_with_timestamp_key(self):
        result = hook.parse_ts({"timestamp": "2024-06-01T12:00:00Z"})
        assert isinstance(result, datetime)
        assert result.month == 6

    def test_none_returns_none(self):
        assert hook.parse_ts(None) is None

    def test_empty_string(self):
        assert hook.parse_ts("") is None

    def test_invalid_string(self):
        assert hook.parse_ts("not-a-date") is None

    def test_integer_returns_none(self):
        assert hook.parse_ts(12345) is None

    def test_dict_without_timestamp_key(self):
        assert hook.parse_ts({"foo": "bar"}) is None
