"""第三方库的版本兼容垫片。

pywinauto 与 rich 都在小版本间改过公开签名（``print_control_identifiers`` 的
``max_width``、``RichHandler`` 的 ``omit_repeated_frames``）。这类差异直接透传会
抛 ``TypeError``，而且报错发生在**日志初始化**或**失败留证**这种最不该出问题的
位置，报错信息也完全指不向真正的成因。

统一在这里按“当前安装版本的实际签名”过滤关键字参数：多写的新参数被安全忽略，
已有的行为不受影响，升级/降级依赖都不需要改调用方。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

__all__ = ["supported_kwargs", "call_with_supported"]


def supported_kwargs(target: Any, wanted: dict[str, Any]) -> dict[str, Any]:
    """过滤出 ``target`` 实际接受的关键字参数。

    :param target: 类（取其 ``__init__`` 签名）或可调用对象（取其自身签名）
    :param wanted: 想要传入的全部关键字参数
    :return: 只保留签名里存在的项；签名不可知或接受 ``**kwargs`` 时原样返回
    """
    func: Any = target
    if isinstance(target, type):
        func = target.__init__
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - C 实现或内置类型
        return wanted
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return wanted
    return {k: v for k, v in wanted.items() if k in params}


def call_with_supported(func: Callable[..., Any], **wanted: Any) -> Any:
    """按 ``func`` 的实际签名过滤后调用。"""
    return func(**supported_kwargs(func, wanted))
