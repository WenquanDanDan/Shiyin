#!/usr/bin/env python3
"""Phase 1 门禁：模型加载 + 识别 + VAD 冒烟测试。

用模型自带的 ``test_wavs`` 跑真实推理，验证：
1. SenseVoice 能加载并输出正确文本
2. 中文识别准确（与官方 ground truth 比对）
3. **英文大小写不被破坏**（官方示例的 ``.lower()`` 陷阱）
4. RTF 足够低（真实时率）
5. VAD 能正常切分

用法::

    python scripts/smoke_asr.py
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import paths  # noqa: E402
from vtw.asr import SenseVoiceEngine  # noqa: E402
from vtw.textutil import clean_text  # noqa: E402

RTF_BUDGET = 0.30

#: 官方文档给出的 zh.wav ground truth 片段。选用的 2024-07-17 int8 模型
#: 配合 use_itn=True 时可以逐字命中，因此这里作为准确率门禁。
ZH_EXPECT_HINTS = ("早上9点至下午5点",)

#: 官方文档给出的 en.wav ground truth（同样应逐字命中，且大小写正确）
EN_EXPECTED = "The tribal chieftain called for the boy and presented him with 50 pieces of code."


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """用标准库读取 16-bit PCM WAV，返回 float32 单声道与采样率。"""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的采样位宽：{width * 8} bit")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def main() -> int:
    print("=" * 64)
    print("vtw 冒烟测试：模型加载 / 识别 / VAD")
    print("=" * 64)

    wav_dir = paths.SENSE_VOICE_DIR / "test_wavs"
    if not wav_dir.is_dir():
        print(f"[失败] 找不到测试音频目录：{wav_dir}", file=sys.stderr)
        return 1

    failures: list[str] = []

    # ---- 1. 加载与预热 --------------------------------------------------
    engine = SenseVoiceEngine(num_threads=4, language="auto", use_itn=True)
    started = time.perf_counter()
    if not engine.warmup():
        print("[失败] 模型加载失败", file=sys.stderr)
        return 1
    load_seconds = time.perf_counter() - started
    print(f"\n[通过] 模型加载耗时 {load_seconds:.2f}s\n")

    # ---- 2. 中文识别 + RTF ---------------------------------------------
    zh_wav = wav_dir / "zh.wav"
    print(f"--- 中文样例：{zh_wav.name} ---")
    audio, rate = read_wav(zh_wav)
    print(f"    音频：{audio.size / rate:.2f}s @ {rate}Hz")

    # 跑两遍：第一遍含首次推理的惰性初始化开销，第二遍才是稳态性能
    engine.transcribe(audio, rate)
    result = engine.transcribe(audio, rate)
    print(f"    识别：{result.text}")
    print(f"    耗时：{result.elapsed:.3f}s    RTF：{result.rtf:.4f}")
    print(f"    语言：{result.lang}  情绪：{result.emotion}  事件：{result.event}")

    if not result.text.strip():
        failures.append("中文样例识别结果为空")
    elif not any(hint in result.text for hint in ZH_EXPECT_HINTS):
        failures.append(
            f"中文识别未命中预期的官方真值片段 {ZH_EXPECT_HINTS}，实际：{result.text!r}"
        )
    if result.rtf >= RTF_BUDGET:
        failures.append(f"RTF {result.rtf:.3f} 超出预算 {RTF_BUDGET}")
    if "<|" in result.text:
        failures.append("识别结果中残留 SenseVoice 控制标签")

    # ---- 3. 英文大小写与归一化 -----------------------------------------
    en_wav = wav_dir / "en.wav"
    if en_wav.is_file():
        print(f"\n--- 英文样例：{en_wav.name} ---")
        en_audio, en_rate = read_wav(en_wav)
        en_result = engine.transcribe(en_audio, en_rate)
        print(f"    识别：{en_result.text}")
        print(f"    真值：{EN_EXPECTED}")
        print(f"    耗时：{en_result.elapsed:.3f}s    RTF：{en_result.rtf:.4f}")
        if en_result.text != EN_EXPECTED:
            failures.append(f"英文识别与官方真值不一致：{en_result.text!r}")
        if en_result.rtf >= RTF_BUDGET:
            failures.append(f"英文 RTF {en_result.rtf:.3f} 超出预算 {RTF_BUDGET}")
    else:
        print(f"\n[跳过] 未找到英文样例 {en_wav}")

    # ---- 4. 多语言（粤语）----------------------------------------------
    yue_wav = wav_dir / "yue.wav"
    if yue_wav.is_file():
        print(f"\n--- 粤语样例：{yue_wav.name} ---")
        yue_audio, yue_rate = read_wav(yue_wav)
        yue_result = engine.transcribe(yue_audio, yue_rate)
        print(f"    识别：{yue_result.text}")
        print(f"    语言标签：{yue_result.lang}")

    # ---- 5. VAD --------------------------------------------------------
    print("\n--- VAD 切分 ---")
    try:
        from vtw.vad import VadSegmenter, VadUnavailableError

        segmenter = VadSegmenter(sample_rate=rate, min_silence_duration=0.3)
        segments: list[np.ndarray] = []
        # 按 100ms 分块喂入，模拟真实流式场景
        block = int(rate * 0.1)
        for offset in range(0, audio.size, block):
            segments.extend(segmenter.accept(audio[offset : offset + block]))
        segments.extend(segmenter.flush())
        total = sum(s.size for s in segments) / rate
        print(f"    切出 {len(segments)} 段，合计 {total:.2f}s")
        if not segments:
            failures.append("VAD 未能从中文样例中切出任何语音段")
        else:
            combined = "".join(
                engine.transcribe(s, rate).text for s in segments if s.size > rate * 0.2
            )
            print(f"    逐段识别拼接：{combined}")
    except VadUnavailableError as exc:
        failures.append(f"VAD 不可用：{exc}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"VAD 测试异常：{type(exc).__name__}: {exc}")

    # ---- 6. 文本清洗 ----------------------------------------------------
    print("\n--- 文本清洗 ---")
    dirty = "<|zh|><|NEUTRAL|><|Speech|>  今天  天气 不错 <|withitn|>"
    cleaned = clean_text(dirty)
    print(f"    输入：{dirty!r}")
    print(f"    输出：{cleaned!r}")
    if "<|" in cleaned:
        failures.append("文本清洗未能剥离控制标签")
    if clean_text("Hello World") != "Hello World":
        failures.append("文本清洗破坏了英文大小写")

    # ---- 汇总 -----------------------------------------------------------
    print("\n" + "=" * 64)
    if failures:
        print("冒烟测试未通过：", file=sys.stderr)
        for item in failures:
            print(f"  ✗ {item}", file=sys.stderr)
        return 1
    print("冒烟测试全部通过 ✓")
    print(f"  · 模型加载：{load_seconds:.2f}s")
    print(f"  · 中文 RTF：{result.rtf:.4f}（预算 {RTF_BUDGET}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
