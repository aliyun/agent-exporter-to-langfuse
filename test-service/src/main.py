import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from src.config import BASE_DIR, Config, load_config, read_version


def _setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _service_ctl(action: str) -> int:
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.langstash-tester.plist"
        if not plist.exists():
            print("ERROR: LaunchAgent not found. Run 'langstash-tester install' first.", file=sys.stderr)
            return 1
        if action == "start":
            subprocess.run(["launchctl", "load", str(plist)])
        elif action == "stop":
            subprocess.run(["launchctl", "unload", str(plist)])
        elif action == "restart":
            subprocess.run(["launchctl", "unload", str(plist)])
            subprocess.run(["launchctl", "load", str(plist)])
    else:
        subprocess.run(["systemctl", "--user", action, "langstash-tester"])
    return 0


def cmd_run(args: argparse.Namespace) -> None:
    _setup_logging()
    logger = logging.getLogger("langstash-tester")

    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)

    from src.git_manager import GitManager
    git = GitManager(config.git.repo_url, config.git.local_repo, config.git.worktree_dir)
    git.validate_bare_repo()

    for d in [Path(config.storage.log_dir), Path(config.storage.db_path).parent,
              Path(config.git.worktree_dir)]:
        d.mkdir(parents=True, exist_ok=True)

    from src.store import Store
    store = Store(config.storage.db_path, config.storage.log_dir)

    from src.queue import JobQueue
    queue = JobQueue(same_branch_policy=config.e2e.same_branch_policy)

    from src.webhook import WebhookNotifier
    notifier = WebhookNotifier(config.webhook)

    def on_job_complete(job_id: str) -> None:
        job = store.get_job(job_id)
        if job and job.get("callback_url"):
            notifier.notify(job["callback_url"], {
                "event": "e2e.completed",
                "job_id": job["job_id"],
                "status": job["status"],
                "branch": job["branch"],
                "commit": job.get("commit"),
                "duration_seconds": job.get("duration_seconds"),
                "exit_code": job.get("exit_code"),
                "summary": job.get("summary"),
                "output_tail": job.get("output_tail"),
                "metadata": job.get("metadata"),
            })

    from src.worker import Worker
    worker = Worker(config, store, queue, git, on_complete=on_job_complete)
    worker.start()

    cleanup_timer = _start_cleanup_timer(store, config.e2e.result_retention_days)

    from src.server import create_app
    app = create_app(config, store=store, queue=queue)

    version = read_version()
    logger.info("langstash-tester v%s starting on %s:%d", version, config.server.host, config.server.port)

    stop_event = threading.Event()

    def handle_signal(signum, frame):
        logger.info("received signal %d, shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.server.host, port=config.server.port, log_level="warning",
    ))

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    stop_event.wait()

    logger.info("stopping worker...")
    worker.stop(timeout=30)
    if cleanup_timer:
        cleanup_timer.cancel()
    server.should_exit = True
    server_thread.join(timeout=5)
    logger.info("langstash-tester stopped")


def _start_cleanup_timer(store, retention_days: int, interval: int = 3600) -> threading.Timer | None:
    if retention_days <= 0:
        return None

    def run_cleanup():
        try:
            deleted = store.cleanup_expired(retention_days)
            if deleted:
                logging.getLogger("langstash-tester").info("cleaned up %d expired jobs", deleted)
        except Exception as e:
            logging.getLogger("langstash-tester").warning("cleanup error: %s", e)
        timer = threading.Timer(interval, run_cleanup)
        timer.daemon = True
        timer.start()

    store.cleanup_expired(retention_days)
    timer = threading.Timer(interval, run_cleanup)
    timer.daemon = True
    timer.start()
    return timer


def cmd_start(args: argparse.Namespace) -> int:
    return _service_ctl("start")


def cmd_stop(args: argparse.Namespace) -> int:
    return _service_ctl("stop")


def cmd_restart(args: argparse.Namespace) -> int:
    return _service_ctl("restart")


def cmd_status(args: argparse.Namespace) -> int:
    version = read_version()
    print(f"langstash-tester v{version}")
    try:
        with urllib.request.urlopen("http://127.0.0.1:5289/health", timeout=3) as resp:
            data = json.loads(resp.read())
        print(f"Status: {data.get('status', 'unknown')}")
    except Exception:
        print("Status: not running")
    return 0


def _find_uv_python() -> str:
    """Find the Python interpreter managed by uv for this project."""
    import shutil
    test_service_dir = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["uv", "python", "find"], cwd=test_service_dir,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    uv_python = shutil.which("python3") or "python3"
    return uv_python


def cmd_install(args: argparse.Namespace) -> int:
    for d in ["config", "repo", "worktrees", "data", "logs"]:
        (BASE_DIR / d).mkdir(parents=True, exist_ok=True)
    print(f"Directories created under {BASE_DIR}")

    test_service_dir = Path(__file__).resolve().parent.parent
    uv_bin = subprocess.run(
        ["which", "uv"], capture_output=True, text=True,
    ).stdout.strip() or "uv"
    working_dir = str(test_service_dir)

    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "langstash-tester"
    wrapper.write_text(
        f'#!/bin/sh\ncd "{working_dir}" && exec "{uv_bin}" run langstash-tester "$@"\n'
    )
    wrapper.chmod(0o755)
    print(f"CLI wrapper created at {wrapper}")

    if sys.platform == "darwin":
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.langstash-tester.plist"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.langstash-tester</string>
    <key>ProgramArguments</key>
    <array>
        <string>{uv_bin}</string>
        <string>run</string>
        <string>langstash-tester</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{BASE_DIR / 'logs' / 'service.log'}</string>
    <key>StandardErrorPath</key>
    <string>{BASE_DIR / 'logs' / 'service.log'}</string>
</dict>
</plist>
"""
        plist_path.write_text(plist_content)
        print(f"LaunchAgent created at {plist_path}")
    else:
        systemd_dir = Path.home() / ".config" / "systemd" / "user"
        systemd_dir.mkdir(parents=True, exist_ok=True)
        service_path = systemd_dir / "langstash-tester.service"
        service_content = f"""[Unit]
Description=langstash-tester E2E testing service
After=network.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={uv_bin} run langstash-tester run
Restart=on-failure
RestartSec=5
StandardOutput=append:{BASE_DIR / 'logs' / 'service.log'}
StandardError=append:{BASE_DIR / 'logs' / 'service.log'}

[Install]
WantedBy=default.target
"""
        service_path.write_text(service_content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "langstash-tester"], capture_output=True)
        print(f"systemd service created at {service_path}")

    print("langstash-tester installed successfully")
    print(f"Edit config: {BASE_DIR / 'config' / 'config.toml'}")
    print(f"Start with: langstash-tester start")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    import shutil

    _service_ctl("stop")

    wrapper = Path.home() / ".local" / "bin" / "langstash-tester"
    if wrapper.exists():
        wrapper.unlink()
        print(f"Removed {wrapper}")

    if args.purge:
        if BASE_DIR.exists():
            shutil.rmtree(BASE_DIR)
            print(f"Purged {BASE_DIR}")
    else:
        for d in ["repo", "worktrees"]:
            p = BASE_DIR / d
            if p.exists():
                shutil.rmtree(p)
                print(f"Removed {p}")
        print(f"Preserved: {BASE_DIR}/config, {BASE_DIR}/data, {BASE_DIR}/logs")

    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.langstash-tester.plist"
        if plist.exists():
            plist.unlink()
            print(f"Removed {plist}")
    else:
        service_file = Path.home() / ".config" / "systemd" / "user" / "langstash-tester.service"
        if service_file.exists():
            service_file.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            print(f"Removed {service_file}")

    print("langstash-tester uninstalled")
    return 0


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="langstash-tester",
        description="E2E testing service for agent-exporter-to-langfuse",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Start the service in foreground")
    run_parser.add_argument("--config", type=str, default=None,
                            help=f"Path to config.toml (default: {BASE_DIR / 'config' / 'config.toml'})")

    subparsers.add_parser("start", help="Start the background service")
    subparsers.add_parser("stop", help="Stop the background service")
    subparsers.add_parser("restart", help="Restart the background service")
    subparsers.add_parser("status", help="Show service status and version")
    subparsers.add_parser("install", help="Install the service (create directories, register service)")

    uninstall_parser = subparsers.add_parser("uninstall", help="Uninstall the service")
    uninstall_parser.add_argument("--purge", action="store_true",
                                  help="Also remove config, data, and logs (~/.langstash-tester/)")

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
        "install": cmd_install,
        "uninstall": cmd_uninstall,
    }

    handler = commands[args.command]
    result = handler(args)
    if isinstance(result, int) and result != 0:
        sys.exit(result)


if __name__ == "__main__":
    cli()
