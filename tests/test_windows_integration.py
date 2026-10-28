"""Windows 集成层测试：剪贴板、单实例、托盘图标、热键校验。

剪贴板测试会**临时改写系统剪贴板**，但会先快照、结束后恢复。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import winclip  # noqa: E402
from vtw.hotkey import validate_combo  # noqa: E402
from vtw.nativehook import parse_combo  # noqa: E402
from vtw.single import SingleInstance  # noqa: E402


class TestClipboard(unittest.TestCase):
    """剪贴板读写与恢复。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._original = winclip.snapshot()

    @classmethod
    def tearDownClass(cls) -> None:
        # 恢复用户原本的剪贴板文本，避免测试副作用
        if cls._original.had_text:
            winclip.set_text(cls._original.text or "")

    def test_set_and_get_roundtrip_ascii(self) -> None:
        self.assertTrue(winclip.set_text("hello clipboard"))
        self.assertEqual(winclip.get_text(), "hello clipboard")

    def test_set_and_get_roundtrip_chinese(self) -> None:
        sample = "今天天气不错，温度 25 度。"
        self.assertTrue(winclip.set_text(sample))
        self.assertEqual(winclip.get_text(), sample)

    def test_empty_string(self) -> None:
        self.assertTrue(winclip.set_text(""))
        self.assertEqual(winclip.get_text(), "")

    def test_multiline_and_special_chars(self) -> None:
        sample = "第一行\n第二行\ttab \"引号\" <尖括号> emoji🎤"
        self.assertTrue(winclip.set_text(sample))
        self.assertEqual(winclip.get_text(), sample)

    def test_snapshot_reports_text(self) -> None:
        winclip.set_text("snapshot probe")
        snap = winclip.snapshot()
        self.assertTrue(snap.had_text)
        self.assertEqual(snap.text, "snapshot probe")

    def test_snapshot_has_format_count(self) -> None:
        winclip.set_text("format probe")
        snap = winclip.snapshot()
        self.assertGreaterEqual(snap.other_formats, 0)


class TestSingleInstance(unittest.TestCase):
    def test_second_acquire_detects_existing(self) -> None:
        first = SingleInstance("Local\\VtwTest_SingleInstance_A")
        second = SingleInstance("Local\\VtwTest_SingleInstance_A")
        try:
            self.assertTrue(first.acquire(), "第一个实例应取得所有权")
            self.assertFalse(second.acquire(), "第二个实例应被识别为重复")
            self.assertTrue(second.already_running)
        finally:
            second.release()
            first.release()

    def test_release_allows_reacquire(self) -> None:
        name = "Local\\VtwTest_SingleInstance_B"
        first = SingleInstance(name)
        self.assertTrue(first.acquire())
        first.release()

        second = SingleInstance(name)
        try:
            self.assertTrue(second.acquire(), "释放后应能重新取得")
        finally:
            second.release()

    def test_context_manager(self) -> None:
        with SingleInstance("Local\\VtwTest_SingleInstance_C") as guard:
            self.assertFalse(guard.already_running)


class TestTrayIcon(unittest.TestCase):
    def test_all_states_render_valid_image(self) -> None:
        from vtw.tray import SIZE, make_icon

        for state in ("idle", "recording", "recognizing", "meeting", "error"):
            img = make_icon(state)
            self.assertEqual(img.size, (SIZE, SIZE))
            self.assertEqual(img.mode, "RGBA")

    def test_unknown_state_falls_back(self) -> None:
        from vtw.tray import make_icon

        self.assertEqual(make_icon("nonsense").size, make_icon("idle").size)

    def test_states_have_distinct_colors(self) -> None:
        from vtw.tray import make_icon

        idle = make_icon("idle").getpixel((32, 24))
        recording = make_icon("recording").getpixel((32, 24))
        self.assertNotEqual(idle, recording, "不同状态应有可区分的颜色")


class TestHotkeyValidation(unittest.TestCase):
    def test_accepts_common_combos(self) -> None:
        for combo in ("ctrl+alt+space", "ctrl+shift+d", "f2", "alt+`"):
            ok, message = validate_combo(combo)
            self.assertTrue(ok, f"{combo} 应被接受：{message}")

    def test_rejects_empty(self) -> None:
        self.assertFalse(validate_combo("")[0])
        self.assertFalse(validate_combo("   ")[0])

    def test_rejects_trailing_modifier(self) -> None:
        ok, message = validate_combo("ctrl+alt")
        self.assertFalse(ok)
        self.assertIn("普通按键", message)

    def test_accepts_single_modifier(self) -> None:
        # 单按右 Ctrl 是合理的"按住说话"键
        self.assertTrue(validate_combo("right ctrl")[0])

    def test_message_is_chinese(self) -> None:
        _, message = validate_combo("ctrl+alt")
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in message))

    def test_rejects_unknown_key(self) -> None:
        ok, message = validate_combo("ctrl+alt+nonsense")
        self.assertFalse(ok)
        self.assertTrue(message)


class TestComboParsing(unittest.TestCase):
    """热键字符串解析 —— 这是原生钩子的入口，解析错了热键就完全失效。"""

    def test_letters_and_modifiers(self) -> None:
        self.assertEqual(parse_combo("ctrl+alt+space"), (["ctrl", "alt"], 0x20))
        self.assertEqual(parse_combo("ctrl+shift+d"), (["ctrl", "shift"], 0x44))

    def test_case_insensitive(self) -> None:
        self.assertEqual(parse_combo("CTRL+ALT+SPACE"), (["ctrl", "alt"], 0x20))

    def test_digits_and_function_keys(self) -> None:
        self.assertEqual(parse_combo("f2"), ([], 0x71))
        self.assertEqual(parse_combo("ctrl+1"), (["ctrl"], 0x31))

    def test_solo_modifier_key(self) -> None:
        self.assertEqual(parse_combo("right ctrl"), ([], 0xA3))
        self.assertEqual(parse_combo("left alt"), ([], 0xA4))

    def test_punctuation_key(self) -> None:
        self.assertEqual(parse_combo("alt+`"), (["alt"], 0xC0))

    def test_no_modifier_plain_key(self) -> None:
        self.assertEqual(parse_combo("space"), ([], 0x20))

    def test_rejects_invalid(self) -> None:
        for combo in ("", "   ", "ctrl+alt", "bogus+nope", "ctrl+nonsense"):
            self.assertIsNone(parse_combo(combo), f"{combo!r} 应解析失败")

    def test_rejects_dangling_modifier(self) -> None:
        self.assertIsNone(parse_combo("ctrl+alt+"))


class TestNativeHookRegistration(unittest.TestCase):
    """只测注册逻辑，不实际安装系统钩子（避免测试环境副作用）。"""

    def test_register_rejects_bad_combo(self) -> None:
        from vtw.nativehook import NativeKeyboardHook

        hook = NativeKeyboardHook()
        self.assertFalse(hook.register("not+a+real+key", lambda: None))

    def test_register_accepts_valid_combo_without_start(self) -> None:
        from vtw.nativehook import NativeKeyboardHook

        hook = NativeKeyboardHook()
        self.assertTrue(hook.register("ctrl+alt+space", lambda: None, lambda: None))
        hook.unregister_all()
        self.assertEqual(len(hook._specs), 0)

    def test_duplicate_registration_replaces(self) -> None:
        from vtw.nativehook import NativeKeyboardHook

        hook = NativeKeyboardHook()
        hook.register("f2", lambda: None)
        hook.register("f2", lambda: None)
        self.assertEqual(len(hook._specs), 1, "同一组合重复注册应替换而不是叠加")

    def test_modifier_only_combo_rejected(self) -> None:
        from vtw.nativehook import NativeKeyboardHook

        hook = NativeKeyboardHook()
        self.assertFalse(hook.register("ctrl+alt", lambda: None))


if __name__ == "__main__":
    unittest.main()
