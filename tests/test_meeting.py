"""会议转写会话测试。

覆盖新增的「会议音源」选择：默认走系统声音、非法值回退、以及非系统声音
音源也能走完整链路（这里用假音源/假 VAD/假引擎，不碰真实设备与模型）。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import meeting as meeting_mod  # noqa: E402
from vtw.meeting import MeetingSession  # noqa: E402


class TestMeetingSourceSelection(unittest.TestCase):
    def make(self, kind: str = "loopback") -> MeetingSession:
        return MeetingSession(engine=object(), source_kind=kind)

    def test_default_is_system_sound(self) -> None:
        session = self.make()
        self.assertEqual(session.source_kind, "loopback")
        self.assertEqual(session.source_label, "系统声音")

    def test_microphone_label(self) -> None:
        session = self.make("mic")
        self.assertEqual(session.source_kind, "mic")
        self.assertEqual(session.source_label, "麦克风")

    def test_both_label(self) -> None:
        self.assertEqual(self.make("both").source_label, "系统声音 + 麦克风")

    def test_invalid_kind_falls_back(self) -> None:
        session = self.make("telepathy")
        self.assertEqual(session.source_kind, "loopback")

    def test_device_name_is_empty_before_start(self) -> None:
        self.assertEqual(self.make("mic").source_device, "")


class FakeSource:
    sample_rate = 16000
    device_name = "假麦克风"

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self._first = True

    def start(self) -> None:
        self.started = True

    def stop(self) -> np.ndarray:
        self.stopped = True
        return np.zeros(0, dtype=np.float32)

    def drain(self) -> np.ndarray:
        if self._first:
            self._first = False
            return np.ones(8000, dtype=np.float32) * 0.1
        return np.zeros(0, dtype=np.float32)


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


class FakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(text="麦克风里的内容")


class TestMeetingSessionRunsWithMicSource(unittest.TestCase):
    """会议音源设为 mic 时，会话应通过工厂取得音源并完整跑通。"""

    def test_mic_source_flows_through_vad_and_transcript(self) -> None:
        engine = FakeEngine()
        source = FakeSource()
        segments: list[tuple[str, float]] = []
        with tempfile.TemporaryDirectory() as tmp:
            session = MeetingSession(
                engine,  # type: ignore[arg-type]
                source_kind="mic",
                output_dir=Path(tmp),
                on_segment=lambda text, offset: segments.append((text, offset)),
            )
            with mock.patch.object(meeting_mod, "VadSegmenter", FakeVad), mock.patch.object(
                meeting_mod, "create_meeting_source", return_value=source
            ) as factory:
                self.assertTrue(session.start())
                time.sleep(0.4)
                path = session.stop()

            factory.assert_called_once()
            self.assertEqual(factory.call_args.args[0], "mic", "必须把配置的音源类型传给工厂")
            self.assertTrue(source.started and source.stopped)
            self.assertEqual(engine.calls, 1)
            self.assertEqual(segments, [("麦克风里的内容", 0.5)])
            assert path is not None
            content = path.read_text(encoding="utf-8")
            self.assertIn("麦克风里的内容", content)
            self.assertIn("共 1 段", content)


if __name__ == "__main__":
    unittest.main()
