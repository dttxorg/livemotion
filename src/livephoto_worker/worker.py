from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Literal, Union

from .file_utils import quick_media_item_state, quick_pair_state
from .models import MediaItem, MediaPair
from .processor import PairProcessor
from .scanner import ScanStats, scan_media
from .settings import Settings

logger = logging.getLogger(__name__)

WorkKind = Literal["pair", "media"]
WorkItem = Union[MediaPair, MediaItem]


@dataclass
class PendingWork:
    kind: WorkKind
    item: WorkItem
    quick_state: str
    stable_since: float


@dataclass
class WorkerScanStats:
    scanned_dirs: int = 0
    scanned_files: int = 0
    skipped_dirs: int = 0
    merged_live_photos: int = 0
    copied_photos: int = 0
    copied_videos: int = 0

    def apply_scan_stats(self, stats: ScanStats) -> None:
        self.scanned_dirs = stats.scanned_dirs
        self.scanned_files = stats.scanned_files
        self.skipped_dirs = stats.skipped_dirs

    def to_dict(self) -> dict[str, int]:
        return {
            "scanned_dirs": self.scanned_dirs,
            "scanned_files": self.scanned_files,
            "skipped_dirs": self.skipped_dirs,
            "merged_live_photos": self.merged_live_photos,
            "copied_photos": self.copied_photos,
            "copied_videos": self.copied_videos,
        }


class LivePhotoWorker:
    def __init__(self, settings: Settings, processor: PairProcessor):
        self.settings = settings
        self.processor = processor
        self.pending: dict[str, PendingWork] = {}
        self.completed_states: dict[str, str] = {}
        self.stop_event = threading.Event()
        self.scan_lock = threading.Lock()
        self.scan_stats = WorkerScanStats()

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        logger.info(
            "Watching %s; output=%s archive=%s failed=%s stable=%ss interval=%ss recursive=%s preserve_structure=%s",
            self.settings.input_dir,
            self.settings.output_dir,
            self.settings.archive_dir,
            self.settings.failed_dir,
            self.settings.stable_seconds,
            self.settings.poll_interval,
            self.settings.recursive_scan,
            self.settings.preserve_directory_structure,
        )
        while not self.stop_event.is_set():
            self.scan_once()
            self.stop_event.wait(self.settings.poll_interval)
        logger.info("Worker stopped")

    def clear_pending(self) -> None:
        with self.scan_lock:
            self.pending.clear()
            self.completed_states.clear()

    def scan_once(self, now: float | None = None) -> int:
        with self.scan_lock:
            return self._scan_once_unlocked(now=now)

    def _scan_once_unlocked(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        scan_result = scan_media(
            self.settings.input_dir,
            recursive=self.settings.recursive_scan,
            output_dir=self.settings.output_dir,
            archive_dir=self.settings.archive_dir,
            failed_dir=self.settings.failed_dir,
            skip_dir_names=self.settings.skip_dir_names,
        )
        self.scan_stats.apply_scan_stats(scan_result.stats)
        seen_keys: set[str] = set()
        processed_count = 0

        for pair in scan_result.pairs:
            key = self._pending_key("pair", pair)
            seen_keys.add(key)
            try:
                current_state = quick_pair_state(pair)
            except FileNotFoundError:
                logger.debug("Pair disappeared while scanning: %s", pair.key)
                continue
            if self.completed_states.get(key) == current_state:
                continue

            if not self._is_stable(key, "pair", pair, current_state, now):
                continue

            self.pending.pop(key, None)
            merged = self.processor.process(pair)
            self.completed_states[key] = current_state
            if merged:
                self.scan_stats.merged_live_photos += 1
            processed_count += 1

        for item in scan_result.media_items:
            key = self._pending_key("media", item)
            seen_keys.add(key)
            try:
                current_state = quick_media_item_state(item)
            except FileNotFoundError:
                logger.debug("Media item disappeared while scanning: %s", item.key)
                continue
            if self.completed_states.get(key) == current_state:
                continue

            if not self._is_stable(key, "media", item, current_state, now):
                continue

            self.pending.pop(key, None)
            copied = self.processor.process_media(item)
            self.completed_states[key] = current_state
            if copied:
                if item.media_type == "photo":
                    self.scan_stats.copied_photos += 1
                elif item.media_type == "video":
                    self.scan_stats.copied_videos += 1
            processed_count += 1

        stale_keys = set(self.pending) - seen_keys
        for key in stale_keys:
            logger.debug("Dropping stale pending work: %s", key)
            self.pending.pop(key, None)

        return processed_count

    def _is_stable(self, key: str, kind: WorkKind, item: WorkItem, current_state: str, now: float) -> bool:
        pending = self.pending.get(key)
        label = item.key
        if pending is None or pending.quick_state != current_state:
            self.pending[key] = PendingWork(
                kind=kind,
                item=item,
                quick_state=current_state,
                stable_since=now,
            )
            logger.info(
                "Detected candidate %s %s; waiting %.1fs for transfer to settle",
                kind,
                label,
                self.settings.stable_seconds,
            )
            return False

        age = now - pending.stable_since
        if age < self.settings.stable_seconds:
            logger.debug("%s %s stable for %.1fs; waiting", kind, label, age)
            return False
        return True

    def _pending_key(self, kind: WorkKind, item: WorkItem) -> str:
        return f"{kind}:{item.key}"
