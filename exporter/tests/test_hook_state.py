"""Tests for src.hook_state — hook state management and startup probe."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.hook_state import (
    HookStatus,
    get_mismatch_info,
    load_hook_state,
    probe_hook_states,
    save_hook_state,
    update_hook_entry,
)


@pytest.fixture(autouse=True)
def _patch_paths(tmp_path: Path):
    with patch("src.hook_state.HOOK_STATE_FILE", tmp_path / "hook-state.json"), \
         patch("src.hook_state.INSTALL_DIR", tmp_path):
        yield


class TestLoadSaveHookState:
    def test_load_empty(self, tmp_path: Path) -> None:
        state = load_hook_state()
        assert state == {}

    def test_save_and_load(self, tmp_path: Path) -> None:
        data = {"claude-code": {"version": "0.3.0", "status": "installed"}}
        save_hook_state(data)
        loaded = load_hook_state()
        assert loaded == data

    def test_load_corrupt_json(self, tmp_path: Path) -> None:
        state_file = tmp_path / "hook-state.json"
        state_file.write_text("not json")
        state = load_hook_state()
        assert state == {}

    def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        with patch("src.hook_state.HOOK_STATE_FILE", tmp_path / "sub" / "hook-state.json"):
            save_hook_state({"x": {"status": "installed"}})


class TestUpdateHookEntry:
    def test_set_installed(self, tmp_path: Path) -> None:
        update_hook_entry("claude-code", HookStatus.INSTALLED, version="0.3.0")
        state = load_hook_state()
        assert state["claude-code"]["status"] == "installed"
        assert state["claude-code"]["version"] == "0.3.0"

    def test_set_error_with_message(self, tmp_path: Path) -> None:
        update_hook_entry("qoder", HookStatus.ERROR, version="0.2.0", error="uv sync failed")
        state = load_hook_state()
        assert state["qoder"]["status"] == "error"
        assert state["qoder"]["error"] == "uv sync failed"

    def test_set_not_installed(self, tmp_path: Path) -> None:
        update_hook_entry("codex", HookStatus.NOT_INSTALLED)
        state = load_hook_state()
        assert state["codex"]["status"] == "not_installed"
        assert "version" not in state["codex"]

    def test_update_preserves_other_agents(self, tmp_path: Path) -> None:
        update_hook_entry("claude-code", HookStatus.INSTALLED, version="0.3.0")
        update_hook_entry("qoder", HookStatus.ERROR, version="0.2.0", error="fail")
        state = load_hook_state()
        assert "claude-code" in state
        assert "qoder" in state


class TestHookStatusEnum:
    def test_four_states(self) -> None:
        assert len(HookStatus) == 4
        assert HookStatus.UNDETECTED.value == "undetected"
        assert HookStatus.NOT_INSTALLED.value == "not_installed"
        assert HookStatus.INSTALLED.value == "installed"
        assert HookStatus.ERROR.value == "error"


class TestGetMismatchInfo:
    def test_no_mismatch(self, tmp_path: Path) -> None:
        (tmp_path / "current").write_text("0.3.0")
        state = {
            "claude-code": {"version": "0.3.0", "status": "installed"},
            "qoder": {"version": "0.3.0", "status": "installed"},
        }
        mismatch, agents = get_mismatch_info(state)
        assert mismatch is False
        assert agents == []

    def test_detects_mismatch(self, tmp_path: Path) -> None:
        (tmp_path / "current").write_text("0.3.0")
        state = {
            "claude-code": {"version": "0.3.0", "status": "installed"},
            "qoder": {"version": "0.2.0", "status": "installed"},
        }
        mismatch, agents = get_mismatch_info(state)
        assert mismatch is True
        assert "qoder" in agents

    def test_ignores_non_installed(self, tmp_path: Path) -> None:
        (tmp_path / "current").write_text("0.3.0")
        state = {
            "claude-code": {"version": "0.3.0", "status": "installed"},
            "codex": {"version": "0.2.0", "status": "error"},
        }
        mismatch, agents = get_mismatch_info(state)
        assert mismatch is False

    def test_no_current_pointer(self, tmp_path: Path) -> None:
        state = {"claude-code": {"version": "0.3.0", "status": "installed"}}
        mismatch, agents = get_mismatch_info(state)
        assert mismatch is False


class TestProbeHookStates:
    def test_detects_uninstalled_agent(self, tmp_path: Path) -> None:
        defs = [
            {
                "id": "test-agent",
                "detection": {"paths": [str(tmp_path / "agent-dir")]},
                "hook": {"settingsPath": str(tmp_path / "settings.json"), "markers": ["langstash"]},
            }
        ]
        (tmp_path / "agent-dir").mkdir()
        with patch("src.hook_state._builtin_agent_definitions", return_value=defs):
            state = probe_hook_states()
        assert state["test-agent"]["status"] == "not_installed"

    def test_detects_undetected_agent(self, tmp_path: Path) -> None:
        defs = [
            {
                "id": "missing-agent",
                "detection": {"paths": [str(tmp_path / "nonexistent")]},
                "hook": {"settingsPath": "", "markers": []},
            }
        ]
        with patch("src.hook_state._builtin_agent_definitions", return_value=defs):
            state = probe_hook_states()
        assert state["missing-agent"]["status"] == "undetected"

    def test_marks_error_when_hook_overwritten(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-dir"
        agent_dir.mkdir()
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {"Stop": "other-command"}}))

        defs = [
            {
                "id": "test-agent",
                "detection": {"paths": [str(agent_dir)]},
                "hook": {"settingsPath": str(settings_file), "markers": ["langstash-deliver"]},
            }
        ]
        save_hook_state({"test-agent": {"version": "0.3.0", "status": "installed"}})
        with patch("src.hook_state._builtin_agent_definitions", return_value=defs):
            state = probe_hook_states()
        assert state["test-agent"]["status"] == "error"
        assert "overwritten" in state["test-agent"]["error"]

    def test_keeps_installed_when_markers_present(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-dir"
        agent_dir.mkdir()
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {"Stop": "langstash-deliver something"}}))

        defs = [
            {
                "id": "test-agent",
                "detection": {"paths": [str(agent_dir)]},
                "hook": {"settingsPath": str(settings_file), "markers": ["langstash-deliver"]},
            }
        ]
        save_hook_state({"test-agent": {"version": "0.3.0", "status": "installed"}})
        with patch("src.hook_state._builtin_agent_definitions", return_value=defs):
            state = probe_hook_states()
        assert state["test-agent"]["status"] == "installed"
