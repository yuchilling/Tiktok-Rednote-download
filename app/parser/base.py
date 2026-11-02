"""统一的数据结构：解析器只负责把平台数据翻译成 MediaItem。

一条笔记/作品 = 一个 :class:`MediaItem`，里面是若干 :class:`Entry`。
Entry 是「用户能单独勾选下载的最小单位」——一个视频档位、一张图、一段实况。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ParseError(RuntimeError):
    """解析失败：链接不对、风控、结构变更、内容已删除等。"""


@dataclass
class Entry:
    """一个可单独下载的条目。"""

    label: str                       # 显示给用户的名称，如 "视频 1080p"、"图片 3/9"、"实况 1/9"
    kind: str                        # video / image / live / audio
    urls: list[str] = field(default_factory=list)   # 候选直链，按优先级尝试
    thumb: list[str] = field(default_factory=list)  # 预览用图片候选
    size: int = 0                    # 字节数；0 = 未知（界面会自己去问一次）

    def is_media(self) -> bool:
        return bool(self.urls) and self.kind in ("video", "image", "live", "audio")

    def size_text(self) -> str:
        """给界面显示用的大小文字。"""
        if self.size <= 0:
            return ""
        from ..utils import human_size
        return human_size(self.size)

    def describe(self) -> str:
        return {
            "video": "视频",
            "image": "图片",
            "live": "实况",
            "audio": "音频",
        }.get(self.kind, self.kind)


@dataclass
class MediaItem:
    platform: str                 # douyin / xhs
    item_id: str
    title: str = ""
    author: str = ""
    page_url: str = ""
    cover: str = ""
    entries: list[Entry] = field(default_factory=list)
    note: str = ""                # 补充说明（例如「抖音需要 Cookie」）
    raw: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.entries = [e for e in self.entries if e.urls]
        if not self.cover and self.entries:
            self.cover = self.entries[0].thumb[0] if self.entries[0].thumb else ""

    # ---- 便捷视图 ----
    def image_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.kind in ("image", "live")]

    def video_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "video"]

    def has_album(self) -> bool:
        return len(self.image_entries()) > 0
