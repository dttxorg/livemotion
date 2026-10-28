from __future__ import annotations

import json
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from livephoto_worker.db import ProcessingStore
from livephoto_worker.logging_buffer import RecentLogHandler
from livephoto_worker.settings import Settings
from livephoto_worker.web import _format_timestamp, create_app


def make_settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "input_dir": root / "input",
        "output_dir": root / "output",
        "archive_dir": root / "archive",
        "failed_dir": root / "failed",
        "config_path": root / "config" / "config.json",
        "db_path": root / "config" / "jobs.sqlite3",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class FormParser(HTMLParser):
    def __init__(self, document: str):
        super().__init__()
        self.inputs: dict[str, dict[str, str | None]] = {}
        self.feed(document)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name"):
            self.inputs[str(attributes["name"])] = attributes


class WebWorker:
    def __init__(self) -> None:
        self.scan_lock = threading.RLock()
        self.cleared = False
        self.pending_calls = 0
        self.candidate_calls = 0

    def clear_pending(self) -> None:
        with self.scan_lock:
            self.cleared = True

    def scan_once(self) -> int:
        return 2

    def force_scan_once(self) -> int:
        return 3

    def pending_status(self) -> dict[str, float | int | None]:
        self.pending_calls += 1
        with self.scan_lock:
            return {
                "waiting_count": 0,
                "waiting_live_pairs": 0,
                "earliest_first_seen_at": None,
                "next_process_at": None,
                "oldest_wait_seconds": 0,
            }

    def candidate_debug_rows(self) -> list[dict[str, object]]:
        self.candidate_calls += 1
        with self.scan_lock:
            return []


class SettingsValidationRegressions(unittest.TestCase):
    def test_invalid_numeric_updates_leave_all_fields_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            original = settings.to_config_dict()
            for field, value in (
                ("stable_seconds", "bad-number"),
                ("stable_seconds", -1),
                ("stable_seconds", "nan"),
                ("stable_seconds", "inf"),
                ("stable_seconds", "1e9999"),
                ("stable_seconds", 10**1000),
                ("poll_interval", ""),
                ("poll_interval", 0),
                ("poll_interval", -0.1),
                ("poll_interval", "-inf"),
                ("poll_interval", "NaN"),
            ):
                with self.subTest(field=field, value=str(value)[:30]):
                    with self.assertRaises(ValueError):
                        settings.update_from_config(
                            {"input_dir": str(Path(tmp) / "changed"), field: value}, save=False
                        )
                    self.assertEqual(settings.to_config_dict(), original)

    def test_zero_stability_and_positive_subsecond_polling_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.update_from_config({"stable_seconds": "0", "poll_interval": "0.1"}, save=False)
            self.assertEqual(settings.stable_seconds, 0)
            self.assertEqual(settings.poll_interval, 0.1)

    def test_constructor_and_config_file_reject_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                make_settings(root, poll_interval=float("nan"))
            config_path = root / "invalid.json"
            config_path.write_text(json.dumps({"stable_seconds": -1}))
            with self.assertRaises(ValueError):
                Settings.load(config_path=config_path)

    def test_empty_relative_and_non_directory_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            original = settings.to_config_dict()
            file_path = root / "a-file"
            file_path.write_text("not a directory")
            for field in ("input_dir", "output_dir", "archive_dir", "failed_dir"):
                for value in ("", " \t", "relative/path", None, str(file_path), "/bad\x00path"):
                    with self.subTest(field=field, value=value):
                        with self.assertRaises(ValueError):
                            settings.update_from_config({field: value}, save=False)
                        self.assertEqual(settings.to_config_dict(), original)

    def test_equal_paths_and_destination_ancestors_of_input_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            original = settings.to_config_dict()
            for change in (
                {"output_dir": str(settings.input_dir)},
                {"archive_dir": str(settings.input_dir)},
                {"failed_dir": str(settings.input_dir)},
                {"output_dir": str(root)},
                {"archive_dir": str(root)},
                {"failed_dir": str(root)},
                {"archive_dir": str(settings.output_dir)},
                {"failed_dir": str(settings.output_dir / "failures")},
                {"output_dir": str(settings.archive_dir / "output")},
            ):
                with self.subTest(change=change):
                    with self.assertRaises(ValueError):
                        settings.update_from_config(change, save=False)
                    self.assertEqual(settings.to_config_dict(), original)

    def test_destinations_nested_under_input_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.update_from_config({
                "output_dir": str(settings.input_dir / "output"),
                "archive_dir": str(settings.input_dir / "archive"),
                "failed_dir": str(settings.input_dir / "failed"),
            }, save=False)
            self.assertEqual(settings.output_dir, root / "input" / "output")

    def test_symlink_aliases_are_checked_for_directory_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.input_dir.mkdir()
            alias = root / "input-alias"
            alias.symlink_to(settings.input_dir, target_is_directory=True)
            with self.assertRaises(ValueError):
                settings.update_from_config({"output_dir": str(alias)}, save=False)


class SettingsPersistenceRegressions(unittest.TestCase):
    def test_directory_creation_failure_preserves_shared_settings_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.save_config()
            old_settings = settings.to_config_dict()
            old_file = settings.config_path.read_bytes()
            blocked = root / "blocked"
            blocked.write_text("file blocks directory creation")
            with self.assertRaises(OSError):
                settings.update_from_config({"output_dir": str(blocked / "output"), "stable_seconds": 9})
            self.assertEqual(settings.to_config_dict(), old_settings)
            self.assertEqual(settings.config_path.read_bytes(), old_file)

    def test_atomic_replace_failure_preserves_settings_file_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.save_config()
            old_settings = settings.to_config_dict()
            old_file = settings.config_path.read_bytes()
            with patch("livephoto_worker.settings.os.replace", side_effect=PermissionError("read-only fixture")):
                with self.assertRaises(OSError):
                    settings.update_from_config({"stable_seconds": 9})
            self.assertEqual(settings.to_config_dict(), old_settings)
            self.assertEqual(settings.config_path.read_bytes(), old_file)
            self.assertEqual(list(settings.config_path.parent.iterdir()), [settings.config_path])

    def test_file_is_replaced_only_after_complete_json_is_written_and_synced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.save_config()
            old_file = settings.config_path.read_bytes()
            import os

            original_replace = os.replace

            def check_replace(source: str | Path, destination: str | Path) -> None:
                self.assertEqual(Path(destination), settings.config_path)
                self.assertEqual(settings.config_path.read_bytes(), old_file)
                self.assertEqual(json.loads(Path(source).read_text())["stable_seconds"], 11)
                self.assertEqual(settings.stable_seconds, 30)
                original_replace(source, destination)

            with patch("livephoto_worker.settings.os.replace", side_effect=check_replace) as replace_call:
                with patch("livephoto_worker.settings.os.fsync", wraps=os.fsync) as fsync_call:
                    settings.update_from_config({"stable_seconds": 11})
            replace_call.assert_called_once()
            fsync_call.assert_called_once()
            self.assertEqual(settings.stable_seconds, 11)
            self.assertEqual(json.loads(settings.config_path.read_text())["stable_seconds"], 11)

    def test_sync_failure_keeps_old_file_and_cleans_up_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.save_config()
            old_settings = settings.to_config_dict()
            old_file = settings.config_path.read_bytes()
            with patch("livephoto_worker.settings.os.fsync", side_effect=OSError("disk-full fixture")):
                with self.assertRaises(OSError):
                    settings.update_from_config({"stable_seconds": 17})
            self.assertEqual(settings.to_config_dict(), old_settings)
            self.assertEqual(settings.config_path.read_bytes(), old_file)
            self.assertEqual(list(settings.config_path.parent.iterdir()), [settings.config_path])


class SettingsWebRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = make_settings(self.root)
        self.settings.ensure_directories()
        self.settings.save_config()
        self.store = ProcessingStore(self.settings.db_path)
        self.addCleanup(self.store.close)
        self.worker = WebWorker()
        self.app = create_app(
            settings=self.settings,
            worker=self.worker,  # type: ignore[arg-type]
            store=self.store,
            log_handler=RecentLogHandler(),
        )
        self.app.testing = True
        self.client = self.app.test_client()

    def form(self, **overrides: str | None) -> dict[str, str]:
        data = {
            "input_dir": str(self.root / "new-input"),
            "output_dir": str(self.root / "new-output"),
            "archive_dir": str(self.root / "new-archive"),
            "failed_dir": str(self.root / "new-failed"),
            "stable_seconds": "1.5",
            "poll_interval": "0.1",
            "move_originals": "1",
            "recursive_scan": "1",
            "skip_dir_names": "custom-one,custom-two\ncustom-three",
        }
        for key, value in overrides.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        return data

    def test_invalid_post_returns_field_error_preserves_values_and_does_not_mutate(self) -> None:
        original = self.settings.to_config_dict()
        old_file = self.settings.config_path.read_bytes()
        for field, value in (
            ("stable_seconds", "oops"),
            ("stable_seconds", "nan"),
            ("stable_seconds", "1e10000"),
            ("stable_seconds", "-1"),
            ("poll_interval", "0"),
            ("poll_interval", "-inf"),
            ("input_dir", ""),
            ("output_dir", "relative/path"),
        ):
            with self.subTest(field=field, value=value):
                data = self.form(**{field: value})
                response = self.client.post("/save", data=data)
                self.assertEqual(response.status_code, 400)
                html = response.get_data(as_text=True)
                parsed = FormParser(html)
                self.assertEqual(parsed.inputs[field]["value"], value)
                self.assertEqual(parsed.inputs[field]["aria-invalid"], "true")
                self.assertIn(f'id="{field}-error"', html)
                self.assertIn(data["skip_dir_names"], html)
                self.assertIn("checked", parsed.inputs["move_originals"])
                self.assertNotIn("checked", parsed.inputs["enable_archive"])
                if field in {"stable_seconds", "poll_interval"}:
                    self.assertEqual(parsed.inputs[field]["type"], "text")
                self.assertEqual(self.settings.to_config_dict(), original)
                self.assertEqual(self.settings.config_path.read_bytes(), old_file)
                self.assertFalse(self.worker.cleared)

    def test_storage_error_is_readable_and_preserves_submitted_values(self) -> None:
        original = self.settings.to_config_dict()
        old_file = self.settings.config_path.read_bytes()
        with patch("livephoto_worker.settings.os.replace", side_effect=PermissionError("read-only fixture")):
            response = self.client.post("/save", data=self.form())
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn("配置保存失败", html)
        self.assertIn("read-only fixture", html)
        self.assertEqual(FormParser(html).inputs["stable_seconds"]["value"], "1.5")
        self.assertEqual(self.settings.to_config_dict(), original)
        self.assertEqual(self.settings.config_path.read_bytes(), old_file)
        self.assertFalse(self.worker.cleared)

    def test_successful_save_keeps_redirect_and_clears_queue(self) -> None:
        response = self.client.post("/save", data=self.form())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("/settings?message="))
        self.assertTrue(self.worker.cleared)
        self.assertEqual(self.settings.poll_interval, 0.1)
        self.assertEqual(self.settings.skip_dir_names, ["custom-one", "custom-two", "custom-three"])
        saved = json.loads(self.settings.config_path.read_text())
        self.assertEqual(saved["input_dir"], str(self.root / "new-input"))
        self.assertEqual(self.client.get(response.headers["Location"]).status_code, 200)

    def test_save_waits_for_scan_before_modifying_any_settings(self) -> None:
        started = threading.Event()
        finished = threading.Event()
        results: list[int] = []
        failures: list[BaseException] = []
        original = self.settings.to_config_dict()

        def save() -> None:
            try:
                with self.app.test_client() as client:
                    started.set()
                    results.append(client.post("/save", data=self.form()).status_code)
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        with self.worker.scan_lock:
            thread = threading.Thread(target=save, daemon=True)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
            self.assertEqual(self.settings.to_config_dict(), original)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        self.assertEqual(results, [302])
        self.assertTrue(self.worker.cleared)

    def test_pages_only_read_worker_state_when_they_display_it(self) -> None:
        for route in ("/settings", "/logs", "/stats", "/about", "/debug/test-pair"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)
        self.assertEqual(self.worker.pending_calls, 0)
        self.assertEqual(self.worker.candidate_calls, 0)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.worker.pending_calls, 1)
        self.assertEqual(self.worker.candidate_calls, 0)
        self.assertEqual(self.client.get("/debug/candidates").status_code, 200)
        self.assertEqual(self.worker.candidate_calls, 1)

    def test_large_valid_wait_time_does_not_break_timestamp_display(self) -> None:
        self.assertIsInstance(_format_timestamp(1e308), str)
        self.assertIsInstance(_format_timestamp(float("inf")), str)

    def test_diagnostic_paths_start_empty_and_empty_or_relative_posts_do_not_run(self) -> None:
        response = self.client.get("/debug/test-pair")
        inputs = FormParser(response.get_data(as_text=True)).inputs
        for field in ("image", "video"):
            self.assertEqual(inputs[field]["value"], "")
            self.assertTrue(inputs[field]["placeholder"])
            self.assertIn("required", inputs[field])
        with patch("livephoto_worker.web.run_motionphoto2_diagnostic") as runner:
            for data in ({}, {"image": " ", "video": ""}, {"image": "relative.HEIC", "video": "/tmp/pair.MOV"}):
                with self.subTest(data=data):
                    response = self.client.post("/debug/test-pair", data=data)
                    self.assertEqual(response.status_code, 400)
                    html = response.get_data(as_text=True)
                    self.assertIn("诊断尚未运行", html)
                    self.assertIn('aria-invalid="true"', html)
                    self.assertEqual(FormParser(html).inputs["video"]["value"], data.get("video", ""))
            runner.assert_not_called()

    def test_favicon_compatibility_routes_use_the_new_icon(self) -> None:
        static_root = Path(self.app.static_folder)
        with self.client.get("/favicon.svg") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/svg+xml")
            self.assertEqual(response.data, (static_root / "livemotion-icon.svg").read_bytes())
        for route in ("/favicon.png", "/favicon.ico"):
            with self.subTest(route=route), self.client.get(route) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/png")
                self.assertEqual(response.data, (static_root / "livemotion-icon-64.png").read_bytes())

    def test_scan_messages_count_candidates_instead_of_photo_pairs(self) -> None:
        for route, count in (("/scan", 2), ("/scan/force", 3)):
            with self.subTest(route=route):
                response = self.client.post(route, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f"本轮处理 {count} 个候选", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
