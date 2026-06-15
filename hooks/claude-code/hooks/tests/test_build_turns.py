"""Tests for build_turns() from langfuse_hook."""

import langfuse_hook as hook


def _user_msg(text="hello", ts="2024-01-01T00:00:00Z"):
    return {"type": "user", "content": text, "timestamp": ts}


def _assistant_msg(text="hi", msg_id="msg_001", ts="2024-01-01T00:00:01Z", model="claude-3.5-sonnet"):
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": ts,
    }


def _tool_use_assistant(tool_id="tu1", tool_name="Bash", tool_input=None, msg_id="msg_002", ts="2024-01-01T00:00:02Z"):
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-3.5-sonnet",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input or {"cmd": "ls"}},
            ],
        },
        "timestamp": ts,
    }


def _tool_result_msg(tool_use_id="tu1", content="result text", ts="2024-01-01T00:00:03Z"):
    return {
        "type": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        "timestamp": ts,
    }


class TestSingleTurn:
    def test_user_then_assistant(self):
        msgs = [_user_msg("what is 1+1?"), _assistant_msg("2")]
        turns = hook.build_turns(msgs)
        assert len(turns) == 1
        turn = turns[0]
        assert turn.user_msg["content"] == "what is 1+1?"
        assert len(turn.assistant_msgs) == 1
        assert turn.tool_results_by_id == {}


class TestMultiMessageDedup:
    def test_same_message_id_latest_wins(self):
        """Multiple assistant rows with the same message.id: the last one wins."""
        msgs = [
            _user_msg(),
            _assistant_msg(text="partial", msg_id="msg_A", ts="2024-01-01T00:00:01Z"),
            _assistant_msg(text="complete", msg_id="msg_A", ts="2024-01-01T00:00:02Z"),
        ]
        turns = hook.build_turns(msgs)
        assert len(turns) == 1
        assert len(turns[0].assistant_msgs) == 1
        content = hook.get_content(turns[0].assistant_msgs[0])
        assert hook.extract_text(content) == "complete"


class TestToolResultAssociation:
    def test_tool_results_matched_by_id(self):
        msgs = [
            _user_msg(),
            _tool_use_assistant(tool_id="tu1", tool_name="Bash"),
            _tool_result_msg(tool_use_id="tu1", content="file1.txt"),
        ]
        turns = hook.build_turns(msgs)
        assert len(turns) == 1
        assert "tu1" in turns[0].tool_results_by_id
        assert turns[0].tool_results_by_id["tu1"]["content"] == "file1.txt"

    def test_multiple_tool_results(self):
        msgs = [
            _user_msg(),
            _tool_use_assistant(tool_id="tu1", msg_id="msg_A"),
            _tool_result_msg(tool_use_id="tu1", content="r1"),
            _tool_use_assistant(tool_id="tu2", msg_id="msg_B"),
            _tool_result_msg(tool_use_id="tu2", content="r2"),
        ]
        turns = hook.build_turns(msgs)
        assert len(turns) == 1
        assert turns[0].tool_results_by_id["tu1"]["content"] == "r1"
        assert turns[0].tool_results_by_id["tu2"]["content"] == "r2"


class TestMultipleTurns:
    def test_second_user_flushes_first(self):
        msgs = [
            _user_msg("q1", ts="2024-01-01T00:00:00Z"),
            _assistant_msg("a1", msg_id="m1", ts="2024-01-01T00:00:01Z"),
            _user_msg("q2", ts="2024-01-01T00:01:00Z"),
            _assistant_msg("a2", msg_id="m2", ts="2024-01-01T00:01:01Z"),
        ]
        turns = hook.build_turns(msgs)
        assert len(turns) == 2
        assert turns[0].user_msg["content"] == "q1"
        assert turns[1].user_msg["content"] == "q2"


class TestEdgeCases:
    def test_empty_input(self):
        assert hook.build_turns([]) == []

    def test_assistant_without_user_ignored(self):
        """Assistant messages appearing before any user message are dropped."""
        msgs = [
            _assistant_msg("orphan", msg_id="m0"),
        ]
        turns = hook.build_turns(msgs)
        assert turns == []

    def test_user_without_assistant_not_emitted(self):
        """A user message with no following assistant message produces no turn."""
        msgs = [_user_msg("lonely")]
        turns = hook.build_turns(msgs)
        assert turns == []

    def test_tool_result_before_tool_use_still_stored(self):
        """tool_result rows are stored by tool_use_id regardless of order."""
        msgs = [
            _user_msg(),
            _tool_result_msg(tool_use_id="tu_early", content="early result"),
            _assistant_msg("done", msg_id="m1"),
        ]
        turns = hook.build_turns(msgs)
        assert len(turns) == 1
        assert "tu_early" in turns[0].tool_results_by_id
