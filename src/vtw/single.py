"""单实例保护。

第二次双击 ``run.pyw`` 时不应产生第二个托盘图标。用命名互斥体实现：
已有实例存在时 ``GetLastError()`` 返回 ``ERROR_ALREADY_EXISTS`` (183)。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\VtwVoiceToText_SingleInstance"

kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    """上下文管理器：退出时自动释放互斥体。"""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self._handle: int | None = None
        self.already_running = False

    def acquire(self) -> bool:
        """尝试取得唯一所有权。返回 ``True`` 表示本进程是唯一实例。"""
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        last_error = ctypes.get_last_error()

        if not handle:
            # 创建失败时不要阻止程序启动，只记日志
            log.warning("创建互斥体失败（错误码 %s），跳过单实例检查", last_error)
            self.already_running = False
            return True

        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            self.already_running = True
            return False

        self._handle = handle
        self.already_running = False
        return True

    def release(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
