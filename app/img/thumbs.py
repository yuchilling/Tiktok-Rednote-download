"""缩略图：先让 CDN 把图缩到 360px，再落本地磁盘缓存。

预览只需要 300×200 的图，原先却把 600KB 的原图整个下下来，慢得没道理。
这里做两件事：

1. `cdn_url()`——把图片地址改成「CDN 先缩图」的版本（小红书图床支持
   `?imageView2/2/w/360/format/jpg`），字节数从几百 KB 掉到十几 KB；
2. 本地缓存——同一张图第二次点只读盘，不联网。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .. import store

THUMB_W = 360
_MAX_BYTES = 2 * 1024 * 1024

# 只有确认支持 imageView2 的小红书图床才动手，别把别的站点的 URL 改坏
_IMG_HOSTS = (
    "sns-img-bd.xhscdn.com",
    "sns-img-qc.xhscdn.com",
    "ci.xiaohongshu.com",
    "sns-webpic-qc.xhscdn.com",
)

_W_RE = re.compile(r"/w/\d+")


def cdn_url(url: str, width: int = THUMB_W) -> str:
    """返回缩略图地址；拿不准的站点原样返回。"""
    if not url:
        return url
    host = url.split("/")[2] if "//" in url else ""
    if host not in _IMG_HOSTS:
        return url
    if "!" in url:                       # webpic 样式后缀，前面不能再拼参数
        return url
    if "imageView2" in url:
        if _W_RE.search(url):
            return _W_RE.sub(f"/w/{width}", url, count=1)
        return url.replace("/w/format", f"/w/{width}/format")
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}imageView2/2/w/{width}/format/jpg"


def cache_path(url: str) -> Path:
    name = hashlib.md5(url.encode("utf-8")).hexdigest()
    return store.THUMB_DIR / f"{name}.img"


def load(url: str) -> bytes | None:
    """读缓存；没有 / 读失败返回 None。"""
    try:
        p = cache_path(url)
        if p.is_file() and 0 < p.stat().st_size <= _MAX_BYTES:
            return p.read_bytes()
    except OSError:
        pass
    return None


def save(url: str, data: bytes) -> None:
    """写缓存；目录不可写就静默跳过（不能因为缓存失败影响预览）。"""
    if not data or len(data) > _MAX_BYTES:
        return
    try:
        store.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache_path(url).with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache_path(url))
    except OSError:
        pass


def fetch(url: str, http) -> bytes:
    """先看缓存，没有就走 `http.get_bytes` 并回写。"""
    thumb = cdn_url(url)
    data = load(thumb)
    if data is not None:
        return data
    data = http.get_bytes(thumb, limit=_MAX_BYTES)
    save(thumb, data)
    return data
