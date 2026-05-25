from __future__ import annotations

from typing import Any

from flask import Flask, redirect, render_template_string, request, url_for

from .db import ProcessingStore
from .logging_buffer import RecentLogHandler
from .settings import Settings
from .worker import LivePhotoWorker

PAGE_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>livephoto-worker 设置</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; background: #f7f7f8; color: #202124; }
    main { max-width: 1100px; margin: 0 auto; }
    h1 { margin-bottom: 0.25rem; }
    .card { background: white; border: 1px solid #ddd; border-radius: 12px; padding: 1.25rem; margin: 1rem 0; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
    .grid { display: grid; grid-template-columns: minmax(180px, 240px) 1fr; gap: 0.75rem 1rem; align-items: center; }
    label { font-weight: 600; }
    input[type="text"], input[type="number"] { width: 100%; box-sizing: border-box; padding: 0.55rem; border: 1px solid #c8c8c8; border-radius: 8px; font-size: 1rem; }
    input[type="checkbox"] { transform: scale(1.2); }
    button, .button { display: inline-block; border: 0; border-radius: 8px; background: #2563eb; color: white; padding: 0.65rem 1rem; font-size: 1rem; cursor: pointer; text-decoration: none; margin-right: 0.5rem; }
    button.secondary, .button.secondary { background: #4b5563; }
    .message { border-left: 4px solid #16a34a; background: #ecfdf5; padding: 0.75rem 1rem; border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e5e7eb; text-align: left; padding: 0.5rem; vertical-align: top; }
    pre { white-space: pre-wrap; word-break: break-word; background: #111827; color: #e5e7eb; border-radius: 8px; padding: 1rem; max-height: 420px; overflow: auto; }
    .muted { color: #6b7280; }
  </style>
</head>
<body>
<main>
  <h1>livephoto-worker 设置</h1>
  <p class="muted">配置文件：{{ settings.config_path }}</p>
  {% if message %}<p class="message">{{ message }}</p>{% endif %}

  <section class="card">
    <h2>配置</h2>
    <form method="post" action="{{ url_for('save_config') }}">
      <div class="grid">
        <label for="input_dir">input_dir</label>
        <input id="input_dir" name="input_dir" type="text" value="{{ settings.input_dir }}" required>

        <label for="output_dir">output_dir</label>
        <input id="output_dir" name="output_dir" type="text" value="{{ settings.output_dir }}" required>

        <label for="archive_dir">archive_dir</label>
        <input id="archive_dir" name="archive_dir" type="text" value="{{ settings.archive_dir }}" required>

        <label for="failed_dir">failed_dir</label>
        <input id="failed_dir" name="failed_dir" type="text" value="{{ settings.failed_dir }}" required>

        <label for="stable_seconds">stable_seconds</label>
        <input id="stable_seconds" name="stable_seconds" type="number" min="0" step="0.1" value="{{ settings.stable_seconds }}" required>

        <label for="poll_interval">poll_interval</label>
        <input id="poll_interval" name="poll_interval" type="number" min="1" step="0.1" value="{{ settings.poll_interval }}" required>

        <label for="move_originals">move_originals</label>
        <input id="move_originals" name="move_originals" type="checkbox" value="1" {% if settings.move_originals %}checked{% endif %}>

        <label for="enable_archive">enable_archive</label>
        <input id="enable_archive" name="enable_archive" type="checkbox" value="1" {% if settings.enable_archive %}checked{% endif %}>
      </div>
      <p style="margin-top: 1rem;"><button type="submit">保存配置</button></p>
    </form>
  </section>

  <section class="card">
    <h2>操作</h2>
    <form method="post" action="{{ url_for('scan_now') }}" style="display: inline;"><button type="submit">立即扫描一次</button></form>
    <a class="button secondary" href="#logs">查看最近日志</a>
    <a class="button secondary" href="#stats">查看处理统计</a>
  </section>

  <section id="stats" class="card">
    <h2>处理统计</h2>
    <table>
      <tr><th>已成功处理去重文件对</th><td>{{ stats.processed_count }}</td></tr>
      <tr><th>任务记录总数</th><td>{{ stats.total_jobs }}</td></tr>
      <tr><th>最近任务时间</th><td>{{ stats.latest_job_at or '-' }}</td></tr>
      <tr><th>按状态统计</th><td>{{ stats.by_status }}</td></tr>
    </table>
    <h3>最近任务</h3>
    <table>
      <thead><tr><th>时间</th><th>状态</th><th>图片</th><th>视频</th><th>输出/错误</th></tr></thead>
      <tbody>
        {% for job in recent_jobs %}
        <tr>
          <td>{{ job.created_at }}</td>
          <td>{{ job.status }}</td>
          <td>{{ job.image_name }}</td>
          <td>{{ job.video_name }}</td>
          <td>{% if job.error %}{{ job.error }}{% else %}{{ job.output_path or '-' }}{% endif %}</td>
        </tr>
        {% else %}
        <tr><td colspan="5" class="muted">暂无任务记录</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </section>

  <section id="logs" class="card">
    <h2>最近日志</h2>
    <pre>{% for line in logs %}{{ line }}
{% else %}暂无日志{% endfor %}</pre>
  </section>
</main>
</body>
</html>
"""


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
    }


def create_app(
    *,
    settings: Settings,
    worker: LivePhotoWorker,
    store: ProcessingStore,
    log_handler: RecentLogHandler,
) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return render_template_string(
            PAGE_TEMPLATE,
            settings=settings,
            stats=store.stats(),
            recent_jobs=store.recent_jobs(limit=20),
            logs=log_handler.recent(limit=120),
            message=request.args.get("message", ""),
        )

    @app.post("/save")
    def save_config():  # type: ignore[no-untyped-def]
        new_config = _form_to_config(request.form)
        settings.update_from_config(new_config, save=True)
        worker.clear_pending()
        return redirect(url_for("index", message="配置已保存并立即生效"))

    @app.post("/scan")
    def scan_now():  # type: ignore[no-untyped-def]
        processed_count = worker.scan_once()
        return redirect(url_for("index", message=f"已立即扫描一次，本轮处理 {processed_count} 对文件"))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
