# -*- coding: utf-8 -*-
"""日志模块：同时输出到控制台与文件，便于排查长时间采集任务的问题。"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_initialized = False


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """初始化根日志器（幂等，重复调用只生效一次）。"""
    global _initialized
    if _initialized:
        return
    log_dir.mkdir(parents=True, exist_ok=True)

    # Windows 控制台代码页可能是 GBK，遇到不可编码字符（如 emoji）会直接抛
    # UnicodeEncodeError 中断程序。这里改为「用替代符输出」而不是崩掉。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 老版本或已重定向的流
            pass

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("astock")
    root.setLevel(level)
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "astock.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    # 第三方库降噪
    for noisy in ("urllib3", "requests", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """获取带命名空间的日志器，如 get_logger('data.collector')。"""
    short = name.split(".")[-1] if name.startswith("astock") else name
    return logging.getLogger(f"astock.{short}")
