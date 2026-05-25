from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from livephoto_worker.db import ProcessingStore
from livephoto_worker.file_utils import hash_pair
from livephoto_worker.models import MediaPair
from livephoto_worker.processor import PairProcessor
from livephoto_worker.scanner import scan_pairs
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

    def clear_pending(self) -> None:
        self.cleared = True

    def scan_once(self) -> int:
        self.scans += 1
        return 7


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
            })

            saved = json.loads(config_path.read_text())
            self.assertEqual(saved["input_dir"], str(root / "new-inbox"))
            self.assertEqual(saved["stable_seconds"], 5)
            self.assertTrue(saved["move_originals"])

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

            self.assertTrue((output_dir / "IMG_0001.HEIC").is_file())
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

            self.assertTrue((output_dir / "IMG_0001.HEIC").is_file())
            self.assertTrue((input_dir / "IMG_0001.HEIC").is_file())
            self.assertTrue((input_dir / "IMG_0001.MOV").is_file())
            self.assertFalse((archive_dir / "IMG_0001.HEIC").exists())
            latest = store.latest_job()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["status"], "success")
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

    @unittest.skipIf(create_app is None, "Flask is not installed")
    def test_web_page_saves_config_and_triggers_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = test_settings(root)
            settings.ensure_directories()
            store = ProcessingStore(settings.db_path)
            fake_worker = FakeWebWorker()
            log_handler = RecentLogHandler()
            app = create_app(  # type: ignore[misc]
                settings=settings,
                worker=fake_worker,  # type: ignore[arg-type]
                store=store,
                log_handler=log_handler,
            )
            client = app.test_client()

            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("livephoto-worker 设置".encode(), response.data)

            response = client.post("/save", data={
                "input_dir": str(root / "photos" / "in"),
                "output_dir": str(root / "photos" / "out"),
                "archive_dir": str(root / "photos" / "archive"),
                "failed_dir": str(root / "photos" / "failed"),
                "stable_seconds": "9",
                "poll_interval": "4",
                "move_originals": "1",
            })
            self.assertEqual(response.status_code, 302)
            saved = json.loads(settings.config_path.read_text())
            self.assertEqual(saved["input_dir"], str(root / "photos" / "in"))
            self.assertEqual(saved["stable_seconds"], 9.0)
            self.assertTrue(saved["move_originals"])
            self.assertFalse(saved["enable_archive"])
            self.assertTrue(fake_worker.cleared)

            response = client.post("/scan")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(fake_worker.scans, 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
