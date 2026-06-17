import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

import uvicorn

from src.cleaner import Cleaner
from src.config import INSTALL_DIR, find_installer, load_config
from src.ingestor import FailedRecovery
from src.sender import Sender
from src.server import create_app
from src.state import (
    load_ingest_state, load_sender_state, migrate_legacy_state,
)
from src.stats import Stats
from src.hook_state import get_mismatch_info, probe_hook_states
from src.updater import Updater


def _setup_logging(debug: bool = False) -> logging.Logger:
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(level)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    return logging.getLogger("langstash")


def _run_installer(subcommand: str, *args: str) -> int:
    installer = find_installer()
    if not installer:
        print("ERROR: installer.sh not found", file=sys.stderr)
        return 1
    cmd = ["bash", str(installer), subcommand, *args]
    result = subprocess.run(cmd)
    return result.returncode


def _service_ctl(action: str) -> int:
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.langstash.plist"
        if not plist.exists():
            print("ERROR: LaunchAgent not found", file=sys.stderr)
            return 1
        if action == "start":
            subprocess.run(["launchctl", "load", str(plist)])
        elif action == "stop":
            subprocess.run(["launchctl", "unload", str(plist)])
        elif action == "restart":
            subprocess.run(["launchctl", "unload", str(plist)])
            subprocess.run(["launchctl", "load", str(plist)])
    else:
        if action == "start":
            subprocess.run(["systemctl", "--user", "start", "langstash"])
        elif action == "stop":
            subprocess.run(["systemctl", "--user", "stop", "langstash"])
        elif action == "restart":
            subprocess.run(["systemctl", "--user", "restart", "langstash"])
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    return _service_ctl("start")


def cmd_stop(args: argparse.Namespace) -> int:
    return _service_ctl("stop")


def cmd_restart(args: argparse.Namespace) -> int:
    return _service_ctl("restart")


def cmd_status(args: argparse.Namespace) -> int:
    import urllib.request
    import json

    from src.updater import _read_local_version

    version = _read_local_version()
    print(f"langstash v{version}")

    try:
        with urllib.request.urlopen("http://127.0.0.1:5288/health", timeout=3) as resp:
            data = json.loads(resp.read())
        print(f"Status: {data.get('status', 'unknown')}")
        print(f"Version: {data.get('version', 'unknown')}")
    except Exception:
        print("Status: not running")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    extra: list[str] = []
    if args.retry_hooks:
        extra.append("--retry-hooks")
    elif args.version:
        extra.extend(["--version", args.version])
    return _run_installer("upgrade", *extra)


def cmd_rollback(args: argparse.Namespace) -> int:
    return _run_installer("rollback")


def cmd_uninstall(args: argparse.Namespace) -> int:
    extra: list[str] = []
    if args.purge:
        extra.append("--purge")
    return _run_installer("uninstall", *extra)


def cmd_run(args: argparse.Namespace) -> None:
    logger = _setup_logging(args.debug)

    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)

    data_dir = Path(config.storage.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "pending").mkdir(exist_ok=True)
    (data_dir / "failed").mkdir(exist_ok=True)

    ingest_state_path = data_dir / "ingest.json"
    sender_state_path = data_dir / "sender.json"
    legacy_state_path = data_dir / "state.json"

    migrate_legacy_state(legacy_state_path, ingest_state_path, sender_state_path)

    ingest_state = load_ingest_state(ingest_state_path)
    sender_state = load_sender_state(sender_state_path)

    if ingest_state.next_seq_id <= sender_state.commit_id:
        logger.warning("next_seq_id (%d) <= commit_id (%d), correcting",
                       ingest_state.next_seq_id, sender_state.commit_id)
        ingest_state.next_seq_id = sender_state.commit_id + 1

    stats = Stats()

    sender = Sender(config.langfuse, config.sender, data_dir,
                    sender_state, sender_state_path, ingest_state_path)
    sender.start()

    cleaner = Cleaner(data_dir, ingest_state, ingest_state_path,
                      sender_state_path, config.storage)
    cleaner.start()

    recovery = FailedRecovery(data_dir, ingest_state, ingest_state_path)
    recovery.start()

    updater = Updater(include_prerelease=config.update.include_prerelease)
    updater.start()

    hook_states = probe_hook_states()
    mismatch, mismatch_agents = get_mismatch_info(hook_states)
    if mismatch:
        logger.warning("Hook version mismatch: %s", ", ".join(mismatch_agents))

    app = create_app(config, ingest_state, ingest_state_path,
                     sender_state, sender_state_path, stats, updater=updater,
                     hook_states=hook_states)

    use_menubar = not args.server_only and sys.platform == "darwin"
    if use_menubar:
        try:
            from src.menubar import run_with_menubar
            run_with_menubar(app, config)
        except ImportError:
            logger.warning("rumps not installed, falling back to server-only mode")
            use_menubar = False

    if not use_menubar:
        logger.info("starting langstash server on %s:%d", config.server.host, config.server.port)
        uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="warning")


def cli() -> None:
    parser = argparse.ArgumentParser(prog="langstash", description="Local buffer for Agent Exporter to Langfuse")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Start the langstash server")
    run_parser.add_argument("--server-only", action="store_true", help="Run without macOS menubar")
    run_parser.add_argument("--config", type=str, default=None, help="Path to config.toml")
    run_parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    subparsers.add_parser("start", help="Start langstash service")
    subparsers.add_parser("stop", help="Stop langstash service")
    subparsers.add_parser("restart", help="Restart langstash service")
    subparsers.add_parser("status", help="Show langstash status")

    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade to a new version")
    upgrade_parser.add_argument("--version", type=str, default=None, help="Target version")
    upgrade_parser.add_argument("--retry-hooks", action="store_true", help="Retry failed hook upgrades")

    subparsers.add_parser("rollback", help="Rollback to the previous version")

    uninstall_parser = subparsers.add_parser("uninstall", help="Uninstall agent-exporter-to-langfuse")
    uninstall_parser.add_argument("--purge", action="store_true", help="Also remove config, data, and logs")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "run": cmd_run,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "upgrade": cmd_upgrade,
        "rollback": cmd_rollback,
        "uninstall": cmd_uninstall,
    }

    handler = commands[args.command]
    result = handler(args)
    if isinstance(result, int) and result != 0:
        sys.exit(result)


if __name__ == "__main__":
    cli()
