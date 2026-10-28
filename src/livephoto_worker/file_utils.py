from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from .models import MediaItem, MediaPair, PairHashes

HASH_CHUNK_SIZE = 1024 * 1024


def quick_file_state(path: Path) -> str:
    stat = path.stat()
    return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"


def quick_pair_state(pair: MediaPair) -> str:
    return f"{quick_file_state(pair.image_path)}|{quick_file_state(pair.video_path)}"


def quick_media_item_state(item: MediaItem) -> str:
    return quick_file_state(item.path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_pair(pair: MediaPair) -> PairHashes:
    return PairHashes(
        image_sha256=sha256_file(pair.image_path),
        video_sha256=sha256_file(pair.video_path),
    )


def file_signature(path: Path) -> tuple[str, str]:
    digest = sha256_file(path)
    return f"sha256:{digest}", digest


def unique_path(directory: Path, filename: str, marker: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists() and not candidate.is_symlink():
        return candidate

    original = Path(filename)
    stem = original.stem
    suffix = original.suffix
    base_marker = marker or "copy"

    marked = directory / f"{stem}_{base_marker}{suffix}"
    if not marked.exists() and not marked.is_symlink():
        return marked

    index = 2
    while True:
        numbered = directory / f"{stem}_{base_marker}_{index}{suffix}"
        if not numbered.exists() and not numbered.is_symlink():
            return numbered
        index += 1


def move_one_file(source: Path, destination_dir: Path, marker: str | None = None) -> Path | None:
    if not source.exists():
        return None
    destination = unique_path(destination_dir, source.name, marker)
    shutil.move(str(source), str(destination))
    return destination


def publish_unique_file(source: Path, directory: Path, *, marker: str | None = None) -> Path:
    """Publish an immutable staged output without replacing any existing path."""
    while True:
        destination = unique_path(directory, source.name, marker)
        try:
            # When staging shares the output filesystem, linking publishes a
            # complete file atomically without replacing a racing writer.
            os.link(source, destination)
        except FileExistsError:
            continue
        except OSError:
            # Cross-filesystem staging and some NAS volumes need a copy.
            # Exclusive creation preserves existing files; the new target may
            # be visible during copying, and is removed if that copy fails.
            created = False
            try:
                with source.open("rb") as input_handle, destination.open("xb") as output_handle:
                    created = True
                    shutil.copyfileobj(input_handle, output_handle)
                shutil.copystat(source, destination)
            except FileExistsError:
                continue
            except BaseException:
                if created:
                    with suppress(OSError):
                        destination.unlink()
                raise
        return destination


def media_staging_directory(work_root: Path, *, media_roots: tuple[Path, ...]) -> tempfile.TemporaryDirectory[str]:
    """Keep converter/copy work out of both scanning and synchronized media trees."""
    excluded = tuple(root.expanduser().resolve() for root in media_roots)
    for root in (work_root / ".livephoto-work", Path(tempfile.gettempdir())):
        resolved = root.expanduser().resolve()
        if any(resolved == media_root or media_root in resolved.parents for media_root in excluded):
            continue
        resolved.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=".livephoto-", dir=resolved)
    raise ValueError("No staging directory exists outside the configured media roots")
