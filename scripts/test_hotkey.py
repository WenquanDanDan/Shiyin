#!/usr/bin/env python3
"""Phase 3 门禁：验证全局热键的**按键抑制**是否真的生效。

为什么必须测这一步
------------------
默认热键是 ``Ctrl+Alt+Space``。如果抑制失效，用户每次说话都会往目标程序里插入
一个空格 —— 这是最容易被忽略、又最影响体验的缺陷。

测试原理（不依赖窗口焦点，完全确定性）
--------------------------------------
Windows 的低级键盘钩子按**安装顺序的倒序**调用（后装的先调用）。
据此可以搭一个"探针"：

1. 先装一个 **监控钩子**，注册裸 ``space`` 热键（``suppress=False``），
   它只负责记录"空格键是否到达了这一层"。
2. 再装一个 **守卫钩子**，注册 ``ctrl+alt+space``（``suppress=True``），
   它会在监控钩子之前被调用。
3. 发送 ``Ctrl+Alt+Space``：
   * 若抑制生效 → 守卫吞掉空格，**监控钩子看不到空格**；
   * 若抑制失效 → 监控钩子看到空格。

再用裸 ``space`` 做对照：此时守卫不应拦截，监控必须看得到。
这样就排除了"钩子本身没工作"的伪阴性。

用法::

    python scripts/test_hotkey.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw.inject import KEYEVENTF_KEYUP, VK_CONTROL, VK_MENU, _vk_event, _send  # noqa: E402
from vtw.nativehook import NativeKeyboardHook, parse_combo  # noqa: E402

VK_SPACE = 0x20
COMBO = "ctrl+alt+space"


def send_ctrl_alt_space() -> None:
    _send([
        _vk_event(VK_CONTROL), _vk_event(VK_MENU), _vk_event(VK_SPACE),
        _vk_event(VK_SPACE, keyup=True), _vk_event(VK_MENU, keyup=True),
        _vk_event(VK_CONTROL, keyup=True),
    ])


def send_bare_space() -> None:
    _send([_vk_event(VK_SPACE), _vk_event(VK_SPACE, keyup=True)])


def main() -> int:
    print("=" * 64)
    print("vtw 全局热键抑制测试（原生 WH_KEYBOARD_LL）")
    print("=" * 64)

    print("\n[0/4] 解析热键写法")
    parsed = parse_combo(COMBO)
    if parsed is None:
        print(f"[失败] 无法解析 {COMBO}", file=sys.stderr)
        return 1
    modifiers, main_vk = parsed
    print(f"      {COMBO} -> 修饰键={modifiers} 主键=0x{main_vk:02X}")

    print("\n[1/4] 安装监控钩子（先装 → 后调用）")
    monitor = NativeKeyboardHook()
    if not monitor.start():
        print("[失败] 监控钩子安装失败", file=sys.stderr)
        return 1
    space_hits: list[float] = []
    monitor.register("space", lambda: space_hits.append(time.monotonic()), None, suppress=False)
    print("      已注册裸 space 探针（suppress=False）")

    print("\n[2/4] 安装守卫钩子（后装 → 先调用）")
    guard = NativeKeyboardHook()
    if not guard.start():
        print("[失败] 守卫钩子安装失败", file=sys.stderr)
        monitor.stop()
        return 1
    guard_press: list[float] = []
    guard_release: list[float] = []
    guard.register(
        COMBO, lambda: guard_press.append(time.monotonic()),
        lambda: guard_release.append(time.monotonic()), suppress=True,
    )
    print(f"      已注册 {COMBO}（suppress=True）")
    time.sleep(0.3)

    # ---- 对照：裸空格必须能被探针看到 ----
    print("\n[3/4] 对照测试：发送裸 space（应当被探针看到）")
    space_hits.clear()
    send_bare_space()
    time.sleep(0.4)
    control_hits = len(space_hits)
    print(f"      探针捕获空格次数 = {control_hits}（期望 ≥1）")
    if control_hits == 0:
        print("\n[无法自动判定] 连裸 space 都收不到，说明钩子链在本机不工作。")
        guard.stop()
        monitor.stop()
        return 2

    # ---- 实验：Ctrl+Alt+Space 必须被吞掉 ----
    print("\n[4/4] 实验测试：发送 Ctrl+Alt+Space（抑制生效则探针看不到空格）")
    space_hits.clear()
    guard_press.clear()
    guard_release.clear()
    for _ in range(3):
        send_ctrl_alt_space()
        time.sleep(0.3)
    time.sleep(0.4)

    leaked = len(space_hits)
    fired = len(guard_press)
    released = len(guard_release)

    print(f"      守卫按下回调 = {fired} 次（期望 3）")
    print(f"      守卫松开回调 = {released} 次（期望 3）")
    print(f"      探针看到空格 = {leaked} 次（期望 0，即被吞掉）")

    guard.stop()
    monitor.stop()

    print("\n" + "=" * 64)
    failures: list[str] = []
    if fired != 3:
        failures.append(f"守卫按下回调 {fired} 次，期望 3 次")
    if released != 3:
        failures.append(f"守卫松开回调 {released} 次，期望 3 次")
    if leaked != 0:
        failures.append(f"有 {leaked} 次空格泄漏到了下游钩子（抑制失效）")

    if failures:
        print("测试未通过：", file=sys.stderr)
        for item in failures:
            print(f"  ✗ {item}", file=sys.stderr)
        return 1

    print("测试通过 ✓")
    print("  · 按下/松开回调均精确触发（3/3）")
    print("  · 组合键里的 Space 被完全吞掉，不会往目标程序插入空格")
    print("  · 裸 Space 仍能正常通过（说明钩子没有误伤普通按键）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
