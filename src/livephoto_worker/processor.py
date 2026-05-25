from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from .db import ProcessingStore
from .file_utils import hash_pair, move_one_file, quick_pair_state, unique_path
from .models import MediaPair, PairHashes
from .settings import Settings

logger = logging.getLogger(__name__)


class PairProcessor:
    def __init__(self, settings: Settings, store: ProcessingStore):
        self.settings = settings
        self.store = store

    def process(self, pair: MediaPair) -> None:
        logger.info("Preparing pair: image=%s video=%s", pair.image_path.name, pair.video_path.name)
        pair_signature: str | None = None
        hashes: PairHashes | None = None
        output_path: Path | None = None

        try:
            quick_state = quick_pair_state(pair)
            logger.debug("Stable pair state: %s", quick_state)

            hashes = hash_pair(pair)
            pair_signature = hashes.signature
            marker = hashes.image_sha256[:12]

            if self.store.is_processed(pair_signature):
                logger.info("Pair already processed; skipping duplicate for %s", pair.stem)
                if self.settings.move_originals and self.settings.enable_archive:
                    self._archive_pair(pair, marker=marker)
                    self.store.record_duplicate(pair, hashes)
                return

            output_path = unique_path(self.settings.output_dir, pair.image_path.name, marker=marker)
            started_at = time.time()
            self._run_motionphoto2(pair, output_path)

            actual_output = self._resolve_output_path(output_path, started_at)
            if actual_output is None or not actual_output.is_file():
                raise RuntimeError(f"MotionPhoto2 finished but output file was not created: {output_path}")

            self._handle_successful_originals(pair, marker=marker)
            self.store.record_success(pair, hashes, actual_output)
            logger.info("Created Motion Photo: %s", actual_output)
        except Exception as exc:  # noqa: BLE001 - quarantine bad inputs and keep the daemon alive.
            error = str(exc)
            logger.exception("Failed to process pair %s: %s", pair.stem, error)
            marker = None
            if hashes is not None:
                marker = hashes.image_sha256[:12]
            self._move_failed_artifacts(pair, output_path, marker=marker)
            self.store.record_failure(pair, pair_signature, error, output_path=output_path)

    def _run_motionphoto2(self, pair: MediaPair, output_path: Path) -> None:
        command = [
            self.settings.motionphoto2_bin,
            "--input-image",
            str(pair.image_path),
            "--input-video",
            str(pair.video_path),
            "--output-file",
            str(output_path),
        ]
        if self.settings.motionphoto2_verbose:
            command.append("--verbose")

        logger.info("Running MotionPhoto2 for %s -> %s", pair.stem, output_path.name)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self.settings.process_timeout_seconds,
            check=False,
        )
        if result.stdout:
            logger.info("MotionPhoto2 stdout for %s:\n%s", pair.stem, result.stdout.rstrip())
        if result.stderr:
            logger.warning("MotionPhoto2 stderr for %s:\n%s", pair.stem, result.stderr.rstrip())
        if result.returncode != 0:
            raise RuntimeError(f"MotionPhoto2 exited with code {result.returncode}")

    def _resolve_output_path(self, expected_path: Path, started_at: float) -> Path | None:
        if expected_path.exists():
            return expected_path
        siblings = sorted(expected_path.parent.glob(f"{expected_path.stem}.*"))
        for sibling in siblings:
            if sibling.is_file() and sibling.stat().st_mtime >= started_at - 1:
                return sibling
        return None

    def _handle_successful_originals(self, pair: MediaPair, marker: str | None) -> None:
        if not self.settings.move_originals:
            logger.info("Leaving originals in input because move_originals=false for %s", pair.stem)
            return
        if not self.settings.enable_archive:
            logger.info("Leaving originals in input because enable_archive=false for %s", pair.stem)
            return
        self._archive_pair(pair, marker=marker)

    def _archive_pair(self, pair: MediaPair, marker: str | None) -> None:
        moved_image = move_one_file(pair.image_path, self.settings.archive_dir, marker=marker)
        moved_video = move_one_file(pair.video_path, self.settings.archive_dir, marker=marker)
        logger.info("Archived originals: image=%s video=%s", moved_image, moved_video)

    def _move_failed_artifacts(self, pair: MediaPair, output_path: Path | None, marker: str | None) -> None:
        moved_image = move_one_file(pair.image_path, self.settings.failed_dir, marker=marker)
        moved_video = move_one_file(pair.video_path, self.settings.failed_dir, marker=marker)
        if output_path is not None and output_path.exists():
            partial = move_one_file(output_path, self.settings.failed_dir, marker=f"{marker or 'failed'}_partial")
            logger.info("Moved partial output to failed: %s", partial)
        logger.info("Moved failed inputs: image=%s video=%s", moved_image, moved_video)
