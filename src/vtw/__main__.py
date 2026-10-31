"""命令行入口：``python -m vtw``。

由 ``run.pyw``（配合 ``pythonw.exe``）调用时无控制台；直接 ``python -m vtw``
调试时日志会同时打到终端。
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path

from . import __version__, elevate, paths
from .logsetup import setup as setup_logging
from .single import SingleInstance

#: 缺了就没法工作的第三方包：(导入名, 用途)
REQUIRED_MODULES: tuple[tuple[str, str], ...] = (
    ("numpy", "数值计算"),
    ("sounddevice", "麦克风采集"),
    ("sherpa_onnx", "语音识别引擎"),
    ("pystray", "系统托盘"),
    ("PIL", "托盘图标"),
)

#: 缺了只会少某个功能、不影响听写：(导入名, 用途, 安装方式)
OPTIONAL_MODULES: tuple[tuple[str, str, str], ...] = (
    ("soundcard", "会议/系统声音环回采集", "pip install -r requirements.txt"),
    ("pyaudiowpatch", "环回采集备用后端", "pip install -r requirements.txt"),
    ("av", "MP3/M4A/FLAC 等音频文件解码", "pip install -r requirements.txt"),
    ("keyboard", "原生钩子不可用时的热键兜底", "pip install -r requirements.txt"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vtw",
        description="vtw —— 完全本地的语音转文字工具",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"配置文件路径（默认 {paths.CONFIG_FILE}）",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="输出 DEBUG 级日志"
    )
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help="允许同时运行多个实例（默认单实例）",
    )
    parser.add_argument("--version", action="version", version=f"vtw {__version__}")
    return parser


def missing_modules() -> list[tuple[str, str]]:
    """返回缺失的必需依赖 ``[(导入名, 用途), ...]``。"""
    missing: list[tuple[str, str]] = []
    for name, purpose in REQUIRED_MODULES:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):  # 父包损坏时 find_spec 也会抛
            found = False
        if not found:
            missing.append((name, purpose))
    return missing


def _dependency_report(missing: list[tuple[str, str]]) -> str:
    lines = [
        "vtw 缺少运行依赖：" + "、".join(f"{name}（{purpose}）" for name, purpose in missing),
        f"当前解释器：{sys.executable}",
        "",
        "请用项目自带环境启动：",
        f'  "{paths.PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"}" '
        f'"{paths.PROJECT_ROOT / "run.pyw"}"',
        "或重新安装依赖：",
        f'  cd /d "{paths.PROJECT_ROOT}" && .venv\\Scripts\\python.exe '
        "-m pip install -r requirements.txt",
    ]
    return "\n".join(lines)


def _show_fatal(message: str) -> None:
    """把致命错误显示出来。

    ``pythonw.exe`` 没有控制台，只写 stderr 等于把错误丢进黑洞 —— 用户看到的
    就是"双击后什么都没发生"。所以窗口模式下用系统消息框，控制台模式照常打印。
    """
    print(message, file=sys.stderr)
    if not paths.IS_WINDOWED:
        return
    try:
        import ctypes

        # MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(None, message, "vtw 无法启动", 0x10 | 0x10000 | 0x40000)
    except Exception:  # noqa: BLE001 - 弹窗失败也不能影响退出码
        pass


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # 提权标记只给 run.pyw 看，不能传给 argparse
    raw = [a for a in raw if a != elevate.MARKER]
    args = build_parser().parse_args(raw)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)

    log = logging.getLogger("vtw")
    log.info("vtw v%s 启动，项目目录 %s", __version__, paths.PROJECT_ROOT)
    # 排障用：确认到底跑在哪个解释器上（venv 重定向启动器会让 sys.executable
    # 与直觉不同，出问题时这行是关键证据）
    log.info("解释器：%s（prefix=%s）", sys.executable, sys.prefix)

    admin = elevate.is_admin()
    log.info("管理员权限：%s", "是" if admin else "否")
    if not admin and elevate.wants_admin():
        # 提权只发生在 run.pyw 入口；直接用 ``python -m vtw`` 时这里只提醒
        log.warning(
            "当前不是管理员权限，全局键盘钩子可能收不到按键"
            "（尤其前台窗口是管理员程序时）。建议用 run.pyw 启动，"
            "或以管理员身份运行本命令。"
        )

    # ---- 依赖自检：必须在导入 app 之前 ----
    # 否则 ModuleNotFoundError 会在"按热键"时才炸在工作线程里，表现为静默无反应。
    missing = missing_modules()
    if missing:
        report = _dependency_report(missing)
        log.critical("缺少运行依赖，无法启动：%s", ", ".join(n for n, _ in missing))
        log.critical("%s", report)
        _show_fatal(report)
        return 2

    for name, purpose, hint in OPTIONAL_MODULES:
        try:
            if importlib.util.find_spec(name) is None:
                log.warning("可选依赖 %s 缺失，%s 将不可用（%s）", name, purpose, hint)
        except (ImportError, ValueError):
            log.warning("可选依赖 %s 无法检查，%s 可能不可用", name, purpose)

    guard = SingleInstance()
    if not args.allow_multiple and not guard.acquire():
        owner = guard.lock_owner()
        log.info(
            "检测到已有 vtw 实例正在运行（持有者 pid=%s），本次启动退出",
            owner or "未知",
        )
        print(f"vtw 已经在运行（pid={owner or '未知'}，见系统托盘）。")
        return 0

    try:
        # 延迟到单实例检查之后再导入：导入 tkinter/pystray 有一定开销
        from .app import App

        app = App(config_path=args.config)
        return app.run()
    except Exception:  # noqa: BLE001 - 顶层兜底，保证异常一定进日志
        log.exception("启动失败")
        raise
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
