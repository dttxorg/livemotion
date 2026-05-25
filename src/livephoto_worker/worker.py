from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
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
    first_seen_at: float
    warned_waiting_too_long: bool = False


@dataclass
class WorkerScanStats:
    scanned_dirs: int = 0
    scanned_files: int = 0
    skipped_dirs: int = 0
    merged_live_photos: int = 0
    copied_photos: int = 0
    copied_videos: int = 0
    skipped: int = 0
    failed: int = 0

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
            "skipped": self.skipped,
            "failed": self.failed,
        }


class LivePhotoWorker:
    def __init__(self, settings: Settings, processor: PairProcessor):
        self.settings = settings
        self.processor = processor
        self.pending: dict[str, PendingWork] = {}
        self.completed_states: dict[str, str] = {}
        self.stop_event = threading.Event()
        self.scan_lock = threading.RLock()
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

    def force_scan_once(self, now: float | None = None) -> int:
        with self.scan_lock:
            return self._scan_once_unlocked(now=now, force=True)

    def pending_status(self, now: float | None = None) -> dict[str, float | int | None]:
        now = time.time() if now is None else now
        with self.scan_lock:
            pending = list(self.pending.values())
        if not pending:
            return {
                "waiting_count": 0,
                "waiting_live_pairs": 0,
                "earliest_first_seen_at": None,
                "next_process_at": None,
                "oldest_wait_seconds": 0,
            }
        earliest = min(item.first_seen_at for item in pending)
        next_process_at = min(item.first_seen_at + self.settings.stable_seconds for item in pending)
        return {
            "waiting_count": len(pending),
            "waiting_live_pairs": sum(1 for item in pending if item.kind == "pair"),
            "earliest_first_seen_at": earliest,
            "next_process_at": next_process_at,
            "oldest_wait_seconds": max(0, now - earliest),
        }

    def _scan_once_unlocked(self, now: float | None = None, *, force: bool = False) -> int:
        now = time.time() if now is None else now
        logger.info("Scanning input folder as full media library: %s", self.settings.input_dir)
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
        merged_live = 0
        copied_photos = 0
        copied_videos = 0
        skipped = 0
        failed = 0
        detected_live = len(scan_result.pairs)
        detected_photos = sum(1 for item in scan_result.media_items if item.media_type == "photo")
        detected_videos = sum(1 for item in scan_result.media_items if item.media_type == "video")
        live_info_count = 0
        suppressed_live_info = 0

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

            is_new_or_changed = self._is_new_or_changed(key, current_state)
            log_live_info = is_new_or_changed and live_info_count < 10
            if is_new_or_changed and log_live_info:
                live_info_count += 1
            elif is_new_or_changed:
                suppressed_live_info += 1

            if not self._is_stable(key, "pair", pair, current_state, now, force=force, log_live_info=log_live_info):
                continue

            self.pending.pop(key, None)
            result = self.processor.process(pair)
            self.completed_states[key] = current_state
            if result == "merged":
                self.scan_stats.merged_live_photos += 1
                merged_live += 1
            elif result == "skipped":
                self.scan_stats.skipped += 1
                skipped += 1
            elif result == "failed":
                self.scan_stats.failed += 1
                failed += 1
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

            if not self._is_stable(key, "media", item, current_state, now, force=force):
                continue

            self.pending.pop(key, None)
            result = self.processor.process_media(item)
            self.completed_states[key] = current_state
            if result == "copied_photo":
                self.scan_stats.copied_photos += 1
                copied_photos += 1
            elif result == "copied_video":
                self.scan_stats.copied_videos += 1
                copied_videos += 1
            elif result == "skipped":
                self.scan_stats.skipped += 1
                skipped += 1
            elif result == "failed":
                self.scan_stats.failed += 1
                failed += 1
            processed_count += 1

        stale_keys = set(self.pending) - seen_keys
        for key in stale_keys:
            logger.debug("Dropping stale pending work: %s", key)
            self.pending.pop(key, None)

        if suppressed_live_info:
            logger.info("Detected %s additional Live Photo candidates; showing only first 10", suppressed_live_info)

        pending_status = self.pending_status(now=now)
        waiting = int(pending_status["waiting_count"] or 0)
        logger.info(
            "Detected candidates: detected_live_pairs=%s detected_normal_photos=%s detected_normal_videos=%s waiting_for_stable=%s",
            detected_live,
            detected_photos,
            detected_videos,
            waiting,
        )
        logger.info(
            "Scan finished:\n"
            "detected_live=%s detected_photos=%s detected_videos=%s\n"
            "processed_live=%s copied_photos=%s copied_videos=%s\n"
            "waiting=%s skipped=%s failed=%s",
            detected_live,
            detected_photos,
            detected_videos,
            merged_live,
            copied_photos,
            copied_videos,
            waiting,
            skipped,
            failed,
        )
        return processed_count

    def _is_stable(
        self,
        key: str,
        kind: WorkKind,
        item: WorkItem,
        current_state: str,
        now: float,
        *,
        force: bool,
        log_live_info: bool = False,
    ) -> bool:
        pending = self.pending.get(key)
        label = item.key
        if pending is None:
            self.pending[key] = PendingWork(
                kind=kind,
                item=item,
                quick_state=current_state,
                first_seen_at=now,
            )
            self._log_candidate_detected(kind, label, log_live_info=log_live_info)
            return force

        if pending.quick_state != current_state:
            self.pending[key] = PendingWork(
                kind=kind,
                item=item,
                quick_state=current_state,
                first_seen_at=now,
            )
            logger.debug("Candidate changed; reset first_seen_at for %s %s", kind, label)
            return force

        if force:
            logger.debug("Force scan is processing candidate without waiting: %s %s", kind, label)
            return True

        age = now - pending.first_seen_at
        if age >= 300 and not pending.warned_waiting_too_long:
            pending.warned_waiting_too_long = True
            logger.warning("Candidate has been waiting too long: %s elapsed=%.1fs", label, age)
        if age < self.settings.stable_seconds:
            logger.debug("%s %s stable for %.1fs; waiting", kind, label, age)
            return False
        return True

    def _is_new_or_changed(self, key: str, current_state: str) -> bool:
        pending = self.pending.get(key)
        return pending is None or pending.quick_state != current_state

    def _log_candidate_detected(self, kind: WorkKind, label: str, *, log_live_info: bool) -> None:
        if kind == "pair" and log_live_info:
            logger.info("Detected Live Photo candidate %s; waiting %.1fs for transfer to settle", label, self.settings.stable_seconds)
            return
        logger.debug("Detected candidate %s %s; waiting %.1fs for transfer to settle", kind, label, self.settings.stable_seconds)

    def _pending_key(self, kind: WorkKind, item: WorkItem) -> str:
        return f"{kind}:{item.key}"
