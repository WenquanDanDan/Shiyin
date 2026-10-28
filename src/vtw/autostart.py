"""开机自启管理（读写当前用户的 Run 键）。

用 ``winreg`` 直接操作 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``：
* 只影响当前用户，**不需要管理员权限**
* 不写 HKLM，不影响其他用户
* 启动命令用 ``pythonw.exe``（不是 ``python.exe``），登录后不会弹黑色控制台

本模块是自启逻辑的**单一事实来源**：``scripts/install_autostart.ps1`` 与
``scripts/uninstall_autostart.ps1`` 都只是调用它，避免两处实现漂移。
"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VtwVoiceToText"

#: 启动入口（项目根下的 run.pyw）
ENTRY_SCRIPT = "run.pyw"


def pythonw_path() -> Path:
    """返回 pythonw.exe 路径（与解释器同一目录）。"""
    return Path(sys.executable).with_name("pythonw.exe")


def entry_path() -> Path:
    return paths.PROJECT_ROOT / ENTRY_SCRIPT


def build_command() -> str:
    """构造写入注册表的启动命令。"""
    return f'"{pythonw_path()}" "{entry_path()}"'


def _read_raw() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.error("读取自启项失败：%s", exc)
        return None


def is_enabled() -> bool:
    """当前是否已注册开机自启。"""
    return _read_raw() is not None


def current_command() -> str | None:
    """返回已注册的命令（未注册返回 None）。"""
    return _read_raw()


def command_matches() -> bool:
    """已注册的命令是否指向当前这份安装（换过目录就会不匹配）。"""
    return _read_raw() == build_command()


def enable() -> tuple[bool, str]:
    """注册开机自启。返回 ``(是否成功, 中文说明)``。"""
    exe = pythonw_path()
    entry = entry_path()

    if not exe.is_file():
        return False, f"找不到 {exe}\n请先运行 scripts\\setup.ps1 创建虚拟环境。"
    if not entry.is_file():
        return False, f"找不到 {entry}"

    command = build_command()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as exc:
        log.error("写入自启项失败：%s", exc)
        return False, f"写入注册表失败：{exc}"

    # 回读校验：写入成功不等于真的写对了
    written = _read_raw()
    if written != command:
        return False, f"写入校验失败。\n期望：{command}\n实际：{written}"

    log.info("已注册开机自启：%s", command)
    return True, "已开启开机自启，下次登录会自动运行。"


def disable() -> tuple[bool, str]:
    """取消开机自启。返回 ``(是否成功, 中文说明)``。"""
    if not is_enabled():
        return True, "开机自启本来就是关闭的。"

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, "开机自启本来就是关闭的。"
    except OSError as exc:
        log.error("删除自启项失败：%s", exc)
        return False, f"删除注册表项失败：{exc}"

    if is_enabled():
        return False, "删除后仍然读到自启项，可能被其他程序写回。"
    log.info("已移除开机自启")
    return True, "已关闭开机自启。"


def set_enabled(enabled: bool) -> tuple[bool, str]:
    return enable() if enabled else disable()


def sync() -> tuple[bool, str]:
    """让自启项指向当前这份安装（目录搬迁后修复用）。"""
    if not is_enabled():
        return True, "未开启开机自启，无需同步。"
    return enable()


# ---------------------------------------------------------------------------
# CLI：供 PowerShell 脚本调用，保证与 GUI 用的是同一套逻辑
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    action = (argv[1] if len(argv) > 1 else "status").lower()
    if action == "enable":
        ok, message = enable()
        print(("[完成] " if ok else "[失败] ") + message)
        return 0 if ok else 1
    if action == "disable":
        ok, message = disable()
        print(("[完成] " if ok else "[失败] ") + message)
        return 0 if ok else 1
    if action == "sync":
        ok, message = sync()
        print(("[完成] " if ok else "[失败] ") + message)
        return 0 if ok else 1

    print(f"自启项名称：{VALUE_NAME}")
    print(f"当前状态：{'已开启' if is_enabled() else '未开启'}")
    print(f"注册命令：{current_command() or '（无）'}")
    print(f"期望命令：{build_command()}")
    print(f"是否指向当前安装：{'是' if command_matches() else '否'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
