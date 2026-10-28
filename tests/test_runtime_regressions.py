from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from livephoto_worker.db import ProcessingStore
from livephoto_worker.diagnostics import diagnostic_output_path, run_motionphoto2_diagnostic
from livephoto_worker.file_utils import media_staging_directory, publish_unique_file
from livephoto_worker.models import MediaItem, MediaPair
from livephoto_worker.motionphoto_paths import resolve_motionphoto_output
from livephoto_worker.processor import PairProcessor
from livephoto_worker.scanner import scan_media
from livephoto_worker.settings import Settings
from livephoto_worker.worker import LivePhotoWorker


def settings_for(root: Path, **overrides: object) -> Settings:
    values = dict(
        input_dir=root / "input",
        output_dir=root / "output",
        archive_dir=root / "archive",
        failed_dir=root / "failed",
        config_path=root / "config" / "config.json",
        db_path=root / "config" / "worker.sqlite3",
        move_originals=False,
        stable_seconds=0,
        poll_interval=0.01,
    )
    values.update(overrides)
    settings = Settings(**values)
    settings.ensure_directories()
    return settings


def write(path: Path, content: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def converter_result(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    output = Path(command[command.index("--output-file") + 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    write(output, b"new-motion-photo")
    return subprocess.CompletedProcess(command, 0, "", "")


class RuntimeRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.settings = settings_for(self.root)
        self.store = ProcessingStore(self.settings.db_path)
        self.addCleanup(self.store.close)
        self.processor = PairProcessor(self.settings, self.store)
        self.worker = LivePhotoWorker(self.settings, self.processor)

    def tearDown(self) -> None:
        logging.disable(self.previous_logging_disable)

    def pair(self) -> MediaPair:
        return MediaPair(
            "IMG",
            write(self.settings.input_dir / "IMG.JPG", b"image"),
            write(self.settings.input_dir / "IMG.MOV", b"video"),
        )

    def test_daemon_recovers_from_scan_exception(self) -> None:
        calls = 0

        def scan() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("temporary mount error")
            self.worker.stop()
            return 0

        with patch.object(self.worker, "scan_once", side_effect=scan):
            self.worker.run_forever()
        self.assertEqual(calls, 2)

    def test_unreadable_snapshot_does_not_skip_later_candidates(self) -> None:
        blocked = write(self.settings.input_dir / "a.JPG", b"blocked")
        write(self.settings.input_dir / "b.JPG", b"readable")
        original_snapshot = self.worker._snapshot

        def snapshot(kind, item):
            if isinstance(item, MediaItem) and item.path == blocked:
                raise PermissionError("unreadable candidate")
            return original_snapshot(kind, item)

        with patch.object(self.worker, "_snapshot", side_effect=snapshot):
            self.assertEqual(self.worker.force_scan_once(now=100), 1)
        self.assertTrue((self.settings.output_dir / "b.JPG").is_file())
        self.assertTrue(blocked.is_file())

    def test_unexpected_processing_error_does_not_skip_later_candidates(self) -> None:
        blocked = write(self.settings.input_dir / "a.JPG", b"blocked")
        write(self.settings.input_dir / "b.JPG", b"readable")
        original_process = self.processor.process_media

        def process(item):
            if item.path == blocked:
                raise RuntimeError("unexpected processor error")
            return original_process(item)

        with patch.object(self.processor, "process_media", side_effect=process):
            self.assertEqual(self.worker.force_scan_once(now=100), 2)
        self.assertTrue((self.settings.output_dir / "b.JPG").is_file())
        self.assertEqual(self.worker.scan_stats.failed, 1)

    def test_media_quarantine_error_keeps_original_failure_record(self) -> None:
        item = MediaItem(write(self.settings.input_dir / "solo.JPG"), "photo")
        with patch("livephoto_worker.processor.shutil.copy2", side_effect=OSError("copy failed")), patch(
            "livephoto_worker.processor.move_one_file", side_effect=PermissionError("quarantine denied")
        ):
            self.assertEqual(self.processor.process_media(item), "failed")
        job = self.store.latest_job()
        self.assertEqual(job["status"], "failed")
        self.assertIn("copy failed", job["error"])
        self.assertIn("quarantine denied", job["error"])

    def test_pair_quarantine_attempts_remaining_files_and_records_original_error(self) -> None:
        pair = self.pair()
        from livephoto_worker.file_utils import move_one_file

        def move(source, destination, marker=None):
            if source == pair.image_path:
                raise PermissionError("image quarantine denied")
            return move_one_file(source, destination, marker)

        with patch.object(self.processor, "_run_motionphoto2", side_effect=RuntimeError("converter failed")), patch(
            "livephoto_worker.processor.move_one_file", side_effect=move
        ):
            self.assertEqual(self.processor.process(pair), "failed")
        self.assertTrue((self.settings.failed_dir / "IMG.MOV").is_file())
        self.assertTrue(pair.image_path.is_file())
        self.assertIn("converter failed", self.store.latest_job()["error"])
        self.assertIn("image quarantine denied", self.store.latest_job()["error"])

    def test_resolver_rejects_stale_expected_and_alternate_outputs(self) -> None:
        expected = write(self.settings.output_dir / "IMG.jpg", b"old expected")
        alternate = write(expected.with_suffix(".jpeg"), b"old alternate")
        old = time.time() - 60
        for path in (expected, alternate):
            os.utime(path, (old, old))
        self.assertIsNone(resolve_motionphoto_output(expected, started_at=time.time()))
        expected.unlink()
        self.assertIsNone(resolve_motionphoto_output(expected, started_at=time.time()))

    def test_pair_no_output_does_not_reuse_existing_alternate(self) -> None:
        pair = self.pair()
        old_output = write(self.settings.output_dir / "IMG.jpeg", b"previous result")
        with patch.object(self.processor, "_run_motionphoto2"):
            self.assertEqual(self.processor.process(pair), "failed")
        self.assertEqual(old_output.read_bytes(), b"previous result")
        self.assertEqual(self.store.latest_job()["status"], "failed")
        self.assertFalse((self.settings.archive_dir / pair.image_path.name).exists())

    def test_pair_suffix_change_preserves_existing_output_and_publishes_new_file(self) -> None:
        pair = self.pair()
        old_output = write(self.settings.output_dir / "IMG.jpeg", b"previous result")

        def convert(_pair, output):
            write(output.with_suffix(".jpeg"), b"new result")

        with patch.object(self.processor, "_run_motionphoto2", side_effect=convert):
            self.assertEqual(self.processor.process(pair), "merged")
        self.assertEqual(old_output.read_bytes(), b"previous result")
        published = Path(self.store.latest_job()["output_path"])
        self.assertNotEqual(published, old_output)
        self.assertEqual(published.suffix, ".jpeg")
        self.assertEqual(published.read_bytes(), b"new result")
        self.assertEqual(list(self.settings.output_dir.glob(".livephoto-*")), [])

    def test_diagnostic_defaults_follow_configured_directories(self) -> None:
        result = run_motionphoto2_diagnostic(
            settings=self.settings,
            image_path=self.settings.input_dir / "album" / "missing.JPG",
            video_path=self.settings.input_dir / "album" / "missing.MOV",
        )
        self.assertEqual(result.output_path, self.settings.output_dir / "album" / "missing.jpg")

    def test_diagnostic_paths_stay_in_output_tree_after_dotdot_and_symlink_resolution(self) -> None:
        outside = write(self.root / "outside" / "outside.JPG", b"outside image")
        linked_input = self.settings.input_dir / "linked"
        linked_input.symlink_to(outside.parent, target_is_directory=True)
        normal_image = write(self.settings.input_dir / "album" / "normal.JPG", b"normal image")
        (self.settings.output_dir / "album").symlink_to(outside.parent, target_is_directory=True)
        (self.settings.output_dir / "outside.jpg").symlink_to(outside)
        cases = (
            self.settings.input_dir / ".." / "outside" / outside.name,
            linked_input / outside.name,
            normal_image,
        )
        for image in cases:
            with self.subTest(image=image):
                output = diagnostic_output_path(
                    image,
                    output_root=self.settings.output_dir,
                    photos_root=self.settings.input_dir,
                )
                self.assertTrue(output.resolve().is_relative_to(self.settings.output_dir.resolve()))
                self.assertEqual(output.parent, self.settings.output_dir)
                self.assertNotIn("..", output.parts)
        self.assertEqual(outside.read_bytes(), b"outside image")

    def test_diagnostic_no_output_does_not_reuse_or_touch_existing_outputs(self) -> None:
        pair = self.pair()
        expected = write(self.settings.output_dir / "IMG.jpg", b"old expected")
        alternate = write(self.settings.output_dir / "IMG.jpeg", b"old alternate")
        with patch("livephoto_worker.diagnostics.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = run_motionphoto2_diagnostic(
                settings=self.settings,
                image_path=pair.image_path,
                video_path=pair.video_path,
                output_root=self.settings.output_dir,
                photos_root=self.settings.input_dir,
            )
        self.assertFalse(result.success)
        self.assertEqual(expected.read_bytes(), b"old expected")
        self.assertEqual(alternate.read_bytes(), b"old alternate")
        self.assertTrue(pair.image_path.is_file())
        self.assertTrue(pair.video_path.is_file())

    def test_diagnostic_suffix_change_preserves_existing_output(self) -> None:
        pair = self.pair()
        old_output = write(self.settings.output_dir / "IMG.jpeg", b"old result")

        def convert(command, **_kwargs):
            staged_output = Path(command[command.index("--output-file") + 1])
            self.assertNotEqual(staged_output.parent, self.settings.output_dir)
            write(staged_output.with_suffix(".jpeg"), b"new result")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("livephoto_worker.diagnostics.subprocess.run", side_effect=convert):
            result = run_motionphoto2_diagnostic(
                settings=self.settings,
                image_path=pair.image_path,
                video_path=pair.video_path,
                output_root=self.settings.output_dir,
                photos_root=self.settings.input_dir,
            )
        self.assertTrue(result.success)
        self.assertNotEqual(result.output_path, old_output)
        self.assertEqual(old_output.read_bytes(), b"old result")
        self.assertEqual(result.output_path.read_bytes(), b"new result")
        self.assertEqual(list(self.settings.output_dir.glob(".livephoto-*")), [])

    def test_deleted_media_output_is_restored_without_source_stat_change(self) -> None:
        source = write(self.settings.input_dir / "solo.JPG", b"source photo")
        self.assertEqual(self.worker.force_scan_once(now=100), 1)
        original_state = source.stat()
        Path(self.store.latest_job()["output_path"]).unlink()
        self.assertEqual(self.worker.force_scan_once(now=101), 1)
        restored = Path(self.store.latest_job()["output_path"])
        self.assertEqual(restored.read_bytes(), b"source photo")
        self.assertEqual(source.stat().st_mtime_ns, original_state.st_mtime_ns)
        self.assertEqual(self.worker.force_scan_once(now=102), 0)
        self.assertEqual(len(self.store.recent_jobs()), 2)

    def test_deleted_pair_output_is_regenerated_and_store_path_updated(self) -> None:
        pair = self.pair()
        with patch.object(self.processor, "_run_motionphoto2", side_effect=lambda pair, output: write(output, b"new-motion-photo")) as run:
            self.assertEqual(self.processor.process(pair), "merged")
            old_output = Path(self.store.latest_job()["output_path"])
            old_output.unlink()
            write(old_output, b"collision")
            with self.store.lock, self.store.conn:
                self.store.conn.execute("UPDATE processed_pairs SET output_path = ?", (str(old_output.with_name("deleted.jpg")),))
            self.assertEqual(self.processor.process(pair), "merged")
            self.assertEqual(self.processor.process(pair), "skipped")
            self.assertEqual(run.call_count, 2)
        self.assertEqual(old_output.read_bytes(), b"collision")
        with self.store.lock:
            recorded = self.store.conn.execute("SELECT output_path FROM processed_pairs").fetchone()[0]
        self.assertTrue(Path(recorded).is_file())
        self.assertNotEqual(recorded, str(old_output))

    def test_completed_cache_drops_disappeared_sources(self) -> None:
        source = write(self.settings.input_dir / "solo.JPG")
        self.worker.force_scan_once(now=100)
        self.assertTrue(self.worker.completed_states)
        source.unlink()
        self.worker.force_scan_once(now=101)
        self.assertEqual(self.worker.completed_states, {})

    def test_broken_symlink_is_occupied_during_publication(self) -> None:
        staged = write(self.root / "staged" / "result.JPG", b"complete output")
        existing = self.settings.output_dir / "result.JPG"
        existing.symlink_to(self.root / "missing-source.JPG")
        published = publish_unique_file(staged, self.settings.output_dir)
        self.assertNotEqual(published, existing)
        self.assertTrue(existing.is_symlink())
        self.assertEqual(published.read_bytes(), b"complete output")

    def test_cross_filesystem_publication_preserves_existing_targets(self) -> None:
        staged = write(self.root / "staged" / "result.JPG", b"complete output")
        old_output = write(self.settings.output_dir / "result.JPG", b"old output")
        with patch("livephoto_worker.file_utils.os.link", side_effect=OSError("cross-device link")):
            published = publish_unique_file(staged, self.settings.output_dir)
        self.assertNotEqual(published, old_output)
        self.assertEqual(old_output.read_bytes(), b"old output")
        self.assertEqual(published.read_bytes(), b"complete output")

    def test_failed_publication_cleans_only_its_own_target(self) -> None:
        staged = write(self.root / "staged" / "result.JPG", b"complete output")
        old_output = write(self.settings.output_dir / "result.JPG", b"old output")
        with patch("livephoto_worker.file_utils.os.link", side_effect=OSError("cross-device link")), patch(
            "livephoto_worker.file_utils.shutil.copystat", side_effect=PermissionError("metadata denied")
        ):
            with self.assertRaisesRegex(PermissionError, "metadata denied"):
                publish_unique_file(staged, self.settings.output_dir)
        self.assertEqual(old_output.read_bytes(), b"old output")
        self.assertEqual(list(self.settings.output_dir.iterdir()), [old_output])

    def test_partial_media_copy_never_remains_in_output(self) -> None:
        item = MediaItem(write(self.settings.input_dir / "solo.JPG", b"source photo"), "photo")

        def partial_copy(_source, destination):
            self.assertNotIn(self.settings.output_dir, destination.parents)
            self.assertNotIn(self.settings.input_dir, destination.parents)
            write(destination, b"half")
            raise OSError("interrupted copy")

        with patch("livephoto_worker.processor.shutil.copy2", side_effect=partial_copy):
            self.assertEqual(self.processor.process_media(item), "failed")
        self.assertEqual(list(self.settings.output_dir.iterdir()), [])
        self.assertIn("interrupted copy", self.store.latest_job()["error"])
        work_root = self.settings.db_path.parent / ".livephoto-work"
        self.assertEqual(list(work_root.iterdir()), [])

    def test_staging_falls_back_if_config_directory_is_inside_media_tree(self) -> None:
        with media_staging_directory(
            self.settings.output_dir / "config",
            media_roots=(self.settings.input_dir, self.settings.output_dir),
        ) as staging:
            self.assertNotIn(self.settings.output_dir, Path(staging).parents)
            self.assertNotIn(self.settings.input_dir, Path(staging).parents)

    def test_staging_reports_when_no_tree_exterior_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the configured media roots"):
            media_staging_directory(self.root, media_roots=(Path("/"),))

    def test_poll_wait_is_bounded_for_large_finite_interval(self) -> None:
        self.settings.poll_interval = 1e308
        waits = []

        def wait(interval):
            waits.append(interval)
            self.worker.stop_event.set()
            return True

        with patch.object(self.worker, "scan_once", return_value=0), patch.object(
            self.worker.stop_event, "wait", side_effect=wait
        ):
            self.worker.run_forever()
        self.assertEqual(waits, [threading.TIMEOUT_MAX])

    @unittest.skipUnless(os.name == "posix", "process group termination requires POSIX")
    def test_shutdown_kills_active_conversion_and_retains_originals(self) -> None:
        self.settings.move_originals = True
        pair = self.pair()
        ready = self.root / "converter-ready"
        script = write(
            self.root / "slow_converter.py",
            (
                "import signal, time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"Path({str(ready)!r}).write_text('ready')\n"
                "time.sleep(60)\n"
            ).encode(),
        )
        self.settings.motionphoto2_bin = ""
        self.settings.motionphoto2_python = sys.executable
        self.settings.motionphoto2_script = script
        results = []
        thread = threading.Thread(target=lambda: results.append(self.processor.process(pair)))
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=5))
        self.addCleanup(self.processor.stop)
        deadline = time.monotonic() + 5
        while not ready.exists() and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), "converter did not reach its running state")
        process = self.processor._active_process
        self.assertIsNotNone(process)
        self.assertEqual(os.getpgid(process.pid), process.pid)
        self.worker.stop()
        self.worker.stop()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["cancelled"])
        self.assertIsNotNone(process.poll())
        self.assertTrue(pair.image_path.is_file())
        self.assertTrue(pair.video_path.is_file())
        self.assertEqual(list(self.settings.output_dir.iterdir()), [])
        self.assertEqual(list(self.settings.failed_dir.iterdir()), [])
        self.assertEqual(list(self.settings.archive_dir.iterdir()), [])
        self.assertIsNone(self.store.latest_job())

    def test_main_closes_store_only_after_worker_join(self) -> None:
        from livephoto_worker import __main__ as entrypoint
        from unittest.mock import Mock

        order = []
        fake_store = Mock()
        fake_store.close.side_effect = lambda: order.append("close")
        fake_worker = Mock()
        fake_worker.stop.side_effect = lambda: order.append("stop")
        fake_thread = Mock()
        fake_thread.join.side_effect = lambda: order.append("join")
        waitress = types.SimpleNamespace(serve=lambda *_args, **_kwargs: None)
        with patch.object(entrypoint.Settings, "load", return_value=self.settings), patch.object(
            entrypoint, "configure_logging"
        ), patch.object(entrypoint, "check_motionphoto2_available"), patch.object(
            entrypoint, "ProcessingStore", return_value=fake_store
        ), patch.object(entrypoint, "PairProcessor"), patch.object(
            entrypoint, "LivePhotoWorker", return_value=fake_worker
        ), patch.object(entrypoint.threading, "Thread", return_value=fake_thread), patch.object(
            entrypoint, "create_app"
        ), patch.object(entrypoint.signal, "signal"), patch.dict(sys.modules, {"waitress": waitress}):
            self.assertEqual(entrypoint.main(), 0)
        fake_thread.join.assert_called_once_with()
        self.assertEqual(order, ["stop", "join", "close"])

    def test_nonselected_same_stem_files_are_kept_as_ordinary_media(self) -> None:
        for filename in ("IMG.heic", "IMG.jpg", "IMG.mov", "IMG.mp4"):
            write(self.settings.input_dir / filename, filename.encode())
        scan = scan_media(self.settings.input_dir)
        self.assertEqual(len(scan.pairs), 1)
        self.assertEqual(scan.pairs[0].image_path.name, "IMG.heic")
        self.assertEqual(scan.pairs[0].video_path.name, "IMG.mov")
        self.assertEqual({item.path.name for item in scan.media_items}, {"IMG.jpg", "IMG.mp4"})


if __name__ == "__main__":
    unittest.main()
