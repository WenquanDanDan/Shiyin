"""设置界面逻辑的单元测试。

只覆盖**纯逻辑**部分（组合键录制状态机、自启命令构造），
不创建真实窗口、也不改动用户真实的自启注册表项。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import autostart, paths  # noqa: E402
from vtw.settings_window import (  # noqa: E402
    ComboRecorder,
    format_combo,
    keysym_to_key_name,
)


class TestKeysymMapping(unittest.TestCase):
    def test_letters_and_digits(self) -> None:
        self.assertEqual(keysym_to_key_name("a"), "a")
        self.assertEqual(keysym_to_key_name("Z"), "z")
        self.assertEqual(keysym_to_key_name("5"), "5")

    def test_named_keys(self) -> None:
        self.assertEqual(keysym_to_key_name("space"), "space")
        self.assertEqual(keysym_to_key_name("Return"), "enter")
        self.assertEqual(keysym_to_key_name("Prior"), "pageup")
        self.assertEqual(keysym_to_key_name("BackSpace"), "backspace")

    def test_function_keys(self) -> None:
        self.assertEqual(keysym_to_key_name("F2"), "f2")
        self.assertEqual(keysym_to_key_name("F12"), "f12")
        self.assertIsNone(keysym_to_key_name("F25"))

    def test_punctuation(self) -> None:
        self.assertEqual(keysym_to_key_name("grave"), "`")
        self.assertEqual(keysym_to_key_name("slash"), "/")

    def test_unsupported_returns_none(self) -> None:
        for keysym in ("Hyper_L", "XF86AudioMute", "Multi_key"):
            self.assertIsNone(keysym_to_key_name(keysym), keysym)


class TestFormatCombo(unittest.TestCase):
    def test_canonical_modifier_order(self) -> None:
        self.assertEqual(format_combo(["alt", "ctrl"], "space"), "ctrl+alt+space")

    def test_no_modifier(self) -> None:
        self.assertEqual(format_combo([], "f2"), "f2")

    def test_all_modifiers(self) -> None:
        self.assertEqual(
            format_combo(["win", "shift", "alt", "ctrl"], "a"), "ctrl+alt+shift+win+a"
        )


class TestComboRecorder(unittest.TestCase):
    """录制状态机 —— 这是设置界面里最容易出错的部分。"""

    def setUp(self) -> None:
        self.recorder = ComboRecorder()

    def test_simple_modifier_combo(self) -> None:
        self.recorder.press("Control_L")
        self.recorder.press("Alt_L")
        outcome = self.recorder.press("space")
        self.assertEqual(outcome.combo, "ctrl+alt+space")

    def test_modifier_order_is_normalized(self) -> None:
        # 先按 alt 再按 ctrl，结果仍应是 ctrl+alt 顺序
        self.recorder.press("Alt_L")
        self.recorder.press("Control_L")
        self.assertEqual(self.recorder.press("d").combo, "ctrl+alt+d")

    def test_plain_function_key(self) -> None:
        self.assertEqual(self.recorder.press("F2").combo, "f2")

    def test_escape_cancels(self) -> None:
        self.assertTrue(self.recorder.press("Escape").cancelled)

    def test_unsupported_key_reports_error(self) -> None:
        outcome = self.recorder.press("XF86AudioMute")
        self.assertIsNotNone(outcome.error)
        self.assertIsNone(outcome.combo)

    def test_preview_while_holding_modifiers(self) -> None:
        self.assertEqual(self.recorder.press("Control_L").preview, "ctrl+…")
        self.assertEqual(self.recorder.press("Alt_L").preview, "ctrl+alt+…")

    def test_single_modifier_key_on_release(self) -> None:
        """单独按下并松开右 Ctrl，应记成 right ctrl。"""
        self.recorder.press("Control_R")
        outcome = self.recorder.release("Control_R")
        self.assertEqual(outcome.combo, "right ctrl")

    def test_modifier_then_key_does_not_trigger_solo(self) -> None:
        """按下修饰键后若还按了别的键，松修饰键不应记成单键热键。"""
        self.recorder.press("Control_L")
        self.recorder.press("a")
        outcome = self.recorder.release("Control_L")
        self.assertIsNone(outcome.combo)

    def test_release_without_press_is_harmless(self) -> None:
        self.assertIsNone(self.recorder.release("Shift_L").combo)

    def test_reset_clears_state(self) -> None:
        self.recorder.press("Control_L")
        self.recorder.reset()
        # reset 后单独按 a，不应残留 ctrl
        self.assertEqual(self.recorder.press("a").combo, "a")

    def test_shift_combo(self) -> None:
        self.recorder.press("Shift_L")
        self.assertEqual(self.recorder.press("F4").combo, "shift+f4")

    def test_win_key_combo(self) -> None:
        self.recorder.press("Super_L")
        self.assertEqual(self.recorder.press("space").combo, "win+space")


class TestAutostartCommand(unittest.TestCase):
    """只测命令构造，**不写真实注册表**。"""

    def test_command_uses_pythonw_not_python(self) -> None:
        # 用 python.exe 会在登录时弹出黑色控制台窗口
        command = autostart.build_command().lower()
        self.assertIn("pythonw.exe", command)
        self.assertNotIn(r"\python.exe", command)

    def test_command_quotes_both_paths(self) -> None:
        command = autostart.build_command()
        # 两条路径都必须带引号，否则路径含空格时会启动失败
        self.assertEqual(command.count('"'), 4)

    def test_command_points_at_run_pyw(self) -> None:
        self.assertIn("run.pyw", autostart.build_command())

    def test_command_matches_current_install(self) -> None:
        """本机已注册自启、且自启项指向这份代码时才校验。

        开发机可能同时装着另一份 vtw；在全新克隆里跑测试也不应因为别的安装而失败。
        """
        if not autostart.is_enabled():
            self.skipTest("本机未注册开机自启")
        command = autostart.current_command() or ""
        if str(paths.PROJECT_ROOT) not in command:
            self.skipTest("自启项指向另一份安装")
        self.assertTrue(autostart.command_matches())
        self.assertEqual(autostart.current_command(), autostart.build_command())

    def test_value_name_is_stable(self) -> None:
        # 改名会让老用户的自启项变成孤儿，因此锁定这个常量
        self.assertEqual(autostart.VALUE_NAME, "VtwVoiceToText")

    def test_enable_rejects_missing_interpreter(self) -> None:
        original = autostart.pythonw_path
        try:
            autostart.pythonw_path = lambda: Path("definitely") / "missing.exe"  # type: ignore[assignment]
            ok, message = autostart.enable()
            self.assertFalse(ok)
            self.assertIn("找不到", message)
        finally:
            autostart.pythonw_path = original  # type: ignore[assignment]


class TestHotkeyPause(unittest.TestCase):
    """暂停全局热键 —— 设置界面捕获快捷键时依赖它。

    这里会短暂安装真实的系统钩子（行为上只是原样放行），测试结束会卸载。
    """

    def test_pause_starts_backend_even_when_never_registered(self) -> None:
        """回归测试：早期实现在 _native 为空时静默忽略暂停，导致按键被自己吞掉。"""
        from vtw.hotkey import HotkeyManager

        manager = HotkeyManager()
        self.assertFalse(manager._backend, "前置条件：后端尚未启动")

        try:
            manager.set_paused(True)
            self.assertTrue(manager.paused, "暂停必须真的生效，而不是被静默忽略")
            manager.set_paused(False)
            self.assertFalse(manager.paused, "恢复后应回到未暂停状态")
        finally:
            manager.shutdown()

    def test_pause_is_idempotent(self) -> None:
        from vtw.hotkey import HotkeyManager

        manager = HotkeyManager()
        try:
            manager.set_paused(True)
            manager.set_paused(True)
            self.assertTrue(manager.paused)
            manager.set_paused(False)
            manager.set_paused(False)
            self.assertFalse(manager.paused)
        finally:
            manager.shutdown()

    def test_unregister_clears_registration_records(self) -> None:
        from vtw.hotkey import HotkeyManager

        manager = HotkeyManager()
        try:
            manager.register_hold("ctrl+alt+space", lambda: None, lambda: None)
            self.assertEqual(len(manager._registrations), 1)
            manager.unregister_all()
            self.assertEqual(len(manager._registrations), 0)
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
