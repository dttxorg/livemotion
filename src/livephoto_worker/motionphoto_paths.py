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
    for candidate in possible_motionphoto_outputs(expected_path):
        try:
            if not candidate.is_file():
                continue
            if started_at is not None and candidate.stat().st_mtime < started_at - 1:
                continue
        except FileNotFoundError:
            continue
        if candidate != expected_path:
            logger.info("MotionPhoto2 actual output path: expected=%s actual=%s", expected_path, candidate)
        return candidate
    return None
