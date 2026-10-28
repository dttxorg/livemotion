from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from .db import ProcessingStore
from .file_utils import file_signature, hash_pair, media_staging_directory, move_one_file, publish_unique_file, quick_pair_state
from .models import MediaItem, MediaPair, PairHashes
from .motionphoto_paths import motionphoto_output_filename, possible_motionphoto_outputs, resolve_motionphoto_output
from .settings import Settings

logger = logging.getLogger(__name__)


class MotionPhoto2Error(RuntimeError):
    def __init__(self, message: str, *, command: list[str], returncode: int | None, stderr: str):
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class ProcessingCancelled(RuntimeError):
    """The daemon is stopping; leave this candidate's originals in place."""


class PairProcessor:
    def __init__(self, settings: Settings, store: ProcessingStore):
        self.settings = settings
        self.store = store
        # The worker serializes processing and reads this only after the call.
        self.last_output_path: Path | None = None
        self._stop_event = threading.Event()
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        self._stop_event.set()
        with self._process_lock:
            process = self._active_process
        if process is not None:
            self._terminate_process(process)

    def process(self, pair: MediaPair) -> str:
        logger.info("Preparing pair: image=%s video=%s", pair.image_path.name, pair.video_path.name)
        self.last_output_path = None
        pair_signature: str | None = None
        hashes: PairHashes | None = None
        output_path: Path | None = None
        staging: tempfile.TemporaryDirectory[str] | None = None

        try:
            self._raise_if_stopping()
            quick_state = quick_pair_state(pair)
            logger.debug("Stable pair state: %s", quick_state)

            hashes = hash_pair(pair)
            pair_signature = hashes.signature
            marker = hashes.image_sha256[:12]

            existing_output = self.store.processed_pair_output(pair_signature)
            if existing_output is not None:
                self._raise_if_stopping()
                logger.info("Pair already processed; skipping duplicate for %s", pair.stem)
                if self.settings.move_originals and self.settings.enable_archive:
                    self._archive_pair(pair, marker=marker)
                self.store.record_duplicate(pair, hashes)
                self.last_output_path = existing_output
                return "skipped"

            destination_dir = self._destination_dir(self.settings.output_dir, pair.image_path)
            destination_dir.mkdir(parents=True, exist_ok=True)
            staging = self._staging_directory()
            output_path = Path(staging.name) / motionphoto_output_filename(pair.image_path)
            logger.info(
                "Starting MotionPhoto2 conversion: image=%s video=%s staged_output=%s destination=%s",
                pair.image_path,
                pair.video_path,
                output_path,
                destination_dir,
            )
            self._run_motionphoto2(pair, output_path)

            # Only this invocation can have written into the private directory.
            # Do not use the output mtime: converters may preserve source dates.
            actual_output = resolve_motionphoto_output(output_path)
            if actual_output is None:
                raise RuntimeError(f"MotionPhoto2 finished but output file was not created: {output_path}")
            if actual_output != output_path:
                logger.info("MotionPhoto2 actual output path: expected=%s actual=%s", output_path, actual_output)
            self._raise_if_stopping()
            output_path = publish_unique_file(actual_output, destination_dir, marker=marker)

            self._handle_successful_originals(pair, marker=marker)
            self.store.record_success(pair, hashes, output_path)
            self.last_output_path = output_path
            logger.info("Created Motion Photo: %s", output_path)
            return "merged"
        except ProcessingCancelled:
            logger.info("Cancelled MotionPhoto2 processing; leaving originals in input: %s", pair.stem)
            return "cancelled"
        except Exception as exc:  # noqa: BLE001 - quarantine bad inputs and keep the daemon alive.
            error = str(exc)
            logger.exception("Failed to process pair %s: %s", pair.stem, error)
            marker = hashes.image_sha256[:12] if hashes is not None else None
            quarantine_errors = self._move_failed_artifacts(
                pair,
                output_path,
                marker=marker,
                include_alternates=staging is not None and output_path is not None and output_path.parent == Path(staging.name),
            )
            if quarantine_errors:
                error += "; quarantine failed: " + "; ".join(quarantine_errors)
            try:
                self.store.record_failure(pair, pair_signature, error, output_path=output_path)
            except Exception:  # noqa: BLE001 - an unavailable history store must not stop scanning.
                logger.exception("Failed to record processing failure for %s; original error=%s", pair.stem, error)
            return "failed"
        finally:
            if staging is not None:
                try:
                    staging.cleanup()
                except OSError:
                    logger.exception("Failed to clean conversion staging directory: %s", staging.name)

    def process_media(self, item: MediaItem) -> str:
        logger.info("Preparing ordinary %s: %s", item.media_type, item.path)
        self.last_output_path = None
        signature: str | None = None
        staging: tempfile.TemporaryDirectory[str] | None = None
        try:
            self._raise_if_stopping()
            signature, digest = file_signature(item.path)
            marker = digest[:12]
            existing_output = self.store.processed_file_output(signature)
            if existing_output is not None:
                logger.info("Ordinary %s already copied; skipping duplicate for %s", item.media_type, item.path.name)
                self.store.record_skipped_file(item, signature)
                self.last_output_path = existing_output
                return "skipped"

            destination_dir = self._destination_dir(self.settings.output_dir, item.path)
            destination_dir.mkdir(parents=True, exist_ok=True)
            staging = self._staging_directory()
            staged_output = Path(staging.name) / item.path.name
            shutil.copy2(item.path, staged_output)
            self._raise_if_stopping()
            output_path = publish_unique_file(staged_output, destination_dir, marker=marker)
            self.store.record_copied_file(item, digest, output_path)
            self.last_output_path = output_path
            logger.info("Copied ordinary %s to output: %s", item.media_type, output_path)
            return "copied_photo" if item.media_type == "photo" else "copied_video"
        except ProcessingCancelled:
            logger.info("Cancelled media processing; leaving original in input: %s", item.path)
            return "cancelled"
        except Exception as exc:  # noqa: BLE001 - quarantine is best-effort, not a second failure boundary.
            error = str(exc)
            logger.exception("Failed to copy ordinary %s %s: %s", item.media_type, item.path, error)
            marker = signature.split(":", 1)[1][:12] if signature else None
            quarantine_errors: list[str] = []
            self._move_failed_file(item.path, item.path, marker, quarantine_errors)
            if quarantine_errors:
                error += "; quarantine failed: " + "; ".join(quarantine_errors)
            try:
                self.store.record_media_failure(item, signature, error)
            except Exception:  # noqa: BLE001 - preserve the original error in logs if history is unavailable.
                logger.exception("Failed to record media failure for %s; original error=%s", item.path, error)
            return "failed"
        finally:
            if staging is not None:
                try:
                    staging.cleanup()
                except OSError:
                    logger.exception("Failed to clean media staging directory: %s", staging.name)

    def _staging_directory(self) -> tempfile.TemporaryDirectory[str]:
        return media_staging_directory(
            self.settings.db_path.parent,
            media_roots=(self.settings.input_dir, self.settings.output_dir, self.settings.archive_dir, self.settings.failed_dir),
        )

    def _raise_if_stopping(self) -> None:
        if self._stop_event.is_set():
            raise ProcessingCancelled("worker stopping")

    @staticmethod
    def _signal_process(process: subprocess.Popen[str], *, force: bool = False) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif process.poll() is None:
                if force:
                    process.kill()
                else:
                    process.terminate()
        except ProcessLookupError:
            pass

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        try:
            self._signal_process(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning("MotionPhoto2 did not terminate promptly; killing process %s", process.pid)
            # A child may still hold the pipes after the parent has exited.
            # The POSIX session belongs exclusively to this conversion.
            self._signal_process(process, force=True)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("Failed to reap MotionPhoto2 process %s during shutdown", process.pid)

    def _run_motionphoto2(self, pair: MediaPair, output_path: Path) -> None:
        command = self.settings.build_motionphoto2_command(
            image_path=pair.image_path,
            video_path=pair.video_path,
            output_path=output_path,
        )

        logger.info("Running MotionPhoto2 for %s -> %s", pair.stem, output_path.name)
        process: subprocess.Popen[str] | None = None
        try:
            with self._process_lock:
                self._raise_if_stopping()
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=os.name == "posix",
                )
                self._active_process = process
            stdout, stderr = process.communicate(timeout=self.settings.process_timeout_seconds)
        except Exception as exc:
            if process is not None:
                self._terminate_process(process)
                process.communicate()
            self._raise_if_stopping()
            logger.exception("MotionPhoto2 invocation raised exception; command=%s", command)
            raise MotionPhoto2Error(
                f"MotionPhoto2 invocation failed: {exc}",
                command=command,
                returncode=None,
                stderr="",
            ) from exc
        finally:
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None
        self._raise_if_stopping()
        if stdout:
            logger.info("MotionPhoto2 stdout for %s:\n%s", pair.stem, stdout.rstrip())
        if stderr:
            logger.warning("MotionPhoto2 stderr for %s:\n%s", pair.stem, stderr.rstrip())
        if process.returncode != 0:
            logger.error(
                "MotionPhoto2 failed; command=%s returncode=%s stderr=%s",
                command,
                process.returncode,
                stderr.rstrip(),
            )
            raise MotionPhoto2Error(
                f"MotionPhoto2 exited with code {process.returncode}; stderr={stderr.rstrip()}",
                command=command,
                returncode=process.returncode,
                stderr=stderr,
            )

    def _resolve_output_path(self, expected_path: Path, started_at: float) -> Path | None:
        return resolve_motionphoto_output(expected_path, started_at=started_at)

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

    def _move_failed_file(self, source: Path, relative_source: Path, marker: str | None, errors: list[str]) -> None:
        try:
            moved = move_one_file(source, self._destination_dir(self.settings.failed_dir, relative_source), marker=marker)
            if moved is not None:
                logger.info("Moved failed artifact: source=%s destination=%s", source, moved)
        except Exception as exc:  # noqa: BLE001 - still attempt the other artifacts and persist the original error.
            errors.append(f"{source}: {exc}")
            logger.exception("Failed to quarantine artifact %s", source)

    def _move_failed_artifacts(
        self, pair: MediaPair, output_path: Path | None, marker: str | None, *, include_alternates: bool = False
    ) -> list[str]:
        errors: list[str] = []
        self._move_failed_file(pair.image_path, pair.image_path, marker, errors)
        self._move_failed_file(pair.video_path, pair.video_path, marker, errors)
        if output_path is not None:
            # While staging, a failed converter can also have changed suffix.
            outputs = possible_motionphoto_outputs(output_path) if include_alternates else [output_path]
            for partial in outputs:
                self._move_failed_file(partial, pair.image_path, f"{marker or 'failed'}_partial", errors)
        return errors

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
