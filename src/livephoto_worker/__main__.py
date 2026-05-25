from __future__ import annotations

import logging
import signal
import sys
import threading

from .db import ProcessingStore
from .logging_buffer import RecentLogHandler
from .processor import PairProcessor
from .settings import Settings
from .web import create_app
from .worker import LivePhotoWorker


def configure_logging(level: str) -> RecentLogHandler:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    recent_handler = RecentLogHandler(capacity=500)
    recent_handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(stream_handler)
    root.addHandler(recent_handler)
    return recent_handler


def main() -> int:
    settings = Settings.load()
    recent_log_handler = configure_logging(settings.log_level)
    settings.ensure_directories()

    logger = logging.getLogger(__name__)
    logger.info("Loaded config from %s", settings.config_path)

    store = ProcessingStore(settings.db_path)
    processor = PairProcessor(settings=settings, store=store)
    worker = LivePhotoWorker(settings=settings, processor=processor)
    worker_thread = threading.Thread(target=worker.run_forever, name="livephoto-worker", daemon=True)
    app = create_app(settings=settings, worker=worker, store=store, log_handler=recent_log_handler)

    def handle_signal(signum: int, _frame: object) -> None:
        logger.info("Received signal %s", signum)
        worker.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    worker_thread.start()
    try:
        from waitress import serve

        logger.info("Starting production Web UI on %s:%s", settings.web_host, settings.web_port)
        serve(app, host=settings.web_host, port=settings.web_port, threads=8)
    finally:
        worker.stop()
        worker_thread.join(timeout=5)
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
