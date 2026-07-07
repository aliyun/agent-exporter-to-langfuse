"""Tests for cursor entry in src.hook_state._builtin_agent_definitions()."""

from src.hook_state import _builtin_agent_definitions


class TestCursorHookStateEntry:
    """Verify the real _builtin_agent_definitions() returns a cursor entry with expected fields."""

    def test_cursor_entry_exists(self) -> None:
        defs = _builtin_agent_definitions()
        cursor = next((d for d in defs if d.get("id") == "cursor"), None)
        assert cursor is not None, "cursor entry should exist in _builtin_agent_definitions()"

    def test_cursor_display_name(self) -> None:
        defs = _builtin_agent_definitions()
        cursor = next(d for d in defs if d.get("id") == "cursor")
        assert cursor["displayName"] == "Cursor", f"displayName should be 'Cursor', got: {cursor['displayName']}"

    def test_cursor_detection_paths_contains_cursor_dir(self) -> None:
        defs = _builtin_agent_definitions()
        cursor = next(d for d in defs if d.get("id") == "cursor")
        paths = cursor["detection"]["paths"]
        assert any(".cursor" in p for p in paths), f"detection.paths should contain ~/.cursor, got: {paths}"

    def test_cursor_detection_no_commands(self) -> None:
        """Cursor is GUI-only IDE — no CLI command detection."""
        defs = _builtin_agent_definitions()
        cursor = next(d for d in defs if d.get("id") == "cursor")
        commands = cursor["detection"].get("commands", [])
        assert commands == [], f"cursor detection should have no commands (GUI-only), got: {commands}"

    def test_cursor_hook_settings_path(self) -> None:
        defs = _builtin_agent_definitions()
        cursor = next(d for d in defs if d.get("id") == "cursor")
        settings_path = cursor["hook"]["settingsPath"]
        assert ".cursor/hooks.json" in settings_path, f"settingsPath should be ~/.cursor/hooks.json, got: {settings_path}"

    def test_cursor_hook_markers_contain_langfuse(self) -> None:
        defs = _builtin_agent_definitions()
        cursor = next(d for d in defs if d.get("id") == "cursor")
        markers = cursor["hook"]["markers"]
        assert "langfuse" in markers, f"markers should contain 'langfuse', got: {markers}"

    def test_cursor_hook_markers_contain_langstash_deliver(self) -> None:
        defs = _builtin_agent_definitions()
        cursor = next(d for d in defs if d.get("id") == "cursor")
        markers = cursor["hook"]["markers"]
        assert "langstash-deliver" in markers, f"markers should contain 'langstash-deliver', got: {markers}"
