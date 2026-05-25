from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

from .models import MediaItem, MediaPair
from .processor import PairProcessor
from .scanner import ScanStats, scan_media
from .settings import Settings

logger = logging.getLogger(__name__)

WorkKind = Literal["pair", "media"]
WorkItem = Union[MediaPair, MediaItem]


@dataclass(frozen=True)
class CandidateSnapshot:
    image_size: int | None = None
    video_size: int | None = None
    image_mtime: int | None = None
    video_mtime: int | None = None

    @property
    def quick_state(self) -> str:
        return f"image={self.image_size}:{self.image_mtime}|video={self.video_size}:{self.video_mtime}"


@dataclass
class PendingWork:
    kind: WorkKind
    item: WorkItem
    quick_state: str
    first_seen_at: float
    last_seen_at: float
    image_size: int | None
    video_size: int | None
    image_mtime: int | None
    video_mtime: int | None
    candidate_type: str
    reason: str = "waiting_for_stable"
    warned_waiting_too_long: bool = False

    def is_stable_with(self, snapshot: CandidateSnapshot) -> bool:
        return self.quick_state == snapshot.quick_state


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

    def candidate_debug_rows(self, now: float | None = None) -> list[dict[str, object]]:
        now = time.time() if now is None else now
        with self.scan_lock:
            pending = list(self.pending.items())
        rows: list[dict[str, object]] = []
        for key, candidate in pending:
            stable = True
            try:
                stable = candidate.is_stable_with(self._snapshot(candidate.kind, candidate.item))
            except FileNotFoundError:
                stable = False
            rows.append({
                "key": key,
                "candidate_type": candidate.candidate_type,
                "path": self._candidate_label(candidate.item),
                "waited_seconds": max(0.0, now - candidate.first_seen_at),
                "first_seen_at": candidate.first_seen_at,
                "last_seen_at": candidate.last_seen_at,
                "next_process_at": candidate.first_seen_at + self.settings.stable_seconds,
                "is_stable": stable,
                "reason": candidate.reason,
                "image_size": candidate.image_size,
                "video_size": candidate.video_size,
                "image_mtime": candidate.image_mtime,
                "video_mtime": candidate.video_mtime,
            })
        rows.sort(key=lambda row: str(row["path"]))
        return rows

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
        processed_live = 0
        copied_photos = 0
        copied_videos = 0
        skipped = 0
        failed = 0
        waiting_for_stable = 0
        detected_live = len(scan_result.pairs)
        detected_photos = sum(1 for item in scan_result.media_items if item.media_type == "photo")
        detected_videos = sum(1 for item in scan_result.media_items if item.media_type == "video")
        live_info_count = 0
        suppressed_live_info = 0

        for pair in scan_result.pairs:
            key = self._pending_key("pair", pair)
            seen_keys.add(key)
            try:
                snapshot = self._snapshot("pair", pair)
            except FileNotFoundError:
                logger.info("Candidate not processed: reason=missing_pair key=%s", key)
                continue
            if self.completed_states.get(key) == snapshot.quick_state:
                skipped += 1
                logger.info("Candidate not processed: reason=already_processed key=%s", key)
                continue

            is_new_or_changed = self._is_new_or_changed(key, snapshot)
            log_live_info = is_new_or_changed and live_info_count < 10
            if is_new_or_changed and log_live_info:
                live_info_count += 1
            elif is_new_or_changed:
                suppressed_live_info += 1

            stable_result = self._stable_decision(key, "pair", pair, snapshot, now, force=force, log_live_info=log_live_info)
            if not stable_result:
                waiting_for_stable += 1
                continue

            self.pending.pop(key, None)
            result = self.processor.process(pair)
            if result == "merged":
                self.completed_states[key] = snapshot.quick_state
                self.scan_stats.merged_live_photos += 1
                processed_live += 1
            elif result == "skipped":
                self.completed_states[key] = snapshot.quick_state
                self.scan_stats.skipped += 1
                skipped += 1
                logger.info("Candidate not processed: reason=already_processed key=%s", key)
            elif result == "failed":
                self.scan_stats.failed += 1
                failed += 1
                logger.info("Candidate not processed: reason=conversion_failed key=%s", key)
            processed_count += 1

        for item in scan_result.media_items:
            key = self._pending_key("media", item)
            seen_keys.add(key)
            try:
                snapshot = self._snapshot("media", item)
            except FileNotFoundError:
                logger.info("Candidate not processed: reason=missing_pair key=%s", key)
                continue
            if self.completed_states.get(key) == snapshot.quick_state:
                skipped += 1
                logger.info("Candidate not processed: reason=already_processed key=%s", key)
                continue

            stable_result = self._stable_decision(key, "media", item, snapshot, now, force=force)
            if not stable_result:
                waiting_for_stable += 1
                continue

            self.pending.pop(key, None)
            result = self.processor.process_media(item)
            if result == "copied_photo":
                self.completed_states[key] = snapshot.quick_state
                self.scan_stats.copied_photos += 1
                copied_photos += 1
            elif result == "copied_video":
                self.completed_states[key] = snapshot.quick_state
                self.scan_stats.copied_videos += 1
                copied_videos += 1
            elif result == "skipped":
                self.completed_states[key] = snapshot.quick_state
                self.scan_stats.skipped += 1
                skipped += 1
                logger.info("Candidate not processed: reason=already_processed key=%s", key)
            elif result == "failed":
                self.scan_stats.failed += 1
                failed += 1
                logger.info("Candidate not processed: reason=conversion_failed key=%s", key)
            processed_count += 1

        stale_keys = set(self.pending) - seen_keys
        for key in stale_keys:
            candidate = self.pending[key]
            logger.info("Candidate not processed: reason=missing_pair key=%s", key)
            logger.debug("Dropping stale pending work: %s", candidate)
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
            "waiting_for_stable=%s processed_live=%s copied_photos=%s copied_videos=%s\n"
            "waiting=%s skipped=%s failed=%s",
            detected_live,
            detected_photos,
            detected_videos,
            waiting_for_stable,
            processed_live,
            copied_photos,
            copied_videos,
            waiting,
            skipped,
            failed,
        )
        return processed_count

    def _stable_decision(
        self,
        key: str,
        kind: WorkKind,
        item: WorkItem,
        snapshot: CandidateSnapshot,
        now: float,
        *,
        force: bool,
        log_live_info: bool = False,
    ) -> bool:
        pending = self.pending.get(key)
        label = self._candidate_label(item)
        if pending is None:
            self.pending[key] = self._new_pending(kind, item, snapshot, now, reason="force" if force else "waiting_for_stable")
            self._log_candidate_detected(kind, label, log_live_info=log_live_info)
            if force:
                logger.info("Candidate entering processing: reason=force key=%s", key)
            else:
                logger.info("Candidate not processed: reason=waiting_for_stable key=%s", key)
            return force

        pending.last_seen_at = now
        if not pending.is_stable_with(snapshot):
            self.pending[key] = self._new_pending(kind, item, snapshot, now, reason="file_changed_reset_timer")
            logger.info("Candidate not processed: reason=file_changed_reset_timer key=%s", key)
            return force

        if force:
            pending.reason = "force"
            logger.info("Candidate entering processing: reason=force key=%s", key)
            return True

        elapsed = now - pending.first_seen_at
        if elapsed >= 300 and not pending.warned_waiting_too_long:
            pending.warned_waiting_too_long = True
            logger.warning("Candidate has been waiting too long: %s elapsed=%.1fs", label, elapsed)
        if elapsed < self.settings.stable_seconds:
            pending.reason = "waiting_for_stable"
            logger.info("Candidate not processed: reason=waiting_for_stable key=%s elapsed=%.1fs required=%.1fs", key, elapsed, self.settings.stable_seconds)
            return False

        pending.reason = "ready"
        logger.info("Candidate entering processing: key=%s elapsed=%.1fs", key, elapsed)
        return True

    def _new_pending(self, kind: WorkKind, item: WorkItem, snapshot: CandidateSnapshot, now: float, *, reason: str) -> PendingWork:
        return PendingWork(
            kind=kind,
            item=item,
            quick_state=snapshot.quick_state,
            first_seen_at=now,
            last_seen_at=now,
            image_size=snapshot.image_size,
            video_size=snapshot.video_size,
            image_mtime=snapshot.image_mtime,
            video_mtime=snapshot.video_mtime,
            candidate_type="live_photo" if kind == "pair" else getattr(item, "media_type", "media"),
            reason=reason,
        )

    def _snapshot(self, kind: WorkKind, item: WorkItem) -> CandidateSnapshot:
        if kind == "pair":
            pair = item
            assert isinstance(pair, MediaPair)
            image_stat = pair.image_path.stat()
            video_stat = pair.video_path.stat()
            return CandidateSnapshot(
                image_size=image_stat.st_size,
                video_size=video_stat.st_size,
                image_mtime=image_stat.st_mtime_ns,
                video_mtime=video_stat.st_mtime_ns,
            )
        media_item = item
        assert isinstance(media_item, MediaItem)
        stat = media_item.path.stat()
        if media_item.media_type == "photo":
            return CandidateSnapshot(image_size=stat.st_size, image_mtime=stat.st_mtime_ns)
        return CandidateSnapshot(video_size=stat.st_size, video_mtime=stat.st_mtime_ns)

    def _is_new_or_changed(self, key: str, snapshot: CandidateSnapshot) -> bool:
        pending = self.pending.get(key)
        return pending is None or not pending.is_stable_with(snapshot)

    def _log_candidate_detected(self, kind: WorkKind, label: str, *, log_live_info: bool) -> None:
        if kind == "pair" and log_live_info:
            logger.info("Detected Live Photo candidate %s; waiting %.1fs for transfer to settle", label, self.settings.stable_seconds)
            return
        logger.debug("Detected candidate %s %s; waiting %.1fs for transfer to settle", kind, label, self.settings.stable_seconds)

    def _candidate_label(self, item: WorkItem) -> str:
        if isinstance(item, MediaPair):
            return f"image={item.image_path} video={item.video_path}"
        return str(item.path)

    def _pending_key(self, kind: WorkKind, item: WorkItem) -> str:
        if isinstance(item, MediaPair):
            return f"{kind}:{item.image_path}|{item.video_path}"
        return f"{kind}:{item.path}"
