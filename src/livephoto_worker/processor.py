from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from .db import ProcessingStore
from .file_utils import file_signature, hash_pair, move_one_file, quick_pair_state, unique_path
from .models import MediaItem, MediaPair, PairHashes
from .settings import Settings

logger = logging.getLogger(__name__)


class PairProcessor:
    def __init__(self, settings: Settings, store: ProcessingStore):
        self.settings = settings
        self.store = store

    def process(self, pair: MediaPair) -> bool:
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
                return False

            output_path = unique_path(self._destination_dir(self.settings.output_dir, pair.image_path), pair.image_path.name, marker=marker)
            started_at = time.time()
            self._run_motionphoto2(pair, output_path)

            actual_output = self._resolve_output_path(output_path, started_at)
            if actual_output is None or not actual_output.is_file():
                raise RuntimeError(f"MotionPhoto2 finished but output file was not created: {output_path}")

            self._handle_successful_originals(pair, marker=marker)
            self.store.record_success(pair, hashes, actual_output)
            logger.info("Created Motion Photo: %s", actual_output)
            return True
        except Exception as exc:  # noqa: BLE001 - quarantine bad inputs and keep the daemon alive.
            error = str(exc)
            logger.exception("Failed to process pair %s: %s", pair.stem, error)
            marker = None
            if hashes is not None:
                marker = hashes.image_sha256[:12]
            self._move_failed_artifacts(pair, output_path, marker=marker)
            self.store.record_failure(pair, pair_signature, error, output_path=output_path)
            return False

    def process_media(self, item: MediaItem) -> bool:
        logger.info("Preparing ordinary %s: %s", item.media_type, item.path)
        signature: str | None = None
        try:
            signature, digest = file_signature(item.path)
            marker = digest[:12]
            if self.store.is_file_processed(signature):
                logger.info("Ordinary %s already copied; skipping duplicate for %s", item.media_type, item.path.name)
                return False

            destination_dir = self._destination_dir(self.settings.output_dir, item.path)
            output_path = unique_path(destination_dir, item.path.name, marker=marker)
            shutil.copy2(item.path, output_path)
            self.store.record_copied_file(item, digest, output_path)
            logger.info("Copied ordinary %s to output: %s", item.media_type, output_path)
            return True
        except Exception as exc:  # noqa: BLE001 - keep daemon alive and quarantine bad inputs when possible.
            error = str(exc)
            logger.exception("Failed to copy ordinary %s %s: %s", item.media_type, item.path, error)
            marker = signature.split(":", 1)[1][:12] if signature else None
            move_one_file(item.path, self._destination_dir(self.settings.failed_dir, item.path), marker=marker)
            return False

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
        moved_image = move_one_file(pair.image_path, self._destination_dir(self.settings.archive_dir, pair.image_path), marker=marker)
        moved_video = move_one_file(pair.video_path, self._destination_dir(self.settings.archive_dir, pair.video_path), marker=marker)
        logger.info("Archived originals: image=%s video=%s", moved_image, moved_video)

    def _move_failed_artifacts(self, pair: MediaPair, output_path: Path | None, marker: str | None) -> None:
        moved_image = move_one_file(pair.image_path, self._destination_dir(self.settings.failed_dir, pair.image_path), marker=marker)
        moved_video = move_one_file(pair.video_path, self._destination_dir(self.settings.failed_dir, pair.video_path), marker=marker)
        if output_path is not None and output_path.exists():
            partial = move_one_file(output_path, self._destination_dir(self.settings.failed_dir, pair.image_path), marker=f"{marker or 'failed'}_partial")
            logger.info("Moved partial output to failed: %s", partial)
        logger.info("Moved failed inputs: image=%s video=%s", moved_image, moved_video)

    def _destination_dir(self, root: Path, source: Path) -> Path:
        if not self.settings.preserve_directory_structure:
            return root
        try:
            relative_parent = source.relative_to(self.settings.input_dir).parent
        except ValueError:
            return root
        if str(relative_parent) in ("", "."):
            return root
        logger.info("Preserved relative path: %s", relative_parent)
        return root / relative_parent
