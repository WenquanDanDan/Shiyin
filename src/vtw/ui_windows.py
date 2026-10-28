"""tkinter 窗口：会议字幕窗口、历史记录查看器、关于对话框。

全部必须在 **Tk 主线程** 中创建/更新 —— app 通过 ``root.after`` 轮询命令队列
把请求转到这里执行。
"""

from __future__ import annotations

import logging
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any

log = logging.getLogger(__name__)

_BG = "#1f2430"
_FG = "#e8ecf1"
_DIM = "#7f8794"
_ACCENT = "#57d9a3"
_FONT = ("Microsoft YaHei UI", 10)


class MeetingWindow:
    """会议实时字幕窗口（也被文件转写复用，因此标题/按钮文字可定制）。"""

    def __init__(
        self,
        root: tk.Tk,
        *,
        on_stop: Any = None,
        title: str = "vtw 会议转写",
        stop_text: str = "停止并保存",
    ) -> None:
        self.root = root
        self.on_stop = on_stop
        self.win = tk.Toplevel(root)
        # 先隐藏：构造期间不闪窗，构造出错时也不会留下一个空白窗口
        self.win.withdraw()
        self.win.title(title)
        self.win.geometry("720x460")
        self.win.configure(bg=_BG)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)

        header = tk.Frame(self.win, bg=_BG, padx=12, pady=8)
        header.pack(fill="x")

        self.status = tk.Label(
            header,
            text="未开始",
            bg=_BG,
            fg=_FG,
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
        )
        self.status.pack(side="left")

        self.stop_button = tk.Button(
            header,
            text=stop_text,
            command=self._stop,
            font=_FONT,
            bg="#2c3340",
            fg=_FG,
            activebackground="#3a4354",
            activeforeground=_FG,
            relief="flat",
            padx=12,
            pady=3,
        )
        self.stop_button.pack(side="right")

        # 控件选项 padx/pady 只接受单个距离，双值元组属于 pack/grid 的几何选项。
        # 曾误写成 pady=(0, 8)，构造时抛 TclError("bad screen distance")，
        # 会议字幕窗口完全建不出来 —— 会议转写看似已启动，界面却始终不出现。
        body = tk.Frame(self.win, bg=_BG, padx=12)
        body.pack(fill="both", expand=True, pady=(0, 8))

        self.text = tk.Text(
            body,
            wrap="word",
            bg="#171b24",
            fg=_FG,
            insertbackground=_FG,
            font=_FONT,
            relief="flat",
            padx=10,
            pady=8,
            state="disabled",
        )
        scrollbar = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.footer = tk.Label(
            self.win,
            text="",
            bg=_BG,
            fg=_DIM,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            padx=12,
            pady=4,
        )
        self.footer.pack(fill="x")

    # -- 显示 -------------------------------------------------------------
    def show(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(200, lambda: self.win.attributes("-topmost", False))

    def hide(self) -> None:
        self.win.withdraw()

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass

    # -- 内容 -------------------------------------------------------------
    def set_status(self, text: str, *, color: str = _FG) -> None:
        try:
            self.status.config(text=text, fg=color)
        except tk.TclError:
            pass

    def set_footer(self, text: str) -> None:
        try:
            self.footer.config(text=text)
        except tk.TclError:
            pass

    def set_recording(
        self, running: bool, *, status: str | None = None, color: str = _DIM
    ) -> None:
        self.stop_button.config(state="normal" if running else "disabled")
        if status is not None:
            self.set_status(status, color=color)
        elif not running:
            self.set_status("已停止", color=_DIM)

    def append(self, text: str, offset: float) -> None:
        stamp = f"{int(offset // 60):02d}:{offset % 60:05.2f}"
        try:
            self.text.config(state="normal")
            self.text.insert("end", f"[{stamp}] {text}\n")
            self.text.see("end")
            self.text.config(state="disabled")
        except tk.TclError:
            pass

    def clear(self) -> None:
        try:
            self.text.config(state="normal")
            self.text.delete("1.0", "end")
            self.text.config(state="disabled")
        except tk.TclError:
            pass

    def _stop(self) -> None:
        if self.on_stop:
            try:
                self.on_stop()
            except Exception:  # noqa: BLE001
                log.exception("停止会议转写失败")


class HistoryWindow:
    """历史记录查看器。"""

    def __init__(self, root: tk.Tk, records: list[dict[str, Any]]) -> None:
        self.win = tk.Toplevel(root)
        self.win.title(f"vtw 历史记录（最近 {len(records)} 条）")
        self.win.geometry("680x480")
        self.win.configure(bg=_BG)

        body = tk.Frame(self.win, bg=_BG, padx=12, pady=12)
        body.pack(fill="both", expand=True)

        self.text = tk.Text(
            body,
            wrap="word",
            bg="#171b24",
            fg=_FG,
            font=_FONT,
            relief="flat",
            padx=10,
            pady=8,
        )
        scrollbar = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        if not records:
            self.text.insert("end", "（还没有历史记录）\n")
        for record in records:
            when = str(record.get("time", ""))
            source = {"mic": "听写", "meeting": "会议"}.get(str(record.get("source")), "?")
            text = str(record.get("text", ""))
            rtf = record.get("elapsed", 0)
            duration = record.get("duration", 0)
            try:
                perf = f"  {float(duration):.1f}s音频/{float(rtf):.2f}s推理"
            except (TypeError, ValueError):
                perf = ""
            self.text.insert("end", f"[{when}] ({source}){perf}\n{text}\n\n")
        self.text.config(state="disabled")


def show_about(root: tk.Tk, version: str, details: str) -> None:
    """关于对话框。"""
    messagebox.showinfo("关于 vtw 语音转文字", f"vtw 本地语音转文字 v{version}\n\n{details}")


def show_error(root: tk.Tk, title: str, message: str) -> None:
    messagebox.showerror(title, message)


def show_info(root: tk.Tk, title: str, message: str) -> None:
    messagebox.showinfo(title, message)
