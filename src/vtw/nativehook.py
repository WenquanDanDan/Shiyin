"""原生 ``WH_KEYBOARD_LL`` 全局键盘钩子。

为什么不用 ``keyboard`` 库
--------------------------
``keyboard`` 最后一次发版是 2020 年，其实现在本机实测**不稳定**：
``add_hotkey(..., suppress=True)`` 第一次触发失败、第二次才成功，
而且抑制与否依赖库内部一套脆弱的按键状态机。而"按住热键说话"是本品最核心的
交互，必须行为确定。

本模块直接用 ``SetWindowsHookExW(WH_KEYBOARD_LL, ...)``：

* **抑制可控**：只吞掉组合键里的主键（如 Space），修饰键照常放行 ——
  这样用户按住 Ctrl+Alt 时，目标程序不会被塞进任何字符，其它快捷键也不受影响。
* **按住/松开语义精确**：按下触发一次回调，长按产生的重复 WM_KEYDOWN 会被吞掉
  且不会重复触发；松开时再触发一次。
* **零第三方依赖**，不再担心上游失修。

线程模型
--------
Windows 要求低级钩子的回调在**安装钩子的那个线程**上被调用，且该线程必须
跑消息循环。因此这里单独起一个线程：安装钩子 → ``GetMessageW`` 循环 → 退出时
卸载钩子。回调里只做"判断 + 调用户回调"，绝不阻塞。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10

HC_ACTION = 0


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL

# --- 虚拟键码表 -----------------------------------------------------------
VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN = 0x11, 0x12, 0x10, 0x5B, 0x5C
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LMENU, VK_RMENU = 0xA4, 0xA5
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1

_NAMED_KEYS: dict[str, int] = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "capslock": 0x14, "pause": 0x13,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "printscreen": 0x2C, "scrolllock": 0x91, "numlock": 0x90,
}

#: 修饰键组 → 组内任意一个物理键按下即视为该修饰键按下
_MODIFIER_GROUPS: dict[str, tuple[int, ...]] = {
    "ctrl": (VK_CONTROL, VK_LCONTROL, VK_RCONTROL),
    "alt": (VK_MENU, VK_LMENU, VK_RMENU),
    "shift": (VK_SHIFT, VK_LSHIFT, VK_RSHIFT),
    "win": (VK_LWIN, VK_RWIN),
    "windows": (VK_LWIN, VK_RWIN),
}

#: 单独作为主键的修饰键写法
_SOLO_MODIFIER_KEYS: dict[str, int] = {
    "left ctrl": VK_LCONTROL, "right ctrl": VK_RCONTROL, "ctrl": VK_CONTROL,
    "left alt": VK_LMENU, "right alt": VK_RMENU, "alt": VK_MENU,
    "left shift": VK_LSHIFT, "right shift": VK_RSHIFT, "shift": VK_SHIFT,
    "left win": VK_LWIN, "right win": VK_RWIN,
}


def parse_combo(combo: str) -> tuple[list[str], int] | None:
    """把 ``ctrl+alt+space`` 解析成 ``(["ctrl", "alt"], 0x20)``。

    返回 ``None`` 表示无法解析。
    """
    if not combo or not combo.strip():
        return None

    text = combo.strip().lower()
    # "left ctrl" 这类带空格的键名先用下划线保护起来
    for name in ("left ctrl", "right ctrl", "left alt", "right alt",
                 "left shift", "right shift", "left win", "right win"):
        text = text.replace(name, name.replace(" ", "_"))

    parts = [p.replace("_", " ").strip() for p in text.split("+")]
    parts = [p for p in parts if p]
    if not parts:
        return None

    modifiers: list[str] = []
    main_vk: int | None = None

    for index, part in enumerate(parts):
        is_last = index == len(parts) - 1
        if part in _MODIFIER_GROUPS and not is_last:
            modifiers.append(part)
            continue
        if is_last:
            if part in _SOLO_MODIFIER_KEYS and len(parts) == 1:
                main_vk = _SOLO_MODIFIER_KEYS[part]
                break
            if part in _MODIFIER_GROUPS:
                return None  # 组合键最后一段不能只是修饰键
            vk = _main_key_vk(part)
            if vk is None:
                return None
            main_vk = vk
            break
        return None

    if main_vk is None:
        return None
    return modifiers, main_vk


def _main_key_vk(name: str) -> int | None:
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if len(name) == 1:
        ch = name
        if "a" <= ch <= "z":
            return ord(ch.upper())
        if "0" <= ch <= "9":
            return ord(ch)
    if name.startswith("f") and name[1:].isdigit():
        num = int(name[1:])
        if 1 <= num <= 24:
            return 0x70 + num - 1
    return None


def _vk_display(vk: int) -> str:
    for name, code in _NAMED_KEYS.items():
        if code == vk:
            return name
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x70 + 1}"
    return f"0x{vk:02X}"


@dataclass
class HotkeySpec:
    combo: str
    modifiers: list[str] = field(default_factory=list)
    main_vk: int = 0
    suppress: bool = True


def _is_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class NativeKeyboardHook:
    """低级键盘钩子，支持多个热键与按键抑制。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._hook: int | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._specs: list[HotkeySpec] = []
        self._callbacks: dict[str, tuple[Callable[[], None], Callable[[], None] | None]] = {}
        self._armed: set[str] = set()  # 已经触发过按下、尚未松开的热键
        self._proc = HOOKPROC(self._hook_proc)  # 必须保持引用，否则会被 GC 回收
        self._install_error: str = ""
        self._paused = False

    # -- 暂停 -------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        """暂停热键：不触发回调，也不吞任何按键。

        设置界面捕获新快捷键时必须先暂停，否则用户按下的组合键会被本钩子吞掉，
        根本传不到设置窗口里 —— 表现为"点进去按什么都没反应"。
        """
        with self._lock:
            if self._paused == paused:
                return
            self._paused = paused
            self._armed.clear()  # 暂停前后丢掉半按状态，避免松键回调错配
        log.info("全局热键已%s", "暂停（设置界面捕获中）" if paused else "恢复")

    # -- 安装 / 卸载 ------------------------------------------------------
    def start(self) -> bool:
        if self._thread is not None:
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="vtw-kbdhook", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            log.error("键盘钩子线程启动超时")
            return False
        if self._hook is None:
            log.error("键盘钩子安装失败：%s", self._install_error or "未知原因")
            return False
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        self._hook = None
        self._thread_id = 0

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        try:
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        except Exception as exc:  # noqa: BLE001
            self._install_error = str(exc)
            self._hook = None

        if not self._hook:
            self._install_error = f"SetWindowsHookExW 失败，错误码 {ctypes.get_last_error()}"
            self._ready.set()
            return

        log.info("原生键盘钩子已安装（thread=%d）", self._thread_id)
        self._ready.set()

        msg = wintypes.MSG()
        while not self._stop.is_set():
            # GetMessageW 返回 0 表示收到 WM_QUIT
            if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) == 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        try:
            user32.UnhookWindowsHookEx(self._hook)
        except Exception:  # noqa: BLE001
            pass
        log.info("原生键盘钩子已卸载")

    # -- 热键注册 ---------------------------------------------------------
    def register(
        self,
        combo: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None] | None = None,
        *,
        suppress: bool = True,
    ) -> bool:
        parsed = parse_combo(combo)
        if parsed is None:
            log.error("无法解析热键：%r", combo)
            return False
        modifiers, main_vk = parsed

        with self._lock:
            # 同一组合重复注册时替换旧回调
            self._specs = [s for s in self._specs if s.combo != combo]
            self._specs.append(
                HotkeySpec(combo=combo, modifiers=modifiers, main_vk=main_vk, suppress=suppress)
            )
            self._callbacks[combo] = (on_press, on_release)
        log.info(
            "注册热键 %s（修饰键=%s 主键=%s 抑制=%s）",
            combo,
            modifiers or "无",
            _vk_display(main_vk),
            suppress,
        )
        return True

    def unregister_all(self) -> None:
        with self._lock:
            self._specs.clear()
            self._callbacks.clear()
            self._armed.clear()

    # -- 钩子过程 ---------------------------------------------------------
    def _hook_proc(self, code: int, wparam: int, lparam: int) -> int:
        if code != HC_ACTION:
            return user32.CallNextHookEx(None, code, wparam, lparam)

        try:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)

            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                swallow = self._handle_down(vk)
            elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                swallow = self._handle_up(vk)
            else:
                swallow = False

            if swallow:
                return 1  # 非零 = 吞掉该事件，不再传给后续钩子/目标窗口
        except Exception:  # noqa: BLE001 - 钩子内绝不能抛异常
            log.exception("键盘钩子处理异常")

        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _handle_down(self, vk: int) -> bool:
        """返回 True 表示要吞掉这次按下事件。"""
        with self._lock:
            if self._paused:
                return False
            specs = list(self._specs)
            callbacks = dict(self._callbacks)
            armed = set(self._armed)

        for spec in specs:
            if vk != spec.main_vk:
                continue
            if not all(any(_is_down(m) for m in _MODIFIER_GROUPS[name]) for name in spec.modifiers):
                continue

            if spec.combo not in armed:
                self._armed.add(spec.combo)
                on_press = callbacks.get(spec.combo, (None, None))[0]
                if on_press:
                    self._invoke(on_press)
            # 长按产生的重复 keydown：已经触发过就只吞掉，不再重复触发按下回调
            return spec.suppress

        return False

    def _handle_up(self, vk: int) -> bool:
        """返回 True 表示要吞掉这次松开事件。"""
        with self._lock:
            if self._paused:
                return False
            specs = list(self._specs)
            callbacks = dict(self._callbacks)
            armed = set(self._armed)

        for spec in specs:
            if vk != spec.main_vk:
                continue
            # 不是我们触发过的（例如用户单独按了主键），放行且不回调
            if spec.combo not in armed:
                continue
            self._armed.discard(spec.combo)
            on_release = callbacks.get(spec.combo, (None, None))[1]
            if on_release:
                self._invoke(on_release)
            return spec.suppress

        return False

    @staticmethod
    def _invoke(callback: Callable[[], None]) -> None:
        """回调必须在钩子里**极快返回**，否则会拖慢整个系统的键盘响应。"""
        try:
            callback()
        except Exception:  # noqa: BLE001
            log.exception("热键回调异常")


# 兼容旧命名
HookInstallError = RuntimeError
