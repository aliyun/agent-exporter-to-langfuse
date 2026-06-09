import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Stats:
    traces_today: int = 0
    tokens_today_input: int = 0
    tokens_today_output: int = 0
    tokens_today_cache_read: int = 0
    tokens_today_cache_creation: int = 0
    sent_today: int = 0
    failed_count: int = 0
    _today_key: str = ""
    _start_time: float = field(default_factory=time.time)

    def _check_day_rollover(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._today_key:
            self._today_key = today
            self.traces_today = 0
            self.tokens_today_input = 0
            self.tokens_today_output = 0
            self.tokens_today_cache_read = 0
            self.tokens_today_cache_creation = 0
            self.sent_today = 0

    def record_ingest(self, trace_json: dict[str, Any]) -> None:
        self._check_day_rollover()
        self.traces_today += 1
        for gen in trace_json.get("generations", []):
            usage = gen.get("usage")
            if isinstance(usage, dict):
                self.tokens_today_input += int(usage.get("input", 0))
                self.tokens_today_output += int(usage.get("output", 0))
                self.tokens_today_cache_read += int(usage.get("cache_read_input_tokens", 0))
                self.tokens_today_cache_creation += int(usage.get("cache_creation_input_tokens", 0))

    def record_sent(self, count: int) -> None:
        self._check_day_rollover()
        self.sent_today += count

    def record_failed(self) -> None:
        self.failed_count += 1

    def to_dict(self, pending_count: int, storage_used_mb: float,
                last_error: Any, last_commit_at: str,
                update_info: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_day_rollover()
        result: dict[str, Any] = {
            "traces_today": self.traces_today,
            "tokens_today": {
                "input": self.tokens_today_input,
                "output": self.tokens_today_output,
                "cache_read": self.tokens_today_cache_read,
                "cache_creation": self.tokens_today_cache_creation,
                "total": self.tokens_today_input + self.tokens_today_output,
            },
            "pending_count": pending_count,
            "failed_count": self.failed_count,
            "sent_today": self.sent_today,
            "last_success_at": last_commit_at or None,
            "last_error": None,
            "storage_used_mb": round(storage_used_mb, 2),
            "uptime_seconds": int(time.time() - self._start_time),
            "update_available": False,
            "current_version": "",
            "latest_version": "",
        }
        if last_error:
            result["last_error"] = {
                "time": last_error.time,
                "seq_id": last_error.seq_id,
                "error": last_error.error,
                "retries": last_error.retries,
            }
        if update_info:
            result.update(update_info)
        return result
