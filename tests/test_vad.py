"""VAD 与音频工具的测试。

VAD 测试需要模型文件，缺失时自动跳过（不失败）。
"""

from __future__ import annotations

import sys
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import paths  # noqa: E402
from vtw.audio_source import (  # noqa: E402
    concat_frames,
    normalize_peak,
    resample,
    to_mono_float32,
)
from vtw.vad import VadSegmenter, VadUnavailableError, pick_vad_model, trim_silence  # noqa: E402


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        channels, rate = wf.getnchannels(), wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


class TestAudioHelpers(unittest.TestCase):
    def test_to_mono_from_stereo(self) -> None:
        stereo = np.array([[1.0, -1.0], [0.5, 0.5]], dtype=np.float32)
        mono = to_mono_float32(stereo)
        self.assertEqual(mono.shape, (2,))
        self.assertAlmostEqual(float(mono[0]), 0.0)

    def test_to_mono_from_column(self) -> None:
        column = np.array([[0.25], [0.5]], dtype=np.float32)
        self.assertEqual(to_mono_float32(column).shape, (2,))

    def test_to_mono_clips_out_of_range(self) -> None:
        loud = np.array([5.0, -5.0], dtype=np.float32)
        mono = to_mono_float32(loud)
        self.assertLessEqual(float(mono.max()), 1.0)
        self.assertGreaterEqual(float(mono.min()), -1.0)

    def test_concat_empty(self) -> None:
        out = concat_frames([])
        self.assertEqual(out.size, 0)
        self.assertEqual(out.dtype, np.float32)

    def test_concat_single_passthrough(self) -> None:
        one = np.array([0.1, 0.2], dtype=np.float32)
        self.assertIs(concat_frames([one]), one)

    def test_resample_halves_length(self) -> None:
        tone = np.sin(np.linspace(0, 100, 32000)).astype(np.float32)
        out = resample(tone, 32000, 16000)
        self.assertAlmostEqual(out.size, 16000, delta=64)

    def test_resample_same_rate_is_identity(self) -> None:
        tone = np.zeros(100, dtype=np.float32)
        self.assertEqual(resample(tone, 16000, 16000).size, 100)

    def test_normalize_peak_boosts_quiet_audio(self) -> None:
        quiet = np.full(1000, 0.02, dtype=np.float32)
        out = normalize_peak(quiet)
        # target 0.9 但最多放大 20 倍，所以峰值到 0.4
        self.assertAlmostEqual(float(np.abs(out).max()), 0.4, places=3)

    def test_normalize_peak_does_not_attenuate_loud_audio(self) -> None:
        loud = np.array([0.95, -0.95], dtype=np.float32)
        np.testing.assert_array_equal(normalize_peak(loud), loud)
        # 略低于目标的重来不衰减，只会小幅放大到 0.9
        medium = np.array([0.8, -0.8], dtype=np.float32)
        self.assertAlmostEqual(float(np.abs(normalize_peak(medium)).max()), 0.9, places=3)

    def test_normalize_peak_handles_empty_and_silence(self) -> None:
        self.assertEqual(normalize_peak(np.zeros(0, dtype=np.float32)).size, 0)
        np.testing.assert_array_equal(
            normalize_peak(np.zeros(100, dtype=np.float32)),
            np.zeros(100, dtype=np.float32),
        )


class TestTrimSilence(unittest.TestCase):
    def test_trims_leading_and_trailing(self) -> None:
        silence = np.zeros(8000, dtype=np.float32)
        speech = np.random.default_rng(0).normal(0, 0.3, 8000).astype(np.float32)
        trimmed = trim_silence(np.concatenate([silence, speech, silence]), sample_rate=16000)
        self.assertLess(trimmed.size, 24000)
        self.assertGreater(trimmed.size, 8000)

    def test_all_silence_returns_empty(self) -> None:
        self.assertEqual(trim_silence(np.zeros(16000, dtype=np.float32)).size, 0)

    def test_keeps_interior_pauses(self) -> None:
        rng = np.random.default_rng(1)
        loud = rng.normal(0, 0.3, 4000).astype(np.float32)
        gap = np.zeros(2000, dtype=np.float32)
        audio = np.concatenate([loud, gap, loud])
        trimmed = trim_silence(audio, sample_rate=16000)
        # 中间停顿必须保留，只裁首尾
        self.assertGreaterEqual(trimmed.size, audio.size - 2 * 1600)

    def test_empty_input(self) -> None:
        self.assertEqual(trim_silence(np.zeros(0, dtype=np.float32)).size, 0)


class TestTrimSilenceQuietSpeech(unittest.TestCase):
    """回归：低增益麦克风（峰值 0.01~0.05）的录音不能被裁掉大半。"""

    @classmethod
    def setUpClass(cls) -> None:
        wav = paths.SENSE_VOICE_DIR / "test_wavs" / "zh.wav"
        if not wav.is_file():
            raise unittest.SkipTest("缺少 test_wavs/zh.wav")
        cls.audio, cls.rate = _read_wav(wav)

    def test_quiet_speech_keeps_most_of_the_clip(self) -> None:
        quiet = (self.audio * (0.03 / float(np.abs(self.audio).max()))).astype(np.float32)
        trimmed = trim_silence(quiet, sample_rate=self.rate)
        self.assertGreater(
            trimmed.size,
            self.audio.size * 0.7,
            "低音量语音被静音裁剪吃掉了（旧版固定 0.01 阈值会只留下约 20%）",
        )

    def test_quiet_speech_is_not_mistaken_for_silence(self) -> None:
        quiet = (self.audio * (0.015 / float(np.abs(self.audio).max()))).astype(np.float32)
        trimmed = trim_silence(quiet, sample_rate=self.rate)
        self.assertGreater(trimmed.size / self.rate, 1.0, "低音量语音不应被裁成零长")

    def test_explicit_threshold_still_works(self) -> None:
        """保留显式传阈值的能力，方便排错脚本使用。"""
        trimmed = trim_silence(self.audio, sample_rate=self.rate, threshold=0.05)
        self.assertGreater(trimmed.size, 0)


class TestVadModelDiscovery(unittest.TestCase):
    def test_pick_returns_none_or_existing_path(self) -> None:
        picked = pick_vad_model()
        if picked is not None:
            self.assertTrue(Path(picked).is_file())


class TestVadSegmentation(unittest.TestCase):
    """需要真实 VAD 模型；缺失时跳过而不是失败。"""

    @classmethod
    def setUpClass(cls) -> None:
        if pick_vad_model() is None:
            raise unittest.SkipTest("未找到 VAD 模型，跳过（先跑 scripts/download_models.py）")
        wav = paths.SENSE_VOICE_DIR / "test_wavs" / "zh.wav"
        if not wav.is_file():
            raise unittest.SkipTest("未找到 test_wavs/zh.wav")
        cls.audio, cls.rate = _read_wav(wav)

    def test_segments_speech_from_wav(self) -> None:
        segmenter = VadSegmenter(sample_rate=self.rate, min_silence_duration=0.3)
        segments: list[np.ndarray] = []
        block = int(self.rate * 0.1)
        for offset in range(0, self.audio.size, block):
            segments.extend(segmenter.accept(self.audio[offset : offset + block]))
        segments.extend(segmenter.flush())
        self.assertGreaterEqual(len(segments), 1, "应当至少切出一段语音")
        self.assertGreater(sum(s.size for s in segments) / self.rate, 0.5)

    def test_reset_clears_state(self) -> None:
        segmenter = VadSegmenter(sample_rate=self.rate)
        segmenter.accept(self.audio[: self.rate])
        segmenter.reset()
        # reset 后不应残留上次的残余缓冲
        self.assertEqual(segmenter._residual.size, 0)

    def test_silence_yields_no_segments(self) -> None:
        segmenter = VadSegmenter(sample_rate=self.rate)
        silence = np.zeros(self.rate * 2, dtype=np.float32)
        segments = segmenter.accept(silence) + segmenter.flush()
        self.assertEqual(len(segments), 0)


class TestVadUnavailable(unittest.TestCase):
    def test_missing_model_raises_helpful_error(self) -> None:
        with self.assertRaises(VadUnavailableError):
            VadSegmenter(model_path=str(Path("definitely") / "missing.onnx"))


if __name__ == "__main__":
    unittest.main()
