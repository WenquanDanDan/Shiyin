"""音频采集：麦克风与系统声音（WASAPI 环回）。

统一接口
--------
所有采集源都实现 :class:`AudioSource`：:meth:`start` 开始、:meth:`stop` 停止并
返回**float32 单声道 16kHz、取值 [-1, 1]** 的完整音频。内部由各自的后台线程
累积帧数据，UI 线程只读 :attr:`level` 显示电平。

后端选择（实测结论）
--------------------
* 麦克风 —— ``sounddevice``（自带 PortAudio）。WASAPI 共享模式下 PortAudio 会
  自动做采样率转换，可直接以 16kHz 打开设备。
* 系统声音 —— **不能用 sounddevice**：其 ``WasapiSettings`` 没有 ``loopback``
  参数（已读源码确认）。首选 ``soundcard``；若在本机声卡上失败，回退
  ``pyaudiowpatch`` 采原生采样率再用 ``soxr`` 重采样到 16kHz。
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod

import numpy as np

log = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000

#: 会议转写可选音源
MEETING_SOURCE_KINDS = ("loopback", "mic", "both")

#: 会议音源的中文展示名
MEETING_SOURCE_LABELS = {
    "loopback": "系统声音",
    "mic": "麦克风",
    "both": "系统声音 + 麦克风",
}


class AudioSourceError(RuntimeError):
    """采集设备不可用。"""


# --------------------------------------------------------------------------
# 基类
# --------------------------------------------------------------------------
class AudioSource(ABC):
    """采集源基类。"""

    #: 供 UI 显示的人可读设备名
    device_name: str = ""

    def __init__(self, sample_rate: int = TARGET_SAMPLE_RATE) -> None:
        self.sample_rate = int(sample_rate)
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._running = False
        self._level = 0.0  # 最近一块的 RMS，供浮条画电平

    # -- 对外接口 ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def level(self) -> float:
        """0.0~1.0 的粗略电平，用于 UI 反馈。"""
        return self._level

    def start(self) -> None:
        if self._running:
            return
        with self._lock:
            self._frames = []
        self._level = 0.0
        self._start_backend()
        self._running = True

    def stop(self) -> np.ndarray:
        """停止采集并返回全部音频。未在采集时返回空数组。"""
        if not self._running:
            return np.zeros(0, dtype=np.float32)
        self._running = False
        try:
            self._stop_backend()
        finally:
            self._level = 0.0
        with self._lock:
            frames = self._frames
            self._frames = []
        return concat_frames(frames)

    def drain(self) -> np.ndarray:
        """取出并清空已采集的音频，但**继续采集**。

        用于流式场景（会议转写）：外层循环按 100ms 节奏取走新音频喂给 VAD，
        而不必等整个会话结束。
        """
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            frames = self._frames
            self._frames = []
        return concat_frames(frames)

    def close(self) -> None:
        """彻底释放资源。"""
        if self._running:
            self.stop()

    # -- 子类实现 ---------------------------------------------------------
    @abstractmethod
    def _start_backend(self) -> None: ...

    @abstractmethod
    def _stop_backend(self) -> None: ...

    # -- 内部工具 ---------------------------------------------------------
    def _append(self, chunk: np.ndarray) -> None:
        """把一块音频追加进缓冲。会把任意形状压成单声道 float32。"""
        if chunk is None:
            return
        mono = to_mono_float32(chunk)
        if mono.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
        self._level = min(1.0, rms * 4.0)  # ×4 让浮条更灵敏
        if self._running:
            with self._lock:
                self._frames.append(mono)


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def to_mono_float32(data: np.ndarray) -> np.ndarray:
    """把任意 (frames,) 或 (frames, channels) 数组转成单声道 float32。"""
    arr = np.asarray(data)
    if arr.ndim == 2:
        if arr.shape[1] > 1:
            arr = arr.mean(axis=1)  # 多声道降混
        else:
            arr = arr[:, 0]
    arr = np.ascontiguousarray(arr, dtype=np.float32).reshape(-1)
    # 越界裁剪：防止重采样/增益引入的过冲导致识别异常
    return np.clip(arr, -1.0, 1.0)


def concat_frames(frames: list[np.ndarray]) -> np.ndarray:
    if not frames:
        return np.zeros(0, dtype=np.float32)
    if len(frames) == 1:
        return frames[0]
    return np.concatenate(frames).astype(np.float32, copy=False)


def mix_audio(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """按起始位置叠加两路等采样率的单声道音频（长度不同时短的一路补零）。

    两路采集线程按墙钟各自累积数据，同一轮 :meth:`AudioSource.drain` 取出的块
    在时间上基本对齐，长度差通常只有几毫秒，因此直接从头叠加即可，不必重采样。
    叠加后可能超过 [-1, 1]，统一裁剪，避免送进识别模型时溢出。
    """
    first = np.asarray(first, dtype=np.float32).reshape(-1)
    second = np.asarray(second, dtype=np.float32).reshape(-1)
    if first.size == 0:
        return second if second.size else np.zeros(0, dtype=np.float32)
    if second.size == 0:
        return first
    size = max(first.size, second.size)
    out = np.zeros(size, dtype=np.float32)
    out[: first.size] += first
    out[: second.size] += second
    return np.clip(out, -1.0, 1.0)


def resample(audio: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    """重采样到目标采样率。优先用 soxr（高质量、快），退化时用线性插值。"""
    if in_rate == out_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    try:
        import soxr

        return np.ascontiguousarray(soxr.resample(audio, in_rate, out_rate), dtype=np.float32)
    except Exception as exc:  # noqa: BLE001 - 回退路径，不应中断采集
        log.warning("soxr 重采样不可用（%s），改用线性插值", exc)
        n_out = int(round(audio.size * out_rate / in_rate))
        if n_out <= 0:
            return np.zeros(0, dtype=np.float32)
        x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)


def normalize_peak(
    audio: np.ndarray, *, target: float = 0.9, max_gain: float = 20.0
) -> np.ndarray:
    """把整体音量偏小的音频线性放大到 ``target`` 附近（只放大、不衰减）。

    有些麦克风增益很低（峰值只有 0.01~0.05），SenseVoice 在这么小的幅度上
    识别率会明显下降；识别前统一做一次峰值归一化可以救回来。限制 ``max_gain``
    倍是为了避免把底噪放大成"语音"。
    """
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return samples
    peak = float(np.abs(samples).max())
    if peak <= 1e-6 or peak >= target:
        return samples
    gain = min(max_gain, target / peak)
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32, copy=False)


# --------------------------------------------------------------------------
# 麦克风
# --------------------------------------------------------------------------
class MicSource(AudioSource):
    """麦克风采集（sounddevice / PortAudio）。"""

    def __init__(self, device: object = None, sample_rate: int = TARGET_SAMPLE_RATE) -> None:
        super().__init__(sample_rate)
        self.device = None if device in (None, "", "default") else device
        self._stream = None
        self._overflow_count = 0

    def _start_backend(self) -> None:
        import sounddevice as sd

        try:
            info = sd.query_devices(self.device, "input")
            self.device_name = str(info.get("name", "未知麦克风"))
        except Exception:  # noqa: BLE001 - 设备名仅用于显示
            self.device_name = "默认麦克风"

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            if status:
                self._overflow_count += 1
                if self._overflow_count <= 3:
                    log.warning("麦克风采集状态异常：%s", status)
            self._append(indata)

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                device=self.device,
                blocksize=0,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            self._stream = None
            raise AudioSourceError(
                f"无法打开麦克风（{self.device_name}）：{exc}\n"
                "请检查：系统设置 → 隐私和安全性 → 麦克风，是否允许桌面应用访问。"
            ) from exc
        log.info("麦克风已开始采集：%s @ %dHz", self.device_name, self.sample_rate)

    def _stop_backend(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("关闭麦克风流失败：%s", exc)
        log.info("麦克风已停止采集")


def list_input_devices() -> list[dict]:
    """列出可用的输入设备，供配置界面/排错使用。"""
    try:
        import sounddevice as sd

        out = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                out.append(
                    {
                        "index": idx,
                        "name": dev.get("name", ""),
                        "channels": dev.get("max_input_channels", 0),
                        "default_rate": dev.get("default_samplerate", 0),
                    }
                )
        return out
    except Exception as exc:  # noqa: BLE001
        log.error("枚举输入设备失败：%s", exc)
        return []


# --------------------------------------------------------------------------
# 系统声音（环回）
# --------------------------------------------------------------------------
class LoopbackSource(AudioSource):
    """系统声音采集：优先 soundcard，失败回退 pyaudiowpatch。"""

    def __init__(self, device: object = None, sample_rate: int = TARGET_SAMPLE_RATE) -> None:
        super().__init__(sample_rate)
        self.device = device
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._backend = ""

    def _start_backend(self) -> None:
        errors: list[str] = []

        try:
            self._start_soundcard()
            self._backend = "soundcard"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"soundcard: {exc}")
            log.warning("soundcard 环回不可用（%s），尝试 pyaudiowpatch", exc)
            try:
                self._start_pyaudio()
                self._backend = "pyaudiowpatch"
            except Exception as exc2:  # noqa: BLE001
                errors.append(f"pyaudiowpatch: {exc2}")
                raise AudioSourceError(
                    "无法采集系统声音：\n  " + "\n  ".join(errors) + "\n"
                    "请确认当前有正在播放/可用的输出设备（扬声器或耳机）。"
                ) from exc2

        log.info("系统声音采集已启动：后端=%s 设备=%s", self._backend, self.device_name)

    # -- soundcard --------------------------------------------------------
    def _start_soundcard(self) -> None:
        import soundcard as sc

        if self.device in (None, "", "default"):
            speaker = sc.default_speaker()
        else:
            speaker = self.device  # 允许传入 soundcard 的 speaker 对象
        if speaker is None:
            raise AudioSourceError("没有找到默认输出设备")

        loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        if loopback is None:
            raise AudioSourceError(f"扬声器 {speaker.name!r} 没有对应的环回设备")
        self.device_name = f"{speaker.name}（环回）"

        recorder = loopback.recorder(samplerate=self.sample_rate, channels=1)
        handle = recorder.__enter__()
        self._recorder_ctx = recorder
        self._stop_event.clear()

        def pump() -> None:
            # 每次 20ms，保证 100ms 级的响应，同时 CPU 占用极低
            block = max(1, int(self.sample_rate * 0.02))
            while not self._stop_event.is_set():
                try:
                    data = handle.record(numframes=block)
                except Exception as exc:  # noqa: BLE001
                    if not self._stop_event.is_set():
                        log.error("环回采集读取失败：%s", exc)
                    break
                self._append(np.asarray(data, dtype=np.float32))

        self._thread = threading.Thread(target=pump, name="loopback-pump", daemon=True)
        self._thread.start()

    # -- pyaudiowpatch ----------------------------------------------------
    def _start_pyaudio(self) -> None:
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        self._pa = pa
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not wasapi.get("isLoopbackDevice", False):
                # 找到该输出设备对应的环回设备
                loopback = None
                for idx in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(idx)
                    if info.get("isLoopbackDevice") and (
                        info.get("name", "").startswith(default_out.get("name", "\x00"))
                    ):
                        loopback = info
                        break
                if loopback is None:
                    raise AudioSourceError("未找到 WASAPI 环回设备")
            else:
                loopback = default_out
        except Exception:
            pa.terminate()
            raise

        native_rate = int(loopback.get("defaultSampleRate", 48000))
        channels = max(1, int(loopback.get("maxInputChannels", 2)))
        self.device_name = f"{loopback.get('name', '系统声音')}（环回）"
        self._native_rate = native_rate
        self._stop_event.clear()

        def callback(in_data, frame_count, time_info, status):  # noqa: ANN001
            if status:
                log.debug("环回采集状态：%s", status)
            raw = np.frombuffer(in_data, dtype=np.float32)
            if channels > 1:
                raw = raw.reshape(-1, channels)
            self._append(raw)
            return (None, pyaudio.paContinue)

        self._stream = pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=native_rate,
            input=True,
            input_device_index=int(loopback["index"]),
            frames_per_buffer=1024,
            stream_callback=callback,
        )
        self._stream.start_stream()

    def _stop_backend(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        stream = getattr(self, "_stream", None)
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("关闭环回流失败：%s", exc)
            self._stream = None

        pa = getattr(self, "_pa", None)
        if pa is not None:
            try:
                pa.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._pa = None

        ctx = getattr(self, "_recorder_ctx", None)
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                log.warning("关闭 soundcard 录制器失败：%s", exc)
            self._recorder_ctx = None

        # pyaudiowpatch 采的是原生采样率，这里统一重采样到 16kHz
        native = getattr(self, "_native_rate", None)
        if native and native != self.sample_rate:
            with self._lock:
                frames = self._frames
                self._frames = []
            merged = concat_frames(frames)
            if merged.size:
                with self._lock:
                    self._frames = [resample(merged, native, self.sample_rate)]
        log.info("系统声音采集已停止")


# --------------------------------------------------------------------------
# 会议音源
# --------------------------------------------------------------------------
class MixedAudioSource(AudioSource):
    """把两路音频实时混合成一路，用于「系统声音 + 麦克风」。

    典型场景：戴耳机开在线会议 —— 系统声音里有对方，麦克风里有你自己。
    注意：外放（不戴耳机）时麦克风会再次拾取扬声器的声音，同一句话可能被
    录两遍，因此「两者」建议只在戴耳机时使用。
    """

    def __init__(self, first: AudioSource, second: AudioSource) -> None:
        if first.sample_rate != second.sample_rate:
            raise ValueError(
                f"无法混合不同采样率的音源：{first.sample_rate} != {second.sample_rate}"
            )
        super().__init__(first.sample_rate)
        self.first = first
        self.second = second
        self._leftover = np.zeros(0, dtype=np.float32)

    @property
    def level(self) -> float:
        return max(self.first.level, self.second.level)

    def start(self) -> None:
        if self._running:
            return
        self.first.start()
        try:
            self.second.start()
        except Exception:
            # 第二路失败时必须回滚第一路，否则会留下一个无人关闭的采集线程
            try:
                self.first.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("回滚第一路音源失败：%s", exc)
            raise
        self._running = True
        self.device_name = f"{self.first.device_name} + {self.second.device_name}"

    def stop(self) -> np.ndarray:
        if not self._running:
            return np.zeros(0, dtype=np.float32)
        self._running = False
        first_audio = np.zeros(0, dtype=np.float32)
        second_audio = np.zeros(0, dtype=np.float32)
        # 两路都要尝试停止：一路出错不能把另一路丢在后台继续录
        try:
            first_audio = self.first.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("停止第一路音源失败：%s", exc)
        try:
            second_audio = self.second.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("停止第二路音源失败：%s", exc)
        self._level = 0.0
        self._leftover = mix_audio(first_audio, second_audio)
        return self._leftover

    def drain(self) -> np.ndarray:
        return mix_audio(self.first.drain(), self.second.drain())

    # -- 基类抽象方法（由上面的 start/stop 覆写，不会走到这里） -----------
    def _start_backend(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def _stop_backend(self) -> None:  # pragma: no cover
        raise NotImplementedError


def create_meeting_source(
    kind: str = "loopback",
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
    loopback_device: object = None,
    mic_device: object = None,
) -> AudioSource:
    """按 ``meeting_source`` 创建会议采集源：系统声音 / 麦克风 / 两者。"""
    if kind == "mic":
        return MicSource(device=mic_device, sample_rate=sample_rate)
    if kind == "both":
        return MixedAudioSource(
            LoopbackSource(device=loopback_device, sample_rate=sample_rate),
            MicSource(device=mic_device, sample_rate=sample_rate),
        )
    if kind == "loopback":
        return LoopbackSource(device=loopback_device, sample_rate=sample_rate)
    raise ValueError(f"未知的会议音源：{kind!r}")


# --------------------------------------------------------------------------
def create_source(kind: str, device: object = None, sample_rate: int = TARGET_SAMPLE_RATE) -> AudioSource:
    """工厂：``"mic"`` 或 ``"loopback"``。"""
    if kind == "mic":
        return MicSource(device=device, sample_rate=sample_rate)
    if kind == "loopback":
        return LoopbackSource(device=device, sample_rate=sample_rate)
    raise ValueError(f"未知的采集类型：{kind!r}")


class SilenceWatcher:
    """基于电平的静音检测，用于 push-to-talk 的提前结束与"没说话"判断。"""

    def __init__(self, threshold: float = 0.012, hold_seconds: float = 1.2) -> None:
        self.threshold = threshold
        self.hold_seconds = hold_seconds
        self._last_loud = 0.0

    def feed(self, level: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if level >= self.threshold:
            self._last_loud = now

    def silent_for(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return now - self._last_loud

    def is_silent(self, now: float | None = None) -> bool:
        return self.silent_for(now) >= self.hold_seconds
