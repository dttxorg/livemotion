from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from livephoto_worker.diagnostics import (
    MotionPhoto2DiagnosticResult,
    MotionPhoto2Status,
    check_motionphoto2_available,
    diagnostic_output_path,
    run_motionphoto2_diagnostic,
)
from livephoto_worker.db import ProcessingStore
from livephoto_worker.file_utils import hash_pair
from livephoto_worker.models import MediaPair
from livephoto_worker.processor import PairProcessor
from livephoto_worker.scanner import scan_media, scan_pairs
from livephoto_worker.settings import Settings
from livephoto_worker.worker import LivePhotoWorker
from livephoto_worker.logging_buffer import RecentLogHandler

try:
    from livephoto_worker.web import create_app
except ModuleNotFoundError:
    create_app = None


class RecordingProcessor:
    def __init__(self) -> None:
        self.processed: list[MediaPair] = []

    def process(self, pair: MediaPair) -> None:
        self.processed.append(pair)


class FakeWebWorker:
    def __init__(self) -> None:
        self.cleared = False
        self.scans = 0
        self.force_scans = 0

    def clear_pending(self) -> None:
        self.cleared = True

    def scan_once(self) -> int:
        self.scans += 1
        return 7

    def force_scan_once(self) -> int:
        self.force_scans += 1
        return 9

    def pending_status(self) -> dict[str, float | int | None]:
        return {
            "waiting_count": 3,
            "waiting_live_pairs": 1,
            "earliest_first_seen_at": 100,
            "next_process_at": 130,
            "oldest_wait_seconds": 20,
        }

    def candidate_debug_rows(self) -> list[dict[str, object]]:
        return [{
            "candidate_type": "live_photo",
            "path": "image=/photos/IMG_0001.HEIC video=/photos/IMG_0001.MOV",
            "waited_seconds": 12.5,
            "first_seen_at": 100,
            "last_seen_at": 112.5,
            "next_process_at": 130,
            "is_stable": True,
            "reason": "waiting_for_stable",
            "image_size": 10,
            "video_size": 20,
            "image_mtime": 1000000,
            "video_mtime": 2000000,
        }]


def write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def create_fake_motionphoto2(path: Path, exit_code: int = 0, write_output: bool = True) -> Path:
    script = """#!/bin/sh
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-file)
      shift
      out="$1"
      ;;
  esac
  shift
done
if [ "{write_output}" = "yes" ] && [ -n "$out" ]; then
  printf 'motion-photo-output' > "$out"
fi
exit {exit_code}
""".format(
        exit_code=exit_code,
        write_output="yes" if write_output else "no",
    )
    path.write_text(script)
    path.chmod(0o755)
    return path


def create_fake_motionphoto2_with_suffix(path: Path, actual_suffix: str) -> Path:
    script = """#!/bin/sh
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-file)
      shift
      out="$1"
      ;;
  esac
  shift
done
if [ -n "$out" ]; then
  base="${{out%.*}}"
  printf 'motion-photo-output' > "${{base}}{actual_suffix}"
fi
exit 0
""".format(actual_suffix=actual_suffix)
    path.write_text(script)
    path.chmod(0o755)
    return path


def test_settings(root: Path, **overrides) -> Settings:
    defaults = dict(
        input_dir=root / "input",
        output_dir=root / "output",
        archive_dir=root / "archive",
        failed_dir=root / "failed",
        db_path=root / "config" / "db.sqlite3",
        config_path=root / "config" / "config.json",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def record_job_for_local_day(
    store: ProcessingStore,
    *,
    day: date,
    status: str,
    hour: int = 12,
    minute: int = 0,
) -> None:
    """Insert a fixture job whose SQLite local date is ``day``."""
    local_timezone = datetime.now().astimezone().tzinfo or timezone.utc
    created_at = datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=local_timezone,
    ).astimezone(timezone.utc).isoformat(timespec="seconds")
    with store.lock, store.conn:
        store.conn.execute(
            """
            INSERT INTO jobs (
                pair_signature, stem, image_name, video_name,
                status, output_path, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                f"fixture-{status}-{day.isoformat()}-{hour:02d}{minute:02d}",
                "fixture.HEIC",
                "fixture.MOV",
                status,
                None,
                None,
                created_at,
            ),
        )


class LivePhotoWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)

    def test_missing_config_uses_new_photo_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"

            settings = Settings.load(config_path=config_path)

            self.assertEqual(settings.input_dir, Path("/photos/live_inbox"))
            self.assertEqual(settings.output_dir, Path("/photos/motion_output"))
            self.assertEqual(settings.archive_dir, Path("/photos/archive"))
            self.assertEqual(settings.failed_dir, Path("/photos/failed"))
            self.assertEqual(settings.stable_seconds, 30)
            self.assertEqual(settings.poll_interval, 10)
            self.assertTrue(settings.move_originals)
            self.assertTrue(settings.enable_archive)
            self.assertTrue(settings.recursive_scan)
            self.assertTrue(settings.preserve_directory_structure)
            self.assertIn(".stfolder", settings.skip_dir_names)
            self.assertIn("@eaDir", settings.skip_dir_names)

    def test_config_file_is_loaded_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps({
                "input_dir": str(root / "inbox"),
                "output_dir": str(root / "out"),
                "archive_dir": str(root / "done"),
                "failed_dir": str(root / "bad"),
                "stable_seconds": 12,
                "poll_interval": 3,
                "move_originals": False,
                "enable_archive": False,
                "recursive_scan": False,
                "preserve_directory_structure": False,
                "skip_dir_names": ["custom-skip"],
            }))

            settings = Settings.load(config_path=config_path)
            settings.db_path = root / "config" / "db.sqlite3"
            settings.update_from_config({
                "input_dir": str(root / "new-inbox"),
                "output_dir": str(root / "new-out"),
                "archive_dir": str(root / "new-archive"),
                "failed_dir": str(root / "new-failed"),
                "stable_seconds": 5,
                "poll_interval": 2,
                "move_originals": True,
                "enable_archive": True,
                "recursive_scan": True,
                "preserve_directory_structure": True,
                "skip_dir_names": [".stfolder", "custom-skip"],
            })

            saved = json.loads(config_path.read_text())
            self.assertEqual(saved["input_dir"], str(root / "new-inbox"))
            self.assertEqual(saved["stable_seconds"], 5)
            self.assertTrue(saved["move_originals"])
            self.assertTrue(saved["recursive_scan"])
            self.assertTrue(saved["preserve_directory_structure"])
            self.assertEqual(saved["skip_dir_names"], [".stfolder", "custom-skip"])

    def test_settings_builds_source_motionphoto2_command_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = test_settings(root)

            command = settings.build_motionphoto2_command(
                image_path=Path("/photos/2026/5/IMG_0056.HEIC"),
                video_path=Path("/photos/2026/5/IMG_0056.MOV"),
                output_path=Path("/photos/motion_output/2026/5/IMG_0056.HEIC"),
            )

            self.assertGreaterEqual(len(command), 8)
            self.assertEqual(command[0], settings.motionphoto2_python)
            self.assertEqual(Path(command[1]), settings.motionphoto2_script)
            self.assertEqual(settings.build_motionphoto2_help_command(), [
                settings.motionphoto2_python,
                str(settings.motionphoto2_script),
                "--help",
            ])
            self.assertIn("--input-image", command)
            self.assertIn("/photos/2026/5/IMG_0056.HEIC", command)
            self.assertIn("--input-video", command)
            self.assertIn("/photos/2026/5/IMG_0056.MOV", command)
            self.assertIn("--output-file", command)
            self.assertIn("/photos/motion_output/2026/5/IMG_0056.HEIC", command)

    def test_diagnostic_output_path_matches_input_image_format(self) -> None:
        photos_root = Path("/photos")

        self.assertEqual(
            diagnostic_output_path(Path("/photos/2026/5/IMG_0056.HEIC"), photos_root=photos_root),
            Path("/photos/live/2026/5/IMG_0056.heic"),
        )
        self.assertEqual(
            diagnostic_output_path(Path("/photos/2026/5/IMG_0057.HEIF"), photos_root=photos_root),
            Path("/photos/live/2026/5/IMG_0057.heic"),
        )
        self.assertEqual(
            diagnostic_output_path(Path("/photos/2026/5/IMG_0058.JPEG"), photos_root=photos_root),
            Path("/photos/live/2026/5/IMG_0058.jpg"),
        )

    def test_motionphoto2_startup_selfcheck_reports_unavailable(self) -> None:
        logging.disable(logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = create_fake_motionphoto2(root / "motionphoto2", exit_code=9, write_output=False)
            settings = test_settings(root, motionphoto2_bin=str(fake_bin))

            status = check_motionphoto2_available(settings)

            self.assertFalse(status.available)
            self.assertEqual(status.returncode, 9)
            self.assertIn(str(fake_bin), status.command)

    def test_scan_pairs_matches_same_stem_and_prefers_heic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root / "IMG_0001.JPG", b"jpg")
            write_file(root / "IMG_0001.HEIC", b"heic")
            write_file(root / "IMG_0001.MOV", b"mov")
            write_file(root / "IMG_0002.HEIC", b"missing-video")

            pairs = scan_pairs(root)

            self.assertEqual(len(pairs), 1)
            self.assertEqual(pairs[0].stem, "IMG_0001")
            self.assertEqual(pairs[0].image_path.name, "IMG_0001.HEIC")
            self.assertEqual(pairs[0].video_path.name, "IMG_0001.MOV")

    def test_scan_media_recurses_and_does_not_pair_across_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root / "2024" / "01" / "IMG_0001.HEIC", b"heic")
            write_file(root / "2024" / "01" / "IMG_0001.MOV", b"mov")
            write_file(root / "2024" / "02" / "IMG_0002.JPG", b"jpg")
            write_file(root / "2025" / "trip" / "IMG_0002.MP4", b"mp4")
            write_file(root / "2025" / "trip" / "CLIP.M4V", b"m4v")
            write_file(root / "2025" / "trip" / "PHOTO.PNG", b"png")

            result = scan_media(root)

            self.assertEqual(len(result.pairs), 1)
            self.assertEqual(result.pairs[0].image_path, root / "2024" / "01" / "IMG_0001.HEIC")
            self.assertEqual(result.pairs[0].video_path, root / "2024" / "01" / "IMG_0001.MOV")
            self.assertEqual(
                {(item.path.name, item.media_type) for item in result.media_items},
                {
                    ("IMG_0002.JPG", "photo"),
                    ("IMG_0002.MP4", "video"),
                    ("CLIP.M4V", "video"),
                    ("PHOTO.PNG", "photo"),
                },
            )

    def test_recursive_worker_merges_live_photo_and_preserves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            archive_dir = root / "archive"
            failed_dir = root / "failed"
            write_file(input_dir / "2024" / "01" / "IMG_0001.HEIC", b"image-bytes")
            write_file(input_dir / "2024" / "01" / "IMG_0001.MOV", b"video-bytes")
            fake_bin = create_fake_motionphoto2(root / "motionphoto2")
            settings = test_settings(
                root,
                input_dir=input_dir,
                output_dir=output_dir,
                archive_dir=archive_dir,
                failed_dir=failed_dir,
                motionphoto2_bin=str(fake_bin),
                stable_seconds=0,
            )
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            worker.scan_once(now=100)
            processed = worker.scan_once(now=101)

            self.assertEqual(processed, 1)
            self.assertTrue((output_dir / "2024" / "01" / "IMG_0001.heic").is_file())
            self.assertFalse((output_dir / "2024" / "01" / "IMG_0001.MOV").exists())
            self.assertTrue((archive_dir / "2024" / "01" / "IMG_0001.HEIC").is_file())
            self.assertTrue((archive_dir / "2024" / "01" / "IMG_0001.MOV").is_file())
            self.assertEqual(worker.scan_stats.merged_live_photos, 1)
            store.close()

    def test_recursive_worker_copies_ordinary_photo_and_video_with_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            write_file(input_dir / "2024" / "02" / "IMG_0002.PNG", b"png")
            write_file(input_dir / "2025" / "trip" / "IMG_0003.M4V", b"m4v")
            settings = test_settings(root, input_dir=input_dir, output_dir=output_dir, stable_seconds=0)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            worker.scan_once(now=100)
            processed = worker.scan_once(now=101)

            self.assertEqual(processed, 2)
            self.assertTrue((output_dir / "2024" / "02" / "IMG_0002.PNG").is_file())
            self.assertTrue((output_dir / "2025" / "trip" / "IMG_0003.M4V").is_file())
            self.assertTrue((input_dir / "2024" / "02" / "IMG_0002.PNG").is_file())
            self.assertTrue((input_dir / "2025" / "trip" / "IMG_0003.M4V").is_file())
            stats = store.stats()
            self.assertEqual(stats["copied_photo_count"], 1)
            self.assertEqual(stats["copied_video_count"], 1)
            self.assertEqual(worker.scan_stats.copied_photos, 1)
            self.assertEqual(worker.scan_stats.copied_videos, 1)

            restarted_worker = LivePhotoWorker(settings=settings, processor=processor)
            restarted_worker.scan_once(now=200)
            restarted_worker.scan_once(now=201)
            restarted_stats = store.stats()
            self.assertEqual(restarted_stats["copied_photo_count"], 1)
            self.assertEqual(restarted_stats["copied_video_count"], 1)
            self.assertEqual(restarted_stats["skipped_count"], 2)
            store.close()

    def test_recursive_scan_skips_output_dir_when_nested_in_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = input_dir / "motion_output"
            write_file(input_dir / "2024" / "solo.JPG", b"photo")
            write_file(output_dir / "already-output.JPG", b"output-only")
            settings = test_settings(root, input_dir=input_dir, output_dir=output_dir, stable_seconds=0)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            worker.scan_once(now=100)
            worker.scan_once(now=101)

            self.assertTrue((output_dir / "2024" / "solo.JPG").is_file())
            self.assertFalse((output_dir / "motion_output" / "already-output.JPG").exists())
            self.assertGreaterEqual(worker.scan_stats.skipped_dirs, 1)
            self.assertEqual(store.stats()["copied_photo_count"], 1)
            store.close()

    def test_scan_logs_full_media_library_summary(self) -> None:
        logging.disable(logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            write_file(input_dir / "album" / "solo.JPG", b"photo")
            settings = test_settings(root, input_dir=input_dir, output_dir=output_dir, stable_seconds=0)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            with self.assertLogs("livephoto_worker.worker", level="INFO") as captured:
                worker.scan_once(now=100)
                worker.scan_once(now=101)

            log_text = "\n".join(captured.output)
            self.assertIn("Scanning input folder as full media library", log_text)
            self.assertIn("detected_live_pairs=0 detected_normal_photos=1 detected_normal_videos=0 waiting_for_stable=0", log_text)
            self.assertIn("Scan finished:", log_text)
            self.assertIn("processed_live=0 copied_photos=1 copied_videos=0", log_text)
            self.assertIn("waiting=0 skipped=0 failed=0", log_text)
            self.assertNotIn("Detected candidate media", log_text)
            self.assertNotIn("solo.JPG; waiting", log_text)
            store.close()

    def test_waiting_too_long_warns_once(self) -> None:
        logging.disable(logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root / "IMG_0001.JPG", b"photo")
            settings = test_settings(root, input_dir=root, output_dir=root / "out", stable_seconds=600)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            worker.scan_once(now=0)
            with self.assertLogs("livephoto_worker.worker", level="WARNING") as captured:
                worker.scan_once(now=301)

            self.assertIn("Candidate has been waiting too long", "\n".join(captured.output))
            store.close()

    def test_force_scan_ignores_stable_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            write_file(input_dir / "album" / "solo.JPG", b"photo")
            settings = test_settings(root, input_dir=input_dir, output_dir=output_dir, stable_seconds=30)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            processed = worker.force_scan_once(now=100)

            self.assertEqual(processed, 1)
            self.assertTrue((output_dir / "album" / "solo.JPG").is_file())
            self.assertEqual(worker.pending_status(now=100)["waiting_count"], 0)
            store.close()

    def test_first_live_photo_scan_records_waiting_candidate_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "2026" / "5" / "IMG_0056.HEIC"
            video = root / "2026" / "5" / "IMG_0056.MOV"
            write_file(image, b"image-bytes")
            write_file(video, b"video-bytes")
            settings = test_settings(root, input_dir=root, output_dir=root / "out", stable_seconds=30)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            processed = worker.scan_once(now=100)

            self.assertEqual(processed, 0)
            self.assertEqual(worker.pending_status(now=100)["waiting_count"], 1)
            rows = worker.candidate_debug_rows(now=100)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["candidate_type"], "live_photo")
            self.assertEqual(rows[0]["reason"], "waiting_for_stable")
            self.assertEqual(rows[0]["image_size"], len(b"image-bytes"))
            self.assertEqual(rows[0]["video_size"], len(b"video-bytes"))
            self.assertIn(str(image), str(rows[0]["path"]))
            self.assertIn(str(video), str(rows[0]["path"]))
            store.close()

    def test_force_scan_processes_live_photo_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            archive_dir = root / "archive"
            failed_dir = root / "failed"
            image = input_dir / "2026" / "5" / "IMG_0056.HEIC"
            video = input_dir / "2026" / "5" / "IMG_0056.MOV"
            write_file(image, b"image-bytes")
            write_file(video, b"video-bytes")
            fake_bin = create_fake_motionphoto2(root / "motionphoto2")
            settings = test_settings(
                root,
                input_dir=input_dir,
                output_dir=output_dir,
                archive_dir=archive_dir,
                failed_dir=failed_dir,
                motionphoto2_bin=str(fake_bin),
                stable_seconds=30,
            )
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            processed = worker.force_scan_once(now=100)

            self.assertEqual(processed, 1)
            self.assertTrue((output_dir / "2026" / "5" / "IMG_0056.heic").is_file())
            self.assertEqual(store.latest_job()["status"], "success")  # type: ignore[index]
            store.close()

    def test_motionphoto2_failure_increments_failed_and_logs_details(self) -> None:
        logging.disable(logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            failed_dir = root / "failed"
            image = input_dir / "IMG_0056.HEIC"
            video = input_dir / "IMG_0056.MOV"
            write_file(image, b"image-bytes")
            write_file(video, b"video-bytes")
            fake_bin = create_fake_motionphoto2(root / "motionphoto2", exit_code=7, write_output=False)
            settings = test_settings(
                root,
                input_dir=input_dir,
                output_dir=output_dir,
                failed_dir=failed_dir,
                motionphoto2_bin=str(fake_bin),
                stable_seconds=0,
            )
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings=settings, store=store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            with self.assertLogs(level="INFO") as captured:
                worker.scan_once(now=100)
                processed = worker.scan_once(now=101)

            log_text = "\n".join(captured.output)
            self.assertEqual(processed, 1)
            self.assertIn("Starting MotionPhoto2 conversion: image=", log_text)
            self.assertIn("MotionPhoto2 failed; command=", log_text)
            self.assertIn("returncode=7", log_text)
            self.assertIn("Traceback", log_text)
            self.assertIn("reason=conversion_failed", log_text)
            self.assertIn("failed=1", log_text)
            self.assertEqual(worker.scan_stats.failed, 1)
            latest = store.latest_job()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["status"], "failed")
            store.close()

    def test_diagnostic_pair_runs_motionphoto2_directly_to_live_folder(self) -> None:
        logging.disable(logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos_root = root / "photos"
            image = photos_root / "2026" / "5" / "IMG_0056.HEIC"
            video = photos_root / "2026" / "5" / "IMG_0056.MOV"
            write_file(image, b"image-bytes")
            write_file(video, b"video-bytes")
            fake_bin = create_fake_motionphoto2(root / "motionphoto2")
            settings = test_settings(root, motionphoto2_bin=str(fake_bin))

            with self.assertLogs("livephoto_worker.diagnostics", level="INFO") as captured:
                result = run_motionphoto2_diagnostic(
                    settings=settings,
                    image_path=image,
                    video_path=video,
                    output_root=photos_root / "live",
                    photos_root=photos_root,
                )

            expected_output = photos_root / "live" / "2026" / "5" / "IMG_0056.heic"
            self.assertTrue(result.success)
            self.assertTrue(result.image_exists)
            self.assertTrue(result.video_exists)
            self.assertEqual(result.output_path, expected_output)
            self.assertTrue(expected_output.is_file())
            self.assertIn("--input-image", result.command)
            self.assertIn(str(image), result.command)
            self.assertIn("--input-video", result.command)
            self.assertIn(str(video), result.command)
            self.assertIn("--output-file", result.command)
            self.assertIn(str(expected_output), result.command)
            self.assertEqual(result.returncode, 0)
            log_text = "\n".join(captured.output)
            self.assertIn("image exists", log_text)
            self.assertIn("video exists", log_text)
            self.assertIn("output path", log_text)
            self.assertIn("MotionPhoto2 command", log_text)
            self.assertIn("return code", log_text)
            self.assertIn("stdout", log_text)
            self.assertIn("stderr", log_text)

    def test_diagnostic_pair_accepts_actual_output_when_expected_missing(self) -> None:
        logging.disable(logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos_root = root / "photos"
            image = photos_root / "2026" / "5" / "IMG_0058.JPG"
            video = photos_root / "2026" / "5" / "IMG_0058.MOV"
            write_file(image, b"image-bytes")
            write_file(video, b"video-bytes")
            fake_bin = create_fake_motionphoto2_with_suffix(root / "motionphoto2", ".jpeg")
            settings = test_settings(root, motionphoto2_bin=str(fake_bin))

            with self.assertLogs("livephoto_worker.diagnostics", level="INFO") as captured:
                result = run_motionphoto2_diagnostic(
                    settings=settings,
                    image_path=image,
                    video_path=video,
                    output_root=photos_root / "live",
                    photos_root=photos_root,
                )

            actual_output = photos_root / "live" / "2026" / "5" / "IMG_0058.jpeg"
            self.assertTrue(result.success)
            self.assertEqual(result.output_path, actual_output)
            self.assertTrue(actual_output.is_file())
            self.assertIn("actual output path", "\n".join(captured.output))

    def test_live_photo_candidate_info_is_limited_to_first_ten(self) -> None:
        logging.disable(logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(12):
                write_file(root / f"IMG_{index:04d}.HEIC", b"image")
                write_file(root / f"IMG_{index:04d}.MOV", b"video")
            settings = test_settings(root, input_dir=root, output_dir=root / "out", stable_seconds=30)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            worker = LivePhotoWorker(settings=settings, processor=processor)

            with self.assertLogs("livephoto_worker.worker", level="INFO") as captured:
                worker.scan_once(now=100)

            log_text = "\n".join(captured.output)
            self.assertEqual(log_text.count("Detected Live Photo candidate"), 10)
            self.assertIn("Detected 2 additional Live Photo candidates; showing only first 10", log_text)
            store.close()

    def test_worker_waits_until_pair_state_is_stable_for_settle_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root / "IMG_0001.HEIC", b"image")
            write_file(root / "IMG_0001.MOV", b"video")
            settings = test_settings(
                root,
                input_dir=root,
                output_dir=root / "out",
                stable_seconds=10,
                poll_interval=1,
            )
            processor = RecordingProcessor()
            worker = LivePhotoWorker(settings=settings, processor=processor)  # type: ignore[arg-type]

            worker.scan_once(now=100)
            worker.scan_once(now=109)
            self.assertEqual(processor.processed, [])

            worker.scan_once(now=111)
            self.assertEqual(len(processor.processed), 1)
            self.assertEqual(processor.processed[0].stem, "IMG_0001")

    def test_worker_resets_first_seen_when_candidate_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "IMG_0001.HEIC"
            video = root / "IMG_0001.MOV"
            write_file(image, b"image")
            write_file(video, b"video")
            settings = test_settings(
                root,
                input_dir=root,
                output_dir=root / "out",
                stable_seconds=10,
                poll_interval=1,
            )
            processor = RecordingProcessor()
            worker = LivePhotoWorker(settings=settings, processor=processor)  # type: ignore[arg-type]

            worker.scan_once(now=100)
            write_file(video, b"video-changed")
            worker.scan_once(now=105)
            worker.scan_once(now=114)
            self.assertEqual(processor.processed, [])

            worker.scan_once(now=116)
            self.assertEqual(len(processor.processed), 1)

    def test_processor_success_runs_motionphoto2_moves_originals_and_records_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            archive_dir = root / "archive"
            failed_dir = root / "failed"
            write_file(input_dir / "IMG_0001.HEIC", b"image-bytes")
            write_file(input_dir / "IMG_0001.MOV", b"video-bytes")
            fake_bin = create_fake_motionphoto2(root / "motionphoto2")
            settings = test_settings(
                root,
                input_dir=input_dir,
                output_dir=output_dir,
                archive_dir=archive_dir,
                failed_dir=failed_dir,
                motionphoto2_bin=str(fake_bin),
            )
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            pair = MediaPair("IMG_0001", input_dir / "IMG_0001.HEIC", input_dir / "IMG_0001.MOV")

            processor.process(pair)

            self.assertTrue((output_dir / "IMG_0001.heic").is_file())
            self.assertFalse((input_dir / "IMG_0001.HEIC").exists())
            self.assertFalse((input_dir / "IMG_0001.MOV").exists())
            self.assertTrue((archive_dir / "IMG_0001.HEIC").is_file())
            self.assertTrue((archive_dir / "IMG_0001.MOV").is_file())
            latest = store.latest_job()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["status"], "success")
            store.close()


    def test_processor_success_can_leave_originals_in_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            archive_dir = root / "archive"
            failed_dir = root / "failed"
            write_file(input_dir / "IMG_0001.HEIC", b"image-bytes")
            write_file(input_dir / "IMG_0001.MOV", b"video-bytes")
            fake_bin = create_fake_motionphoto2(root / "motionphoto2")
            settings = test_settings(
                root,
                input_dir=input_dir,
                output_dir=output_dir,
                archive_dir=archive_dir,
                failed_dir=failed_dir,
                motionphoto2_bin=str(fake_bin),
                move_originals=False,
            )
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            pair = MediaPair("IMG_0001", input_dir / "IMG_0001.HEIC", input_dir / "IMG_0001.MOV")

            processor.process(pair)

            self.assertTrue((output_dir / "IMG_0001.heic").is_file())
            self.assertTrue((input_dir / "IMG_0001.HEIC").is_file())
            self.assertTrue((input_dir / "IMG_0001.MOV").is_file())
            self.assertFalse((archive_dir / "IMG_0001.HEIC").exists())
            latest = store.latest_job()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["status"], "success")
            store.close()

    def test_processor_records_actual_output_when_expected_missing(self) -> None:
        logging.disable(logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            archive_dir = root / "archive"
            failed_dir = root / "failed"
            write_file(input_dir / "IMG_0058.JPG", b"image-bytes")
            write_file(input_dir / "IMG_0058.MOV", b"video-bytes")
            fake_bin = create_fake_motionphoto2_with_suffix(root / "motionphoto2", ".jpeg")
            settings = test_settings(
                root,
                input_dir=input_dir,
                output_dir=output_dir,
                archive_dir=archive_dir,
                failed_dir=failed_dir,
                motionphoto2_bin=str(fake_bin),
            )
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            pair = MediaPair("IMG_0058", input_dir / "IMG_0058.JPG", input_dir / "IMG_0058.MOV")

            with self.assertLogs("livephoto_worker.processor", level="INFO") as captured:
                result = processor.process(pair)

            actual_output = output_dir / "IMG_0058.jpeg"
            self.assertEqual(result, "merged")
            self.assertTrue(actual_output.is_file())
            latest = store.latest_job()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["status"], "success")
            self.assertEqual(latest["output_path"], str(actual_output))
            self.assertIn("actual output path", "\n".join(captured.output))
            store.close()

    def test_processor_duplicate_skips_motionphoto2_and_archives_originals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            archive_dir = root / "archive"
            failed_dir = root / "failed"
            write_file(input_dir / "IMG_0001.HEIC", b"image-bytes")
            write_file(input_dir / "IMG_0001.MOV", b"video-bytes")
            settings = test_settings(
                root,
                input_dir=input_dir,
                output_dir=output_dir,
                archive_dir=archive_dir,
                failed_dir=failed_dir,
                motionphoto2_bin=str(root / "does-not-exist"),
            )
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            pair = MediaPair("IMG_0001", input_dir / "IMG_0001.HEIC", input_dir / "IMG_0001.MOV")
            hashes = hash_pair(pair)
            store.record_success(pair, hashes, output_dir / "IMG_0001.HEIC")

            processor = PairProcessor(settings, store)
            processor.process(pair)

            self.assertFalse((output_dir / "IMG_0001.HEIC").exists())
            self.assertTrue((archive_dir / "IMG_0001.HEIC").is_file())
            self.assertTrue((archive_dir / "IMG_0001.MOV").is_file())
            latest = store.latest_job()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["status"], "skipped_duplicate")
            store.close()

    def test_processor_failure_moves_inputs_to_failed_and_records_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            archive_dir = root / "archive"
            failed_dir = root / "failed"
            write_file(input_dir / "IMG_0001.HEIC", b"image-bytes")
            write_file(input_dir / "IMG_0001.MOV", b"video-bytes")
            fake_bin = create_fake_motionphoto2(root / "motionphoto2", exit_code=3, write_output=False)
            settings = test_settings(
                root,
                input_dir=input_dir,
                output_dir=output_dir,
                archive_dir=archive_dir,
                failed_dir=failed_dir,
                motionphoto2_bin=str(fake_bin),
            )
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            processor = PairProcessor(settings, store)
            pair = MediaPair("IMG_0001", input_dir / "IMG_0001.HEIC", input_dir / "IMG_0001.MOV")

            processor.process(pair)

            self.assertFalse((input_dir / "IMG_0001.HEIC").exists())
            self.assertFalse((input_dir / "IMG_0001.MOV").exists())
            self.assertTrue((failed_dir / "IMG_0001.HEIC").is_file())
            self.assertTrue((failed_dir / "IMG_0001.MOV").is_file())
            latest = store.latest_job()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["status"], "failed")
            self.assertIn("MotionPhoto2 exited with code 3", latest["error"])
            store.close()

    def test_store_seven_day_trend_groups_statuses_zero_fills_and_respects_local_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProcessingStore(test_settings(root).db_path)
            end_day = date(2042, 4, 20)
            first_day = end_day - timedelta(days=6)

            # The first and last days are both inclusive.  The local timestamps
            # exercise SQLite's date(..., 'localtime') grouping boundary.
            record_job_for_local_day(store, day=first_day, status="success", hour=0, minute=1)
            record_job_for_local_day(store, day=first_day, status="failed", hour=23, minute=59)
            record_job_for_local_day(store, day=end_day, status="copied_photo", hour=0, minute=1)
            record_job_for_local_day(store, day=end_day, status="copied_video", hour=23, minute=59)
            record_job_for_local_day(store, day=end_day, status="skipped_duplicate", hour=12)
            record_job_for_local_day(
                store,
                day=first_day - timedelta(days=1),
                status="failed",
                hour=23,
                minute=59,
            )

            trend = store.seven_day_trend(days=7, end_date=end_day)

            self.assertEqual(
                [point["date"] for point in trend],
                [(first_day + timedelta(days=offset)).isoformat() for offset in range(7)],
            )
            self.assertEqual(trend[0]["completed"], 1)
            self.assertEqual(trend[0]["failed"], 1)
            self.assertEqual(trend[1]["completed"], 0)
            self.assertEqual(trend[1]["failed"], 0)
            self.assertEqual(trend[-1]["completed"], 2)
            self.assertEqual(trend[-1]["failed"], 0)
            self.assertEqual(trend[-1]["label"], "4/20")
            stats = store.stats()
            self.assertEqual(stats["completed_count"], 3)
            self.assertEqual(stats["failed_count"], 2)
            self.assertEqual(len(stats["seven_day_trend"]), 7)
            with self.assertRaises(ValueError):
                store.seven_day_trend(days=0, end_date=end_day)
            store.close()

    @unittest.skipIf(create_app is None, "Flask is not installed")
    def test_web_dashboard_settings_and_scan_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = test_settings(root)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            fake_worker = FakeWebWorker()
            log_handler = RecentLogHandler()
            motionphoto2_status = MotionPhoto2Status(
                available=False,
                command=["python", "/opt/MotionPhoto2/motionphoto2.py", "--help"],
                returncode=127,
                stdout="",
                stderr="GLIBC_2.38 not found",
                error="MotionPhoto2 self-check failed",
            )
            pair = MediaPair("IMG_0001", root / "IMG_0001.HEIC", root / "IMG_0001.MOV")
            store.record_job(pair, pair_signature="sig-success", status="success", output_path=root / "out.heic", error=None)
            store.record_job(pair, pair_signature="sig-failed", status="failed", output_path=None, error="bad file")
            app = create_app(  # type: ignore[misc]
                settings=settings,
                worker=fake_worker,  # type: ignore[arg-type]
                store=store,
                log_handler=log_handler,
                motionphoto2_status=motionphoto2_status,
            )
            client = app.test_client()

            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"bootstrap@5.3.3", response.data)
            self.assertIn(b"/static/dashboard.css", response.data)
            for label in ("控制台", "设置", "任务记录", "日志", "高级工具", "关于"):
                self.assertIn(label.encode(), response.data)
            for label in ("运行概览", "运行健康", "今日完成", "今日失败", "等待稳定", "最近活动", "7 天趋势", "最近任务"):
                self.assertIn(label.encode(), response.data)
            self.assertIn("立即扫描".encode(), response.data)
            self.assertIn("强制扫描会跳过稳定等待".encode(), response.data)
            self.assertEqual(response.data.count(b'class="trend-day"'), 7)
            self.assertNotIn("Live Photo 输入目录".encode(), response.data)
            self.assertIn("MotionPhoto2 不可用".encode(), response.data)
            self.assertIn("GLIBC_2.38 not found".encode(), response.data)

            settings_response = client.get("/settings")
            self.assertEqual(settings_response.status_code, 200)
            self.assertIn(b'data-page="settings"', settings_response.data)
            for label in (
                "目录与同步",
                "扫描节奏",
                "文件处理",
                "扫描排除",
                "Live Photo 输入目录",
                "Pixel 同步输出目录",
                "原始文件归档目录",
                "失败文件目录",
                "文件稳定等待时间（秒）",
                "扫描间隔（秒）",
                "转换后移动原文件",
                "启用归档",
                "递归扫描",
                "保留原目录结构",
                "跳过目录列表",
                "使用标准目录",
            ):
                self.assertIn(label.encode(), settings_response.data)
            self.assertEqual(settings_response.data.count(b"data-fill-default-dirs"), 1)
            self.assertNotIn(b"data-dir-target", settings_response.data)

            response = client.post("/save", data={
                "input_dir": str(root / "photos" / "in"),
                "output_dir": str(root / "photos" / "out"),
                "archive_dir": str(root / "photos" / "archive"),
                "failed_dir": str(root / "photos" / "failed"),
                "stable_seconds": "9",
                "poll_interval": "4",
                "move_originals": "1",
                "recursive_scan": "1",
                "preserve_directory_structure": "1",
                "skip_dir_names": ".stfolder\n@eaDir\n#recycle",
            })
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].startswith("/settings?message="))
            saved = json.loads(settings.config_path.read_text())
            self.assertEqual(saved["input_dir"], str(root / "photos" / "in"))
            self.assertEqual(saved["stable_seconds"], 9.0)
            self.assertTrue(saved["move_originals"])
            self.assertFalse(saved["enable_archive"])
            self.assertTrue(saved["recursive_scan"])
            self.assertTrue(saved["preserve_directory_structure"])
            self.assertEqual(saved["skip_dir_names"], [".stfolder", "@eaDir", "#recycle"])
            self.assertTrue(fake_worker.cleared)

            saved_page = client.get(response.headers["Location"])
            self.assertEqual(saved_page.status_code, 200)
            self.assertIn("配置已保存并立即生效".encode(), saved_page.data)

            response = client.post("/scan")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].startswith("/?message="))
            self.assertEqual(fake_worker.scans, 1)

            response = client.post("/scan/force")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].startswith("/?message="))
            self.assertEqual(fake_worker.force_scans, 1)

            response = client.get("/debug/candidates")
            self.assertEqual(response.status_code, 200)
            for label in ("高级工具", "候选队列", "文件路径", "已等待", "稳定状态", "预计处理", "原因", "文件状态"):
                self.assertIn(label.encode(), response.data)
            self.assertIn("Live Photo 文件对".encode(), response.data)
            self.assertIn("等待文件稳定".encode(), response.data)
            store.close()

    @unittest.skipIf(create_app is None, "Flask is not installed")
    def test_web_diagnostic_pair_page_runs_and_shows_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = test_settings(root)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            fake_worker = FakeWebWorker()
            log_handler = RecentLogHandler()
            calls: list[tuple[Path, Path]] = []

            def fake_diagnostic_runner(image_path: Path, video_path: Path) -> MotionPhoto2DiagnosticResult:
                calls.append((image_path, video_path))
                return MotionPhoto2DiagnosticResult(
                    success=False,
                    reason="MotionPhoto2 exited with code 7",
                    image_path=image_path,
                    video_path=video_path,
                    output_path=Path("/photos/live/2026/5/IMG_0056.heic"),
                    image_exists=True,
                    video_exists=True,
                    command=["motionphoto2", "--input-image", str(image_path), "--input-video", str(video_path)],
                    returncode=7,
                    stdout="diagnostic stdout",
                    stderr="diagnostic stderr",
                )

            app = create_app(  # type: ignore[misc]
                settings=settings,
                worker=fake_worker,  # type: ignore[arg-type]
                store=store,
                log_handler=log_handler,
                diagnostic_runner=fake_diagnostic_runner,
            )
            client = app.test_client()

            response = client.get("/debug/test-pair")
            self.assertEqual(response.status_code, 200)
            self.assertIn("测试指定文件对".encode(), response.data)
            self.assertIn("/photos/2026/5/IMG_0056.HEIC".encode(), response.data)
            self.assertIn("/photos/2026/5/IMG_0056.MOV".encode(), response.data)
            self.assertIn("成功时输出会沿用原图格式".encode(), response.data)
            self.assertNotIn("成功时输出到 /photos/live/年份/月份/文件名.jpg".encode(), response.data)

            response = client.post("/debug/test-pair", data={
                "image": "/photos/2026/5/IMG_0056.HEIC",
                "video": "/photos/2026/5/IMG_0056.MOV",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(calls, [(Path("/photos/2026/5/IMG_0056.HEIC"), Path("/photos/2026/5/IMG_0056.MOV"))])
            self.assertIn("测试失败".encode(), response.data)
            self.assertIn("MotionPhoto2 exited with code 7".encode(), response.data)
            self.assertIn("退出码".encode(), response.data)
            self.assertIn("标准输出".encode(), response.data)
            self.assertIn("错误输出".encode(), response.data)
            self.assertIn("diagnostic stdout".encode(), response.data)
            self.assertIn("diagnostic stderr".encode(), response.data)
            self.assertIn("/photos/live/2026/5/IMG_0056.heic".encode(), response.data)
            store.close()

    @unittest.skipIf(create_app is None, "Flask is not installed")
    def test_web_logs_stats_and_about_are_productized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = test_settings(root)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            fake_worker = FakeWebWorker()
            log_handler = RecentLogHandler()
            log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
            log_handler.emit(logging.LogRecord(
                name="livephoto_worker.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="\x1b[31m转换失败\x1b[0m",
                args=(),
                exc_info=None,
            ))
            pair = MediaPair("IMG_0001", root / "IMG_0001.HEIC", root / "IMG_0001.MOV")
            store.record_job(pair, pair_signature="sig-success", status="success", output_path=root / "out.heic", error=None)
            store.record_job(pair, pair_signature="sig-failed", status="failed", output_path=None, error="bad file")
            app = create_app(  # type: ignore[misc]
                settings=settings,
                worker=fake_worker,  # type: ignore[arg-type]
                store=store,
                log_handler=log_handler,
            )
            client = app.test_client()

            logs_response = client.get("/logs")
            self.assertEqual(logs_response.status_code, 200)
            self.assertIn("日志阅读".encode(), logs_response.data)
            self.assertIn("最近输出".encode(), logs_response.data)
            self.assertIn("ERROR".encode(), logs_response.data)
            self.assertIn("转换失败".encode(), logs_response.data)
            self.assertNotIn(b"\x1b[31m", logs_response.data)
            self.assertIn("清空日志".encode(), logs_response.data)

            clear_response = client.post("/logs/clear", follow_redirects=True)
            self.assertEqual(clear_response.status_code, 200)
            self.assertIn("日志已清空".encode(), clear_response.data)
            self.assertIn("暂无日志".encode(), clear_response.data)

            stats_response = client.get("/stats")
            self.assertEqual(stats_response.status_code, 200)
            for label in (
                "任务记录",
                "处理分类",
                "已合并 Live Photo",
                "已复制普通照片",
                "已复制普通视频",
                "已跳过",
                "失败",
                "任务明细",
            ):
                self.assertIn(label.encode(), stats_response.data)

            about_response = client.get("/about")
            self.assertEqual(about_response.status_code, 200)
            self.assertIn("LiveMotion".encode(), about_response.data)
            self.assertIn("版本".encode(), about_response.data)
            self.assertIn("https://github.com/dttxorg/livemotion".encode(), about_response.data)
            self.assertIn("核心能力".encode(), about_response.data)
            store.close()

    @unittest.skipIf(create_app is None, "Flask is not installed")
    def test_web_existing_page_routes_remain_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = test_settings(root)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            app = create_app(  # type: ignore[misc]
                settings=settings,
                worker=FakeWebWorker(),  # type: ignore[arg-type]
                store=store,
                log_handler=RecentLogHandler(),
            )
            client = app.test_client()

            for route in ("/", "/stats", "/logs", "/debug/candidates", "/debug/test-pair", "/about", "/settings"):
                with self.subTest(route=route):
                    self.assertEqual(client.get(route).status_code, 200)
            self.assertEqual(client.get("/healthz").get_json(), {"status": "ok"})
            self.assertEqual(client.get("/favicon.svg").mimetype, "image/svg+xml")
            store.close()


if __name__ == "__main__":
    unittest.main()
