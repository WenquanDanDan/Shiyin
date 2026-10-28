"""历史记录（JSONL）。

一行一条，方便追加与人工查看，也不会因为写坏一行而丢掉整个文件。
识别结果**先落盘再注入** —— 这样即使注入失败（目标程序不吃 Ctrl+V），
用户的语音内容也不会丢。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)


class History:
    """线程安全的追加式历史记录。"""

    def __init__(self, path: Path | None = None, *, enabled: bool = True) -> None:
        self.path = Path(path) if path else paths.HISTORY_FILE
        self.enabled = enabled
        self._lock = threading.Lock()

    def append(
        self,
        text: str,
        *,
        source: str = "mic",
        duration: float = 0.0,
        elapsed: float = 0.0,
        injected: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """追加一条记录。写盘失败只记日志，不影响主流程。"""
        if not self.enabled or not text:
            return

        record: dict[str, Any] = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "text": text,
            "duration": round(duration, 3),
            "elapsed": round(elapsed, 3),
            "injected": injected,
        }
        if extra:
            record.update(extra)

        line = json.dumps(record, ensure_ascii=False)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            log.error("写入历史记录失败：%s", exc)

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """读取最近 ``limit`` 条记录（用于"查看历史"）。"""
        if not self.path.is_file():
            return []
        try:
            with self._lock:
                lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.error("读取历史记录失败：%s", exc)
            return []

        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if len(out) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 跳过坏行，不影响其余记录
        out.reverse()
        return out
