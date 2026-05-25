from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaPair:
    stem: str
    image_path: Path
    video_path: Path

    @property
    def key(self) -> str:
        return f"{self.image_path}|{self.video_path}"


@dataclass(frozen=True)
class MediaItem:
    path: Path
    media_type: str

    @property
    def key(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class PairHashes:
    image_sha256: str
    video_sha256: str

    @property
    def signature(self) -> str:
        return f"sha256:{self.image_sha256}:{self.video_sha256}"
