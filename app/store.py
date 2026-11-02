"""本地存储：下载历史（sqlite3）与用户偏好（json），都放在 %APPDATA%\\VideoSaver。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

# 可用 VIDEO_SAVER_HOME 覆盖（绿色便携版：指到 exe 旁边的 data 目录）
APP_DIR = Path(
    os.getenv("VIDEO_SAVER_HOME") or (Path(os.getenv("APPDATA") or Path.home()) / "VideoSaver")
)
DB_PATH = APP_DIR / "history.db"
CONFIG_PATH = APP_DIR / "config.json"
THUMB_DIR = APP_DIR / "thumbs"   # 预览图本地缓存（按 URL 哈希存原始字节）

DEFAULT_THIRD_PARTY = "\n".join((
    # 2026-11 实测可用（返回 type/title/author/cover/url/images/live_photo）
    "https://api.bugpk.com/api/douyin?url={url}",
    "https://api.bugpk.com/api/douyin/v2?url={url}",
    # 备用，时好时坏；接口挂掉时在设置里删掉或补新的
    "https://api.xingzhige.com/API/douyin/?url={url}",
    "https://api.suyanw.cn/api/douyin.php?url={url}",
))

DEFAULT_CONFIG = {
    "last_dir": "",
    "remember_dir": True,
    "auto_open": True,
    "douyin_cookie": "",
    "third_party": DEFAULT_THIRD_PARTY,
}


def ensure_dir() -> bool:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            cfg.update({k: got[k] for k in DEFAULT_CONFIG if k in got})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    if not ensure_dir():
        return
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG},
                      f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- 历史

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    item_id     TEXT,
    title       TEXT,
    author      TEXT,
    resource    TEXT,
    out_dir     TEXT,
    files       TEXT,
    file_count  INTEGER DEFAULT 0,
    total_bytes INTEGER DEFAULT 0,
    created_at  REAL NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    ensure_dir()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute(_SCHEMA)
    return conn


def add_history(platform: str, item_id: str, title: str, author: str, resource: str,
                out_dir: str, files: list[str], total_bytes: int = 0) -> None:
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO downloads (platform, item_id, title, author, resource,"
                " out_dir, files, file_count, total_bytes, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (platform, item_id, title, author, resource, out_dir,
                 json.dumps(files, ensure_ascii=False), len(files), total_bytes,
                 time.time()),
            )
    except (sqlite3.Error, OSError):
        pass


def list_history(limit: int = 200) -> list[dict]:
    try:
        with _conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["files"] = json.loads(d.get("files") or "[]")
            except json.JSONDecodeError:
                d["files"] = []
            out.append(d)
        return out
    except (sqlite3.Error, OSError):
        return []


def recent_dirs(limit: int = 8) -> list[str]:
    seen, out = set(), []
    for row in list_history(100):
        d = row.get("out_dir") or ""
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
        if len(out) >= limit:
            break
    return out


def clear_history() -> None:
    try:
        with _conn() as conn:
            conn.execute("DELETE FROM downloads")
    except (sqlite3.Error, OSError):
        pass
