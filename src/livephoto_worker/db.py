from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import MediaPair, PairHashes


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

    def stats(self) -> dict[str, Any]:
        with self.lock:
            processed_count = self.conn.execute("SELECT COUNT(*) AS count FROM processed_pairs").fetchone()["count"]
            job_rows = self.conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
            ).fetchall()
            total_jobs = self.conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
            latest = self.conn.execute("SELECT created_at FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "processed_count": processed_count,
            "total_jobs": total_jobs,
            "by_status": {row["status"]: row["count"] for row in job_rows},
            "latest_job_at": latest["created_at"] if latest is not None else None,
        }
