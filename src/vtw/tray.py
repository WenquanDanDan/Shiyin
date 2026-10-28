"""系统托盘图标与菜单。

图标用 Pillow **程序化绘制**，不依赖任何二进制资源文件（便于版本管理与搬迁）。
颜色即状态：灰＝空闲、红＝录音、黄＝识别中、绿＝会议转写中。

菜单动作通过回调字典交给 :mod:`vtw.app`，托盘层不直接碰业务逻辑。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

SIZE = 64

# 状态 → 主色
STATE_COLORS = {
    "idle": (150, 156, 166),
    "recording": (255, 92, 92),
    "recognizing": (255, 196, 77),
    "meeting": (87, 217, 163),
    "error": (214, 69, 69),
}

STATE_TITLES = {
    "idle": "vtw 语音转文字 — 空闲",
    "recording": "vtw 语音转文字 — 正在聆听",
    "recognizing": "vtw 语音转文字 — 识别中",
    "meeting": "vtw 语音转文字 — 会议转写中",
    "error": "vtw 语音转文字 — 需要处理",
}


def make_icon(state: str = "idle") -> Image.Image:
    """画一个"话筒"图标，颜色随状态变化。"""
    color = STATE_COLORS.get(state, STATE_COLORS["idle"])
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 外圈底色（深色圆），保证在任何主题的任务栏上都清晰
    draw.ellipse((2, 2, SIZE - 3, SIZE - 3), fill=(31, 36, 48, 255))

    # 话筒头（圆角矩形）
    mx0, my0, mx1, my1 = 24, 13, 40, 36
    draw.rounded_rectangle((mx0, my0, mx1, my1), radius=8, fill=color)

    # 话筒支架（U 形）
    draw.arc((17, 22, 47, 46), start=0, end=180, fill=color, width=4)
    # 立柱与底座
    draw.line((32, 46, 32, 52), fill=color, width=4)
    draw.line((24, 53, 40, 53), fill=color, width=4)
    return img


class TrayIcon:
    """托盘图标封装。所有回调都在其内部线程被调用，务必快速返回。"""

    def __init__(
        self,
        *,
        on_toggle_dictate: Callable[[], None],
        on_toggle_meeting: Callable[[], None],
        on_show_meeting_window: Callable[[], None],
        on_transcribe_file: Callable[[], None],
        on_show_file_window: Callable[[], None],
        on_show_history: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_open_config: Callable[[], None],
        on_open_log: Callable[[], None],
        on_open_output: Callable[[], None],
        on_reload: Callable[[], None],
        on_set_mode: Callable[[str], None],
        on_set_meeting_source: Callable[[str], None],
        on_about: Callable[[], None],
        on_quit: Callable[[], None],
        get_state: Callable[[], str],
        get_mode: Callable[[], str],
        get_meeting_source: Callable[[], str],
        get_meeting_running: Callable[[], bool],
    ) -> None:
        self._on_toggle_dictate = on_toggle_dictate
        self._on_toggle_meeting = on_toggle_meeting
        self._on_show_meeting_window = on_show_meeting_window
        self._on_transcribe_file = on_transcribe_file
        self._on_show_file_window = on_show_file_window
        self._on_show_history = on_show_history
        self._on_open_settings = on_open_settings
        self._on_open_config = on_open_config
        self._on_open_log = on_open_log
        self._on_open_output = on_open_output
        self._on_reload = on_reload
        self._on_set_mode = on_set_mode
        self._on_set_meeting_source = on_set_meeting_source
        self._on_about = on_about
        self._on_quit = on_quit
        self._get_state = get_state
        self._get_mode = get_mode
        self._get_meeting_source = get_meeting_source
        self._get_meeting_running = get_meeting_running

        self._icon = None
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> bool:
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001
            log.error("托盘不可用（pystray 导入失败）：%s", exc)
            return False

        menu = pystray.Menu(
            pystray.MenuItem(
                "开始/结束语音输入",
                lambda: self._safe(self._on_toggle_dictate),
                default=True,
            ),
            pystray.MenuItem(
                lambda item: "停止会议转写" if self._get_meeting_running() else "开始会议转写",
                lambda: self._safe(self._on_toggle_meeting),
            ),
            pystray.MenuItem("显示会议字幕窗口", lambda: self._safe(self._on_show_meeting_window)),
            pystray.MenuItem("转写音频文件…", lambda: self._safe(self._on_transcribe_file)),
            pystray.MenuItem("显示文件转写窗口", lambda: self._safe(self._on_show_file_window)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "听写模式",
                pystray.Menu(
                    pystray.MenuItem(
                        "按住说话",
                        lambda: self._safe(lambda: self._on_set_mode("push_to_talk")),
                        checked=lambda item: self._get_mode() == "push_to_talk",
                        radio=True,
                    ),
                    pystray.MenuItem(
                        "按一下开始/结束",
                        lambda: self._safe(lambda: self._on_set_mode("toggle")),
                        checked=lambda item: self._get_mode() == "toggle",
                        radio=True,
                    ),
                ),
            ),
            pystray.MenuItem(
                "会议音源",
                pystray.Menu(
                    self._meeting_source_item("loopback", "系统声音（在线会议）"),
                    self._meeting_source_item("mic", "麦克风（线下会议）"),
                    self._meeting_source_item("both", "系统声音 + 麦克风（戴耳机）"),
                ),
            ),
            pystray.MenuItem("重新载入配置", lambda: self._safe(self._on_reload)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置…", lambda: self._safe(self._on_open_settings), default=False),
            pystray.MenuItem("查看历史记录", lambda: self._safe(self._on_show_history)),
            pystray.MenuItem("打开输出目录", lambda: self._safe(self._on_open_output)),
            pystray.MenuItem("打开配置文件", lambda: self._safe(self._on_open_config)),
            pystray.MenuItem("打开日志", lambda: self._safe(self._on_open_log)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("关于", lambda: self._safe(self._on_about)),
            pystray.MenuItem("退出", lambda: self._safe(self._on_quit)),
        )

        try:
            self._icon = pystray.Icon(
                "vtw", make_icon("idle"), STATE_TITLES["idle"], menu
            )
        except Exception as exc:  # noqa: BLE001
            log.error("创建托盘图标失败：%s", exc)
            return False

        # run_detached 让托盘在自己线程里跑，不占用 Tk 主线程
        try:
            self._icon.run_detached()
        except Exception as exc:  # noqa: BLE001
            log.error("启动托盘失败：%s", exc)
            return False

        log.info("托盘图标已启动")
        return True

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("停止托盘失败：%s", exc)
            self._icon = None

    # -- 状态 -------------------------------------------------------------
    def set_state(self, state: str) -> None:
        with self._lock:
            if state == self._state:
                return
            self._state = state
        if self._icon is None:
            return
        try:
            self._icon.icon = make_icon(state)
            self._icon.title = STATE_TITLES.get(state, STATE_TITLES["idle"])
        except Exception as exc:  # noqa: BLE001
            log.debug("更新托盘状态失败：%s", exc)

    @property
    def state(self) -> str:
        return self._state

    def notify(self, message: str, title: str = "vtw 语音转文字") -> None:
        """气泡通知。失败不影响功能。"""
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception as exc:  # noqa: BLE001
            log.debug("托盘通知失败：%s", exc)

    # -- 内部 -------------------------------------------------------------
    def _meeting_source_item(self, kind: str, label: str) -> Any:
        """生成「会议音源」子菜单里的一个单选项目。"""
        import pystray

        def action() -> None:
            self._safe(lambda: self._on_set_meeting_source(kind))

        return pystray.MenuItem(
            label,
            action,
            checked=lambda item: self._get_meeting_source() == kind,
            radio=True,
        )

    def _safe(self, action: Callable[[], None]) -> None:
        """包一层异常保护：托盘菜单里的异常不能让整个程序挂掉。"""
        try:
            action()
        except Exception:  # noqa: BLE001
            log.exception("托盘菜单动作异常")
