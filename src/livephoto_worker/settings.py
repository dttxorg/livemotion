from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


TRUTHY = {"1", "true", "yes", "y", "on"}
CONFIG_KEYS = {
    "input_dir",
    "output_dir",
    "archive_dir",
    "failed_dir",
    "stable_seconds",
    "poll_interval",
    "move_originals",
    "enable_archive",
    "recursive_scan",
    "preserve_directory_structure",
    "skip_dir_names",
}

DEFAULT_SKIP_DIR_NAMES = [
    ".stfolder",
    "@eaDir",
    "#recycle",
    ".Trash",
    ".AppleDouble",
    "__MACOSX",
]


def env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser()


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in TRUTHY


def _coerce_path(value: Any) -> Path:
    return Path(str(value)).expanduser()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in TRUTHY


def env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.replace(",", "\n").splitlines() if item.strip()]


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]
    return list(DEFAULT_SKIP_DIR_NAMES)


@dataclass
class Settings:
    input_dir: Path = env_path("INPUT_DIR", "/photos/live_inbox")
    output_dir: Path = env_path("OUTPUT_DIR", "/photos/motion_output")
    archive_dir: Path = env_path("ARCHIVE_DIR", "/photos/archive")
    failed_dir: Path = env_path("FAILED_DIR", "/photos/failed")
    stable_seconds: float = env_float("STABLE_SECONDS", env_float("TRANSFER_SETTLE_SECONDS", 30.0))
    poll_interval: float = env_float("POLL_INTERVAL", env_float("WATCH_INTERVAL_SECONDS", 10.0))
    move_originals: bool = env_bool("MOVE_ORIGINALS", True)
    enable_archive: bool = env_bool("ENABLE_ARCHIVE", True)
    recursive_scan: bool = env_bool("RECURSIVE_SCAN", True)
    preserve_directory_structure: bool = env_bool("PRESERVE_DIRECTORY_STRUCTURE", True)
    skip_dir_names: list[str] = field(default_factory=lambda: env_list("SKIP_DIR_NAMES", DEFAULT_SKIP_DIR_NAMES))

    config_path: Path = env_path("CONFIG_PATH", "/config/config.json")
    db_path: Path = env_path("DB_PATH", "/config/livephoto-worker.sqlite3")
    motionphoto2_python: str = os.getenv("MOTIONPHOTO2_PYTHON", "python")
    motionphoto2_script: Path = env_path("MOTIONPHOTO2_SCRIPT", "/opt/MotionPhoto2/motionphoto2.py")
    motionphoto2_bin: str = os.getenv("MOTIONPHOTO2_BIN", "")
    process_timeout_seconds: int = env_int("PROCESS_TIMEOUT_SECONDS", 3600)
    motionphoto2_verbose: bool = env_bool("MOTIONPHOTO2_VERBOSE", False)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    web_host: str = os.getenv("WEB_HOST", "0.0.0.0")
    web_port: int = env_int("WEB_PORT", 8011)

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings":
        settings = cls()
        if config_path is not None:
            settings.config_path = config_path
        settings.apply_config_file()
        return settings

    def apply_config_file(self) -> None:
        if not self.config_path.exists():
            return
        with self.config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError(f"Config file must contain a JSON object: {self.config_path}")
        self.apply_config(raw)

    def apply_config(self, raw: dict[str, Any]) -> None:
        if "input_dir" in raw:
            self.input_dir = _coerce_path(raw["input_dir"])
        if "output_dir" in raw:
            self.output_dir = _coerce_path(raw["output_dir"])
        if "archive_dir" in raw:
            self.archive_dir = _coerce_path(raw["archive_dir"])
        if "failed_dir" in raw:
            self.failed_dir = _coerce_path(raw["failed_dir"])
        if "stable_seconds" in raw:
            self.stable_seconds = float(raw["stable_seconds"])
        if "poll_interval" in raw:
            self.poll_interval = float(raw["poll_interval"])
        if "move_originals" in raw:
            self.move_originals = _coerce_bool(raw["move_originals"])
        if "enable_archive" in raw:
            self.enable_archive = _coerce_bool(raw["enable_archive"])
        if "recursive_scan" in raw:
            self.recursive_scan = _coerce_bool(raw["recursive_scan"])
        if "preserve_directory_structure" in raw:
            self.preserve_directory_structure = _coerce_bool(raw["preserve_directory_structure"])
        if "skip_dir_names" in raw:
            self.skip_dir_names = _coerce_str_list(raw["skip_dir_names"])

    def to_config_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            key: str(data[key]) if key.endswith("_dir") else data[key]
            for key in CONFIG_KEYS
        }

    def save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self.to_config_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def update_from_config(self, new_config: dict[str, Any], *, save: bool = True) -> None:
        self.apply_config(new_config)
        self.ensure_directories()
        if save:
            self.save_config()

    @property
    def transfer_settle_seconds(self) -> float:
        return self.stable_seconds

    @property
    def watch_interval_seconds(self) -> float:
        return self.poll_interval

    def ensure_directories(self) -> None:
        directories = [
            self.input_dir,
            self.output_dir,
            self.failed_dir,
            self.db_path.parent,
            self.config_path.parent,
        ]
        if self.enable_archive:
            directories.append(self.archive_dir)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def motionphoto2_base_command(self) -> list[str]:
        if self.motionphoto2_bin:
            return [self.motionphoto2_bin]
        return [self.motionphoto2_python, str(self.motionphoto2_script)]

    def build_motionphoto2_command(self, *, image_path: Path, video_path: Path, output_path: Path) -> list[str]:
        command = [
            *self.motionphoto2_base_command(),
            "--input-image",
            str(image_path),
            "--input-video",
            str(video_path),
            "--output-file",
            str(output_path),
        ]
        if self.motionphoto2_verbose:
            command.append("--verbose")
        return command

    def build_motionphoto2_help_command(self) -> list[str]:
        return [*self.motionphoto2_base_command(), "--help"]
