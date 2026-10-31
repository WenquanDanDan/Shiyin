"""原生键盘钩子的热键匹配测试（纯逻辑，不需要真实按键）。

回归背景
--------
把听写热键设成**单个 `ctrl`** 之后，按下完全没反应。

根因：``WH_KEYBOARD_LL`` 送来的 ``vkCode`` 是左/右键的具体码
``VK_LCONTROL(0xA2)`` / ``VK_RCONTROL(0xA3)``，**永远不会**是泛化的
``VK_CONTROL(0x11)``。旧实现只比较 0x11，真实按键永远匹配不上。

顺带解决副作用：单个修饰键若像普通单键那样抑制，Ctrl+C / Alt+Tab 会全部失效。
所以单修饰键改为"不抑制 + 按住约 0.3 秒才算触发"，普通快捷键的快速点按不受影响。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import vtw.nativehook as nh  # noqa: E402
from vtw.nativehook import (  # noqa: E402
    NativeKeyboardHook,
    is_solo_modifier_combo,
    parse_combo,
)

VK_LCONTROL, VK_RCONTROL, VK_CONTROL, VK_A = 0xA2, 0xA3, 0x11, 0x41


class TestSoloModifierParsing(unittest.TestCase):
    def test_solo_modifier_detected(self) -> None:
        for combo in ("ctrl", "left ctrl", "right alt", "shift", "left win"):
            self.assertTrue(is_solo_modifier_combo(combo), combo)

    def test_normal_combo_is_not_solo_modifier(self) -> None:
        for combo in ("ctrl+alt+space", "alt+z", "end", "left ctrl+a"):
            self.assertFalse(is_solo_modifier_combo(combo), combo)

    def test_parse_combo_signature_unchanged(self) -> None:
        # 其它模块依赖这个返回形状
        self.assertEqual(parse_combo("ctrl+alt+space"), (["ctrl", "alt"], 0x20))
        self.assertEqual(parse_combo("ctrl"), ([], VK_CONTROL))


class TestSoloModifierMatching(unittest.TestCase):
    """核心回归：左/右 Ctrl 都必须能匹配 `ctrl` 热键。"""

    def setUp(self) -> None:
        patcher = mock.patch.object(nh, "SOLO_MODIFIER_HOLD_DELAY", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.hook = NativeKeyboardHook()
        self.presses: list[str] = []
        self.releases: list[str] = []
        self.hook.register(
            "ctrl",
            lambda: self.presses.append("p"),
            lambda: self.releases.append("r"),
            suppress=True,  # 调用方要求抑制，但单修饰键必须被强制放行
        )

    def test_left_ctrl_matches(self) -> None:
        self.hook._handle_down(VK_LCONTROL)
        self.hook._handle_up(VK_LCONTROL)
        self.assertEqual(self.presses, ["p"], "左 Ctrl 没触发听写热键")
        self.assertEqual(self.releases, ["r"])

    def test_right_ctrl_matches(self) -> None:
        self.hook._handle_down(VK_RCONTROL)
        self.hook._handle_up(VK_RCONTROL)
        self.assertEqual(self.presses, ["p"], "右 Ctrl 没触发听写热键")

    def test_generic_vk_also_matches(self) -> None:
        self.hook._handle_down(VK_CONTROL)
        self.hook._handle_up(VK_CONTROL)
        self.assertEqual(self.presses, ["p"])

    def test_solo_modifier_is_never_swallowed(self) -> None:
        # 吞掉 Ctrl 会让 Ctrl+C / Ctrl+V / Alt+Tab 全部失效
        self.assertFalse(self.hook._handle_down(VK_LCONTROL))
        self.assertFalse(self.hook._handle_up(VK_LCONTROL))

    def test_auto_repeat_does_not_retrigger(self) -> None:
        self.hook._handle_down(VK_LCONTROL)
        self.hook._handle_down(VK_LCONTROL)
        self.hook._handle_down(VK_LCONTROL)
        self.assertEqual(self.presses, ["p"])

    def test_unrelated_key_does_not_match(self) -> None:
        self.assertFalse(self.hook._handle_down(VK_A))
        self.assertEqual(self.presses, [])


class TestSoloModifierHoldDelay(unittest.TestCase):
    """按住确认：普通 Ctrl+C 不该被当成一次听写。"""

    def test_quick_tap_does_not_fire(self) -> None:
        with mock.patch.object(nh, "SOLO_MODIFIER_HOLD_DELAY", 0.2):
            hook = NativeKeyboardHook()
            presses: list[int] = []
            releases: list[int] = []
            hook.register("ctrl", lambda: presses.append(1), lambda: releases.append(1))

            hook._handle_down(VK_LCONTROL)
            time.sleep(0.05)  # 比阈值短，模拟 Ctrl+C 的快速点按
            hook._handle_up(VK_LCONTROL)
            time.sleep(0.3)

        self.assertEqual(presses, [], "快速点按不应触发听写")
        self.assertEqual(releases, [])

    def test_hold_fires_press_then_release(self) -> None:
        with mock.patch.object(nh, "SOLO_MODIFIER_HOLD_DELAY", 0.1):
            hook = NativeKeyboardHook()
            presses: list[int] = []
            releases: list[int] = []
            hook.register("ctrl", lambda: presses.append(1), lambda: releases.append(1))

            hook._handle_down(VK_LCONTROL)
            self.assertEqual(presses, [], "按住未到阈值前不应触发")
            time.sleep(0.25)
            self.assertEqual(presses, [1], "按住超过阈值应触发")
            hook._handle_up(VK_LCONTROL)

        self.assertEqual(releases, [1], "松开应回调一次")


class TestNormalHotkeyUnchanged(unittest.TestCase):
    """普通组合键的行为不能因为这次改动而改变。"""

    def test_normal_key_matches_and_suppresses(self) -> None:
        hook = NativeKeyboardHook()
        presses: list[int] = []
        hook.register("alt+z", lambda: presses.append(1), None, suppress=True)

        with mock.patch.object(nh, "_is_down", return_value=True):
            swallowed = hook._handle_down(0x5A)
        self.assertTrue(swallowed, "普通组合键的主键仍然要抑制")
        self.assertEqual(presses, [1])

    def test_modifier_missing_means_no_match(self) -> None:
        hook = NativeKeyboardHook()
        presses: list[int] = []
        hook.register("alt+z", lambda: presses.append(1), None, suppress=True)

        with mock.patch.object(nh, "_is_down", return_value=False):
            self.assertFalse(hook._handle_down(0x5A))
        self.assertEqual(presses, [])


if __name__ == "__main__":
    unittest.main()
