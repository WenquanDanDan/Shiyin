"""音频文件转写测试：WAV 解码、重采样/降混、文字稿命名与后台转写流程。

真实识别模型不参与测试（用假引擎/假 VAD），但 WAV 解码走真实代码路径；
MP3/M4A 解码在安装了 PyAV 时才测，否则跳过。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
import wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import file_transcribe as ft  # noqa: E402


def write_wav(
    path: Path,
    samples: np.ndarray,
    *,
    rate: int = 16000,
    width: int = 2,
) -> None:
    """把 float32 [-1, 1] 写成 PCM WAV；二维数组按 (帧, 声道) 交织。"""
    data = np.asarray(samples, dtype=np.float32)
    channels = 1 if data.ndim == 1 else int(data.shape[1])
    flat = np.clip(data.reshape(-1), -1.0, 1.0)
    if width == 2:
        pcm = (flat * 32767.0).astype("<i2").tobytes()
    elif width == 3:
        value = (flat * 8388607.0).astype(np.int32)
        raw = np.empty((value.size, 3), dtype=np.uint8)
        raw[:, 0] = value & 0xFF
        raw[:, 1] = (value >> 8) & 0xFF
        raw[:, 2] = (value >> 16) & 0xFF
        pcm = raw.tobytes()
    else:
        raise ValueError(width)

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(pcm)


class TestNaming(unittest.TestCase):
    def test_sanitize_removes_illegal_characters(self) -> None:
        self.assertEqual(ft.sanitize_stem('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_sanitize_empty_and_dots(self) -> None:
        self.assertEqual(ft.sanitize_stem(""), "音频")
        self.assertEqual(ft.sanitize_stem("..."), "音频")

    def test_sanitize_truncates_long_names(self) -> None:
        self.assertLessEqual(len(ft.sanitize_stem("a" * 200)), 60)

    def test_transcript_path_shape(self) -> None:
        path = ft.make_transcript_path(
            Path("output"),
            Path("我的 会议/记录 2026.wav"),
            when=datetime(2026, 10, 24, 1, 2, 3),
        )
        self.assertEqual(path.name, "转写-记录 2026-20261024-010203.txt")
        self.assertEqual(path.parent, Path("output"))


class TestWavDecoding(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_mono_16k_roundtrip(self) -> None:
        path = self.tmp / "mono.wav"
        write_wav(path, np.full(16000, 0.25, dtype=np.float32))
        audio = ft.load_audio_file(path)
        self.assertEqual(audio.size, 16000)
        self.assertAlmostEqual(float(np.abs(audio).max()), 0.25, places=3)

    def test_stereo_is_downmixed(self) -> None:
        path = self.tmp / "stereo.wav"
        stereo = np.column_stack(
            [np.full(8000, 0.5, np.float32), np.full(8000, -0.5, np.float32)]
        )
        write_wav(path, stereo)
        audio = ft.load_audio_file(path)
        self.assertEqual(audio.size, 8000)
        self.assertLess(float(np.abs(audio).max()), 1e-4, "左右反相应互相抵消")

    def test_48k_is_resampled_to_16k(self) -> None:
        path = self.tmp / "48k.wav"
        t = np.arange(48000, dtype=np.float32) / 48000.0
        write_wav(path, 0.3 * np.sin(2 * np.pi * 440 * t), rate=48000)
        audio = ft.load_audio_file(path)
        self.assertLessEqual(abs(audio.size - 16000), 20)
        self.assertAlmostEqual(ft.audio_duration(path), 1.0, places=2)

    def test_24bit_pcm(self) -> None:
        path = self.tmp / "24bit.wav"
        write_wav(path, np.full(4000, -0.25, dtype=np.float32), width=3)
        audio = ft.load_audio_file(path)
        self.assertEqual(audio.size, 4000)
        self.assertAlmostEqual(float(np.median(audio)), -0.25, places=3)

    def test_iter_blocks_covers_whole_file(self) -> None:
        path = self.tmp / "blocks.wav"
        write_wav(path, np.zeros(40000, dtype=np.float32))
        blocks = list(ft.iter_audio_blocks(path, block_seconds=0.5))
        self.assertEqual(len(blocks), 5)
        self.assertEqual(sum(block.size for block in blocks), 40000)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(ft.AudioFileError):
            ft.load_audio_file(self.tmp / "missing.wav")
        with self.assertRaises(ft.AudioFileError):
            ft.audio_duration(self.tmp / "missing.wav")

    def test_corrupt_wav_raises_readable_error(self) -> None:
        path = self.tmp / "broken.wav"
        path.write_bytes(b"not a wav file")
        with self.assertRaises(ft.AudioFileError):
            ft.load_audio_file(path)


@unittest.skipUnless(ft.av_available(), "未安装 PyAV，跳过 MP3/M4A 解码测试")
class TestCompressedFormats(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _encode(self, path: Path, audio: np.ndarray, codec: str, rate: int = 16000) -> None:
        import av

        container = av.open(str(path), "w")
        try:
            stream = container.add_stream(codec, rate=rate)
            stream.layout = "mono"
            frame = av.AudioFrame.from_ndarray(
                audio.reshape(1, -1).astype(np.float32), format="flt", layout="mono"
            )
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        finally:
            container.close()

    def _tone(self) -> np.ndarray:
        t = np.arange(16000, dtype=np.float32) / 16000.0
        return (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    def test_mp3(self) -> None:
        path = self.tmp / "tone.mp3"
        self._encode(path, self._tone(), "mp3")
        audio = ft.load_audio_file(path)
        self.assertGreater(audio.size, 14000)
        self.assertGreater(float(np.abs(audio).max()), 0.05)

    def test_m4a(self) -> None:
        path = self.tmp / "tone.m4a"
        try:
            self._encode(path, self._tone(), "aac")
        except Exception as exc:  # noqa: BLE001 - 本机 FFmpeg 没有 AAC 编码器
            self.skipTest(f"本机 PyAV 无法编码 AAC：{exc}")
        audio = ft.load_audio_file(path)
        self.assertGreater(audio.size, 14000)
        self.assertGreater(float(np.abs(audio).max()), 0.05)


class FakeVad:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self._sent = False

    def accept(self, samples: np.ndarray) -> list[np.ndarray]:
        if self._sent:
            return []
        self._sent = True
        return [samples]

    def flush(self) -> list[np.ndarray]:
        return []


class SilentVad:
    def __init__(self, **kwargs) -> None:
        pass

    def accept(self, samples: np.ndarray) -> list[np.ndarray]:
        return []

    def flush(self) -> list[np.ndarray]:
        return []


class FakeEngine:
    def __init__(self, text: str = "文件里的内容") -> None:
        self.text = text
        self.calls: list[int] = []

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000, **kwargs):
        self.calls.append(int(samples.size))
        return SimpleNamespace(text=self.text)


class TestFileTranscriber(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_runs_to_completion_and_writes_transcript(self) -> None:
        source = self.tmp / "讲话 录音.wav"
        write_wav(source, np.full(16000, 0.1, dtype=np.float32))
        out_dir = self.tmp / "out"

        segments: list[tuple[str, float]] = []
        progress: list[tuple[float, float, int]] = []
        finished: dict[str, object] = {}
        done = threading.Event()

        def on_finished(path, count, duration, cancelled) -> None:
            finished.update(path=str(path), count=count, duration=duration, cancelled=cancelled)
            done.set()

        engine = FakeEngine()
        transcriber = ft.FileTranscriber(
            engine,  # type: ignore[arg-type]
            output_dir=out_dir,
            on_segment=lambda text, offset: segments.append((text, offset)),
            on_progress=lambda done_s, total, count: progress.append((done_s, total, count)),
            on_finished=on_finished,
            on_error=lambda message: self.fail(f"不应出错：{message}"),
        )
        with mock.patch.object(ft, "VadSegmenter", FakeVad):
            self.assertTrue(transcriber.start(source))
            self.assertTrue(done.wait(5), "转写线程未在 5 秒内结束")

        self.assertFalse(finished["cancelled"])
        self.assertEqual(finished["count"], 1)
        self.assertAlmostEqual(float(finished["duration"]), 1.0, places=2)
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(segments, [("文件里的内容", 0.0)])
        self.assertTrue(progress, "应至少上报一次进度")

        transcript = transcriber.transcript_path
        assert transcript is not None
        self.assertTrue(transcript.is_file())
        content = transcript.read_text(encoding="utf-8")
        self.assertIn("文件里的内容", content)
        self.assertIn("共 1 段", content)
        self.assertIn("讲话 录音.wav", content)

    def test_cancel_stops_before_finishing(self) -> None:
        source = self.tmp / "长录音.wav"
        source.write_bytes(b"")  # start() 只检查存在性；解码被打桩替代

        finished: dict[str, object] = {}
        done = threading.Event()

        def on_finished(path, count, duration, cancelled) -> None:
            finished.update(count=count, cancelled=cancelled)
            done.set()

        def slow_blocks(path, *, sample_rate=16000, block_seconds=0.5):
            for _ in range(40):
                time.sleep(0.05)
                yield np.zeros(8000, dtype=np.float32)

        transcriber = ft.FileTranscriber(
            FakeEngine(),  # type: ignore[arg-type]
            output_dir=self.tmp / "out",
            on_finished=on_finished,
            on_error=lambda message: self.fail(f"不应出错：{message}"),
        )
        with (
            mock.patch.object(ft, "VadSegmenter", SilentVad),
            mock.patch.object(ft, "audio_duration", lambda path: 20.0),
            mock.patch.object(ft, "iter_audio_blocks", slow_blocks),
        ):
            self.assertTrue(transcriber.start(source))
            time.sleep(0.16)
            transcriber.cancel()
            self.assertTrue(done.wait(5), "取消后线程未退出")

        self.assertTrue(finished["cancelled"])
        self.assertEqual(finished["count"], 0)
        transcript = transcriber.transcript_path
        assert transcript is not None
        self.assertIn("共 0 段", transcript.read_text(encoding="utf-8"))

    def test_missing_file_reports_error_and_returns_false(self) -> None:
        errors: list[str] = []
        transcriber = ft.FileTranscriber(
            FakeEngine(),  # type: ignore[arg-type]
            output_dir=self.tmp,
            on_error=errors.append,
        )
        self.assertFalse(transcriber.start(self.tmp / "missing.wav"))
        self.assertTrue(errors and "文件不存在" in errors[0])


class TestAppFileTranscription(unittest.TestCase):
    """App 层：从任务启动到窗口字幕、完成状态与落盘的完整链路。"""

    @classmethod
    def setUpClass(cls) -> None:
        import tkinter as tk

        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"当前环境无法创建 Tk 窗口：{exc}") from exc
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def test_app_runs_file_transcription_and_updates_window(self) -> None:
        import queue as queue_mod

        from vtw.app import App

        with tempfile.TemporaryDirectory() as tmp:
            app = App(Path(tmp) / "config.json")
            app._root = self.root
            app.engine = FakeEngine("窗口里的内容")  # type: ignore[assignment]
            app.history.enabled = False
            app.config.set("output_dir", str(Path(tmp) / "out"))

            source = Path(tmp) / "会议 录音.wav"
            write_wav(source, np.full(8000, 0.1, dtype=np.float32))

            with mock.patch.object(ft, "VadSegmenter", FakeVad):
                app._start_file_transcription(source)
                deadline = time.monotonic() + 5.0
                while app.file is not None and time.monotonic() < deadline:
                    try:
                        while True:
                            app._handle_ui(app._ui_queue.get_nowait())
                    except queue_mod.Empty:
                        pass
                    self.root.update()
                    time.sleep(0.02)

            window = app._file_window
            self.assertIsNotNone(window)
            self.assertEqual(window.win.title(), "vtw 文件转写")
            self.assertIn("窗口里的内容", window.text.get("1.0", "end"))
            self.assertEqual(window.status.cget("text"), "已完成")
            self.assertEqual(str(window.stop_button.cget("state")), "disabled")
            self.assertIsNone(app.file)

            transcripts = list((Path(tmp) / "out").glob("转写-*.txt"))
            self.assertEqual(len(transcripts), 1)
            self.assertIn("窗口里的内容", transcripts[0].read_text(encoding="utf-8"))
            if window is not None:
                window.destroy()


if __name__ == "__main__":
    unittest.main()
