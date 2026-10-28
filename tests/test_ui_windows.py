"""窗口层的冒烟测试。

背景（真实回归）：``MeetingWindow`` 曾把 ``pady=(0, 8)`` 传给 ``tk.Frame``。
控件的 ``padx/pady`` 选项只接受**单个**距离，双值元组是 ``pack``/``grid``
这类几何管理器的选项。于是构造窗口时抛 ``TclError: bad screen distance "0 8"``，
被 ``App._pump`` 的兜底 ``except`` 吞掉，表现为：按会议热键后日志显示"已开始"，
录音也在跑，但字幕窗口永远不出现 —— 会议转写等于不可用。

这类错误只有**真的把窗口建出来**才能发现，所以这里创建真实 Tk 窗口
（不进入 mainloop，用完立即销毁）。环境没有可用显示时整组跳过。
"""

from __future__ import annotations

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import ui_windows  # noqa: E402

_ROOT: tk.Tk | None = None


def setUpModule() -> None:  # noqa: N802 - unittest 约定的模块级钩子
    global _ROOT
    try:
        _ROOT = tk.Tk()
    except tk.TclError as exc:  # 无显示环境（如 CI）不是产品缺陷
        raise unittest.SkipTest(f"当前环境无法创建 Tk 窗口：{exc}") from exc
    _ROOT.withdraw()  # 测试期间不显示根窗口


def tearDownModule() -> None:  # noqa: N802
    global _ROOT
    if _ROOT is not None:
        try:
            _ROOT.destroy()
        except tk.TclError:
            pass
        _ROOT = None


class TestMeetingWindow(unittest.TestCase):
    """会议字幕窗口：能建出来、能显示字幕、按钮能回调。"""

    def make(self, on_stop=None, **kwargs) -> ui_windows.MeetingWindow:
        assert _ROOT is not None
        window = ui_windows.MeetingWindow(_ROOT, on_stop=on_stop, **kwargs)
        self.addCleanup(window.destroy)
        return window

    def test_window_can_be_constructed(self) -> None:
        """回归：构造阶段不得抛出 TclError。"""
        window = self.make()
        self.assertEqual(window.win.title(), "vtw 会议转写")
        self.assertEqual(window.win.state(), "withdrawn", "构造完成后不应自动弹窗")

    def test_custom_title_and_stop_text(self) -> None:
        """文件转写复用同一个窗口，但标题和按钮文字要能改。"""
        window = self.make(title="vtw 文件转写", stop_text="取消")
        self.assertEqual(window.win.title(), "vtw 文件转写")
        self.assertEqual(window.stop_button.cget("text"), "取消")

    def test_set_recording_accepts_custom_status(self) -> None:
        window = self.make()
        window.set_recording(False, status="已完成", color="#57d9a3")
        self.assertEqual(window.status.cget("text"), "已完成")
        self.assertEqual(window.status.cget("fg"), "#57d9a3")
        self.assertEqual(str(window.stop_button.cget("state")), "disabled")

    def test_append_renders_timestamp_and_text(self) -> None:
        window = self.make()
        window.append("大家好，今天天气不错。", 65.5)
        window.append("第二段", 0.25)
        content = window.text.get("1.0", "end").strip().splitlines()
        self.assertEqual(content, ["[01:05.50] 大家好，今天天气不错。", "[00:00.25] 第二段"])

    def test_clear_removes_all_lines(self) -> None:
        window = self.make()
        window.append("待清空", 1.0)
        window.clear()
        self.assertEqual(window.text.get("1.0", "end").strip(), "")

    def test_status_footer_and_recording_state(self) -> None:
        window = self.make()
        window.set_status("● 正在转写", color="#ff5c5c")
        window.set_footer("输出：output/会议.txt")
        window.set_recording(True)
        self.assertEqual(window.status.cget("text"), "● 正在转写")
        self.assertEqual(window.status.cget("fg"), "#ff5c5c")
        self.assertEqual(window.footer.cget("text"), "输出：output/会议.txt")
        self.assertEqual(str(window.stop_button.cget("state")), "normal")

        # 停止后按钮禁用、状态改成"已停止"
        window.set_recording(False)
        self.assertEqual(str(window.stop_button.cget("state")), "disabled")
        self.assertEqual(window.status.cget("text"), "已停止")

    def test_stop_button_invokes_callback(self) -> None:
        calls: list[int] = []
        window = self.make(on_stop=lambda: calls.append(1))
        window.stop_button.invoke()
        self.assertEqual(calls, [1])

    def test_show_and_hide_toggle_window_state(self) -> None:
        window = self.make()
        window.show()
        assert _ROOT is not None
        _ROOT.update()
        self.assertEqual(window.win.state(), "normal")
        window.hide()
        _ROOT.update()
        self.assertEqual(window.win.state(), "withdrawn")


class TestHistoryWindow(unittest.TestCase):
    """历史记录窗口。"""

    def test_renders_records(self) -> None:
        assert _ROOT is not None
        records = [
            {
                "time": "2026-10-23T15:00:00",
                "source": "mic",
                "text": "听写的内容",
                "duration": 1.25,
                "elapsed": 0.1,
            },
            {"time": "2026-10-23T15:05:00", "source": "meeting", "text": "会议的内容"},
        ]
        window = ui_windows.HistoryWindow(_ROOT, records)
        self.addCleanup(window.win.destroy)
        content = window.text.get("1.0", "end")
        self.assertIn("(听写)", content)
        self.assertIn("听写的内容", content)
        self.assertIn("(会议)", content)
        self.assertIn("会议的内容", content)
        self.assertIn("1.2s音频", content)

    def test_empty_state_has_hint(self) -> None:
        assert _ROOT is not None
        window = ui_windows.HistoryWindow(_ROOT, [])
        self.addCleanup(window.win.destroy)
        self.assertIn("还没有历史记录", window.text.get("1.0", "end"))


class TestAppCreatesMeetingWindow(unittest.TestCase):
    """复现出问题的那条路径：App 收到 meeting_started 时创建字幕窗口。"""

    def test_ensure_meeting_window_reuses_one_instance(self) -> None:
        from vtw.app import App

        with tempfile.TemporaryDirectory() as tmp:
            app = App(Path(tmp) / "config.json")
            assert _ROOT is not None
            app._root = _ROOT
            try:
                window = app._ensure_meeting_window()
                self.assertIsInstance(window, ui_windows.MeetingWindow)
                self.assertIs(app._ensure_meeting_window(), window, "应复用同一个窗口实例")
            finally:
                if app._meeting_window is not None:
                    app._meeting_window.destroy()
                    app._meeting_window = None


if __name__ == "__main__":
    unittest.main()
