#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app GUI 自动化测试 —— 唯一入口。

用法::

    python auto_test.py --execute=E:/path/to/app.exe
    python auto_test.py --execute=... --suite=test_flow_file/suite_smoke.json
    python auto_test.py --execute=... --flow=tc001_launch --case=TC_LOGIN_001
    python auto_test.py --inspect --execute=... --wait=30
    python auto_test.py --validate
    python auto_test.py --list

完整参数见 ``python auto_test.py --help``，流程编写指南见 ``docs/flow_authoring.md``。

退出码：``0`` 全部通过 / ``1`` 存在未通过用例 / ``2`` 框架或配置错误。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

# 无论从哪个工作目录调用（双击、CI、绝对路径），都要能找到 app_at 包
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _set_dpi_awareness() -> None:
    """把进程设为 Per-Monitor V2 DPI 感知，逐级降级。

    **必须在 import pywinauto 之前执行**：进程的 DPI 感知只能在首次 UI/GDI 调用
    前设置一次，而 pywinauto 导入时会初始化 COM 与 UIA，之后再来就晚了。
    高分屏下若不设置，UIA 返回的控件坐标与真实屏幕坐标之间存在缩放偏差，
    ``click_input()`` 会整体点偏 —— 这类问题表现为"随机失败"，极难排查。

    这里**只用标准库 ctypes**，不 import 框架任何模块：入口必须在依赖尚未装好时
    也能给出可读的提示，而不是抛一个莫名其妙的 ImportError。
    """
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4，Win10 1703+，返回 BOOL
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    # PROCESS_PER_MONITOR_DPI_AWARE == 2，Win8.1+，返回 HRESULT（0 为成功）
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


_set_dpi_awareness()


def main(argv: list[str] | None = None) -> int:
    try:
        from app_at.cli import main as cli_main
    except ImportError as exc:
        sys.stderr.write(f"[FATAL] 无法导入框架 app_at: {exc}\n")
        sys.stderr.write(
            "请先安装依赖: .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
        )
        return 2
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
