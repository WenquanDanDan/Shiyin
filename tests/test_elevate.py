"""提权逻辑测试（不会真的拉起提权进程）。

回归背景
--------
本项目的热键依赖 ``WH_KEYBOARD_LL`` 全局钩子。非提权进程在两种情况下收不到按键：

* UIPI：前台窗口属于更高完整性（管理员）进程时；
* 安全软件（如火绒）拦截非提权进程的键盘钩子。

实测：普通终端跑 ``diagnose_hotkey.py`` 一个键都收不到，管理员终端正常。
因此默认以管理员运行，这里锁定"要不要提权"的判定逻辑。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import elevate  # noqa: E402
from vtw.config import DEFAULTS  # noqa: E402


class TestWantsAdmin(unittest.TestCase):
    def _write_config(self, payload: dict) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_default_is_true_when_key_missing(self) -> None:
        self.assertTrue(elevate.wants_admin(self._write_config({})))

    def test_respects_false(self) -> None:
        self.assertFalse(elevate.wants_admin(self._write_config({"run_as_admin": False})))

    def test_respects_true(self) -> None:
        self.assertTrue(elevate.wants_admin(self._write_config({"run_as_admin": True})))

    def test_broken_config_falls_back_to_true(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text("{ not json", encoding="utf-8")
        self.assertTrue(elevate.wants_admin(path))

    def test_defaults_contains_run_as_admin(self) -> None:
        self.assertIs(DEFAULTS["run_as_admin"], True)


class TestShouldRelaunch(unittest.TestCase):
    def test_skips_when_already_admin(self) -> None:
        with mock.patch.object(elevate, "is_admin", return_value=True):
            self.assertFalse(elevate.should_relaunch([]))

    def test_skips_when_marker_present(self) -> None:
        # 提权副本必须带着标记，否则会无限套娃
        with mock.patch.object(elevate, "is_admin", return_value=False):
            self.assertFalse(elevate.should_relaunch([elevate.MARKER]))

    def test_skips_when_env_disables(self) -> None:
        with (
            mock.patch.object(elevate, "is_admin", return_value=False),
            mock.patch.dict(os.environ, {"VTW_NO_ELEVATE": "1"}),
        ):
            self.assertFalse(elevate.should_relaunch([]))

    def test_relaunches_when_not_admin_and_wanted(self) -> None:
        with (
            mock.patch.object(elevate, "is_admin", return_value=False),
            mock.patch.object(elevate, "wants_admin", return_value=True),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("VTW_NO_ELEVATE", None)
            self.assertTrue(elevate.should_relaunch([]))

    def test_no_elevation_when_config_disables(self) -> None:
        with (
            mock.patch.object(elevate, "is_admin", return_value=False),
            mock.patch.object(elevate, "wants_admin", return_value=False),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("VTW_NO_ELEVATE", None)
            self.assertFalse(elevate.should_relaunch([]))


class TestIsAdmin(unittest.TestCase):
    def test_returns_bool(self) -> None:
        self.assertIsInstance(elevate.is_admin(), bool)

    def test_marker_is_stable(self) -> None:
        # run.pyw 与 __main__ 都靠这个标记识别提权副本
        self.assertEqual(elevate.MARKER, "--vtw-elevated")


if __name__ == "__main__":
    unittest.main()
