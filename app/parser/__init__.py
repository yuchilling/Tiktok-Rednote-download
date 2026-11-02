"""平台识别与统一入口。"""

from __future__ import annotations

import time
from collections import OrderedDict
from urllib.parse import urlparse

from ..utils import extract_url
from . import douyin, xhs
from .base import Entry, MediaItem, ParseError

__all__ = ["Entry", "MediaItem", "ParseError", "detect_platform", "parse", "douyin", "xhs"]

_CACHE: "OrderedDict[str, tuple[float, MediaItem]]" = OrderedDict()
_CACHE_TTL = 300.0        # 5 分钟后过期（作品可能被作者改）
_CACHE_MAX = 32


def _cache_key(url: str, cookie: str, third_party: str) -> str:
    return url + "|" + str(len(cookie or "")) + "|" + str(len(third_party or ""))


def clear_cache() -> None:
    _CACHE.clear()


DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
XHS_HOSTS = ("xiaohongshu.com", "xhslink.com")


def detect_platform(url: str) -> str:
    """从链接判断平台，认不出来返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    if any(host == h or host.endswith("." + h) for h in DOUYIN_HOSTS):
        return "douyin"
    if any(host == h or host.endswith("." + h) for h in XHS_HOSTS):
        return "xhs"
    return ""


def parse(text: str, cookie: str = "", third_party: str = "") -> MediaItem:
    """解析一条分享链接/分享文案。

    - 认不出平台时按「抖音 → 小红书」的顺序各试一次（分享文案里常有短链）
    - :param cookie: 抖音需要的浏览器 Cookie（可选）
    - :param third_party: 抖音第三方解析接口模板（可多行，含 ``{url}`` 占位）
    """
    url = extract_url(text)
    key = _cache_key(url, cookie, third_party)
    hit = _CACHE.get(key)
    if hit is not None and time.time() - hit[0] < _CACHE_TTL:
        _CACHE.move_to_end(key)
        return hit[1]

    item = _parse_uncached(url, cookie, third_party)
    _CACHE[key] = (time.time(), item)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return item


def _parse_uncached(url: str, cookie: str = "", third_party: str = "") -> MediaItem:
    platform = detect_platform(url)
    if platform == "douyin":
        return douyin.parse(url, cookie=cookie, third_party=third_party)
    if platform == "xhs":
        return xhs.parse(url)

    if not url:
        raise ParseError("没有识别到链接，把分享文案整段粘进来试试。")

    errors: list[str] = []
    for name, fn in (("抖音", lambda: douyin.parse(url, cookie=cookie,
                                                   third_party=third_party)),
                     ("小红书", lambda: xhs.parse(url))):
        try:
            return fn()
        except ParseError as e:
            errors.append(f"{name}：{e}")
        except OSError as e:
            errors.append(f"{name}：网络错误 {e}")
    raise ParseError("认不出这个链接属于哪个平台：\n" + "\n".join(errors))
