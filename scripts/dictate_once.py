#!/usr/bin/env python3
"""Phase 2 门禁：麦克风 → 识别 →（可选）注入 的端到端验证。

用法::

    python scripts/dictate_once.py                    # 录 5 秒并打印识别结果
    python scripts/dictate_once.py --seconds 8 --inject   # 识别后注入到当前焦点窗口
    python scripts/dictate_once.py --device 1         # 指定输入设备
    python scripts/dictate_once.py --list-devices     # 列出设备后退出

用法提示：先点开记事本把光标放好，再运行带 ``--inject`` 的命令；
脚本会在开始录音前留 3 秒准备时间。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import inject, paths  # noqa: E402
from vtw.asr import SenseVoiceEngine, verify_model_files  # noqa: E402
from vtw.audio_source import AudioSourceError, MicSource, list_input_devices  # noqa: E402
from vtw.textutil import is_meaningful  # noqa: E402
from vtw.vad import trim_silence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="vtw 麦克风端到端测试")
    parser.add_argument("--seconds", type=float, default=5.0, help="录音时长（秒）")
    parser.add_argument("--inject", action="store_true", help="识别后注入到焦点窗口")
    parser.add_argument("--device", default=None, help="输入设备索引或名称")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--prepare", type=float, default=3.0, help="录音前准备秒数")
    args = parser.parse_args()

    if args.list_devices:
        print("可用输入设备：")
        for dev in list_input_devices():
            print(f"  [{dev['index']}] {dev['name']}  ({dev['channels']}ch @ {dev['default_rate']:.0f}Hz)")
        return 0

    ok, message = verify_model_files()
    if not ok:
        print(f"[失败] {message}", file=sys.stderr)
        return 1

    print("正在加载模型…")
    engine = SenseVoiceEngine(num_threads=args.threads, language="auto", use_itn=False)
    load_started = time.perf_counter()
    engine.load()
    print(f"模型就绪（{time.perf_counter() - load_started:.2f}s）")

    device = args.device
    if device is not None:
        try:
            device = int(device)
        except ValueError:
            pass

    source = MicSource(device=device)
    try:
        source.start()
    except AudioSourceError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    print(f"麦克风：{source.device_name}")
    if args.inject:
        print(f"请把光标放到目标输入框（记事本/微信/浏览器），{args.prepare:.0f} 秒后开始…")
    else:
        print(f"{args.prepare:.0f} 秒后开始录音…")
    for remaining in range(int(args.prepare), 0, -1):
        print(f"  {remaining}…", end="\r", flush=True)
        time.sleep(1)

    print(f"● 录音中（{args.seconds:.0f} 秒）…".ljust(40))
    started = time.perf_counter()
    peak = 0.0
    while time.perf_counter() - started < args.seconds:
        peak = max(peak, source.level)
        time.sleep(0.05)

    audio = source.stop()
    source.close()

    duration = audio.size / 16000.0
    print(f"\n采集完成：{duration:.2f}s，峰值电平 {peak:.3f}")
    if peak < 0.01:
        print("[警告] 几乎没有采集到声音，请检查麦克风是否被静音或选错设备。", file=sys.stderr)

    if audio.size == 0:
        print("[失败] 没有采到音频", file=sys.stderr)
        return 1

    trimmed = trim_silence(audio, sample_rate=16000)
    spoken = trimmed.size / 16000.0
    if trimmed.size > 1600:
        audio = trimmed
    print(f"静音裁剪后：{spoken:.2f}s")

    started = time.perf_counter()
    result = engine.transcribe(audio, 16000)
    wall = time.perf_counter() - started

    print("-" * 60)
    print(f"识别结果：{result.text!r}")
    print(f"推理耗时：{result.elapsed:.3f}s   端到端（含裁剪）：{wall:.3f}s")
    print(f"RTF：{result.rtf:.4f}   语言：{result.lang}")
    print("-" * 60)

    if not is_meaningful(result.text):
        print("[警告] 识别结果为空或无意义（可能没说话，或麦克风电平太低）")
        return 1

    if args.inject:
        print("正在注入到焦点窗口…")
        ok_inject = inject.inject_text(result.text, method="clipboard")
        print("注入成功" if ok_inject else "注入失败")
        return 0 if ok_inject else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
