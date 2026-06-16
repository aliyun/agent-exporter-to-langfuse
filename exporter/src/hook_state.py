import json
import logging
import os
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any

from src.config import INSTALL_DIR

logger = logging.getLogger("langstash.hook_state")

HOOK_STATE_FILE = INSTALL_DIR / "hook-state.json"

AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents.d"


class HookStatus(str, Enum):
    UNDETECTED = "undetected"
    NOT_INSTALLED = "not_installed"
    INSTALLED = "installed"
    ERROR = "error"


AGENT_DEFINITIONS: list[dict[str, Any]] = []


def _load_agent_definitions() -> list[dict[str, Any]]:
    global AGENT_DEFINITIONS
    if AGENT_DEFINITIONS:
        return AGENT_DEFINITIONS

    defs = []
    search_paths = [AGENTS_DIR]

    current = _read_current_version()
    if current:
        ver_agents = INSTALL_DIR / "versions" / current / "agents.d"
        if ver_agents.is_dir():
            search_paths.insert(0, ver_agents)

    for agents_dir in search_paths:
        if not agents_dir.is_dir():
            continue
        for f in sorted(agents_dir.glob("*.json")):
            try:
                with open(f) as fh:
                    defn = json.load(fh)
                    if defn.get("id") not in [d.get("id") for d in defs]:
                        defs.append(defn)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load agent definition %s: %s", f, e)

    if not defs:
        defs = _builtin_agent_definitions()

    AGENT_DEFINITIONS = defs
    return defs


def _builtin_agent_definitions() -> list[dict[str, Any]]:
    home = Path.home()
    return [
        {
            "id": "claude-code",
            "displayName": "Claude Code",
            "detection": {"paths": [str(home / ".claude")], "commands": ["claude"]},
            "hook": {
                "settingsPath": str(home / ".claude" / "settings.json"),
                "markers": ["langstash-deliver", "langstash_deliver", "langfuse-entrypoint", "langfuse_hook"],
                "pluginCheck": str(home / ".claude" / "plugins" / "installed_plugins.json"),
                "pluginMarker": "langfuse",
            },
        },
        {
            "id": "qoder",
            "displayName": "Qoder",
            "detection": {"paths": [str(home / ".qoder")], "commands": ["qoder", "qodercli"]},
            "hook": {
                "settingsPath": str(home / ".qoder" / "settings.json"),
                "markers": ["langstash-deliver", "langstash_deliver", "langfuse-entrypoint", "langfuse_hook"],
            },
        },
        {
            "id": "qoderwork",
            "displayName": "QoderWork",
            "detection": {"paths": [str(home / ".qoderwork")]},
            "hook": {
                "settingsPath": str(home / ".qoderwork" / "settings.json"),
                "markers": ["langstash-deliver", "langstash_deliver", "langfuse-entrypoint", "langfuse_hook"],
            },
        },
        {
            "id": "opencode",
            "displayName": "OpenCode",
            "detection": {"paths": [str(home / ".config" / "opencode")], "commands": ["opencode"]},
            "hook": {
                "settingsPath": str(home / ".config" / "opencode" / "hooks.json"),
                "markers": ["langstash-deliver", "langstash_deliver", "langfuse-exporter"],
                "fileCheck": str(home / ".config" / "opencode" / "plugins" / "langfuse-exporter.mjs"),
            },
        },
        {
            "id": "codex",
            "displayName": "Codex",
            "detection": {"paths": [str(home / ".codex")], "commands": ["codex"]},
            "hook": {
                "settingsPath": str(home / ".codex" / "hooks.json"),
                "markers": ["langstash-deliver", "langstash_deliver", "langfuse-entrypoint", "hooks/langfuse"],
            },
        },
    ]


def _read_current_version() -> str:
    pointer = INSTALL_DIR / "current"
    if pointer.is_file():
        return pointer.read_text().strip()
    return ""


def load_hook_state() -> dict[str, dict[str, Any]]:
    if not HOOK_STATE_FILE.is_file():
        return {}
    try:
        with open(HOOK_STATE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load hook-state.json: %s", e)
        return {}


def save_hook_state(state: dict[str, dict[str, Any]]) -> None:
    HOOK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOOK_STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(str(tmp), str(HOOK_STATE_FILE))
    except OSError as e:
        logger.error("Failed to save hook-state.json: %s", e)
        tmp.unlink(missing_ok=True)


def update_hook_entry(
    agent: str,
    status: HookStatus,
    version: str | None = None,
    error: str | None = None,
) -> None:
    state = load_hook_state()
    entry: dict[str, Any] = {"status": status.value}
    if version:
        entry["version"] = version
    if error:
        entry["error"] = error
    state[agent] = entry
    save_hook_state(state)


def _detect_agent_installed(defn: dict[str, Any]) -> bool:
    detection = defn.get("detection", {})

    for p in detection.get("paths", []):
        expanded = os.path.expanduser(p)
        if os.path.exists(expanded):
            return True

    for cmd in detection.get("commands", []):
        if shutil.which(cmd):
            return True

    return False


def _check_hook_markers(defn: dict[str, Any]) -> bool:
    hook_conf = defn.get("hook", {})
    markers = hook_conf.get("markers", [])

    # Check plugin install (e.g., Claude Code plugin system)
    plugin_check = hook_conf.get("pluginCheck", "")
    plugin_marker = hook_conf.get("pluginMarker", "")
    if plugin_check and plugin_marker:
        expanded = os.path.expanduser(plugin_check)
        if os.path.isfile(expanded):
            try:
                with open(expanded) as f:
                    content = f.read()
                if plugin_marker in content.lower():
                    return True
            except OSError:
                pass

    # Check file existence (e.g., OpenCode plugin file)
    file_check = hook_conf.get("fileCheck", "")
    if file_check:
        if os.path.isfile(os.path.expanduser(file_check)):
            return True

    # Check settings/hooks config file for markers
    settings_path = hook_conf.get("settingsPath", "")
    if not settings_path or not markers:
        return False

    expanded = os.path.expanduser(settings_path)
    if not os.path.isfile(expanded):
        return False

    try:
        with open(expanded) as f:
            content = f.read()
        return any(m in content for m in markers)
    except OSError:
        return False


def probe_hook_states() -> dict[str, dict[str, Any]]:
    state = load_hook_state()
    defs = _load_agent_definitions()

    for defn in defs:
        agent_id = defn["id"]
        current_entry = state.get(agent_id, {})
        current_status = current_entry.get("status", "")

        agent_present = _detect_agent_installed(defn)

        if not agent_present:
            if current_status != HookStatus.INSTALLED.value:
                state[agent_id] = {"status": HookStatus.UNDETECTED.value}
            continue

        if current_status == HookStatus.INSTALLED.value:
            if not _check_hook_markers(defn):
                state[agent_id] = {
                    "status": HookStatus.ERROR.value,
                    "version": current_entry.get("version", ""),
                    "error": "hook entry missing or overwritten in agent settings",
                }
        elif current_status == HookStatus.ERROR.value:
            pass
        elif current_status == HookStatus.UNDETECTED.value or current_status == "":
            state[agent_id] = {"status": HookStatus.NOT_INSTALLED.value}

    save_hook_state(state)
    return state


def get_mismatch_info(
    state: dict[str, dict[str, Any]],
) -> tuple[bool, list[str]]:
    current_ver = _read_current_version()
    if not current_ver:
        return False, []

    mismatch_agents = []
    for agent_id, entry in state.items():
        if entry.get("status") != HookStatus.INSTALLED.value:
            continue
        hook_ver = entry.get("version", "")
        if hook_ver and hook_ver != current_ver:
            mismatch_agents.append(agent_id)

    return bool(mismatch_agents), mismatch_agents
