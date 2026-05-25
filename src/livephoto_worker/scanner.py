from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .models import MediaItem, MediaPair

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = (".heic", ".heif", ".jpg", ".jpeg", ".png")
VIDEO_EXTENSIONS = (".mov", ".mp4", ".m4v")
DEFAULT_SKIP_DIR_NAMES = (
    ".stfolder",
    "@eaDir",
    "#recycle",
    ".Trash",
    ".AppleDouble",
    "__MACOSX",
)


@dataclass(frozen=True)
class ScanStats:
    scanned_dirs: int = 0
    scanned_files: int = 0
    skipped_dirs: int = 0


@dataclass(frozen=True)
class ScanResult:
    pairs: list[MediaPair] = field(default_factory=list)
    media_items: list[MediaItem] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)


def _priority(path: Path, extensions: tuple[str, ...]) -> tuple[int, str]:
    ext = path.suffix.lower()
    try:
        ext_priority = extensions.index(ext)
    except ValueError:
        ext_priority = len(extensions)
    return ext_priority, path.name.lower()


def _safe_resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _should_skip_dir(path: Path, *, skip_names: set[str], skip_roots: tuple[Path, ...]) -> bool:
    if path.name in skip_names:
        return True
    resolved = _safe_resolve(path)
    return any(resolved == root or _is_relative_to(resolved, root) for root in skip_roots)


def _collect_from_directory(directory: Path) -> tuple[list[MediaPair], list[MediaItem]]:
    images: dict[str, list[Path]] = defaultdict(list)
    videos: dict[str, list[Path]] = defaultdict(list)

    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except FileNotFoundError:
        return [], []

    for child in children:
        if not child.is_file():
            continue
        suffix = child.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            images[child.stem].append(child)
        elif suffix in VIDEO_EXTENSIONS:
            videos[child.stem].append(child)

    pairs: list[MediaPair] = []
    paired_stems = set(images).intersection(videos)
    for stem in sorted(paired_stems):
        image_path = sorted(images[stem], key=lambda p: _priority(p, IMAGE_EXTENSIONS))[0]
        video_path = sorted(videos[stem], key=lambda p: _priority(p, VIDEO_EXTENSIONS))[0]
        pairs.append(MediaPair(stem=stem, image_path=image_path, video_path=video_path))

    media_items: list[MediaItem] = []
    for stem, paths in images.items():
        if stem in paired_stems:
            continue
        for path in sorted(paths, key=lambda p: _priority(p, IMAGE_EXTENSIONS)):
            media_items.append(MediaItem(path=path, media_type="photo"))
    for stem, paths in videos.items():
        if stem in paired_stems:
            continue
        for path in sorted(paths, key=lambda p: _priority(p, VIDEO_EXTENSIONS)):
            media_items.append(MediaItem(path=path, media_type="video"))

    media_items.sort(key=lambda item: str(item.path).lower())
    return pairs, media_items


def scan_media(
    input_dir: Path,
    *,
    recursive: bool = True,
    output_dir: Path | None = None,
    archive_dir: Path | None = None,
    failed_dir: Path | None = None,
    skip_dir_names: list[str] | tuple[str, ...] = DEFAULT_SKIP_DIR_NAMES,
) -> ScanResult:
    if not input_dir.exists():
        return ScanResult()

    skip_names = {name for name in DEFAULT_SKIP_DIR_NAMES}
    skip_names.update(name for name in skip_dir_names if name)
    input_root = _safe_resolve(input_dir)
    skip_roots = tuple(
        root
        for root in (
            _safe_resolve(output_dir) if output_dir is not None else None,
            _safe_resolve(archive_dir) if archive_dir is not None else None,
            _safe_resolve(failed_dir) if failed_dir is not None else None,
        )
        if root is not None and (root == input_root or _is_relative_to(root, input_root))
    )

    if recursive:
        logger.info("Scanning recursively from input_dir: %s", input_dir)
    else:
        logger.info("Scanning input_dir without recursion: %s", input_dir)

    pairs: list[MediaPair] = []
    media_items: list[MediaItem] = []
    scanned_dirs = 0
    scanned_files = 0
    skipped_dirs = 0

    if not recursive:
        pairs, media_items = _collect_from_directory(input_dir)
        scanned_dirs = 1
        try:
            scanned_files = sum(1 for child in input_dir.iterdir() if child.is_file())
        except FileNotFoundError:
            scanned_files = 0
        return ScanResult(pairs=pairs, media_items=media_items, stats=ScanStats(scanned_dirs, scanned_files, 0))

    for current_root, dirnames, filenames in os.walk(input_dir, topdown=True):
        current_path = Path(current_root)
        scanned_dirs += 1
        kept_dirnames: list[str] = []
        for dirname in dirnames:
            child_dir = current_path / dirname
            if _should_skip_dir(child_dir, skip_names=skip_names, skip_roots=skip_roots):
                skipped_dirs += 1
                logger.info("Skipped directory: %s", child_dir)
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        scanned_files += len(filenames)
        directory_pairs, directory_items = _collect_from_directory(current_path)
        pairs.extend(directory_pairs)
        media_items.extend(directory_items)

    pairs.sort(key=lambda pair: str(pair.image_path).lower())
    media_items.sort(key=lambda item: str(item.path).lower())
    return ScanResult(
        pairs=pairs,
        media_items=media_items,
        stats=ScanStats(
            scanned_dirs=scanned_dirs,
            scanned_files=scanned_files,
            skipped_dirs=skipped_dirs,
        ),
    )


def scan_pairs(input_dir: Path) -> list[MediaPair]:
    """Return image/video pairs that share the same filename stem in the same directory."""
    return scan_media(input_dir).pairs
