from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .models import MediaPair, PairHashes

HASH_CHUNK_SIZE = 1024 * 1024


def quick_file_state(path: Path) -> str:
    stat = path.stat()
    return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"


def quick_pair_state(pair: MediaPair) -> str:
    return f"{quick_file_state(pair.image_path)}|{quick_file_state(pair.video_path)}"


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


def unique_path(directory: Path, filename: str, marker: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    original = Path(filename)
    stem = original.stem
    suffix = original.suffix
    base_marker = marker or "copy"

    marked = directory / f"{stem}_{base_marker}{suffix}"
    if not marked.exists():
        return marked

    index = 2
    while True:
        numbered = directory / f"{stem}_{base_marker}_{index}{suffix}"
        if not numbered.exists():
            return numbered
        index += 1


def move_one_file(source: Path, destination_dir: Path, marker: str | None = None) -> Path | None:
    if not source.exists():
        return None
    destination = unique_path(destination_dir, source.name, marker)
    shutil.move(str(source), str(destination))
    return destination
