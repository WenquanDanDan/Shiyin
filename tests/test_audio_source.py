"""音频采集层测试：会议音源选择与「系统声音 + 麦克风」混音。

真实的录音设备不参与测试（CI/无声卡环境也能跑），用假音源覆盖逻辑：
选择哪种采集源、两路音频如何对齐相加、第二路启动失败是否回滚第一路。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import audio_source  # noqa: E402
from vtw.audio_source import (  # noqa: E402
    MEETING_SOURCE_KINDS,
    MEETING_SOURCE_LABELS,
    AudioSourceError,
    LoopbackSource,
    MicSource,
    MixedAudioSource,
    create_meeting_source,
    mix_audio,
)


class FakeSource:
    """最小的采集源替身，接口与 AudioSource 一致。"""

    def __init__(
        self,
        name: str = "fake",
        *,
        sample_rate: int = 16000,
        chunks: list[np.ndarray] | None = None,
        leftover: np.ndarray | None = None,
        fail_start: bool = False,
        level: float = 0.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.device_name = name
        self.level = level
        self.fail_start = fail_start
        self.started = False
        self.stopped = False
        self._chunks = list(chunks or [])
        self._leftover = (
            leftover if leftover is not None else np.zeros(0, dtype=np.float32)
        )

    @property
    def running(self) -> bool:
        return self.started and not self.stopped

    def start(self) -> None:
        if self.fail_start:
            raise AudioSourceError("假音源启动失败")
        self.started = True

    def stop(self) -> np.ndarray:
        self.stopped = True
        return self._leftover

    def drain(self) -> np.ndarray:
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        return self._chunks.pop(0)


class TestMixAudio(unittest.TestCase):
    def test_same_length_is_summed(self) -> None:
        mixed = mix_audio(np.array([0.1, 0.2], np.float32), np.array([0.2, -0.1], np.float32))
        np.testing.assert_allclose(mixed, [0.3, 0.1], atol=1e-6)

    def test_short_side_is_zero_padded(self) -> None:
        mixed = mix_audio(np.array([0.1, 0.2, 0.3], np.float32), np.array([0.5], np.float32))
        np.testing.assert_allclose(mixed, [0.6, 0.2, 0.3], atol=1e-6)

    def test_empty_side_returns_the_other(self) -> None:
        other = np.array([0.4, -0.4], np.float32)
        np.testing.assert_allclose(mix_audio(np.zeros(0, np.float32), other), other)
        np.testing.assert_allclose(mix_audio(other, np.zeros(0, np.float32)), other)

    def test_both_empty_returns_empty(self) -> None:
        self.assertEqual(mix_audio(np.zeros(0, np.float32), np.zeros(0, np.float32)).size, 0)

    def test_result_is_clipped_to_valid_range(self) -> None:
        mixed = mix_audio(np.array([0.8], np.float32), np.array([0.5], np.float32))
        self.assertAlmostEqual(float(mixed[0]), 1.0)
        mixed = mix_audio(np.array([-0.9], np.float32), np.array([-0.6], np.float32))
        self.assertAlmostEqual(float(mixed[0]), -1.0)

    def test_output_stays_float32(self) -> None:
        mixed = mix_audio(np.array([1, 2], np.int16), np.array([3, 4], np.int16))
        self.assertEqual(mixed.dtype, np.float32)


class TestMixedAudioSource(unittest.TestCase):
    def test_mixes_drains_from_both_sources(self) -> None:
        first = FakeSource("系统", chunks=[np.array([0.1, 0.2], np.float32)])
        second = FakeSource("麦克风", chunks=[np.array([0.3], np.float32)])
        source = MixedAudioSource(first, second)
        source.start()
        try:
            np.testing.assert_allclose(source.drain(), [0.4, 0.2], atol=1e-6)
            self.assertTrue(first.started and second.started)
            self.assertEqual(source.device_name, "系统 + 麦克风")
        finally:
            source.stop()

    def test_level_is_max_of_both(self) -> None:
        source = MixedAudioSource(FakeSource(level=0.1), FakeSource(level=0.7))
        self.assertAlmostEqual(source.level, 0.7)

    def test_stop_returns_mixed_leftovers(self) -> None:
        first = FakeSource(leftover=np.array([0.2], np.float32))
        second = FakeSource(leftover=np.array([0.3], np.float32))
        source = MixedAudioSource(first, second)
        source.start()
        np.testing.assert_allclose(source.stop(), [0.5], atol=1e-6)
        self.assertTrue(first.stopped and second.stopped)
        self.assertFalse(source.running)

    def test_stop_without_start_is_empty(self) -> None:
        source = MixedAudioSource(FakeSource(), FakeSource())
        self.assertEqual(source.stop().size, 0)

    def test_second_source_failure_rolls_back_first(self) -> None:
        first = FakeSource("系统")
        second = FakeSource("麦克风", fail_start=True)
        source = MixedAudioSource(first, second)
        with self.assertRaises(AudioSourceError):
            source.start()
        self.assertTrue(first.stopped, "第二路启动失败时必须停掉已经启动的第一路")
        self.assertFalse(source.running)

    def test_rejects_mismatched_sample_rates(self) -> None:
        with self.assertRaises(ValueError):
            MixedAudioSource(FakeSource(sample_rate=16000), FakeSource(sample_rate=48000))


class TestMeetingSourceFactory(unittest.TestCase):
    def test_loopback(self) -> None:
        self.assertIsInstance(create_meeting_source("loopback"), LoopbackSource)

    def test_microphone(self) -> None:
        self.assertIsInstance(create_meeting_source("mic"), MicSource)

    def test_both(self) -> None:
        source = create_meeting_source("both")
        self.assertIsInstance(source, MixedAudioSource)
        self.assertIsInstance(source.first, LoopbackSource)
        self.assertIsInstance(source.second, MicSource)

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_meeting_source("telepathy")

    def test_every_kind_has_a_label(self) -> None:
        self.assertEqual(set(MEETING_SOURCE_LABELS), set(MEETING_SOURCE_KINDS))

    def test_default_is_loopback(self) -> None:
        self.assertIsInstance(create_meeting_source(), LoopbackSource)
        self.assertEqual(audio_source.MEETING_SOURCE_KINDS[0], "loopback")


if __name__ == "__main__":
    unittest.main()
