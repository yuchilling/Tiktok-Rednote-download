"""程序入口：python main.py（或双击 run.bat）。

附带的命令行开关（给排查用，普通用户不需要）：

    python main.py --parse <链接>     不开界面，直接把解析结果打印出来
    python main.py --diagnose [目录]  检查保存目录能不能写（建文件夹/写文件/改名）
    python main.py --log              打印日志文件路径（出错后把它发回来）

打包成 exe 之后没有控制台，这三个开关会把结果写成 `%APPDATA%\\VideoSaver\\report.txt`，
并用一个消息框把内容显示出来。
"""

from __future__ import annotations

import os
import sys


def _enable_dpi_awareness() -> None:
    """Windows 高分屏下让界面不糊。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 老系统没有 shcore
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 输出通道

def _emit(lines: list[str], title: str = "素材下载器") -> None:
    """把排查结果同时送到：控制台（有就送）、report.txt、消息框。"""
    text = "\n".join(lines)

    out = sys.stdout
    if out is not None:
        try:
            out.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 老环境没有 reconfigure
            pass
        try:
            print(text)
            out.flush()
        except Exception:  # noqa: BLE001 - 打包后没有控制台
            pass

    path = _write_report(text)

    if out is None or getattr(sys, "frozen", False):
        msg = text if not path else f"{text}\n\n（已保存到：{path}）"
        _msgbox(title, msg)


def _write_report(text: str) -> str:
    try:
        from app import store

        store.ensure_dir()
        path = store.APP_DIR / "report.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - 写不了就算了
        return ""


def _msgbox(title: str, text: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 子命令

def _cmd_parse(text: str) -> int:
    """命令行：只跑解析，打印成不成功的细节（排查用）。"""
    from app import logbook, store
    from app.parser import parse
    from app.parser.base import ParseError

    cfg = store.load_config()
    try:
        item = parse(
            text,
            cookie=str(cfg.get("douyin_cookie") or ""),
            third_party=str(cfg.get("third_party") or store.DEFAULT_THIRD_PARTY),
        )
    except ParseError as e:
        logbook.log(f"CLI 解析失败：{e}")
        _emit([f"解析失败：{e}"], "解析失败")
        return 1
    except OSError as e:
        logbook.log(f"CLI 网络错误：{e}")
        _emit([f"网络错误：{e}"], "解析失败")
        return 1

    lines = [
        f"平台：{item.platform}",
        f"标题：{item.title or '（无）'}",
        f"作者：{item.author or '（无）'}",
        f"条目：{len(item.entries)} 项",
    ]
    for e in item.entries:
        lines.append(f"  - {e.label}  [{e.kind}]  {len(e.urls)} 个候选地址")
    if item.note:
        lines.append(f"备注：{item.note}")
    _emit(lines, "解析结果")
    return 0


def _cmd_diagnose(target: str) -> int:
    """命令行：诊断保存目录能不能写（对着报错目录跑，一眼看出卡在哪一步）。"""
    from app import store

    path = (target or store.load_config().get("last_dir") or "").strip()
    if not path:
        path = os.path.join(os.path.expanduser("~"), "Downloads")
    path = os.path.abspath(path)

    lines = [f"目标目录：{path}"]
    lines.append(f"  存在      ：{os.path.exists(path)}")
    lines.append(f"  是文件夹  ：{os.path.isdir(path)}")
    lines.append(f"  是文件    ：{os.path.isfile(path)}")
    if not os.path.exists(path):
        parent = os.path.dirname(path)
        lines.append(f"  上级目录  ：{parent}（存在={os.path.exists(parent)}）")

    box: dict[str, str] = {}

    def mkdir_step() -> None:
        sub = os.path.join(path, "写入测试")
        os.makedirs(sub, exist_ok=True)
        box["dir"] = sub

    steps = [
        ("建子文件夹", mkdir_step),
        ("写文件", lambda: _write_probe(os.path.join(box["dir"], "a.txt"))),
        ("改名/替换", lambda: os.replace(os.path.join(box["dir"], "a.txt"),
                                         os.path.join(box["dir"], "b.txt"))),
        ("清理", lambda: _rm_probe(box["dir"])),
    ]

    failed = 0
    for name, fn in steps:
        try:
            fn()
            lines.append(f"  [OK]   {name}")
        except OSError as e:
            failed += 1
            lines.append(f"  [FAIL] {name}：{type(e).__name__}: {e}")
            if getattr(e, "winerror", None) == 5:
                lines += [
                    "      → WinError 5「拒绝访问」通常是：",
                    "        1) Windows 安全中心 → 病毒和威胁防护 → 勒索软件防护"
                    "（受控文件夹访问）拦了程序，把它加进允许列表；",
                    "        2) 杀软/公司策略禁止在该盘或该目录写；",
                    "        3) 目录挂在只读介质或网络盘上。",
                ]
            break

    if failed:
        lines.append("")
        lines.append("结论：这个目录写不了，换一个目录（例如 D:\\下载）再试。")
    else:
        lines.append("")
        lines.append("结论：目录可写，下载应该能正常工作。")

    _emit(lines, "目录自检")
    return 1 if failed else 0


def _write_probe(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("probe")


def _rm_probe(folder: str) -> None:
    for name in ("a.txt", "b.txt"):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            os.remove(p)
    try:
        os.rmdir(folder)
    except OSError:
        pass


def _cmd_log() -> int:
    from app import logbook

    p = logbook.log_path()
    lines = [f"日志文件：{p}"]
    if p.exists():
        lines.append(f"大小：{p.stat().st_size} 字节")
        raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append("—— 最后 20 行 ——")
        lines += raw[-20:]
    else:
        lines.append("还没有日志（还没出过错）。")
    _emit(lines, "运行日志")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--log" in args:
        return _cmd_log()
    if "--diagnose" in args:
        idx = args.index("--diagnose")
        rest = " ".join(args[idx + 1:]).strip()
        return _cmd_diagnose(rest)
    if "--parse" in args:
        idx = args.index("--parse")
        rest = " ".join(args[idx + 1:]).strip()
        if not rest:
            _emit(["用法：素材下载器.exe --parse <分享链接或文案>"], "用法")
            return 2
        return _cmd_parse(rest)

    _enable_dpi_awareness()
    try:
        from app.ui.main_window import App
    except ImportError as e:
        _emit([f"导入失败：{e}", "请确认在项目根目录（含 app 文件夹）下运行。"], "启动失败")
        return 1
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
