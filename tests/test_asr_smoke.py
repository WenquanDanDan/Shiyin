"""端到端识别测试：真实加载模型跑真实音频。

模型缺失时跳过而不失败，保证在没下载模型的机器上 `unittest discover` 仍可用。
"""

from __future__ import annotations

import sys
import time
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import paths  # noqa: E402
from vtw.asr import ModelNotReadyError, SenseVoiceEngine, verify_model_files  # noqa: E402
from vtw.vad import trim_silence  # noqa: E402

#: 官方文档给出的 zh.wav ground truth 片段。
#: 选用的 2024-07-17 int8 模型 + use_itn=True 可以逐字命中，故作为准确率断言。
ZH_EXPECTED_FRAGMENT = "早上9点至下午5点"
#: 官方文档给出的 en.wav ground truth（含大小写与数字归一化）
EN_EXPECTED = "The tribal chieftain called for the boy and presented him with 50 pieces of code."
RTF_BUDGET = 0.30


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        channels, rate = wf.getnchannels(), wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


class TestModelFiles(unittest.TestCase):
    def test_verify_returns_actionable_message(self) -> None:
        ok, message = verify_model_files()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(message, str)
        self.assertTrue(message)


@unittest.skipUnless(
    paths.sense_voice_model_path().is_file(), "未下载 SenseVoice 模型，跳过端到端测试"
)
class TestSenseVoiceEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wav_dir = paths.SENSE_VOICE_DIR / "test_wavs"
        # 与 config.json 默认值保持一致：use_itn=True
        cls.engine = SenseVoiceEngine(num_threads=4, language="auto", use_itn=True)
        cls.engine.warmup()

    def test_chinese_recognition_and_rtf(self) -> None:
        wav = self.wav_dir / "zh.wav"
        if not wav.is_file():
            self.skipTest("缺少 test_wavs/zh.wav")
        audio, rate = _read_wav(wav)

        self.engine.transcribe(audio, rate)  # 预热
        result = self.engine.transcribe(audio, rate)

        self.assertTrue(result.text, "识别结果不应为空")
        self.assertLess(result.rtf, RTF_BUDGET, f"RTF 过高：{result.rtf:.3f}")
        self.assertIn(ZH_EXPECTED_FRAGMENT, result.text)
        self.assertNotIn("<|", result.text, "结果不应含控制标签")

    def test_english_recognition_matches_ground_truth(self) -> None:
        """锁定英文质量：大小写正确、数字已归一化。

        这条断言是为 2025-09-09 版模型整句输出大写（THE TRIVBAL CHIEF…）而加的回归保护。
        """
        wav = self.wav_dir / "en.wav"
        if not wav.is_file():
            self.skipTest("缺少 test_wavs/en.wav")
        audio, rate = _read_wav(wav)
        self.engine.transcribe(audio, rate)  # 预热
        result = self.engine.transcribe(audio, rate)
        self.assertEqual(result.text, EN_EXPECTED)

    def test_quiet_recording_is_normalized(self) -> None:
        """回归：麦克风增益低（峰值约 0.03）时也要能识别整句。

        旧版 ``trim_silence`` 用固定 0.01 阈值，会把这类录音裁到只剩约 1 秒，
        用户看到的就是"说了一句话，只识别出一个字"。
        """
        wav = self.wav_dir / "zh.wav"
        if not wav.is_file():
            self.skipTest("缺少 test_wavs/zh.wav")
        audio, rate = _read_wav(wav)
        quiet = (audio * (0.03 / float(np.abs(audio).max()))).astype(np.float32)

        trimmed = trim_silence(quiet, sample_rate=rate)
        self.assertGreater(trimmed.size, audio.size * 0.7, "低音量语音不应被裁掉大半")

        result = self.engine.transcribe(trimmed, rate)
        self.assertIn(ZH_EXPECTED_FRAGMENT, result.text)

        # 即使不裁剪，引擎自己也要能把低电平音频归一化后识别出来
        result = self.engine.transcribe(quiet, rate)
        self.assertIn(ZH_EXPECTED_FRAGMENT, result.text)

    def test_empty_audio_returns_empty(self) -> None:
        result = self.engine.transcribe(np.zeros(0, dtype=np.float32), 16000)
        self.assertEqual(result.text, "")

    def test_engine_is_reusable(self) -> None:
        wav = self.wav_dir / "zh.wav"
        if not wav.is_file():
            self.skipTest("缺少 test_wavs/zh.wav")
        audio, rate = _read_wav(wav)
        first = self.engine.transcribe(audio, rate).text
        second = self.engine.transcribe(audio, rate).text
        self.assertEqual(first, second, "同一音频两次识别结果应一致（确定性）")

    def test_unload_then_reload_works(self) -> None:
        engine = SenseVoiceEngine(num_threads=2)
        engine.load()
        self.assertTrue(engine.is_loaded)
        engine.unload()
        self.assertFalse(engine.is_loaded)
        engine.load()  # 必须能重新加载
        self.assertTrue(engine.is_loaded)

    def test_missing_model_raises_model_not_ready(self) -> None:
        engine = SenseVoiceEngine()
        engine._recognizer = None
        original = paths.sense_voice_model_path
        try:
            paths.sense_voice_model_path = lambda: Path("nope") / "missing.onnx"  # type: ignore[assignment]
            with self.assertRaises(ModelNotReadyError):
                engine.load()
        finally:
            paths.sense_voice_model_path = original  # type: ignore[assignment]


class TestModelLoadCost(unittest.TestCase):
    """记录加载耗时，作为性能基线（不强制阈值，避免 CI 抖动）。"""

    @unittest.skipUnless(
        paths.sense_voice_model_path().is_file(), "未下载 SenseVoice 模型，跳过"
    )
    def test_load_is_reasonably_fast(self) -> None:
        engine = SenseVoiceEngine(num_threads=4)
        started = time.perf_counter()
        engine.load()
        elapsed = time.perf_counter() - started
        engine.unload()
        self.assertLess(elapsed, 30.0, f"模型加载耗时 {elapsed:.1f}s，过慢")


if __name__ == "__main__":
    unittest.main()
