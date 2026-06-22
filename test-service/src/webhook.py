import logging
import threading
import time
from typing import Any

import httpx

from src.config import WebhookConfig

logger = logging.getLogger("langstash-tester.webhook")


class WebhookNotifier:
    def __init__(self, config: WebhookConfig):
        self._config = config

    def notify(self, callback_url: str, payload: dict[str, Any]) -> None:
        if not callback_url:
            return
        thread = threading.Thread(
            target=self._send_with_retry,
            args=(callback_url, payload),
            daemon=True,
            name="webhook-notify",
        )
        thread.start()

    def _send_with_retry(self, url: str, payload: dict[str, Any]) -> None:
        delays = self._config.retry_delays[:self._config.retry_count]
        attempts = [0] + delays

        for i, delay in enumerate(attempts):
            if i > 0:
                time.sleep(delay)
            try:
                resp = httpx.post(
                    url, json=payload,
                    timeout=self._config.timeout_seconds,
                )
                if 200 <= resp.status_code < 300:
                    logger.info("webhook sent to %s (attempt %d)", url, i + 1)
                    return
                logger.warning("webhook %s returned %d (attempt %d)", url, resp.status_code, i + 1)
            except Exception as e:
                logger.warning("webhook %s failed (attempt %d): %s", url, i + 1, e)

        logger.error("webhook %s failed after %d attempts", url, len(attempts))
