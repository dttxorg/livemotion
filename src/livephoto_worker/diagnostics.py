from __future__ import annotations

import logging
import shlex
import subprocess
import traceback
from dataclasses import dataclass
from pathlib import Path

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
    try:
        relative_image = image_path.relative_to(photos_root)
    except ValueError:
        relative_image = Path(image_path.name)
    return output_root / relative_image.with_suffix(preferred_motionphoto_suffix(image_path))


def run_motionphoto2_diagnostic(
    *,
    settings: Settings,
    image_path: Path,
    video_path: Path,
    output_root: Path = DEFAULT_DIAGNOSTIC_OUTPUT_ROOT,
    photos_root: Path = DEFAULT_PHOTOS_ROOT,
) -> MotionPhoto2DiagnosticResult:
    image_path = image_path.expanduser()
    video_path = video_path.expanduser()
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
    logger.info("MotionPhoto2 command: %s", shlex.join(command))

    if not image_exists or not video_exists:
        missing = []
        if not image_exists:
            missing.append(f"image not found: {image_path}")
        if not video_exists:
            missing.append(f"video not found: {video_path}")
        reason = "; ".join(missing)
        logger.info("return code: %s", None)
        logger.info("stdout:\n%s", "")
        logger.info("stderr:\n%s", reason)
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

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.process_timeout_seconds,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic endpoint must report invocation failures directly.
        stderr = traceback.format_exc()
        reason = f"MotionPhoto2 invocation failed: {exc}"
        logger.exception("MotionPhoto2 diagnostic invocation raised exception")
        logger.info("return code: %s", None)
        logger.info("stdout:\n%s", "")
        logger.info("stderr:\n%s", stderr.rstrip())
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
            stderr=stderr,
        )

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    logger.info("return code: %s", result.returncode)
    logger.info("stdout:\n%s", stdout.rstrip())
    logger.info("stderr:\n%s", stderr.rstrip())

    actual_output_path = resolve_motionphoto_output(output_path) if result.returncode == 0 else output_path
    if actual_output_path != output_path:
        logger.info("actual output path: %s", actual_output_path)

    success = result.returncode == 0 and actual_output_path is not None and actual_output_path.is_file()
    if success:
        reason = "MotionPhoto2 diagnostic conversion succeeded"
    elif result.returncode != 0:
        reason = f"MotionPhoto2 exited with code {result.returncode}"
        if stderr.strip():
            reason = f"{reason}: {stderr.strip()}"
    else:
        reason = f"MotionPhoto2 exited with code 0 but output file was not created: {output_path}"

    return MotionPhoto2DiagnosticResult(
        success=success,
        reason=reason,
        image_path=image_path,
        video_path=video_path,
        output_path=actual_output_path or output_path,
        image_exists=image_exists,
        video_exists=video_exists,
        command=command,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )
