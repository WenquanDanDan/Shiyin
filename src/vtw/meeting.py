"""会议 / 系统声音转写。

流程：WASAPI 环回采集 → silero VAD 切句 → SenseVoice 逐句识别 →
实时回调给 UI，同时增量写盘。

为什么用 VAD 逐句而不是整段识别：环回是**连续流**，没有"松键"这个天然断点；
而且 SenseVoice 是非流式模型，长音频会显著变慢且容易丢标点。

落盘采用**追加写**：每识别出一句就立即写入，哪怕程序中途被强杀，已经转写的
内容也已经存在磁盘上。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np

from . import paths
from .asr import ModelNotReadyError, SenseVoiceEngine
from .audio_source import (
    MEETING_SOURCE_KINDS,
    MEETING_SOURCE_LABELS,
    AudioSource,
    AudioSourceError,
    create_meeting_source,
)
from .textutil import is_meaningful
from .vad import VadSegmenter, VadUnavailableError

log = logging.getLogger(__name__)

#: 每次从采集缓冲取走的音频时长
_POLL_INTERVAL = 0.1


class MeetingSession:
    """一次会议转写会话。start()/stop() 成对调用。"""

    def __init__(
        self,
        engine: SenseVoiceEngine,
        *,
        vad_options: dict | None = None,
        sample_rate: int = 16000,
        source_kind: str = "loopback",
        loopback_device: object = None,
        mic_device: object = None,
        output_dir: Path | None = None,
        save_audio: bool = False,
        on_segment: Callable[[str, float], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_state: Callable[[bool], None] | None = None,
    ) -> None:
        self.engine = engine
        self.sample_rate = int(sample_rate)
        if source_kind not in MEETING_SOURCE_KINDS:
            log.warning("会议音源 %r 非法，回退 loopback", source_kind)
            source_kind = "loopback"
        self.source_kind = source_kind
        self.loopback_device = loopback_device
        self.mic_device = mic_device
        self.output_dir = output_dir or paths.resolve_output_dir("output")
        self.save_audio = save_audio
        self.on_segment = on_segment
        self.on_error = on_error
        self.on_state = on_state
        vad_options = vad_options or {}

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._source: AudioSource | None = None
        self._segments: list[tuple[str, float]] = []
        self._audio_parts: list[np.ndarray] = []
        self._transcript_path: Path | None = None

    # -- 状态 -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def source_label(self) -> str:
        return MEETING_SOURCE_LABELS.get(self.source_kind, self.source_kind)

    @property
    def source_device(self) -> str:
        return self._source.device_name if self._source is not None else ""

    @property
    def transcript_path(self) -> Path | None:
        return self._transcript_path

    @property
    def segment_count(self) -> int:
        return len(self._segments)

    # -- 控制 -------------------------------------------------------------
    def start(self) -> bool:
        """启动会话。返回是否成功（失败原因通过 on_error 回调给出）。"""
        if self._running:
            return True

        # 先建 VAD 与采集源：这一步最容易失败（缺模型 / 没有环回设备），
        # 失败时应立刻反馈，而不是等到线程里静默挂掉。
        try:
            vad = VadSegmenter(sample_rate=self.sample_rate, **self._vad_kwargs())
        except VadUnavailableError as exc:
            self._fail(str(exc))
            return False

        source = create_meeting_source(
            self.source_kind,
            sample_rate=self.sample_rate,
            loopback_device=self.loopback_device,
            mic_device=self.mic_device,
        )
        try:
            source.start()
        except AudioSourceError as exc:
            self._fail(str(exc))
            return False
        self._source = source

        self._stop_event.clear()
        self._segments = []
        self._audio_parts = []
        self._transcript_path = self._make_transcript_path()
        self._write_header()
        self._running = True

        self._thread = threading.Thread(
            target=self._run, args=(source, vad), name="meeting", daemon=True
        )
        self._thread.start()
        log.info(
            "会议转写已开始（音源：%s，设备：%s），输出：%s",
            self.source_label,
            source.device_name,
            self._transcript_path,
        )
        if self.on_state:
            self.on_state(True)
        return True

    def stop(self) -> Path | None:
        """停止会话并等待落盘完成，返回文字稿路径。"""
        if not self._running and self._thread is None:
            return self._transcript_path

        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=8.0)
            if thread.is_alive():
                log.warning("会议转写线程未在 8 秒内退出，继续收尾")

        source, self._source = self._source, None
        if source is not None:
            try:
                leftover = source.stop()
                if leftover.size:
                    self._audio_parts.append(leftover)
            except Exception as exc:  # noqa: BLE001
                log.warning("停止会议采集失败：%s", exc)

        self._running = False
        if self.save_audio:
            self._save_audio()
        self._write_footer()
        log.info("会议转写已停止，共 %d 段：%s", len(self._segments), self._transcript_path)
        if self.on_state:
            self.on_state(False)
        return self._transcript_path

    # -- 内部 -------------------------------------------------------------
    def set_vad_options(self, options: dict) -> None:
        """设置 VAD 参数（需在 :meth:`start` 之前调用）。"""
        self._vad_opts = dict(options or {})

    def _vad_kwargs(self) -> dict:
        """把配置里的 VAD 参数整理成 :class:`VadSegmenter` 的关键字参数。"""
        raw = getattr(self, "_vad_opts", {}) or {}

        def num(key: str, default: float) -> float:
            try:
                return float(raw.get(key, default))
            except (TypeError, ValueError):
                return default

        return {
            "threshold": num("threshold", 0.5),
            "min_silence_duration": num("min_silence_duration", 0.5),
            "min_speech_duration": num("min_speech_duration", 0.25),
            "max_speech_duration": num("max_speech_duration", 30.0),
        }

    def _run(self, source: AudioSource, vad: VadSegmenter) -> None:
        try:
            while not self._stop_event.is_set():
                time.sleep(_POLL_INTERVAL)
                chunk = source.drain()
                if chunk.size == 0:
                    continue
                self._audio_parts.append(chunk)
                for segment in vad.accept(chunk):
                    self._handle_segment(segment)

            # 收尾：把 VAD 里还没吐出来的尾巴处理掉
            for segment in vad.flush():
                self._handle_segment(segment)
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须上报，否则静默失败
            log.exception("会议转写线程异常退出")
            self._fail(f"转写线程异常：{exc}")
            self._running = False

    def _handle_segment(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        try:
            recognition = self.engine.transcribe(samples, self.sample_rate)
        except ModelNotReadyError as exc:
            self._fail(str(exc))
            self._stop_event.set()
            return
        except Exception as exc:  # noqa: BLE001
            log.error("识别失败：%s", exc)
            return

        if not is_meaningful(recognition.text):
            return

        offset = sum(part.size for part in self._audio_parts) / float(self.sample_rate)
        self._segments.append((recognition.text, offset))
        self._append_line(recognition.text, offset)

        if self.on_segment:
            try:
                self.on_segment(recognition.text, offset)
            except Exception as exc:  # noqa: BLE001 - UI 回调异常不应中断转写
                log.warning("会议回调异常：%s", exc)

    def _make_transcript_path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.output_dir / f"会议-{stamp}.txt"

    def _write_header(self) -> None:
        if self._transcript_path is None:
            return
        try:
            self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._transcript_path, "w", encoding="utf-8") as fh:
                fh.write(f"会议转写记录\n开始时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
                fh.write("=" * 50 + "\n")
        except OSError as exc:
            log.error("创建文字稿失败：%s", exc)

    def _append_line(self, text: str, offset: float) -> None:
        if self._transcript_path is None:
            return
        stamp = f"{int(offset // 60):02d}:{offset % 60:05.2f}"
        try:
            # 逐句追加并立即落盘：即使程序被强杀，已转写内容也不丢
            with open(self._transcript_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {text}\n")
                fh.flush()
        except OSError as exc:
            log.error("写入文字稿失败：%s", exc)

    def _write_footer(self) -> None:
        if self._transcript_path is None:
            return
        try:
            with open(self._transcript_path, "a", encoding="utf-8") as fh:
                fh.write("=" * 50 + "\n")
                fh.write(f"结束时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
                fh.write(f"共 {len(self._segments)} 段\n")
        except OSError as exc:
            log.error("写入文字稿结尾失败：%s", exc)

    def _save_audio(self) -> None:
        if not self._audio_parts or self._transcript_path is None:
            return
        import wave

        audio = np.concatenate(self._audio_parts).astype(np.float32, copy=False)
        target = self._transcript_path.with_suffix(".wav")
        try:
            pcm = np.clip(audio, -1.0, 1.0)
            pcm16 = (pcm * 32767.0).astype("<i2")
            with wave.open(str(target), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(pcm16.tobytes())
            log.info("会议音频已保存：%s", target)
        except (OSError, ValueError) as exc:
            log.error("保存会议音频失败：%s", exc)

    def _fail(self, message: str) -> None:
        log.error("会议转写失败：%s", message)
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:  # noqa: BLE001
                log.exception("错误回调本身抛出异常")
