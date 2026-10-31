"""以管理员权限重新启动自己。

为什么本项目需要提权
--------------------
热键用的是 ``WH_KEYBOARD_LL`` 全局键盘钩子。Windows 的 UIPI 规则下：

* **非提权进程收不到发往高完整性（管理员）窗口的按键**；
* 部分安全软件（如火绒）会额外拦截非提权进程的键盘钩子。

实测：普通终端里跑 ``scripts/diagnose_hotkey.py`` —— 钩子一个按键都收不到；
用管理员终端跑同一个脚本 —— 正常收到。所以默认以管理员身份运行。

提权方式
--------
``ShellExecuteW(..., "runas", ...)``。管理员账号且 UAC 设为"提权不提示"
（``ConsentPromptBehaviorAdmin=0``）时是**静默提权**，不弹框；其它策略下会弹一次 UAC。

关掉的办法
----------
配置文件 ``run_as_admin: false``，或设置环境变量 ``VTW_NO_ELEVATE=1``
（调试/用别的方式处理钩子权限时有用）。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

#: 提权子进程的命令行标记。带它就说明"已经是提权拉起的那一份"，
#: 不再重复提权（否则会无限套娃）。
MARKER = "--vtw-elevated"

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wintypes.BOOL
shell32.ShellExecuteW.argtypes = [
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_int,
]
# HINSTANCE 是指针宽度，restype 写成默认的 int 会在 x64 上被截断
shell32.ShellExecuteW.restype = ctypes.c_void_p

SW_SHOWNORMAL = 1


def is_admin() -> bool:
    """当前进程是否已提权。"""
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 非 Windows 或 API 异常时按"否"处理
        return False


def wants_admin(config_file: Path | None = None) -> bool:
    """配置里是否要求以管理员运行（默认 True）。

    这里直接读 JSON，不走 :class:`vtw.config.Config` —— 提权发生在加载配置之前，
    要尽量避免额外依赖和副作用。
    """
    path = config_file
    if path is None:
        from . import paths

        path = paths.CONFIG_FILE
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True  # 配置缺失/损坏时按默认值处理，与 DEFAULTS 一致
    return bool(data.get("run_as_admin", True))


def should_relaunch(argv: list[str] | None = None) -> bool:
    """是否应该提权重启。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if MARKER in args:
        return False  # 已经是提权副本
    if os.environ.get("VTW_NO_ELEVATE") == "1":
        return False
    if is_admin():
        return False
    return wants_admin()


def relaunch(argv: list[str] | None = None, *, script: Path | None = None) -> bool:
    """用管理员权限拉起本脚本的一份副本。成功返回 ``True``，调用方应立刻退出。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if MARKER not in args:
        args.insert(0, MARKER)

    entry = script or Path(sys.argv[0]).resolve()
    exe = Path(sys.executable)
    if not exe.is_file():
        log.error("提权失败：找不到解释器 %s", exe)
        return False

    params = subprocess.list2cmdline([str(entry), *args])
    try:
        result = shell32.ShellExecuteW(
            None, "runas", str(exe), params, str(entry.parent), SW_SHOWNORMAL
        )
    except Exception as exc:  # noqa: BLE001
        log.error("提权失败（ShellExecuteW 异常）：%s", exc)
        return False

    code = int(result or 0)
    if code > 32:
        log.info("已用管理员权限重新启动：%s", exe)
        return True

    # 常见返回值：5=拒绝访问，1223=用户取消
    log.error("提权失败（ShellExecuteW 返回 %s）", code)
    return False
