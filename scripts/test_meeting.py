#!/usr/bin/env python3
"""会议转写链路测试：播放音频 → WASAPI 环回采集 → VAD 切句 → 识别。

这是"会议/系统声音转写"功能里可自动化的部分。脚本会把模型自带的 zh.wav
**从扬声器播放出来**（约 5.6 秒，声音不大），同时用环回设备录下来，
再走完整的 VAD + 识别流程，最后与预期文本比对。

注意：需要系统有可用的输出设备；若主机静音或没有声卡，测试会明确报出
"环境不支持"而不是误判成产品缺陷。

用法::

    python scripts/test_meeting.py
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
from vtw.audio_source import AudioSourceError, LoopbackSource  # noqa: E402
from vtw.textutil import is_meaningful  # noqa: E402
from vtw.vad import VadSegmenter, VadUnavailableError  # noqa: E402

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
    print("vtw 会议转写链路测试（扬声器播放 → 环回采集 → VAD → 识别）")
    print("=" * 64)

    audio, rate = read_wav(wav)
    print(f"\n[1/5] 测试音频：{wav.name}  {audio.size / rate:.2f}s @ {rate}Hz")
    print("      接下来会从扬声器播放这段音频（约 5.6 秒）")

    # ---- 先开环回采集，再播放，避免漏掉开头 ----
    print("\n[2/5] 启动 WASAPI 环回采集 …")
    source = LoopbackSource()
    try:
        source.start()
    except AudioSourceError as exc:
        print(f"\n[环境不支持] 无法启动系统声音采集：\n{exc}", file=sys.stderr)
        return 2
    print(f"      设备：{source.device_name}")

    # ---- 播放 ----
    print("\n[3/5] 播放音频 …")
    try:
        import sounddevice as sd

        # 首次探测 0.4 倍音量，避免声音过大
        sd.play(audio * 0.4, rate)
        deadline = time.monotonic() + audio.size / rate + 1.5
        while time.monotonic() < deadline:
            time.sleep(0.05)
        sd.stop()
    except Exception as exc:  # noqa: BLE001
        print(f"\n[环境不支持] 播放失败：{exc}", file=sys.stderr)
        source.stop()
        return 2

    captured = source.stop()
    source.close()
    duration = captured.size / rate
    peak = float(np.abs(captured).max()) if captured.size else 0.0
    print(f"      采集到 {duration:.2f}s 音频，峰值 {peak:.4f}")

    if peak < 0.005:
        print(
            "\n[环境不支持] 环回采到的音频几乎全是静音。\n"
            "      常见原因：系统音量被静音、没有默认输出设备、或本会话没有音频。",
            file=sys.stderr,
        )
        return 2

    # ---- VAD + 识别 ----
    print("\n[4/5] VAD 切句并逐句识别 …")
    try:
        segmenter = VadSegmenter(sample_rate=rate, min_silence_duration=0.4)
    except VadUnavailableError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    engine = SenseVoiceEngine(num_threads=4, language="auto", use_itn=True)
    engine.warmup()

    segments: list[np.ndarray] = []
    block = int(rate * 0.1)
    for offset in range(0, captured.size, block):
        segments.extend(segmenter.accept(captured[offset : offset + block]))
    segments.extend(segmenter.flush())

    print(f"      切出 {len(segments)} 段")
    texts: list[str] = []
    for index, segment in enumerate(segments):
        if segment.size < rate * 0.2:
            continue
        result = engine.transcribe(segment, rate)
        if is_meaningful(result.text):
            texts.append(result.text)
            print(f"      [{index}] {result.text}")

    combined = " ".join(texts)

    # ---- 判定 ----
    print("\n[5/5] 结果比对")
    print(f"      识别拼接：{combined!r}")
    print(f"      预期包含：{EXPECTED!r}")

    if not texts:
        print("\n测试未通过：VAD 未识别出任何内容", file=sys.stderr)
        return 1
    if EXPECTED not in combined.replace(" ", ""):
        print(
            "\n测试未通过：识别结果与预期不符。\n"
            "      注意：环回采集会叠加系统音效/音量影响，语音质量可能低于原始文件。",
            file=sys.stderr,
        )
        return 1

    print("\n测试通过：扬声器播放的声音被正确采集、切句并识别。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
