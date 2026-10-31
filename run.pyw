"""vtw 无控制台启动入口。

用法
----
``pythonw.exe run.pyw``（开机自启走的就是这条），或者直接双击本文件。

为什么这里要"换解释器"
----------------------
``.pyw`` 在 Windows 上关联到 ``pyw.exe``（Python Launcher），它默认用**系统
Python**；而系统 Python 里没有本项目的 numpy / sounddevice / sherpa_onnx。
直接双击本文件会起一个"空壳实例"：托盘和全局热键都能装上，但一按热键
``import sounddevice`` 就失败，表现出来的就是**按了快捷键完全不录音、也没提示**。
更糟的是受限会话里单实例互斥体可能创建失败（错误码 5，见 ``single.py``），
这个空壳进程还会和正常实例并存、抢走热键。

所以入口先检查自己是否跑在项目 ``.venv`` 里，不是就用
``.venv\\Scripts\\pythonw.exe`` 重启自己。这样"双击 run.pyw"永远等价于
"用项目自带环境启动"，不会再出现零依赖的空壳实例。

需要看日志时改用 ``启动(调试).bat``，或直接运行 ``python -m vtw -v``。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
_VENV_DIR = _ROOT / ".venv"

#: 子进程用它标记"我已经被换过一次解释器了"，避免无限重启
_REEXEC_FLAG = "VTW_REEXECED"


def venv_python(windowed: bool = True) -> Path | None:
    """项目 venv 里的解释器路径；没有 venv 时返回 ``None``。

    优先返回与实际运行方式匹配的那个（窗口模式用 ``pythonw.exe``），
    缺失时退回另一个。
    """
    scripts = _VENV_DIR / "Scripts"
    preferred = scripts / ("pythonw.exe" if windowed else "python.exe")
    if preferred.is_file():
        return preferred
    fallback = scripts / ("python.exe" if windowed else "pythonw.exe")
    return fallback if fallback.is_file() else None


def running_in_venv() -> bool:
    """当前解释器是否就是项目自带的 ``.venv``。"""
    try:
        return Path(sys.prefix).resolve() == _VENV_DIR.resolve()
    except OSError:  # pragma: no cover - 路径不可解析时按"不在 venv"处理
        return False


def needs_reexec() -> bool:
    """是否需要换到 venv 解释器重新启动。"""
    if os.environ.get(_REEXEC_FLAG) == "1":
        return False  # 已经换过一次，再换就是死循环
    if running_in_venv():
        return False
    return venv_python() is not None


def reexec_in_venv() -> bool:
    """用 venv 解释器重启自己；返回 ``True`` 表示已把工作交给子进程。"""
    windowed = Path(sys.executable).name.lower().startswith("pythonw")
    target = venv_python(windowed)
    if target is None:
        return False

    env = dict(os.environ)
    env[_REEXEC_FLAG] = "1"
    try:
        subprocess.Popen(
            [str(target), str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=str(_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        return False  # 起不来就原地继续，__main__ 的依赖自检会给出明确提示
    return True


if __name__ == "__main__":
    if needs_reexec() and reexec_in_venv():
        raise SystemExit(0)

    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

    from vtw import elevate  # noqa: E402

    # 全局键盘钩子在非提权进程里可能收不到按键（UIPI / 安全软件），
    # 所以默认以管理员身份运行；配置 run_as_admin=false 或环境变量
    # VTW_NO_ELEVATE=1 可以关掉。
    if elevate.should_relaunch(sys.argv[1:]):
        if elevate.relaunch([*sys.argv[1:]], script=Path(__file__).resolve()):
            raise SystemExit(0)  # 已把工作交给提权副本

    from vtw.__main__ import main  # noqa: E402

    # 提权标记只是给本文件看的，不能传给 argparse
    args = [a for a in sys.argv[1:] if a != elevate.MARKER]
    raise SystemExit(main(args))
