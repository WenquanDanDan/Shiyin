"""剪贴板读写（纯 ctypes，不引入 pywin32）。

只处理 ``CF_UNICODETEXT``：这是语音注入唯一需要的格式。
非文本内容（图片/文件）无法恢复，:func:`snapshot` 会如实报告，由调用方决定是否
仍然使用剪贴板方案 —— 见 :mod:`vtw.inject` 的说明。
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_HDROP = 15

GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.CountClipboardFormats.argtypes = []
user32.CountClipboardFormats.restype = ctypes.c_int
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


class ClipboardSnapshot:
    """剪贴板快照。

    :param had_text: 原本是否含文本。
    :param text: 原本的文本内容（``had_text`` 为真时有效）。
    :param other_formats: 除文本外的其它格式数量；>0 表示有无法恢复的内容。
    """

    __slots__ = ("had_text", "text", "other_formats")

    def __init__(self, had_text: bool, text: str | None, other_formats: int) -> None:
        self.had_text = had_text
        self.text = text
        self.other_formats = other_formats

    @property
    def has_unrestorable_content(self) -> bool:
        """是否含有非文本内容（图片、文件等），恢复时会被丢弃。"""
        return self.other_formats > 0

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ClipboardSnapshot(had_text={self.had_text}, "
            f"other_formats={self.other_formats}, len={len(self.text or '')})"
        )


def _open_clipboard(retries: int = 12, delay: float = 0.02) -> bool:
    """打开剪贴板。别的进程占用时会失败，需要重试。"""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def _list_formats() -> list[int]:
    formats: list[int] = []
    fmt = 0
    while True:
        fmt = user32.EnumClipboardFormats(fmt)
        if fmt == 0:
            break
        formats.append(int(fmt))
        if len(formats) > 512:  # 防御异常实现
            break
    return formats


def get_text() -> str | None:
    """读取剪贴板文本；无文本时返回 ``None``。"""
    if not _open_clipboard():
        log.debug("打开剪贴板失败（读取）")
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_text(text: str) -> bool:
    """写入剪贴板文本。成功返回 ``True``。"""
    if text is None:
        text = ""
    if not _open_clipboard():
        log.warning("打开剪贴板失败（写入）")
        return False

    handle = None
    try:
        if not user32.EmptyClipboard():
            log.warning("清空剪贴板失败")
            return False

        # 末尾必须有 NUL；wstring 的 2 字节/字符
        data = text.encode("utf-16-le") + b"\x00\x00"
        size = len(data)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            log.warning("GlobalAlloc 失败（%d 字节）", size)
            return False

        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(pointer, data, size)
        finally:
            kernel32.GlobalUnlock(handle)

        # 交出所有权后不能再 GlobalFree
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            log.warning("SetClipboardData 失败")
            return False
        handle = None
        return True
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def snapshot() -> ClipboardSnapshot:
    """抓取剪贴板状态，供注入后恢复。"""
    if not _open_clipboard():
        log.debug("打开剪贴板失败（快照），将按无文本处理")
        return ClipboardSnapshot(False, None, 0)
    try:
        formats = _list_formats()
        text: str | None = None
        if CF_UNICODETEXT in formats:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if handle:
                pointer = kernel32.GlobalLock(handle)
                if pointer:
                    try:
                        text = ctypes.wstring_at(pointer)
                    finally:
                        kernel32.GlobalUnlock(handle)
        others = sum(1 for f in formats if f != CF_UNICODETEXT)
        return ClipboardSnapshot(text is not None, text, others)
    finally:
        user32.CloseClipboard()


def clear() -> bool:
    if not _open_clipboard():
        return False
    try:
        return bool(user32.EmptyClipboard())
    finally:
        user32.CloseClipboard()
