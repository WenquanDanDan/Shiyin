#!/usr/bin/env python3
"""候选模型横向对比。

起因：smoke 测试发现 2025-09-09 版模型把英文整句输出成大写
（``THE TRIVBAL CHIEF THIN``），而官方文档里 2024-07-17 版输出的是正常的
``the tribal chieftain called for the boy``；并且开启 ITN 后中文会掉字。

因此这里把候选模型都下下来，在同一批样例上实测，用数据决定默认模型。

用法::

    python scripts/compare_models.py            # 下载缺失的候选模型并对比
    python scripts/compare_models.py --no-download
"""

from __future__ import annotations

import argparse
import gc
import shutil
import sys
import tarfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import paths  # noqa: E402

CANDIDATES = [
    (
        "2024-07-17-int8",
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        155.5,
    ),
    (
        "2025-09-09-int8",
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
        158.1,
    ),
    (
        "funasr-nano-int8-2025-12-17",
        "sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17.tar.bz2",
        179.0,
    ),
]

GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
CANDIDATE_DIR = paths.MODELS_DIR / "_candidates"

#: 官方文档给出的 ground truth，用于人工/自动比对
GROUND_TRUTH = {
    "zh": "开放时间早上9点至下午5点。",
    "en": "The tribal chieftain called for the boy and presented him with 50 pieces of code.",
    "yue": "呢几个字都表达唔到我想讲嘅意思",
}


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def download(url: str, dest: Path, expect_mb: float) -> bool:
    """复用 download_models 里带重试/断点续传/进度输出的下载器。

    早期版本这里用了自己写的简易下载，遇到 GitHub 个别资产会**卡死不动**
    （.part 长期为 0 字节），因此改为复用已经验证过的实现。
    """
    from download_models import download as robust_download

    if dest.is_file() and dest.stat().st_size > expect_mb * 0.9 * 1024 * 1024:
        return True
    print(f"    下载 {dest.name} (~{expect_mb:.0f}MB) …")
    result = robust_download(
        [url],
        dest,
        expected_min=int(expect_mb * 0.5 * 1024 * 1024),
        retries=3,
        timeout=45,
    )
    if not result.ok:
        print(f"    下载失败：{result.error}")
    return result.ok


def ensure_candidate(tag: str, tarball: str, size_mb: float) -> Path | None:
    """下载并解压候选模型，返回含 model.int8.onnx 的目录。"""
    target = CANDIDATE_DIR / tag
    if (target / "model.int8.onnx").is_file():
        return target
    archive = CANDIDATE_DIR / tarball
    if not download(f"{GH}/{tarball}", archive, size_mb):
        return None
    staging = CANDIDATE_DIR / f"_x_{tag}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(staging, filter="data")
    except Exception as exc:  # noqa: BLE001
        print(f"    解压失败：{exc}")
        return None
    inner = next((d for d in staging.iterdir() if d.is_dir()), None)
    if inner is None:
        return None
    shutil.copytree(inner, target, dirs_exist_ok=True)
    shutil.rmtree(staging, ignore_errors=True)
    archive.unlink(missing_ok=True)  # 候选包用完即删，省空间
    return target if (target / "model.int8.onnx").is_file() else None


def rss_mb() -> float:
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
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

    c = PMC()
    c.cb = ctypes.sizeof(c)
    ctypes.WinDLL("psapi").GetProcessMemoryInfo(
        ctypes.WinDLL("kernel32").GetCurrentProcess(), ctypes.byref(c), c.cb
    )
    return c.WorkingSetSize / 1024 / 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    import sherpa_onnx

    wav_dir = paths.SENSE_VOICE_DIR / "test_wavs"
    samples = [name for name in ("zh", "en", "yue") if (wav_dir / f"{name}.wav").is_file()]

    models: list[tuple[str, Path]] = []
    for tag, tarball, size_mb in CANDIDATES:
        # 已安装的正式模型直接复用，避免重复下载 158MB
        if tag.startswith("2025-09-09") and paths.sense_voice_model_path().is_file():
            models.append((tag + "（已安装）", paths.SENSE_VOICE_DIR))
            continue
        if args.no_download:
            path = CANDIDATE_DIR / tag
            if (path / "model.int8.onnx").is_file():
                models.append((tag, path))
            continue
        print(f"[准备] {tag}")
        path = ensure_candidate(tag, tarball, size_mb)
        if path is None:
            print(f"    [跳过] {tag} 不可用")
            continue
        models.append((tag, path))

    if not models:
        print("没有可用的候选模型", file=sys.stderr)
        return 1

    print(f"\n候选模型 {len(models)} 个，样例 {samples}\n")
    print("=" * 92)

    for tag, model_dir in models:
        model = str(model_dir / "model.int8.onnx")
        tokens_file = model_dir / "tokens.txt"
        if not tokens_file.is_file():
            tokens_file = paths.sense_voice_tokens_path()
        print(f"\n########## {tag} ##########")

        for itn in (False, True):
            try:
                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=model,
                    tokens=str(tokens_file),
                    num_threads=args.threads,
                    language="auto",
                    use_itn=itn,
                    debug=False,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ITN={itn}: 加载失败 {exc}")
                continue

            rtf_list = []
            for name in samples:
                audio, rate = read_wav(wav_dir / f"{name}.wav")
                # 预热一次
                s = recognizer.create_stream()
                s.accept_waveform(rate, audio)
                recognizer.decode_stream(s)
                gc.collect()
                t0 = time.perf_counter()
                s = recognizer.create_stream()
                s.accept_waveform(rate, audio)
                recognizer.decode_stream(s)
                elapsed = time.perf_counter() - t0
                rtf = elapsed / (audio.size / rate)
                rtf_list.append(rtf)
                text = (s.result.text or "").strip()
                truth = GROUND_TRUTH.get(name, "")
                print(f"  ITN={str(itn):<5} [{name}] RTF={rtf:6.4f}  {text}")
                if truth:
                    print(f"               真值: {truth}")
            if rtf_list:
                print(f"  → 平均 RTF {sum(rtf_list) / len(rtf_list):.4f}  内存 {rss_mb():.0f}MB")
            del recognizer

    print("\n" + "=" * 92)
    print("对比完成。请依据文本质量（尤其是英文大小写与中文是否掉字）选择默认模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
