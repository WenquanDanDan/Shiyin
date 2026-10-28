"""vtw 无控制台启动入口。

配合 ``.venv\\Scripts\\pythonw.exe`` 使用：``pythonw.exe run.pyw``
不会创建控制台窗口，适合开机自启与日常使用。

需要看日志时改用 ``启动(调试).bat``，或直接运行 ``python -m vtw -v``。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vtw.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
