from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, redirect, render_template_string, request, url_for

from .db import ProcessingStore
from .diagnostics import MotionPhoto2DiagnosticResult, run_motionphoto2_diagnostic
from .logging_buffer import RecentLogHandler
from .settings import Settings
from .worker import LivePhotoWorker

APP_VERSION = "0.1.0"
GITHUB_URL = "https://github.com/dttxorg/livemotion"
COMMON_DIRECTORIES = (
    "/photos/live_inbox",
    "/photos/motion_output",
    "/photos/archive",
    "/photos/failed",
)
STATUS_LABELS = {
    "success": "成功",
    "failed": "失败",
    "skipped_duplicate": "已跳过重复",
    "copied_photo": "已复制照片",
    "copied_video": "已复制视频",
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

PAGE_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} - LiveMotion</title>
  <link rel="icon" href="{{ url_for('favicon') }}" type="image/svg+xml">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    :root {
      --lm-bg: #f3f5f7;
      --lm-panel: #ffffff;
      --lm-border: #e5e7eb;
      --lm-text: #111827;
      --lm-muted: #6b7280;
      --lm-nav: #111827;
      --lm-accent: #2563eb;
      --lm-accent-soft: #e8f0ff;
    }
    body {
      min-height: 100vh;
      background: radial-gradient(circle at top left, #eef4ff 0, #f6f7f9 28rem, var(--lm-bg) 100%);
      color: var(--lm-text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    .app-navbar {
      background: rgba(17, 24, 39, 0.96);
      backdrop-filter: blur(10px);
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.18);
    }
    .brand-mark {
      width: 2.25rem;
      height: 2.25rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 0.85rem;
      background: linear-gradient(135deg, #60a5fa, #2563eb);
      color: #fff;
      font-size: 1.15rem;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.28);
    }
    .page-shell { max-width: 1240px; }
    .hero-card {
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 1.35rem;
      background: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
      box-shadow: 0 18px 45px rgba(15, 23, 42, 0.08);
    }
    .section-card {
      border: 1px solid var(--lm-border);
      border-radius: 1.15rem;
      background: var(--lm-panel);
      box-shadow: 0 10px 28px rgba(15, 23, 42, 0.05);
    }
    .section-title {
      display: flex;
      align-items: center;
      gap: .55rem;
      margin: 0;
      font-size: 1.05rem;
      font-weight: 700;
    }
    .metric-card {
      border: 1px solid #e7edf5;
      border-radius: 1rem;
      padding: 1rem;
      background: #fbfdff;
      min-height: 7.25rem;
    }
    .metric-label { color: var(--lm-muted); font-size: .88rem; }
    .metric-value { font-size: 1.8rem; font-weight: 800; letter-spacing: -.03em; }
    .metric-small { color: var(--lm-muted); font-size: .85rem; overflow-wrap: anywhere; }
    .config-label { font-weight: 650; color: #263244; }
    .form-control, .form-check-input { border-color: #d5dbe5; }
    .form-control:focus, .form-check-input:focus {
      border-color: #8bb7ff;
      box-shadow: 0 0 0 .2rem rgba(37, 99, 235, .12);
    }
    .quick-dir button {
      --bs-btn-padding-y: .22rem;
      --bs-btn-padding-x: .55rem;
      --bs-btn-font-size: .78rem;
    }
    .table thead th { color: #526071; font-size: .82rem; text-transform: none; white-space: nowrap; }
    .text-path { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .88rem; }
    .log-panel {
      max-height: 34rem;
      overflow: auto;
      border: 1px solid #dbe3ee;
      border-radius: 1rem;
      background: #0f172a;
      padding: .4rem;
    }
    .log-row {
      display: grid;
      grid-template-columns: 5.75rem 1fr;
      gap: .75rem;
      align-items: start;
      color: #dbeafe;
      padding: .55rem .7rem;
      border-bottom: 1px solid rgba(148, 163, 184, .15);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: .86rem;
    }
    .log-row:last-child { border-bottom: 0; }
    .log-message { white-space: pre-wrap; word-break: break-word; }
    .diagnostic-output {
      max-height: 18rem;
      overflow: auto;
      border: 1px solid #dbe3ee;
      border-radius: .85rem;
      background: #0f172a;
      color: #dbeafe;
      padding: 1rem;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: .86rem;
    }
    .empty-state {
      border: 1px dashed #cad4e2;
      border-radius: 1rem;
      color: var(--lm-muted);
      background: #f8fafc;
      padding: 1.2rem;
      text-align: center;
    }
    .about-logo {
      width: 5rem;
      height: 5rem;
      border-radius: 1.5rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, #60a5fa, #1d4ed8);
      color: #fff;
      font-size: 2.5rem;
      box-shadow: 0 16px 36px rgba(37, 99, 235, .24);
    }
    .muted { color: var(--lm-muted); }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark app-navbar sticky-top">
  <div class="container-fluid page-shell py-1">
    <a class="navbar-brand d-flex align-items-center gap-2 fw-bold" href="{{ url_for('index') }}">
      <span class="brand-mark">▶</span>
      <span>LiveMotion</span>
    </a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#topNav" aria-controls="topNav" aria-expanded="false" aria-label="切换导航">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div id="topNav" class="collapse navbar-collapse">
      <ul class="navbar-nav ms-auto gap-lg-1">
        <li class="nav-item"><a class="nav-link {% if page == 'dashboard' %}active{% endif %}" href="{{ url_for('index') }}">控制台</a></li>
        <li class="nav-item"><a class="nav-link {% if page == 'stats' %}active{% endif %}" href="{{ url_for('stats_page') }}">查看统计</a></li>
        <li class="nav-item"><a class="nav-link {% if page == 'logs' %}active{% endif %}" href="{{ url_for('logs_page') }}">查看日志</a></li>
        <li class="nav-item"><a class="nav-link {% if page == 'debug_candidates' %}active{% endif %}" href="{{ url_for('debug_candidates_page') }}">候选调试</a></li>
        <li class="nav-item"><a class="nav-link {% if page == 'diagnostic_pair' %}active{% endif %}" href="{{ url_for('test_pair_page') }}">诊断</a></li>
        <li class="nav-item"><a class="nav-link {% if page == 'about' %}active{% endif %}" href="{{ url_for('about_page') }}">关于</a></li>
      </ul>
    </div>
  </div>
</nav>

<main class="container-fluid page-shell py-4 py-lg-5">
  {% if message %}
  <div class="alert alert-success border-0 shadow-sm rounded-4" role="status">{{ message }}</div>
  {% endif %}

  {% if page == 'dashboard' %}
  <section class="hero-card p-4 p-lg-5 mb-4">
    <div class="row align-items-center g-4">
      <div class="col-lg-8">
        <div class="d-flex align-items-center gap-3 mb-3">
          <span class="brand-mark" style="width:3rem;height:3rem;font-size:1.45rem;">▶</span>
          <div>
            <p class="text-primary fw-semibold mb-1">Live Photo 自动转换服务</p>
            <h1 class="display-6 fw-bold mb-0">LiveMotion 控制台</h1>
          </div>
        </div>
        <p class="lead text-secondary mb-0">监听完整照片库，自动合并 Live Photo，并把普通照片/视频整理到 Pixel 同步输出目录。Pixel 只需要同步此输出目录。</p>
      </div>
      <div class="col-lg-4 text-lg-end">
        <form method="post" action="{{ url_for('scan_now') }}" class="d-inline">
          <button type="submit" class="btn btn-primary btn-lg rounded-3">⚡ 立即扫描</button>
        </form>
        <form method="post" action="{{ url_for('force_scan_now') }}" class="d-inline">
          <button type="submit" class="btn btn-warning btn-lg rounded-3 ms-2">🚀 强制扫描</button>
        </form>
        <a class="btn btn-outline-primary btn-lg rounded-3 mt-2 mt-lg-0 ms-lg-2" href="{{ url_for('test_pair_page') }}">🧪 测试指定文件对</a>
      </div>
    </div>
    <div class="alert alert-info border-0 rounded-4 mt-4 mb-0">首次扫描大目录时，建议使用强制扫描。</div>
  </section>

  <section class="section-card p-4 mb-4">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h2 class="section-title">📡 系统状态</h2>
      <span class="badge text-bg-success rounded-pill px-3 py-2">运行中</span>
    </div>
    <div class="row g-3">
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已处理文件数</div><div class="metric-value">{{ stats.processed_count }}</div><div class="metric-small">已成功去重的文件对</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">失败文件数</div><div class="metric-value text-danger">{{ stats.failed_count }}</div><div class="metric-small">需要人工检查</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">最近处理时间</div><div class="metric-value fs-6 mt-2">{{ stats.latest_job_at or '暂无记录' }}</div><div class="metric-small">任务记录更新时间</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">当前监听目录</div><div class="metric-value fs-6 mt-2 text-path">{{ settings.input_dir }}</div><div class="metric-small">Pixel 只同步输出目录</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">等待稳定的文件数</div><div class="metric-value">{{ pending_status.waiting_count }}</div><div class="metric-small">当前等待队列</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">等待稳定的 Live Photo 数</div><div class="metric-value">{{ pending_status.waiting_live_pairs }}</div><div class="metric-small">等待稳定的文件对</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">当前最早等待时间</div><div class="metric-value fs-6 mt-2">{{ pending_status.earliest_first_seen_text }}</div><div class="metric-small">最早进入等待队列</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">下次预计处理时间</div><div class="metric-value fs-6 mt-2">{{ pending_status.next_process_text }}</div><div class="metric-small">可用强制扫描跳过等待</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">扫描目录数</div><div class="metric-value">{{ scan_stats.scanned_dirs }}</div><div class="metric-small">最近一次扫描</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">扫描文件数</div><div class="metric-value">{{ scan_stats.scanned_files }}</div><div class="metric-small">最近一次扫描</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">跳过目录数</div><div class="metric-value">{{ scan_stats.skipped_dirs }}</div><div class="metric-small">已排除输出/归档/特殊目录</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已合并 Live Photo 数量</div><div class="metric-value">{{ stats.success_count }}</div><div class="metric-small">已生成 Motion Photo 单文件</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已复制普通照片数量</div><div class="metric-value">{{ stats.copied_photo_count }}</div><div class="metric-small">已进入 Pixel 同步输出目录</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已复制普通视频数量</div><div class="metric-value">{{ stats.copied_video_count }}</div><div class="metric-small">已进入 Pixel 同步输出目录</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已跳过数量</div><div class="metric-value">{{ stats.skipped_count }}</div><div class="metric-small">已处理过或重复内容</div></div></div>
    </div>
  </section>

  <section class="section-card p-4 mb-4">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h2 class="section-title">⚙️ 当前配置</h2>
      <span class="text-secondary small">配置文件：<span class="text-path">{{ settings.config_path }}</span></span>
    </div>
    {{ config_form|safe }}
  </section>

  <div class="row g-4">
    <div class="col-xl-7">
      <section class="section-card p-4 h-100">
        <div class="d-flex justify-content-between align-items-center mb-3">
          <h2 class="section-title">🧾 最近任务</h2>
          <a class="btn btn-sm btn-outline-secondary" href="{{ url_for('stats_page') }}">查看统计</a>
        </div>
        {{ jobs_table|safe }}
      </section>
    </div>
    <div class="col-xl-5">
      <section class="section-card p-4 h-100">
        <div class="d-flex justify-content-between align-items-center mb-3">
          <h2 class="section-title">📜 最近日志</h2>
          <a class="btn btn-sm btn-outline-secondary" href="{{ url_for('logs_page') }}">查看日志</a>
        </div>
        {{ logs_panel|safe }}
      </section>
    </div>
  </div>
  {% elif page == 'logs' %}
  <section class="section-card p-4">
    <div class="d-flex flex-column flex-md-row justify-content-between gap-3 align-items-md-center mb-3">
      <div>
        <h1 class="h3 fw-bold mb-1">📜 查看日志</h1>
        <p class="text-secondary mb-0">显示最近运行日志，已自动过滤 ANSI 颜色转义字符。</p>
      </div>
      <form method="post" action="{{ url_for('clear_logs') }}">
        <button type="submit" class="btn btn-outline-danger rounded-3">清空日志</button>
      </form>
    </div>
    {{ logs_panel|safe }}
  </section>
  {% elif page == 'stats' %}
  <section class="section-card p-4 mb-4">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <div>
        <h1 class="h3 fw-bold mb-1">📊 查看统计</h1>
        <p class="text-secondary mb-0">转换结果、今日任务与最近处理文件。</p>
      </div>
      <div class="d-flex gap-2">
        <form method="post" action="{{ url_for('scan_now') }}"><button type="submit" class="btn btn-primary rounded-3">立即扫描</button></form>
        <form method="post" action="{{ url_for('force_scan_now') }}"><button type="submit" class="btn btn-warning rounded-3">强制扫描并忽略稳定等待</button></form>
      </div>
    </div>
    <div class="row g-3">
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已合并 Live Photo 数量</div><div class="metric-value text-success">{{ stats.success_count }}</div><div class="metric-small">成功合成 Motion Photo</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">失败数量</div><div class="metric-value text-danger">{{ stats.failed_count }}</div><div class="metric-small">失败文件会移动到失败目录</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">今日处理数量</div><div class="metric-value">{{ stats.today_count }}</div><div class="metric-small">按 NAS 本地日期统计</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">等待稳定的文件数</div><div class="metric-value">{{ pending_status.waiting_count }}</div><div class="metric-small">Live Photo 和普通媒体</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">等待稳定的 Live Photo 数</div><div class="metric-value">{{ pending_status.waiting_live_pairs }}</div><div class="metric-small">等待稳定的文件对</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">扫描目录数</div><div class="metric-value">{{ scan_stats.scanned_dirs }}</div><div class="metric-small">最近一次扫描</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">扫描文件数</div><div class="metric-value">{{ scan_stats.scanned_files }}</div><div class="metric-small">最近一次扫描</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">跳过目录数</div><div class="metric-value">{{ scan_stats.skipped_dirs }}</div><div class="metric-small">最近一次扫描</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已跳过数量</div><div class="metric-value">{{ stats.skipped_count }}</div><div class="metric-small">已处理过或重复内容</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已复制普通照片数量</div><div class="metric-value">{{ stats.copied_photo_count }}</div><div class="metric-small">已复制到 Pixel 同步输出目录</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">已复制普通视频数量</div><div class="metric-value">{{ stats.copied_video_count }}</div><div class="metric-small">已复制到 Pixel 同步输出目录</div></div></div>
    </div>
  </section>
  <section class="section-card p-4 mb-4">
    <h2 class="section-title mb-3">📁 最近处理文件</h2>
    {% if stats.latest_processed_file %}
    <div class="row g-3">
      <div class="col-md-6"><div class="metric-card"><div class="metric-label">图片文件</div><div class="metric-small text-path mt-2">{{ stats.latest_processed_file.image_name }}</div></div></div>
      <div class="col-md-6"><div class="metric-card"><div class="metric-label">视频文件</div><div class="metric-small text-path mt-2">{{ stats.latest_processed_file.video_name }}</div></div></div>
      <div class="col-12"><div class="metric-card"><div class="metric-label">输出路径 / 状态</div><div class="metric-small text-path mt-2">{{ stats.latest_processed_file.output_path or stats.latest_processed_file.status }}</div></div></div>
    </div>
    {% else %}
    <div class="empty-state">暂无处理记录</div>
    {% endif %}
  </section>
  <section class="section-card p-4">{{ jobs_table|safe }}</section>
  {% elif page == 'debug_candidates' %}
  <section class="section-card p-4">
    <div class="d-flex flex-column flex-md-row justify-content-between gap-3 align-items-md-center mb-3">
      <div>
        <h1 class="h3 fw-bold mb-1">🧪 候选调试</h1>
        <p class="text-secondary mb-0">查看当前等待稳定的候选队列、已等待时间和未处理原因。</p>
      </div>
      <div class="d-flex gap-2">
        <form method="post" action="{{ url_for('scan_now') }}"><button type="submit" class="btn btn-primary rounded-3">立即扫描</button></form>
        <form method="post" action="{{ url_for('force_scan_now') }}"><button type="submit" class="btn btn-warning rounded-3">强制处理</button></form>
      </div>
    </div>
    {% if candidate_rows %}
    <div class="table-responsive">
      <table class="table align-middle">
        <thead><tr><th>类型</th><th>文件路径</th><th>已等待秒数</th><th>size/mtime 是否稳定</th><th>下一次可处理时间</th><th>状态原因</th><th>大小 / mtime</th></tr></thead>
        <tbody>
          {% for row in candidate_rows %}
          <tr>
            <td><span class="badge text-bg-secondary">{{ row.candidate_type }}</span></td>
            <td class="text-path">{{ row.path }}</td>
            <td>{{ "%.1f"|format(row.waited_seconds) }}</td>
            <td>{% if row.is_stable %}<span class="badge text-bg-success">稳定</span>{% else %}<span class="badge text-bg-warning">变化中</span>{% endif %}</td>
            <td>{{ row.next_process_text }}</td>
            <td>{{ row.reason }}</td>
            <td class="text-path small">image={{ row.image_size }}/{{ row.image_mtime }}<br>video={{ row.video_size }}/{{ row.video_mtime }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <div class="empty-state">当前没有等待稳定的候选文件</div>
    {% endif %}
  </section>
  {% elif page == 'diagnostic_pair' %}
  <section class="section-card p-4 mb-4">
    <div class="d-flex flex-column flex-md-row justify-content-between gap-3 align-items-md-center mb-3">
      <div>
        <h1 class="h3 fw-bold mb-1">🧪 测试指定文件对</h1>
        <p class="text-secondary mb-0">绕过扫描、稳定等待和去重，直接调用 MotionPhoto2，适合复现单个文件对无法合并的问题。</p>
      </div>
      <a class="btn btn-outline-secondary rounded-3" href="{{ url_for('logs_page') }}">查看日志</a>
    </div>
    <form method="post" action="{{ url_for('test_pair_run') }}">
      <div class="row g-3">
        <div class="col-lg-6">
          <label class="form-label config-label" for="diagnostic_image">image</label>
          <input class="form-control text-path" id="diagnostic_image" name="image" type="text" value="{{ diagnostic_image }}" required>
        </div>
        <div class="col-lg-6">
          <label class="form-label config-label" for="diagnostic_video">video</label>
          <input class="form-control text-path" id="diagnostic_video" name="video" type="text" value="{{ diagnostic_video }}" required>
        </div>
      </div>
      <div class="d-flex flex-wrap gap-2 mt-4">
        <button type="submit" class="btn btn-danger rounded-3">测试指定文件对</button>
        <span class="text-secondary small align-self-center">成功时输出到 /photos/live/年份/月份/文件名.jpg</span>
      </div>
    </form>
  </section>
  {% if diagnostic_result %}
  <section class="section-card p-4">
    {% if diagnostic_result.success %}
    <div class="alert alert-success rounded-4 border-0">测试成功：{{ diagnostic_result.reason }}</div>
    {% else %}
    <div class="alert alert-danger rounded-4 border-0">测试失败：{{ diagnostic_result.reason }}</div>
    {% endif %}
    <div class="row g-3 mb-4">
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">image exists</div><div class="metric-value fs-4">{{ diagnostic_result.image_exists }}</div><div class="metric-small text-path">{{ diagnostic_result.image_path }}</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">video exists</div><div class="metric-value fs-4">{{ diagnostic_result.video_exists }}</div><div class="metric-small text-path">{{ diagnostic_result.video_path }}</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">return code</div><div class="metric-value fs-4">{{ diagnostic_result.returncode }}</div><div class="metric-small">MotionPhoto2 退出码</div></div></div>
      <div class="col-md-6 col-xl-3"><div class="metric-card"><div class="metric-label">output path</div><div class="metric-value fs-6 mt-2 text-path">{{ diagnostic_result.output_path }}</div><div class="metric-small">诊断输出文件</div></div></div>
    </div>
    <h2 class="section-title mb-2">MotionPhoto2 command</h2>
    <pre class="diagnostic-output mb-4">{{ diagnostic_result.command_text }}</pre>
    <div class="row g-3">
      <div class="col-lg-6">
        <h2 class="section-title mb-2">stdout</h2>
        <pre class="diagnostic-output">{{ diagnostic_result.stdout or '(empty)' }}</pre>
      </div>
      <div class="col-lg-6">
        <h2 class="section-title mb-2">stderr</h2>
        <pre class="diagnostic-output">{{ diagnostic_result.stderr or '(empty)' }}</pre>
      </div>
    </div>
  </section>
  {% endif %}
  {% elif page == 'about' %}
  <section class="section-card p-5 text-center">
    <div class="about-logo mb-3">▶</div>
    <h1 class="fw-bold">LiveMotion</h1>
    <p class="text-secondary fs-5">Live Photo 到 Google Motion Photo 的 NAS 自动化转换应用</p>
    <div class="row g-3 mt-4 text-start">
      <div class="col-md-6"><div class="metric-card"><div class="metric-label">Version</div><div class="metric-value fs-4">{{ app_version }}</div></div></div>
      <div class="col-md-6"><div class="metric-card"><div class="metric-label">GitHub 地址</div><div class="metric-small mt-2"><a href="{{ github_url }}" target="_blank" rel="noreferrer">{{ github_url }}</a></div></div></div>
      <div class="col-12"><div class="metric-card"><div class="metric-label">MotionPhoto2 致谢</div><div class="metric-small mt-2">LiveMotion 调用 MotionPhoto2 完成 Motion Photo 合成，感谢 MotionPhoto2 项目提供核心合成能力。</div></div></div>
    </div>
  </section>
  {% endif %}
</main>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
  document.querySelectorAll('[data-dir-target]').forEach((button) => {
    button.addEventListener('click', () => {
      const target = document.getElementById(button.dataset.dirTarget);
      if (target) {
        target.value = button.dataset.dirValue || '';
        target.focus();
      }
    });
  });
</script>
</body>
</html>
"""

CONFIG_FORM_TEMPLATE = """
<form method="post" action="{{ url_for('save_config') }}">
  <div class="row g-4">
    {% for field in dir_fields %}
    <div class="col-lg-6">
      <label class="form-label config-label" for="{{ field.name }}">{{ field.label }}</label>
      <input class="form-control" id="{{ field.name }}" name="{{ field.name }}" type="text" value="{{ field.value }}" required>
      <div class="quick-dir d-flex flex-wrap gap-2 mt-2" aria-label="{{ field.label }}快捷目录">
        {% for directory in common_directories %}
        <button type="button" class="btn btn-light border" data-dir-target="{{ field.name }}" data-dir-value="{{ directory }}">{{ directory }}</button>
        {% endfor %}
      </div>
    </div>
    {% endfor %}
    <div class="col-md-6">
      <label class="form-label config-label" for="stable_seconds">文件稳定等待时间（秒）</label>
      <input class="form-control" id="stable_seconds" name="stable_seconds" type="number" min="0" step="0.1" value="{{ settings.stable_seconds }}" required>
    </div>
    <div class="col-md-6">
      <label class="form-label config-label" for="poll_interval">扫描间隔（秒）</label>
      <input class="form-control" id="poll_interval" name="poll_interval" type="number" min="1" step="0.1" value="{{ settings.poll_interval }}" required>
    </div>
    <div class="col-md-6">
      <div class="form-check form-switch p-3 rounded-4 border bg-light h-100">
        <input class="form-check-input ms-0 me-2" id="move_originals" name="move_originals" type="checkbox" value="1" {% if settings.move_originals %}checked{% endif %}>
        <label class="form-check-label fw-semibold" for="move_originals">转换后移动原文件</label>
        <div class="text-secondary small mt-1">关闭后，原始 HEIC/JPG 与 MOV 会保留在输入目录。</div>
      </div>
    </div>
    <div class="col-md-6">
      <div class="form-check form-switch p-3 rounded-4 border bg-light h-100">
        <input class="form-check-input ms-0 me-2" id="enable_archive" name="enable_archive" type="checkbox" value="1" {% if settings.enable_archive %}checked{% endif %}>
        <label class="form-check-label fw-semibold" for="enable_archive">启用归档</label>
        <div class="text-secondary small mt-1">开启后，成功转换的原始文件会移动到归档目录。</div>
      </div>
    </div>
    <div class="col-md-6">
      <div class="form-check form-switch p-3 rounded-4 border bg-light h-100">
        <input class="form-check-input ms-0 me-2" id="recursive_scan" name="recursive_scan" type="checkbox" value="1" {% if settings.recursive_scan %}checked{% endif %}>
        <label class="form-check-label fw-semibold" for="recursive_scan">递归扫描</label>
        <div class="text-secondary small mt-1">开启后会扫描输入目录下所有子文件夹。</div>
      </div>
    </div>
    <div class="col-md-6">
      <div class="form-check form-switch p-3 rounded-4 border bg-light h-100">
        <input class="form-check-input ms-0 me-2" id="preserve_directory_structure" name="preserve_directory_structure" type="checkbox" value="1" {% if settings.preserve_directory_structure %}checked{% endif %}>
        <label class="form-check-label fw-semibold" for="preserve_directory_structure">保留原目录结构</label>
        <div class="text-secondary small mt-1">输出、归档和失败目录会保留相对路径。</div>
      </div>
    </div>
    <div class="col-12">
      <label class="form-label config-label" for="skip_dir_names">跳过目录列表</label>
      <textarea class="form-control" id="skip_dir_names" name="skip_dir_names" rows="7" spellcheck="false">{{ settings.skip_dir_names | join('\n') }}</textarea>
      <div class="text-secondary small mt-2">每行一个目录名。默认跳过 .stfolder、@eaDir、#recycle、.Trash、.AppleDouble、__MACOSX，并自动排除 output/archive/failed 目录。</div>
    </div>
  </div>
  <div class="d-flex flex-wrap gap-2 mt-4">
    <button type="submit" class="btn btn-primary rounded-3">保存配置</button>
    <a class="btn btn-outline-secondary rounded-3" href="{{ url_for('logs_page') }}">查看日志</a>
    <a class="btn btn-outline-secondary rounded-3" href="{{ url_for('stats_page') }}">查看统计</a>
  </div>
</form>
"""

JOBS_TABLE_TEMPLATE = """
{% if recent_jobs %}
<div class="table-responsive">
  <table class="table align-middle mb-0">
    <thead><tr><th>时间</th><th>状态</th><th>图片</th><th>视频</th><th>输出 / 错误</th></tr></thead>
    <tbody>
      {% for job in recent_jobs %}
      <tr>
        <td class="text-secondary small text-nowrap">{{ job.created_at }}</td>
        <td><span class="badge {{ status_badge(job.status) }}">{{ status_label(job.status) }}</span></td>
        <td class="text-path">{{ job.image_name }}</td>
        <td class="text-path">{{ job.video_name }}</td>
        <td class="text-path {% if job.error %}text-danger{% endif %}">{{ job.error or job.output_path or '-' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<div class="empty-state">暂无任务记录</div>
{% endif %}
"""

LOGS_PANEL_TEMPLATE = """
{% if log_entries %}
<div class="log-panel" role="log" aria-label="最近日志">
  {% for entry in log_entries %}
  <div class="log-row">
    <div><span class="badge {{ entry.badge_class }}">{{ entry.level }}</span></div>
    <div class="log-message">{{ entry.message }}</div>
  </div>
  {% endfor %}
</div>
{% else %}
<div class="empty-state">暂无日志</div>
{% endif %}
"""

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
        entries.append({
            "level": level,
            "badge_class": LOG_LEVEL_BADGES.get(level, "text-bg-secondary"),
            "message": clean,
        })
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
        rows.append(copied)
    return rows


def _scan_stats(worker: LivePhotoWorker) -> dict[str, int]:
    stats = getattr(worker, "scan_stats", None)
    if stats is None:
        return {
            "scanned_dirs": 0,
            "scanned_files": 0,
            "skipped_dirs": 0,
            "merged_live_photos": 0,
            "copied_photos": 0,
            "copied_videos": 0,
            "skipped": 0,
            "failed": 0,
        }
    to_dict = getattr(stats, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(stats)


def _dir_fields(settings: Settings) -> list[dict[str, str]]:
    return [
        {"name": "input_dir", "label": "Live Photo 输入目录", "value": str(settings.input_dir)},
        {"name": "output_dir", "label": "Pixel 同步输出目录", "value": str(settings.output_dir)},
        {"name": "archive_dir", "label": "原始文件归档目录", "value": str(settings.archive_dir)},
        {"name": "failed_dir", "label": "失败文件目录", "value": str(settings.failed_dir)},
    ]


def create_app(
    *,
    settings: Settings,
    worker: LivePhotoWorker,
    store: ProcessingStore,
    log_handler: RecentLogHandler,
    diagnostic_runner: Callable[[Path, Path], MotionPhoto2DiagnosticResult] | None = None,
) -> Flask:
    app = Flask(__name__)

    def run_diagnostic(image_path: Path, video_path: Path) -> MotionPhoto2DiagnosticResult:
        if diagnostic_runner is not None:
            return diagnostic_runner(image_path, video_path)
        return run_motionphoto2_diagnostic(settings=settings, image_path=image_path, video_path=video_path)

    def render_page(
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
        log_entries = _log_entries(log_handler.recent(limit=120))
        config_form = render_template_string(
            CONFIG_FORM_TEMPLATE,
            settings=settings,
            dir_fields=_dir_fields(settings),
            common_directories=COMMON_DIRECTORIES,
        )
        jobs_table = render_template_string(
            JOBS_TABLE_TEMPLATE,
            recent_jobs=recent_jobs,
            status_label=_status_label,
            status_badge=_status_badge,
        )
        logs_panel = render_template_string(LOGS_PANEL_TEMPLATE, log_entries=log_entries)
        return render_template_string(
            PAGE_TEMPLATE,
            page=page,
            title=title,
            settings=settings,
            stats=stats,
            recent_jobs=recent_jobs,
            log_entries=log_entries,
            config_form=config_form,
            jobs_table=jobs_table,
            logs_panel=logs_panel,
            queue_count=_queue_count(worker),
            pending_status=_pending_status(worker),
            candidate_rows=_candidate_rows(worker),
            scan_stats=_scan_stats(worker),
            app_version=APP_VERSION,
            github_url=GITHUB_URL,
            message=message,
            diagnostic_image=diagnostic_image,
            diagnostic_video=diagnostic_video,
            diagnostic_result=diagnostic_result,
        )

    @app.get("/")
    def index() -> str:
        return render_page("dashboard", "控制台", message=request.args.get("message", ""))

    @app.get("/logs")
    def logs_page() -> str:
        return render_page("logs", "查看日志", message=request.args.get("message", ""))

    @app.post("/logs/clear")
    def clear_logs():  # type: ignore[no-untyped-def]
        log_handler.clear()
        return redirect(url_for("logs_page", message="日志已清空"))

    @app.get("/stats")
    def stats_page() -> str:
        return render_page("stats", "查看统计", message=request.args.get("message", ""))

    @app.get("/about")
    def about_page() -> str:
        return render_page("about", "关于")

    @app.get("/debug/candidates")
    def debug_candidates_page() -> str:
        return render_page("debug_candidates", "候选调试", message=request.args.get("message", ""))

    @app.get("/debug/test-pair")
    def test_pair_page() -> str:
        return render_page(
            "diagnostic_pair",
            "测试指定文件对",
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
        return redirect(url_for("index", message="配置已保存并立即生效"))

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
            "diagnostic_pair",
            "测试指定文件对",
            diagnostic_image=image,
            diagnostic_video=video,
            diagnostic_result=result.to_dict(),
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
