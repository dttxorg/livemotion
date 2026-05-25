from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .file_utils import quick_pair_state
from .models import MediaPair
from .processor import PairProcessor
from .scanner import scan_pairs
from .settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class PendingPair:
    pair: MediaPair
    quick_state: str
    stable_since: float


class LivePhotoWorker:
    def __init__(self, settings: Settings, processor: PairProcessor):
        self.settings = settings
        self.processor = processor
        self.pending: dict[str, PendingPair] = {}
        self.stop_event = threading.Event()
        self.scan_lock = threading.Lock()

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        logger.info(
            "Watching %s; output=%s archive=%s failed=%s stable=%ss interval=%ss",
            self.settings.input_dir,
            self.settings.output_dir,
            self.settings.archive_dir,
            self.settings.failed_dir,
            self.settings.stable_seconds,
            self.settings.poll_interval,
        )
        while not self.stop_event.is_set():
            self.scan_once()
            self.stop_event.wait(self.settings.poll_interval)
        logger.info("Worker stopped")

    def clear_pending(self) -> None:
        with self.scan_lock:
            self.pending.clear()

    def scan_once(self, now: float | None = None) -> int:
        with self.scan_lock:
            return self._scan_once_unlocked(now=now)

    def _scan_once_unlocked(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        pairs = scan_pairs(self.settings.input_dir)
        seen_keys: set[str] = set()
        processed_count = 0

        for pair in pairs:
            seen_keys.add(pair.key)
            try:
                current_state = quick_pair_state(pair)
            except FileNotFoundError:
                logger.debug("Pair disappeared while scanning: %s", pair.key)
                continue

            pending = self.pending.get(pair.key)
            if pending is None or pending.quick_state != current_state:
                self.pending[pair.key] = PendingPair(
                    pair=pair,
                    quick_state=current_state,
                    stable_since=now,
                )
                logger.info(
                    "Detected candidate pair %s; waiting %.1fs for transfer to settle",
                    pair.key,
                    self.settings.stable_seconds,
                )
                continue

            age = now - pending.stable_since
            if age < self.settings.stable_seconds:
                logger.debug("Pair %s stable for %.1fs; waiting", pair.key, age)
                continue

            self.pending.pop(pair.key, None)
            self.processor.process(pair)
            processed_count += 1

        stale_keys = set(self.pending) - seen_keys
        for key in stale_keys:
            logger.debug("Dropping stale pending pair: %s", key)
            self.pending.pop(key, None)

        return processed_count
