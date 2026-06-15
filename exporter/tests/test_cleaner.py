"""Tests for src.cleaner — _cleanup_retention, _cleanup_size, _dir_size_mb."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from src.cleaner import _cleanup_retention, _cleanup_size, _dir_size_mb
from src.state import FileEntry, IngestState


class TestDirSizeMb:
    def test_empty_directory(self, tmp_path: Path) -> None:
        assert _dir_size_mb(tmp_path) == 0.0

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        assert _dir_size_mb(tmp_path / "nope") == 0.0

    def test_calculates_size(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f1.write_bytes(b"x" * 1024)  # 1 KB
        f2 = tmp_path / "b.txt"
        f2.write_bytes(b"y" * 1024)  # 1 KB
        size = _dir_size_mb(tmp_path)
        assert abs(size - 2.0 / 1024) < 0.001  # ~0.00195 MB

    def test_includes_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "file.bin").write_bytes(b"z" * 2048)
        size = _dir_size_mb(tmp_path)
        assert size > 0


class TestCleanupRetention:
    def test_removes_old_committed_files(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        old_file = pending_dir / "2024-01-01.jsonl"
        old_file.write_text("data\n")
        recent_file = pending_dir / "2024-06-10.jsonl"
        recent_file.write_text("data\n")

        state = IngestState(files={
            "2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=5),
            "2024-06-10.jsonl": FileEntry(min_seq=6, max_seq=10),
        })
        # Both files are committed (commit_id=10)
        # Retention = 7 days, fixed "now" = 2024-06-15
        fixed_now = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        with patch("src.cleaner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            removed = _cleanup_retention(tmp_path, state, commit_id=10, retention_days=7)

        assert removed == 1
        assert not old_file.exists()
        assert recent_file.exists()
        assert "2024-01-01.jsonl" not in state.files
        assert "2024-06-10.jsonl" in state.files

    def test_skips_uncommitted_files(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        old_file = pending_dir / "2023-01-01.jsonl"
        old_file.write_text("data\n")

        state = IngestState(files={
            "2023-01-01.jsonl": FileEntry(min_seq=1, max_seq=5),
        })
        # commit_id=0 means nothing committed
        fixed_now = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        with patch("src.cleaner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            removed = _cleanup_retention(tmp_path, state, commit_id=0, retention_days=7)

        assert removed == 0
        assert old_file.exists()

    def test_no_files_to_remove(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        state = IngestState()
        fixed_now = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        with patch("src.cleaner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            removed = _cleanup_retention(tmp_path, state, commit_id=0, retention_days=30)
        assert removed == 0


class TestCleanupSize:
    def test_no_removal_when_under_limit(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        (pending_dir / "2024-01-01.jsonl").write_bytes(b"x" * 100)

        state = IngestState(files={
            "2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=5),
        })
        removed = _cleanup_size(tmp_path, state, commit_id=5, max_size_gb=1.0)
        assert removed == 0

    def test_removes_committed_first(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        # Create files that exceed the size limit
        committed_file = pending_dir / "2024-01-01.jsonl"
        committed_file.write_bytes(b"x" * 2048)
        uncommitted_file = pending_dir / "2024-06-15.jsonl"
        uncommitted_file.write_bytes(b"y" * 1024)

        state = IngestState(files={
            "2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=5),
            "2024-06-15.jsonl": FileEntry(min_seq=6, max_seq=10),
        })
        # Set a very low size limit (< total)
        max_size_gb = 1.5 / (1024 * 1024)  # ~1.5 KB in GB
        removed = _cleanup_size(tmp_path, state, commit_id=5, max_size_gb=max_size_gb)

        assert removed >= 1
        assert not committed_file.exists()

    def test_removes_failed_after_committed(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()

        # No committed files, but failed files exist
        (failed_dir / "2024-01-01.jsonl").write_bytes(b"x" * 2048)
        uncommitted_file = pending_dir / "2024-06-15.jsonl"
        uncommitted_file.write_bytes(b"y" * 1024)

        state = IngestState(files={
            "2024-06-15.jsonl": FileEntry(min_seq=1, max_seq=5),
        })
        max_size_gb = 1.5 / (1024 * 1024)  # tiny limit
        removed = _cleanup_size(tmp_path, state, commit_id=0, max_size_gb=max_size_gb)

        assert removed >= 1

    def test_removes_uncommitted_as_last_resort(self, tmp_path: Path) -> None:
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()

        # Only uncommitted files
        f1 = pending_dir / "2024-01-01.jsonl"
        f1.write_bytes(b"x" * 2048)
        f2 = pending_dir / "2024-06-15.jsonl"
        f2.write_bytes(b"y" * 2048)

        state = IngestState(files={
            "2024-01-01.jsonl": FileEntry(min_seq=1, max_seq=3),
            "2024-06-15.jsonl": FileEntry(min_seq=4, max_seq=6),
        })
        # Very small limit to force removal
        max_size_gb = 1.0 / (1024 * 1024)  # ~1 KB in GB
        removed = _cleanup_size(tmp_path, state, commit_id=0, max_size_gb=max_size_gb)

        assert removed >= 1
