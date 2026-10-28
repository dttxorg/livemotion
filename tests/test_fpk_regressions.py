from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ROOT / "fpk" / "cmd"


class ScriptFixtures(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="livemotion-script-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = {
            **os.environ,
            "TRIM_APPNAME": "livemotion",
            "TRIM_PKGVAR": "/vol1/livemotion-regression-fixture",
            "TRIM_TEMP_LOGFILE": str(self.root / "installer log.txt"),
            "TRIM_TEMP_UPGRADE_FOLDER": str(self.root),
            "TRACE": str(self.root / "trace.txt"),
        }

    def run_shell(self, body: str, *, common: bool = False) -> subprocess.CompletedProcess[str]:
        if common:
            # Isolate the fnOS data-directory side effect, not hook/status code.
            body = (
                "mkdir() { return 0; }; "
                f". {shlex.quote(str(COMMANDS / 'common'))} || exit $?; "
                "unset -f mkdir; " + body
            )
        return subprocess.run(
            ["bash", "-c", body], cwd=ROOT, env=self.environment,
            text=True, capture_output=True, timeout=20,
        )

    def setup_body(self, body: str) -> str:
        return (
            f". {shlex.quote(str(COMMANDS / 'service-setup'))}; "
            'install_log() { printf "%s\\n" "$*" >&2; }; '
            f"PHOTOS_DIR={shlex.quote(str(self.root / 'photos'))}; "
            f"CONFIG_DIR={shlex.quote(str(self.root / 'config'))}; "
            'DEFAULT_CONFIG="$CONFIG_DIR/config.json"; ' + body
        )


class LifecycleFailureRegressions(ScriptFixtures):
    def test_logged_hook_retains_nonzero_status(self) -> None:
        result = self.run_shell(
            'failed_hook() { echo "hook output"; return 17; }; '
            'call_func failed_hook install_log', common=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertIn("hook output", result.stderr)
        self.assertIn("status=17", result.stderr)

    def test_hook_exit_in_pipeline_retains_nonzero_status(self) -> None:
        result = self.run_shell(
            'failed_hook() { exit 19; }; call_func failed_hook install_log', common=True,
        )
        self.assertEqual(result.returncode, 19, result.stderr)

    def test_logger_failure_is_not_reported_as_success(self) -> None:
        result = self.run_shell(
            'good_hook() { echo "output"; }; '
            'failed_logger() { cat >/dev/null; return 23; }; '
            'call_func good_hook failed_logger', common=True,
        )
        self.assertEqual(result.returncode, 23, result.stderr)

    def test_unlogged_hook_retains_nonzero_status(self) -> None:
        result = self.run_shell(
            'failed_hook() { return 17; }; call_func failed_hook', common=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_absent_optional_hook_is_successful(self) -> None:
        result = self.run_shell('call_func absent_fixture_hook install_log', common=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lifecycle_returns_hook_failures(self) -> None:
        hooks = {
            "install_init": "service_preinst",
            "install_callback": "service_postinst",
            "uninstall_init": "service_preuninst",
            "uninstall_callback": "service_postuninst",
            "upgrade_init": "service_save",
            "upgrade_callback": "service_postupgrade",
            "config_init": "service_preconfig",
            "config_callback": "service_postconfig",
        }
        for lifecycle, hook in hooks.items():
            with self.subTest(lifecycle=lifecycle):
                result = self.run_shell(
                    f"{hook}() {{ return 17; }}; {lifecycle}", common=True,
                )
                self.assertEqual(result.returncode, 17, result.stderr)

    def test_failed_precheck_prevents_later_install_hooks(self) -> None:
        result = self.run_shell(
            'validate_preinst() { return 17; }; '
            'service_preinst() { echo called > "$TRACE"; }; install_init', common=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertFalse(Path(self.environment["TRACE"]).exists())

    def test_failed_upgrade_precheck_does_not_stop_or_save_service(self) -> None:
        result = self.run_shell(
            'validate_preupgrade() { return 17; }; '
            'stop_daemon() { echo stopped > "$TRACE"; }; '
            'service_save() { echo saved > "$TRACE"; }; upgrade_init', common=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertFalse(Path(self.environment["TRACE"]).exists())

    def test_failed_restore_prevents_upgrade_posthook(self) -> None:
        result = self.run_shell(
            'service_restore() { return 17; }; '
            'service_postupgrade() { echo called > "$TRACE"; }; upgrade_callback', common=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertFalse(Path(self.environment["TRACE"]).exists())

    def test_common_directory_failure_is_visible(self) -> None:
        result = self.run_shell(
            'mkdir() { return 23; }; '
            f". {shlex.quote(str(COMMANDS / 'common'))}"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("创建应用数据目录失败", result.stderr)

    def copied_wrapper(self, extra_hooks: str) -> Path:
        commands = self.root / "commands with spaces"
        shutil.copytree(COMMANDS, commands)
        with (commands / "service-setup").open("a") as target:
            target.write("\n" + extra_hooks + "\n")
        binary = self.root / "bin"
        binary.mkdir()
        mkdir = binary / "mkdir"
        mkdir.write_text("#!/bin/sh\nexit 0\n")
        mkdir.chmod(0o755)
        self.environment["PATH"] = str(binary) + os.pathsep + os.environ["PATH"]
        return commands / "install_callback"

    def test_wrapper_with_spaces_propagates_hook_status(self) -> None:
        wrapper = self.copied_wrapper('service_postinst() { return 17; }')
        result = subprocess.run(
            ["bash", str(wrapper)], env=self.environment,
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertTrue(Path(self.environment["TRIM_TEMP_LOGFILE"]).exists())

    def test_initialization_failure_stops_wrapper(self) -> None:
        wrapper = self.copied_wrapper(
            'initialize_variables() { return 19; }\n'
            'service_postinst() { echo called > "$TRACE"; }'
        )
        result = subprocess.run(
            ["bash", str(wrapper)], env=self.environment,
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertFalse(Path(self.environment["TRACE"]).exists())


class ServiceStatusRegressions(ScriptFixtures):
    def setUp(self) -> None:
        super().setUp()
        self.destination = self.root / "app destination"
        (self.destination / "docker").mkdir(parents=True)
        self.compose = self.destination / "docker" / "docker-compose.yaml"
        self.environment["TRIM_APPDEST"] = str(self.destination)
        self.environment["MOCK_DOCKER_LOG"] = str(self.root / "docker-calls.txt")
        binary = self.root / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$MOCK_DOCKER_LOG"\n'
            'printf "%s\\n" "${MOCK_DOCKER_RUNNING:-false}"\n'
            'exit "${MOCK_DOCKER_RC:-0}"\n'
        )
        docker.chmod(0o755)
        self.environment["PATH"] = str(binary) + os.pathsep + os.environ["PATH"]

    def status(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(COMMANDS / "main"), "status"], env=self.environment,
            text=True, capture_output=True, timeout=20,
        )

    def test_missing_compose_returns_not_running(self) -> None:
        self.assertEqual(self.status().returncode, 3)
        self.assertFalse(Path(self.environment["MOCK_DOCKER_LOG"]).exists())

    def test_missing_or_empty_container_name_returns_not_running(self) -> None:
        for content in ("services:\n  worker:\n", "container_name:\n"):
            with self.subTest(content=content):
                self.compose.write_text(content)
                self.environment["MOCK_DOCKER_RUNNING"] = "true"
                self.assertEqual(self.status().returncode, 3)
        self.assertFalse(Path(self.environment["MOCK_DOCKER_LOG"]).exists())

    def test_stopped_missing_container_and_daemon_failures_return_three(self) -> None:
        self.compose.write_text("  container_name: fixture-worker\n")
        for running, status in (("false", "0"), ("", "1"), ("true", "125")):
            with self.subTest(running=running, docker_status=status):
                self.environment.update(MOCK_DOCKER_RUNNING=running, MOCK_DOCKER_RC=status)
                self.assertEqual(self.status().returncode, 3)

    def test_only_confirmed_running_container_returns_zero(self) -> None:
        self.compose.write_text("  container_name: fixture-worker\n")
        self.environment["MOCK_DOCKER_RUNNING"] = "true"
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            Path(self.environment["MOCK_DOCKER_LOG"]).read_text().splitlines(),
            ["inspect", "--format", "{{.State.Running}}", "fixture-worker"],
        )

    def test_fn_os_managed_start_stop_do_not_invoke_docker(self) -> None:
        for action in ("start", "stop"):
            result = subprocess.run(
                ["bash", str(COMMANDS / "main"), action], env=self.environment,
                text=True, capture_output=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(Path(self.environment["MOCK_DOCKER_LOG"]).exists())

    def test_common_status_also_rejects_missing_compose(self) -> None:
        self.assertEqual(self.run_shell("daemon_status", common=True).returncode, 1)


class DefaultConfigurationRegressions(ScriptFixtures):
    def test_defaults_are_created_as_complete_json(self) -> None:
        result = self.run_shell(self.setup_body("create_livemotion_defaults"))
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((self.root / "config" / "config.json").read_text())
        self.assertEqual(config["input_dir"], "/photos/live_inbox")
        for name in ("live_inbox", "motion_output", "archive", "failed"):
            self.assertTrue((self.root / "photos" / name).is_dir())
        self.assertEqual(list((self.root / "config").glob(".config.json.*")), [])

    def test_existing_configuration_is_preserved(self) -> None:
        config = self.root / "config" / "config.json"
        config.parent.mkdir()
        config.write_text('{"custom": true}\n')
        result = self.run_shell(self.setup_body("create_livemotion_defaults"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), '{"custom": true}\n')

    def test_directory_creation_failure_propagates_from_all_service_hooks(self) -> None:
        for hook in ("create_livemotion_defaults", "service_postinst", "service_postupgrade", "service_postconfig"):
            with self.subTest(hook=hook):
                result = self.run_shell(self.setup_body(f"mkdir() {{ return 23; }}; {hook}"))
                self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse((self.root / "config" / "config.json").exists())

    def test_file_blocking_config_directory_fails_visibly(self) -> None:
        (self.root / "config").write_text("blocked")
        result = self.run_shell(self.setup_body("create_livemotion_defaults"))
        self.assertNotEqual(result.returncode, 0)
        # mkdir diagnostics differ by platform/locale; require a visible error,
        # not a particular system-generated filename or English message.
        self.assertTrue(result.stderr.strip(), "directory failure must be visible")
        self.assertEqual((self.root / "config").read_text(), "blocked")

    def test_missing_volume_is_not_created_on_root_filesystem(self) -> None:
        missing_volume = "/vol987654321-livemotion-regression"
        self.assertFalse(Path(missing_volume).exists())
        result = self.run_shell(self.setup_body(
            f"PHOTOS_DIR={missing_volume}/photos; create_livemotion_defaults"
        ))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Storage volume is missing", result.stderr)
        self.assertFalse(Path(missing_volume).exists())

    def test_failed_config_write_leaves_no_partial_default(self) -> None:
        result = self.run_shell(self.setup_body(
            'cat() { printf "partial"; return 28; }; create_livemotion_defaults'
        ))
        self.assertEqual(result.returncode, 28, result.stderr)
        self.assertFalse((self.root / "config" / "config.json").exists())
        self.assertEqual(list((self.root / "config").iterdir()), [])

    def test_failed_temporary_file_creation_is_propagated(self) -> None:
        result = self.run_shell(self.setup_body(
            'mktemp() { return 24; }; create_livemotion_defaults'
        ))
        self.assertEqual(result.returncode, 24, result.stderr)
        self.assertFalse((self.root / "config" / "config.json").exists())

    def test_failed_config_rename_removes_temporary_file(self) -> None:
        result = self.run_shell(self.setup_body(
            'mv() { return 29; }; create_livemotion_defaults'
        ))
        self.assertEqual(result.returncode, 29, result.stderr)
        self.assertFalse((self.root / "config" / "config.json").exists())
        self.assertEqual(list((self.root / "config").iterdir()), [])

    def test_directory_at_config_file_path_is_rejected(self) -> None:
        (self.root / "config" / "config.json").mkdir(parents=True)
        result = self.run_shell(self.setup_body("create_livemotion_defaults"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a regular file", result.stderr)
        self.assertEqual(list((self.root / "config" / "config.json").iterdir()), [])


def rewrite_archive(blob: bytes, replacements: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as source:
        with tarfile.open(fileobj=output, mode="w:gz") as destination:
            for original in source.getmembers():
                member = copy.copy(original)
                if member.isfile():
                    name = member.name.removeprefix("./")
                    stream = source.extractfile(original)
                    assert stream is not None
                    data = replacements.get(name, stream.read())
                    member.size = len(data)
                    destination.addfile(member, io.BytesIO(data))
                else:
                    destination.addfile(member)
    return output.getvalue()


def archive_file(blob: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        stream = archive.extractfile(name)
        assert stream is not None
        return stream.read()


class PackageIntegrityRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(
            [str(ROOT / "build-fpk.sh")], cwd=ROOT,
            check=True, text=True, capture_output=True, timeout=30,
        )
        cls.package = (ROOT / "dist" / "LiveMotion.fpk").read_bytes()

    def verify(self, blob: bytes) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="livemotion-corruption-") as temporary:
            fixture = Path(temporary) / "fixture.fpk"
            fixture.write_bytes(blob)
            return subprocess.run(
                [str(ROOT / "verify-fpk.sh"), str(fixture)], cwd=ROOT,
                env={**os.environ, "TMPDIR": temporary}, text=True,
                capture_output=True, timeout=30,
            )

    def test_valid_generated_package_has_matching_checksum(self) -> None:
        manifest = archive_file(self.package, "manifest").decode()
        checksum = re.search(r"^checksum\s*=\s*([0-9a-f]{32})$", manifest, re.MULTILINE)
        self.assertIsNotNone(checksum)
        self.assertEqual(checksum[1], hashlib.md5(archive_file(self.package, "app.tgz")).hexdigest())
        result = self.verify(self.package)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_well_formed_but_wrong_checksum_is_rejected(self) -> None:
        manifest = archive_file(self.package, "manifest").decode()
        altered = re.sub(r"(?m)^checksum\s*=.*$", "checksum = " + "0" * 32, manifest)
        result = self.verify(rewrite_archive(self.package, {"manifest": altered.encode()}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)

    def test_tampered_app_tgz_is_rejected_before_extraction(self) -> None:
        payload = archive_file(self.package, "app.tgz") + b"tampered"
        result = self.verify(rewrite_archive(self.package, {"app.tgz": payload}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)

    def test_desktop_icon_with_wrong_dimensions_is_rejected(self) -> None:
        result = self.verify(rewrite_archive(self.package, {
            "ui/images/64.png": archive_file(self.package, "ICON_256.PNG"),
        }))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be 64x64", result.stderr)

    def test_distinct_desktop_icon_at_correct_size_is_rejected(self) -> None:
        icon = archive_file(self.package, "ICON.PNG")
        payload = b"fixture\x00different export"
        chunk = b"tEXt" + payload
        ancillary = struct.pack(">I", len(payload)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        different = icon[:33] + ancillary + icon[33:]
        result = self.verify(rewrite_archive(self.package, {"ui/images/64.png": different}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("icons differ", result.stderr)

    def test_png_chunk_corruption_is_rejected(self) -> None:
        icon = bytearray(archive_file(self.package, "ICON.PNG"))
        icon[-1] ^= 1
        result = self.verify(rewrite_archive(self.package, {"ICON.PNG": bytes(icon)}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PNG CRC mismatch", result.stderr)

    def test_payload_desktop_icon_mismatch_is_rejected_even_with_updated_checksum(self) -> None:
        payload = rewrite_archive(archive_file(self.package, "app.tgz"), {
            "ui/images/64.png": archive_file(self.package, "ICON_256.PNG"),
        })
        manifest = archive_file(self.package, "manifest").decode()
        manifest = re.sub(
            r"(?m)^checksum\s*=.*$", "checksum = " + hashlib.md5(payload).hexdigest(), manifest,
        )
        result = self.verify(rewrite_archive(self.package, {
            "app.tgz": payload, "manifest": manifest.encode(),
        }))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Root and app.tgz desktop", result.stderr)


class LocalBuildArgumentRegressions(unittest.TestCase):
    def test_compose_motionphoto_build_argument_matches_dockerfile_argument(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        docker_args = dict(re.findall(r"^ARG\s+(MOTIONPHOTO2_\w+)=(\S+)", dockerfile, re.MULTILINE))
        compose_args = dict(re.findall(r"^\s+(MOTIONPHOTO2_\w+):\s*(\S+)", compose, re.MULTILINE))
        self.assertTrue(compose_args)
        self.assertEqual(compose_args, docker_args)


if __name__ == "__main__":
    unittest.main()
