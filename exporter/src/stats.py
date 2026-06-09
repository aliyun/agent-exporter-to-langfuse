import time
from dataclasses import dataclass, field


@dataclass
class Stats:
    _start_time: float = field(default_factory=time.time)

    @property
    def uptime_seconds(self) -> int:
        return int(time.time() - self._start_time)
