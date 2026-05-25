from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
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
}


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

    config_path: Path = env_path("CONFIG_PATH", "/config/config.json")
    db_path: Path = env_path("DB_PATH", "/config/livephoto-worker.sqlite3")
    motionphoto2_bin: str = os.getenv("MOTIONPHOTO2_BIN", "/usr/local/bin/motionphoto2")
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
