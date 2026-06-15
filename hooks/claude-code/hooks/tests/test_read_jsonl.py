"""Tests for read_new_jsonl() from langfuse_hook."""

import json

import langfuse_hook as hook
from langfuse_hook import SessionState


def _write(path, text):
    path.write_text(text, encoding="utf-8")


class TestIncrementalRead:
    def test_reads_new_bytes_from_offset(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        line1 = json.dumps({"type": "user", "content": "hello"})
        line2 = json.dumps({"type": "assistant", "message": {"role": "assistant"}})
        _write(f, line1 + "\n" + line2 + "\n")

        ss = SessionState()

        # First read: gets both lines
        msgs, ss = hook.read_new_jsonl(f, ss)
        assert len(msgs) == 2
        assert ss.offset > 0

        # No new data: returns empty
        msgs2, ss = hook.read_new_jsonl(f, ss)
        assert msgs2 == []

        # Append a third line
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "content": "follow-up"}) + "\n")

        msgs3, ss = hook.read_new_jsonl(f, ss)
        assert len(msgs3) == 1
        assert msgs3[0]["content"] == "follow-up"


class TestPartialLineBuffer:
    def test_incomplete_line_buffered_and_completed(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        full_line = json.dumps({"type": "user", "content": "hello"})
        # Write first part of the JSON line without a newline
        partial = full_line[:10]
        _write(f, partial)

        ss = SessionState()
        msgs, ss = hook.read_new_jsonl(f, ss)
        # No complete line yet
        assert msgs == []
        assert ss.buffer == partial

        # Write the rest plus newline
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(full_line[10:] + "\n")

        msgs2, ss = hook.read_new_jsonl(f, ss)
        assert len(msgs2) == 1
        assert msgs2[0]["content"] == "hello"
        assert ss.buffer == ""


class TestFileTruncation:
    def test_detects_shrink_and_resets(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        line1 = json.dumps({"type": "user", "content": "original"}) + "\n"
        _write(f, line1)

        ss = SessionState()
        msgs, ss = hook.read_new_jsonl(f, ss)
        assert len(msgs) == 1
        old_offset = ss.offset

        # Truncate the file (simulate rotation) and write shorter content
        new_content = json.dumps({"type": "user", "content": "new"}) + "\n"
        _write(f, new_content)
        assert f.stat().st_size < old_offset

        msgs2, ss = hook.read_new_jsonl(f, ss)
        assert len(msgs2) == 1
        assert msgs2[0]["content"] == "new"


class TestEmptyFile:
    def test_empty_file_returns_empty(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write(f, "")

        ss = SessionState()
        msgs, ss = hook.read_new_jsonl(f, ss)
        assert msgs == []

    def test_nonexistent_file_returns_empty(self, tmp_path):
        f = tmp_path / "does_not_exist.jsonl"
        ss = SessionState()
        msgs, ss = hook.read_new_jsonl(f, ss)
        assert msgs == []


class TestMalformedLines:
    def test_invalid_json_lines_skipped(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        good = json.dumps({"type": "user", "content": "ok"})
        _write(f, "not json\n" + good + "\n")

        ss = SessionState()
        msgs, ss = hook.read_new_jsonl(f, ss)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "ok"
