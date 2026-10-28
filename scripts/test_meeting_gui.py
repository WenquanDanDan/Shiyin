#!/usr/bin/env python3
"""会议转写界面端到端测试。

与 ``test_meeting.py`` 的区别：那个脚本只验证"环回采集 → VAD → 识别"这段
音频链路；本脚本走 **App 的真实路径** —— 启动会议会话、把 ``meeting_started``
消息交给 UI 泵、真正创建字幕窗口、播放声音、检查字幕是否出现在窗口里、
停止后检查文字稿是否落盘。

为什么需要它：``MeetingWindow`` 曾因 ``tk.Frame(..., pady=(0, 8))`` 抛
``TclError: bad screen distance``，而 ``App._pump`` 会把异常吞掉只写日志。
音频链路测试当时是"通过"的，但用户按会议热键后什么都看不到 —— 只有把窗口
真正建出来才能发现。

环境不支持（没有输出设备/环回采集失败）时返回 2，不误判成产品缺陷。

用法::

    python scripts/test_meeting_gui.py                 # 系统声音（在线会议，默认）
    python scripts/test_meeting_gui.py --source mic    # 麦克风（线下会议）
    python scripts/test_meeting_gui.py --source both   # 系统声音 + 麦克风（戴耳机）
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import threading
import time
import tkinter as tk
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.WARNING, format="      [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout
)

from vtw import paths  # noqa: E402
from vtw.app import App  # noqa: E402

EXPECTED = ("9点", "下午5点")  # 环回会叠加扬声器音质，允许首字识别偏差
PLAY_VOLUME = 0.4


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as fh:
        channels, rate = fh.getnchannels(), fh.getframerate()
        frames = fh.readframes(fh.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def main() -> int:
    parser = argparse.ArgumentParser(description="vtw 会议转写界面端到端测试")
    parser.add_argument(
        "--source",
        choices=("loopback", "mic", "both"),
        default="loopback",
        help="会议音源（默认 loopback=系统声音；mic=麦克风；both=系统声音+麦克风）",
    )
    args = parser.parse_args()
    source_label = {"loopback": "系统声音", "mic": "麦克风", "both": "系统声音 + 麦克风"}[
        args.source
    ]

    wav = paths.SENSE_VOICE_DIR / "test_wavs" / "zh.wav"
    if not wav.is_file():
        print(f"[失败] 找不到测试音频：{wav}", file=sys.stderr)
        return 1
    audio, rate = read_wav(wav)

    print("=" * 64)
    print("vtw 会议转写界面端到端测试（窗口 -> 实时字幕 -> 落盘）")
    print("=" * 64)
    print(f"\n[1/5] 音源：{source_label}；测试音频：{wav.name}  {audio.size / rate:.2f}s")

    tmp = tempfile.TemporaryDirectory(prefix="vtw-meeting-gui-")
    app = App(Path(tmp.name) / "config.json")
    app.config.set("output_dir", str(Path(tmp.name) / "output"))
    app.config.set("save_meeting_audio", False)
    app.config.set("meeting_source", args.source)
    app.history.enabled = False  # 测试不写用户的历史记录

    print("\n[2/5] 预热识别模型 …")
    if not app.engine.warmup():
        print("[失败] 模型不可用，请先运行 python scripts/download_models.py", file=sys.stderr)
        tmp.cleanup()
        return 1

    errors: list[str] = []
    original_on_error = app._on_meeting_error

    def record_error(message: str) -> None:
        errors.append(message)
        original_on_error(message)

    app._on_meeting_error = record_error  # type: ignore[method-assign]

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"\n[环境不支持] 无法创建 Tk 窗口：{exc}", file=sys.stderr)
        tmp.cleanup()
        return 2
    root.withdraw()
    app._root = root
    state: dict[str, object] = {}

    def play() -> None:
        import sounddevice as sd

        try:
            time.sleep(1.0)
            # 麦克风是"声学回环"：扬声器要开大一点才录得清
            volume = 0.9 if args.source in ("mic", "both") else PLAY_VOLUME
            sd.play(audio * volume, rate)
            time.sleep(audio.size / rate + 1.0)
            sd.stop()
        except Exception as exc:  # noqa: BLE001 - 播放失败属于环境问题
            state["play_error"] = str(exc)

    def stop_after_audio() -> None:
        state["path"] = app.meeting.transcript_path if app.meeting else None
        threading.Thread(target=app._do_stop_meeting, daemon=True).start()
        root.after(8000, root.quit)

    print(f"\n[3/5] 启动会议转写（音源：{source_label}），并从扬声器播放测试音频 …")
    root.after(50, app._pump)
    root.after(300, app._do_start_meeting)
    threading.Thread(target=play, daemon=True).start()
    root.after(int((audio.size / rate + 3.5) * 1000), stop_after_audio)
    root.mainloop()

    window = app._meeting_window
    text = window.text.get("1.0", "end").strip() if window is not None else ""
    path = state.get("path")
    content = ""
    if path and Path(str(path)).is_file():
        content = Path(str(path)).read_text(encoding="utf-8")

    # ---- 先区分"环境不支持"与真正的产品缺陷 ------------------------------
    if errors:
        print(f"\n[环境不支持] 会议转写无法启动：{errors[0]}", file=sys.stderr)
        _cleanup(window, root, tmp)
        return 2
    if state.get("play_error"):
        print(f"\n[环境不支持] 扬声器播放失败：{state['play_error']}", file=sys.stderr)
        _cleanup(window, root, tmp)
        return 2

    failure = 0
    print("\n[4/5] 检查字幕窗口 …")
    if window is None:
        print("      [失败] 会议窗口没有创建（按热键后将看不到任何字幕）", file=sys.stderr)
        failure = 1
    else:
        print(f"      [OK] 窗口已创建：{window.win.title()}")
        print(f"      字幕：{text!r}")
        print(f"      状态：{window.status.cget('text')}  页脚：{window.footer.cget('text')}")
        if not text:
            print("      [失败] 窗口里没有任何字幕", file=sys.stderr)
            failure = 1

    print("\n[5/5] 检查文字稿落盘 …")
    transcript_ok = bool(content) and "共 0 段" not in content and "[00:" in content
    if content:
        print(f"      文件：{path}")
        for line in content.splitlines():
            if line.startswith("[") or line.startswith("共 "):
                print(f"      {line}")
    if not transcript_ok:
        print("      [失败] 没有识别到内容（确认系统音量未被静音）", file=sys.stderr)
        failure = 1
    elif args.source == "loopback" and not all(word in text for word in EXPECTED):
        print(
            "\n[失败] 窗口字幕与预期不符。注意：环回采集会受系统音量/音效影响。",
            file=sys.stderr,
        )
        failure = 1
    elif args.source != "loopback" and not all(word in text for word in EXPECTED):
        # 麦克风是声学回环：房间混响、距离、音量都会影响内容，不要求逐字一致
        print("      提示：麦克风模式走声学回环，字幕内容与测试音不同属正常，"
              "有字幕即视为通过。")

    if failure:
        print("\n测试未通过：会议转写界面仍有问题。", file=sys.stderr)
    else:
        print("\n测试通过：会议窗口、实时字幕、文字稿落盘均正常。")

    _cleanup(window, root, tmp)
    return failure


def _cleanup(window: object, root: tk.Tk, tmp: tempfile.TemporaryDirectory) -> None:
    try:
        if window is not None:
            window.destroy()  # type: ignore[attr-defined]
        root.destroy()
    except tk.TclError:
        pass
    tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
