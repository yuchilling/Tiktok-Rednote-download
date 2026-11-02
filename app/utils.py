"""杂项工具：链接清洗、文件名安全化、体积格式化。"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# 分享口令里夹带的 URL；中文标点、空格、引号都要截断
_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\"'<>]+")

# Windows 文件名非法字符
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif")
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".m3u8")
_AUDIO_EXTS = (".m4a", ".mp3", ".aac", ".wav", ".ogg")


def extract_url(text: str) -> str:
    """从「复制打开抖音…」这类口令长文里抠出第一条链接。"""
    if not text:
        return ""
    text = text.strip()
    m = _URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;!?")
    # 用户可能只粘了不带协议的域名
    m = re.search(r"((?:v\.douyin\.com|xhslink\.com|www\.douyin\.com|www\.xiaohongshu\.com)[^\s]*)", text)
    return ("https://" + m.group(1)) if m else ""


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """把标题变成合法的 Windows 文件名。

    标题常带换行与话题标签，这里统一压成单行空格、去掉 #。
    """
    name = (name or "").strip()
    name = _ILLEGAL.sub(" ", name)
    name = name.replace("#", " ")
    name = re.sub(r"\s+", " ", name).strip()
    if max_len > 1 and len(name) > max_len:
        name = name[:max_len].strip()
    name = name.strip(" .")
    return name or "untitled"


def human_size(n: int | float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def guess_ext(url: str, kind: str = "video") -> str:
    """按 URL 猜扩展名；猜不到就按内容种类给默认值。

    小红书的图片 URL 常常没有后缀（fileId + `?imageView2/2/w/format/jpg`），
    所以要连查询参数一起看。
    """
    low = (url or "").lower()
    path = urlparse(low).path
    query = parse_qs(urlparse(low).query)

    for ext in _IMAGE_EXTS + _VIDEO_EXTS + _AUDIO_EXTS:
        if path.endswith(ext) or low.split("?")[0].endswith(ext):
            return ext
    # CDN 的格式参数，如 ?imageView2/2/w/format/jpg
    fmt = (query.get("format") or [""])[0].lower()
    if fmt in ("jpg", "jpeg", "png", "webp", "heic", "avif"):
        return "." + ("jpg" if fmt == "jpeg" else fmt)
    if "/format/" in low:
        tail = low.rsplit("/format/", 1)[-1].split("?")[0].split("&")[0]
        tail = re.sub(r"[^a-z0-9]", "", tail)
        if tail in ("jpg", "jpeg", "png", "webp", "heic", "avif"):
            return "." + ("jpg" if tail == "jpeg" else tail)
    if ".m3u8" in low or "video" in low or ".mp4" in low:
        return ".mp4"

    if kind in ("image", "live"):
        return ".jpg" if kind == "image" else ".mp4"
    if kind == "audio":
        return ".m4a"
    return ".mp4"
