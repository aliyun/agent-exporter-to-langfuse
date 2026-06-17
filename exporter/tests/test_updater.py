"""Tests for src.updater — _parse_semver and start_upgrade."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.updater import _parse_semver, start_upgrade


class TestParseSemver:
    def test_stable_version(self) -> None:
        result = _parse_semver("1.2.3")
        assert result == (1, 2, 3, 1, "")

    def test_stable_with_v_prefix(self) -> None:
        result = _parse_semver("v1.2.3")
        assert result == (1, 2, 3, 1, "")

    def test_prerelease_alpha(self) -> None:
        result = _parse_semver("v0.1.0-alpha")
        assert result == (0, 1, 0, 0, "alpha")

    def test_prerelease_beta_numbered(self) -> None:
        result = _parse_semver("v2.0.0-beta.3")
        assert result == (2, 0, 0, 0, "beta.3")

    def test_invalid_returns_zeros(self) -> None:
        result = _parse_semver("not-a-version")
        assert result == (0, 0, 0, 0, "")

    def test_empty_string(self) -> None:
        result = _parse_semver("")
        assert result == (0, 0, 0, 0, "")

    def test_stable_sorts_higher_than_prerelease(self) -> None:
        stable = _parse_semver("1.0.0")
        pre = _parse_semver("1.0.0-alpha")
        assert stable > pre

    def test_higher_major_wins(self) -> None:
        assert _parse_semver("2.0.0") > _parse_semver("1.9.9")

    def test_higher_minor_wins(self) -> None:
        assert _parse_semver("1.2.0") > _parse_semver("1.1.9")

    def test_higher_patch_wins(self) -> None:
        assert _parse_semver("1.0.2") > _parse_semver("1.0.1")

    def test_whitespace_stripped(self) -> None:
        result = _parse_semver("  v1.0.0  ")
        assert result == (1, 0, 0, 1, "")

    def test_prerelease_rc(self) -> None:
        result = _parse_semver("v3.1.0-rc.1")
        assert result == (3, 1, 0, 0, "rc.1")


class TestStartUpgradeRetryAgent:
    @patch("src.updater.subprocess.Popen")
    @patch("src.updater.find_installer")
    def test_retry_hooks_without_agent(self, mock_find, mock_popen, tmp_path: Path) -> None:
        installer = tmp_path / "installer.sh"
        installer.touch()
        mock_find.return_value = installer
        with patch("src.updater.UPGRADE_LOG", tmp_path / "upgrade.log"):
            result = start_upgrade(retry_hooks=True)
        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert cmd == ["bash", str(installer), "upgrade", "--retry-hooks"]

    @patch("src.updater.subprocess.Popen")
    @patch("src.updater.find_installer")
    def test_retry_hooks_with_agent(self, mock_find, mock_popen, tmp_path: Path) -> None:
        installer = tmp_path / "installer.sh"
        installer.touch()
        mock_find.return_value = installer
        with patch("src.updater.UPGRADE_LOG", tmp_path / "upgrade.log"):
            result = start_upgrade(retry_hooks=True, retry_agent="cursor")
        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert cmd == ["bash", str(installer), "upgrade", "--retry-hooks", "--agent", "cursor"]

    @patch("src.updater.subprocess.Popen")
    @patch("src.updater.find_installer")
    def test_retry_agent_ignored_without_retry_hooks(self, mock_find, mock_popen, tmp_path: Path) -> None:
        mock_find.return_value = None
        result = start_upgrade(retry_hooks=False, retry_agent="cursor")
        assert result is False
