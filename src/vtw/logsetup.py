"""日志初始化。

关键点：本工具由 ``pythonw.exe`` 启动（无控制台），此时 ``sys.stdout`` /
``sys.stderr`` 为 ``None``，任何 ``print()`` 都会抛 ``AttributeError``。
这里把两者替换成转发到日志文件的写入器，并接管线程/全局异常钩子 ——
否则后台线程里的异常在无控制台环境下会**静默丢失**。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from . import paths

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


class _LogStream:
    """把写入转发到 logger 的伪文件对象，用于替代 pythonw 下为 None 的 stdout/stderr。"""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level

    def write(self, message: str) -> int:
        text = (message or "").rstrip()
        if text:
            self._logger.log(self._level, "%s", text)
        return len(message or "")

    def flush(self) -> None:
        for handler in self._logger.handlers:
            try:
                handler.flush()
            except Exception:  # noqa: BLE001 - flush 失败不应影响调用方
                pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"


def _install_stream_redirects() -> None:
    """pythonw 下 stdout/stderr 为 None 时，用日志流顶上。"""
    root = logging.getLogger()
    if sys.stdout is None:
        sys.stdout = _LogStream(root, logging.INFO)  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _LogStream(root, logging.ERROR)  # type: ignore[assignment]


def _install_excepthooks() -> None:
    log = logging.getLogger("vtw.crash")

    def _global_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.critical("未捕获异常", exc_info=(exc_type, exc, tb))

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "线程 %s 未捕获异常",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _global_hook
    threading.excepthook = _thread_hook


def setup(level: int = logging.INFO, *, console: bool | None = None) -> logging.Logger:
    """初始化日志。幂等，可重复调用。

    :param level: 文件日志级别。
    :param console: 是否同时输出到控制台；``None`` 表示自动判断（有控制台才输出）。
    """
    global _configured
    root = logging.getLogger()
    if _configured:
        return root

    root.setLevel(logging.DEBUG)

    log_path: Path = paths.LOG_FILE
    try:
        paths.ensure_dir(log_path.parent)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_048_576, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        root.addHandler(file_handler)
    except OSError as exc:  # 磁盘满/无权限时不能因此起不来
        print(f"[vtw] 日志文件初始化失败: {exc}", file=sys.stderr or sys.__stderr__)

    if console is None:
        console = not paths.IS_WINDOWED and sys.stderr is not None
    if console:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(level)
        stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        root.addHandler(stream_handler)

    # 第三方库降噪
    for noisy in ("PIL", "pystray", "asyncio", "comtypes", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _install_stream_redirects()
    _install_excepthooks()

    _configured = True
    root.info("=" * 60)
    root.info("vtw 日志初始化完成 (pythonw=%s, pid=%d)", paths.IS_WINDOWED, _pid())
    return root


def _pid() -> int:
    import os

    return os.getpid()
