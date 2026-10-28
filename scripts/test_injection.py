#!/usr/bin/env python3
"""端到端注入测试：音频文件 → 识别 → 注入到真实窗口。

这是"按住热键说话"链路里**可以被自动化验证**的那一半：
真实模型推理 → 真实剪贴板 → 真实 ``SendInput`` → 真实窗口控件。
（唯一无法自动化的部分是人对着麦克风说话，那属于手工验收清单。）

为什么用原生 Win32 EDIT 控件而不是 Tk 输入框
--------------------------------------------
实测发现 Tk/Tcl **无法解析 SendInput 注入的按键**（keysym 变成 ``??``），
连显式绑定 ``<Control-Key-v>`` 都不触发，因此 Tk 是不合适的测试目标 ——
用它测会得到"产品有 bug"的错误结论。而原生 EDIT 控件正是记事本、各类
Win32/Electron 应用处理粘贴的方式，是忠实的目标。

实测结论：两种写法（仅 wVk / wVk+wScan）在原生控件上都能成功粘贴；
``inject.py`` 采用带扫描码的写法，因为那是 Windows 文档推荐的方式，
对依赖扫描码做键盘布局翻译的程序兼容性更好。

用法::

    python scripts/test_injection.py
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import wave
from ctypes import wintypes
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 日志走 stdout：默认的 logging 兜底处理器会写 stderr，
# 在 PowerShell 里会被渲染成 NativeCommandError，导致脚本明明成功却报 exit 1。
logging.basicConfig(
    level=logging.WARNING,
    format="      [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

from vtw import paths, winclip  # noqa: E402
from vtw.asr import SenseVoiceEngine  # noqa: E402
from vtw.inject import inject_text  # noqa: E402
from vtw.textutil import is_meaningful  # noqa: E402

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

u32.CreateWindowExW.restype = wintypes.HWND
u32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetForegroundWindow.restype = wintypes.HWND

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CHILD, WS_VISIBLE, WS_BORDER, WS_VSCROLL = 0x40000000, 0x10000000, 0x00800000, 0x00200000
ES_MULTILINE, ES_AUTOVSCROLL = 0x0004, 0x0040
SW_SHOW = 5

MSG = wintypes.MSG


def pump(seconds: float) -> None:
    """跑消息循环。

    PeekMessage 的 hWnd 必须传 NULL（不按窗口过滤），否则 EDIT 子控件的
    WM_KEYDOWN 会一直留在队列里，看起来像"粘贴没反应"。
    """
    end = time.monotonic() + seconds
    msg = MSG()
    while time.monotonic() < end:
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def force_foreground(hwnd: int) -> bool:
    """把窗口切到系统前台（SendInput 只送给前台窗口）。"""
    cur = k32.GetCurrentThreadId()
    fg = u32.GetForegroundWindow()
    fg_thread = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_thread and fg_thread != cur:
        attached = bool(u32.AttachThreadInput(fg_thread, cur, True))
    try:
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        u32.SetFocus(hwnd)
    finally:
        if attached:
            u32.AttachThreadInput(fg_thread, cur, False)
    return int(u32.GetForegroundWindow() or 0) == hwnd


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as fh:
        channels, rate = fh.getnchannels(), fh.getframerate()
        frames = fh.readframes(fh.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def acquire_foreground(hwnd: int, *, stable_checks: int = 3, timeout: float = 3.0) -> bool:
    """反复抢前台，直到**连续若干次**检测都稳定在我们窗口上。

    本机环境里其他进程会间歇抢占前台焦点，单次抢到并不代表注入那一刻还在。
    因此要求连续稳定后才认为可以开始注入。
    """
    deadline = time.monotonic() + timeout
    streak = 0
    while time.monotonic() < deadline:
        if int(u32.GetForegroundWindow() or 0) != hwnd:
            streak = 0
            force_foreground(hwnd)
            pump(0.05)
            continue
        streak += 1
        if streak >= stable_checks:
            return True
        pump(0.05)
    return False


def inject_with_pump(
    text: str,
    method: str,
    hwnd: int,
    edit: int,
    *,
    attempts: int = 4,
    **kwargs,
) -> tuple[bool, bool]:
    """在**持续跑消息循环**的同时执行注入，并用控件内容校验是否真的成功。

    返回 ``(是否成功写入, 成功后目标窗口是否仍是前台)``。

    三个关键点：

    1. 真实目标程序（记事本、微信、浏览器）一直在处理消息，收到 Ctrl+V 会在
       几毫秒内完成粘贴。若测试里等 ``inject_text`` 返回后才开始 pump，粘贴会
       被推迟到"剪贴板已恢复"之后，从而误判成注入失败。因此注入放子线程，
       主线程持续 pump，忠实复现真实时序。
    2. 注入前必须确认目标窗口**稳定地**处于前台。本机有进程会间歇抢焦点，
       被抢走时 SendInput 会打到别人身上，表现为"注入无反应"——
       这属于测试环境干扰，必须与产品缺陷区分开。
    3. 判定依据是**控件里的实际内容**，而不是 ``inject_text`` 的返回值；
       失败就重新抢前台再来一遍。
    """
    buf = ctypes.create_unicode_buffer(8192)

    def content() -> str:
        u32.GetWindowTextW(edit, buf, 8192)
        return buf.value.strip()

    last_foreground = True
    for attempt in range(1, attempts + 1):
        u32.SetWindowTextW(edit, "")
        pump(0.1)

        if not acquire_foreground(hwnd):
            print(f"      [第 {attempt} 次] 无法稳定取得前台焦点，重试…")
            last_foreground = False
            continue

        u32.SetFocus(edit)
        pump(0.1)
        if int(u32.GetForegroundWindow() or 0) != hwnd:
            last_foreground = False
            continue  # 就在这一瞬间被抢走了

        holder: dict[str, object] = {}

        def worker() -> None:
            try:
                holder["ok"] = inject_text(text, method=method, **kwargs)
            except Exception as exc:  # noqa: BLE001
                holder["error"] = exc

        thread = threading.Thread(target=worker, name="injector")
        thread.start()
        while thread.is_alive():
            pump(0.02)
        thread.join(timeout=5.0)
        pump(0.6)

        last_foreground = int(u32.GetForegroundWindow() or 0) == hwnd
        if "error" in holder:
            print(f"      [第 {attempt} 次] 异常：{holder['error']}")
            continue
        if content() == text:
            return True, last_foreground
        print(
            f"      [第 {attempt} 次] 未写入"
            f"（inject_text={holder.get('ok')}, 前台仍属目标={last_foreground}），重试…"
        )

    return False, last_foreground


def main() -> int:
    wav = paths.SENSE_VOICE_DIR / "test_wavs" / "zh.wav"
    if not wav.is_file():
        print(f"[失败] 找不到测试音频：{wav}", file=sys.stderr)
        return 1

    print("=" * 64)
    print("vtw 端到端注入测试（识别 → 剪贴板/SendInput → 原生控件）")
    print("=" * 64)

    # ---------------------------------------------------------- 1. 识别
    print("\n[1/4] 加载模型并识别 zh.wav …")
    engine = SenseVoiceEngine(num_threads=4, language="auto", use_itn=True)
    audio, rate = read_wav(wav)
    engine.transcribe(audio, rate)  # 预热
    result = engine.transcribe(audio, rate)
    print(f"      识别结果：{result.text!r}")
    if not is_meaningful(result.text):
        print("[失败] 识别结果为空", file=sys.stderr)
        return 1

    # ---------------------------------------------------------- 2. 目标窗口
    print("\n[2/4] 创建原生 EDIT 控件作为注入目标 …")
    hinst = k32.GetModuleHandleW(None)
    root = u32.CreateWindowExW(
        0, "STATIC", "vtw 注入测试目标（原生 EDIT 控件）", WS_OVERLAPPEDWINDOW,
        260, 260, 560, 240, None, None, hinst, None,
    )
    edit = u32.CreateWindowExW(
        0, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_BORDER | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL,
        10, 10, 520, 170, root, None, hinst, None,
    )
    u32.ShowWindow(root, SW_SHOW)
    u32.UpdateWindow(root)

    focused = False
    for _ in range(20):
        pump(0.1)
        if force_foreground(root):
            focused = True
            break
    if not focused:
        print("      [无法判定] 抢不到系统前台窗口，本环境可能没有可交互桌面。")
        u32.DestroyWindow(root)
        return 2
    u32.SetFocus(edit)
    pump(0.3)
    print(f"      目标就绪（root=0x{root:X} edit=0x{edit:X}，已是系统前台）")

    # ---------------------------------------------------------- 3. 剪贴板方案
    print("\n[3/4] 剪贴板方案注入 …")
    sentinel = "VTW_CLIPBOARD_SENTINEL_请勿手动修改"
    original = winclip.snapshot()
    winclip.set_text(sentinel)
    pump(0.1)

    u32.SetWindowTextW(edit, "")
    pump(0.1)
    ok_clip, fg_clip = inject_with_pump(
        result.text, "clipboard", root, edit, restore_clipboard=True, restore_delay_ms=500
    )
    buf = ctypes.create_unicode_buffer(8192)
    u32.GetWindowTextW(edit, buf, 8192)
    clip_content = buf.value.strip()
    restored = winclip.get_text()

    # ---------------------------------------------------------- 4. Unicode 方案
    print("\n[4/4] Unicode 直接注入方案（不碰剪贴板）…")
    ok_uni, fg_uni = inject_with_pump(result.text, "unicode", root, edit)
    u32.GetWindowTextW(edit, buf, 8192)
    uni_content = buf.value.strip()

    u32.DestroyWindow(root)

    # ---------------------------------------------------------- 判定
    print("\n" + "=" * 64)
    print(f"识别文本            ：{result.text!r}")
    print(f"剪贴板注入后控件内容：{clip_content!r}")
    print(f"Unicode 注入后内容  ：{uni_content!r}")
    print(f"剪贴板恢复          ：{restored!r}")
    print(f"注入后仍在前台      ：剪贴板={fg_clip}  Unicode={fg_uni}")

    failures: list[str] = []
    if not ok_clip:
        failures.append("剪贴板方案 inject_text 返回 False")
    if clip_content != result.text:
        if not fg_clip:
            failures.append(
                f"[环境] 注入期间目标窗口被别的进程抢走前台，无法判定"
                f"（内容={clip_content!r}）"
            )
        else:
            failures.append(f"剪贴板方案内容不符：{clip_content!r}")
    if restored != sentinel:
        failures.append(f"剪贴板未被正确恢复：{restored!r}")
    if not ok_uni:
        failures.append("Unicode 方案 inject_text 返回 False")
    if uni_content != result.text:
        if not fg_uni:
            failures.append(
                f"[环境] 注入期间目标窗口被别的进程抢走前台，无法判定"
                f"（内容={uni_content!r}）"
            )
        else:
            failures.append(f"Unicode 方案内容不符：{uni_content!r}")

    if failures:
        environment_only = all(item.startswith("[环境]") for item in failures)
        if environment_only:
            print("\n[无法自动判定] 本机有进程持续抢占前台焦点，注入测试不适用。")
            for item in failures:
                print(f"  ! {item}", file=sys.stderr)
            print("  请按 README 的手工验收清单验证注入。")
            return 2
        print("\n测试未通过：", file=sys.stderr)
        for item in failures:
            print(f"  ✗ {item}", file=sys.stderr)
        return 1

    # 收尾：把用户原来的剪贴板还回去
    if original.had_text:
        winclip.set_text(original.text or "")

    print("\n测试通过 ✓")
    print("  · 识别 → 剪贴板 → Ctrl+V → 原生控件：正常，且剪贴板已恢复")
    print("  · 识别 → Unicode SendInput → 原生控件：正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
