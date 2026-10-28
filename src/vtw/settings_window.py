"""设置窗口：修改快捷键与开机自启。

设计要点
--------
**捕获快捷键前必须暂停全局热键。**
否则用户按下 ``Ctrl+Alt+Space`` 想改键时，这个组合会被我们自己的全局钩子吞掉，
根本传不到设置窗口 —— 表现为"点进去按什么都没反应"。因此捕获控件进入录制态时
调用 ``app.hotkeys.set_paused(True)``，结束（确认或取消）后立即恢复。

窗口只在 Tk 主线程创建与更新，由 :mod:`vtw.app` 通过 UI 队列调度。
"""

from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox
from typing import Any, Callable

from . import autostart
from .hotkey import validate_combo
from .nativehook import parse_combo

log = logging.getLogger(__name__)

_BG = "#1f2430"
_PANEL = "#171b24"
_FG = "#e8ecf1"
_DIM = "#8b93a1"
_ACCENT = "#57d9a3"
_ERROR = "#ff6b6b"
_FIELD = "#2c3340"
_FONT = ("Microsoft YaHei UI", 10)
_FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")

#: tkinter keysym → 修饰键名（用于组合键展示）
_MODIFIER_KEYSYMS: dict[str, str] = {
    "Control_L": "ctrl",
    "Control_R": "right ctrl",
    "Alt_L": "alt",
    "Alt_R": "right alt",
    "Shift_L": "shift",
    "Shift_R": "right shift",
    "Super_L": "win",
    "Super_R": "right win",
    "Meta_L": "win",
    "Meta_R": "right win",
}

#: tkinter keysym → 组合键里的按键名
_KEY_KEYSYMS: dict[str, str] = {
    "space": "space",
    "Return": "enter",
    "KP_Enter": "enter",
    "Tab": "tab",
    "BackSpace": "backspace",
    "Delete": "delete",
    "Insert": "insert",
    "Home": "home",
    "End": "end",
    "Prior": "pageup",
    "Next": "pagedown",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "grave": "`",
    "minus": "-",
    "equal": "=",
    "bracketleft": "[",
    "bracketright": "]",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "Print": "printscreen",
    "Caps_Lock": "capslock",
}

#: 组合键展示顺序
_MOD_ORDER = ["ctrl", "alt", "shift", "win", "right ctrl", "right alt", "right shift", "right win"]


def keysym_to_key_name(keysym: str) -> str | None:
    """把 tkinter 的 keysym 转成组合键里的按键名。"""
    if keysym in _KEY_KEYSYMS:
        return _KEY_KEYSYMS[keysym]
    if len(keysym) == 1 and keysym.isalnum():
        return keysym.lower()
    if keysym.startswith("F") and keysym[1:].isdigit():
        num = int(keysym[1:])
        if 1 <= num <= 24:
            return keysym.lower()
    return None


def format_combo(modifiers: list[str], key: str) -> str:
    """把修饰键与主键拼成组合键字符串。"""
    ordered = [m for m in _MOD_ORDER if m in modifiers]
    return "+".join(ordered + [key])


@dataclass
class RecordOutcome:
    """录制状态机一次输入的结果。"""

    combo: str | None = None  # 录制完成，得到组合键
    error: str | None = None  # 不支持的键或非法组合
    preview: str | None = None  # 当前按住状态的预览文本
    cancelled: bool = False  # 用户按了 Esc


class ComboRecorder:
    """组合键录制状态机。

    与 tkinter 解耦的**纯逻辑**，因此可以直接用单测覆盖最容易出错的部分：
    修饰键跟踪、单修饰键（如右 Ctrl）、非法组合拦截。
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.held_mods: set[str] = set()
        self.mod_pressed_alone: str | None = None
        self.saw_non_modifier = False

    def press(self, keysym: str) -> RecordOutcome:
        if keysym == "Escape":
            return RecordOutcome(cancelled=True)

        if keysym in _MODIFIER_KEYSYMS:
            name = _MODIFIER_KEYSYMS[keysym]
            self.held_mods.add(name)
            if not self.saw_non_modifier:
                self.mod_pressed_alone = name
            ordered = [m for m in _MOD_ORDER if m in self.held_mods]
            return RecordOutcome(preview="+".join(ordered) + "+…" if ordered else None)

        key = keysym_to_key_name(keysym)
        if key is None:
            return RecordOutcome(error=f"不支持的按键：{keysym}")

        self.saw_non_modifier = True
        modifiers = sorted(self.held_mods & {"ctrl", "alt", "shift", "win"})
        combo = format_combo(modifiers, key)
        ok, message = validate_combo(combo)
        if not ok:
            return RecordOutcome(error=message)
        return RecordOutcome(combo=combo)

    def release(self, keysym: str) -> RecordOutcome:
        """处理松开事件。单独按一个修饰键（如右 Ctrl）也算合法热键。"""
        if keysym not in _MODIFIER_KEYSYMS:
            return RecordOutcome()

        name = _MODIFIER_KEYSYMS[keysym]
        if not self.saw_non_modifier and self.mod_pressed_alone == name:
            ok, _ = validate_combo(name)
            if ok:
                return RecordOutcome(combo=name)

        self.held_mods.discard(name)
        ordered = [m for m in _MOD_ORDER if m in self.held_mods]
        return RecordOutcome(preview="+".join(ordered) + "+…" if ordered else None)


class HotkeyCapture(tk.Frame):
    """点击后进入"按下组合键"录制态的输入控件。"""

    def __init__(self, master: tk.Misc, *, on_state_change: Callable[[bool], None]) -> None:
        super().__init__(master, bg=_BG)
        self.on_state_change = on_state_change
        self.recorder = ComboRecorder()

        self.value: str = ""
        self._capturing = False

        self.button = tk.Button(
            self,
            text="（未设置）",
            font=("Consolas", 11),
            bg=_FIELD,
            fg=_FG,
            activebackground="#3a4354",
            activeforeground=_FG,
            relief="flat",
            padx=14,
            pady=6,
            anchor="w",
            width=22,
            command=self.start_capture,
        )
        self.button.pack(side="left")

        self.hint = tk.Label(self, text="", bg=_BG, fg=_DIM, font=("Microsoft YaHei UI", 9))
        self.hint.pack(side="left", padx=(10, 0))

    # -- 值 ---------------------------------------------------------------
    def set_value(self, combo: str) -> None:
        self.value = combo
        if not self._capturing:
            self.button.config(text=combo or "（未设置）", fg=_FG)

    # -- 录制 -------------------------------------------------------------
    @property
    def capturing(self) -> bool:
        return self._capturing

    def start_capture(self) -> None:
        if self._capturing:
            self.cancel_capture()
            return
        self._capturing = True
        self.recorder.reset()
        self.button.config(text="请按下组合键…", bg="#3d4a2f", fg=_ACCENT)
        self.hint.config(text="Esc 取消", fg=_DIM)
        self.on_state_change(True)

    def cancel_capture(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        self.button.config(text=self.value or "（未设置）", bg=_FIELD, fg=_FG)
        self.hint.config(text="")
        self.on_state_change(False)

    def _finish(self, combo: str) -> None:
        self._capturing = False
        self.value = combo
        self.button.config(text=combo, bg=_FIELD, fg=_FG)
        self.hint.config(text="已记录", fg=_ACCENT)
        self.on_state_change(False)

    # -- 事件 -------------------------------------------------------------
    def handle_key_press(self, event: tk.Event) -> None:
        if not self._capturing:
            return
        outcome = self.recorder.press(event.keysym)
        self._apply(outcome)

    def handle_key_release(self, event: tk.Event) -> None:
        if not self._capturing:
            return
        outcome = self.recorder.release(event.keysym)
        if outcome.combo:
            self._finish(outcome.combo)

    def _apply(self, outcome: RecordOutcome) -> None:
        if outcome.cancelled:
            self.cancel_capture()
        elif outcome.combo:
            self._finish(outcome.combo)
        elif outcome.error:
            self.hint.config(text=outcome.error, fg=_ERROR)
        elif outcome.preview:
            self.button.config(text=outcome.preview)


class SettingsWindow:
    """设置窗口。"""

    def __init__(self, root: tk.Tk, app: Any) -> None:
        self.root = root
        self.app = app
        self.win = tk.Toplevel(root)
        self.win.title("vtw 设置")
        self.win.configure(bg=_BG)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._captures: list[HotkeyCapture] = []
        self._build()
        self._load()
        self._center()

        # 键盘事件统一在窗口级处理，再转给正在录制的控件
        self.win.bind("<KeyPress>", self._on_key_press)
        self.win.bind("<KeyRelease>", self._on_key_release)
        self.win.focus_force()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 18}

        # ---- 快捷键 ----
        hotkey_box = tk.LabelFrame(
            self.win, text=" 快捷键 ", bg=_BG, fg=_FG, font=_FONT_BOLD,
            bd=1, relief="solid", labelanchor="nw",
        )
        hotkey_box.pack(fill="x", padx=16, pady=(14, 8))

        self.dictate_capture = self._add_capture_row(
            hotkey_box, "听写热键", "按住它说话，松开即出字", first=True
        )
        self.meeting_capture = self._add_capture_row(
            hotkey_box, "会议转写热键", "按一下开始 / 再按一下结束"
        )

        # ---- 听写模式 ----
        mode_box = tk.LabelFrame(
            self.win, text=" 听写模式 ", bg=_BG, fg=_FG, font=_FONT_BOLD,
            bd=1, relief="solid", labelanchor="nw",
        )
        mode_box.pack(fill="x", padx=16, pady=8)

        self.mode_var = tk.StringVar(value="push_to_talk")
        for value, label, tip in (
            ("push_to_talk", "按住说话", "按住热键期间录音，松手立刻识别"),
            ("toggle", "按一下开始 / 结束", "适合说长段落，手不用一直按着"),
        ):
            row = tk.Frame(mode_box, bg=_BG)
            row.pack(fill="x", padx=12, pady=(8, 0))
            tk.Radiobutton(
                row, text=label, value=value, variable=self.mode_var,
                bg=_BG, fg=_FG, selectcolor=_PANEL, activebackground=_BG,
                activeforeground=_FG, font=_FONT, highlightthickness=0, bd=0,
            ).pack(side="left")
            tk.Label(row, text=tip, bg=_BG, fg=_DIM, font=("Microsoft YaHei UI", 9)).pack(
                side="left", padx=(10, 0)
            )
        tk.Frame(mode_box, bg=_BG, height=10).pack()

        # ---- 会议音源 ----
        source_box = tk.LabelFrame(
            self.win, text=" 会议音源 ", bg=_BG, fg=_FG, font=_FONT_BOLD,
            bd=1, relief="solid", labelanchor="nw",
        )
        source_box.pack(fill="x", padx=16, pady=8)

        self.meeting_source_var = tk.StringVar(value="loopback")
        for value, label, tip in (
            ("loopback", "系统声音", "在线会议 / 视频：抓取电脑正在播放的声音"),
            ("mic", "麦克风", "线下会议：抓取房间里大家说话的声音"),
            ("both", "系统声音 + 麦克风", "戴耳机开在线会：同时录到对方和你自己"),
        ):
            row = tk.Frame(source_box, bg=_BG)
            row.pack(fill="x", padx=12, pady=(8, 0))
            tk.Radiobutton(
                row, text=label, value=value, variable=self.meeting_source_var,
                bg=_BG, fg=_FG, selectcolor=_PANEL, activebackground=_BG,
                activeforeground=_FG, font=_FONT, highlightthickness=0, bd=0,
            ).pack(side="left")
            tk.Label(row, text=tip, bg=_BG, fg=_DIM, font=("Microsoft YaHei UI", 9)).pack(
                side="left", padx=(10, 0)
            )

        tk.Label(
            source_box,
            text="外放时选「系统声音 + 麦克风」会把同一句话录两遍，建议戴耳机；"
                 "切换后下次开始会议转写时生效。",
            bg=_BG, fg=_DIM, font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
        ).pack(fill="x", padx=14, pady=(6, 10))

        # ---- 文本处理 ----
        text_box = tk.LabelFrame(
            self.win, text=" 文本处理 ", bg=_BG, fg=_FG, font=_FONT_BOLD,
            bd=1, relief="solid", labelanchor="nw",
        )
        text_box.pack(fill="x", padx=16, pady=8)

        self.spoken_punct_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            text_box,
            text="识别口述的标点（说「逗号」「句号」「换行」就插入对应符号）",
            variable=self.spoken_punct_var,
            bg=_BG, fg=_FG, selectcolor=_PANEL, activebackground=_BG,
            activeforeground=_FG, font=_FONT, highlightthickness=0, bd=0, anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 2))

        tk.Label(
            text_box,
            text="注意：中文没有词间空格，讨论标点本身时（如「这个逗号用法不对」）"
                 "也会被替换，\n遇到这种情况可以临时关掉这一项。",
            bg=_BG, fg=_DIM, font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
        ).pack(fill="x", padx=14, pady=(0, 10))

        # ---- 开机自启 ----
        boot_box = tk.LabelFrame(
            self.win, text=" 启动 ", bg=_BG, fg=_FG, font=_FONT_BOLD,
            bd=1, relief="solid", labelanchor="nw",
        )
        boot_box.pack(fill="x", padx=16, pady=8)

        self.autostart_var = tk.BooleanVar(value=False)
        self.autostart_check = tk.Checkbutton(
            boot_box, text="开机时自动启动（登录后静默运行，无控制台窗口）",
            variable=self.autostart_var,
            bg=_BG, fg=_FG, selectcolor=_PANEL, activebackground=_BG,
            activeforeground=_FG, font=_FONT, highlightthickness=0, bd=0, anchor="w",
        )
        self.autostart_check.pack(fill="x", padx=12, pady=(8, 2))

        self.autostart_detail = tk.Label(
            boot_box, text="", bg=_BG, fg=_DIM, font=("Consolas", 9), anchor="w", justify="left"
        )
        self.autostart_detail.pack(fill="x", padx=14, pady=(0, 10))

        # ---- 底部按钮 ----
        footer = tk.Frame(self.win, bg=_BG)
        footer.pack(fill="x", padx=16, pady=(6, 14))

        self.status = tk.Label(footer, text="", bg=_BG, fg=_DIM, font=("Microsoft YaHei UI", 9))
        self.status.pack(side="left")

        for text, command, primary in (
            ("保存", self.save, True),
            ("取消", self.close, False),
            ("恢复默认", self.restore_defaults, False),
        ):
            tk.Button(
                footer, text=text, command=command, font=_FONT,
                bg=_ACCENT if primary else _FIELD,
                fg="#12261c" if primary else _FG,
                activebackground="#6fe6b4" if primary else "#3a4354",
                activeforeground="#12261c" if primary else _FG,
                relief="flat", padx=18, pady=5, cursor="hand2",
            ).pack(side="right", padx=(8, 0))

    def _add_capture_row(
        self, parent: tk.Misc, title: str, tip: str, *, first: bool = False
    ) -> HotkeyCapture:
        row = tk.Frame(parent, bg=_BG)
        row.pack(fill="x", padx=12, pady=(12 if first else 4, 4))

        left = tk.Frame(row, bg=_BG)
        left.pack(side="left")
        tk.Label(left, text=title, bg=_BG, fg=_FG, font=_FONT, width=12, anchor="w").pack(
            anchor="w"
        )
        tk.Label(left, text=tip, bg=_BG, fg=_DIM, font=("Microsoft YaHei UI", 9), anchor="w").pack(
            anchor="w"
        )

        capture = HotkeyCapture(row, on_state_change=self._on_capture_state)
        capture.pack(side="left", padx=(16, 0))
        self._captures.append(capture)
        return capture

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def _load(self) -> None:
        cfg = self.app.config
        self.dictate_capture.set_value(str(cfg["hotkey_dictate"]))
        self.meeting_capture.set_value(str(cfg["hotkey_meeting_toggle"]))
        self.mode_var.set(str(cfg["mode"]))
        self.meeting_source_var.set(str(cfg["meeting_source"]))
        self.spoken_punct_var.set(bool(cfg["spoken_punctuation"]))
        self._refresh_autostart()

    def _refresh_autostart(self) -> None:
        enabled = autostart.is_enabled()
        self.autostart_var.set(enabled)
        if enabled:
            command = autostart.current_command() or ""
            if autostart.command_matches():
                self.autostart_detail.config(text=command, fg=_DIM)
            else:
                self.autostart_detail.config(
                    text=f"{command}\n（注意：此路径与当前安装不一致，保存时会自动修正）",
                    fg="#ffc44d",
                )
        else:
            self.autostart_detail.config(text="当前未开启", fg=_DIM)

    def restore_defaults(self) -> None:
        from .config import DEFAULTS

        self.dictate_capture.set_value(str(DEFAULTS["hotkey_dictate"]))
        self.meeting_capture.set_value(str(DEFAULTS["hotkey_meeting_toggle"]))
        self.mode_var.set(str(DEFAULTS["mode"]))
        self.meeting_source_var.set(str(DEFAULTS["meeting_source"]))
        self.spoken_punct_var.set(bool(DEFAULTS["spoken_punctuation"]))
        self.autostart_var.set(True)
        self.status.config(text="已填入默认值，点保存生效", fg=_DIM)

    # ------------------------------------------------------------------
    # 捕获状态：暂停/恢复全局热键
    # ------------------------------------------------------------------
    def _on_capture_state(self, capturing: bool) -> None:
        try:
            self.app.hotkeys.set_paused(capturing)
        except Exception:  # noqa: BLE001
            log.exception("切换热键暂停状态失败")
        if capturing:
            self.status.config(text="正在录制快捷键，按 Esc 取消", fg=_ACCENT)
        else:
            self.status.config(text="")

    def _on_key_press(self, event: tk.Event) -> None:
        for capture in self._captures:
            if capture.capturing:
                capture.handle_key_press(event)
                return "break"

    def _on_key_release(self, event: tk.Event) -> None:
        for capture in self._captures:
            if capture.capturing:
                capture.handle_key_release(event)
                return "break"

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def save(self) -> None:
        for capture in self._captures:
            capture.cancel_capture()  # 顺带恢复全局热键

        dictate = self.dictate_capture.value.strip()
        meeting = self.meeting_capture.value.strip()

        # ---- 校验热键 ----
        for label, combo in (("听写热键", dictate), ("会议转写热键", meeting)):
            ok, message = validate_combo(combo)
            if not ok:
                messagebox.showerror("热键无效", f"{label}：{message}", parent=self.win)
                return
            if parse_combo(combo) is None:
                messagebox.showerror(
                    "热键无效", f"{label}：{combo} 无法解析，请换一个组合。", parent=self.win
                )
                return

        if dictate == meeting:
            messagebox.showerror(
                "热键冲突",
                "听写热键与会议转写热键不能相同，请改成不同的组合。",
                parent=self.win,
            )
            return

        cfg = self.app.config
        hotkeys_changed = (
            dictate != str(cfg["hotkey_dictate"]) or meeting != str(cfg["hotkey_meeting_toggle"])
        )

        cfg.update(
            hotkey_dictate=dictate,
            hotkey_meeting_toggle=meeting,
            mode=self.mode_var.get(),
            meeting_source=self.meeting_source_var.get(),
            spoken_punctuation=bool(self.spoken_punct_var.get()),
        )
        cfg.save()

        # ---- 开机自启 ----
        want_autostart = bool(self.autostart_var.get())
        if want_autostart != autostart.is_enabled():
            ok, message = autostart.set_enabled(want_autostart)
            if not ok:
                messagebox.showerror("自启设置失败", message, parent=self.win)
                return
        elif want_autostart and not autostart.command_matches():
            autostart.enable()  # 目录变了，顺手修正

        # ---- 让热键立即生效，不必重启 ----
        if hotkeys_changed:
            try:
                self.app.apply_hotkeys()
            except Exception as exc:  # noqa: BLE001
                log.exception("重新注册热键失败")
                messagebox.showwarning(
                    "热键可能未生效",
                    f"配置已保存，但重新注册热键时出错：{exc}\n请重启程序。",
                    parent=self.win,
                )

        self.app.notify_settings_saved()
        self.close()

    def close(self) -> None:
        for capture in self._captures:
            capture.cancel_capture()
        try:
            self.app.hotkeys.set_paused(False)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    def _center(self) -> None:
        self.win.update_idletasks()
        width = self.win.winfo_reqwidth()
        height = self.win.winfo_reqheight()
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        x = (screen_w - width) // 2
        y = max(40, (screen_h - height) // 3)
        self.win.geometry(f"+{x}+{y}")

    def lift(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
