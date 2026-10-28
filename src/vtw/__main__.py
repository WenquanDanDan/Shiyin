"""命令行入口：``python -m vtw``。

由 ``run.pyw``（配合 ``pythonw.exe``）调用时无控制台；直接 ``python -m vtw``
调试时日志会同时打到终端。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__, paths
from .logsetup import setup as setup_logging
from .single import SingleInstance


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)

    log = logging.getLogger("vtw")
    log.info("vtw v%s 启动，项目目录 %s", __version__, paths.PROJECT_ROOT)

    guard = SingleInstance()
    if not args.allow_multiple and not guard.acquire():
        log.info("检测到已有 vtw 实例正在运行，本次启动退出")
        print("vtw 已经在运行（见系统托盘）。")
        return 0

    try:
        # 延迟到单实例检查之后再导入：导入 tkinter/pystray 有一定开销
        from .app import App

        app = App(config_path=args.config)
        return app.run()
    except Exception:  # noqa: BLE001 - 顶层兜底，保证异常一定进日志
        logging.getLogger("vtw").exception("启动失败")
        raise
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
