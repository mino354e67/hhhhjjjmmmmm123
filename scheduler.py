#!/usr/bin/env python3
"""
VPS 随机凌晨下行流量调度器

每天在 [WINDOW_START, WINDOW_END] 窗口内随机挑选一个时刻，
从国内镜像站循环下载大文件到 /dev/null，产生下行流量，
并通过 curl --limit-rate 限速以避免占满带宽。

所有配置通过环境变量或命令行参数控制，详见 README。
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import logging.handlers
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))
LOG_FILE = LOG_DIR / "scheduler.log"

_shutdown = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _shutdown
        logging.getLogger(__name__).warning("收到信号 %s，准备退出", signum)
        _shutdown = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("scheduler")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_h = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(fmt)
    logger.addHandler(file_h)
    logger.addHandler(stream_h)
    return logger


def _parse_hhmm(value: str, name: str) -> dt.time:
    try:
        hh, mm = value.strip().split(":")
        return dt.time(hour=int(hh), minute=int(mm))
    except Exception as e:
        raise SystemExit(f"环境变量 {name} 格式错误（应为 HH:MM）: {value!r} ({e})")


def load_config() -> dict:
    cfg = {
        "window_start": _parse_hhmm(os.environ.get("WINDOW_START", "02:00"), "WINDOW_START"),
        "window_end": _parse_hhmm(os.environ.get("WINDOW_END", "06:00"), "WINDOW_END"),
        "min_gb": float(os.environ.get("MIN_GB", "10")),
        "max_gb": float(os.environ.get("MAX_GB", "30")),
        "urls_file": Path(os.environ.get("URLS_FILE", "/app/urls.txt")),
        "rate_limit": os.environ.get("RATE_LIMIT", "12500k"),  # ≈100 Mbps
        "connect_timeout": int(os.environ.get("CONNECT_TIMEOUT", "15")),
        "max_time": int(os.environ.get("MAX_TIME", "3600")),
        "min_size_mb": int(os.environ.get("MIN_FILE_MB", "500")),
    }
    if cfg["window_start"] == cfg["window_end"]:
        raise SystemExit("WINDOW_START 不能等于 WINDOW_END")
    if cfg["min_gb"] > cfg["max_gb"]:
        raise SystemExit("MIN_GB 不能大于 MAX_GB")
    return cfg


def load_urls(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit(f"URL 文件不存在: {path}")
    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    if not urls:
        raise SystemExit(f"URL 文件为空: {path}")
    return urls


def head_probe(url: str, timeout: int, min_size_bytes: int, log: logging.Logger) -> int | None:
    """
    对 URL 做 HEAD 预检，返回 Content-Length（字节）或 None（不可用）。
    """
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sSI",
                "-L",
                "--max-time",
                str(timeout),
                url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit("未找到 curl 可执行文件")

    if proc.returncode != 0:
        log.warning("HEAD 失败 %s: rc=%s err=%s", url, proc.returncode, proc.stderr.strip()[:200])
        return None

    status_ok = False
    size: int | None = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("2"):
                status_ok = True
            else:
                status_ok = False  # 后续跳转继续覆盖
        elif line.lower().startswith("content-length:"):
            try:
                size = int(line.split(":", 1)[1].strip())
            except ValueError:
                size = None

    if not status_ok or size is None:
        log.warning("HEAD 未返回可用资源 %s (status_ok=%s, size=%s)", url, status_ok, size)
        return None
    if size < min_size_bytes:
        log.info("跳过小文件 %s (%.1f MB < %.1f MB)", url, size / 1024 / 1024, min_size_bytes / 1024 / 1024)
        return None
    return size


def preflight(urls: list[str], cfg: dict, log: logging.Logger) -> list[tuple[str, int]]:
    min_bytes = cfg["min_size_mb"] * 1024 * 1024
    good: list[tuple[str, int]] = []
    for u in urls:
        size = head_probe(u, timeout=5, min_size_bytes=min_bytes, log=log)
        if size is not None:
            good.append((u, size))
            log.info("可用 %s (%.2f GB)", urlparse(u).netloc + urlparse(u).path, size / 1024 ** 3)
    return good


def curl_download(url: str, cfg: dict, log: logging.Logger) -> int:
    """
    调用 curl 下载到 /dev/null，返回本次实际下载字节数。
    """
    cmd = [
        "curl",
        "-sS",
        "-L",
        "-o",
        "/dev/null",
        "--limit-rate",
        cfg["rate_limit"],
        "--connect-timeout",
        str(cfg["connect_timeout"]),
        "--max-time",
        str(cfg["max_time"]),
        "-w",
        "%{size_download} %{speed_download} %{http_code}\n",
        url,
    ]
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as e:
        log.error("curl 调用异常 %s: %s", url, e)
        return 0
    elapsed = time.monotonic() - start

    if proc.returncode != 0:
        log.warning(
            "下载失败 %s: rc=%s 耗时=%.1fs err=%s",
            url,
            proc.returncode,
            elapsed,
            proc.stderr.strip()[:200],
        )
        # 即使失败也尽量解析已下载的字节数
    try:
        parts = proc.stdout.strip().split()
        size = int(parts[0]) if parts else 0
        speed = float(parts[1]) if len(parts) > 1 else 0.0
        code = parts[2] if len(parts) > 2 else "-"
    except Exception:
        size, speed, code = 0, 0.0, "-"

    log.info(
        "下载 %s http=%s 大小=%.2f GB 速率=%.2f MB/s 耗时=%.1fs",
        urlparse(url).netloc + urlparse(url).path,
        code,
        size / 1024 ** 3,
        speed / 1024 / 1024,
        elapsed,
    )
    return size


def run_download_session(cfg: dict, log: logging.Logger, target_bytes: int, urls_with_size: list[tuple[str, int]]) -> None:
    if not urls_with_size:
        log.error("没有可用 URL，跳过本次 session")
        return

    log.info(
        "启动下载 session: 目标 %.2f GB, 限速 %s, 候选 %d 个 URL",
        target_bytes / 1024 ** 3,
        cfg["rate_limit"],
        len(urls_with_size),
    )
    downloaded = 0
    session_start = time.monotonic()
    round_no = 0
    while downloaded < target_bytes and not _shutdown:
        round_no += 1
        pool = urls_with_size[:]
        random.shuffle(pool)
        for url, _size in pool:
            if _shutdown or downloaded >= target_bytes:
                break
            got = curl_download(url, cfg, log)
            downloaded += got
            log.info("累计 %.2f / %.2f GB", downloaded / 1024 ** 3, target_bytes / 1024 ** 3)
        if downloaded == 0:
            log.error("第 %d 轮全部失败，终止 session", round_no)
            return
        if round_no >= 10:
            log.error("超过 10 轮仍未达标，终止 session")
            return

    elapsed = time.monotonic() - session_start
    avg_mbps = (downloaded * 8 / 1024 / 1024) / elapsed if elapsed > 0 else 0
    log.info(
        "session 结束: 共下载 %.2f GB, 耗时 %.1fs, 平均 %.1f Mbps",
        downloaded / 1024 ** 3,
        elapsed,
        avg_mbps,
    )


def next_run_time(cfg: dict, now: dt.datetime | None = None) -> dt.datetime:
    """
    计算下一次触发时刻。如果当前时间早于今日窗口开始，则在今日窗口内随机；
    否则在明日窗口内随机。窗口支持跨零点（end < start）。
    """
    now = now or dt.datetime.now()
    ws, we = cfg["window_start"], cfg["window_end"]

    def _window_for(date: dt.date) -> tuple[dt.datetime, dt.datetime]:
        start = dt.datetime.combine(date, ws)
        if we > ws:
            end = dt.datetime.combine(date, we)
        else:  # 跨零点
            end = dt.datetime.combine(date + dt.timedelta(days=1), we)
        return start, end

    today_start, today_end = _window_for(now.date())
    if now < today_start:
        start, end = today_start, today_end
    elif now < today_end:
        # 已在窗口内，仍允许在剩余窗口随机（但至少 30 秒后）
        start = now + dt.timedelta(seconds=30)
        end = today_end
        if start >= end:
            start, end = _window_for(now.date() + dt.timedelta(days=1))
    else:
        start, end = _window_for(now.date() + dt.timedelta(days=1))

    delta = (end - start).total_seconds()
    return start + dt.timedelta(seconds=random.uniform(0, delta))


def sleep_until(target: dt.datetime, log: logging.Logger) -> None:
    while not _shutdown:
        remaining = (target - dt.datetime.now()).total_seconds()
        if remaining <= 0:
            return
        # 分段 sleep，便于信号响应
        chunk = min(remaining, 300)
        time.sleep(chunk)


def main_loop(cfg: dict, log: logging.Logger) -> None:
    urls = load_urls(cfg["urls_file"])
    log.info("加载 %d 个 URL，开始 HEAD 预检...", len(urls))
    good = preflight(urls, cfg, log)
    if not good:
        log.error("所有 URL 预检失败，1 小时后重试")
        for _ in range(3600):
            if _shutdown:
                return
            time.sleep(1)
        return main_loop(cfg, log)

    log.info("%d 个 URL 通过预检", len(good))

    while not _shutdown:
        target = next_run_time(cfg)
        log.info("下一次触发时刻: %s", target.strftime("%Y-%m-%d %H:%M:%S"))
        sleep_until(target, log)
        if _shutdown:
            break
        gb = random.uniform(cfg["min_gb"], cfg["max_gb"])
        target_bytes = int(gb * 1024 ** 3)
        run_download_session(cfg, log, target_bytes, good)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VPS 随机下行流量调度器")
    p.add_argument("--once", action="store_true", help="立即执行一次 download session 后退出（调试用）")
    p.add_argument("--target-mb", type=float, default=None, help="配合 --once 使用，指定本次下载目标 MB")
    return p.parse_args()


def main() -> int:
    log = _setup_logging()
    _install_signal_handlers()
    cfg = load_config()
    log.info(
        "配置: 窗口 %s-%s, 每日 %.1f-%.1f GB, 限速 %s",
        cfg["window_start"].strftime("%H:%M"),
        cfg["window_end"].strftime("%H:%M"),
        cfg["min_gb"],
        cfg["max_gb"],
        cfg["rate_limit"],
    )
    args = parse_args()

    if args.once:
        urls = load_urls(cfg["urls_file"])
        good = preflight(urls, cfg, log)
        if not good:
            log.error("所有 URL 预检失败")
            return 2
        if args.target_mb is not None:
            target_bytes = int(args.target_mb * 1024 * 1024)
        else:
            gb = random.uniform(cfg["min_gb"], cfg["max_gb"])
            target_bytes = int(gb * 1024 ** 3)
        run_download_session(cfg, log, target_bytes, good)
        return 0

    try:
        main_loop(cfg, log)
    except Exception as e:
        log.exception("主循环异常: %s", e)
        return 1
    log.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
