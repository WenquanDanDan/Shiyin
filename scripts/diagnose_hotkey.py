#!/usr/bin/env python3
"""热键排障：确认全局键盘钩子到底有没有收到你的按键。

为什么需要这个脚本
------------------
"按了热键没反应"有三种完全不同的原因，光看主程序日志分不开：

1. 钩子根本没收到按键（权限/安全软件/钩子链被别的程序打断）；
2. 钩子收到了，但 ``vkCode`` 跟配置的热键对不上；
3. 收到了也对上了，但被"按住确认"（单修饰键需要按 0.3 秒）挡掉。

本脚本把 ``vkCode`` 原样打印出来，并按你的 ``config.json`` 注册同一个热键，
**按一下就知道卡在哪一层**。

用法::

    .\\.venv\\Scripts\\python.exe scripts\\diagnose_hotkey.py
    .\\.venv\\Scripts\\python.exe scripts\\diagnose_hotkey.py --seconds 20
    .\\.venv\\Scripts\\python.exe scripts\\diagnose_hotkey.py --combo ctrl+alt+z

也可以先退出正在运行的 vtw，避免两个钩子互相干扰（不退出通常也能测）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import paths  # noqa: E402
from vtw.config import Config  # noqa: E402
from vtw.nativehook import (  # noqa: E402
    SOLO_MODIFIER_HOLD_DELAY,
    NativeKeyboardHook,
    _vk_display,
    is_solo_modifier_combo,
    parse_combo,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vtw 热键排障")
    parser.add_argument("--seconds", type=float, default=15.0, help="监听时长（默认 15 秒）")
    parser.add_argument("--combo", default=None, help="要测试的热键（默认取配置里的听写热键）")
    args = parser.parse_args(argv)

    combo = args.combo
    if combo is None:
        try:
            combo = str(Config.load()["hotkey_dictate"])
        except Exception as exc:  # noqa: BLE001
            print(f"读取配置失败：{exc}")
            return 1

    parsed = parse_combo(combo)
    print("=" * 66)
    print("vtw 热键排障")
    print("=" * 66)
    print(f"配置热键   : {combo!r}")
    print(f"解析结果   : {parsed}")
    if parsed is None:
        print(f"[失败] 这个写法解析不了，请先在设置里改成 ctrl+alt+z 这类组合。")
        return 1
    if is_solo_modifier_combo(combo):
        print(
            f"注意       : 这是单个修饰键，**不抑制按键**，需要按住约 "
            f"{SOLO_MODIFIER_HOLD_DELAY:.1f} 秒才算触发；快速点按不会触发。"
        )
    print(f"监听时长   : {args.seconds:.0f} 秒")
    print("\n现在请按几下你的热键（单修饰键请按住不放 1 秒左右）…\n")

    seen: list[int] = []
    matched: list[float] = []
    released: list[float] = []

    hook = NativeKeyboardHook()
    hook.set_raw_listener(lambda vk: seen.append(vk))
    if not hook.start():
        print("[失败] 键盘钩子安装失败，说明本机安全策略/权限不允许全局钩子。")
        return 1

    hook.register(
        combo,
        lambda: matched.append(time.monotonic()),
        lambda: released.append(time.monotonic()),
        suppress=True,
    )

    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        print("\n（提前结束）")
    finally:
        hook.set_raw_listener(None)
        hook.stop()

    unique = sorted(set(seen))
    print("-" * 66)
    print(f"钩子收到的按键事件 : {len(seen)} 次，共 {len(unique)} 个不同键")
    if unique:
        print("出现过的主键       : " + "、".join(f"{_vk_display(v)}(0x{v:02X})" for v in unique[:24]))
    print(f"热键触发次数       : {len(matched)}（松开 {len(released)} 次）")
    print("-" * 66)

    if not seen:
        print("[结论] 钩子**一个按键都没收到**。")
        print("  · 说明不是热键写法问题；检查安全软件/杀毒是否拦截全局键盘钩子，")
        print("    或者当前会话是不是无交互桌面（远程/服务会话收不到本地键盘）。")
        return 2
    if not matched:
        print("[结论] 钩子能收到按键，但配置的热键没有被触发。")
        print(f"  · 期望的主键是 {parsed[1]:#04x}；如果你按的是单修饰键，")
        print("    请确认按住时间超过阈值再松开。")
        print("  · 可用 --combo 换一个组合再测，例如 --combo ctrl+alt+z")
        return 3
    print(f"[结论] 正常：热键 {combo} 触发了 {len(matched)} 次。")
    print("  · 若主程序里仍没录音，问题在热键之后的环节，看 logs\\vtw.log 的")
    print("    「麦克风已开始采集」/「启动录音失败」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
