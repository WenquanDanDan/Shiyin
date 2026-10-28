"""应用装配与生命周期。

线程模型
--------
============  ==========================================================
线程           职责
============  ==========================================================
主线程         ``tk.Tk().mainloop()`` —— 独占浮条、会议窗口、历史窗口
工作线程        音频采集停止 / 识别 / 注入（耗时操作）
热键线程        ``keyboard`` 的监听线程；回调只做"入队"，立刻返回
托盘线程        ``pystray.run_detached()``
============  ==========================================================

跨线程一律通过两个队列：``_ui_queue``（→ Tk 主线程）与 ``_jobs``（→ 工作线程）。
热键回调**绝不能**阻塞，否则会卡住全局键盘处理。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Any

import numpy as np

from . import (
    __version__,
    file_transcribe,
    history as history_mod,
    inject,
    paths,
    settings_window,
    tray as tray_mod,
    ui_windows,
)
from .asr import ModelNotReadyError, SenseVoiceEngine, verify_model_files
from .audio_source import (
    MEETING_SOURCE_KINDS,
    MEETING_SOURCE_LABELS,
    AudioSource,
    AudioSourceError,
    MicSource,
)
from .config import Config
from .hotkey import HotkeyManager
from .meeting import MeetingSession
from .overlay import Overlay
from .textutil import is_meaningful
from .vad import trim_silence

log = logging.getLogger(__name__)

#: UI 泵的轮询间隔（毫秒）
_UI_POLL_MS = 50


class App:
    """vtw 主应用。"""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config = Config.load(config_path)
        self.history = history_mod.History(enabled=bool(self.config["history_enabled"]))
        self.hotkeys = HotkeyManager()

        self.engine = self._make_engine()
        self.tray: tray_mod.TrayIcon | None = None
        self.overlay: Overlay | None = None
        self.meeting: MeetingSession | None = None
        self.file: file_transcribe.FileTranscriber | None = None

        self._root: tk.Tk | None = None
        self._meeting_window: ui_windows.MeetingWindow | None = None
        self._file_window: ui_windows.MeetingWindow | None = None
        self._settings_window: settings_window.SettingsWindow | None = None
        self._ui_queue: queue.Queue[tuple] = queue.Queue()
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._worker: threading.Thread | None = None

        self._dictating = False
        self._source: AudioSource | None = None
        self._dictate_started = 0.0
        self._state = "idle"
        self._warned_self_capture = False
        self._quitting = False

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    def _make_engine(self) -> SenseVoiceEngine:
        return SenseVoiceEngine(
            num_threads=int(self.config["num_threads"]),
            language=str(self.config["language"]),
            use_itn=bool(self.config["use_itn"]),
            idle_unload_seconds=int(self.config["idle_unload_seconds"]),
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self) -> int:
        """启动应用并进入事件循环，直到用户退出。"""
        self._root = tk.Tk()
        self._root.withdraw()  # 只需要一个隐藏的 Tk 根窗口托管各 Toplevel

        self.overlay = Overlay(
            self._root,
            position=str(self.config["overlay_position"]),
            enabled=bool(self.config["overlay_enabled"]),
        )

        self._start_worker()
        self._start_tray()
        self._register_hotkeys()
        self._schedule_warmup()
        self._check_models_at_startup()

        self._root.after(_UI_POLL_MS, self._pump)
        log.info("vtw 已启动（v%s）", __version__)
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C，正在退出")
        finally:
            self._shutdown()
        return 0

    def quit(self) -> None:
        """请求退出（可从任意线程调用）。"""
        if self._quitting:
            return
        self._quitting = True
        root = self._root
        if root is not None:
            try:
                root.after(0, root.quit)
            except Exception:  # noqa: BLE001
                pass

    def _shutdown(self) -> None:
        log.info("正在退出…")
        if self._dictating:
            self._do_stop_dictation()
        if self.meeting is not None and self.meeting.running:
            try:
                self.meeting.stop()
            except Exception:  # noqa: BLE001
                log.exception("停止会议转写失败")
        transcriber = self.file
        if transcriber is not None and transcriber.running:
            try:
                transcriber.cancel()
                transcriber.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                log.exception("停止文件转写失败")
        self.hotkeys.unregister_all()
        if self.tray is not None:
            self.tray.stop()
        self.hotkeys.shutdown()
        if self.overlay is not None:
            self.overlay.destroy()
        self._jobs.put(None)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout=3.0)
        log.info("已退出")

    # ------------------------------------------------------------------
    # 启动子步骤
    # ------------------------------------------------------------------
    def _start_worker(self) -> None:
        def loop() -> None:
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                try:
                    job()
                except Exception:  # noqa: BLE001 - 工作线程异常必须记录
                    log.exception("工作线程任务异常")

        self._worker = threading.Thread(target=loop, name="vtw-worker", daemon=True)
        self._worker.start()

    def _start_tray(self) -> None:
        self.tray = tray_mod.TrayIcon(
            on_toggle_dictate=self.toggle_dictate,
            on_toggle_meeting=self.toggle_meeting,
            on_show_meeting_window=self.show_meeting_window,
            on_transcribe_file=self.transcribe_file,
            on_show_file_window=self.show_file_window,
            on_show_history=self.show_history,
            on_open_settings=self.open_settings,
            on_open_config=lambda: self._open_path(self.config.path),
            on_open_log=lambda: self._open_path(paths.LOG_FILE),
            on_open_output=lambda: self._open_path(paths.resolve_output_dir(self.config["output_dir"])),
            on_reload=self.reload_config,
            on_set_mode=self.set_mode,
            on_set_meeting_source=self.set_meeting_source,
            on_about=self.show_about,
            on_quit=self.quit,
            get_state=lambda: self._state,
            get_mode=lambda: str(self.config["mode"]),
            get_meeting_source=lambda: str(self.config["meeting_source"]),
            get_meeting_running=lambda: bool(self.meeting and self.meeting.running),
        )
        if not self.tray.start():
            log.error("托盘启动失败，程序仍会运行但无法通过托盘操作")

    def _register_hotkeys(self) -> None:
        self.hotkeys.unregister_all()

        hold_combo = str(self.config["hotkey_dictate"])
        binding = self.hotkeys.register_hold(
            hold_combo, self._on_hotkey_press, self._on_hotkey_release
        )
        if not binding.ok:
            self._notify(
                f"热键 {hold_combo} 注册失败：{binding.error}\n"
                "请在「设置」里改成别的组合（例如 ctrl+alt+d）。",
                error=True,
            )

        meeting_combo = str(self.config["hotkey_meeting_toggle"])
        meeting_binding = self.hotkeys.register_toggle(meeting_combo, self.toggle_meeting)
        if not meeting_binding.ok:
            self._notify(
                f"会议热键 {meeting_combo} 注册失败：{meeting_binding.error}", error=True
            )

    def apply_hotkeys(self) -> None:
        """按当前配置重新注册热键，使其立即生效（无需重启）。"""
        self._register_hotkeys()
        log.info(
            "热键已重新注册：听写=%s 会议=%s",
            self.config["hotkey_dictate"],
            self.config["hotkey_meeting_toggle"],
        )

    def _schedule_warmup(self) -> None:
        """延迟预热模型：避免拖慢登录，又让首次呼出无需等待。"""
        if not bool(self.config["preload_model"]):
            log.info("已关闭模型预热，首次识别会稍慢")
            return
        delay_ms = int(float(self.config["warmup_delay_seconds"]) * 1000)

        def warm() -> None:
            log.info("开始后台预热模型…")
            if self.engine.warmup():
                log.info("模型预热完成")
                self._notify("已就绪，按住热键即可说话", title="vtw 已就绪")
                self._ui_queue.put(("state", "idle"))
            else:
                self._ui_queue.put(("state", "error"))

        if self._root is not None:
            self._root.after(delay_ms, lambda: threading.Thread(
                target=warm, name="warmup", daemon=True
            ).start())

    def _check_models_at_startup(self) -> None:
        ok, message = verify_model_files()
        if ok:
            return
        log.error("模型未就绪：%s", message)
        self._notify(message, error=True)
        self._ui_queue.put(("state", "error"))

    # ------------------------------------------------------------------
    # 热键回调（必须极快返回）
    # ------------------------------------------------------------------
    def _on_hotkey_press(self) -> None:
        mode = str(self.config["mode"])
        if mode == "toggle":
            self._jobs.put(self.toggle_dictate)
        else:
            self._jobs.put(self._do_start_dictation)

    def _on_hotkey_release(self) -> None:
        if str(self.config["mode"]) == "push_to_talk":
            self._jobs.put(self._do_stop_dictation)

    # ------------------------------------------------------------------
    # 听写
    # ------------------------------------------------------------------
    def toggle_dictate(self) -> None:
        """切换听写状态（托盘菜单 / 切换模式用）。"""
        if self._dictating:
            self._jobs.put(self._do_stop_dictation)
        else:
            self._jobs.put(self._do_start_dictation)

    def _do_start_dictation(self) -> None:
        if self._dictating:
            return

        source = MicSource(
            device=self.config["input_device"], sample_rate=int(self.config["sample_rate"])
        )
        try:
            source.start()
        except AudioSourceError as exc:
            log.error("麦克风启动失败：%s", exc)
            self._notify(str(exc), error=True)
            self._ui_queue.put(("message", "麦克风不可用", True))
            return

        if self.meeting is not None and self.meeting.running and not self._warned_self_capture:
            self._warned_self_capture = True
            self._notify(
                "会议转写正在进行。此时对着麦克风说话，可能被系统声音环回重复采集一次。"
            )

        self._source = source
        self._dictating = True
        self._dictate_started = time.monotonic()
        self._set_state("recording")
        self._ui_queue.put(("overlay_recording",))

    def _do_stop_dictation(self) -> None:
        if not self._dictating:
            return
        source, self._source = self._source, None
        self._dictating = False

        audio = source.stop() if source is not None else None
        if audio is None or audio.size == 0:
            self._set_state("idle")
            self._ui_queue.put(("overlay_hide",))
            return

        rate = int(self.config["sample_rate"])
        duration = audio.size / float(rate)
        min_seconds = float(self.config["min_dictation_seconds"])
        peak = float(np.abs(audio).max())

        if duration < min_seconds:
            log.info("录音过短（%.2fs < %.2fs），忽略", duration, min_seconds)
            self._set_state("idle")
            self._ui_queue.put(("message", "录音太短", True))
            return

        if peak < 0.001:
            # 麦克风基本没信号：继续识别只会得到随机的一两个字，还会污染历史
            log.warning("麦克风信号极低（峰值 %.5f），放弃识别", peak)
            self._set_state("idle")
            self._notify(
                "没有听到声音：麦克风信号几乎为零。\n"
                "请检查麦克风是否被静音，或在系统声音设置里换一个输入设备。",
                error=True,
            )
            self._ui_queue.put(("message", "没有听到声音", True))
            return

        # 裁掉首尾静音，能明显减少短音频上的空识别
        try:
            trimmed = trim_silence(audio, sample_rate=rate)
            if trimmed.size > rate * 0.2:
                audio = trimmed
        except Exception as exc:  # noqa: BLE001 - 裁剪失败不影响主流程
            log.debug("静音裁剪失败：%s", exc)

        log.info(
            "听写音频：原始 %.2fs（峰值 %.4f），裁剪后 %.2fs",
            duration,
            peak,
            audio.size / float(rate),
        )

        self._set_state("recognizing")
        self._ui_queue.put(("overlay_recognizing",))

        try:
            result = self.engine.transcribe(
                audio,
                rate,
                lowercase=bool(self.config["lowercase_output"]),
                trailing_space=bool(self.config["trailing_space"]),
                english_sentence_case=bool(self.config["english_sentence_case"]),
                spoken_punctuation=bool(self.config["spoken_punctuation"]),
            )
        except ModelNotReadyError as exc:
            log.error("识别失败：%s", exc)
            self._notify(str(exc), error=True)
            self._set_state("error")
            self._ui_queue.put(("overlay_hide",))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("识别异常")
            self._notify(f"识别出错：{exc}", error=True)
            self._set_state("idle")
            self._ui_queue.put(("overlay_hide",))
            return

        text = result.text
        log.info(
            "听写结果：%.2fs 音频 / %.3fs 推理 / RTF=%.3f / %d 字",
            result.duration,
            result.elapsed,
            result.rtf,
            len(text),
        )

        if not is_meaningful(text):
            self._set_state("idle")
            self._ui_queue.put(("message", "没有识别到内容", False))
            return

        # 注入先于落盘：inject_text 内部已做完整异常保护，不会抛出，
        # 因此可以拿到真实的 injected 结果，避免写两条重复记录。
        injected = False
        if self._user_is_typing_target():
            injected = inject.inject_text(
                text,
                method=str(self.config["inject_method"]),
                restore_clipboard=bool(self.config["restore_clipboard"]),
                restore_delay_ms=int(self.config["clipboard_restore_delay_ms"]),
            )
            if not injected:
                log.error("文本注入失败，内容已保存在历史记录中")
                self._notify("文字注入失败，内容已保存到历史记录")

        self.history.append(
            text,
            source="mic",
            duration=result.duration,
            elapsed=result.elapsed,
            injected=injected,
            extra={"lang": result.lang},
        )

        self._set_state("idle")
        self._ui_queue.put(("overlay_text", text, True))

    def _user_is_typing_target(self) -> bool:
        """注入前的自检：至少确认前台窗口不是我们自己。"""
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid():
                log.warning("前台窗口属于 vtw 自身，跳过注入以免文字打到自己的界面上")
                return False
        except Exception as exc:  # noqa: BLE001
            log.debug("前台窗口检查失败：%s", exc)
        return True

    # ------------------------------------------------------------------
    # 会议转写
    # ------------------------------------------------------------------
    def toggle_meeting(self) -> None:
        if self.meeting is not None and self.meeting.running:
            self._jobs.put(self._do_stop_meeting)
        else:
            self._jobs.put(self._do_start_meeting)

    def _do_start_meeting(self) -> None:
        if self.meeting is not None and self.meeting.running:
            return

        session = MeetingSession(
            self.engine,
            sample_rate=int(self.config["sample_rate"]),
            source_kind=str(self.config["meeting_source"]),
            loopback_device=self.config["loopback_device"],
            mic_device=self.config["input_device"],
            output_dir=paths.resolve_output_dir(self.config["output_dir"]),
            save_audio=bool(self.config["save_meeting_audio"]),
            on_segment=self._on_meeting_segment,
            on_error=self._on_meeting_error,
        )
        session.set_vad_options(dict(self.config["vad"] or {}))

        if not session.start():
            return

        self.meeting = session
        self._set_state("meeting")
        self._ui_queue.put(
            ("meeting_started", str(session.transcript_path), session.source_label)
        )

    def _do_stop_meeting(self) -> None:
        session, self.meeting = self.meeting, None
        if session is None:
            return
        path = session.stop()
        self._set_state("idle")
        self._ui_queue.put(("meeting_stopped", str(path) if path else ""))

        lines: list[str] = []
        if path:
            lines.append(f"文字稿：{path}")
        if session.segment_count == 0:
            lines.append(
                {
                    "mic": "没有识别到内容，请确认麦克风正在采集声音。",
                    "both": "没有识别到内容，请确认系统声音或麦克风有输入。",
                }.get(session.source_kind, "没有识别到内容，请确认系统正在播放声音。")
            )
        self._notify("\n".join(lines) if lines else "会议转写已停止")

    def _on_meeting_segment(self, text: str, offset: float) -> None:
        """VAD 线程回调：只入队，UI 主线程负责渲染。"""
        self._ui_queue.put(("meeting_segment", text, offset))
        self.history.append(
            text,
            source="meeting",
            extra={"offset": round(offset, 2)},
        )

    def _on_meeting_error(self, message: str) -> None:
        self._notify(f"会议转写失败：{message}", error=True)
        self._ui_queue.put(("meeting_error", message))
        if self.meeting is not None:
            self.meeting = None
        self._set_state("error")

    # ------------------------------------------------------------------
    # 文件转写
    # ------------------------------------------------------------------
    def transcribe_file(self) -> None:
        """托盘入口：选择一个音频文件开始转写。"""
        self._ui_queue.put(("pick_audio_file",))

    def show_file_window(self) -> None:
        self._ui_queue.put(("show_file_window",))

    def _pick_audio_file(self) -> None:
        if self._root is None:
            return
        if self.file is not None and self.file.running:
            self._notify("已有一个文件正在转写，请等它结束或在窗口里取消。")
            return
        path = filedialog.askopenfilename(
            parent=self._root,
            title="选择要转写的音频文件",
            filetypes=file_transcribe.AUDIO_FILE_FILETYPES,
        )
        if not path:
            return
        self._start_file_transcription(Path(path))

    def _start_file_transcription(self, source: Path) -> None:
        if self.file is not None and self.file.running:
            self._notify("已有一个文件正在转写，请等它结束或在窗口里取消。")
            return
        if not source.is_file():
            self._notify(f"文件不存在：{source}", error=True)
            return

        window = self._ensure_file_window()
        window.clear()
        window.set_status("● 正在转写 0%")
        window.set_footer(f"文件：{source.name}")
        window.set_recording(True)
        window.show()

        transcriber = file_transcribe.FileTranscriber(
            self.engine,
            sample_rate=int(self.config["sample_rate"]),
            vad_options=dict(self.config["vad"] or {}),
            transcribe_options={
                "lowercase": bool(self.config["lowercase_output"]),
                "trailing_space": bool(self.config["trailing_space"]),
                "english_sentence_case": bool(self.config["english_sentence_case"]),
                "spoken_punctuation": bool(self.config["spoken_punctuation"]),
            },
            output_dir=paths.resolve_output_dir(self.config["output_dir"]),
            on_segment=lambda text, offset: self._ui_queue.put(
                ("file_segment", text, offset)
            ),
            on_progress=lambda done, total, count: self._ui_queue.put(
                ("file_progress", done, total, count)
            ),
            on_finished=lambda path, count, duration, cancelled: self._ui_queue.put(
                ("file_finished", str(path), count, duration, cancelled)
            ),
            on_error=lambda message: self._ui_queue.put(("file_error", message)),
        )
        if not transcriber.start(source):
            window.set_recording(False, status="无法开始", color="#ff5c5c")
            return
        self.file = transcriber
        self._notify(f"开始转写：{source.name}")

    def cancel_file_transcription(self) -> None:
        transcriber = self.file
        if transcriber is None or not transcriber.running:
            return
        transcriber.cancel()
        if self._file_window is not None:
            self._file_window.set_status("正在取消…", color="#ffc44d")
        log.info("用户取消了文件转写")

    def _ensure_file_window(self) -> ui_windows.MeetingWindow:
        if self._file_window is None:
            assert self._root is not None
            self._file_window = ui_windows.MeetingWindow(
                self._root,
                on_stop=self.cancel_file_transcription,
                title="vtw 文件转写",
                stop_text="取消",
            )
        return self._file_window

    def _update_file_progress(self, done: float, total: float, count: int) -> None:
        window = self._file_window
        if window is None:
            return
        if self.file is not None and self.file.cancelled:
            return  # 取消后保持“正在取消…”，不要被迟到的进度覆盖
        if total > 0:
            window.set_status(f"● 正在转写 {min(100, int(done / total * 100))}%")
        else:
            window.set_status(f"● 正在转写 {done:.0f}s")

        name = "音频文件"
        if self.file is not None and self.file.source_path is not None:
            name = self.file.source_path.name
        if len(name) > 28:
            name = name[:27] + "…"
        if total > 0:
            window.set_footer(f"{name} · {done:.0f}/{total:.0f}s · 已转写 {count} 段")
        else:
            window.set_footer(f"{name} · 已处理 {done:.0f}s · 已转写 {count} 段")

    # ------------------------------------------------------------------
    # 界面动作（路由到 Tk 主线程）
    # ------------------------------------------------------------------
    def show_meeting_window(self) -> None:
        self._ui_queue.put(("show_meeting_window",))

    def show_history(self) -> None:
        records = self.history.tail(50)
        self._ui_queue.put(("show_history", records))

    def open_settings(self) -> None:
        """打开设置窗口（路由到 Tk 主线程）。"""
        self._ui_queue.put(("show_settings",))

    def notify_settings_saved(self) -> None:
        self._notify(
            f"设置已保存。听写热键：{self.config['hotkey_dictate']}，"
            f"会议热键：{self.config['hotkey_meeting_toggle']}"
        )

    def show_about(self) -> None:
        ok, message = verify_model_files()
        details = (
            f"完全本地运行，不上传任何音频。\n"
            f"引擎：SenseVoice int8（sherpa-onnx）\n"
            f"模型：{'就绪' if ok else '未就绪'}\n"
            f"配置：{self.config.path}\n"
            f"历史：{self.history.path}\n\n"
            f"听写热键：{self.config['hotkey_dictate']}（模式：{self.config['mode']}）\n"
            f"会议热键：{self.config['hotkey_meeting_toggle']}"
        )
        if not ok:
            details += f"\n\n{message}"
        self._ui_queue.put(("about", details))

    def set_mode(self, mode: str) -> None:
        self.config.set("mode", mode)
        self.config.save()
        log.info("听写模式已切换为 %s", mode)
        self._notify(f"听写模式：{'按住说话' if mode == 'push_to_talk' else '按一下开始/结束'}")

    def set_meeting_source(self, source_kind: str) -> None:
        """切换会议音源（托盘 / 设置界面），下次开始会议转写时生效。"""
        if source_kind not in MEETING_SOURCE_KINDS:
            return
        self.config.set("meeting_source", source_kind)
        self.config.save()
        label = MEETING_SOURCE_LABELS[source_kind]
        if self.meeting is not None and self.meeting.running:
            self._notify(f"会议音源已改为：{label}（当前会议结束后生效）")
        else:
            self._notify(f"会议音源：{label}")
        log.info("会议音源已切换为 %s（%s）", source_kind, label)

    def reload_config(self) -> None:
        self.config = Config.load(self.config.path)
        self.history.enabled = bool(self.config["history_enabled"])
        self.engine = self._make_engine()
        self._register_hotkeys()
        if self.overlay is not None:
            self.overlay.enabled = bool(self.config["overlay_enabled"])
            self.overlay.position = str(self.config["overlay_position"])
        self._notify("配置已重新载入")
        log.info("配置已重新载入")

    def _open_path(self, path: Path | str) -> None:
        target = Path(path)
        try:
            if not target.exists():
                self._notify(f"路径不存在：{target}")
                return
            os.startfile(str(target))  # noqa: S606 - 打开用户自己的文件
        except OSError as exc:
            log.error("打开 %s 失败：%s", target, exc)
            self._notify(f"打开失败：{exc}")

    # ------------------------------------------------------------------
    # 状态与通知
    # ------------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        self._state = state
        if self.tray is not None:
            self.tray.set_state(state)

    def _notify(self, message: str, *, error: bool = False, title: str | None = None) -> None:
        log.log(logging.ERROR if error else logging.INFO, "通知：%s", message)
        if not bool(self.config["show_notifications"]):
            return
        if self.tray is not None:
            self.tray.notify(message, title or ("vtw 出错了" if error else "vtw 语音转文字"))
        else:
            self._ui_queue.put(("message", message, error))

    # ------------------------------------------------------------------
    # UI 泵（Tk 主线程）
    # ------------------------------------------------------------------
    def _pump(self) -> None:
        if self._root is None:
            return
        try:
            while True:
                message = self._ui_queue.get_nowait()
                try:
                    self._handle_ui(message)
                except Exception:  # noqa: BLE001 - UI 异常不应中断事件循环
                    log.exception("处理 UI 消息失败：%r", message[:1])
        except queue.Empty:
            pass

        # 电平表 + 最长录音看门狗
        if self._dictating:
            if self.overlay is not None and self._source is not None:
                self.overlay.push_level(self._source.level)
            elapsed = time.monotonic() - self._dictate_started
            limit = float(self.config["max_dictation_seconds"])
            if elapsed > limit:
                log.warning("录音超过 %.0fs 上限，自动结束", limit)
                self._jobs.put(self._do_stop_dictation)

        # 空闲卸载模型
        self.engine.maybe_unload_if_idle()

        self._root.after(_UI_POLL_MS, self._pump)

    def _handle_ui(self, message: tuple) -> None:
        kind = message[0]

        if kind == "overlay_recording" and self.overlay:
            self.overlay.show_recording()
        elif kind == "overlay_recognizing" and self.overlay:
            self.overlay.show_recognizing()
        elif kind == "overlay_text" and self.overlay:
            self.overlay.show_text(message[1], ok=bool(message[2]))
        elif kind == "overlay_hide" and self.overlay:
            self.overlay.hide()
        elif kind == "message" and self.overlay:
            self.overlay.show_message(message[1], error=bool(message[2]))
        elif kind == "state":
            self._set_state(message[1])
        elif kind == "meeting_started":
            window = self._ensure_meeting_window()
            window.clear()
            window.set_status(f"● 正在转写（{message[2]}）", color="#ff5c5c")
            window.set_recording(True)
            window.set_footer(f"音源：{message[2]} · 输出：{message[1]}")
            window.show()
        elif kind == "meeting_segment":
            window = self._ensure_meeting_window()
            window.append(message[1], float(message[2]))
            window.set_footer(f"已转写 {self.meeting.segment_count if self.meeting else 0} 段")
        elif kind == "meeting_stopped":
            if self._meeting_window is not None:
                self._meeting_window.set_recording(False)
                self._meeting_window.set_footer(f"已保存：{message[1]}" if message[1] else "已停止")
        elif kind == "meeting_error":
            if self._meeting_window is not None:
                self._meeting_window.set_recording(False)
                self._meeting_window.set_status(f"出错：{message[1]}", color="#ff5c5c")
        elif kind == "pick_audio_file":
            self._pick_audio_file()
        elif kind == "file_progress":
            self._update_file_progress(float(message[1]), float(message[2]), int(message[3]))
        elif kind == "file_segment":
            if self._file_window is not None:
                self._file_window.append(message[1], float(message[2]))
        elif kind == "file_finished":
            path, count = message[1], int(message[2])
            duration, cancelled = float(message[3]), bool(message[4])
            window = self._file_window
            if window is not None:
                window.set_recording(
                    False,
                    status="已取消" if cancelled else "已完成",
                    color="#ffc44d" if cancelled else "#57d9a3",
                )
                window.set_footer(f"已保存：{path}")
            if cancelled:
                self._notify(f"文件转写已取消，已保存已转写部分：\n{path}")
            else:
                self._notify(f"文件转写完成（{duration:.1f}s 音频，{count} 段）：\n{path}")
            self.file = None
        elif kind == "file_error":
            window = self._file_window
            if window is not None:
                window.set_recording(False, status="出错", color="#ff5c5c")
                window.set_footer(f"出错了：{message[1]}")
            self._notify(f"文件转写失败：{message[1]}", error=True)
            self.file = None
        elif kind == "show_file_window":
            if self._file_window is not None:
                self._file_window.show()
            else:
                self._notify("还没有文件转写窗口：从托盘「转写音频文件…」选一个文件开始。")
        elif kind == "show_meeting_window":
            window = self._ensure_meeting_window()
            window.show()
        elif kind == "show_history":
            self._open_history_window(message[1])
        elif kind == "show_settings":
            self._open_settings_window()
        elif kind == "about":
            if self._root is not None:
                ui_windows.show_about(self._root, __version__, message[1])

    def _ensure_meeting_window(self) -> ui_windows.MeetingWindow:
        if self._meeting_window is None:
            assert self._root is not None
            self._meeting_window = ui_windows.MeetingWindow(
                self._root, on_stop=self.toggle_meeting
            )
        return self._meeting_window

    def _open_history_window(self, records: list[dict[str, Any]]) -> None:
        if self._root is None:
            return
        ui_windows.HistoryWindow(self._root, records)

    def _open_settings_window(self) -> None:
        if self._root is None:
            return
        # 复用同一个窗口实例：关闭时只是 destroy，因此每次重新创建
        self._settings_window = settings_window.SettingsWindow(self._root, self)
        self._settings_window.lift()
