"""SenseVoice 识别引擎封装。

设计要点
--------
* **单例 + 懒加载**：模型加载约需 1~2 秒、占用数百 MB 内存，只在首次真正
  需要时加载，并用锁保护，避免多个线程同时加载。
* **后台预热**：启动后延迟 ``warmup_delay_seconds`` 秒再加载，既避免拖慢登录，
  又保证用户第一次按热键时无需等待。
* **空闲卸载**：``idle_unload_seconds > 0`` 时，空闲超时后释放模型，
  内存回落到 Python 基线（约 120MB），代价是下次识别多约 1.5 秒。
* **结果清洗**：不做小写化（官方示例的 ``.lower()`` 是英文场景写法）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import paths
from .audio_source import normalize_peak
from .textutil import clean_text

log = logging.getLogger(__name__)


class ModelNotReadyError(RuntimeError):
    """模型文件缺失或加载失败。"""


@dataclass
class Recognition:
    """一次识别结果。"""

    text: str
    duration: float = 0.0  # 音频时长（秒）
    elapsed: float = 0.0  # 推理耗时（秒）
    lang: str = ""
    emotion: str = ""
    event: str = ""

    @property
    def rtf(self) -> float:
        """实时率 = 推理耗时 / 音频时长。越小越快。"""
        return self.elapsed / self.duration if self.duration > 0 else 0.0


def verify_model_files() -> tuple[bool, str]:
    """启动时校验模型是否就绪；返回 (是否就绪, 中文说明)。"""
    model = paths.sense_voice_model_path()
    tokens = paths.sense_voice_tokens_path()

    if not model.is_file() or not tokens.is_file():
        missing = [str(p) for p in (model, tokens) if not p.is_file()]
        return False, "模型缺失：" + "、".join(missing) + "\n请运行：python scripts/download_models.py"

    size = model.stat().st_size
    if size < 100 * 1024 * 1024:
        return False, (
            f"模型文件不完整（{size / 1024 / 1024:.1f}MB，期望 ≥100MB）：\n{model}\n"
            "请重新下载：python scripts/download_models.py --force"
        )
    return True, "模型文件校验通过"


class SenseVoiceEngine:
    """线程安全的 SenseVoice 识别器包装。"""

    def __init__(
        self,
        *,
        num_threads: int = 4,
        language: str = "auto",
        use_itn: bool = True,
        idle_unload_seconds: int = 0,
    ) -> None:
        self.num_threads = max(1, int(num_threads))
        self.language = language or "auto"
        self.use_itn = bool(use_itn)
        self.idle_unload_seconds = max(0, int(idle_unload_seconds))

        self._recognizer: Any | None = None
        self._load_lock = threading.Lock()
        self._decode_lock = threading.Lock()
        self._last_used = 0.0
        self._load_count = 0

    # -- 生命周期 ---------------------------------------------------------
    @property
    def is_loaded(self) -> bool:
        return self._recognizer is not None

    def load(self) -> Any:
        """加载（或复用）识别器。线程安全。"""
        existing = self._recognizer
        if existing is not None:
            return existing

        with self._load_lock:
            if self._recognizer is not None:  # 双检：可能已被其它线程加载
                return self._recognizer

            ok, message = verify_model_files()
            if not ok:
                raise ModelNotReadyError(message)

            import sherpa_onnx  # 延迟导入：加快进程冷启动

            model_path = str(paths.sense_voice_model_path())
            tokens_path = str(paths.sense_voice_tokens_path())
            started = time.perf_counter()
            log.info(
                "加载 SenseVoice 模型：threads=%d language=%s itn=%s",
                self.num_threads,
                self.language,
                self.use_itn,
            )
            try:
                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=model_path,
                    tokens=tokens_path,
                    num_threads=self.num_threads,
                    language=self.language,
                    use_itn=self.use_itn,
                    debug=False,
                )
            except Exception as exc:  # noqa: BLE001 - 转成可读中文错误
                log.exception("SenseVoice 模型加载失败")
                raise ModelNotReadyError(f"模型加载失败：{exc}") from exc

            self._recognizer = recognizer
            self._load_count += 1
            self._last_used = time.monotonic()
            log.info("SenseVoice 加载完成，耗时 %.2fs", time.perf_counter() - started)
            return recognizer

    def unload(self) -> None:
        """释放模型以回收内存。"""
        with self._load_lock:
            if self._recognizer is not None:
                self._recognizer = None
                log.info("已卸载 SenseVoice 模型（空闲回收）")
        import gc

        gc.collect()

    def maybe_unload_if_idle(self) -> bool:
        """供定时器调用：空闲超时则卸载。返回是否执行了卸载。"""
        if self.idle_unload_seconds <= 0 or self._recognizer is None:
            return False
        if time.monotonic() - self._last_used < self.idle_unload_seconds:
            return False
        with self._load_lock:
            if self._recognizer is None:
                return False
            if time.monotonic() - self._last_used < self.idle_unload_seconds:
                return False
            self._recognizer = None
        import gc

        gc.collect()
        log.info("模型已因空闲 %ds 卸载", self.idle_unload_seconds)
        return True

    def warmup(self) -> bool:
        """后台预热；失败只记日志，不抛出（预热不是关键路径）。"""
        try:
            self.load()
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("模型预热失败：%s", exc)
            return False

    # -- 识别 -------------------------------------------------------------
    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        *,
        lowercase: bool = False,
        trailing_space: bool = False,
        english_sentence_case: bool = True,
        spoken_punctuation: bool = True,
    ) -> Recognition:
        """识别一段 float32 单声道音频（取值范围 [-1, 1]）。

        识别前会做一次峰值归一化：麦克风增益低（峰值 0.01~0.05）时，
        SenseVoice 的识别率会明显下降，归一化后再送模型可避免"只识别出一个字"。
        """
        if audio is None or audio.size == 0:
            return Recognition(text="")

        recognizer = self.load()
        samples = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        samples = normalize_peak(samples)
        duration = samples.size / float(sample_rate)

        # sherpa-onnx 的 C++ 层不是线程安全的，用锁串行化
        with self._decode_lock:
            started = time.perf_counter()
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
            elapsed = time.perf_counter() - started

        result = stream.result
        raw_text = getattr(result, "text", "") or ""
        text = clean_text(
            raw_text,
            lowercase=lowercase,
            trailing_space=trailing_space,
            english_sentence_case=english_sentence_case,
            spoken_punctuation=spoken_punctuation,
        )

        self._last_used = time.monotonic()
        recognition = Recognition(
            text=text,
            duration=duration,
            elapsed=elapsed,
            lang=_tag_value(getattr(result, "lang", "")),
            emotion=_tag_value(getattr(result, "emotion", "")),
            event=_tag_value(getattr(result, "event", "")),
        )
        log.debug(
            "识别完成：%.2fs 音频 / %.3fs 推理 / RTF=%.3f -> %r",
            recognition.duration,
            recognition.elapsed,
            recognition.rtf,
            recognition.text,
        )
        return recognition


def _tag_value(value: Any) -> str:
    """把 ``<|zh|>`` 这类标签值转成 ``zh``。"""
    if not isinstance(value, str):
        return ""
    return value.strip().strip("<|>").strip("|")
