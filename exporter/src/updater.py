import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from src.config import INSTALL_DIR

logger = logging.getLogger("langstash.updater")

UPDATE_CHECK_FILE = INSTALL_DIR / ".update-check"
VERSION_FILE = INSTALL_DIR / "VERSION"
DEFAULT_REPO = "aliyun/agent-exporter-to-langfuse"

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")


def _parse_semver(v: str) -> tuple[int, int, int, int, str]:
    m = _SEMVER_RE.match(v.strip())
    if not m:
        return (0, 0, 0, 0, "")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre = m.group(4) or ""
    # no pre-release (stable) sorts higher than any pre-release
    return (major, minor, patch, 0 if pre else 1, pre)


def _read_local_version() -> str:
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "0.0.0"


def _check_latest_tag(repo: str, local_version: str, include_prerelease: bool = False) -> tuple[bool, str]:
    repo_url = f"https://github.com/{repo}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--sort=-v:refname", repo_url, "v*"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return False, local_version
        for line in result.stdout.splitlines():
            ref = line.split("refs/tags/")[-1] if "refs/tags/" in line else ""
            if not ref or ref.endswith("^{}") or not re.match(r"^v\d", ref):
                continue
            if not include_prerelease and "-" in ref:
                continue
            remote = ref.lstrip("v")
            return _parse_semver(remote) > _parse_semver(local_version), remote
    except Exception:
        pass
    return False, local_version


def _write_check_file(local: str, remote: str, available: bool) -> None:
    epoch = int(time.time() / 86400)
    UPDATE_CHECK_FILE.write_text(
        f"LAST_CHECK_EPOCH={epoch}\n"
        f"REMOTE_VERSION={remote}\n"
        f"LOCAL_VERSION={local}\n"
        f"UPDATE_AVAILABLE={'true' if available else 'false'}\n"
    )


def _read_check_file() -> dict[str, Any]:
    if not UPDATE_CHECK_FILE.exists():
        return {}
    result: dict[str, Any] = {}
    for line in UPDATE_CHECK_FILE.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def get_update_info() -> dict[str, Any]:
    local = _read_local_version()
    cached = _read_check_file()
    return {
        "update_available": cached.get("UPDATE_AVAILABLE") == "true",
        "current_version": local,
        "latest_version": cached.get("REMOTE_VERSION", local),
    }


UPGRADE_LOG = INSTALL_DIR / "logs" / "upgrade.log"


def start_upgrade(include_prerelease: bool = False) -> bool:
    upgrade_script = INSTALL_DIR / "upgrade.sh"
    if not upgrade_script.exists():
        return False
    cmd = ["bash", str(upgrade_script)]
    if include_prerelease:
        cmd.append("--pre-release")
    UPGRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(UPGRADE_LOG, "w") as log:
        subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(INSTALL_DIR),
        )
    return True


class Updater:
    def __init__(self, frequency_hours: int = 24, repo: str = DEFAULT_REPO,
                 include_prerelease: bool = False):
        self._frequency = frequency_hours * 3600
        self._repo = repo
        self._include_prerelease = include_prerelease
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="langstash-updater")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._check()
            except Exception as e:
                logger.debug("update check failed: %s", e)
            self._stop.wait(self._frequency)

    def _check(self) -> None:
        cached = _read_check_file()
        last_epoch = int(cached.get("LAST_CHECK_EPOCH", 0))
        current_epoch = int(time.time() / 86400)
        freq_days = max(1, self._frequency // 86400)
        if current_epoch - last_epoch < freq_days:
            return

        local = _read_local_version()
        available, remote = _check_latest_tag(self._repo, local, self._include_prerelease)
        _write_check_file(local, remote, available)
        if available:
            logger.info("update available: v%s → v%s", local, remote)
