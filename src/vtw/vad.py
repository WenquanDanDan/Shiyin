"""silero VAD 分段。

用途
----
* **会议/系统声音转写**：环回音频是连续流，必须靠 VAD 切出一句句再送去识别。
* **听写裁剪**：去掉用户按下热键后、开口前的静音，提升识别稳定性。

实现要点
--------
``sherpa_onnx.VoiceActivityDetector`` 要求按固定 ``window_size``（本机为 512 采样
= 32ms）喂数据，因此这里维护一个残余缓冲，凑够一个窗口才提交。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from . import paths

log = logging.getLogger(__name__)


class VadUnavailableError(RuntimeError):
    """VAD 模型缺失或初始化失败。"""


def pick_vad_model() -> str | None:
    """按优先级选择可用的 VAD 模型文件。

    优先 v5：实测 ``silero_vad.onnx`` 在 release 服务器上下载会超时，
    而 ``silero_vad_v5.onnx``（2.2MB）可正常获取。
    """
    for name in ("silero_vad_v5.onnx", "silero_vad.onnx"):
        candidate = paths.VAD_DIR / name
        if candidate.is_file() and candidate.stat().st_size > 1024:
            return str(candidate)
    return None


class VadSegmenter:
    """把连续音频切成一段段语音。"""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence_duration: float = 0.5,
        min_speech_duration: float = 0.25,
        max_speech_duration: float = 30.0,
        num_threads: int = 1,
        buffer_size_in_seconds: int = 120,
        model_path: str | None = None,
    ) -> None:
        import sherpa_onnx

        resolved = model_path or pick_vad_model()
        if not resolved:
            raise VadUnavailableError(
                "未找到 VAD 模型（silero_vad_v5.onnx / silero_vad.onnx）。\n"
                "请运行：python scripts/download_models.py"
            )
        if not Path(resolved).is_file():
            # 显式传入的路径也要校验，否则 sherpa-onnx 会抛出难以理解的 RuntimeError
            raise VadUnavailableError(
                f"VAD 模型文件不存在：{resolved}\n"
                "请检查 config.json，或运行：python scripts/download_models.py"
            )

        self.sample_rate = int(sample_rate)
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = resolved
        config.silero_vad.threshold = float(threshold)
        config.silero_vad.min_silence_duration = float(min_silence_duration)
        config.silero_vad.min_speech_duration = float(min_speech_duration)
        config.silero_vad.max_speech_duration = float(max_speech_duration)
        config.sample_rate = self.sample_rate
        config.num_threads = max(1, int(num_threads))

        self.window_size = int(config.silero_vad.window_size) or 512
        try:
            self._vad: Any = sherpa_onnx.VoiceActivityDetector(
                config, buffer_size_in_seconds=int(buffer_size_in_seconds)
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成可读中文错误
            raise VadUnavailableError(f"VAD 初始化失败：{exc}") from exc
        self._residual = np.zeros(0, dtype=np.float32)
        log.info(
            "VAD 就绪：model=%s window=%d threshold=%.2f min_silence=%.2fs",
            resolved.rsplit("\\", 1)[-1],
            self.window_size,
            threshold,
            min_silence_duration,
        )

    # -- 主接口 -----------------------------------------------------------
    def accept(self, samples: np.ndarray) -> list[np.ndarray]:
        """喂入任意长度音频，返回本次新完成的语音段（可能为空列表）。"""
        if samples is None or len(samples) == 0:
            return []
        buffer = np.concatenate(
            [self._residual, np.asarray(samples, dtype=np.float32).reshape(-1)]
        )

        # 只在有足够一个窗口时才喂，避免 sherpa-onnx 内部断言
        if buffer.size >= self.window_size:
            usable = buffer.size // self.window_size * self.window_size
            for offset in range(0, usable, self.window_size):
                self._vad.accept_waveform(buffer[offset : offset + self.window_size])
            self._residual = buffer[usable:]
        else:
            self._residual = buffer

        return self._drain()

    def flush(self) -> list[np.ndarray]:
        """结束输入：把尾部不足一个窗口的数据补齐后提交，返回剩余语音段。"""
        if self._residual.size:
            padded = np.zeros(self.window_size, dtype=np.float32)
            padded[: self._residual.size] = self._residual[: self.window_size]
            self._vad.accept_waveform(padded)
            self._residual = np.zeros(0, dtype=np.float32)
        try:
            self._vad.flush()
        except Exception as exc:  # noqa: BLE001 - flush 在某些版本实现不一致
            log.debug("VAD flush 异常（忽略）：%s", exc)
        return self._drain()

    def reset(self) -> None:
        self._vad.reset()
        self._residual = np.zeros(0, dtype=np.float32)

    @property
    def is_speech_detected(self) -> bool:
        try:
            return bool(self._vad.is_speech_detected())
        except Exception:  # noqa: BLE001
            return False

    # -- 内部 -------------------------------------------------------------
    def _drain(self) -> list[np.ndarray]:
        segments: list[np.ndarray] = []
        while not self._vad.empty():
            segment = self._vad.front
            samples = np.asarray(segment.samples, dtype=np.float32).reshape(-1)
            self._vad.pop()
            if samples.size:
                segments.append(np.ascontiguousarray(samples))
        return segments


def trim_silence(
    audio: np.ndarray,
    *,
    sample_rate: int = 16000,
    threshold: float | None = None,
    frame_ms: int = 20,
    padding_ms: int = 200,
    leading_padding_ms: int = 800,
) -> np.ndarray:
    """用简单能量阈值裁掉首尾静音（比 VAD 轻量，用于听写后处理）。

    只裁首尾，不动中间 —— 中间停顿属于正常语气，裁掉会破坏语义。

    ``threshold=None`` 时**自适应**：旧版固定用 0.01，麦克风增益低（峰值只有
    0.01~0.05）时会把整段正常语音判成静音，只留下最响的一两个音节，表现为
    "说了半天只识别出一个字"。现在改为按噪声底与峰值动态定阈值。

    开头的 padding 特意留得比结尾长（800ms）：汉语的"开、说、请"等字起音很轻，
    阈值裁剪很容易把第一个音节切掉，导致第一个字识别错误。
    """
    if audio.size == 0:
        return audio
    frame = max(1, int(sample_rate * frame_ms / 1000))
    usable = audio.size // frame * frame
    if usable == 0:
        return audio

    frames = audio[:usable].reshape(-1, frame)
    energy = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    peak = float(energy.max())
    if peak <= 1e-6:  # 纯数字静音
        return np.zeros(0, dtype=np.float32)

    if threshold is None:
        # 10% 分位数近似噪声底；阈值取噪声底的 3 倍或峰值的 5%，取较大者。
        # 再用峰值 40% 兜底，保证很安静但连续的语音不会被整段判成静音。
        noise = float(np.percentile(energy, 10))
        threshold = max(noise * 3.0, peak * 0.05, 1e-4)
        threshold = min(threshold, peak * 0.4)

    loud = np.flatnonzero(energy >= threshold)
    if loud.size == 0:
        return np.zeros(0, dtype=np.float32)

    lead = max(1, int(sample_rate * leading_padding_ms / 1000))
    tail = max(1, int(sample_rate * padding_ms / 1000))
    start = max(0, int(loud[0]) * frame - lead)
    end = min(audio.size, (int(loud[-1]) + 1) * frame + tail)
    return audio[start:end]
