"""
Three-tier delivery: langstash → direct Langfuse SDK push → failed log.

Usage in hook main():
    from hooks.lib.deliver import deliver_trace
    deliver_trace(trace_json, direct_push_fn=lambda tj: emit_turn_direct(...))
"""

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

INSTALL_DIR = Path.home() / ".agent-exporter-to-langfuse"
FAILED_DIR = INSTALL_DIR / "data" / "failed"


def _langstash_enabled() -> bool:
    return os.environ.get("LANGSTASH_ENABLED", "").lower() == "true"


def _langstash_url() -> str:
    return os.environ.get("LANGSTASH_URL", "http://127.0.0.1:5288")


def _langstash_timeout() -> int:
    try:
        return int(os.environ.get("LANGSTASH_TIMEOUT", "10"))
    except ValueError:
        return 10


def _post_langstash(trace_json: dict[str, Any]) -> bool:
    url = f"{_langstash_url().rstrip('/')}/ingest"
    body = json.dumps(trace_json, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urlopen(req, timeout=_langstash_timeout())
        return 200 <= resp.status < 300
    except Exception as e:
        logger.debug("langstash POST failed: %s", e)
        return False


def append_failed_trace(trace_json: dict[str, Any]) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = FAILED_DIR / f"{today}.jsonl"
    line = json.dumps(trace_json, ensure_ascii=False) + "\n"

    fd = open(path, "a", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.write(line)
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def deliver_trace(
    trace_json: dict[str, Any],
    direct_push_fn: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> bool:
    if _langstash_enabled():
        if _post_langstash(trace_json):
            return True
        logger.debug("langstash delivery failed, trying direct push")

    if direct_push_fn:
        try:
            if direct_push_fn(trace_json):
                return True
        except Exception as e:
            logger.debug("direct push failed: %s", e)

    try:
        append_failed_trace(trace_json)
        logger.debug("trace saved to failed log")
    except Exception as e:
        logger.debug("failed log write error: %s", e)

    return False
