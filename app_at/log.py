"""日志配置。

控制台使用 rich 输出（可读性优先），文件使用纯文本 UTF-8（便于 grep 与归档）。

注意 ``RichHandler(markup=False)``：控件树、UIA 属性里大量出现
``[Button]`` / ``[class='...']`` 这类方括号，若开启 rich 标记解析会被当成
样式标签，导致输出被吞掉甚至抛错。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from .compat import supported_kwargs

__all__ = ["setup_logging", "get_logger", "FILE_FORMAT", "CONSOLE_FORMAT"]

FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
CONSOLE_FORMAT = "%(message)s"

# 第三方库在 DEBUG 级别下极其聒噪，统一压到 WARNING
_NOISY_LOGGERS = (
    "pywinauto",
    "comtypes",
    "urllib3",
    "PIL",
    "asyncio",
)

_configured = False


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> logging.Logger:
    """初始化根日志器，可重复调用（幂等，仅首次生效）。

    :param level: 日志级别名，非法值回退到 INFO
    :param log_file: 日志文件路径，None 表示不落盘
    :return: 框架根 logger（名为 ``app_at``）
    """
    global _configured

    _harden_stdio()
    root = logging.getLogger()
    lvl = getattr(logging, str(level).upper(), logging.INFO)

    if not _configured:
        root.setLevel(lvl)
        # 清掉可能已存在的 handler，避免重复输出（例如被 pytest 或宿主程序预置）
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(_build_console_handler(lvl))
        _configured = True
    else:
        root.setLevel(lvl)
        for h in root.handlers:
            if isinstance(h, _rich_handler_type()):
                h.setLevel(lvl)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(lvl, logging.WARNING))

    if log_file is not None:
        _attach_file_handler(root, log_file, lvl)

    return logging.getLogger("app_at")


def get_logger(name: str) -> logging.Logger:
    """获取框架命名空间下的子 logger。"""
    return logging.getLogger(f"app_at.{name}")


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _rich_handler_type() -> type:
    try:
        from rich.logging import RichHandler

        return RichHandler
    except ImportError:  # pragma: no cover - rich 缺失时降级
        return logging.StreamHandler


def _build_console_handler(level: int) -> logging.Handler:
    fmt = logging.Formatter(CONSOLE_FORMAT, datefmt="%H:%M:%S")
    try:
        from rich.logging import RichHandler

        wanted: dict[str, Any] = {
            "level": level,
            "show_time": True,
            "show_path": False,
            "markup": False,
            "rich_tracebacks": True,
            # 保留重复栈帧：UIA 的报错往往靠“同一帧出现几次”区分调用来源
            "omit_repeated_frames": False,
            "log_time_format": "[%H:%M:%S]",
        }
        handler: logging.Handler = RichHandler(**supported_kwargs(RichHandler, wanted))
    except ImportError:  # pragma: no cover - rich 缺失时降级为普通输出
        handler = logging.StreamHandler()
        fmt = logging.Formatter(f"%(asctime)s | %(levelname)-7s | {CONSOLE_FORMAT}", "%H:%M:%S")
    handler.setFormatter(fmt)
    handler.setLevel(level)
    return handler


def _harden_stdio() -> None:
    """把标准输出的编码错误策略放宽为 ``replace``。

    中文 Windows 控制台默认 GBK，而日志里难免出现 GBK 编不出的字符
    （控件名里的生僻字、emoji、装饰符号）。rich 的 legacy console 写失败会在
    **logging 内部**抛 ``UnicodeEncodeError``，刷出几十行 ``--- Logging error ---``
    并把真正的运行结果淹没掉 —— 而报告文件是 utf-8 写的，问题只在控制台这一层，
    因此这里只降级控制台编码策略，不去改代码页。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - 被宿主程序替换过的流
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 已关闭或不可重配置
            continue


def _attach_file_handler(root: logging.Logger, log_file: Path, level: int) -> None:
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # encoding 必须显式指定 utf-8：中文 Windows 默认 GBK，
    # 写入控件名等字符时可能报 UnicodeEncodeError，且报告归档后跨平台读取会乱码
    handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
