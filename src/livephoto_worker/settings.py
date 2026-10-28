from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
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
DIRECTORY_LABELS = {
    "input_dir": "Live Photo 输入目录",
    "output_dir": "Pixel 同步输出目录",
    "archive_dir": "原始文件归档目录",
    "failed_dir": "失败文件目录",
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


class SettingsValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("；".join(errors.values()))


def _coerce_path(value: Any) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError("请填写非空的绝对目录路径。")
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("请填写绝对目录路径，例如 /photos/live_inbox。")
    # resolve also rejects malformed paths and lets alias checks include symlinks.
    path.resolve(strict=False)
    if path.exists() and not path.is_dir():
        raise ValueError("此路径已被文件占用，请选择目录。")
    return path


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

    def __post_init__(self) -> None:
        self.apply_config({})

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
        self._apply_config_values(self._validated_config_values(raw))

    def _validated_config_values(self, raw: dict[str, Any]) -> dict[str, Any]:
        values = {key: raw.get(key, getattr(self, key)) for key in CONFIG_KEYS}
        errors: dict[str, str] = {}
        resolved_paths: dict[str, Path] = {}
        for key, label in DIRECTORY_LABELS.items():
            try:
                values[key] = _coerce_path(values[key])
                resolved_paths[key] = values[key].resolve(strict=False)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors[key] = f"{label}：{exc}"

        for key, label, minimum_allowed in (
            ("stable_seconds", "文件稳定等待时间", True),
            ("poll_interval", "扫描间隔", False),
        ):
            try:
                number = float(values[key])
                if not math.isfinite(number) or number < 0 or (not minimum_allowed and number == 0):
                    raise ValueError
                values[key] = number
            except (TypeError, ValueError, OverflowError):
                comparison = "大于或等于 0" if minimum_allowed else "大于 0"
                errors[key] = f"{label}须为{comparison} 的有限数字。"

        input_path = resolved_paths.get("input_dir")
        destinations = ("output_dir", "archive_dir", "failed_dir")
        for key in destinations:
            path = resolved_paths.get(key)
            if input_path is not None and path is not None and input_path.is_relative_to(path):
                errors[key] = f"{DIRECTORY_LABELS[key]}须与输入目录不同，且不包含输入目录。"
        for index, key in enumerate(destinations):
            path = resolved_paths.get(key)
            if path is None:
                continue
            for other_key in destinations[index + 1:]:
                other_path = resolved_paths.get(other_key)
                if other_path is not None and (path.is_relative_to(other_path) or other_path.is_relative_to(path)):
                    message = f"{DIRECTORY_LABELS[key]}与{DIRECTORY_LABELS[other_key]}须分别独立，避免相同或互相包含。"
                    errors[key] = message
                    errors[other_key] = message

        for key in ("move_originals", "enable_archive", "recursive_scan", "preserve_directory_structure"):
            values[key] = _coerce_bool(values[key])
        values["skip_dir_names"] = _coerce_str_list(values["skip_dir_names"])
        if errors:
            raise SettingsValidationError(errors)
        return values

    def _apply_config_values(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            setattr(self, key, value)

    def to_config_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            key: str(data[key]) if key.endswith("_dir") else data[key]
            for key in CONFIG_KEYS
        }

    def save_config(self) -> None:
        self._validated_config_values({})
        serialized = json.dumps(
            self.to_config_dict(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        ) + "\n"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.config_path.parent,
                prefix=f".{self.config_path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.config_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def update_from_config(self, new_config: dict[str, Any], *, save: bool = True) -> None:
        values = self._validated_config_values(new_config)
        candidate = replace(self, **values)
        candidate.ensure_directories()
        if save:
            candidate.save_config()
        # Publish only after validation, directory creation and persistence succeed.
        self._apply_config_values(values)

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
