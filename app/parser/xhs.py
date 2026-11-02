"""小红书解析：笔记页 __INITIAL_STATE__ → 无水印视频 / 原图 / 实况。

关键点：
- 分享短链 xhslink.com 会 302 到 /explore/<note_id>?xsec_token=…，token 必须跟着请求走；
- 视频无水印靠 video.consumer.originVideoKey 拼 sns-video-bd.xhscdn.com；
- 图片原图走 fileId 直连 `sns-img-bd.xhscdn.com/<fileId>?imageView2/2/w/format/jpg`
  （`!nd_dft…` 样式后缀不能删，删了 CDN 直接 403）；
- 实况图（live photo）：imageList[i].livePhoto 为真，动图地址在
  imageList[i].stream.h264[0].masterUrl，下载下来就是一段 mp4。
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from .. import http
from ..utils import extract_url
from .base import Entry, MediaItem, ParseError

_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item|item)/([0-9a-zA-Z]{16,32})")
_STATE_MARKER = r"window\.__INITIAL_STATE__\s*=\s*"


def _initial_state(html: str):
    m = re.search(_STATE_MARKER, html)
    if not m:
        return None
    rest = html[m.end():]
    # __INITIAL_STATE__ 里含裸 undefined，JSON 解析前换成 null
    rest = re.sub(r"(?<![\w\"])undefined(?![\w\"])", "null", rest)
    try:
        obj, _ = json.JSONDecoder().raw_decode(rest)
        return obj
    except json.JSONDecodeError:
        return None


def _note_id_of(text: str) -> str:
    m = _NOTE_ID_RE.search(text)
    return m.group(1) if m else ""


def _clean(url: str) -> str:
    if not url or not isinstance(url, str):
        return ""
    url = url.replace("\\u002F", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


def _raw_image(url: str) -> str:
    """只去掉查询参数，保留 `!nd_dft…` 样式后缀。

    实测：把 `!` 后面的样式段删掉，CDN 会直接 403；真正的原图走 fileId 直连。
    """
    return _clean(url).split("?")[0]


_IMG_ORIGIN_HOSTS = ("https://sns-img-bd.xhscdn.com/", "https://sns-img-qc.xhscdn.com/")
_IMG_SCENES = ("WB_DFT", "CRD_WM_WEBP", "WB_PRV")


def _origin_by_file_id(file_id: str) -> list[str]:
    """fileId 直连 → 无损原图。默认是 HEIC，加 CDN 参数转成 JPEG 更好用。"""
    fid = _clean(file_id).lstrip("/")
    if not fid:
        return []
    return [h + fid + "?imageView2/2/w/format/jpg" for h in _IMG_ORIGIN_HOSTS]


def _image_candidates(img: dict) -> list[str]:
    out: list[str] = list(_origin_by_file_id(img.get("fileId") or ""))

    infos = [i for i in (img.get("infoList") or []) if isinstance(i, dict)]
    for scene in _IMG_SCENES:
        for info in infos:
            if (info.get("imageScene") or "") == scene:
                out.append(_raw_image(info.get("url") or ""))

    for key in ("urlDefault", "urlPre", "url"):
        out.append(_raw_image(img.get(key) or ""))

    # 兜底：fileId 直连的 HEIC 版本（体积与画质最大，但 Windows 可能打不开）
    fid = _clean(img.get("fileId") or "").lstrip("/")
    if fid:
        out.extend(h + fid for h in _IMG_ORIGIN_HOSTS)

    seen, uniq = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _video_candidates(video: dict) -> list[str]:
    out: list[str] = []
    key = _clean((video.get("consumer") or {}).get("originVideoKey") or "")
    if key:
        out.append("https://sns-video-bd.xhscdn.com/" + key.lstrip("/"))

    stream = (video.get("media") or {}).get("stream") or {}
    for codec in ("h264", "h265", "av1"):
        for v in stream.get(codec) or []:
            master = _clean((v or {}).get("masterUrl") or "")
            if master:
                out.append(master)
            out.extend(_clean(u) for u in (v or {}).get("backupUrls") or [])

    seen, uniq = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _live_candidates(img: dict) -> list[str]:
    """实况图的动图部分（一段短的 mp4）。结构在不同版本里有几种写法，都试一遍。"""
    out: list[str] = []
    stream = img.get("stream") or {}
    if isinstance(stream, dict):
        for codec in ("h264", "h265", "av1"):
            for v in stream.get(codec) or []:
                if isinstance(v, dict):
                    master = _clean(v.get("masterUrl") or "")
                    if master:
                        out.append(master)
                    out.extend(_clean(u) for u in v.get("backupUrls") or [])

    video = img.get("video") or {}
    if isinstance(video, dict):
        out.extend(_video_candidates(video))

    seen, uniq = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _is_live(img: dict) -> bool:
    if img.get("livePhoto") in (True, 1, "1", "true"):
        return True
    stream = img.get("stream")
    return isinstance(stream, dict) and bool(stream.get("h264"))


def _pick_note(state: dict, note_id: str) -> tuple[dict, str]:
    note_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    if isinstance(note_map, dict):
        entry = note_map.get(note_id)
        if isinstance(entry, dict) and entry.get("note"):
            return entry["note"], note_id
        for k, v in note_map.items():
            if isinstance(v, dict) and v.get("note"):
                return v["note"], k
    return {}, note_id


def parse(text: str) -> MediaItem:
    url = extract_url(text)
    if not url:
        raise ParseError("没有从文本里识别到小红书链接")

    final_url, html = http.get(url, ua=http.UA_DESKTOP)
    note_id = _note_id_of(final_url) or _note_id_of(html)
    token = (parse_qs(urlparse(final_url).query).get("xsec_token") or [""])[0]

    state = _initial_state(html)
    if state is None and note_id:
        retry = f"https://www.xiaohongshu.com/explore/{note_id}"
        if token:
            retry += f"?xsec_token={token}&xsec_source=pc_share"
        final_url, html = http.get(retry, ua=http.UA_DESKTOP)
        state = _initial_state(html)

    if state is None:
        raise ParseError(
            "没有解析到笔记数据：可能触发了风控/登录墙，或页面结构已改。\n"
            "试着先在本机浏览器打开一次该链接，再回来解析。"
        )

    note, note_id = _pick_note(state, note_id)
    if not note:
        raise ParseError("笔记数据为空：内容可能已删除、仅自己可见，或触发了登录墙。")

    entries: list[Entry] = []

    video_urls = _video_candidates(note.get("video") or {})
    if video_urls:
        entries.append(Entry(label="视频（无水印）", kind="video", urls=video_urls))

    images = [i for i in (note.get("imageList") or []) if isinstance(i, dict)]
    total = len(images)
    for i, img in enumerate(images, 1):
        cands = _image_candidates(img)
        if not cands:
            continue
        entries.append(Entry(label=f"图片 {i}/{total}", kind="image",
                             urls=cands, thumb=cands[:2]))
        if _is_live(img):
            live = _live_candidates(img)
            if live:
                entries.append(Entry(label=f"实况 {i}/{total}", kind="live",
                                     urls=live, thumb=cands[:2]))

    if not entries:
        raise ParseError("这条笔记里没有可下载的媒体（纯文字笔记）。")

    user = note.get("user") or note.get("userInfo") or {}
    title = (note.get("title") or "").strip()
    desc = (note.get("desc") or "").strip()
    if not title:
        title = desc[:40]

    cover = ""
    if images:
        cands = _image_candidates(images[0])
        cover = cands[0] if cands else ""

    has_live = any(e.kind == "live" for e in entries)
    return MediaItem(
        platform="xhs",
        item_id=note_id,
        title=title,
        author=(user.get("nickname") or "").strip(),
        page_url=final_url,
        cover=cover,
        entries=entries,
        note=("含实况图：勾选「实况」会下载对应的一段 mp4（本身就是视频，无需转码）。"
              if has_live else ""),
        raw=note,
    )
