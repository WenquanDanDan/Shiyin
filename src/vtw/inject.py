"""把识别文本注入到当前焦点窗口。

两种方式
--------
``clipboard``（默认）
    快照剪贴板 → 写入识别文本 → 发 Ctrl+V → 延时 → 恢复原剪贴板。
    对中文/长文本最快最稳，绝大多数输入框都支持。缺点是会短暂占用剪贴板，
    且原剪贴板的非文本内容（图片/文件）无法恢复。

``unicode``
    用 ``SendInput`` + ``KEYEVENTF_UNICODE`` 逐字注入，完全不碰剪贴板。
    适合剪贴板被监控/占用、或目标程序不支持粘贴的场景；字符多时较慢。

**关键竞态**：热键是 ``Ctrl+Alt+Space``，用户松开 Space 时 Ctrl/Alt 往往还按着。
此时若直接发 Ctrl+V，会变成 ``Ctrl+Alt+V``（在很多程序里是别的命令）。
所以注入前必须等所有修饰键抬起 —— 见 :func:`wait_modifiers_released`。
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

from . import winclip

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

# --- 虚拟键码 ---
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56

_MODIFIER_VKS = (
    VK_SHIFT,
    VK_CONTROL,
    VK_MENU,
    0xA0,  # VK_LSHIFT
    0xA1,  # VK_RSHIFT
    0xA2,  # VK_LCONTROL
    0xA3,  # VK_RCONTROL
    0xA4,  # VK_LMENU
    0xA5,  # VK_RMENU
    VK_LWIN,
    VK_RWIN,
)

# --- SendInput 结构 ---
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

_INPUT_SIZE = ctypes.sizeof(_INPUT)
_MAPVK_VK_TO_VSC = 0

#: 需要 KEYEVENTF_EXTENDEDKEY 的"扩展键"（小键盘/右侧修饰键等）
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D}
_scan_cache: dict[int, int] = {}


def _scan_code(vk: int) -> int:
    """取虚拟键对应的硬件扫描码。

    **必须同时设置 wScan**：只给 wVk 而把 wScan 留 0 时，那些依赖扫描码做
    键盘布局翻译的程序（Tk/Tcl、部分 Electron/Java 应用）解析不出按键，
    表现为"Ctrl+V 发出去了但什么都没粘贴"。实测 Tk 输入框下 keysym 会变成
    ``??``，粘贴绑定不触发；补上扫描码后立即正常。
    """
    cached = _scan_cache.get(vk)
    if cached is None:
        cached = user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
        _scan_cache[vk] = cached
    return cached


def _key_event(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    event = _INPUT()
    event.type = INPUT_KEYBOARD
    event.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return event


def _vk_event(vk: int, *, keyup: bool = False) -> _INPUT:
    """构造一个"普通按键"事件（同时填 wVk 与 wScan）。"""
    flags = KEYEVENTF_KEYUP if keyup else 0
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    return _key_event(vk=vk, scan=_scan_code(vk), flags=flags)


def _send(events: list[_INPUT]) -> bool:
    if not events:
        return True
    array = (_INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, _INPUT_SIZE)
    if sent != len(events):
        err = ctypes.get_last_error()
        log.error("SendInput 只发送了 %d/%d 个事件（错误码 %s）", sent, len(events), err)
        return False
    return True


def modifier_is_down() -> bool:
    """当前是否有任何修饰键处于按下状态。"""
    for vk in _MODIFIER_VKS:
        if user32.GetAsyncKeyState(vk) & 0x8000:
            return True
    return False


def wait_modifiers_released(timeout: float = 2.0, poll: float = 0.01) -> bool:
    """等待所有修饰键抬起。

    没有这一步，用户松开 Space 时仍按着的 Ctrl/Alt 会把注入的 ``Ctrl+V``
    变成 ``Ctrl+Alt+V``。返回是否在超时前等到。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not modifier_is_down():
            return True
        time.sleep(poll)
    log.warning("等待修饰键抬起超时（%.1fs），仍继续注入", timeout)
    return False


def send_ctrl_v() -> bool:
    """发送一次 Ctrl+V（带扫描码，确保各程序都能正确解析）。"""
    events = [
        _vk_event(VK_CONTROL),
        _vk_event(VK_V),
        _vk_event(VK_V, keyup=True),
        _vk_event(VK_CONTROL, keyup=True),
    ]
    return _send(events)


def type_unicode(text: str, *, char_delay: float = 0.0) -> bool:
    """用 KEYEVENTF_UNICODE 逐字符注入（不经过剪贴板）。

    按 UTF-16 码元发送，因此 BMP 之外的字符（emoji）也能正确输入。
    """
    if not text:
        return True

    # 以 UTF-16 码元为单位切分（BMP 之外的字符也能正确输入）
    encoded = text.encode("utf-16-le")
    units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]

    ok = True
    batch: list[_INPUT] = []
    for unit in units:
        batch.append(_key_event(scan=unit, flags=KEYEVENTF_UNICODE))
        batch.append(_key_event(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if len(batch) >= 64:  # 分批发送，避免数组过大
            ok = _send(batch) and ok
            batch = []
            if char_delay:
                time.sleep(char_delay)
    ok = _send(batch) and ok
    return ok


def inject_text(
    text: str,
    *,
    method: str = "clipboard",
    restore_clipboard: bool = True,
    restore_delay_ms: int = 400,
    wait_modifiers_timeout: float = 2.0,
) -> bool:
    """把文本注入当前焦点窗口。成功返回 ``True``。

    :param method: ``"clipboard"`` 或 ``"unicode"``。
    :param restore_clipboard: 剪贴板方式下，注入后是否恢复原文本内容。
    :param restore_delay_ms: 注入后等待多久再恢复剪贴板。
    """
    if not text:
        return True

    # 必须先等修饰键抬起，否则 Ctrl+V 会带上用户还没松开的 Ctrl/Alt
    wait_modifiers_released(wait_modifiers_timeout)

    if method == "unicode":
        return type_unicode(text)

    # ---- 剪贴板方案 ----
    snapshot = winclip.snapshot()
    if snapshot.has_unrestorable_content:
        log.warning(
            "剪贴板含 %d 个非文本格式（图片/文件等），注入后无法恢复这类内容",
            snapshot.other_formats,
        )

    if not winclip.set_text(text):
        log.error("写入剪贴板失败，回退到 Unicode 直接注入")
        return type_unicode(text)

    # 给剪贴板一点时间真正生效，再触发粘贴
    time.sleep(0.03)
    if not send_ctrl_v():
        log.error("发送 Ctrl+V 失败")
        return False

    if restore_clipboard and snapshot.had_text:
        time.sleep(max(0.05, restore_delay_ms / 1000.0))
        if not winclip.set_text(snapshot.text or ""):
            log.warning("恢复原剪贴板内容失败")
    return True
