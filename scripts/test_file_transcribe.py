#!/usr/bin/env python3
"""音频文件转写端到端测试。

默认会转写模型自带的 ``test_wavs/zh.wav``，并额外生成两份副本验证解码层：

1. 44.1kHz 立体声 WAV（验证降混 + 重采样）
2. MP3 / M4A（验证 PyAV 解码；未安装 PyAV 时跳过）

也可以转写你自己的文件::

    python scripts/test_file_transcribe.py --file "D:\\audio\\meeting.m4a"
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.WARNING, format="      [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout
)

from vtw import file_transcribe as ft  # noqa: E402
from vtw import paths  # noqa: E402
from vtw.asr import SenseVoiceEngine  # noqa: E402

EXPECTED = "开饭时间早上9点至下午5点"


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as fh:
        channels, rate = fh.getnchannels(), fh.getframerate()
        raw = fh.readframes(fh.getnframes())
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def write_wav(path: Path, audio: np.ndarray, rate: int, channels: int = 1) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(channels)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(pcm.tobytes())


def encode(path: Path, audio: np.ndarray, rate: int, codec: str) -> None:
    import av

    container = av.open(str(path), "w")
    try:
        stream = container.add_stream(codec, rate=rate)
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(
            audio.reshape(1, -1).astype(np.float32), format="flt", layout="mono"
        )
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()


def transcribe(path: Path, engine: SenseVoiceEngine, out_dir: Path):
    """跑一次完整文件转写，返回 (段列表, 结果字典, 耗时)。"""
    segments: list[tuple[str, float]] = []
    result: dict[str, object] = {}
    done = threading.Event()

    def finished(transcript, count, duration, cancelled) -> None:
        result.update(
            transcript=str(transcript),
            count=int(count),
            duration=float(duration),
            cancelled=bool(cancelled),
        )
        done.set()

    def failed(message: str) -> None:
        result["error"] = message
        done.set()

    transcriber = ft.FileTranscriber(
        engine,
        output_dir=out_dir,
        on_segment=lambda text, offset: segments.append((text, offset)),
        on_finished=finished,
        on_error=failed,
    )
    started = time.monotonic()
    if not transcriber.start(path):
        return segments, {"error": "无法开始转写"}, 0.0
    if not done.wait(600):
        transcriber.cancel()
        return segments, {"error": "转写超时（10 分钟）"}, time.monotonic() - started
    return segments, result, time.monotonic() - started


def report(name: str, segments, result, elapsed: float, *, strict: bool) -> bool:
    text = "".join(seg for seg, _ in segments)
    duration = float(result.get("duration") or 0.0)
    print(f"      段数：{len(segments)}  音频：{duration:.2f}s  耗时：{elapsed:.2f}s")
    if duration > 0 and elapsed > 0:
        print(f"      RTF：{elapsed / duration:.3f}")
    for index, (seg, offset) in enumerate(segments):
        print(f"      [{index}] {offset:6.2f}s  {seg}")
    if "error" in result:
        print(f"      [失败] {result['error']}", file=sys.stderr)
        return False
    print(f"      文字稿：{result.get('transcript')}")
    if not text:
        print("      [失败] 没有识别出任何内容", file=sys.stderr)
        return False
    normalized = text.replace(" ", "")
    if strict:
        ok = EXPECTED in normalized
        print(f"      预期包含：{EXPECTED!r} -> {'通过' if ok else '不符'}")
        return ok
    ok = "9点" in normalized and "下午5点" in normalized
    print(f"      压缩格式容错校验（包含 9点 / 下午5点）-> {'通过' if ok else '不符'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="音频文件转写端到端测试")
    parser.add_argument("--file", type=Path, help="转写指定文件（跳过内置样例）")
    args = parser.parse_args()

    print("=" * 66)
    print("vtw 音频文件转写端到端测试")
    print("=" * 66)

    engine = SenseVoiceEngine(num_threads=4, language="auto", use_itn=True)
    if not engine.warmup():
        print("[失败] 模型不可用，请先运行 python scripts/download_models.py", file=sys.stderr)
        return 1

    if args.file:
        source = args.file.expanduser()
        if not source.is_file():
            print(f"[失败] 文件不存在：{source}", file=sys.stderr)
            return 1
        print(f"\n转写：{source}")
        with tempfile.TemporaryDirectory(prefix="vtw-file-") as tmp:
            segments, result, elapsed = transcribe(source, engine, Path(tmp))
            report(source.name, segments, result, elapsed, strict=False)
        return 0 if segments and "error" not in result else 1

    sample = paths.SENSE_VOICE_DIR / "test_wavs" / "zh.wav"
    if not sample.is_file():
        print(f"[失败] 找不到测试音频：{sample}", file=sys.stderr)
        return 1
    audio, rate = read_wav(sample)
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="vtw-file-") as tmp_name:
        tmp = Path(tmp_name)
        out_dir = tmp / "out"

        print(f"\n[1/3] 转写 WAV：{sample.name}（16kHz 单声道）…")
        segments, result, elapsed = transcribe(sample, engine, out_dir)
        if not report("wav", segments, result, elapsed, strict=True):
            failures.append("WAV 转写结果不符")

        print("\n[2/3] 生成并转写 44.1kHz 立体声 WAV（降混 + 重采样）…")
        from vtw.audio_source import resample

        stereo_audio = resample(audio, rate, 44100)
        stereo = tmp / "stereo-44k.wav"
        write_wav(stereo, stereo_audio, 44100, channels=2)
        segments, result, elapsed = transcribe(stereo, engine, out_dir)
        if not report("stereo", segments, result, elapsed, strict=True):
            failures.append("立体声/44.1kHz 转写结果不符")

        print("\n[3/3] 压缩格式（MP3 / M4A）…")
        if not ft.av_available():
            print("      未安装 PyAV，跳过（WAV 功能不受影响）")
        else:
            for codec, suffix in (("mp3", ".mp3"), ("aac", ".m4a")):
                target = tmp / f"sample{suffix}"
                print(f"      生成 {suffix} 并转写 …")
                try:
                    encode(target, audio, rate, codec)
                except Exception as exc:  # noqa: BLE001 - 本机 FFmpeg 缺编码器
                    print(f"      [跳过] 本机无法编码 {codec}：{exc}")
                    continue
                segments, result, elapsed = transcribe(target, engine, out_dir)
                if not report(suffix, segments, result, elapsed, strict=False):
                    failures.append(f"{suffix} 转写结果不符")

    print("\n" + "=" * 66)
    if failures:
        print("测试未通过：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("测试通过：WAV / 44.1kHz 立体声 / 压缩格式的文件转写均正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
