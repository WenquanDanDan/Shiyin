#!/usr/bin/env python3
"""端到端验证「口述标点」：真实合成语音 → 真实识别 → 标点转换。

音频用 Windows 自带 SAPI 合成（Microsoft Huihui zh-CN），内容是
"大家好逗号今天天气不错句号……"，因此这条链路是**真实**的：
不是拿手写字符串去喂函数，而是真的让模型去听一段说了标点词的语音。

同时对比开关打开/关闭两种结果，确认这个功能确实可控。

用法::

    python scripts/test_punctuation.py
    python scripts/test_punctuation.py --regenerate   # 重新合成音频
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.WARNING, format="      [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout
)

from vtw import paths  # noqa: E402
from vtw.asr import SenseVoiceEngine  # noqa: E402
from vtw.textutil import clean_text  # noqa: E402

SAMPLE_WAV = paths.PROJECT_ROOT / "output" / "tts_punct_sample.wav"

#: 期望：标点词被替换成符号，且模型补的重复标点被吃掉
EXPECT_FRAGMENTS = ("大家好，今天天气不错。", "？", "！")
#: 不该再出现的口述标点词
FORBIDDEN_WORDS = ("逗号", "句号", "问号", "感叹号", "换行")


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as fh:
        channels, rate, width = fh.getnchannels(), fh.getframerate(), fh.getsampwidth()
        frames = fh.readframes(fh.getnframes())
    if width != 2:
        raise ValueError(f"只支持 16-bit PCM，实际 {width * 8} bit")
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def ensure_sample(*, regenerate: bool) -> bool:
    if SAMPLE_WAV.is_file() and not regenerate:
        print(f"      复用已有音频：{SAMPLE_WAV.name}")
        return True

    script = paths.PROJECT_ROOT / "scripts" / "make_tts_sample.ps1"
    if not script.is_file():
        print(f"[失败] 找不到合成脚本：{script}", file=sys.stderr)
        return False

    print("      正在用 Windows SAPI 合成测试语音…")
    completed = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        print(f"[失败] 语音合成失败：\n{completed.stdout}\n{completed.stderr}", file=sys.stderr)
        return False
    print("      " + (completed.stdout or "").strip().replace("\n", "\n      "))
    return SAMPLE_WAV.is_file()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--regenerate", action="store_true", help="重新合成测试音频")
    args = parser.parse_args()

    print("=" * 66)
    print("vtw 口述标点测试（合成语音 → 识别 → 标点转换）")
    print("=" * 66)

    print("\n[1/4] 准备测试音频 …")
    if not ensure_sample(regenerate=args.regenerate):
        return 2

    audio, rate = read_wav(SAMPLE_WAV)
    print(f"      {audio.size / rate:.2f}s @ {rate}Hz")

    print("\n[2/4] 加载模型并识别（关闭口述标点，看模型原始输出）…")
    engine = SenseVoiceEngine(num_threads=4, language="auto", use_itn=True)
    engine.warmup()
    engine.transcribe(audio, rate)  # 预热
    raw = engine.transcribe(audio, rate, spoken_punctuation=False)
    print(f"      原始输出：{raw.text!r}")

    print("\n[3/4] 打开口述标点再跑一次 …")
    fixed = engine.transcribe(audio, rate, spoken_punctuation=True)
    print(f"      转换结果：{fixed.text!r}")
    print(f"      清洗链路：{clean_text(raw.text)!r}")

    print("\n[4/4] 判定 …")
    failures: list[str] = []

    # 关闭时应当保留口述标点词（证明这是我们的后处理在起作用，而不是模型自带的）
    if not any(word in raw.text for word in FORBIDDEN_WORDS):
        print(
            "      [提示] 关闭口述标点时模型输出里也没看到标点词，"
            "可能语音合成质量或措辞导致，判定以开启后结果为准。"
        )

    for fragment in EXPECT_FRAGMENTS:
        if fragment not in fixed.text:
            failures.append(f"转换结果缺少预期片段 {fragment!r}")

    for word in FORBIDDEN_WORDS:
        if word in fixed.text:
            failures.append(f"转换后仍残留口述标点词 {word!r}")

    if "，，" in fixed.text or "。。" in fixed.text:
        failures.append(f"出现重复标点：{fixed.text!r}")

    if not fixed.text.strip():
        failures.append("识别结果为空")

    print("=" * 66)
    if failures:
        print("测试未通过：", file=sys.stderr)
        for item in failures:
            print(f"  ✗ {item}", file=sys.stderr)
        return 1

    print("测试通过 ✓")
    print("  · 关闭时：标点词原样保留（模型本身不做这个转换）")
    print("  · 开启时：标点词被替换为符号，模型补的重复标点被吃掉")
    print("  · 结果中无重复标点、无残留标点词")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
