import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from src.cleaner import Cleaner
from src.config import load_config
from src.ingestor import FailedRecovery
from src.sender import Sender
from src.server import create_app
from src.state import (
    load_ingest_state, load_sender_state, migrate_legacy_state,
)
from src.stats import Stats
from src.updater import Updater


def cli() -> None:
    parser = argparse.ArgumentParser(prog="langstash", description="Local buffer for Agent Exporter to Langfuse")
    parser.add_argument("--server-only", action="store_true", help="Run without macOS menubar (for Linux / headless)")
    parser.add_argument("--config", type=str, default=None, help="Path to config.toml")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
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

    logger = logging.getLogger("langstash")

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

    app = create_app(config, ingest_state, ingest_state_path,
                     sender_state, sender_state_path, stats, updater=updater)

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


if __name__ == "__main__":
    cli()
