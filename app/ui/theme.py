"""瑞士国际主义（Swiss Style）主题。

取色与规则来自 guizang-ppt-skill 的 `references/themes-swiss.md`「🔵 克莱因蓝 IKB」预设：
单一锚点色 + 高级灰白底 + 直角纯色 + 1px hairline，不许渐变 / 阴影 / 圆角 / 多色高亮。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 色板
PAPER = "#FAFAF8"      # 主底色（极浅暖白）
INK = "#0A0A0A"        # 文字主色
GREY1 = "#F0F0EE"      # 浅灰底
GREY2 = "#D4D4D2"      # 中灰：hairline / 边框
GREY3 = "#737373"      # 暗灰：辅助文字 / meta
ACCENT = "#002FA7"     # IKB 克莱因蓝（唯一高亮色）
ACCENT_ON = "#FFFFFF"  # 蓝底上的字

# ---------------------------------------------------------------- 字体
# 瑞士风全程无衬线：中文用 YaHei UI（Helvetica 气质），拉丁小标用 Arial。
F_TITLE = ("Microsoft YaHei UI", 19, "bold")
F_BODY = ("Microsoft YaHei UI", 10)
F_BOLD = ("Microsoft YaHei UI", 10, "bold")
F_SM = ("Microsoft YaHei UI", 9)
F_NUM = ("Arial", 22)
F_META = ("Arial", 8, "bold")

PAD_X = 24


def hairline(parent: tk.Misc, pady: tuple[int, int] = (0, 0),
             grid_row: int | None = None) -> tk.Frame:
    """1px 分割线。瑞士风里这是唯一允许的「装饰」。

    `grid_row` 给了就走 grid（用于整窗用 grid 排版的场合），否则 pack。
    """
    line = tk.Frame(parent, height=1, background=GREY2, bd=0, highlightthickness=0)
    if grid_row is None:
        line.pack(fill="x", pady=pady)
    else:
        line.grid(row=grid_row, column=0, sticky="ew", pady=pady)
    return line


def apply_theme(root: tk.Misc) -> ttk.Style:
    """把 clam 主题改成瑞士风：直角、纯色、hairline。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    try:
        root.configure(background=PAPER)
    except tk.TclError:
        pass

    style.configure(".", background=PAPER, foreground=INK, font=F_BODY,
                    borderwidth=0, focuscolor=PAPER)
    style.configure("TFrame", background=PAPER)
    style.configure("TLabel", background=PAPER, foreground=INK, font=F_BODY)

    # ---- 文字层级：大标题粗且大，小标细且灰
    style.configure("Title.TLabel", font=F_TITLE, foreground=INK)
    style.configure("Num.TLabel", font=F_NUM, foreground=ACCENT)
    style.configure("Section.TLabel", font=F_META, foreground=GREY3)
    style.configure("SectionZh.TLabel", font=F_BOLD, foreground=INK)
    style.configure("Meta.TLabel", font=F_META, foreground=GREY3)
    style.configure("Info.TLabel", font=F_SM, foreground=GREY3)
    style.configure("Count.TLabel", font=F_SM, foreground=GREY3)
    style.configure("Status.TLabel", font=F_SM, foreground=GREY3)

    # ---- 按钮：白底 + 1px 灰边 + 直角；hover 反白
    style.configure("TButton", background=PAPER, foreground=INK, font=F_SM,
                    bordercolor=GREY2, lightcolor=GREY2, darkcolor=GREY2,
                    relief="solid", borderwidth=1, padding=(12, 6), anchor="center")
    style.map("TButton",
              background=[("pressed", GREY2), ("active", GREY1), ("disabled", PAPER)],
              foreground=[("disabled", GREY2)],
              bordercolor=[("active", INK)],
              relief=[("pressed", "solid")])
    style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_ON,
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
    style.map("Accent.TButton",
              background=[("pressed", "#00237D"), ("active", "#00268C"),
                          ("disabled", GREY2)],
              foreground=[("disabled", PAPER)],
              bordercolor=[("active", ACCENT)])

    # ---- 输入框
    style.configure("TEntry", fieldbackground=PAPER, foreground=INK,
                    bordercolor=GREY2, lightcolor=PAPER, darkcolor=PAPER,
                    insertcolor=INK, padding=(8, 6))
    style.map("TEntry", bordercolor=[("focus", ACCENT)])

    # ---- 清单：无竖线，行高大，选中用 IKB 实心
    style.configure("Swiss.Treeview", background=PAPER, fieldbackground=PAPER,
                    foreground=INK, font=F_SM, rowheight=30,
                    bordercolor=GREY2, borderwidth=1, relief="flat")
    style.map("Swiss.Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", ACCENT_ON)])
    style.configure("Swiss.Treeview.Heading", background=GREY1, foreground=GREY3,
                    font=F_META, relief="flat", borderwidth=0, padding=(8, 7))
    style.map("Swiss.Treeview.Heading", background=[("active", GREY2)])

    # ---- 进度条：细轨 + IKB 实心
    style.configure("Swiss.Horizontal.TProgressbar", troughcolor=GREY1,
                    background=ACCENT, bordercolor=GREY1, lightcolor=ACCENT,
                    darkcolor=ACCENT, thickness=6)

    # 预览框里那条「正在解析」的进度条：轨道稍深一点，在白底画布上才看得见
    style.configure("Parse.Horizontal.TProgressbar", troughcolor="#E3E3E1",
                    background=ACCENT, bordercolor="#E3E3E1", lightcolor=ACCENT,
                    darkcolor=ACCENT, thickness=6)

    # ---- 滚动条：中性细条
    style.configure("Vertical.TScrollbar", background=GREY2, troughcolor=PAPER,
                    bordercolor=PAPER, arrowcolor=GREY3, width=10, relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", GREY3)])
    return style


def section_header(parent: tk.Misc, num: str, en: str, zh: str) -> ttk.Frame:
    """章节头：`01  SOURCE  粘贴分享链接` + 到右侧的 hairline。"""
    row = ttk.Frame(parent)
    ttk.Label(row, text=num, style="Num.TLabel").pack(side="left")
    ttk.Label(row, text=en, style="Section.TLabel").pack(side="left", padx=(10, 8))
    ttk.Label(row, text=zh, style="SectionZh.TLabel").pack(side="left")
    tk.Frame(row, height=1, background=GREY2, bd=0,
             highlightthickness=0).pack(side="left", fill="x", expand=True, padx=(12, 0))
    return row
