"""极简日志：出错时把完整 traceback 落到 `%APPDATA%\\VideoSaver\\app.log`。

用户报「下载失败：…」时，弹窗里只有一句 str(e)，看不出是哪行崩的。
这里把完整堆栈记下来，让用户把 app.log 发回来就能定位。
写不进去（沙箱 / 只读盘）也绝不抛异常。
"""

from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

from . import store

MAX_BYTES = 256 * 1024


def log_path() -> Path:
    return store.APP_DIR / "app.log"


def _trim(path: Path) -> None:
    """太大就只留后一半，避免日志无限长。"""
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        data = path.read_bytes()[-MAX_BYTES // 2:]
        path.write_bytes(b"...\n" + data)
    except OSError:
        pass


def log(message: str) -> None:
    path = log_path()
    try:
        store.ensure_dir()
        _trim(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except (OSError, ValueError):
        pass


def log_exc(tag: str) -> str:
    """记一条带完整堆栈的错误，返回路径（写入失败时返回空串）。"""
    text = traceback.format_exc()
    log(f"{tag}\n{text}")
    return str(log_path()) if os.path.exists(log_path()) else ""
