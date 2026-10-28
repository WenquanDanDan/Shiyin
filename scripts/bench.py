#!/usr/bin/env python3
"""引擎配置对比与性能实测。

用来确定默认配置（语言、ITN）并产出可复现的性能数据：
* 在模型自带的 zh / en / ja / ko / yue 样例上跑不同配置
* 记录文本、RTF、耗时、内存占用

用法::

    python scripts/bench.py            # 只跑中文 + 英文
    python scripts/bench.py --all      # 跑全部语言样例
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import paths  # noqa: E402


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    else:
        raise ValueError(f"不支持的位宽：{width}")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def rss_mb() -> float:
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ctypes.WinDLL("psapi").GetProcessMemoryInfo(
            ctypes.WinDLL("kernel32").GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return counters.WorkingSetSize / 1024 / 1024
    except Exception:  # noqa: BLE001
        return -1.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="包含日/韩/粤样例")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    import sherpa_onnx

    wav_dir = paths.SENSE_VOICE_DIR / "test_wavs"
    model = str(paths.sense_voice_model_path())
    tokens = str(paths.sense_voice_tokens_path())

    samples = [("zh", "zh.wav"), ("en", "en.wav")]
    if args.all:
        samples += [("ja", "ja.wav"), ("ko", "ko.wav"), ("yue", "yue.wav")]

    baseline = rss_mb()
    print(f"进程基线内存：{baseline:.0f} MB")
    print("=" * 78)

    configs = [
        ("auto + ITN", "", True, args.threads),
        ("auto 无ITN", "", False, args.threads),
        ("zh 指定 + ITN", "zh", True, args.threads),
        ("en 指定 + ITN", "en", True, args.threads),
        ("auto + ITN 单线程", "", True, 1),
    ]

    for lang_name, _ in samples:
        wav = wav_dir / f"{lang_name}.wav"
        if not wav.is_file():
            continue
        audio, rate = read_wav(wav)
        print(f"\n### {lang_name}.wav  ({audio.size / rate:.2f}s)")

        for label, lang, itn, threads in configs:
            try:
                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=model,
                    tokens=tokens,
                    num_threads=threads,
                    language=lang,
                    use_itn=itn,
                    debug=False,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  {label:<20} 加载失败：{exc}")
                continue

            # 第一遍预热，第二遍计时
            stream = recognizer.create_stream()
            stream.accept_waveform(rate, audio)
            recognizer.decode_stream(stream)

            gc.collect()
            before = rss_mb()
            started = time.perf_counter()
            stream = recognizer.create_stream()
            stream.accept_waveform(rate, audio)
            recognizer.decode_stream(stream)
            elapsed = time.perf_counter() - started
            after = rss_mb()

            rtf = elapsed / (audio.size / rate)
            text = (stream.result.text or "").strip()
            print(f"  {label:<20} RTF={rtf:6.4f}  {elapsed * 1000:6.1f}ms  内存Δ{after - before:+6.0f}MB")
            print(f"      {text}")
            del recognizer

    print("\n" + "=" * 78)
    print(f"结束内存：{rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
