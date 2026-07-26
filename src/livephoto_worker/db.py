from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import MediaItem, MediaPair, PairHashes


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProcessingStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS processed_pairs (
                    pair_signature TEXT PRIMARY KEY,
                    stem TEXT NOT NULL,
                    image_name TEXT NOT NULL,
                    video_name TEXT NOT NULL,
                    image_sha256 TEXT NOT NULL,
                    video_sha256 TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_signature TEXT,
                    stem TEXT NOT NULL,
                    image_name TEXT NOT NULL,
                    video_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output_path TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_files (
                    file_signature TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()

    def is_processed(self, pair_signature: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM processed_pairs WHERE pair_signature = ? LIMIT 1",
                (pair_signature,),
            ).fetchone()
        return row is not None

    def is_file_processed(self, file_signature: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM processed_files WHERE file_signature = ? LIMIT 1",
                (file_signature,),
            ).fetchone()
        return row is not None

    def record_success(self, pair: MediaPair, hashes: PairHashes, output_path: Path) -> None:
        now = utc_now()
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO processed_pairs (
                    pair_signature, stem, image_name, video_name,
                    image_sha256, video_sha256, output_path, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hashes.signature,
                    pair.stem,
                    pair.image_path.name,
                    pair.video_path.name,
                    hashes.image_sha256,
                    hashes.video_sha256,
                    str(output_path),
                    now,
                ),
            )
            self._record_job_unlocked(
                pair=pair,
                pair_signature=hashes.signature,
                status="success",
                output_path=output_path,
                error=None,
            )

    def record_failure(
        self,
        pair: MediaPair,
        pair_signature: str | None,
        error: str,
        output_path: Path | None = None,
    ) -> None:
        self.record_job(
            pair=pair,
            pair_signature=pair_signature,
            status="failed",
            output_path=output_path,
            error=error,
        )

    def record_duplicate(self, pair: MediaPair, hashes: PairHashes) -> None:
        self.record_job(
            pair=pair,
            pair_signature=hashes.signature,
            status="skipped_duplicate",
            output_path=None,
            error=None,
        )

    def record_copied_file(self, item: MediaItem, file_sha256: str, output_path: Path) -> None:
        now = utc_now()
        file_signature = f"sha256:{file_sha256}"
        status = "copied_photo" if item.media_type == "photo" else "copied_video"
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO processed_files (
                    file_signature, media_type, file_name, file_sha256, output_path, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    file_signature,
                    item.media_type,
                    item.path.name,
                    file_sha256,
                    str(output_path),
                    now,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO jobs (
                    pair_signature, stem, image_name, video_name,
                    status, output_path, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_signature,
                    item.path.stem,
                    item.path.name if item.media_type == "photo" else "",
                    item.path.name if item.media_type == "video" else "",
                    status,
                    str(output_path),
                    None,
                    now,
                ),
            )

    def record_skipped_file(self, item: MediaItem, file_signature: str) -> None:
        self._record_media_job(
            item=item,
            file_signature=file_signature,
            status="skipped_duplicate",
            output_path=None,
            error=None,
        )

    def record_media_failure(self, item: MediaItem, file_signature: str | None, error: str) -> None:
        self._record_media_job(
            item=item,
            file_signature=file_signature,
            status="failed",
            output_path=None,
            error=error,
        )

    def _record_media_job(
        self,
        item: MediaItem,
        file_signature: str | None,
        status: str,
        output_path: Path | None,
        error: str | None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO jobs (
                    pair_signature, stem, image_name, video_name,
                    status, output_path, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_signature,
                    item.path.stem,
                    item.path.name if item.media_type == "photo" else "",
                    item.path.name if item.media_type == "video" else "",
                    status,
                    str(output_path) if output_path is not None else None,
                    error,
                    utc_now(),
                ),
            )

    def record_job(
        self,
        pair: MediaPair,
        pair_signature: str | None,
        status: str,
        output_path: Path | None,
        error: str | None,
    ) -> None:
        with self.lock, self.conn:
            self._record_job_unlocked(
                pair=pair,
                pair_signature=pair_signature,
                status=status,
                output_path=output_path,
                error=error,
            )

    def _record_job_unlocked(
        self,
        pair: MediaPair,
        pair_signature: str | None,
        status: str,
        output_path: Path | None,
        error: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO jobs (
                pair_signature, stem, image_name, video_name,
                status, output_path, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pair_signature,
                pair.stem,
                pair.image_path.name,
                pair.video_path.name,
                status,
                str(output_path) if output_path is not None else None,
                error,
                utc_now(),
            ),
        )

    def latest_job(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def seven_day_trend(self, *, days: int = 7, end_date: date | None = None) -> list[dict[str, Any]]:
        """Return a zero-filled local-date activity trend for the dashboard.

        Completed work includes both Live Photo merges and copied ordinary media;
        skipped duplicates stay out of the chart so the two series remain a
        clear operational success/failure signal.
        """
        if days < 1:
            raise ValueError("days must be at least 1")

        final_day = end_date or datetime.now().astimezone().date()
        first_day = final_day - timedelta(days=days - 1)
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT
                    date(created_at, 'localtime') AS day,
                    SUM(CASE WHEN status IN ('success', 'copied_photo', 'copied_video') THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM jobs
                WHERE date(created_at, 'localtime') BETWEEN ? AND ?
                GROUP BY date(created_at, 'localtime')
                ORDER BY day
                """,
                (first_day.isoformat(), final_day.isoformat()),
            ).fetchall()

        by_day = {
            row["day"]: {
                "completed": int(row["completed"] or 0),
                "failed": int(row["failed"] or 0),
            }
            for row in rows
        }
        trend: list[dict[str, Any]] = []
        for offset in range(days):
            current_day = first_day + timedelta(days=offset)
            values = by_day.get(current_day.isoformat(), {"completed": 0, "failed": 0})
            trend.append(
                {
                    "date": current_day.isoformat(),
                    "label": f"{current_day.month}/{current_day.day}",
                    "completed": values["completed"],
                    "failed": values["failed"],
                }
            )
        return trend

    def stats(self) -> dict[str, Any]:
        with self.lock:
            processed_count = self.conn.execute("SELECT COUNT(*) AS count FROM processed_pairs").fetchone()["count"]
            job_rows = self.conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
            ).fetchall()
            total_jobs = self.conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
            latest = self.conn.execute("SELECT created_at FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            success_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status = 'success'"
            ).fetchone()["count"]
            failed_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status = 'failed'"
            ).fetchone()["count"]
            copied_photo_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status = 'copied_photo'"
            ).fetchone()["count"]
            copied_video_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status = 'copied_video'"
            ).fetchone()["count"]
            skipped_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status = 'skipped_duplicate'"
            ).fetchone()["count"]
            today_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE date(created_at, 'localtime') = date('now', 'localtime')",
            ).fetchone()["count"]
            today_completed_count = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM jobs
                WHERE date(created_at, 'localtime') = date('now', 'localtime')
                    AND status IN ('success', 'copied_photo', 'copied_video')
                """,
            ).fetchone()["count"]
            today_failed_count = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM jobs
                WHERE date(created_at, 'localtime') = date('now', 'localtime')
                    AND status = 'failed'
                """,
            ).fetchone()["count"]
            latest_file = self.conn.execute(
                """
                SELECT image_name, video_name, output_path, status, created_at
                FROM jobs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return {
            "processed_count": processed_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "copied_photo_count": copied_photo_count,
            "copied_video_count": copied_video_count,
            "skipped_count": skipped_count,
            "today_count": today_count,
            "completed_count": success_count + copied_photo_count + copied_video_count,
            "today_completed_count": today_completed_count,
            "today_failed_count": today_failed_count,
            "total_jobs": total_jobs,
            "by_status": {row["status"]: row["count"] for row in job_rows},
            "latest_job_at": latest["created_at"] if latest is not None else None,
            "latest_processed_file": dict(latest_file) if latest_file is not None else None,
            "seven_day_trend": self.seven_day_trend(),
        }
