"""HTTP 基建：只用标准库 urllib，避免任何第三方依赖。

- get(): 跟随重定向，返回 (最终URL, 文本, 响应头)
- open_stream(): 给下载器用的流式句柄
"""

from __future__ import annotations

import gzip
import http.client
import re
import ssl
import threading
import urllib.error
import urllib.request
import zlib
import urllib.parse
from urllib.parse import urljoin, urlsplit

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

DEFAULT_TIMEOUT = 20


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------- 连接复用
# urllib 每次请求都新建 TCP+TLS；预览连点几张图时光握手的开销就比图还大。
# 这里按线程缓存 (scheme, host) 的连接，避免并发线程互相踩。
_SSL_CTX = ssl.create_default_context()
_local = threading.local()
MAX_REDIRECT = 4


def _conn(scheme: str, host: str, timeout: int, fresh: bool = False):
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    key = (scheme, host)
    if fresh:
        old = pool.pop(key, None)
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
    conn = pool.get(key)
    if conn is None:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, timeout=timeout, context=_SSL_CTX)
        else:
            conn = http.client.HTTPConnection(host, timeout=timeout)
        pool[key] = conn
    else:
        conn.timeout = timeout
    return conn


def _drop(scheme: str, host: str) -> None:
    pool = getattr(_local, "pool", None)
    if not pool:
        return
    conn = pool.pop((scheme, host), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


_NON_ASCII = re.compile(r"[^\x20-\x7e]")


def ascii_header(value: str) -> str:
    """把配置里粘进来的文本压成合法的 HTTP 头值。

    http.client 用 latin-1 编码头值，一旦里面混进中文（比如误把分享口令
    粘进了 Cookie 框）就会抛 `'latin-1' codec can't encode ...`，
    报错信息对用户毫无意义。这里直接把非 ASCII 字符去掉。
    """
    if not isinstance(value, str):
        return ""
    return _NON_ASCII.sub("", value)


def clean_headers(headers: dict | None) -> dict | None:
    """逐个值做 ascii 化，避免 latin-1 编码炸掉整个请求。"""
    if not headers:
        return headers
    return {k: ascii_header(str(v)) for k, v in headers.items()}


def _build_headers(headers: dict | None, ua: str) -> dict:
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        h.update(clean_headers(headers))
    return h


def _decode_body(raw: bytes, headers) -> str:
    enc = (headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
    charset = "utf-8"
    ctype = headers.get("Content-Type") or ""
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _path_of(url: str) -> tuple[str, str, str]:
    parts = urlsplit(url)
    scheme = parts.scheme or "https"
    host = parts.netloc
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    path = urllib.parse.quote(path, safe="/?=&%:+,;@!$'()*[]~")
    if not host:
        raise FetchError(f"地址不合法：{url}")
    return scheme, host, path


def get(url: str, headers: dict | None = None, ua: str = UA_DESKTOP,
        timeout: int = DEFAULT_TIMEOUT) -> tuple[str, str]:
    """GET 一个页面，自动跟随 302，返回 (最终URL, HTML 文本)。

    走连接池：短链展开 + 取页面通常是同一台主机上的两次请求，
    复用同一条 TLS 连接能省掉一次完整握手（约 100–200 ms）。
    """
    h = _build_headers(headers, ua)
    h["Connection"] = "keep-alive"

    for _hop in range(MAX_REDIRECT + 1):
        scheme, host, path = _path_of(url)
        for attempt in (0, 1):
            conn = _conn(scheme, host, timeout, fresh=bool(attempt))
            try:
                conn.request("GET", path, headers=h)
                resp = conn.getresponse()
                if resp.status in (301, 302, 303, 307, 308):
                    where = resp.getheader("Location")
                    resp.read()
                    if not where:
                        raise FetchError(f"HTTP {resp.status} 但缺少 Location：{url}")
                    url = urljoin(url, where)
                    break
                if resp.status >= 400:
                    resp.read()
                    raise FetchError(f"HTTP {resp.status}：{url}")
                raw = resp.read()
                return url, _decode_body(raw, resp.headers)
            except FetchError:
                raise
            except (http.client.HTTPException, OSError) as e:
                _drop(scheme, host)
                if attempt:
                    raise FetchError(f"网络错误：{e}") from e
        else:
            raise FetchError(f"网络错误：{url}")
    raise FetchError("重定向次数过多")


def open_stream(url: str, headers: dict | None = None, ua: str = UA_DESKTOP,
                timeout: int = 60):
    """给下载器用：返回 (连接, 响应, 最终地址)，调用方负责读完并 close。

    同样走连接池——一个作品 20 多张图，复用连接能省掉 20 次 TLS 握手。
    """
    h = _build_headers(headers, ua)
    h["Accept"] = "*/*"
    h["Accept-Encoding"] = "identity"
    h["Connection"] = "keep-alive"

    for _hop in range(MAX_REDIRECT + 1):
        scheme, host, path = _path_of(url)
        for attempt in (0, 1):
            conn = _conn(scheme, host, timeout, fresh=bool(attempt))
            try:
                conn.request("GET", path, headers=h)
                resp = conn.getresponse()
                if resp.status in (301, 302, 303, 307, 308):
                    where = resp.getheader("Location")
                    resp.read()
                    if not where:
                        raise FetchError(f"HTTP {resp.status} 但缺少 Location：{url}")
                    url = urljoin(url, where)
                    break
                if resp.status >= 400:
                    resp.read()
                    raise FetchError(f"HTTP {resp.status}：{url}")
                return conn, resp, url
            except FetchError:
                raise
            except (http.client.HTTPException, OSError) as e:
                _drop(scheme, host)
                if attempt:
                    raise FetchError(f"网络错误：{e}") from e
        else:
            raise FetchError(f"网络错误：{url}")
    raise FetchError("重定向次数过多")


def drop_conn(url: str) -> None:
    """下载中途取消/出错时把那条连接丢掉（流没读完，不能复用）。"""
    try:
        scheme, host, _ = _path_of(url)
    except FetchError:
        return
    _drop(scheme, host)


def get_bytes(url: str, headers: dict | None = None, ua: str = UA_DESKTOP,
              timeout: int = DEFAULT_TIMEOUT, limit: int = 8 * 1024 * 1024) -> bytes:
    """取原始字节（图片/封面用）。

    走 http.client 而不是 urllib：同一线程里连续取图能复用 TLS 连接，
    预览一连串缩略图时省掉每张一次的握手（约 100–200ms/张）。
    超过 limit 就截断，避免把内存吃满。
    """
    h = _build_headers(headers, ua)
    h["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    h["Accept-Encoding"] = "identity"
    h["Connection"] = "keep-alive"

    for _hop in range(MAX_REDIRECT + 1):
        parts = urlsplit(url)
        scheme = parts.scheme or "https"
        host = parts.netloc
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        path = urllib.parse.quote(path, safe="/?=&%:+,;@!$'()*[]~")
        if not host:
            raise FetchError(f"地址不合法：{url}")

        for attempt in (0, 1):
            conn = _conn(scheme, host, timeout, fresh=bool(attempt))
            try:
                conn.request("GET", path, headers=h)
                resp = conn.getresponse()
                status = resp.status
                if status in (301, 302, 303, 307, 308):
                    where = resp.getheader("Location")
                    resp.read()
                    if not where:
                        raise FetchError(f"HTTP {status} 但缺少 Location：{url}")
                    url = urljoin(url, where)
                    break           # 跳到外层处理新地址
                if status >= 400:
                    resp.read()
                    raise FetchError(f"HTTP {status}：{url}")
                return resp.read(limit)
            except FetchError:
                raise
            except (http.client.HTTPException, OSError) as e:
                _drop(scheme, host)     # 连接可能已被服务端关掉，换一条重试
                if attempt:
                    raise FetchError(f"网络错误：{e}") from e
        else:                       # 两次都没成功
            raise FetchError(f"网络错误：{url}")
    else:
        raise FetchError("重定向次数过多")


def close_connections() -> None:
    """关掉当前线程缓存的连接（测试/退出时用）。"""
    pool = getattr(_local, "pool", None) or {}
    for conn in pool.values():
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    pool.clear()


def head_final_url(url: str, ua: str = UA_DESKTOP, timeout: int = 15) -> str:
    """只取重定向后的最终地址（用于短链展开）。"""
    final, _ = get(url, ua=ua, timeout=timeout)
    return final
