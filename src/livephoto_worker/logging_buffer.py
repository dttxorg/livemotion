from __future__ import annotations

import logging
import threading
from collections import deque


class RecentLogHandler(logging.Handler):
    def __init__(self, capacity: int = 300):
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)
        self.lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - logging handlers should not crash the app.
            message = record.getMessage()
        with self.lock:
            self.records.append(message)

    def recent(self, limit: int = 100) -> list[str]:
        with self.lock:
            records = list(self.records)
        if limit <= 0:
            return []
        return records[-limit:]

    def clear(self) -> None:
        with self.lock:
            self.records.clear()
