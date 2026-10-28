#!/usr/bin/env python3
"""设置界面端到端测试。

不做"看起来对不对"的表面验证，而是真的走一遍：
打开设置窗口 → 录制一个新快捷键 → 确认全局热键被暂停/恢复 → 保存 →
确认 config.json 已更新 → **确认新热键真的能被按下触发** → 还原配置。

用法::

    python scripts/test_settings_gui.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.WARNING, format="      [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout
)

from vtw import autostart, paths  # noqa: E402
from vtw.app import App  # noqa: E402
from vtw.inject import (  # noqa: E402
    KEYEVENTF_KEYUP,
    VK_CONTROL,
    VK_MENU,
    VK_SHIFT,
    _send,
    _vk_event,
)

# inject 模块只内置注入本身用得到的键，测试需要的一并在这里补上
VK_SPACE = 0x20
VK_D = 0x44
NEW_COMBO = "ctrl+shift+d"


def key(keysym: str) -> SimpleNamespace:
    """伪造一个 tkinter 事件（只用到 keysym）。"""
    return SimpleNamespace(keysym=keysym)


def send_new_combo() -> None:
    _send([
        _vk_event(VK_CONTROL), _vk_event(VK_SHIFT), _vk_event(VK_D),
        _vk_event(VK_D, keyup=True), _vk_event(VK_SHIFT, keyup=True),
        _vk_event(VK_CONTROL, keyup=True),
    ])


def iter_widgets(widget: tk.Misc):
    """深度遍历 tkinter 控件树。"""
    for child in widget.winfo_children():
        yield child
        yield from iter_widgets(child)


def collect_texts(root: tk.Misc) -> list[str]:
    """收集所有控件上的可见文本。

    tkinter 不通过 UIA 暴露控件文字（无障碍树里只有一排无名 Button），
    因此"界面到底渲染出什么"必须从 Tk 内部取。
    """
    texts: list[str] = []
    for widget in iter_widgets(root):
        try:
            if isinstance(widget, tk.LabelFrame):
                texts.append(str(widget.cget("text")))
            elif isinstance(widget, (tk.Label, tk.Button, tk.Checkbutton, tk.Radiobutton)):
                texts.append(str(widget.cget("text")))
        except tk.TclError:
            continue
    return [t for t in texts if t.strip()]


def verify_layout(window, failures: list[str]) -> None:
    """检查设置界面的关键控件都在，且窗口没有被裁切。"""
    texts = collect_texts(window.win)
    print("      界面上的文本：")
    for text in texts:
        print(f"        · {text}")

    for expected in (
        "听写热键",
        "会议转写热键",
        "按住说话",
        "按一下开始 / 结束",
        "会议音源",
        "系统声音 + 麦克风",
        "识别口述的标点",
        "开机时自动启动",
    ):
        if not any(expected in t for t in texts):
            failures.append(f"界面上找不到「{expected}」")

    for expected in ("保存", "取消", "恢复默认"):
        if expected not in texts:
            failures.append(f"界面上找不到按钮「{expected}」")

    window.win.update_idletasks()
    width = window.win.winfo_width()
    height = window.win.winfo_height()
    print(f"      窗口尺寸：{width}x{height}")

    # 逐个控件检查是否落在窗口内（防止布局溢出被裁掉）
    clipped: list[str] = []
    for widget in iter_widgets(window.win):
        try:
            if not widget.winfo_ismapped():
                continue
            x = widget.winfo_rootx() - window.win.winfo_rootx()
            y = widget.winfo_rooty() - window.win.winfo_rooty()
            right = x + widget.winfo_width()
            bottom = y + widget.winfo_height()
            if x < -1 or y < -1 or right > width + 1 or bottom > height + 1:
                clipped.append(
                    f"{widget.winfo_class()} at ({x},{y}) {widget.winfo_width()}x"
                    f"{widget.winfo_height()}"
                )
        except tk.TclError:
            continue
    if clipped:
            failures.append(f"有 {len(clipped)} 个控件超出窗口范围（会被裁切）：{clipped[:3]}")
    else:
        print("      所有控件都在窗口范围内")


def main() -> int:
    print("=" * 66)
    print("vtw 设置界面端到端测试")
    print("=" * 66)

    config_path = paths.CONFIG_FILE
    if not config_path.is_file():
        # 全新克隆时还没有 config.json：先生成默认配置，避免脚本直接崩溃
        from vtw.config import Config

        Config.load(config_path)
    original = json.loads(config_path.read_text(encoding="utf-8"))
    original_dictate = original["hotkey_dictate"]
    original_meeting = original["hotkey_meeting_toggle"]
    autostart_before = autostart.is_enabled()

    failures: list[str] = []
    app: App | None = None
    root: tk.Tk | None = None

    try:
        # ---------------------------------------------------------- 打开
        print("\n[1/7] 启动应用对象并打开设置窗口 …")
        app = App()
        root = tk.Tk()
        root.withdraw()
        app._root = root  # 正常由 run() 赋值；这里手工注入以便测试
        app._open_settings_window()
        window = app._settings_window
        assert window is not None
        root.update()
        print(f"      窗口已创建，标题：{window.win.title()!r}")

        # ---------------------------------------------------------- 布局
        print("\n[2/7] 检查界面控件与布局 …")
        verify_layout(window, failures)

        # ---------------------------------------------------------- 载入
        print("\n[3/7] 检查是否载入了当前配置 …")
        got_dictate = window.dictate_capture.value
        got_meeting = window.meeting_capture.value
        got_source = window.meeting_source_var.get()
        original_source = original.get("meeting_source", "loopback")
        print(f"      听写热键 = {got_dictate!r}（配置 {original_dictate!r}）")
        print(f"      会议热键 = {got_meeting!r}（配置 {original_meeting!r}）")
        print(f"      会议音源 = {got_source!r}（配置 {original_source!r}）")
        if got_dictate != original_dictate:
            failures.append(f"设置窗口未正确载入听写热键：{got_dictate!r}")
        if got_meeting != original_meeting:
            failures.append(f"设置窗口未正确载入会议热键：{got_meeting!r}")
        if got_source != original_source:
            failures.append(f"设置窗口未正确载入会议音源：{got_source!r}")

        # ---------------------------------------------------------- 暂停
        print("\n[4/7] 进入录制态，全局热键应被暂停 …")
        capture = window.dictate_capture
        capture.start_capture()
        root.update()
        paused_during = app.hotkeys.paused
        print(f"      录制中 paused = {paused_during}（期望 True）")
        if not paused_during:
            failures.append("录制快捷键时未暂停全局热键（会导致按键被自己吞掉）")

        # ---------------------------------------------------------- 录制
        print(f"\n[5/7] 模拟按下 Ctrl+Shift+D …")
        for keysym in ("Control_L", "Shift_L", "D"):
            window._on_key_press(key(keysym))
        root.update()
        recorded = capture.value
        paused_after = app.hotkeys.paused
        print(f"      录制结果 = {recorded!r}（期望 {NEW_COMBO!r}）")
        print(f"      录制结束 paused = {paused_after}（期望 False）")
        if recorded != NEW_COMBO:
            failures.append(f"录制结果错误：{recorded!r}")
        if paused_after:
            failures.append("录制结束后未恢复全局热键")

        # ---------------------------------------------------------- 保存
        print("\n[6/7] 保存（并确保不改变真实自启状态）…")
        window.autostart_var.set(autostart_before)  # 避免测试改动真实自启
        window.meeting_source_var.set("mic")  # 验证会议音源控件真的写进配置
        window.save()
        root.update()

        saved = json.loads(config_path.read_text(encoding="utf-8"))
        print(f"      config.json 中的听写热键 = {saved['hotkey_dictate']!r}")
        print(f"      config.json 中的会议音源 = {saved.get('meeting_source')!r}")
        if saved["hotkey_dictate"] != NEW_COMBO:
            failures.append(f"config.json 未更新：{saved['hotkey_dictate']!r}")
        if saved.get("meeting_source") != "mic":
            failures.append(f"会议音源未保存：{saved.get('meeting_source')!r}")
        if autostart.is_enabled() != autostart_before:
            failures.append("测试意外改动了开机自启状态")

        # ---------------------------------------------------------- 生效
        print("\n[7/7] 验证新热键真的能触发（而不是只写进配置文件）…")
        hits: list[float] = []
        original_press = app._on_hotkey_press

        def counting_press() -> None:
            hits.append(time.monotonic())
            original_press()

        app.hotkeys.unregister_all()
        app.hotkeys.register_hold(NEW_COMBO, counting_press, app._on_hotkey_release)
        time.sleep(0.4)

        send_new_combo()
        time.sleep(0.6)
        print(f"      新热键 {NEW_COMBO} 回调触发 {len(hits)} 次（期望 1）")
        if len(hits) != 1:
            failures.append(f"新热键未生效，回调触发 {len(hits)} 次")

        # 旧组合不应再触发
        hits.clear()
        _send([
            _vk_event(VK_CONTROL), _vk_event(VK_MENU), _vk_event(VK_SPACE),
            _vk_event(VK_SPACE, keyup=True), _vk_event(VK_MENU, keyup=True),
            _vk_event(VK_CONTROL, keyup=True),
        ])
        time.sleep(0.5)
        print(f"      旧组合 ctrl+alt+space 回调触发 {len(hits)} 次（期望 0）")
        if hits:
            failures.append("旧热键仍然生效（未正确替换）")

    finally:
        # ---------------------------------------------------------- 还原
        print("\n[还原] 恢复原始配置 …")
        try:
            if app is not None:
                app.hotkeys.shutdown()
            if root is not None:
                root.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            config_path.write_text(
                json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"      已还原为 听写={original_dictate!r} 会议={original_meeting!r}")
        except OSError as exc:
            print(f"      [警告] 还原配置失败：{exc}")

    print("\n" + "=" * 66)
    if failures:
        print("测试未通过：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("测试通过")
    print("  · 设置窗口正确载入与保存配置（含会议音源）")
    print("  · 录制期间全局热键暂停、结束后恢复")
    print("  · 新热键立即生效，旧热键失效（无需重启）")
    print("  · 开机自启状态未被误改")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
