"""听写链路测试：低电平麦克风 → 静音裁剪 → 音量归一化 → 识别 → 注入。

回归背景：麦克风增益低时（峰值约 0.03），旧版固定 0.01 的静音裁剪会把
5.59 秒语音裁到约 1.16 秒，用户说一整句只得到一两个字。这里直接跑 App 的
``_do_start_dictation`` / ``_do_stop_dictation``，用一段衰减后的真实语音验证。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import paths  # noqa: E402
import vtw.app as app_mod  # noqa: E402
from vtw.app import App  # noqa: E402

EXPECTED_FRAGMENT = "早上9点至下午5点"


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels, rate = handle.getnchannels(), handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


class FakeMic:
    """替身采集源：不打开真实设备，直接返回预设音频。"""

    audio: np.ndarray = np.zeros(0, dtype=np.float32)

    def __init__(self, *args, **kwargs) -> None:
        self.device_name = "测试麦克风"
        self.sample_rate = 16000

    def start(self) -> None:
        pass

    def stop(self) -> np.ndarray:
        return self.audio


@unittest.skipUnless(
    paths.sense_voice_model_path().is_file(), "未下载 SenseVoice 模型，跳过听写链路测试"
)
class TestQuietDictation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        wav = paths.SENSE_VOICE_DIR / "test_wavs" / "zh.wav"
        if not wav.is_file():
            raise unittest.SkipTest("缺少 test_wavs/zh.wav")
        cls.audio, cls.rate = _read_wav(wav)

    def _run_dictation(self, audio: np.ndarray) -> tuple[list[str], App]:
        FakeMic.audio = audio.astype(np.float32)
        injected: list[str] = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        app = App(Path(tmp.name) / "config.json")
        app.history.enabled = False
        app.engine.warmup()
        app._user_is_typing_target = lambda: True  # type: ignore[method-assign]

        with (
            mock.patch.object(app_mod, "MicSource", FakeMic),
            mock.patch.object(
                app_mod.inject,
                "inject_text",
                lambda text, **kwargs: injected.append(text) or True,
            ),
        ):
            app._do_start_dictation()
            app._do_stop_dictation()
        return injected, app

    def test_quiet_recording_is_fully_transcribed(self) -> None:
        quiet = self.audio * (0.03 / float(np.abs(self.audio).max()))
        injected, _ = self._run_dictation(quiet)
        self.assertTrue(injected, "低音量语音没有产生注入文本")
        self.assertIn(EXPECTED_FRAGMENT, injected[0])

    def test_silent_recording_is_not_injected(self) -> None:
        injected, _ = self._run_dictation(np.zeros(self.rate * 2, dtype=np.float32))
        self.assertEqual(injected, [], "纯静音不应注入任何文字")


if __name__ == "__main__":
    unittest.main()
