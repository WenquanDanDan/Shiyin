"""配置读写的单元测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw.config import DEFAULTS, Config, normalize  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_defaults_fill_missing_keys(self) -> None:
        cfg = normalize({})
        for key in DEFAULTS:
            self.assertIn(key, cfg)

    def test_nested_dict_merged_not_replaced(self) -> None:
        cfg = normalize({"vad": {"threshold": 0.7}})
        self.assertAlmostEqual(cfg["vad"]["threshold"], 0.7)
        # 未覆盖的子键必须保留默认值
        self.assertIn("min_silence_duration", cfg["vad"])

    def test_unknown_keys_preserved(self) -> None:
        cfg = normalize({"my_custom_key": 123})
        self.assertEqual(cfg["my_custom_key"], 123)

    def test_invalid_enum_falls_back(self) -> None:
        cfg = normalize({"mode": "nonsense", "inject_method": "telepathy", "language": "klingon"})
        self.assertEqual(cfg["mode"], DEFAULTS["mode"])
        self.assertEqual(cfg["inject_method"], DEFAULTS["inject_method"])
        self.assertEqual(cfg["language"], "auto")

    def test_meeting_source(self) -> None:
        self.assertEqual(normalize({})["meeting_source"], "loopback")
        for kind in ("loopback", "mic", "both"):
            self.assertEqual(normalize({"meeting_source": kind})["meeting_source"], kind)
        # 旧配置可能没有这个键，非法值也不能让程序起不来
        self.assertEqual(normalize({"meeting_source": "telepathy"})["meeting_source"], "loopback")
        self.assertEqual(normalize({"meeting_source": None})["meeting_source"], "loopback")

    def test_numeric_clamping(self) -> None:
        cfg = normalize({"num_threads": 9999, "min_dictation_seconds": -5})
        self.assertLessEqual(cfg["num_threads"], 16)
        self.assertGreaterEqual(cfg["min_dictation_seconds"], 0.1)

    def test_booleans_coerced(self) -> None:
        cfg = normalize({"use_itn": "yes", "lowercase_output": 0})
        self.assertIs(cfg["use_itn"], True)
        self.assertIs(cfg["lowercase_output"], False)

    def test_bad_types_do_not_raise(self) -> None:
        cfg = normalize({"num_threads": "abc", "vad": {"threshold": None}})
        self.assertIsInstance(cfg["num_threads"], int)
        self.assertIsInstance(cfg["vad"]["threshold"], float)


class TestConfigFile(unittest.TestCase):
    def test_missing_file_creates_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config.load(path)
            self.assertTrue(path.is_file())
            self.assertEqual(cfg["mode"], DEFAULTS["mode"])

    def test_corrupt_file_is_backed_up_and_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ this is not json ,,, ", encoding="utf-8")
            cfg = Config.load(path)
            # 必须能正常返回默认配置，不能抛异常
            self.assertEqual(cfg["mode"], DEFAULTS["mode"])
            backup = path.with_suffix(".json.bad")
            self.assertTrue(backup.is_file(), "损坏的配置应被备份为 .json.bad")

    def test_non_object_root_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            cfg = Config.load(path)
            self.assertEqual(cfg["mode"], DEFAULTS["mode"])

    def test_save_then_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config.load(path)
            cfg.set("hotkey_dictate", "ctrl+shift+d")
            cfg.save()

            reloaded = Config.load(path)
            self.assertEqual(reloaded["hotkey_dictate"], "ctrl+shift+d")

    def test_saved_file_is_valid_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            Config.load(path).save()
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)

    def test_attribute_and_item_access_agree(self) -> None:
        cfg = Config({})
        self.assertEqual(cfg["mode"], cfg.mode)

    def test_missing_attribute_raises(self) -> None:
        cfg = Config({})
        with self.assertRaises(AttributeError):
            _ = cfg.definitely_not_a_key

    def test_load_fills_in_new_keys_into_file(self) -> None:
        """旧版配置缺少新增项时，应写回补齐，让 config.json 始终自解释。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            # 模拟旧版本写的文件：只有少数几个键
            path.write_text(
                json.dumps({"hotkey_dictate": "alt+z", "mode": "toggle"}, ensure_ascii=False),
                encoding="utf-8",
            )
            cfg = Config.load(path)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("spoken_punctuation", saved, "新增项应被补进文件")
            self.assertIn("vad", saved)
            # 已有值必须原样保留
            self.assertEqual(saved["hotkey_dictate"], "alt+z")
            self.assertEqual(saved["mode"], "toggle")
            self.assertEqual(cfg["hotkey_dictate"], "alt+z")

    def test_complete_file_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            Config.load(path).save()
            first_mtime = path.stat().st_mtime_ns

            # 再加载一次不应触发写盘（幂等）
            Config.load(path)
            self.assertEqual(path.stat().st_mtime_ns, first_mtime)

    def test_create_if_missing_false_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            Config.load(path, create_if_missing=False)
            self.assertFalse(path.is_file())


if __name__ == "__main__":
    unittest.main()
