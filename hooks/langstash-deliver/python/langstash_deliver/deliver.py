"""
Three-tier OTLP JSON delivery: langstash buffer → Langfuse OTel endpoint → failed log.

Usage in hook main():
    from langstash_deliver.deliver import deliver_trace
    deliver_trace(otlp_json)
"""

import base64
import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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


def _post_langstash(otlp_json: dict[str, Any]) -> bool:
    url = f"{_langstash_url().rstrip('/')}/ingest"
    body = json.dumps(otlp_json, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urlopen(req, timeout=_langstash_timeout())
        return 200 <= resp.status < 300
    except Exception as e:
        logger.debug("langstash POST failed: %s", e)
        return False


def _post_langfuse_otel(otlp_json: dict[str, Any]) -> bool:
    base_url = os.environ.get("LANGFUSE_BASE_URL", "").rstrip("/")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")

    if not base_url or not public_key or not secret_key:
        logger.debug("Langfuse credentials not configured, skipping Tier 2")
        return False

    url = f"{base_url}/api/public/otel/v1/traces"
    body = json.dumps(otlp_json, ensure_ascii=False).encode("utf-8")

    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {credentials}",
    }

    req = Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urlopen(req, timeout=30)
        return 200 <= resp.status < 300
    except Exception as e:
        logger.debug("Langfuse OTel POST failed: %s", e)
        return False


def append_failed_trace(otlp_json: dict[str, Any]) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = FAILED_DIR / f"{today}.jsonl"
    line = json.dumps(otlp_json, ensure_ascii=False) + "\n"

    fd = open(path, "a", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.write(line)
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def deliver_trace(otlp_json: dict[str, Any]) -> bool:
    if _langstash_enabled():
        if _post_langstash(otlp_json):
            return True
        logger.debug("langstash delivery failed, trying Langfuse OTel direct push")

    if _post_langfuse_otel(otlp_json):
        return True

    try:
        append_failed_trace(otlp_json)
        logger.debug("trace saved to failed log")
    except Exception as e:
        logger.debug("failed log write error: %s", e)

    return False
