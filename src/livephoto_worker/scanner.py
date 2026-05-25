from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .models import MediaPair

IMAGE_EXTENSIONS = (".heic", ".heif", ".jpg", ".jpeg")
VIDEO_EXTENSIONS = (".mov", ".mp4")


def _priority(path: Path, extensions: tuple[str, ...]) -> tuple[int, str]:
    ext = path.suffix.lower()
    try:
        ext_priority = extensions.index(ext)
    except ValueError:
        ext_priority = len(extensions)
    return ext_priority, path.name.lower()


def scan_pairs(input_dir: Path) -> list[MediaPair]:
    """Return direct-child image/video pairs that share the same filename stem."""
    if not input_dir.exists():
        return []

    images: dict[str, list[Path]] = defaultdict(list)
    videos: dict[str, list[Path]] = defaultdict(list)

    for child in input_dir.iterdir():
        if not child.is_file():
            continue
        suffix = child.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            images[child.stem].append(child)
        elif suffix in VIDEO_EXTENSIONS:
            videos[child.stem].append(child)

    pairs: list[MediaPair] = []
    for stem in sorted(set(images).intersection(videos)):
        image_path = sorted(images[stem], key=lambda p: _priority(p, IMAGE_EXTENSIONS))[0]
        video_path = sorted(videos[stem], key=lambda p: _priority(p, VIDEO_EXTENSIONS))[0]
        pairs.append(MediaPair(stem=stem, image_path=image_path, video_path=video_path))
    return pairs
