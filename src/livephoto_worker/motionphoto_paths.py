from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MOTIONPHOTO_OUTPUT_SUFFIXES = (".heic", ".HEIC", ".jpg", ".jpeg", ".JPG", ".JPEG")


def preferred_motionphoto_suffix(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    if suffix in {".heic", ".heif"}:
        return ".heic"
    if suffix in {".jpg", ".jpeg"}:
        return ".jpg"
    return suffix or ".jpg"


def motionphoto_output_filename(image_path: Path) -> str:
    return f"{image_path.stem}{preferred_motionphoto_suffix(image_path)}"


def possible_motionphoto_outputs(expected_path: Path) -> list[Path]:
    seen: set[Path] = set()
    candidates: list[Path] = []
    for candidate in [expected_path, *(expected_path.with_suffix(suffix) for suffix in MOTIONPHOTO_OUTPUT_SUFFIXES)]:
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def resolve_motionphoto_output(expected_path: Path, *, started_at: float | None = None) -> Path | None:
    if expected_path.exists():
        return expected_path

    candidates = possible_motionphoto_outputs(expected_path)
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if not existing:
        return None

    if started_at is not None:
        recent = [candidate for candidate in existing if candidate.stat().st_mtime >= started_at - 1]
        if recent:
            actual = recent[0]
            logger.info("MotionPhoto2 actual output path: expected=%s actual=%s", expected_path, actual)
            return actual

    actual = existing[0]
    logger.info("MotionPhoto2 actual output path: expected=%s actual=%s", expected_path, actual)
    return actual
