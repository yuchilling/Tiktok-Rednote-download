"""下载器：把选中的 Entry 落到用户选定的目录，带进度回调、重试与自动重命名。

零第三方依赖：http.client 流式读取 + 本地临时文件，下载完成后改名。
多个文件时并发下载（WORKERS），连接按线程复用。
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from . import http
from .parser.base import Entry, MediaItem
from .utils import guess_ext, sanitize_filename

CHUNK = 512 * 1024
MAX_RETRY = 3
WORKERS = 6          # 单个作品内并发下载的文件数：实测 6 最快，8 会被 CDN 限速反变慢
RANGE_PARTS = 6      # 单个大文件切成几段并发（抖音某些镜像单连接只有 100~200 KB/s）
RANGE_MIN = 400 * 1024     # 小于这个大小就懒得切了，开销不划算
_RANGE_MIN_PART = 256 * 1024
SLOW_KBPS = 500      # 单连接低于这个速度就改走多段并发
SAMPLE = 256 * 1024  # 挑镜像时每个候选先试拉这么多字节
STREAM_TIMEOUT = 20  # 单次读超时：卡住的连接早点放弃，换镜像/重试

_REFERER = {
    "douyin": "https://www.douyin.com/",
    "xhs": "https://www.xiaohongshu.com/",
}

_GEAR_RE = re.compile(r"^(?:视频\s*)?(.+)$")
_INDEX_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


class DownloadCancelled(RuntimeError):
    pass


class WriteBlocked(OSError):
    """本机不让我们在这个目录里写文件（受控文件夹访问 / 杀软 / 只读盘）。"""


@dataclass
class Progress:
    index: int      # 当前第几个文件（1 起）
    count: int      # 本次任务文件总数
    name: str       # 当前文件名
    done: int       # 当前文件已下载字节
    total: int      # 当前文件总字节，0 表示服务器没给 Content-Length
    speed: float    # 瞬时速度 B/s


@dataclass
class Target:
    urls: list[str]
    path: str
    kind: str = "video"


# ---------------------------------------------------------------- 目录与命名

def _unique(path: str) -> str:
    """同名文件不覆盖，改成 `名字 (1).ext` 这种。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(1, 1000):
        cand = f"{stem} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
    return f"{stem} ({int(time.time())}){ext}"


def _unique_dir(path: str) -> str:
    """同名文件夹不覆盖，但**空文件夹直接复用**——否则连点两次下载会叠出一串 (1)(2)(3)。"""
    if not os.path.exists(path):
        return path
    if os.path.isdir(path):
        try:
            if not os.listdir(path):
                return path
        except OSError:
            pass
    return _unique(path)


_BLOCKED_HINT = (
    "如果开启了 Windows「受控文件夹访问」（Windows 安全中心 → 病毒和威胁防护 → "
    "勒索软件防护），把这个程序加进允许列表；也可以换一个目录再试。"
)


def _safe_home() -> str:
    """最后一个兜底位置：用户 Downloads\素材下载器（不在受控文件夹的保护范围里）。"""
    home = os.path.expanduser("~")
    for name in ("Downloads", "下载"):
        cand = os.path.join(home, name)
        if os.path.isdir(cand):
            return os.path.join(cand, "素材下载器")
    return os.path.join(home, "素材下载器")


def _dir_chain(path: str) -> list[str]:
    """从最理想的目录一路退到「上级目录」，最后再退到用户 Downloads。"""
    parent, leaf = os.path.split(path)
    out = [path]
    if leaf:
        out.append(os.path.join(parent, f"{leaf} {time.strftime('%H%M%S')}"))
    # 上级目录兜底，但不能退到盘符根（D:\ 那个位置只会更乱）
    if parent and len(os.path.splitdrive(parent)[1].strip("\\/")) > 0:
        out.append(parent)
    spare = _safe_home()
    if os.path.normcase(spare) not in {os.path.normcase(x) for x in out}:
        out.append(spare)
    return out


def _probe(directory: str) -> bool:
    """真的往里写一个临时文件——只看 os.access 会被「目录能进、文件不能建」骗过去。

    注意：**只要写进去了就算可写**。删不掉不算失败——杀软（Defender 实时防护、
    火绒、360）经常在文件刚写完的那一瞬间锁住它，这时候 os.remove 会抛
    `PermissionError`，但它说明的是「文件被扫描」，不是「目录不可写」。
    """
    probe = os.path.join(directory, f".probe-{os.getpid()}-{int(time.time() * 1000)}.tmp")
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
    except OSError:
        return False
    for _ in range(3):
        try:
            os.remove(probe)
            break
        except OSError:
            time.sleep(0.05)      # 让杀软松手；实在删不掉也留着，不影响判定
    return True


def _pick_dir(path: str, start: int = 0) -> str:
    """从 path 这条退让链的第 start 个开始，挑一个「能建目录 + 真能写文件」的。"""
    chain = _dir_chain(path)
    last: OSError | None = None
    for cand in chain[start:]:
        try:
            os.makedirs(cand, exist_ok=True)
        except OSError as e:
            last = e
            continue
        if os.path.isdir(cand) and os.access(cand, os.W_OK) and _probe(cand):
            return cand
        last = PermissionError(f"目录不可写：{cand}")
    if last is not None:
        raise last
    raise PermissionError(f"目录不可写：{path}")


def _writable_dir(path: str) -> str:
    """挑一个真能写的目录。

    目标目录建不出来、或者建得出来但写不进文件（`[WinError 5] 拒绝访问` /
    `[Errno 13] Permission denied`：受控文件夹访问、杀软、只读盘、公司策略……）
    时依次退：加时间戳 → 直接平铺到用户选的目录。
    """
    return _pick_dir(path)


def _base_name(item: MediaItem) -> str:
    # 抖音标题常是一大段文案，取第一行、短一点更适合当文件名
    title = (item.title or "").splitlines()[0] if item.title else ""
    title = sanitize_filename(title, 40)
    if not title or title == "untitled":
        title = f"{item.platform}_{item.item_id}"
    return title


def _gear_suffix(entry: Entry) -> str:
    """把「视频 1080p」这种标签压成后缀 `_1080p`。"""
    label = (entry.label or "").strip()
    m = _GEAR_RE.match(label)
    if m:
        label = m.group(1).strip()
    if not label or label in ("（默认）", "(默认)", "视频（默认）", "无水印",
                              "（无水印）", "(无水印)"):
        return ""
    label = sanitize_filename(label, 20).replace(" ", "")
    return f"_{label}" if label else ""


def _label_index(label: str) -> int:
    """从「图片 2/3」「实况 2/3」这类标签里取出序号，取不到返回 0。"""
    m = _INDEX_RE.search(label or "")
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def _dedupe(name: str, used: set[str]) -> str:
    """同一个文件夹里不重名：重名就加 (2)、(3)…，避免后者把前者覆盖掉。"""
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        cand = f"{stem} ({i}){ext}"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


def plan(item: MediaItem, entries: list[Entry], out_dir: str) -> list[Target]:
    """算出「哪个直链写到哪个文件」。

    - 只选了一个视频/音频 → 直接放在 out_dir 下，用作品标题命名
    - 选了多张图/实况/多个视频 → 在 out_dir 下建一个同名文件夹，按序号排
    """
    picked = [e for e in entries if e.urls]
    if not picked:
        return []

    base = _base_name(item)
    simple = (len(picked) == 1 and picked[0].kind in ("video", "audio"))

    if simple:
        e = picked[0]
        ext = guess_ext(e.urls[0], e.kind)
        path = _unique(os.path.join(out_dir, f"{base}{_gear_suffix(e)}{ext}"))
        return [Target(urls=list(e.urls), path=path, kind=e.kind)]

    folder = os.path.join(out_dir, base)
    plans: list[tuple[str, Entry]] = []
    videos = [e for e in picked if e.kind == "video"]
    used: set[str] = set()
    img_no = 0      # 最近一张图的序号
    live_no = 0     # 没有同序号图片时，实况自己的兜底序号
    for e in picked:
        if e.kind == "video":
            suffix = _gear_suffix(e) if len(videos) > 1 else ""
            name = f"video{suffix}{guess_ext(e.urls[0], 'video')}"
        elif e.kind == "audio":
            name = f"audio{guess_ext(e.urls[0], 'audio')}"
        elif e.kind == "live":
            # 跟同一张图共用序号：01.jpg + 01_live.mp4
            idx = _label_index(e.label)
            if not idx:
                if img_no:
                    idx = img_no
                else:
                    live_no += 1
                    idx = live_no
            name = f"{idx:02d}_live.mp4"
        else:
            img_no = _label_index(e.label) or (img_no + 1)
            name = f"{img_no:02d}{guess_ext(e.urls[0], 'image')}"
        plans.append((_dedupe(name, used), e))

    # 同一个作品重下一次：文件夹里正好就是我们这几个文件名 → 原地重下（覆盖），
    # 别再造 `标题 (5)` 这种新目录
    if not (os.path.isdir(folder) and _same_names(folder, [n for n, _ in plans])):
        folder = _unique_dir(folder)

    return [Target(urls=list(e.urls), path=os.path.join(folder, name), kind=e.kind)
            for name, e in plans]


def _same_names(folder: str, names: list[str]) -> bool:
    """文件夹里现有的文件，正好就是我们要写的这几个（不含别的）？"""
    try:
        existing = set(os.listdir(folder))
    except OSError:
        return False
    return existing == set(names)


# ---------------------------------------------------------------- 字节嗅探

def _sniff_ext(path: str) -> str:
    """按文件头纠正扩展名（CDN 常给 .jpg 但实际是 webp / heic）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return ""
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:3] == b"ID3":
        return ".mp3"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return ".heic"
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand in (b"isom", b"iso2", b"mp41", b"mp42", b"dash"):
            return ".mp4"
        return ".m4a"
    return ""


def _fix_ext(path: str, kind: str = "video") -> str:
    real = _sniff_ext(path)
    if real and not path.lower().endswith(real):
        stem = os.path.splitext(path)[0]
        new_path = _unique(stem + real)
        os.replace(path, new_path)
        return new_path
    return path


# ---------------------------------------------------------------- 单个文件

def _open_wb(path: str):
    """以写模式打开临时文件；如果它上次留成只读（或者是个同名目录），先清掉再开。"""
    try:
        return open(path, "wb")
    except (PermissionError, IsADirectoryError) as e:
        try:
            if os.path.isdir(path):
                os.rmdir(path)
            else:
                os.chmod(path, 0o666)
                os.remove(path)
            return open(path, "wb")
        except OSError:
            raise e



def _range_total(url: str, referer: str) -> int:
    """问服务器支持不支持分段：支持就返回总字节数，否则 0。"""
    headers = {"Referer": http.ascii_header(referer or ""), "Range": "bytes=0-0"}
    conn, resp, _final = http.open_stream(url, headers=headers, timeout=30)
    try:
        if resp.status != 206:
            return 0
        m = re.search(r"/(\d+)\s*$", resp.getheader("Content-Range") or "")
        total = int(m.group(1)) if m else 0
        try:
            resp.read()
        except Exception:  # noqa: BLE001
            pass
        return total if total >= RANGE_MIN else 0
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def _stream_ranged(url: str, path: str, referer: str, total: int, on_progress, cancel) -> tuple[str, int]:
    """把一个文件切成 N 段并发拉——单连接被限速时这是唯一有效的提速办法。"""
    parts = max(2, min(RANGE_PARTS, total // _RANGE_MIN_PART))
    step = total // parts
    tmp = path + ".part"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with _open_wb(tmp) as f:
            f.truncate(total)
    except OSError as e:
        raise WriteBlocked(f"{tmp}（{e}）") from e

    lock = threading.Lock()
    state = {"got": 0}
    start = time.time()
    stop = threading.Event()
    errors: list[Exception] = []

    def part(i: int) -> None:
        first = i * step
        last = total - 1 if i == parts - 1 else (i + 1) * step - 1
        pos = first
        try:
            for attempt in range(MAX_RETRY):
                if stop.is_set() or (cancel is not None and cancel.is_set()):
                    return
                try:
                    headers = {"Referer": http.ascii_header(referer or ""),
                               "Range": f"bytes={pos}-{last}"}
                    _conn, resp, _final = http.open_stream(url, headers=headers, timeout=STREAM_TIMEOUT)
                    try:
                        if resp.status not in (200, 206):
                            raise RuntimeError(f"HTTP {resp.status}")
                        with open(tmp, "r+b") as fh:
                            fh.seek(pos)
                            while pos <= last:
                                if stop.is_set() or (cancel is not None and cancel.is_set()):
                                    return
                                chunk = resp.read(min(CHUNK, last - pos + 1))
                                if not chunk:
                                    break
                                fh.write(chunk)
                                pos += len(chunk)
                                with lock:
                                    state["got"] += len(chunk)
                                    if on_progress:
                                        got = state["got"]
                                        on_progress(got, total,
                                                    got / max(time.time() - start, 0.001))
                    finally:
                        try:
                            resp.close()
                        except Exception:  # noqa: BLE001
                            pass
                    if pos > last:
                        return
                except (DownloadCancelled, WriteBlocked):
                    raise
                except Exception as e:  # noqa: BLE001
                    if attempt == MAX_RETRY - 1:
                        with lock:
                            errors.append(e)
                        stop.set()
                        return
                    time.sleep(0.5 * (attempt + 1))
        except (DownloadCancelled, WriteBlocked) as e:
            with lock:
                errors.append(e)
            stop.set()

    threads = [threading.Thread(target=part, args=(i,), daemon=True) for i in range(parts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if cancel is not None and cancel.is_set():
        _discard(tmp)
        raise DownloadCancelled()
    if errors:
        _discard(tmp)
        raise errors[0].__class__(*errors[0].args) if not isinstance(errors[0], WriteBlocked) else errors[0]
    if state["got"] < total:
        _discard(tmp)
        raise RuntimeError(f"分段下载没收全（{state['got']}/{total}）")
    os.replace(tmp, path)
    return _fix_ext(path), state["got"]


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _speed_probe(url: str, referer: str, size: int = SAMPLE) -> tuple[float, float]:
    """试拉一小段，返回（字节/秒, 耗时）。"""
    headers = {"Referer": http.ascii_header(referer or ""), "Range": f"bytes=0-{size - 1}"}
    t0 = time.time()
    _conn, resp, _final = http.open_stream(url, headers=headers, timeout=12)
    n = 0
    try:
        while n < size:
            chunk = resp.read(size - n)
            if not chunk:
                break
            n += len(chunk)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    dt = max(time.time() - t0, 0.001)
    return n / dt, dt


def _best_url(urls: list[str], referer: str) -> list[str]:
    """多个候选地址时，先并发试拉一小段，快的排前面。

    抖音同一个作品常给两三个镜像，实测有的 800 KB/s、有的 120 KB/s，
    闷头用第一个就是用户看到「只有一百来 KB」的原因。
    """
    if len(urls) < 2:
        return list(urls)
    scored: dict[str, float] = {}
    pool = ThreadPoolExecutor(max_workers=min(3, len(urls)))
    try:
        futs = {pool.submit(_speed_probe, u, referer): u for u in urls[:3]}
        for fut, u in list(futs.items()):
            try:
                scored[u] = fut.result(timeout=8)[0]
            except Exception:  # noqa: BLE001 - 试不出来就按原顺序
                pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not scored:
        return list(urls)
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    best = [u for u, _ in ranked]
    rest = [u for u in urls if u not in scored]
    return best + rest


def _stream(url: str, path: str, referer: str, on_progress, cancel,
            allow_swap: bool = True) -> tuple[str, int]:
    """流式下载一个地址。

    - 走 `http.open_stream`：连接按线程复用，省掉每次握手
    - 第一块下完发现速度不到 `SLOW_KBPS`，且服务器支持分段 → 立刻切成多段并发重下
      （抖音有些镜像单连接只给 100~200 KB/s，分段能翻好几倍）
    """
    tmp = path + ".part"
    got = 0
    start = time.time()
    headers = {"Referer": http.ascii_header(referer or "")}
    _conn, resp, final = http.open_stream(url, headers=headers, timeout=STREAM_TIMEOUT)
    try:
        total = int(resp.getheader("Content-Length") or 0)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            f = _open_wb(tmp)
        except OSError as e:
            http.drop_conn(url)
            raise WriteBlocked(f"{tmp}（{e}）") from e
        swap = False
        with f:
            while True:
                if cancel is not None and cancel.is_set():
                    http.drop_conn(url)
                    raise DownloadCancelled()
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                try:
                    f.write(chunk)
                except OSError as e:
                    http.drop_conn(url)
                    raise WriteBlocked(f"{tmp}（{e}）") from e
                got += len(chunk)
                elapsed = max(time.time() - start, 0.001)
                if on_progress:
                    on_progress(got, total, got / elapsed)
                slow = got / elapsed < SLOW_KBPS * 1024
                if (allow_swap and slow and len(chunk) >= CHUNK and got < total
                        and total >= RANGE_MIN):
                    swap = True
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass
                    http.drop_conn(url)
                    break
        if swap:
            real = _range_total(final or url, referer)
            if real:
                # 注意：必须等上面的 with 把 .part 关掉，否则 Windows 上 os.replace 会失败
                return _stream_ranged(final or url, path, referer, real,
                                      on_progress, cancel)
            _discard(tmp)
            return _stream(final or url, path, referer, on_progress, cancel,
                           allow_swap=False)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    if got == 0:
        raise RuntimeError("服务器返回了空文件")
    os.replace(tmp, path)
    return _fix_ext(path), got


def _fetch(target: Target, referer: str, on_progress, cancel) -> tuple[str, int]:
    last: Exception | None = None
    ordered = _best_url(target.urls, referer)
    for attempt in range(MAX_RETRY):
        for url in ordered:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            try:
                return _stream(url, target.path, referer, on_progress, cancel)
            except (DownloadCancelled, WriteBlocked):
                raise
            except Exception as e:  # noqa: BLE001 - 换下一个候选地址
                last = e
        if attempt < MAX_RETRY - 1:
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"下载失败：{os.path.basename(target.path)}（{last}）")


# ---------------------------------------------------------------- 主入口

def _rebase(targets: list[Target], new_dir: str) -> list[Target]:
    """目录被换掉之后，把每个目标文件挪到新目录下（文件名不变）。"""
    return [Target(urls=list(t.urls),
                   path=os.path.join(new_dir, os.path.basename(t.path)),
                   kind=t.kind) for t in targets]


def _run_batch(targets: list[Target], indexes: list[int], referer: str, count: int,
               on_status, on_progress, cancel) -> tuple[dict[int, tuple[str, int]], list[int], dict[int, Exception]]:
    """并发下载一批文件，返回（成功的、写不进目录的、其它失败的）。"""
    done: dict[int, tuple[str, int]] = {}
    blocked: list[int] = []
    failed: dict[int, Exception] = {}
    workers = max(1, min(WORKERS, len(indexes)))

    def work(i: int):
        t = targets[i]
        name = os.path.basename(t.path)

        def cb(got: int, total: int, speed: float):
            if on_progress:
                on_progress(Progress(index=i + 1, count=count, name=name,
                                     done=got, total=total, speed=speed))

        return _fetch(t, referer, cb, cancel)

    if workers == 1:
        for i in indexes:
            try:
                done[i] = work(i)
            except WriteBlocked:
                blocked.append(i)
            except DownloadCancelled:
                raise
            except Exception as e:      # noqa: BLE001
                failed[i] = e
            else:
                if on_status:
                    on_status(f"已完成 {len(done)}/{count}：{os.path.basename(targets[i].path)}")
        return done, blocked, failed

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(work, i): i for i in indexes}
        for fut in as_completed(futures):
            i = futures[fut]
            if cancel is not None and cancel.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                raise DownloadCancelled()
            try:
                done[i] = fut.result()
            except WriteBlocked:
                blocked.append(i)
            except DownloadCancelled:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception as e:      # noqa: BLE001
                failed[i] = e
            else:
                if on_status:
                    on_status(f"已完成 {len(done)}/{count}：{os.path.basename(targets[i].path)}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return done, blocked, failed


def download(item: MediaItem, entries: list[Entry], out_dir: str,
             on_status: Callable[[str], None] | None = None,
             on_progress: Callable[[Progress], None] | None = None,
             cancel=None) -> list[str]:
    """把选中的条目下载到 out_dir，返回实际写出的文件路径列表。"""
    if not out_dir:
        raise ValueError("没有指定保存目录")
    if not entries:
        raise ValueError("没有勾选任何内容")

    targets = plan(item, entries, out_dir)
    if not targets:
        raise RuntimeError("这些条目没有可下载的地址")

    # 目录可能建不出来，也可能建得出来但写不进文件（受控文件夹访问 / 杀软 / 只读盘）
    want = os.path.dirname(targets[0].path) or out_dir
    try:
        real = _writable_dir(want)
    except OSError as e:
        tried = " / ".join(_dir_chain(want))
        raise RuntimeError(
            f"没法在「{out_dir}」下写文件（试过：{tried}）：{e}。{_BLOCKED_HINT}"
        ) from e
    if os.path.normcase(real) != os.path.normcase(want):
        targets = _rebase(targets, real)
        if on_status:
            on_status(f"原目录创建失败，改存到：{real}")

    for t in targets:
        os.makedirs(os.path.dirname(t.path) or ".", exist_ok=True)

    referer = _REFERER.get(item.platform, "https://www.douyin.com/")
    count = len(targets)
    saved: dict[int, str] = {}
    pending = list(range(count))
    hop = 0
    chain = _dir_chain(want)

    while pending:
        workers = max(1, min(WORKERS, len(pending)))
        if on_status:
            if len(pending) == count and workers > 1:
                on_status(f"开始下载 {count} 个文件（并发 {workers}）")
            elif len(saved) == 0:
                on_status(f"开始下载 {len(pending)} 个文件（并发 {workers}）")

        done, blocked, failed = _run_batch(targets, pending, referer, count,
                                           on_status, on_progress, cancel)
        for i, (path, _size) in done.items():
            saved[i] = path
        pending = [i for i in pending if i not in done and i not in blocked]

        if blocked:
            hop += 1
            if hop >= len(chain):
                raise RuntimeError(
                    f"文件写不进去：已经试过 {' / '.join(chain)}。{_BLOCKED_HINT}"
                )
            try:
                real = _pick_dir(want, start=hop)
            except OSError as e:
                raise RuntimeError(f"文件写不进去：{e}。{_BLOCKED_HINT}") from e
            targets = _rebase(targets, real)
            for t in targets:
                os.makedirs(os.path.dirname(t.path) or ".", exist_ok=True)
            if on_status:
                on_status(f"「{os.path.dirname(targets[0].path)}」写不进去，改存到：{real}")
            pending = sorted(set(pending) | set(blocked))
            continue

        if failed:
            first = next(iter(failed.values()))
            raise RuntimeError(str(first))

    if on_status:
        on_status(f"完成：{count} 个文件")
    return [saved[i] for i in sorted(saved)]
