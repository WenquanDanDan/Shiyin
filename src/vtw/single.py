"""单实例保护。

第二次双击 ``run.pyw`` 时不应产生第二个托盘图标，更不应让第二个进程去抢全局
热键。这里用**两层**判断：

1. 命名互斥体 ``Local\\VtwVoiceToText_SingleInstance``（首选，零副作用）。
2. 独占锁文件（兜底）。

为什么需要第二层
----------------
在受限/无交互桌面会话里 ``CreateMutexW`` 会返回 ``ERROR_ACCESS_DENIED``（错误码 5）。
原实现遇到这种情况只记一条警告就"跳过单实例检查"，结果是**两个实例并存**：
后启动的那个会装上自己的 ``WH_KEYBOARD_LL`` 钩子，并且因为后装先调用，
把热键事件吞掉 —— 正常实例从此再也收不到按键，用户看到的就是
"按了快捷键没反应"。所以互斥体不可用时必须还有别的办法判断重复。

锁文件用 ``CreateFileW(..., dwShareMode=0)`` 打开：进程退出时句柄由系统回收，
不存在"残留锁"问题；第二个进程会拿到 ``ERROR_SHARING_VIOLATION``（错误码 32）。
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_ALREADY_EXISTS = 183

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x80

MUTEX_NAME = "Local\\VtwVoiceToText_SingleInstance"

#: ctypes 里 HANDLE 是无符号的，INVALID_HANDLE_VALUE 需要按位取反得到全 1
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.SetFilePointer.argtypes = [
    wintypes.HANDLE,
    ctypes.c_long,
    ctypes.POINTER(ctypes.c_long),
    wintypes.DWORD,
]
kernel32.SetFilePointer.restype = wintypes.DWORD
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_char_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def default_lock_path(name: str) -> Path:
    """给某个互斥体名字算出稳定的锁文件路径。

    放在项目 ``logs`` 目录里 —— 这个目录程序本来就要写日志，权限一定够；
    ``%LOCALAPPDATA%`` 在受限策略下可能不可写，反而会让单实例检查失效。
    """
    from . import paths

    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[-40:].strip("_") or "vtw"
    return paths.LOGS_DIR / "locks" / f"{safe}-{digest}.lock"


class SingleInstance:
    """上下文管理器：退出时自动释放互斥体与锁文件。"""

    def __init__(self, name: str = MUTEX_NAME, lock_path: Path | None = None) -> None:
        self.name = name
        self.lock_path = Path(lock_path) if lock_path is not None else default_lock_path(name)
        self._handle: int | None = None
        self._lock_handle: int | None = None
        self.already_running = False

    # -- 互斥体 -----------------------------------------------------------
    def _acquire_mutex(self) -> bool | None:
        """``True`` 取得所有权 / ``False`` 已有实例 / ``None`` 无法判断。"""
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        last_error = ctypes.get_last_error()

        if not handle:
            if last_error == ERROR_ACCESS_DENIED:
                # 受限会话常见：没有权限创建同名互斥体（可能是其它会话留下的）。
                # 这不代表"已有实例"，交给锁文件判断。
                log.debug("互斥体创建被拒绝（错误码 5），改用锁文件判断")
            else:
                log.warning("创建互斥体失败（错误码 %s），改用锁文件判断", last_error)
            return None

        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False

        self._handle = handle
        return True

    # -- 锁文件 -----------------------------------------------------------
    def _acquire_lock(self) -> bool | None:
        """``True`` 取得独占 / ``False`` 已被占用 / ``None`` 无法判断。"""
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("创建锁文件目录失败：%s", exc)
            return None

        ctypes.set_last_error(0)
        handle = kernel32.CreateFileW(
            str(self.lock_path),
            GENERIC_READ | GENERIC_WRITE,
            # 允许别人**读**（好让后来者能从锁文件里读到"是谁占着"），
            # 但仍然拒绝别的写入/独占打开，第二个实例照样拿不到
            FILE_SHARE_READ,
            None,
            OPEN_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        last_error = ctypes.get_last_error()

        if handle == INVALID_HANDLE_VALUE or not handle:
            if last_error == ERROR_SHARING_VIOLATION:
                return False  # 已被另一个实例独占
            # 权限问题不代表"已有实例"，返回 None 让调用方自行决定放不放行
            log.warning("打开锁文件失败（错误码 %s）：%s", last_error, self.lock_path)
            return None

        self._lock_handle = handle
        self._write_owner_pid(handle)
        return True

    def _write_owner_pid(self, handle: int) -> None:
        """把本进程 pid 写进锁文件，方便排查"到底是谁占着单实例锁"。"""
        try:
            kernel32.SetFilePointer(handle, 0, None, 0)
            data = str(os.getpid()).encode("ascii")
            written = wintypes.DWORD(0)
            kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
        except Exception:  # noqa: BLE001 - 纯诊断信息，失败不影响单实例判定
            log.debug("写入锁文件 pid 失败", exc_info=True)

    def lock_owner(self) -> str:
        """读锁文件里记录的持有者 pid；读不到返回空串。"""
        try:
            return self.lock_path.read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            return ""

    # -- 对外接口 ---------------------------------------------------------
    def acquire(self) -> bool:
        """尝试取得唯一所有权。返回 ``True`` 表示本进程是唯一实例。"""
        mutex_state = self._acquire_mutex()
        if mutex_state is False:
            self.already_running = True
            return False

        lock_state = self._acquire_lock()
        if lock_state is False:
            self._release_mutex()
            self.already_running = True
            return False

        if mutex_state is None and lock_state is None:
            # 两条路都判断不了：宁可让程序启动，也不要因为权限问题打不开
            log.warning("互斥体与锁文件都不可用，跳过单实例检查")

        self.already_running = False
        return True

    def _release_mutex(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def _release_lock(self) -> None:
        if self._lock_handle:
            kernel32.CloseHandle(self._lock_handle)
            self._lock_handle = None

    def release(self) -> None:
        self._release_lock()
        self._release_mutex()

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
