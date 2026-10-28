"""全局热键。

需求是**对讲机式"按住说话"**：按下开始录音、松开立刻识别并注入。这要求热键能
被**抑制**（否则 ``Ctrl+Alt+Space`` 里的 Space 会往目标程序里插空格）。

后端选择
--------
首选 :class:`vtw.nativehook.NativeKeyboardHook` —— 直接调用
``SetWindowsHookExW(WH_KEYBOARD_LL)``，行为完全可控、零第三方依赖。

曾被考虑的 ``keyboard`` 库（0.13.5，2020 年后未再更新）在本机实测**不稳定**：
``add_hotkey(..., suppress=True)`` 第一次触发失败、第二次才成功，抑制行为依赖
库内部一套脆弱的按键状态机。因此它只作为原生钩子安装失败时的兜底。

**回调必须极快返回**：钩子过程在系统键盘链上同步执行，一旦阻塞会拖慢整机键盘
响应。真正的识别/注入请丢到工作线程 —— 见 :mod:`vtw.app`。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .nativehook import NativeKeyboardHook, parse_combo

log = logging.getLogger(__name__)

#: 官方文档里的修饰键写法
MODIFIER_NAMES = {
    "ctrl",
    "alt",
    "shift",
    "windows",
    "win",
    "cmd",
    "left ctrl",
    "right ctrl",
    "left alt",
    "right alt",
    "left shift",
    "right shift",
}

#: 推荐的备选组合：当默认组合被别的软件占用时提示用户改用
SUGGESTED_FALLBACKS = (
    "ctrl+alt+d",
    "ctrl+shift+space",
    "alt+`",
    "ctrl+alt+z",
)


@dataclass
class HotkeyBinding:
    """一次热键注册的结果。"""

    combo: str
    ok: bool = False
    error: str = ""
    backend: str = ""


@dataclass
class HotkeyManager:
    """管理全局热键的注册与注销。"""

    suppress: bool = True
    _bindings: list[HotkeyBinding] = field(default_factory=list)
    _native: NativeKeyboardHook | None = None
    _backend: str = ""
    #: 记录注册详情，供 keyboard 兜底后端在暂停后重新绑定
    _registrations: list[tuple[str, str, Callable[[], None], Callable[[], None] | None]] = field(
        default_factory=list
    )
    _paused: bool = False

    # -- 后端 -------------------------------------------------------------
    def _ensure_backend(self) -> str:
        """确保热键后端就绪，返回后端名（失败返回空串）。"""
        if self._backend:
            return self._backend

        native = NativeKeyboardHook()
        if native.start():
            self._native = native
            self._backend = "native"
            log.info("全局热键后端：原生 WH_KEYBOARD_LL 钩子")
            return self._backend

        log.warning("原生键盘钩子不可用，回退 keyboard 库")
        try:
            import keyboard  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            log.error("keyboard 库也不可用：%s", exc)
            return ""
        self._backend = "keyboard"
        log.info("全局热键后端：keyboard 库（兜底）")
        return self._backend

    # -- 注册 -------------------------------------------------------------
    def register_hold(
        self,
        combo: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> HotkeyBinding:
        """注册"按住触发 / 松开触发"的热键。"""
        binding = HotkeyBinding(combo=combo)
        backend = self._ensure_backend()
        binding.backend = backend

        if not backend:
            binding.error = "没有可用的全局热键后端"
            self._bindings.append(binding)
            return binding

        if backend == "native":
            if parse_combo(combo) is None:
                binding.error = f"无法解析热键写法：{combo}"
                self._bindings.append(binding)
                return binding
            assert self._native is not None
            ok = self._native.register(combo, on_press, on_release, suppress=self.suppress)
            binding.ok = ok
            if not ok:
                binding.error = "原生钩子注册失败"
            else:
                self._registrations.append(("hold", combo, on_press, on_release))
            self._bindings.append(binding)
            return binding

        # ---- keyboard 兜底 ----
        import keyboard

        try:
            keyboard.add_hotkey(combo, on_press, suppress=self.suppress)
            keyboard.add_hotkey(
                combo, on_release, suppress=self.suppress, trigger_on_release=True
            )
            binding.ok = True
        except Exception as exc:  # noqa: BLE001
            binding.error = f"注册失败：{exc}"
            log.error("热键 %s 注册失败：%s", combo, exc)
        if binding.ok:
            self._registrations.append(("hold", combo, on_press, on_release))
        self._bindings.append(binding)
        return binding

    def register_toggle(self, combo: str, on_toggle: Callable[[], None]) -> HotkeyBinding:
        """注册"按一下切换"的热键（用于会议转写开关）。"""
        binding = HotkeyBinding(combo=combo)
        backend = self._ensure_backend()
        binding.backend = backend

        if not backend:
            binding.error = "没有可用的全局热键后端"
            self._bindings.append(binding)
            return binding

        if backend == "native":
            if parse_combo(combo) is None:
                binding.error = f"无法解析热键写法：{combo}"
                self._bindings.append(binding)
                return binding
            assert self._native is not None
            ok = self._native.register(combo, on_toggle, None, suppress=self.suppress)
            binding.ok = ok
            if not ok:
                binding.error = "原生钩子注册失败"
            else:
                self._registrations.append(("toggle", combo, on_toggle, None))
            self._bindings.append(binding)
            return binding

        import keyboard

        try:
            keyboard.add_hotkey(combo, on_toggle, suppress=self.suppress)
            binding.ok = True
        except Exception as exc:  # noqa: BLE001
            binding.error = f"注册失败：{exc}"
            log.error("热键 %s 注册失败：%s", combo, exc)
        if binding.ok:
            self._registrations.append(("toggle", combo, on_toggle, None))
        self._bindings.append(binding)
        return binding

    # -- 注销 -------------------------------------------------------------
    def unregister_all(self) -> None:
        if self._native is not None:
            self._native.unregister_all()
        elif self._backend == "keyboard":
            try:
                import keyboard

                keyboard.unhook_all_hotkeys()
            except Exception as exc:  # noqa: BLE001
                log.warning("注销热键失败：%s", exc)
        self._bindings.clear()
        self._registrations.clear()

    def shutdown(self) -> None:
        """彻底关闭后端线程（退出程序时调用）。"""
        self.unregister_all()
        if self._native is not None:
            self._native.stop()
            self._native = None
        self._backend = ""

    @property
    def bindings(self) -> list[HotkeyBinding]:
        return list(self._bindings)

    @property
    def failed(self) -> list[HotkeyBinding]:
        return [b for b in self._bindings if not b.ok]

    @property
    def backend(self) -> str:
        return self._backend

    # -- 暂停 -------------------------------------------------------------
    @property
    def paused(self) -> bool:
        if self._native is not None:
            return self._native.paused
        return self._paused

    def set_paused(self, paused: bool) -> None:
        """暂停/恢复全局热键。

        设置界面捕获新快捷键期间必须暂停：否则用户按下的组合键会被自己的钩子
        吞掉，传不到捕获控件里，表现为"点进去按什么都没反应"。

        注意：暂停前要确保后端已经启动。早期实现在 ``_native`` 为空时直接返回，
        导致还没注册过任何热键时"暂停"静默失效 —— 这是被
        ``scripts/test_settings_gui.py`` 抓出来的真实缺陷。
        """
        if paused and not self._backend:
            self._ensure_backend()  # 先建好后端，否则暂停会被静默忽略

        if self._native is not None:
            self._native.set_paused(paused)
            self._paused = paused
            return

        if self._backend == "keyboard":
            self._set_keyboard_paused(paused)
        self._paused = paused

    def _set_keyboard_paused(self, paused: bool) -> None:
        """``keyboard`` 兜底后端没有"暂停"概念，只能整体解绑再重绑。"""
        import keyboard

        try:
            if paused:
                keyboard.unhook_all_hotkeys()
            else:
                for kind, combo, on_press, on_release in self._registrations:
                    if kind == "hold":
                        keyboard.add_hotkey(combo, on_press, suppress=self.suppress)
                        keyboard.add_hotkey(
                            combo, on_release, suppress=self.suppress, trigger_on_release=True
                        )
                    else:
                        keyboard.add_hotkey(combo, on_press, suppress=self.suppress)
        except Exception as exc:  # noqa: BLE001
            log.error("keyboard 后端暂停/恢复失败：%s", exc)


def validate_combo(combo: str) -> tuple[bool, str]:
    """校验组合键写法，给出中文提示。"""
    if not combo or not combo.strip():
        return False, "热键不能为空"
    if parse_combo(combo) is not None:
        return True, ""

    normalized = combo.strip().lower()
    parts = [p.strip() for p in normalized.split("+") if p.strip()]
    if not parts:
        return False, "热键格式应为 ctrl+alt+space 这样"
    if parts[-1] in MODIFIER_NAMES:
        return False, "热键最后一段必须是一个普通按键，例如 space、a、f2"
    return False, f"无法识别的按键：{parts[-1]}（可用：字母、数字、F1-F24、space、tab 等）"
