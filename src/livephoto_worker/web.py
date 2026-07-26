from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, redirect, render_template, request, url_for

from .db import ProcessingStore
from .diagnostics import MotionPhoto2DiagnosticResult, MotionPhoto2Status, run_motionphoto2_diagnostic
from .logging_buffer import RecentLogHandler
from .settings import Settings
from .worker import LivePhotoWorker

APP_VERSION = "0.1.3"
GITHUB_URL = "https://github.com/dttxorg/livemotion"
DEFAULT_DIRECTORY_PRESET = {
    "input_dir": "/photos/live_inbox",
    "output_dir": "/photos/motion_output",
    "archive_dir": "/photos/archive",
    "failed_dir": "/photos/failed",
}
STATUS_LABELS = {
    "success": "成功合成",
    "failed": "失败",
    "skipped_duplicate": "已跳过重复",
    "copied_photo": "已复制照片",
    "copied_video": "已复制视频",
}
CANDIDATE_TYPE_LABELS = {
    "live_photo": "Live Photo 文件对",
    "photo": "普通照片",
    "video": "普通视频",
    "media": "媒体文件",
}
CANDIDATE_REASON_LABELS = {
    "waiting_for_stable": "等待文件稳定",
    "file_changed_reset_timer": "文件仍在变化，已重新计时",
    "ready": "文件已稳定，等待处理",
    "force": "已请求强制处理",
    "missing_pair": "文件对不完整",
    "already_processed": "已处理",
    "conversion_failed": "处理失败",
}
LOG_LEVEL_BADGES = {
    "DEBUG": "text-bg-secondary",
    "INFO": "text-bg-info",
    "WARNING": "text-bg-warning",
    "ERROR": "text-bg-danger",
    "CRITICAL": "text-bg-danger",
}
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="18" fill="#2563eb"/><path d="M25 18l23 14-23 14z" fill="#fff"/><circle cx="20" cy="20" r="5" fill="#93c5fd"/></svg>"""


def _form_to_config(form: Any) -> dict[str, Any]:
    return {
        "input_dir": form.get("input_dir", "").strip(),
        "output_dir": form.get("output_dir", "").strip(),
        "archive_dir": form.get("archive_dir", "").strip(),
        "failed_dir": form.get("failed_dir", "").strip(),
        "stable_seconds": float(form.get("stable_seconds", 30)),
        "poll_interval": float(form.get("poll_interval", 10)),
        "move_originals": "move_originals" in form,
        "enable_archive": "enable_archive" in form,
        "recursive_scan": "recursive_scan" in form,
        "preserve_directory_structure": "preserve_directory_structure" in form,
        "skip_dir_names": [
            line.strip()
            for line in form.get("skip_dir_names", "").replace(",", "\n").splitlines()
            if line.strip()
        ],
    }


def _strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def _log_entries(logs: list[str]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in logs:
        clean = _strip_ansi(line)
        match = LOG_LEVEL_RE.search(clean)
        level = match.group(1) if match else "INFO"
        entries.append(
            {
                "level": level,
                "badge_class": LOG_LEVEL_BADGES.get(level, "text-bg-secondary"),
                "message": clean,
            }
        )
    return entries


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _status_badge(status: str) -> str:
    if status == "success":
        return "text-bg-success"
    if status == "failed":
        return "text-bg-danger"
    if status == "skipped_duplicate":
        return "text-bg-secondary"
    if status in {"copied_photo", "copied_video"}:
        return "text-bg-primary"
    return "text-bg-info"


def _queue_count(worker: LivePhotoWorker) -> int:
    pending = getattr(worker, "pending", None)
    if pending is None:
        return 0
    scan_lock = getattr(worker, "scan_lock", None)
    if scan_lock is None:
        return len(pending)
    with scan_lock:
        return len(pending)


def _format_timestamp(value: float | int | None) -> str:
    if value is None:
        return "暂无等待"
    return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")


def _format_job_timestamp(value: object | None) -> str:
    if not value:
        return "暂无记录"
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone()
    return timestamp.strftime("%Y-%m-%d %H:%M")


def _pending_status(worker: LivePhotoWorker) -> dict[str, str | int | float | None]:
    status_method = getattr(worker, "pending_status", None)
    if callable(status_method):
        status = dict(status_method())
    else:
        status = {
            "waiting_count": _queue_count(worker),
            "waiting_live_pairs": 0,
            "earliest_first_seen_at": None,
            "next_process_at": None,
            "oldest_wait_seconds": 0,
        }
    status["earliest_first_seen_text"] = _format_timestamp(status.get("earliest_first_seen_at"))  # type: ignore[arg-type]
    status["next_process_text"] = _format_timestamp(status.get("next_process_at"))  # type: ignore[arg-type]
    return status


def _candidate_rows(worker: LivePhotoWorker) -> list[dict[str, object]]:
    rows_method = getattr(worker, "candidate_debug_rows", None)
    if not callable(rows_method):
        return []
    rows = []
    for row in rows_method():
        copied = dict(row)
        copied["next_process_text"] = _format_timestamp(copied.get("next_process_at"))  # type: ignore[arg-type]
        candidate_type = str(copied.get("candidate_type", "media"))
        reason = str(copied.get("reason", "waiting_for_stable"))
        copied["candidate_type_label"] = CANDIDATE_TYPE_LABELS.get(candidate_type, candidate_type)
        copied["reason_label"] = CANDIDATE_REASON_LABELS.get(reason, reason)
        rows.append(copied)
    return rows


def _dir_fields(settings: Settings) -> list[dict[str, str]]:
    return [
        {
            "name": "input_dir",
            "label": "Live Photo 输入目录",
            "value": str(settings.input_dir),
            "help": "同步工具写入照片和视频文件的位置。",
        },
        {
            "name": "output_dir",
            "label": "Pixel 同步输出目录",
            "value": str(settings.output_dir),
            "help": "Pixel 或 Syncthing 只需要同步此整理后的目录。",
        },
        {
            "name": "archive_dir",
            "label": "原始文件归档目录",
            "value": str(settings.archive_dir),
            "help": "开启归档且移动原文件时的目标目录。",
        },
        {
            "name": "failed_dir",
            "label": "失败文件目录",
            "value": str(settings.failed_dir),
            "help": "处理失败的文件会移动到这里，避免重复尝试。",
        },
    ]


def create_app(
    *,
    settings: Settings,
    worker: LivePhotoWorker,
    store: ProcessingStore,
    log_handler: RecentLogHandler,
    motionphoto2_status: MotionPhoto2Status | None = None,
    diagnostic_runner: Callable[[Path, Path], MotionPhoto2DiagnosticResult] | None = None,
) -> Flask:
    app = Flask(__name__)
    if motionphoto2_status is None:
        motionphoto2_status = MotionPhoto2Status(
            available=True,
            command=settings.build_motionphoto2_help_command(),
            returncode=0,
            stdout="",
            stderr="",
        )

    def run_diagnostic(image_path: Path, video_path: Path) -> MotionPhoto2DiagnosticResult:
        if diagnostic_runner is not None:
            return diagnostic_runner(image_path, video_path)
        return run_motionphoto2_diagnostic(settings=settings, image_path=image_path, video_path=video_path)

    def render_page(
        template_name: str,
        page: str,
        title: str,
        *,
        message: str = "",
        diagnostic_image: str = "/photos/2026/5/IMG_0056.HEIC",
        diagnostic_video: str = "/photos/2026/5/IMG_0056.MOV",
        diagnostic_result: dict[str, object] | None = None,
    ) -> str:
        stats = store.stats()
        recent_jobs = store.recent_jobs(limit=20)
        for job in recent_jobs:
            job["created_at_text"] = _format_job_timestamp(job.get("created_at"))
        log_entries = _log_entries(log_handler.recent(limit=120))
        trend = stats.get("seven_day_trend", [])
        trend_max = max(
            (max(int(point["completed"]), int(point["failed"])) for point in trend),
            default=1,
        )
        return render_template(
            template_name,
            page=page,
            title=title,
            settings=settings,
            stats=stats,
            recent_jobs=recent_jobs,
            dashboard_jobs=recent_jobs[:5],
            latest_job_text=_format_job_timestamp(stats.get("latest_job_at")),
            log_entries=log_entries,
            pending_status=_pending_status(worker),
            candidate_rows=_candidate_rows(worker),
            app_version=APP_VERSION,
            github_url=GITHUB_URL,
            message=message,
            motionphoto2_status=motionphoto2_status,
            diagnostic_image=diagnostic_image,
            diagnostic_video=diagnostic_video,
            diagnostic_result=diagnostic_result,
            dir_fields=_dir_fields(settings),
            default_directories=DEFAULT_DIRECTORY_PRESET,
            status_label=_status_label,
            status_badge=_status_badge,
            trend_max=max(trend_max, 1),
        )

    @app.get("/")
    def index() -> str:
        return render_page("dashboard.html", "dashboard", "控制台", message=request.args.get("message", ""))

    @app.get("/settings")
    def settings_page() -> str:
        return render_page("settings.html", "settings", "设置", message=request.args.get("message", ""))

    @app.get("/logs")
    def logs_page() -> str:
        return render_page("logs.html", "logs", "日志", message=request.args.get("message", ""))

    @app.post("/logs/clear")
    def clear_logs():  # type: ignore[no-untyped-def]
        log_handler.clear()
        return redirect(url_for("logs_page", message="日志已清空"))

    @app.get("/stats")
    def stats_page() -> str:
        return render_page("tasks.html", "tasks", "任务记录", message=request.args.get("message", ""))

    @app.get("/about")
    def about_page() -> str:
        return render_page("about.html", "about", "关于")

    @app.get("/debug/candidates")
    def debug_candidates_page() -> str:
        return render_page(
            "candidates.html",
            "debug_candidates",
            "候选队列",
            message=request.args.get("message", ""),
        )

    @app.get("/debug/test-pair")
    def test_pair_page() -> str:
        return render_page(
            "diagnostic.html",
            "diagnostic_pair",
            "文件对诊断",
            diagnostic_image=request.args.get("image", "/photos/2026/5/IMG_0056.HEIC"),
            diagnostic_video=request.args.get("video", "/photos/2026/5/IMG_0056.MOV"),
        )

    @app.get("/favicon.svg")
    def favicon() -> Response:
        return Response(FAVICON_SVG, mimetype="image/svg+xml")

    @app.post("/save")
    def save_config():  # type: ignore[no-untyped-def]
        new_config = _form_to_config(request.form)
        settings.update_from_config(new_config, save=True)
        worker.clear_pending()
        return redirect(url_for("settings_page", message="配置已保存并立即生效"))

    @app.post("/scan")
    def scan_now():  # type: ignore[no-untyped-def]
        processed_count = worker.scan_once()
        return redirect(url_for("index", message=f"已立即扫描，本轮处理 {processed_count} 对文件"))

    @app.post("/scan/force")
    def force_scan_now():  # type: ignore[no-untyped-def]
        force_scan = getattr(worker, "force_scan_once", None)
        if callable(force_scan):
            processed_count = force_scan()
        else:
            processed_count = worker.scan_once()
        return redirect(url_for("index", message=f"已强制扫描并忽略稳定等待，本轮处理 {processed_count} 个候选"))

    @app.post("/debug/test-pair")
    def test_pair_run() -> str:
        image = request.form.get("image", "/photos/2026/5/IMG_0056.HEIC").strip()
        video = request.form.get("video", "/photos/2026/5/IMG_0056.MOV").strip()
        result = run_diagnostic(Path(image), Path(video))
        return render_page(
            "diagnostic.html",
            "diagnostic_pair",
            "文件对诊断",
            diagnostic_image=image,
            diagnostic_video=video,
            diagnostic_result=result.to_dict(),
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
