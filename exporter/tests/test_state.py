"""Tests for src.state — persistence, migration, and helpers."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.state import (
    FileEntry,
    IngestState,
    LastError,
    SenderState,
    allocate_seq_id,
    load_ingest_state,
    load_sender_state,
    migrate_legacy_state,
    record_commit,
    record_error,
    save_ingest_state,
    save_sender_state,
    update_file_entry,
)


class TestIngestStatePersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "ingest.json"
        state = IngestState(
            next_seq_id=42,
            files={"2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=10)},
            tokens_date="2024-01-01",
            tokens_input=100,
            tokens_output=200,
            tokens_cache_read=30,
            tokens_cache_creation=15,
        )
        save_ingest_state(path, state)
        loaded = load_ingest_state(path)

        assert loaded.next_seq_id == 42
        assert "2024-01-01.jsonl" in loaded.files
        assert loaded.files["2024-01-01.jsonl"].min_seq == 1
        assert loaded.files["2024-01-01.jsonl"].max_seq == 10
        assert loaded.tokens_date == "2024-01-01"
        assert loaded.tokens_input == 100
        assert loaded.tokens_output == 200
        assert loaded.tokens_cache_read == 30
        assert loaded.tokens_cache_creation == 15

    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        state = load_ingest_state(tmp_path / "missing.json")
        assert state.next_seq_id == 1
        assert state.files == {}
        assert state.tokens_input == 0

    def test_corrupt_file_returns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("not valid json {{{")
        state = load_ingest_state(path)
        assert state.next_seq_id == 1


class TestSenderStatePersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "sender.json"
        state = SenderState(
            commit_id=50,
            last_commit_at="2024-01-01T00:00:00+00:00",
            last_error=None,
        )
        save_sender_state(path, state)
        loaded = load_sender_state(path)

        assert loaded.commit_id == 50
        assert loaded.last_commit_at == "2024-01-01T00:00:00+00:00"
        assert loaded.last_error is None

    def test_round_trip_with_error(self, tmp_path: Path) -> None:
        path = tmp_path / "sender.json"
        state = SenderState(
            commit_id=10,
            last_commit_at="2024-01-01T00:00:00+00:00",
            last_error=LastError(
                time="2024-01-02T12:00:00+00:00",
                seq_id=11,
                error="HTTP 500",
                retries=3,
            ),
        )
        save_sender_state(path, state)
        loaded = load_sender_state(path)

        assert loaded.last_error is not None
        assert loaded.last_error.seq_id == 11
        assert loaded.last_error.error == "HTTP 500"
        assert loaded.last_error.retries == 3

    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        state = load_sender_state(tmp_path / "nope.json")
        assert state.commit_id == 0
        assert state.last_error is None

    def test_corrupt_file_returns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("<<<")
        state = load_sender_state(path)
        assert state.commit_id == 0


class TestMigrateLegacyState:
    def test_splits_legacy_into_ingest_and_sender(self, tmp_path: Path) -> None:
        legacy = tmp_path / "state.json"
        legacy.write_text(json.dumps({
            "next_seq_id": 20,
            "commit_id": 15,
            "last_commit_at": "2024-06-01T00:00:00+00:00",
            "files": {
                "2024-06-01.jsonl": {"min_seq": 1, "max_seq": 20},
            },
            "last_error": {
                "time": "2024-06-01T01:00:00+00:00",
                "seq_id": 16,
                "error": "timeout",
                "retries": 2,
            },
        }))
        ingest_path = tmp_path / "ingest.json"
        sender_path = tmp_path / "sender.json"

        result = migrate_legacy_state(legacy, ingest_path, sender_path)
        assert result is True

        ingest = load_ingest_state(ingest_path)
        assert ingest.next_seq_id == 20
        assert "2024-06-01.jsonl" in ingest.files

        sender = load_sender_state(sender_path)
        assert sender.commit_id == 15
        assert sender.last_error is not None
        assert sender.last_error.error == "timeout"

    def test_no_legacy_file(self, tmp_path: Path) -> None:
        result = migrate_legacy_state(
            tmp_path / "missing", tmp_path / "i.json", tmp_path / "s.json"
        )
        assert result is False

    def test_skips_if_target_exists(self, tmp_path: Path) -> None:
        legacy = tmp_path / "state.json"
        legacy.write_text("{}")
        ingest_path = tmp_path / "ingest.json"
        ingest_path.write_text("{}")
        result = migrate_legacy_state(legacy, ingest_path, tmp_path / "s.json")
        assert result is False

    def test_corrupt_legacy_returns_false(self, tmp_path: Path) -> None:
        legacy = tmp_path / "state.json"
        legacy.write_text("not json")
        result = migrate_legacy_state(
            legacy, tmp_path / "i.json", tmp_path / "s.json"
        )
        assert result is False


class TestAllocateSeqId:
    def test_increments(self) -> None:
        state = IngestState(next_seq_id=1)
        assert allocate_seq_id(state) == 1
        assert allocate_seq_id(state) == 2
        assert allocate_seq_id(state) == 3
        assert state.next_seq_id == 4


class TestRecordCommit:
    def test_updates_state(self) -> None:
        fixed = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        state = SenderState(
            commit_id=5,
            last_error=LastError(time="", seq_id=6, error="err", retries=1),
        )
        with patch("src.state.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            record_commit(state, 10)

        assert state.commit_id == 10
        assert state.last_error is None
        assert state.last_commit_at == fixed.isoformat()


class TestRecordError:
    def test_first_error(self) -> None:
        state = SenderState()
        fixed = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch("src.state.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            record_error(state, 7, "network error")

        assert state.last_error is not None
        assert state.last_error.seq_id == 7
        assert state.last_error.error == "network error"
        assert state.last_error.retries == 1

    def test_retry_increments(self) -> None:
        state = SenderState(
            last_error=LastError(time="t0", seq_id=7, error="old", retries=2),
        )
        fixed = datetime(2024, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
        with patch("src.state.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            record_error(state, 7, "still failing")

        assert state.last_error.retries == 3
        assert state.last_error.error == "still failing"

    def test_new_seq_resets_retries(self) -> None:
        state = SenderState(
            last_error=LastError(time="t0", seq_id=7, error="old", retries=5),
        )
        fixed = datetime(2024, 6, 1, 14, 0, 0, tzinfo=timezone.utc)
        with patch("src.state.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            record_error(state, 8, "new error")

        assert state.last_error.seq_id == 8
        assert state.last_error.retries == 1


class TestUpdateFileEntry:
    def test_new_file_creates_entry(self) -> None:
        state = IngestState()
        update_file_entry(state, "2024-01-01.jsonl", 5)
        assert state.files["2024-01-01.jsonl"].min_seq == 5
        assert state.files["2024-01-01.jsonl"].max_seq == 5

    def test_updates_max_seq(self) -> None:
        state = IngestState(
            files={"f.jsonl": FileEntry(min_seq=1, max_seq=3)},
        )
        update_file_entry(state, "f.jsonl", 7)
        assert state.files["f.jsonl"].min_seq == 1
        assert state.files["f.jsonl"].max_seq == 7

    def test_updates_min_seq(self) -> None:
        state = IngestState(
            files={"f.jsonl": FileEntry(min_seq=5, max_seq=10)},
        )
        update_file_entry(state, "f.jsonl", 2)
        assert state.files["f.jsonl"].min_seq == 2
        assert state.files["f.jsonl"].max_seq == 10
