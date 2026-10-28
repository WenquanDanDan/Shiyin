"""音频文件转写：把本地音频文件切成句子后逐句识别并落盘。

与会议转写的区别：输入是**文件**而不是实时采集流，因此：

* 支持常见格式：WAV 用标准库直接读；MP3/M4A/FLAC/OGG 等由可选依赖
  `PyAV`（FFmpeg）解码。没装 PyAV 时仍可转写 WAV，其它格式给出中文提示。
* 按约 0.5 秒一块**流式解码**，不会把整段音频读进内存（一小时的会议录音
  也只有几十 KB 的临时块）。
* 支持取消；每识别完一句就立即追加写入文字稿，取消或崩溃都不丢已转写内容。
"""

from __future__ import annotations

import importlib.util
import logging
import re
import threading
import time
import wave
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import numpy as np

from . import paths
from .asr import ModelNotReadyError, SenseVoiceEngine
from .audio_source import resample
from .textutil import is_meaningful
from .vad import VadSegmenter, VadUnavailableError

log = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000

#: 每次解码的音频块时长（秒）
BLOCK_SECONDS = 0.5

#: 文件选择框里的过滤器
AUDIO_FILE_FILETYPES = [
    (
        "音频文件",
        "*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.wma *.aiff *.aif *.mp4 *.mkv",
    ),
    ("WAV 波形", "*.wav"),
    ("所有文件", "*.*"),
]

_AV_MISSING_MESSAGE = (
    "暂不支持这种格式：本机没有安装音频解码库 PyAV。\n"
    "可以：1) 先把文件转成 WAV 再试；"
    "2) 安装 PyAV：.venv\\Scripts\\python.exe -m pip install av"
)

_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_AV_AVAILABLE: bool | None = None


class AudioFileError(RuntimeError):
    """音频文件不存在、损坏或格式无法解码。"""


# --------------------------------------------------------------------------
# 文件名与文字稿路径
# --------------------------------------------------------------------------
def sanitize_stem(stem: str, *, limit: int = 60) -> str:
    """把源文件名清理成安全的文字稿前缀。"""
    name = _ILLEGAL_FILENAME.sub("_", stem or "").strip(" ._")
    if not name:
        name = "音频"
    return name[:limit]


def make_transcript_path(
    output_dir: Path | str, source_path: Path | str, when: datetime | None = None
) -> Path:
    """生成 ``转写-<源文件名>-<时间戳>.txt`` 的落盘路径。"""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    stem = sanitize_stem(Path(source_path).stem)
    return Path(output_dir) / f"转写-{stem}-{stamp}.txt"


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------
def av_available() -> bool:
    """本机是否安装了 PyAV（用于 MP3/M4A 等非 WAV 格式）。"""
    global _AV_AVAILABLE
    if _AV_AVAILABLE is None:
        try:
            _AV_AVAILABLE = importlib.util.find_spec("av") is not None
        except (ImportError, ValueError):
            _AV_AVAILABLE = False
    return _AV_AVAILABLE


def _wav_info(path: Path) -> tuple[int, int, int, int]:
    with wave.open(str(path), "rb") as fh:
        return fh.getnchannels(), fh.getsampwidth(), fh.getframerate(), fh.getnframes()


def _pcm_bytes_to_float(raw: bytes, width: int) -> np.ndarray:
    if width == 1:
        # 8-bit WAV 是无符号的
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if width == 3:
        data = np.frombuffer(raw, dtype=np.uint8)
        count = data.size // 3
        data = data[: count * 3].reshape(-1, 3).astype(np.int32)
        value = data[:, 0] | (data[:, 1] << 8) | (data[:, 2] << 16)
        value = np.where(value >= 0x800000, value - 0x1000000, value)
        return value.astype(np.float32) / 8388608.0
    if width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise AudioFileError(f"不支持的 WAV 位深：{width * 8} bit")


def _iter_wav(path: Path, target_rate: int, block_seconds: float) -> Iterator[np.ndarray]:
    try:
        handle = wave.open(str(path), "rb")
    except Exception as exc:  # noqa: BLE001 - 统一转成可读中文错误
        raise AudioFileError(f"无法读取 WAV 文件：{exc}") from exc

    with handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        if rate <= 0:
            raise AudioFileError("WAV 文件采样率无效")
        frames_per_block = max(1, int(rate * block_seconds))
        while True:
            raw = handle.readframes(frames_per_block)
            if not raw:
                break
            data = _pcm_bytes_to_float(raw, width)
            if channels > 1:
                usable = (data.size // channels) * channels
                data = data[:usable].reshape(-1, channels).mean(axis=1)
            if rate != target_rate:
                data = resample(data.astype(np.float32, copy=False), rate, target_rate)
            if data.size:
                yield np.ascontiguousarray(data, dtype=np.float32)


def _iter_av(path: Path, target_rate: int) -> Iterator[np.ndarray]:
    try:
        import av
    except Exception as exc:  # noqa: BLE001 - 依赖缺失
        raise AudioFileError(_AV_MISSING_MESSAGE) from exc

    try:
        container = av.open(str(path))
    except Exception as exc:  # noqa: BLE001 - 损坏/不支持
        raise AudioFileError(f"无法打开音频文件：{exc}") from exc

    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise AudioFileError("文件里没有音频轨道（可能是纯视频或损坏文件）")

        resampler = av.AudioResampler(format="flt", layout="mono", rate=target_rate)
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame) or []:
                data = np.asarray(resampled.to_ndarray(), dtype=np.float32).reshape(-1)
                if data.size:
                    yield np.clip(data, -1.0, 1.0)
        try:
            tail = resampler.resample(None) or []
        except Exception:  # noqa: BLE001 - 某些 FFmpeg 版本 flush 行为不同
            tail = []
        for resampled in tail:
            data = np.asarray(resampled.to_ndarray(), dtype=np.float32).reshape(-1)
            if data.size:
                yield np.clip(data, -1.0, 1.0)
    finally:
        container.close()


def _av_duration(path: Path) -> float:
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration:
                return float(container.duration) / float(av.time_base)
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is not None and stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
    except Exception as exc:  # noqa: BLE001 - 时长只用于进度条，失败不致命
        log.debug("读取音频时长失败：%s", exc)
    return 0.0


def _backend(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            _wav_info(path)
            return "wav"
        except Exception as wav_error:  # noqa: BLE001 - 交给 PyAV 再试
            if av_available():
                log.info("标准库无法解码该 WAV（%s），改用 PyAV", wav_error)
                return "av"
            raise AudioFileError(
                f"无法读取 WAV 文件：{wav_error}\n"
                "如果是 24-bit/浮点/扩展格式，请安装 PyAV，或先转成 16-bit PCM WAV。"
            ) from wav_error

    if av_available():
        return "av"

    raise AudioFileError(
        f"暂不支持 {suffix or '该'} 格式：本机没有安装音频解码库 PyAV。\n"
        "可以：1) 先把文件转成 WAV 再试；"
        "2) 安装 PyAV：.venv\\Scripts\\python.exe -m pip install av"
    )


def audio_duration(path: Path | str) -> float:
    """返回音频时长（秒）；无法读取时返回 0。"""
    source = Path(path)
    if not source.is_file():
        raise AudioFileError(f"文件不存在：{source}")
    backend = _backend(source)
    if backend == "wav":
        _, _, rate, frames = _wav_info(source)
        return frames / rate if rate else 0.0
    return _av_duration(source)


def iter_audio_blocks(
    path: Path | str,
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
    block_seconds: float = BLOCK_SECONDS,
) -> Iterator[np.ndarray]:
    """流式读取音频，按块产出 float32 单声道 16kHz 采样。"""
    source = Path(path)
    if not source.is_file():
        raise AudioFileError(f"文件不存在：{source}")
    backend = _backend(source)
    if backend == "wav":
        yield from _iter_wav(source, int(sample_rate), float(block_seconds))
    else:
        yield from _iter_av(source, int(sample_rate))


def load_audio_file(
    path: Path | str, *, sample_rate: int = TARGET_SAMPLE_RATE
) -> np.ndarray:
    """把整个文件读成一段 float32 单声道音频（测试与小文件用；大文件请用迭代器）。"""
    blocks = list(iter_audio_blocks(path, sample_rate=sample_rate))
    if not blocks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(blocks).astype(np.float32, copy=False)


# --------------------------------------------------------------------------
# 转写会话
# --------------------------------------------------------------------------
class FileTranscriber:
    """一次文件转写任务。``start()`` 起后台线程，``cancel()`` 可随时取消。"""

    def __init__(
        self,
        engine: SenseVoiceEngine,
        *,
        sample_rate: int = TARGET_SAMPLE_RATE,
        vad_options: dict | None = None,
        transcribe_options: dict | None = None,
        output_dir: Path | None = None,
        block_seconds: float = BLOCK_SECONDS,
        on_segment: Callable[[str, float], None] | None = None,
        on_progress: Callable[[float, float, int], None] | None = None,
        on_finished: Callable[[Path, int, float, bool], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.engine = engine
        self.sample_rate = int(sample_rate)
        self.vad_options = dict(vad_options or {})
        self.transcribe_options = dict(transcribe_options or {})
        self.output_dir = Path(output_dir) if output_dir else paths.resolve_output_dir("output")
        self.block_seconds = float(block_seconds)
        self.on_segment = on_segment
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.on_error = on_error

        self.source_path: Path | None = None
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._running = False
        self._segments = 0
        self._duration = 0.0
        self._transcript_path: Path | None = None

    # -- 状态 -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def segment_count(self) -> int:
        return self._segments

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def transcript_path(self) -> Path | None:
        return self._transcript_path

    # -- 控制 -------------------------------------------------------------
    def start(self, path: Path | str) -> bool:
        """开始转写。立即返回；完成/失败通过回调通知。"""
        if self._running:
            return False
        source = Path(path)
        if not source.is_file():
            self._fail(f"文件不存在：{source}")
            return False

        self.source_path = source
        self._cancel.clear()
        self._segments = 0
        self._duration = 0.0
        self._transcript_path = make_transcript_path(self.output_dir, source)
        self._write_header()
        self._running = True
        self._thread = threading.Thread(target=self._run, name="file-transcribe", daemon=True)
        self._thread.start()
        log.info("开始转写文件：%s → %s", source, self._transcript_path)
        return True

    def cancel(self) -> None:
        """请求取消；当前这一句识别完后停止。"""
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- 内部 -------------------------------------------------------------
    def _run(self) -> None:
        processed = 0
        duration = 0.0
        failed = False
        last_progress = 0.0
        try:
            duration = audio_duration(self.source_path)  # type: ignore[arg-type]
            self._duration = duration
            vad = VadSegmenter(sample_rate=self.sample_rate, **self._vad_kwargs())

            for block in iter_audio_blocks(
                self.source_path,  # type: ignore[arg-type]
                sample_rate=self.sample_rate,
                block_seconds=self.block_seconds,
            ):
                if self._cancel.is_set():
                    break
                processed += block.size
                for segment in vad.accept(block):
                    if self._cancel.is_set():
                        break
                    self._handle_segment(segment, processed)

                now = time.monotonic()
                if now - last_progress >= 0.5:
                    last_progress = now
                    self._emit_progress(processed, duration)

            if not self._cancel.is_set():
                for segment in vad.flush():
                    self._handle_segment(segment, processed)
        except (AudioFileError, VadUnavailableError, ModelNotReadyError) as exc:
            failed = True
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须上报
            failed = True
            log.exception("文件转写线程异常")
            self._fail(f"转写失败：{exc}")
        finally:
            self._write_footer()
            self._running = False

        if failed:
            return

        self._emit_progress(processed, duration)
        if self.on_finished is not None:
            try:
                self.on_finished(
                    self._transcript_path or Path(""),  # type: ignore[arg-type]
                    self._segments,
                    duration,
                    self._cancel.is_set(),
                )
            except Exception:  # noqa: BLE001
                log.exception("文件转写完成回调异常")
        log.info(
            "文件转写结束：%d 段，取消=%s，输出：%s",
            self._segments,
            self._cancel.is_set(),
            self._transcript_path,
        )

    def _vad_kwargs(self) -> dict:
        raw = self.vad_options

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

    def _handle_segment(self, samples: np.ndarray, processed: int) -> None:
        if samples.size == 0:
            return
        try:
            result = self.engine.transcribe(
                samples, self.sample_rate, **self.transcribe_options
            )
        except ModelNotReadyError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单句失败不中断整个文件
            log.error("识别失败：%s", exc)
            return

        if not is_meaningful(result.text):
            return

        # 该句的起点 ≈ 已解码总长 - 本句长度（VAD 还会含一点尾静音，只用于排序）
        offset = max(0.0, (processed - samples.size) / float(self.sample_rate))
        self._segments += 1
        self._append_line(result.text, offset)
        if self.on_segment is not None:
            try:
                self.on_segment(result.text, offset)
            except Exception as exc:  # noqa: BLE001 - UI 回调异常不影响转写
                log.warning("文件转写回调异常：%s", exc)

    def _emit_progress(self, processed: int, duration: float) -> None:
        if self.on_progress is None:
            return
        try:
            self.on_progress(
                processed / float(self.sample_rate),
                duration,
                self._segments,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("进度回调异常：%s", exc)

    def _write_header(self) -> None:
        if self._transcript_path is None:
            return
        try:
            self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._transcript_path, "w", encoding="utf-8") as fh:
                fh.write("文件转写记录\n")
                fh.write(f"源文件：{self.source_path}\n")
                fh.write(f"开始时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
                fh.write("=" * 50 + "\n")
        except OSError as exc:
            log.error("创建文字稿失败：%s", exc)

    def _append_line(self, text: str, offset: float) -> None:
        if self._transcript_path is None:
            return
        stamp = f"{int(offset // 60):02d}:{offset % 60:05.2f}"
        try:
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
                fh.write(f"共 {self._segments} 段\n")
        except OSError as exc:
            log.error("写入文字稿结尾失败：%s", exc)

    def _fail(self, message: str) -> None:
        log.error("文件转写失败：%s", message)
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:  # noqa: BLE001
                log.exception("错误回调本身抛出异常")
