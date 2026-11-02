"""抖音解析：视频 / 图集 / 实况（动图）。

现状（2026-11 实测）
--------------------
- 分享页 HTML（`iesdouyin.com/share/video/<id>`）里已经没有 `videoInfoRes` 了，
  `_ROUTER_DATA` 只剩上下文；`/web/api/v2/aweme/iteminfo/` 返回 0 字节；
  `aweme/v1/web/aweme/detail/` 需要真实浏览器 Cookie（缺 UIFID 会 403
  `Blocked by ArgusSecurityPlugin Uifid Not Found`）。
- 因此这里做成三级兜底：
  ① 本地签名（a_bogus）+ 用户粘贴的浏览器 Cookie → 官方 web 详情接口；
  ② 页面 HTML 里的 `_ROUTER_DATA` / `RENDER_DATA`（对部分链接仍有效）；
  ③ 可选的第三方解析接口（设置里填 URL 模板，默认关闭）。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import re
import string
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import http
from ..utils import extract_url
from .base import Entry, MediaItem, ParseError

_ID_RE = re.compile(r"/(?:share/)?(?:video|note|slides)/(\d{15,25})")
_QUERY_ID_RE = re.compile(r"(?:modal_id|aweme_id|item_ids|vid)=(\d{15,25})")
_ROUTER_MARKER = r"window\._ROUTER_DATA\s*=\s*"
_RENDER_MARKER = r'<script id="RENDER_DATA"[^>]*>(.*?)</script>'
_PLAYLIST_RE = re.compile(r"self\.__pace_f\.push\(\[1,\"(.*?)\"\]\)", re.S)

_MS_TOKEN_RE = re.compile(r"msToken=([^;\s]+)")
_TTWID_RE = re.compile(r"ttwid=([^;\s]+)")


# ---------------------------------------------------------------- 工具

def _rand_seq(n: int = 107) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits + "-_", k=n))


def _clean_url(url: str) -> str:
    if not url or not isinstance(url, str):
        return ""
    url = url.replace("\\u002F", "/").replace("\\/", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


def _play(url: str) -> str:
    """把带水印的 playwm 换成无水印的 play。"""
    return _clean_url(url).replace("playwm", "play")


def _url_list(obj: Any) -> list[str]:
    """从 `{"url_list": [...]}` 这类结构里取候选直链。"""
    out: list[str] = []
    if isinstance(obj, dict):
        urls = obj.get("url_list") or obj.get("urlList") or []
        for u in urls:
            u = _play(u if isinstance(u, str) else "")
            if u:
                out.append(u)
    elif isinstance(obj, list):
        for u in obj:
            u = _play(u if isinstance(u, str) else "")
            if u:
                out.append(u)
    return out


def _json_after(text: str, marker: str):
    m = re.search(marker, text)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[m.end():])
        return obj
    except json.JSONDecodeError:
        return None


def _find_key(obj: Any, key: str, depth: int = 0) -> Any:
    """在嵌套结构里找第一个 key（抖音的 SSR 数据层次不稳定）。"""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            got = _find_key(v, key, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_key(v, key, depth + 1)
            if got is not None:
                return got
    return None


def _item_id_of(text: str) -> str:
    m = _ID_RE.search(text) or _QUERY_ID_RE.search(text)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 数据结构 → Entry

def _gear_label(br: dict) -> str:
    name = (br.get("gear_name") or "").strip()
    if name:
        return name
    width = ((br.get("play_addr") or {}).get("width")) or 0
    height = ((br.get("play_addr") or {}).get("height")) or 0
    if height:
        if height >= 2000:
            return "4K"
        if height >= 1000:
            return "1080p"
        if height >= 700:
            return "720p"
        return f"{height}p"
    bitrate = br.get("bit_rate") or 0
    return f"{int(bitrate / 1000)}kbps" if bitrate else "视频"


def _build_entries(aweme: dict) -> list[Entry]:
    entries: list[Entry] = []
    video = aweme.get("video") or {}
    images = aweme.get("images") or []

    cover = (_url_list(video.get("origin_cover") or video.get("cover"))
             or _url_list(video.get("dynamic_cover")))[:1]

    # 视频：先给默认档，再给各清晰度
    base_urls = _url_list(video.get("play_addr"))
    if base_urls and not images:
        entries.append(Entry(label="视频（默认）", kind="video", urls=base_urls, thumb=cover))

    for br in video.get("bit_rate") or []:
        urls = _url_list(br.get("play_addr"))
        if urls:
            entries.append(Entry(label=f"视频 {_gear_label(br)}", kind="video",
                                 urls=urls, thumb=cover))

    if base_urls and images:
        # 图集作品的 video.play_addr 其实是配乐，单独标出来
        entries.append(Entry(label="配乐", kind="audio", urls=base_urls, thumb=cover))

    # 图集 / 实况
    total = len(images)
    for i, img in enumerate(images, 1):
        if not isinstance(img, dict):
            continue
        img_urls = _url_list(img)
        # 图集里的实况/动图：img["video"]["play_addr"]["url_list"][0] 就是那段 mp4
        live_obj = img.get("video")
        live_urls = _url_list(live_obj)
        if not live_urls and isinstance(live_obj, dict):
            live_urls = (_url_list(live_obj.get("play_addr"))
                         or _url_list(live_obj.get("play_addr_h264"))
                         or _url_list(live_obj.get("download_addr")))
        if not img_urls and not live_urls:
            continue
        thumb = img_urls[:1]
        if img_urls:
            entries.append(Entry(label=f"图片 {i}/{total}", kind="image",
                                 urls=img_urls, thumb=thumb))
        if live_urls:
            entries.append(Entry(label=f"实况 {i}/{total}", kind="live",
                                 urls=live_urls, thumb=thumb))

    music = aweme.get("music") or {}
    if not images:
        music_urls = _url_list(music.get("play_url"))
        if music_urls:
            entries.append(Entry(label="原声", kind="audio", urls=music_urls, thumb=cover))

    # 去重（同一 url 只保留第一条）
    seen: set[str] = set()
    uniq: list[Entry] = []
    for e in entries:
        key = e.urls[0] if e.urls else ""
        if key and key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq


def _item_from_aweme(aweme: dict, item_id: str, page_url: str) -> MediaItem:
    author = (aweme.get("author") or {})
    title = (aweme.get("desc") or aweme.get("item_title") or "").strip()
    entries = _build_entries(aweme)
    if not entries:
        raise ParseError(
            "接口返回了数据，但里面没有可下载的媒体地址。\n"
            "如果这是图集，可能是「仅自己可见」或已被风控过滤。"
        )
    return MediaItem(
        platform="douyin",
        item_id=str(aweme.get("aweme_id") or item_id),
        title=title,
        author=(author.get("nickname") or "").strip(),
        page_url=page_url or f"https://www.douyin.com/video/{item_id}",
        cover="",
        entries=entries,
        raw=aweme,
    )


# ---------------------------------------------------------------- 三级兜底

def _page_data(html: str):
    obj = _json_after(html, _ROUTER_MARKER)
    if obj is not None:
        return obj
    m = re.search(_RENDER_MARKER, html, re.S)
    if m:
        try:
            return json.loads(urllib.parse.unquote(m.group(1)))
        except json.JSONDecodeError:
            return None
    return None


def _item_from_html(html: str, page_url: str, item_id: str) -> MediaItem | None:
    data = _page_data(html)
    if not data:
        return None
    info = _find_key(data, "videoInfoRes") or _find_key(data, "aweme_detail")
    if not info:
        return None
    items = info.get("item_list") if isinstance(info, dict) else None
    if not items:
        return None
    aweme = items[0]
    if not isinstance(aweme, dict):
        return None
    return _item_from_aweme(aweme, item_id, page_url)


def _web_api_params(item_id: str, cookie: str, ua: str) -> dict[str, str]:
    web_id = "".join(random.choices(string.digits, k=19))
    m = _MS_TOKEN_RE.search(cookie)
    ms_token = m.group(1) if m else _rand_seq() + "=="
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": item_id,
        "pc_client_type": "1",
        "version_code": "190500",
        "version_name": "19.5.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "126.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "126.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "8",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "50",
        "webid": web_id,
        "msToken": ms_token,
    }


_DETAIL_HOSTS = ("www.douyin.com", "www.iesdouyin.com")


def _from_web_api(item_id: str, cookie: str, page_url: str) -> MediaItem:
    """本地签名 + 浏览器 Cookie 打官方详情接口；两个域名轮着试。"""
    errors: list[str] = []
    for host in _DETAIL_HOSTS:
        try:
            return _web_api_once(host, item_id, cookie, page_url)
        except ParseError as e:
            errors.append(f"{host}：{e}")
    raise ParseError(errors[-1] if errors else "抖音详情接口没有响应")


def _web_api_once(host: str, item_id: str, cookie: str, page_url: str) -> MediaItem:
    from ..sign.abogus import ABogus

    ua = http.UA_DESKTOP
    cookie = http.ascii_header(cookie)
    params = _web_api_params(item_id, cookie, ua)
    query = urllib.parse.urlencode(params)
    bogus = ABogus(ua).sign(params)
    url = (f"https://{host}/aweme/v1/web/aweme/detail/?{query}"
           f"&a_bogus={urllib.parse.quote(bogus, safe='')}")

    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Referer": f"https://www.douyin.com/video/{item_id}",
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=http.DEFAULT_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if "Uifid" in body or e.code == 403:
            raise ParseError(
                "抖音详情接口返回 403：本机 Cookie 里缺少 UIFID。\n"
                "请在浏览器里打开一集抖音视频，F12 → Network → 任意请求 →\n"
                "复制完整 Cookie（含 ttwid、UIFID、msToken），粘贴到设置里重试。"
            ) from e
        raise ParseError(f"抖音详情接口出错：HTTP {e.code} {body}") from e
    except OSError as e:
        raise ParseError(f"抖音详情接口连不上：{e}") from e

    if not raw:
        raise ParseError("抖音详情接口返回空数据（多半是 Cookie 失效或被风控）。")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise ParseError(f"抖音详情接口返回的不是 JSON：{raw[:120]!r}") from e

    aweme = payload.get("aweme_detail") or {}
    if not aweme:
        reason = (payload.get("filter_detail") or {}).get("filter_reason") or ""
        raise ParseError(
            "抖音没有返回作品数据"
            + (f"（{reason}）" if reason else "")
            + "。作品可能已删除/仅自己可见，或 Cookie 不完整。"
        )
    return _item_from_aweme(aweme, item_id, page_url)


_MP4_RE = re.compile(r"https?://[^\s\"'\\]+?\.mp4[^\s\"'\\]*")
_PAGE_HINT = ("douyin.com/video/", "douyin.com/note/", "iesdouyin.com/share")


def _is_media_url(url: str) -> bool:
    """排除第三方接口里顺手回传的「作品页面链接」。"""
    if not url or not url.startswith("http"):
        return False
    low = url.lower()
    if any(h in low for h in _PAGE_HINT):
        return False
    return any(k in low for k in (".mp4", "video_id=", "mime_type=video", "douyinvod",
                                  "aweme/v1/play", "zjcdn", ".m3u8"))


def _collect_media_urls(obj: Any, out: list[str], depth: int = 0) -> None:
    """递归收集所有像媒体直链的字符串，保持出现顺序。"""
    if depth > 7:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_media_urls(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _collect_media_urls(v, out, depth + 1)
    elif isinstance(obj, str) and _is_media_url(obj):
        if obj not in out:
            out.append(obj)


def _collect_images(obj: Any, out: list[str], depth: int = 0) -> None:
    """第三方接口若回传 images 数组，把里面的图片地址也收进来。"""
    if depth > 7 or len(out) >= 40:
        return
    if isinstance(obj, dict):
        imgs = obj.get("images") or obj.get("image_list") or obj.get("imageList")
        if isinstance(imgs, list):
            for im in imgs:
                if isinstance(im, str):
                    if im.startswith("http") and im not in out:
                        out.append(im)
                elif isinstance(im, dict):
                    for key in ("url", "url_list", "urlList", "origin_url"):
                        v = im.get(key)
                        if isinstance(v, str) and v.startswith("http"):
                            if v not in out:
                                out.append(v)
                            break
                        if isinstance(v, list):
                            for u in v:
                                if isinstance(u, str) and u.startswith("http") and u not in out:
                                    out.append(u)
                                    break
                            break
        for v in obj.values():
            _collect_images(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _collect_images(v, out, depth + 1)


def _deep_find(obj: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            got = _deep_find(v, keys, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _deep_find(v, keys, depth + 1)
            if got:
                return got
    return None


def _dedup(items: list[str]) -> list[str]:
    """保序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _as_urls(value: Any) -> list[str]:
    """把各种形态的字段（字符串 / 列表 / 字典）都摊平成 http 地址列表。"""
    out: list[str] = []
    if isinstance(value, str):
        if value.startswith("http"):
            out.append(value)
    elif isinstance(value, list):
        for v in value:
            out.extend(_as_urls(v))
    elif isinstance(value, dict):
        for key in ("url", "url_list", "urlList", "play_addr", "origin_url",
                    "masterUrl", "backupUrls", "video_url"):
            out.extend(_as_urls(value.get(key)))
    return out


def _third_party_known(data: Any) -> dict | None:
    """识别常见第三方接口的固定结构。

    目前认两种：

    - xingzhige：``{"code":"0","data":{"jx":{"type":…},"author":{"name":…},
      "item":{"title":…,"url":主链,"ury":备用链,"cover":…,"quality":…}}}``
    - bugpk：``{"code":200,"data":{"type":"video","title":…,"desc":…,
      "author":{"name":…,"avatar":…},"cover":…,"url":…,"video_backup":…,
      "music":{"title":…,"url":…},"quality":…,"duration":…,"size":…,
      "images":[…],"live_photo":…}}``
    """
    if not isinstance(data, dict):
        return None
    d = data.get("data")
    if not isinstance(d, dict):
        return None

    videos: list[str] = []
    images: list[str] = []
    lives: list[str] = []
    title = author = cover = quality = size = music = ""
    kind = ""

    # --- bugpk 这类「平铺在 data 上」的结构
    for key in ("url", "video_url", "video_backup", "nwm_video_url"):
        videos.extend(_as_urls(d.get(key)))
    for key in ("images", "image_list", "imageList", "image_urls"):
        images.extend(_as_urls(d.get(key)))
    for key in ("live_photo", "livePhoto", "live_photos"):
        lives.extend(_as_urls(d.get(key)))
    if videos or images or lives:
        title = str(d.get("title") or d.get("desc") or "").strip()
        autor = d.get("author")
        if isinstance(autor, dict):
            author = str(autor.get("name") or autor.get("nickname") or "").strip()
            cover = str(autor.get("avatar") or "").strip()
        elif isinstance(autor, str):
            author = autor.strip()
        cover = str(d.get("cover") or cover or "").strip()
        quality = str(d.get("quality") or "").strip()
        size = str(d.get("size_label") or d.get("size") or "").strip()
        mus = d.get("music")
        if isinstance(mus, dict):
            music = str(mus.get("url") or "").strip()
        kind = str(d.get("type") or "").strip()

    # --- xingzhige 这类「item 里套一层」的结构
    it = d.get("item")
    if not videos and not images and isinstance(it, dict):
        videos.extend(u for u in (it.get("url"), it.get("ury"), it.get("url_h265"))
                      if isinstance(u, str) and _is_media_url(u))
        images.extend(u for u in _as_urls(it.get("images")) if u.startswith("http"))
        if videos or images:
            title = str(it.get("title") or "").strip()
            autor = d.get("author")
            if isinstance(autor, dict):
                author = str(autor.get("name") or "").strip()
            cover = str(it.get("cover") or it.get("cover_gif") or "").strip()
            music = str(it.get("muisic") or "").strip()
            quality = str(it.get("quality") or "").strip()
            size = str(it.get("size_str") or "").strip()
            jx = d.get("jx")
            if isinstance(jx, dict):
                kind = str(jx.get("type") or "").strip()

    videos = [u for u in _dedup(videos) if _is_media_url(u)]
    images = _dedup([u for u in images if u.startswith("http")])
    lives = _dedup([u for u in lives if _is_media_url(u)])
    if not videos and not images and not lives:
        return None
    return {
        "videos": videos,
        "images": images,
        "lives": lives,
        "title": title,
        "author": author,
        "cover": cover,
        "music": music,
        "quality": quality,
        "size": size,
        "type": kind,
    }


_QUALITY_ZH = {
    "original": "原画", "origin": "原画", "uhd": "超清", "hd": "高清",
    "sd": "标清", "540p": "540p", "720p": "720p", "1080p": "1080p",
}


def _quality_label(raw: str) -> str:
    q = (raw or "").strip()
    if not q:
        return ""
    return _QUALITY_ZH.get(q.lower(), q)


def _templates(raw: str) -> list[str]:
    """配置里可以写多行/多个接口，按顺序依次试。"""
    parts = [p.strip() for p in re.split(r"[\r\n;,]+", raw or "")]
    return [p for p in parts if p and "{url}" in p]


def _from_third_party(url: str, template: str) -> MediaItem:
    """第三方解析接口：模板里用 `{url}` 占位。

    多个接口**并发**问（最多 4 个），谁先返回可用结果就用谁——
    串行时只要第一个接口超时，用户就得干等十几秒。
    """
    tpls = _templates(template)
    if not tpls:
        raise ParseError("没有配置第三方解析接口（设置里那栏要含 {url} 占位）")

    errors: dict[str, str] = {}

    def host_of(tpl: str) -> str:
        return urllib.parse.urlsplit(tpl).netloc or tpl

    if len(tpls) == 1:
        try:
            return _one_third_party(url, tpls[0])
        except ParseError as e:
            raise ParseError(f"第三方接口都没成功：\n- {host_of(tpls[0])}：{e}") from e
        except OSError as e:
            raise ParseError(f"第三方接口都没成功：\n- {host_of(tpls[0])}：连不上 {e}") from e

    pool = ThreadPoolExecutor(max_workers=min(4, len(tpls)))
    try:
        futures = {pool.submit(_one_third_party, url, t): t for t in tpls}
        for fut in as_completed(futures):
            tpl = futures[fut]
            try:
                item = fut.result()
            except ParseError as e:
                errors[host_of(tpl)] = str(e)
            except OSError as e:
                errors[host_of(tpl)] = f"连不上 {e}"
            else:
                pool.shutdown(wait=False, cancel_futures=True)
                return item
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    detail = "\n".join(f"- {h}：{e}" for h, e in errors.items()) or "- 全部超时"
    raise ParseError("第三方接口都没成功：\n" + detail)


def _one_third_party(url: str, template: str) -> MediaItem:
    """用单个模板问一次接口。先认固定结构，再退回通用收集。"""
    api = template.replace("{url}", urllib.parse.quote(url, safe=""))
    req = urllib.request.Request(api, headers={"User-Agent": http.UA_DESKTOP})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
    except OSError as e:
        raise ParseError(f"连不上（{e}）") from e
    text = raw.decode("utf-8", "replace")

    videos: list[str] = []
    images: list[str] = []
    lives: list[str] = []
    title = author = cover = quality = size = ""
    try:
        data = json.loads(text)
        known = _third_party_known(data)
        if known:
            videos = known["videos"]
            images = known["images"]
            lives = known["lives"]
            title, author, cover = known["title"], known["author"], known["cover"]
            quality, size = known["quality"], known["size"]
        else:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg") if isinstance(data, dict) else None
            if code not in (None, 0, "0", 200, "200"):
                raise ParseError(f"接口返回 code={code} {msg or ''}".strip())
            _collect_media_urls(data, videos)
            _collect_images(data, images)
            title = _deep_find(data, ("title", "desc", "text")) or ""
            author = _deep_find(data, ("name", "nickname")) or ""
            cover = _deep_find(data, ("cover", "cover_url", "coverUrl", "avatar")) or ""
    except json.JSONDecodeError:
        videos = [m.group(0) for m in _MP4_RE.finditer(text)]

    if not videos and not images and not lives:
        raise ParseError("没返回可下载地址：\n" + text[:200])

    entries: list[Entry] = []
    if videos:
        label = "视频" + (f" {_quality_label(quality)}" if quality else "")
        entries.append(Entry(label=label, kind="video", urls=videos,
                             thumb=[cover] if cover else []))
    total_img = len(images)
    for i, img in enumerate(images, 1):
        entries.append(Entry(label=f"图片 {i}/{total_img}", kind="image", urls=[img],
                             thumb=[img]))
    total_live = len(lives)
    for i, live in enumerate(lives, 1):
        entries.append(Entry(label=f"实况 {i}/{total_live}", kind="live", urls=[live],
                             thumb=[cover] if cover else []))

    note = "抖音内容由第三方接口解析；不保证一定无水印，图集/实况要看接口返回。"
    if size:
        note += f" 大小约 {size}。"
    return MediaItem(
        platform="douyin",
        item_id=_item_id_of(url),
        title=title,
        author=author,
        page_url=url,
        cover=cover,
        entries=entries,
        note=note,
    )


# ---------------------------------------------------------------- 主入口

def parse(text: str, cookie: str = "", third_party: str = "") -> MediaItem:
    """解析一条抖音分享链接/分享文案。

    通道顺序（按产品定位排的）：

    1. **第三方解析接口**——抖音把数据挪到签名接口后面之后，这是最省事、
       也最稳的一条路：我们只发一个 URL 出去，指纹、Cookie、签名都由对方维护。
    2. 手动 Cookie + 本地 a_bogus 签名——想自己掌控时用，需要浏览器 Cookie。
    3. 分享页内嵌数据——老页面还有 `_ROUTER_DATA` 时能用。

    短链（`v.douyin.com/…`）会先自己展开成 `www.douyin.com/video/{id}`，
    因为多数第三方接口只认后者。
    """
    url = extract_url(text)
    if not url:
        raise ParseError("没有从文本里识别到抖音链接")
    item_id = _item_id_of(url)
    tried: list[str] = []

    # 手动 Cookie 先洗干净：混进一个中文字符，http.client 就会抛
    # "'latin-1' codec can't encode ..."，用户完全看不懂。
    raw_cookie = cookie
    cookie = http.ascii_header(cookie or "").strip()
    if raw_cookie and raw_cookie != cookie:
        tried.append("手动 Cookie：里面有非 ASCII 字符（可能误粘了分享口令），已忽略这些字符")

    # ⓪ 短链先展开：既为了拿 item_id，也因为第三方接口多半只认完整链接
    page_url = url
    if not item_id or "v.douyin.com" in url:
        try:
            final, html = http.get(url, ua=http.UA_MOBILE)
            item_id = item_id or _item_id_of(final) or _item_id_of(html)
            page_url = final
        except OSError:
            html = ""
            page_url = url
    else:
        html = ""

    if not item_id and "v.douyin.com" in url and page_url.rstrip("/") == "https://www.douyin.com":
        tried.append("短链：展开后落到了抖音首页——这条短链多半已经失效，"
                     "请在抖音里重新复制一次分享链接")

    canonical = page_url
    if item_id and "douyin.com/video/" not in page_url:
        canonical = f"https://www.douyin.com/video/{item_id}"

    # ① 第三方解析接口（主路径）
    if third_party:
        try:
            return _from_third_party(canonical, third_party)
        except ParseError as e:
            tried.append(f"第三方接口：{e}")

    # ② 手动 Cookie + 本地签名
    if cookie and item_id:
        if not any(k in cookie for k in ("ttwid", "UIFID", "msToken")):
            tried.append("手动 Cookie：没看到 ttwid / UIFID / msToken，可能不是抖音的 Cookie")
        else:
            try:
                return _from_web_api(item_id, cookie, page_url)
            except ParseError as e:
                tried.append(f"官方接口：{e}")

    # ③ 分享页内嵌数据
    if item_id:
        try:
            if not html:
                _, page_html = http.get(
                    f"https://www.iesdouyin.com/share/video/{item_id}", ua=http.UA_MOBILE)
            else:
                page_html = html
            got = _item_from_html(page_html, page_url, item_id)
            if got:
                return got
            tried.append("分享页：没有可用的内嵌数据")
        except OSError as e:
            tried.append(f"分享页：{e}")

    detail = "\n".join(f"- {t}" for t in tried) if tried else "- 没有可用的通道"
    raise ParseError(
        "抖音这条没能解析出来，试试下面的办法：\n"
        "1) 在「设置 → 第三方解析接口」里填一个能用的解析接口（默认已经填了三个，\n"
        "   其中第一个目前是可用的；接口挂掉时换一个或加一行新的即可）；\n"
        "2) 或在设置里手动粘贴抖音 Cookie（含 ttwid / UIFID / msToken），程序会自己签名请求；\n"
        "3) 小红书不受影响，可以正常解析。\n\n"
        f"已经试过的通道：\n{detail}"
    )
