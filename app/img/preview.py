"""图片预览：用系统自带的 GDI+（gdiplus.dll）解码，再交给 Tk 显示。

为什么不用纯 Python 解 JPEG：完整解码慢、渐进式 JPEG 还得额外实现，而
GDI+ 是 Windows 自带组件，ctypes 调一下就能拿到像素，速度快几十倍。

对外只暴露三样东西：

- :func:`load_thumb`  —— 从字节或文件得到 (宽, 高, RGB 字节)
- :func:`to_ppm`      —— 打包成 Tk 原生支持的 P6 PPM
- :func:`PreviewError`—— 解码失败（非 Windows、格式不支持、文件损坏）
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import threading
from ctypes import wintypes

__all__ = ["PreviewError", "load_thumb", "to_ppm", "scale"]

PixelFormat32bppARGB = 0x0026200A
ImageLockModeRead = 1
MAX_SIDE = 1600  # 再大的图先让 GDI+ 缩到这个尺寸以内，避免一次性拷几百 MB


class PreviewError(RuntimeError):
    """取不到预览图。"""


class _GpRect(ctypes.Structure):
    _fields_ = [("X", ctypes.c_int), ("Y", ctypes.c_int),
                ("Width", ctypes.c_int), ("Height", ctypes.c_int)]


class _BitmapData(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_uint), ("Height", ctypes.c_uint),
                ("Stride", ctypes.c_int), ("PixelFormat", ctypes.c_int),
                ("Scan0", ctypes.c_void_p), ("Reserved", ctypes.c_size_t)]


class _StartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", ctypes.c_int),
                ("SuppressExternalCodecs", ctypes.c_int)]


_lock = threading.Lock()
_gdiplus = None
_token = None


def _lib():
    """惰性加载 gdiplus.dll（只初始化一次）。"""
    global _gdiplus, _token
    if _gdiplus is not None:
        return _gdiplus
    with _lock:
        if _gdiplus is not None:
            return _gdiplus
        if sys.platform != "win32":
            raise PreviewError("当前系统不支持 GDI+ 预览")
        try:
            lib = ctypes.WinDLL("gdiplus.dll")
        except OSError as e:  # pragma: no cover - 正常 Windows 不会走到
            raise PreviewError(f"加载 gdiplus.dll 失败：{e}") from e

        lib.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t),
                                       ctypes.POINTER(_StartupInput), ctypes.c_void_p]
        lib.GdiplusStartup.restype = ctypes.c_int
        lib.GdipCreateBitmapFromFile.argtypes = [wintypes.LPCWSTR,
                                                 ctypes.POINTER(ctypes.c_void_p)]
        lib.GdipCreateBitmapFromFile.restype = ctypes.c_int
        for name in ("GdipGetImageWidth", "GdipGetImageHeight"):
            fn = getattr(lib, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            fn.restype = ctypes.c_int
        lib.GdipGetImageThumbnail.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                              ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.c_void_p, ctypes.c_void_p]
        lib.GdipGetImageThumbnail.restype = ctypes.c_int
        lib.GdipBitmapLockBits.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GpRect),
                                           ctypes.c_uint, ctypes.c_uint,
                                           ctypes.POINTER(_BitmapData)]
        lib.GdipBitmapLockBits.restype = ctypes.c_int
        lib.GdipBitmapUnlockBits.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BitmapData)]
        lib.GdipBitmapUnlockBits.restype = ctypes.c_int
        lib.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        lib.GdipDisposeImage.restype = ctypes.c_int

        token = ctypes.c_size_t()
        startup = _StartupInput(1, None, 0, 0)
        st = lib.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None)
        if st != 0:
            raise PreviewError(f"GdiplusStartup 失败（{st}）")
        _gdiplus, _token = lib, token
        return lib


def scale(width: int, height: int, rgb: bytes | bytearray,
          max_w: int, max_h: int) -> tuple[int, int, bytearray]:
    """等比缩放（最近邻，预览够用）。"""
    if width <= 0 or height <= 0:
        return 0, 0, bytearray()
    ratio = min(max_w / width, max_h / height, 1.0)
    tw = max(1, int(width * ratio))
    th = max(1, int(height * ratio))
    if tw == width and th == height:
        return width, height, bytearray(rgb)
    xmap = [min(width - 1, int(x * width / tw)) for x in range(tw)]
    out = bytearray(tw * th * 3)
    for y in range(th):
        sy = min(height - 1, int(y * height / th)) * width
        drow = y * tw * 3
        for x in range(tw):
            si = (sy + xmap[x]) * 3
            di = drow + x * 3
            out[di] = rgb[si]
            out[di + 1] = rgb[si + 1]
            out[di + 2] = rgb[si + 2]
    return tw, th, out


def _decode_file(path: str, max_w: int, max_h: int) -> tuple[int, int, bytearray]:
    lib = _lib()
    bmp = ctypes.c_void_p()
    if lib.GdipCreateBitmapFromFile(path, ctypes.byref(bmp)) != 0:
        raise PreviewError("这张图解码失败（格式不支持或文件损坏）")

    thumb = ctypes.c_void_p()
    try:
        w, h = ctypes.c_uint(), ctypes.c_uint()
        lib.GdipGetImageWidth(bmp, ctypes.byref(w))
        lib.GdipGetImageHeight(bmp, ctypes.byref(h))
        if w.value == 0 or h.value == 0:
            raise PreviewError("图片尺寸异常")

        ratio = min(max_w / w.value, max_h / h.value, MAX_SIDE / max(w.value, h.value), 1.0)
        tw = max(1, int(w.value * ratio))
        th = max(1, int(h.value * ratio))

        if lib.GdipGetImageThumbnail(bmp, tw, th, ctypes.byref(thumb), None, None) != 0:
            raise PreviewError("生成缩略图失败")

        rw, rh = ctypes.c_uint(), ctypes.c_uint()
        lib.GdipGetImageWidth(thumb, ctypes.byref(rw))
        lib.GdipGetImageHeight(thumb, ctypes.byref(rh))
        rect = _GpRect(0, 0, rw.value, rh.value)
        data = _BitmapData()
        if lib.GdipBitmapLockBits(thumb, ctypes.byref(rect), ImageLockModeRead,
                                  PixelFormat32bppARGB, ctypes.byref(data)) != 0:
            raise PreviewError("读取像素失败")
        try:
            out = bytearray(rw.value * rh.value * 3)
            i = 0
            for y in range(rh.value):
                row = ctypes.string_at(data.Scan0 + y * data.Stride, data.Width * 4)
                for x in range(rw.value):
                    o = x * 4
                    out[i] = row[o + 2]      # R
                    out[i + 1] = row[o + 1]  # G
                    out[i + 2] = row[o]      # B
                    i += 3
        finally:
            lib.GdipBitmapUnlockBits(thumb, ctypes.byref(data))
        return rw.value, rh.value, out
    finally:
        if thumb:
            lib.GdipDisposeImage(thumb)
        lib.GdipDisposeImage(bmp)


def load_thumb(data: bytes | None = None, path: str | None = None,
               max_w: int = 320, max_h: int = 220) -> tuple[int, int, bytearray]:
    """把图片（字节或路径）解码成不超过 max_w×max_h 的 RGB 位图。"""
    if path is None and data is None:
        raise PreviewError("没有传入图片数据")
    if path is not None:
        return _decode_file(path, max_w, max_h)

    fd, tmp = tempfile.mkstemp(suffix=".img", prefix="thumb_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data or b"")
    except OSError as e:
        raise PreviewError(f"写临时文件失败：{e}") from e
    try:
        return _decode_file(tmp, max_w, max_h)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def to_ppm(width: int, height: int, rgb: bytes | bytearray) -> bytes:
    """打包成 Tk 原生支持的 P6 PPM。"""
    return b"P6\n%d %d\n255\n" % (width, height) + bytes(rgb)
