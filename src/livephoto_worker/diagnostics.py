from __future__ import annotations

import logging
import shlex
import subprocess
import traceback
from dataclasses import dataclass
from pathlib import Path

from .file_utils import media_staging_directory, publish_unique_file, unique_path
from .motionphoto_paths import preferred_motionphoto_suffix, resolve_motionphoto_output
from .settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_PHOTOS_ROOT = Path("/photos")
DEFAULT_DIAGNOSTIC_OUTPUT_ROOT = Path("/photos/live")


@dataclass(frozen=True)
class MotionPhoto2DiagnosticResult:
    success: bool
    reason: str
    image_path: Path
    video_path: Path
    output_path: Path
    image_exists: bool
    video_exists: bool
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str

    @property
    def command_text(self) -> str:
        return shlex.join(self.command)

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "reason": self.reason,
            "image_path": str(self.image_path),
            "video_path": str(self.video_path),
            "output_path": str(self.output_path),
            "image_exists": self.image_exists,
            "video_exists": self.video_exists,
            "command": self.command,
            "command_text": self.command_text,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class MotionPhoto2Status:
    available: bool
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    error: str = ""

    @property
    def command_text(self) -> str:
        return shlex.join(self.command)

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "command": self.command,
            "command_text": self.command_text,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }


def check_motionphoto2_available(settings: Settings) -> MotionPhoto2Status:
    command = settings.build_motionphoto2_help_command()
    logger.info("Running MotionPhoto2 startup self-check: %s", shlex.join(command))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - surface startup diagnostics in Web UI.
        stderr = traceback.format_exc()
        error = f"MotionPhoto2 self-check invocation failed: {exc}"
        logger.exception("MotionPhoto2 startup self-check invocation failed")
        return MotionPhoto2Status(
            available=False,
            command=command,
            returncode=None,
            stdout="",
            stderr=stderr,
            error=error,
        )

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode == 0:
        logger.info("MotionPhoto2 startup self-check passed")
        return MotionPhoto2Status(
            available=True,
            command=command,
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    error = f"MotionPhoto2 self-check failed with code {result.returncode}"
    logger.error("%s; stderr=%s", error, stderr.rstrip())
    return MotionPhoto2Status(
        available=False,
        command=command,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
        error=error,
    )


def diagnostic_output_path(
    image_path: Path,
    *,
    output_root: Path = DEFAULT_DIAGNOSTIC_OUTPUT_ROOT,
    photos_root: Path = DEFAULT_PHOTOS_ROOT,
) -> Path:
    image_path = image_path.expanduser().resolve(strict=False)
    photos_root = photos_root.expanduser().resolve(strict=False)
    output_root = output_root.expanduser()
    resolved_output_root = output_root.resolve(strict=False)
    try:
        relative_image = image_path.relative_to(photos_root)
    except ValueError:
        relative_image = Path(image_path.name)
    output_path = output_root / relative_image.with_suffix(preferred_motionphoto_suffix(image_path))
    if not output_path.resolve(strict=False).is_relative_to(resolved_output_root):
        # A pre-existing output subdirectory can itself be a symlink out of the
        # configured tree. Flatten in that case rather than following it.
        output_path = output_root / Path(image_path.name).with_suffix(preferred_motionphoto_suffix(image_path))
        if not output_path.resolve(strict=False).is_relative_to(resolved_output_root):
            # A leaf symlink is occupied too; never return its external target.
            output_path = unique_path(output_root, output_path.name, marker="diagnostic")
    return output_path


def run_motionphoto2_diagnostic(
    *,
    settings: Settings,
    image_path: Path,
    video_path: Path,
    output_root: Path | None = None,
    photos_root: Path | None = None,
) -> MotionPhoto2DiagnosticResult:
    image_path = image_path.expanduser()
    video_path = video_path.expanduser()
    if output_root is None:
        output_root = settings.output_dir
    if photos_root is None:
        photos_root = settings.input_dir if settings.preserve_directory_structure else image_path.parent
    output_path = diagnostic_output_path(image_path, output_root=output_root, photos_root=photos_root)
    command = settings.build_motionphoto2_command(
        image_path=image_path,
        video_path=video_path,
        output_path=output_path,
    )

    image_exists = image_path.is_file()
    video_exists = video_path.is_file()
    logger.info("image exists: %s path=%s", image_exists, image_path)
    logger.info("video exists: %s path=%s", video_exists, video_path)
    logger.info("output path: %s", output_path)

    if not image_exists or not video_exists:
        missing = []
        if not image_exists:
            missing.append(f"image not found: {image_path}")
        if not video_exists:
            missing.append(f"video not found: {video_path}")
        reason = "; ".join(missing)
        return MotionPhoto2DiagnosticResult(
            success=False,
            reason=reason,
            image_path=image_path,
            video_path=video_path,
            output_path=output_path,
            image_exists=image_exists,
            video_exists=video_exists,
            command=command,
            returncode=None,
            stdout="",
            stderr=reason,
        )

    returncode: int | None = None
    stdout = ""
    stderr = ""
    success = False
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with media_staging_directory(
            settings.db_path.parent,
            media_roots=(settings.input_dir, output_root, settings.archive_dir, settings.failed_dir),
        ) as staging:
            staged_output = Path(staging) / output_path.name
            command = settings.build_motionphoto2_command(
                image_path=image_path,
                video_path=video_path,
                output_path=staged_output,
            )
            logger.info("MotionPhoto2 command: %s", shlex.join(command))
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.process_timeout_seconds,
                check=False,
            )
            returncode = result.returncode
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            # A private empty directory proves provenance even when the tool
            # copies the original media's old filesystem timestamps.
            actual_output = resolve_motionphoto_output(staged_output) if returncode == 0 else None
            if actual_output is not None:
                output_path = publish_unique_file(actual_output, output_path.parent, marker="diagnostic")
                logger.info("actual output path: %s", output_path)
                success = True
                reason = "MotionPhoto2 diagnostic conversion succeeded"
            elif returncode != 0:
                reason = f"MotionPhoto2 exited with code {returncode}"
                if stderr.strip():
                    reason = f"{reason}: {stderr.strip()}"
            else:
                reason = f"MotionPhoto2 exited with code 0 but output file was not created: {output_path}"
    except Exception as exc:  # noqa: BLE001 - report invocation and publication failures directly.
        stderr = traceback.format_exc()
        reason = f"MotionPhoto2 diagnostic failed: {exc}"
        logger.exception("MotionPhoto2 diagnostic raised exception")

    logger.info("return code: %s", returncode)
    logger.info("stdout:\n%s", stdout.rstrip())
    logger.info("stderr:\n%s", stderr.rstrip())
    return MotionPhoto2DiagnosticResult(
        success=success,
        reason=reason,
        image_path=image_path,
        video_path=video_path,
        output_path=output_path,
        image_exists=image_exists,
        video_exists=video_exists,
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
