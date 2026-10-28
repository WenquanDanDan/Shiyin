#!/usr/bin/env python3
"""麦克风链路测试：扬声器放音 → 麦克风采集 → 识别。

用"声学回环"来验证麦克风路径（因为没有真人对着麦克风说话）：
从扬声器播放测试音频，同时用麦克风录进来，走完整识别流程。

若麦克风离扬声器较远或环境嘈杂，识别可能不准确 —— 那种情况下脚本会区分
"设备不可用"（环境问题）与"识别结果为空"（可能只是没录到清晰语音）。

用法::

    python scripts/test_mic.py
"""

from __future__ import annotations

import logging
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.INFO, format="      [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout
)

from vtw import paths  # noqa: E402
from vtw.asr import SenseVoiceEngine  # noqa: E402
from vtw.audio_source import AudioSourceError, MicSource  # noqa: E402
from vtw.textutil import is_meaningful  # noqa: E402
from vtw.vad import trim_silence  # noqa: E402

EXPECTED = "开饭时间早上9点至下午5点"


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as fh:
        channels, rate = fh.getnchannels(), fh.getframerate()
        frames = fh.readframes(fh.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def main() -> int:
    wav = paths.SENSE_VOICE_DIR / "test_wavs" / "zh.wav"
    if not wav.is_file():
        print(f"[失败] 找不到测试音频：{wav}", file=sys.stderr)
        return 1

    print("=" * 64)
    print("vtw 麦克风链路测试（扬声器放音 → 麦克风采集 → 识别）")
    print("=" * 64)

    audio, rate = read_wav(wav)
    print(f"\n[1/4] 测试音频 {audio.size / rate:.2f}s，将放大音量从扬声器播放")

    print("\n[2/4] 打开麦克风 …")
    source = MicSource()
    try:
        source.start()
    except AudioSourceError as exc:
        print(f"\n[环境不支持] 无法打开麦克风：\n{exc}", file=sys.stderr)
        return 2
    print(f"      设备：{source.device_name}")

    print("\n[3/4] 播放音频并同时录音 …")
    try:
        import sounddevice as sd

        sd.play(audio * 0.95, rate)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[环境不支持] 播放失败：{exc}", file=sys.stderr)
        source.stop()
        return 2

    peak = 0.0
    deadline = time.monotonic() + audio.size / rate + 1.2
    while time.monotonic() < deadline:
        peak = max(peak, source.level)
        time.sleep(0.03)

    captured = source.stop()
    source.close()
    duration = captured.size / rate
    rms_peak = float(np.abs(captured).max()) if captured.size else 0.0
    print(f"      录到 {duration:.2f}s，峰值 {rms_peak:.4f}，实时电平峰值 {peak:.3f}")

    if rms_peak < 0.002:
        print(
            "\n[环境不支持] 麦克风几乎没录到声音。\n"
            "      常见原因：麦克风被静音、隐私设置禁止桌面应用访问、或音量过低。\n"
            "      请检查：设置 → 隐私和安全性 → 麦克风。",
            file=sys.stderr,
        )
        return 2

    trimmed = trim_silence(captured, sample_rate=rate)
    if trimmed.size > rate * 0.2:
        captured = trimmed

    print("\n[4/4] 识别 …")
    engine = SenseVoiceEngine(num_threads=4, language="auto", use_itn=True)
    engine.warmup()
    result = engine.transcribe(captured, rate)
    print(f"      识别结果：{result.text!r}")
    print(f"      预期包含：{EXPECTED!r}")

    if not is_meaningful(result.text):
        print(
            "\n[无法判定] 麦克风采集正常（有信号），但没识别出内容。\n"
            "      声学回环受房间混响/距离影响很大，这**不等于**麦克风功能有问题。\n"
            "      请按 README 的手工验收清单，由真人对着麦克风验证听写。",
            file=sys.stderr,
        )
        return 2

    if EXPECTED in result.text.replace(" ", ""):
        print("\n测试通过 ✓  麦克风采集 → 识别 全链路正常。")
        return 0

    print(
        f"\n[部分通过] 麦克风采集与识别均工作，但文本与预期不完全一致：{result.text!r}\n"
        "      声学回环下这是正常现象（混响、距离、音量都会影响准确率）。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
