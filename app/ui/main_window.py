"""Tkinter 主窗口：粘贴链接 → 解析 → 勾选内容（带预览）→ 选目录下载。"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import webbrowser
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import theme as swiss
from .. import REPO_NAME, REPO_URL, __version__ as APP_VERSION
from .. import downloader, http, logbook, store
from ..downloader import DownloadCancelled, Progress
from ..img import preview, thumbs
from ..parser import ParseError, parse
from ..parser.base import Entry, MediaItem
from ..utils import extract_url, human_size

_KIND_LABEL = {
    "video": "视频",
    "image": "图片",
    "live": "实况",
    "audio": "音频",
}

_PLATFORM_LABEL = {"douyin": "抖音", "xhs": "小红书"}

PREVIEW_W = 320
PREVIEW_H = 200

# 勾选框：16px 的小方块在清单里太难分辨，改成「白底细边」与「IKB 实心 + 白勾」两张位图
BOX = 18


def _hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _box_image(checked: bool, size: int = BOX) -> tk.PhotoImage:
    """生成勾选框图片（P6 位图，Tk 原生支持，不依赖 Pillow）。"""
    w = h = size
    buf = bytearray(b"\xff\xff\xff" * (w * h))

    def put(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < w and 0 <= y < h:
            i = (y * w + x) * 3
            buf[i:i + 3] = bytes(color)

    def line(x0: int, y0: int, x1: int, y1: int, color, thick: int = 2) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            for k in range(thick):
                put(x0, y0 + k, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    if checked:
        edge = _hex_rgb(swiss.ACCENT)
        mark = _hex_rgb(swiss.ACCENT)
        tint = _hex_rgb("#DCE3F7")     # 淡蓝填充，与未选的纯白一眼分开
        thick = 2
        for y in range(h):
            for x in range(w):
                put(x, y, tint)
                if x < thick or y < thick or x >= w - thick or y >= h - thick:
                    put(x, y, edge)
        line(size // 5, size // 2, size // 2, size - size // 3, mark)
        line(size // 2, size - size // 3, size - size // 5, size // 4, mark)
    else:
        edge = _hex_rgb(swiss.GREY3)
        for y in range(h):
            for x in range(w):
                if x < 1 or y < 1 or x >= w - 1 or y >= h - 1:
                    put(x, y, edge)

    ppm = b"P6\n%d %d\n255\n" % (w, h) + bytes(buf)
    return tk.PhotoImage(data=ppm, format="PPM")


def _known_downloads() -> str:
    """用 Windows 的已知文件夹 API 取「下载」目录（OneDrive 重定向也能取到）。"""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
        guid = GUID(0x374DE290, 0x123F, 0x4565,
                    (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        out = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(out))
        if hr == 0 and out.value:
            path = str(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:  # noqa: BLE001 - 取不到就走下面的兜底
        pass
    return ""


def default_download_dir() -> str:
    """默认保存目录：Windows「下载」→ ~/Downloads → 用户主目录。"""
    known = _known_downloads()
    if known and os.path.isdir(known):
        return known
    home = Path.home()
    for name in ("Downloads", "下载"):
        p = home / name
        if p.is_dir():
            return str(p)
    return str(home)


def open_in_explorer(path: str) -> None:
    """打开资源管理器并定位到 path（不存在就打开它的父目录）。"""
    if not path:
        return
    try:
        if os.path.isdir(path):
            os.startfile(path)  # noqa: S606 - Windows 专用
        elif os.path.isdir(os.path.dirname(path)):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    except OSError:
        pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("素材下载器 · 抖音 / 小红书")
        self.geometry("1020x840")
        self.minsize(920, 700)

        self.cfg = store.load_config()
        self.q: queue.Queue = queue.Queue()
        self.item: MediaItem | None = None
        self.checked: set[int] = set()
        self.busy = False
        self._closing = False              # 工作线程只读这个普通标志，不碰 Tk
        self.cancel_event = threading.Event()
        self._preview_seq = 0
        self._thumbs: dict[str, tk.PhotoImage] = {}
        self._thumb_order: list[str] = []
        self._inflight: set[str] = set()   # 正在下载的缩略图地址，防重复请求
        self._shown: tk.PhotoImage | None = None
        self._canvas_msg = "解析后点某一行看预览"
        self._canvas_shift = 0
        self._last_out_dir = ""

        swiss.apply_theme(self)

        self._build_ui()
        self.after(35, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        px = swiss.PAD_X
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)   # 只有中间段吸收多余高度，底栏永不被裁

        # ── 顶栏：大标题 + 右侧工具
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", padx=px, pady=(18, 0))
        left = ttk.Frame(head)
        left.pack(side="left")
        ttk.Label(left, text="素材下载器", style="Title.TLabel").pack(anchor="w")
        tools = ttk.Frame(head)
        tools.pack(side="right")
        ttk.Button(tools, text="历史记录", command=self._show_history).pack(side="left")
        ttk.Button(tools, text="设置", command=self._show_settings).pack(side="left", padx=(8, 0))

        swiss.hairline(outer, pady=(14, 0), grid_row=1)

        body = ttk.Frame(outer)
        body.grid(row=2, column=0, sticky="nsew", padx=px, pady=(16, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        # ── 01 SOURCE
        s1 = ttk.Frame(body)
        s1.grid(row=0, column=0, sticky="ew")
        swiss.section_header(s1, "01", "SOURCE", "粘贴分享链接").pack(fill="x")

        row = ttk.Frame(s1)
        row.pack(fill="x", pady=(12, 0))
        self.txt_url = tk.Text(row, height=2, width=40, wrap="word", font=swiss.F_BODY,
                               bd=0, relief="flat", bg=swiss.PAPER, fg=swiss.INK,
                               insertbackground=swiss.INK, highlightthickness=1,
                               highlightbackground=swiss.GREY2,
                               highlightcolor=swiss.ACCENT, padx=10, pady=8, undo=True)
        self.txt_url.pack(side="left", fill="both", expand=True)
        self.txt_url.bind("<Control-Return>", lambda e: (self._on_parse(), "break")[1])

        btns = ttk.Frame(row)
        btns.pack(side="right", padx=(12, 0), fill="y")
        ttk.Button(btns, text="粘贴", width=8, command=self._on_paste).pack(fill="x")
        ttk.Button(btns, text="清空", width=8, command=self._on_clear).pack(fill="x", pady=6)
        self.btn_parse = ttk.Button(btns, text="解析", width=8,
                                    style="Accent.TButton", command=self._on_parse)
        self.btn_parse.pack(fill="x")

        # ── 02 SELECT / 03 PREVIEW
        mid = ttk.Frame(body)
        mid.grid(row=1, column=0, sticky="nsew", pady=(22, 0))
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=0)   # 清单按内容自然宽度
        mid.columnconfigure(1, weight=1)   # 预览吃掉剩余宽度

        left = ttk.Frame(mid)
        left.grid(row=0, column=0, sticky="nsew")
        swiss.section_header(left, "02", "SELECT", "选择要下载的内容").pack(fill="x")

        # 清单上方一条工具行：左边计数、右边切换按钮，左右边缘与表格对齐
        bar = ttk.Frame(left, height=34)
        bar.pack(fill="x", pady=(8, 6))
        bar.pack_propagate(False)
        bar.columnconfigure(0, weight=1)
        self.lbl_sel = ttk.Label(bar, text="未解析", style="Count.TLabel", anchor="w")
        self.lbl_sel.grid(row=0, column=0, sticky="w", pady=7)
        self.btn_all = ttk.Button(bar, text="全选", width=7, command=self._on_toggle_all)
        self.btn_all.grid(row=0, column=1, sticky="e", pady=3)

        wrap = ttk.Frame(left)
        wrap.pack(fill="both", expand=True)
        self._tree_wrap = wrap
        self._img_on = _box_image(True)
        self._img_off = _box_image(False)
        cols = ("type", "label", "size")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings",
                                 selectmode="browse", height=6,
                                 style="Swiss.Treeview")
        self.tree.heading("#0", text="选")
        self.tree.heading("type", text="类型")
        self.tree.heading("label", text="内容")
        self.tree.heading("size", text="大小")
        self.tree.column("#0", width=46, anchor="center", stretch=False)
        self.tree.column("type", width=64, anchor="center", stretch=False)
        self.tree.column("label", width=280, anchor="w")
        self.tree.column("size", width=84, anchor="e", stretch=False)
        self.tree.tag_configure("live", foreground=swiss.ACCENT)
        self.tree.tag_configure("audio", foreground=swiss.GREY3)
        # 已选：淡蓝底；未选：文字压灰。两档叠加色块+对勾，隔屏也能看清
        self.tree.tag_configure("on", background="#ECF0FB")
        self.tree.tag_configure("off", foreground=swiss.GREY3)

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", lambda e: (self._toggle_current(), "break")[1])
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        wrap.bind("<Configure>", self._fit_tree)

        right = ttk.Frame(mid)
        right.grid(row=0, column=1, sticky="nsew", padx=(26, 0))
        swiss.section_header(right, "03", "PREVIEW", "预览").pack(fill="x")

        # 右栏同一高度的信息行，保证两边内容顶边对齐
        rbar = ttk.Frame(right, height=34)
        rbar.pack(fill="x", pady=(8, 6))
        rbar.pack_propagate(False)
        self.lbl_info = ttk.Label(rbar, text="解析后点某一行看预览", style="Info.TLabel",
                                  anchor="w")
        self.lbl_info.grid(row=0, column=0, sticky="we", pady=7)
        rbar.columnconfigure(0, weight=1)

        holder = tk.Frame(right, bg=swiss.GREY2, bd=0, highlightthickness=0)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, width=PREVIEW_W, height=PREVIEW_H,
                                bg=swiss.PAPER, bd=0, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=1, pady=1)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # 解析进度条：平时不显示，解析时盖在画布中下部
        self.parse_bar = ttk.Progressbar(holder, mode="determinate", maximum=100,
                                         value=0,
                                         style="Parse.Horizontal.TProgressbar")
        self._parse_tick = None
        self._parse_pct = 0.0

        # ── 04 OUTPUT
        swiss.hairline(outer, pady=(16, 0), grid_row=3)
        bottom = ttk.Frame(outer)
        bottom.grid(row=4, column=0, sticky="ew", padx=px, pady=(12, 14))
        swiss.section_header(bottom, "04", "OUTPUT", "保存并下载").pack(fill="x")

        row1 = ttk.Frame(bottom)
        row1.pack(fill="x", pady=(12, 0))
        ttk.Label(row1, text="保存到", style="SectionZh.TLabel").pack(side="left", padx=(0, 12))
        self.var_dir = tk.StringVar(value=self.cfg.get("last_dir") or default_download_dir())
        self.ent_dir = ttk.Entry(row1, textvariable=self.var_dir, font=swiss.F_BODY)
        self.ent_dir.pack(side="left", fill="x", expand=True)
        ttk.Button(row1, text="浏览…", width=8,
                   command=self._on_browse).pack(side="left", padx=(10, 0))

        row2 = ttk.Frame(bottom)
        row2.pack(fill="x", pady=(10, 0))
        self.btn_download = ttk.Button(row2, text="下载选中", width=12,
                                       style="Accent.TButton",
                                       command=self._on_download, state="disabled")
        self.btn_download.pack(side="left")
        self.btn_cancel = ttk.Button(row2, text="取消", width=8,
                                     command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(10, 0))
        ttk.Button(row2, text="打开文件夹", width=12,
                   command=self._on_open_dir).pack(side="left", padx=(10, 0))

        self.bar = ttk.Progressbar(row2, mode="determinate", maximum=100,
                                   style="Swiss.Horizontal.TProgressbar")
        self.bar.pack(side="left", fill="x", expand=True, padx=(18, 0))

        self.lbl_status = ttk.Label(bottom, text="就绪", style="Status.TLabel", anchor="w")
        self.lbl_status.pack(fill="x", pady=(10, 0))
        self._status_text = "就绪"
        self._status_font = tkfont.Font(root=self, font=swiss.F_SM)
        bottom.bind("<Configure>", self._clip_status)

    # ------------------------------------------------------------ 小工具
    def _fit_tree(self, _event=None) -> None:
        """清单的可视行数随窗口高度伸缩，避免把底栏挤出窗口。"""
        h = self._tree_wrap.winfo_height()
        if h <= 1:
            return
        # 34 是表头，30 是行高；至少留一行，否则窗口一压就切出半个行
        rows = max(1, (h - 34) // 30)
        if int(self.tree.cget("height") or 0) != rows:
            self.tree.configure(height=rows)

    def _status(self, text: str) -> None:
        self._status_text = text
        self._clip_status()

    def _clip_status(self, _event=None) -> None:
        """状态行只占一行：装不下就在末尾省略，绝不让它把布局撑高。"""
        text = self._status_text
        avail = self.lbl_status.winfo_width() - 6
        if avail < 40:
            avail = max(160, self.winfo_width() - 120)
        if self._status_font.measure(text) <= avail:
            self.lbl_status.configure(text=text)
            return
        cut = text
        while cut and self._status_font.measure(cut + "…") > avail:
            cut = cut[:-1]
        self.lbl_status.configure(text=(cut + "…") if cut else text[:1])

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_parse.configure(state=state)
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self._refresh_download_button()

    def _refresh_download_button(self) -> None:
        ok = bool(self.item and self.checked) and not self.busy
        self.btn_download.configure(state="normal" if ok else "disabled")
        total = len(self.item.entries) if self.item else 0
        if self.item:
            self.lbl_sel.configure(text=f"已选 {len(self.checked)}/{total}")
        else:
            self.lbl_sel.configure(text="未解析")
        # 合并后的那个按钮：还没全选时写「全选」，已经全选时写「全不选」
        fully = bool(self.item) and len(self.checked) >= total and total > 0
        self.btn_all.configure(text="全不选" if fully else "全选",
                               state="normal" if self.item else "disabled")

    def _on_paste(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            self._status("剪贴板是空的")
            return
        self.txt_url.delete("1.0", "end")
        self.txt_url.insert("1.0", text.strip())
        self._status("已粘贴，点「解析」开始")

    def _on_clear(self) -> None:
        self.txt_url.delete("1.0", "end")
        self.item = None
        self.checked.clear()
        self.tree.delete(*self.tree.get_children())
        self._set_canvas_text("解析后点某一行看预览")
        self.lbl_info.configure(text="解析后点某一行看预览")
        self._refresh_download_button()
        self._status("就绪")

    # ------------------------------------------------------------ 解析
    def _on_parse(self) -> None:
        if self.busy:
            return
        text = self.txt_url.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("提示", "先把分享链接或分享文案粘进来。")
            return
        url = extract_url(text)
        if not url:
            messagebox.showinfo("提示", "没从这段文字里找到 http 链接。")
            return

        self.tree.delete(*self.tree.get_children())
        self._set_canvas_text("正在解析…", dy=-18)
        self._parse_progress(True)
        self.item = None
        self.checked.clear()
        self._set_busy(True)
        self._status("正在解析…")

        cookie = (self.cfg.get("douyin_cookie") or "").strip()
        third = (self.cfg.get("third_party") or "").strip()

        def work() -> None:
            try:
                self.q.put(("parsed", parse(url, cookie=cookie,
                                            third_party=third or store.DEFAULT_THIRD_PARTY)))
            except ParseError as e:
                self.q.put(("parse_error", str(e)))
            except Exception as e:  # noqa: BLE001
                logbook.log_exc("解析失败")
                self.q.put(("parse_error", f"解析失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_item(self, item: MediaItem) -> None:
        self.item = item
        self.checked.clear()
        self.tree.delete(*self.tree.get_children())

        for i, e in enumerate(item.entries):
            kind = _KIND_LABEL.get(e.kind, e.kind)
            # 默认勾选：主视频 + 所有图片 + 所有实况（跳过音频）
            if e.kind in ("image", "live"):
                self.checked.add(i)
            on = i in self.checked
            self.tree.insert("", "end", iid=str(i),
                             image=self._img_on if on else self._img_off, text="",
                             values=(kind, e.label, e.size_text() or "—"),
                             tags=(e.kind, "on" if on else "off"))
        first_video = next((i for i, e in enumerate(item.entries) if e.kind == "video"), None)
        if first_video is not None:
            self.checked.add(first_video)

        self._sync_checks()
        self._refresh_download_button()

        head = item.title or "（无标题）"
        plat = _PLATFORM_LABEL.get(item.platform, item.platform)
        who = f"　作者：{item.author}" if item.author else ""
        self._status(f"{plat}｜{head}{who} ｜ 共 {len(item.entries)} 项可下载")

        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
            # 当前这条正在下载，其余几条先在后台备好缩略图
            first = item.entries[0]
            cur = (first.thumb[0] if first.thumb else "") or item.cover or ""
            self._prefetch(item, skip=cur)
        self._probe_sizes(item)

    def _probe_sizes(self, item: MediaItem) -> None:
        """清单里不知道大小的条目，后台各问一次（只发 1 个字节的 Range 请求）。

        解析器能直接给出大小的（抖音视频的 `size`）就用现成的，剩下的
        图片/实况/小红书视频才需要探——解析完立刻显示清单，大小随后填进去。
        """
        todo = [(i, e) for i, e in enumerate(item.entries) if e.size <= 0 and e.urls]
        if not todo:
            return
        self._size_token = getattr(self, "_size_token", 0) + 1
        token = self._size_token

        def one(pair: tuple[int, Entry]) -> tuple[int, int]:
            idx, entry = pair
            for url in entry.urls[:2]:
                n = http.content_length(url)
                if n > 0:
                    return idx, n
            return idx, 0

        def work() -> None:
            with ThreadPoolExecutor(max_workers=6) as pool:
                for idx, n in pool.map(one, todo):
                    if self._closing or token != self._size_token:
                        return
                    if n > 0:
                        self.q.put(("size", idx, n))

        threading.Thread(target=work, daemon=True).start()

    def _sync_checks(self) -> None:
        for i in range(len(self.item.entries) if self.item else 0):
            e = self.item.entries[i]
            on = i in self.checked
            self.tree.item(str(i),
                           image=self._img_on if on else self._img_off,
                           tags=(e.kind, "on" if on else "off"))

    # ------------------------------------------------------------ 勾选
    def _on_tree_click(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        if region not in ("cell", "tree"):
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if col == "#0":  # 最左边的勾选框
            self._toggle(int(row))

    def _toggle_current(self) -> None:
        sel = self.tree.selection()
        if sel:
            self._toggle(int(sel[0]))

    def _toggle(self, idx: int) -> None:
        if not self.item or idx >= len(self.item.entries):
            return
        if idx in self.checked:
            self.checked.discard(idx)
        else:
            self.checked.add(idx)
        self._sync_checks()
        self._refresh_download_button()

    def _select_all(self) -> None:
        if not self.item:
            return
        self.checked = set(range(len(self.item.entries)))
        self._sync_checks()
        self._refresh_download_button()

    def _select_none(self) -> None:
        self.checked.clear()
        self._sync_checks()
        self._refresh_download_button()

    def _on_toggle_all(self) -> None:
        """一个按钮两用：没全选就全选，已经全选就全部取消。"""
        if not self.item:
            return
        if len(self.checked) >= len(self.item.entries):
            self._select_none()
        else:
            self._select_all()

    def _select_invert(self) -> None:
        if not self.item:
            return
        allidx = set(range(len(self.item.entries)))
        self.checked = allidx - self.checked
        self._sync_checks()
        self._refresh_download_button()

    # ------------------------------------------------------------ 预览
    def _canvas_center(self) -> tuple[int, int]:
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 1:
            w = PREVIEW_W
        if h <= 1:
            h = PREVIEW_H
        return w // 2, h // 2

    def _on_canvas_resize(self, _event=None) -> None:
        """画布随窗口伸缩：把当前图片/提示文字重新居中。"""
        if self._shown is not None:
            self._draw_thumb(self._shown)
        elif self._canvas_msg:
            self._set_canvas_text(self._canvas_msg, self._canvas_shift)

    def _set_canvas_text(self, text: str, dy: int = 0) -> None:
        self._canvas_msg = text
        self._canvas_shift = dy
        self._shown = None
        cx, cy = self._canvas_center()
        cy += dy
        self.canvas.delete("all")
        self.canvas.create_text(cx, cy, text=text, fill=swiss.GREY3, font=swiss.F_SM,
                                width=max(80, self.canvas.winfo_width() - 24),
                                justify="center")

    def _parse_progress(self, on: bool) -> None:
        """解析期间在预览框里显示进度条。

        解析耗时不可预知，所以用「慢速爬升」：最多爬到 90% 停住，
        解析真正返回时才补到 100% 并收起 —— 不会假装已经完成。
        """
        if on:
            self._parse_pct = 0.0
            try:
                self.parse_bar.configure(value=0)
                self.parse_bar.place(relx=0.5, rely=0.60, anchor="center",
                                     relwidth=0.56)
            except tk.TclError:
                return
            self._tick_parse()
        else:
            if self._parse_tick is not None:
                try:
                    self.after_cancel(self._parse_tick)
                except (tk.TclError, ValueError):
                    pass
                self._parse_tick = None
            try:
                self.parse_bar.configure(value=100)
                self.parse_bar.place_forget()
            except tk.TclError:
                pass

    def _tick_parse(self) -> None:
        """让进度条缓慢逼近 90%，越接近越慢。"""
        self._parse_tick = None
        if self._parse_pct >= 90:
            return
        step = max(0.6, (90 - self._parse_pct) / 14.0)
        self._parse_pct = min(90.0, self._parse_pct + step)
        try:
            self.parse_bar.configure(value=self._parse_pct)
        except tk.TclError:
            return
        self._parse_tick = self.after(110, self._tick_parse)

    def _on_tree_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel or not self.item:
            return
        idx = int(sel[0])
        e = self.item.entries[idx]
        kind = _KIND_LABEL.get(e.kind, e.kind)
        self.lbl_info.configure(text=f"{e.label}　·　{kind}")
        self._preview_for(e)

    def _preview_for(self, e: Entry) -> None:
        url = (e.thumb[0] if e.thumb else "") or ""
        if not url and self.item:
            url = self.item.cover or ""
        if not url:
            self._set_canvas_text("（无可预览的封面）")
            return

        cached = self._thumbs.get(url)
        if cached is not None:
            self._draw_thumb(cached)
            return

        self._set_canvas_text("加载预览…")
        self._preview_seq += 1
        seq = self._preview_seq
        if url in self._inflight:      # 同一条已经在下了，别再开一个线程
            return
        self._inflight.add(url)

        def work() -> None:
            try:
                data = thumbs.fetch(url, http)      # CDN 先缩图 + 本地缓存
                ppm = None
                try:
                    w, h, rgb = preview.load_thumb(data=data, max_w=PREVIEW_W, max_h=PREVIEW_H)
                    ppm = preview.to_ppm(w, h, rgb)
                except Exception:  # noqa: BLE001 - 解码失败就不显示
                    ppm = None
                self.q.put(("preview", seq, url, ppm))
            except Exception:  # noqa: BLE001
                self.q.put(("preview", seq, url, None))
            finally:
                self._inflight.discard(url)

        threading.Thread(target=work, daemon=True).start()

    def _prefetch(self, item: MediaItem, skip: str = "") -> None:
        """解析完先把前几条的缩略图偷偷备好，点哪条都快。"""
        urls: list[str] = []
        for e in item.entries:
            u = (e.thumb[0] if e.thumb else "") or item.cover or ""
            if u and u != skip and u not in self._thumbs and u not in urls:
                urls.append(u)
        urls = urls[:6]
        if not urls:
            return

        def work() -> None:
            for u in urls:
                if self._closing:          # 注意：工作线程里不能碰 Tk，只能读普通标志
                    return
                try:
                    data = thumbs.fetch(u, http)
                    w, h, rgb = preview.load_thumb(data=data, max_w=PREVIEW_W, max_h=PREVIEW_H)
                    self.q.put(("warm", u, preview.to_ppm(w, h, rgb)))
                except Exception:  # noqa: BLE001 - 预热失败无所谓
                    continue

        threading.Thread(target=work, daemon=True).start()

    def _draw_thumb(self, img: tk.PhotoImage) -> None:
        self._canvas_msg = ""
        self._shown = img
        cx, cy = self._canvas_center()
        self.canvas.delete("all")
        self.canvas.create_image(cx, cy, image=img)

    def _store_thumb(self, url: str, ppm: bytes) -> None:
        try:
            img = tk.PhotoImage(data=ppm, format="PPM")
        except tk.TclError:
            return
        self._thumbs[url] = img
        self._thumb_order.append(url)
        while len(self._thumb_order) > 24:
            old = self._thumb_order.pop(0)
            self._thumbs.pop(old, None)

    # ------------------------------------------------------------ 下载
    def _on_browse(self) -> None:
        start = self.var_dir.get() or default_download_dir()
        if not os.path.isdir(start):
            start = default_download_dir()
        picked = filedialog.askdirectory(title="选择保存目录", initialdir=start,
                                         mustexist=True)
        if picked:
            self.var_dir.set(os.path.normpath(picked))

    def _on_download(self) -> None:
        if self.busy or not self.item or not self.checked:
            return
        self._on_browse()  # 下载时总是让用户确认/选择目录
        out_dir = self.var_dir.get().strip()
        if not out_dir:
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("目录不可用", f"无法创建/写入该目录：\n{out_dir}\n\n{e}")
            return

        entries = [self.item.entries[i] for i in sorted(self.checked)]
        self._set_busy(True)
        self.cancel_event.clear()
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self._status("准备下载…")

        def on_status(text: str) -> None:
            self.q.put(("status", text))

        def on_progress(p: Progress) -> None:
            self.q.put(("progress", p))

        def work() -> None:
            try:
                files = downloader.download(self.item, entries, out_dir,
                                            on_status=on_status,
                                            on_progress=on_progress,
                                            cancel=self.cancel_event)
                self.q.put(("done", files, out_dir))
            except DownloadCancelled:
                self.q.put(("cancelled", None))
            except Exception as e:  # noqa: BLE001
                logbook.log_exc("下载失败")
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_cancel(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self._status("正在取消…")

    def _on_open_dir(self) -> None:
        path = self._last_out_dir or self.var_dir.get().strip()
        if path and os.path.isdir(path):
            open_in_explorer(path)
        else:
            self._status("这个目录还不存在")

    def _finish_download(self, files: list[str], out_dir: str) -> None:
        self.bar.stop()
        self.bar.configure(mode="determinate", value=100 if files else 0)
        self._last_out_dir = out_dir
        self._set_busy(False)

        if self.cfg.get("remember_dir"):
            self.cfg["last_dir"] = out_dir
            store.save_config(self.cfg)

        if self.item:
            total = 0
            for f in files:
                try:
                    total += os.path.getsize(f)
                except OSError:
                    pass
            store.add_history(self.item.platform, self.item.item_id, self.item.title,
                              self.item.author, f"{len(files)} 个文件", out_dir,
                              files, total)

        names = "\n".join(os.path.basename(f) for f in files[:8])
        more = f"\n… 另有 {len(files) - 8} 个" if len(files) > 8 else ""
        self._status(f"完成：{len(files)} 个文件 → {out_dir}")
        if self.cfg.get("auto_open"):
            open_in_explorer(files[0] if len(files) == 1 else out_dir)
        messagebox.showinfo("下载完成", f"已保存 {len(files)} 个文件到：\n{out_dir}\n\n{names}{more}")

    # ------------------------------------------------------------ 设置
    def _toggle_row(self, parent, text: str, var: tk.BooleanVar,
                    sub: str = "") -> ttk.Frame:
        """一行「方框 + 文字」的开关，点哪儿都能切。"""
        row = ttk.Frame(parent)
        img = tk.Label(row, bg=swiss.PAPER, bd=0)
        img.configure(image=self._img_on if var.get() else self._img_off)
        img.pack(side="left")
        lbl = ttk.Label(row, text=text, style="Body.TLabel" if sub else "Info.TLabel")
        lbl.pack(side="left", padx=(10, 0))

        def flip(_event=None):
            var.set(not bool(var.get()))
            img.configure(image=self._img_on if var.get() else self._img_off)
            return "break"

        for widget in (img, lbl):
            widget.bind("<Button-1>", flip)
            try:
                widget.configure(cursor="hand2")
            except tk.TclError:
                pass
        if sub:
            ttk.Label(row, text=sub, style="Meta.TLabel").pack(side="left", padx=(8, 0))
        return row

    def _show_settings(self) -> None:
        win = tk.Toplevel(self)
        win.title("设置 · SETTINGS")
        win.geometry("720x560")
        win.transient(self)
        win.grab_set()
        win.configure(background=swiss.PAPER)

        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=swiss.PAD_X, pady=(20, 16))

        swiss.section_header(wrap, "01", "API", "第三方解析接口（抖音）").pack(fill="x")
        ttk.Frame(wrap, height=8).pack()
        txt_tp = tk.Text(wrap, height=6, font=swiss.F_SM, wrap="char",
                         bd=0, relief="flat", bg=swiss.PAPER, fg=swiss.INK,
                         insertbackground=swiss.INK, highlightthickness=1,
                         highlightbackground=swiss.GREY2,
                         highlightcolor=swiss.ACCENT, padx=8, pady=6)
        txt_tp.pack(fill="x")
        txt_tp.insert("1.0", self.cfg.get("third_party") or store.DEFAULT_THIRD_PARTY)

        ttk.Frame(wrap, height=16).pack()
        swiss.section_header(wrap, "02", "OPTIONS", "选项").pack(fill="x")
        var_remember = tk.BooleanVar(value=bool(self.cfg.get("remember_dir")))
        var_open = tk.BooleanVar(value=bool(self.cfg.get("auto_open")))
        self._toggle_row(wrap, "记住上次的保存目录", var_remember).pack(
            anchor="w", pady=(10, 0))
        self._toggle_row(wrap, "下载完成后自动打开文件夹", var_open).pack(
            anchor="w", pady=(8, 0))

        ttk.Frame(wrap, height=16).pack()
        swiss.section_header(wrap, "03", "ABOUT", "关于").pack(fill="x")

        about = ttk.Frame(wrap)
        about.pack(fill="x", pady=(10, 0))
        ttk.Label(about, text=f"版本 v{APP_VERSION}　·　MIT 许可证　·　源码仓库",
                  style="Meta.TLabel").pack(anchor="w")

        link = tk.Label(about, text=REPO_URL, bg=swiss.PAPER, fg=swiss.ACCENT,
                        font=swiss.F_SM, cursor="hand2", anchor="w")
        link.pack(anchor="w", pady=(6, 0))

        def open_repo(_e=None) -> None:
            webbrowser.open(REPO_URL)

        link.bind("<Button-1>", open_repo)

        swiss.hairline(wrap, pady=(14, 0))
        bar = ttk.Frame(wrap)
        bar.pack(fill="x", pady=(12, 0))

        def save() -> None:
            self.cfg["third_party"] = txt_tp.get("1.0", "end").strip()
            self.cfg["remember_dir"] = bool(var_remember.get())
            self.cfg["auto_open"] = bool(var_open.get())
            store.save_config(self.cfg)
            win.destroy()
            self._status("设置已保存")

        ttk.Button(bar, text="保存", width=10, style="Accent.TButton",
                   command=save).pack(side="right")
        ttk.Button(bar, text="取消", width=10, command=win.destroy).pack(
            side="right", padx=(0, 10))

    # ------------------------------------------------------------ 历史
    def _show_history(self) -> None:
        win = tk.Toplevel(self)
        win.title("下载历史 · HISTORY")
        win.geometry("960x600")
        win.minsize(760, 460)
        win.transient(self)
        win.configure(background=swiss.PAPER)

        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=swiss.PAD_X, pady=(20, 16))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(1, weight=1)

        swiss.section_header(wrap, "01", "HISTORY", "下载记录").grid(
            row=0, column=0, sticky="we")

        body = ttk.Frame(wrap)
        body.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        cols = ("time", "platform", "title", "count", "dir")
        tree = ttk.Treeview(body, columns=cols, show="headings",
                            style="Swiss.Treeview")
        for c, t, w in (("time", "时间", 150), ("platform", "平台", 74),
                        ("title", "标题", 330), ("count", "文件", 64),
                        ("dir", "保存位置", 300)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w",
                        stretch=(c in ("title", "dir")))
        sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        rows = []
        for r in store.list_history(300):
            import datetime
            ts = datetime.datetime.fromtimestamp(r.get("created_at") or 0)
            rows.append((ts.strftime("%Y-%m-%d %H:%M"),
                         _PLATFORM_LABEL.get(r.get("platform"), ""),
                         (r.get("title") or "").replace("\n", " "),
                         str(r.get("file_count") or 0),
                         r.get("out_dir") or "",
                         r.get("files") or []))
        for i, (t, p, title, cnt, d, _files) in enumerate(rows):
            tree.insert("", "end", iid=str(i), values=(t, p, title, cnt, d))

        def open_sel(_e=None) -> None:
            sel = tree.selection()
            if not sel:
                return
            files = rows[int(sel[0])][5]
            if files:
                open_in_explorer(files[0])
            else:
                open_in_explorer(rows[int(sel[0])][4])

        tree.bind("<Double-1>", open_sel)

        def clear() -> None:
            if not messagebox.askyesno("清空历史",
                                       "确定清空所有下载记录吗？（不会删除已下载的文件）",
                                       parent=win):
                return
            store.clear_history()
            tree.delete(*tree.get_children())

        swiss.hairline(wrap, pady=(12, 0), grid_row=2)
        foot = ttk.Frame(wrap)
        foot.grid(row=3, column=0, sticky="we", pady=(10, 0))
        ttk.Label(foot, text="双击一行打开文件所在位置",
                  style="Meta.TLabel").pack(side="left")
        ttk.Button(foot, text="清空历史", width=12, command=clear).pack(side="right")

    # ------------------------------------------------------------ 事件泵
    def _pump(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "parsed":
                    self._parse_progress(False)
                    self._set_busy(False)
                    self._apply_item(msg[1])
                elif kind == "parse_error":
                    self._parse_progress(False)
                    self._set_busy(False)
                    self._status("解析失败")
                    messagebox.showerror("解析失败", msg[1])
                elif kind == "status":
                    self._status(msg[1])
                elif kind in ("session", "session_error"):
                    self._status(msg[1])  # 浏览器会话通道已取消，保留分支防旧队列消息
                elif kind == "progress":
                    self._on_progress(msg[1])
                elif kind == "preview":
                    _, seq, url, ppm = msg
                    if seq != self._preview_seq:
                        continue
                    if ppm:
                        self._store_thumb(url, ppm)
                        img = self._thumbs.get(url)
                        if img is not None:
                            self._draw_thumb(img)
                    else:
                        self._set_canvas_text("（预览加载失败）")
                elif kind == "warm":
                    _, url, ppm = msg
                    if url not in self._thumbs:
                        self._store_thumb(url, ppm)
                elif kind == "size":
                    _, idx, nbytes = msg
                    if self.item and 0 <= idx < len(self.item.entries):
                        self.item.entries[idx].size = nbytes
                        try:
                            self.tree.set(str(idx), "size", human_size(nbytes))
                        except tk.TclError:
                            pass
                elif kind == "done":
                    self._finish_download(msg[1], msg[2])
                elif kind == "cancelled":
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                    self._set_busy(False)
                    self._status("已取消")
                elif kind == "error":
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                    self._set_busy(False)
                    self._status("下载失败")
                    hint = f"\n\n详细日志：{logbook.log_path()}"
                    messagebox.showerror("下载失败", f"{msg[1]}{hint}")
        except queue.Empty:
            pass
        self.after(35, self._pump)

    def _on_progress(self, p: Progress) -> None:
        if p.total > 0:
            if str(self.bar.cget("mode")) != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar.configure(value=p.done * 100.0 / p.total)
        else:
            if str(self.bar.cget("mode")) != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(12)
        speed = f"{human_size(int(p.speed))}/s" if p.speed else ""
        self._status(f"下载中 {p.index}/{p.count}：{p.name}　"
                     f"{human_size(p.done)}" + (f" / {human_size(p.total)}" if p.total else "")
                     + (f"　{speed}" if speed else ""))

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("还在忙", "解析/下载还没结束，确定退出吗？"):
                return
            self.cancel_event.set()
        self._closing = True
        self.destroy()
