from __future__ import annotations

import os
import subprocess
import tarfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FPK_DIR = ROOT / "fpk"


class FpkPackageTests(unittest.TestCase):
    def test_manifest_and_compose_defaults_match_installable_docker_fpk_shape(self) -> None:
        manifest = (FPK_DIR / "manifest").read_text(encoding="utf-8")
        self.assertIn("appname         = livemotion", manifest)
        self.assertIn("display_name    = LiveMotion", manifest)
        self.assertIn("service_port    = 8011", manifest)
        self.assertIn("checkport       = false", manifest)
        self.assertIn("fpk_version     = 0.1.2-r1", manifest)
        self.assertFalse((FPK_DIR / "manifest.json").exists())

        compose = (FPK_DIR / "docker" / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn("build:", compose)
        self.assertIn("image: ghcr.io/dttxorg/livemotion:0.1.2", compose)
        self.assertNotIn("ghcr.io/your-user", compose)
        self.assertIn("8011:8011", compose)
        self.assertIn("/vol2/photos:/photos", compose)
        self.assertIn("/vol1/docker/livephoto-worker/config:/config", compose)
        volume_lines = [line.strip() for line in compose.splitlines() if line.strip().startswith("- /vol")]
        self.assertEqual(volume_lines, [
            "- /vol2/photos:/photos",
            "- /vol1/docker/livephoto-worker/config:/config",
        ])
        self.assertFalse((FPK_DIR / "docker" / "docker-compose.yml").exists())

    def test_fpk_tree_has_no_local_mac_paths_or_forbidden_recursive_delete(self) -> None:
        forbidden_fragments = [
            "/" + "Users/zhuli/Documents/live photo",
            "rm " + "-rf",
            "rmdir " + "/s",
            "rd " + "/s",
            "del " + "/s",
            "Remove-Item " + "-Recurse",
        ]
        checked = []
        for path in [ROOT / "build-fpk.sh", ROOT / "verify-fpk.sh", *FPK_DIR.rglob("*")]:
            if not path.is_file():
                continue
            if path.suffix.lower() in {".png"} or path.name in {"ICON.PNG", "ICON_256.PNG"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            checked.append(path)
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment, text, f"{fragment} found in {path}")
        self.assertGreater(len(checked), 5)

    def test_lifecycle_scripts_are_executable(self) -> None:
        for name in [
            "main",
            "common",
            "installer",
            "service-setup",
            "install_init",
            "install_callback",
            "uninstall_init",
            "uninstall_callback",
            "upgrade_init",
            "upgrade_callback",
            "config_init",
            "config_callback",
        ]:
            path = FPK_DIR / "cmd" / name
            self.assertTrue(path.is_file(), name)
            self.assertTrue(os.access(path, os.X_OK), name)

    def test_github_actions_builds_and_pushes_ghcr_image(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "docker-ghcr.yml"
        self.assertTrue(workflow_path.is_file())
        workflow = workflow_path.read_text(encoding="utf-8")

        self.assertIn("branches:", workflow)
        self.assertIn("- main", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("IMAGE_NAME: ghcr.io/dttxorg/livemotion", workflow)
        self.assertIn("IMAGE_VERSION: 0.1.2", workflow)
        self.assertIn("file: ./Dockerfile", workflow)
        self.assertIn("${{ env.IMAGE_NAME }}:${{ env.IMAGE_VERSION }}", workflow)
        self.assertIn("${{ env.IMAGE_NAME }}:latest", workflow)

    def test_dockerfile_installs_motionphoto2_from_source_not_release_binary(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertNotIn("MotionPhoto2_Linux", dockerfile)
        self.assertNotIn("releases/download", dockerfile)
        self.assertNotIn("unzip /tmp/motionphoto2.zip", dockerfile)
        self.assertIn("git clone --depth 1 --branch", dockerfile)
        self.assertIn("https://github.com/PetrVys/MotionPhoto2", dockerfile)
        self.assertIn("MOTIONPHOTO2_SCRIPT=/opt/MotionPhoto2/motionphoto2.py", dockerfile)
        self.assertIn("libimage-exiftool-perl", dockerfile)
        self.assertIn("ffmpeg", dockerfile)
        self.assertIn("python /opt/MotionPhoto2/motionphoto2.py --help", dockerfile)

    def test_build_fpk_script_creates_package_with_expected_contents(self) -> None:
        result = subprocess.run(
            [str(ROOT / "build-fpk.sh")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        package_path = ROOT / "dist" / "LiveMotion.fpk"
        self.assertTrue(package_path.is_file(), result.stdout + result.stderr)

        with tarfile.open(package_path, "r:gz") as package:
            names = {name.removeprefix("./") for name in package.getnames()}
            self.assertFalse(any("/._" in name or name.startswith("._") for name in names))
            self.assertNotIn("fpk/manifest", names)
            self.assertNotIn("manifest.json", names)
            self.assertNotIn("docker/docker-compose.yaml", names)
            self.assertIn("manifest", names)
            self.assertIn("app.tgz", names)
            self.assertIn("cmd/main", names)
            self.assertIn("cmd/common", names)
            self.assertIn("cmd/installer", names)
            self.assertIn("cmd/config_init", names)
            self.assertIn("cmd/config_callback", names)
            self.assertIn("cmd/install_callback", names)
            self.assertIn("config/resource", names)
            self.assertIn("ui/config", names)
            self.assertIn("ui/images/256.png", names)
            self.assertIn("ICON.PNG", names)
            self.assertIn("ICON_256.PNG", names)
            manifest_file = package.extractfile("manifest")
            self.assertIsNotNone(manifest_file)
            manifest_text = manifest_file.read().decode("utf-8")  # type: ignore[union-attr]
            self.assertRegex(manifest_text, r"checksum\s*=\s*[0-9a-f]{32}")
            self.assertIn("fpk_version     = 0.1.2-r1", manifest_text)
            app_tgz = package.extractfile("app.tgz")
            self.assertIsNotNone(app_tgz)
            temp_app = ROOT / "dist" / "_test_app_payload.tgz"
            temp_app.write_bytes(app_tgz.read())  # type: ignore[union-attr]

        try:
            with tarfile.open(temp_app, "r:gz") as app_payload:
                app_names = {name.removeprefix("./") for name in app_payload.getnames()}
                self.assertFalse(any("/._" in name or name.startswith("._") for name in app_names))
                self.assertIn("docker/docker-compose.yaml", app_names)
                self.assertNotIn("docker/Dockerfile", app_names)
                self.assertNotIn("docker/requirements.txt", app_names)
                self.assertFalse(any(name.startswith("docker/src/") for name in app_names))
                self.assertIn("ui/config", app_names)
                self.assertIn("ui/images/64.png", app_names)
                self.assertIn("ui/images/256.png", app_names)
        finally:
            if temp_app.exists():
                temp_app.unlink()

    def test_verify_fpk_script_accepts_generated_package(self) -> None:
        subprocess.run(
            [str(ROOT / "build-fpk.sh")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            [str(ROOT / "verify-fpk.sh"), str(ROOT / "dist" / "LiveMotion.fpk")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Verification complete", result.stdout)


if __name__ == "__main__":
    unittest.main()
