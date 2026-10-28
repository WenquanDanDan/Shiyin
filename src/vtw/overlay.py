"""录音状态浮条（tkinter，标准库自带，零额外依赖）。

**最关键的一点：浮条绝不能抢焦点。**
用户按下热键时焦点在记事本/微信里，识别完要 ``Ctrl+V`` 粘贴到那个窗口。
一旦浮条获得焦点，粘贴就会打到浮条自己身上，文字"消失"。
因此除了 ``overrideredirect(True)``（无边框、不进任务栏），还额外设置
``WS_EX_NOACTIVATE``，从窗口样式层面禁止激活。
"""

from __future__ import annotations

import logging
import tkinter as tk

log = logging.getLogger(__name__)

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

# 配色
_BG = "#1f2430"
_FG = "#e8ecf1"
_DIM = "#7f8794"
_RED = "#ff5c5c"
_AMBER = "#ffc44d"
_GREEN = "#57d9a3"


class _LevelMeter(tk.Canvas):
    """简单的电平条。"""

    BARS = 14

    def __init__(self, master: tk.Misc, width: int = 120, height: int = 22) -> None:
        super().__init__(
            master, width=width, height=height, bg=_BG, highlightthickness=0, bd=0
        )
        self._levels = [0.0] * self.BARS
        self._items: list[int] = []
        gap = 2
        bar_w = max(2, (width - gap * (self.BARS - 1)) / self.BARS)
        for i in range(self.BARS):
            x0 = i * (bar_w + gap)
            self._items.append(
                self.create_rectangle(x0, height, x0 + bar_w, height, fill=_DIM, width=0)
            )
        self._bar_w = bar_w
        self._height = height

    def push(self, level: float) -> None:
        self._levels.pop(0)
        self._levels.append(max(0.0, min(1.0, level)))
        for item, value in zip(self._items, self._levels):
            h = max(2.0, value * self._height)
            x0, _, x1, _ = self.coords(item)
            self.coords(item, x0, self._height - h, x1, self._height)
            color = _GREEN if value < 0.75 else _AMBER if value < 0.92 else _RED
            self.itemconfig(item, fill=color if value > 0.02 else _DIM)


class Overlay:
    """置顶状态浮条。所有方法都必须在 Tk 主线程中调用。"""

    def __init__(self, root: tk.Tk, *, position: str = "bottom-center", enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled and position != "off"
        self.position = position
        self._win: tk.Toplevel | None = None
        self._dot: tk.Canvas | None = None
        self._label: tk.Label | None = None
        self._meter: _LevelMeter | None = None
        self._pulse_job: str | None = None
        self._pulse_on = True
        self._visible = False

    # -- 构建 -------------------------------------------------------------
    def _build(self) -> None:
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)  # 无边框、不进任务栏
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.90)
        win.configure(bg=_BG)

        frame = tk.Frame(win, bg=_BG, padx=12, pady=8)
        frame.pack(fill="both", expand=True)

        self._dot = tk.Canvas(
            frame, width=12, height=12, bg=_BG, highlightthickness=0, bd=0
        )
        self._dot.pack(side="left", padx=(0, 8))
        self._dot_id = self._dot.create_oval(2, 2, 10, 10, fill=_DIM, width=0)

        self._label = tk.Label(
            frame,
            text="",
            bg=_BG,
            fg=_FG,
            font=("Microsoft YaHei UI", 10),
            anchor="w",
            justify="left",
        )
        self._label.pack(side="left")

        self._meter = _LevelMeter(frame)
        self._meter.pack(side="left", padx=(10, 0))

        win.update_idletasks()
        self._win = win
        self._apply_no_activate()

    def _apply_no_activate(self) -> None:
        """从 Win32 层面禁止激活，确保不会抢走焦点。"""
        if self._win is None:
            return
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = int(self._win.winfo_id())
            # winfo_id 给的是子窗口，取顶层窗口句柄
            parent = user32.GetParent(hwnd)
            target = parent if parent else hwnd
            user32.GetWindowLongW.restype = ctypes.c_long
            style = user32.GetWindowLongW(target, GWL_EXSTYLE)
            user32.SetWindowLongW(
                target, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
        except Exception as exc:  # noqa: BLE001 - 失败也不致命，只是保险层
            log.debug("设置 WS_EX_NOACTIVATE 失败：%s", exc)

    # -- 定位 -------------------------------------------------------------
    def _reposition(self) -> None:
        if self._win is None:
            return
        self._win.update_idletasks()
        w = max(self._win.winfo_reqwidth(), self._win.winfo_width())
        h = max(self._win.winfo_reqheight(), self._win.winfo_height())
        screen_w = self._win.winfo_screenwidth()
        screen_h = self._win.winfo_screenheight()
        margin = 90

        if self.position == "top-center":
            x, y = (screen_w - w) // 2, margin
        elif self.position == "bottom-right":
            x, y = screen_w - w - 40, screen_h - h - margin
        else:  # bottom-center
            x, y = (screen_w - w) // 2, screen_h - h - margin
        self._win.geometry(f"{w}x{h}+{x}+{y}")

    # -- 状态 -------------------------------------------------------------
    def _show(self) -> None:
        if not self.enabled or self._win is None:
            return
        self._reposition()
        self._win.deiconify()
        self._win.attributes("-topmost", True)
        self._visible = True

    def hide(self) -> None:
        self._stop_pulse()
        if self._win is not None and self._visible:
            self._win.withdraw()
        self._visible = False

    def show_recording(self) -> None:
        if not self.enabled:
            return
        if self._win is None:
            self._build()
        self._set_state("正在聆听…", _RED)
        if self._meter is not None:
            self._meter.pack(side="left", padx=(10, 0))
        self._start_pulse()
        self._show()

    def show_recognizing(self) -> None:
        if not self.enabled:
            return
        if self._win is None:
            self._build()
        self._set_state("识别中…", _AMBER)
        self._stop_pulse()
        self._show()

    def show_text(self, text: str, *, ok: bool = True) -> None:
        """短暂显示识别结果。"""
        if not self.enabled:
            return
        if self._win is None:
            self._build()
        preview = text if len(text) <= 40 else text[:39] + "…"
        self._set_state(preview or "（未识别到内容）", _GREEN if ok else _DIM)
        self._stop_pulse()
        self._show()
        self.root.after(1600, self.hide)

    def show_message(self, message: str, *, error: bool = False) -> None:
        if not self.enabled:
            return
        if self._win is None:
            self._build()
        self._set_state(message, _RED if error else _DIM)
        self._stop_pulse()
        self._show()
        self.root.after(2600, self.hide)

    def push_level(self, level: float) -> None:
        """更新电平条。仅录音状态下有效。"""
        if self._meter is not None and self._visible:
            self._meter.push(level)

    # -- 内部 -------------------------------------------------------------
    def _set_state(self, text: str, color: str) -> None:
        if self._label is not None:
            self._label.config(text=text)
        if self._dot is not None:
            self._dot.itemconfig(self._dot_id, fill=color)

    def _start_pulse(self) -> None:
        self._stop_pulse()
        self._pulse_on = True
        self._pulse()

    def _pulse(self) -> None:
        if self._dot is None:
            return
        self._pulse_on = not self._pulse_on
        self._dot.itemconfig(self._dot_id, fill=_RED if self._pulse_on else "#7a2b2b")
        self._pulse_job = self.root.after(500, self._pulse)

    def _stop_pulse(self) -> None:
        if self._pulse_job is not None:
            try:
                self.root.after_cancel(self._pulse_job)
            except Exception:  # noqa: BLE001
                pass
            self._pulse_job = None

    def destroy(self) -> None:
        self._stop_pulse()
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._win = None
