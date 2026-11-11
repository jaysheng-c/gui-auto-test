"""框架异常体系。

异常分两大类，``runner`` 与 ``cli`` 依据 ``exit_code`` 区分处理：

* **框架/配置错误**（``exit_code = 2``）：配置缺失、JSON 格式非法、应用无法启动等。
  这类错误说明"测试没跑起来"，不应被统计为用例失败。
* **用例执行失败**（``exit_code = 1``）：控件找不到、断言不通过、动作执行异常。
  这类错误是测试的正常产出，会被记入报告。
"""

from __future__ import annotations

__all__ = [
    "AppAtError",
    "ConfigError",
    "FlowFormatError",
    "AppLaunchError",
    "AppCrashedError",
    "ActionNotFoundError",
    "VisionNotImplementedError",
    "StepFailure",
    "ElementNotFoundError",
    "ElementNotReadyError",
    "ActionError",
    "AssertFailed",
    "VariableError",
    "FocusLostError",
    "FlowAborted",
    "FlowSkipped",
]


class AppAtError(Exception):
    """框架基础异常，默认视为框架错误。"""

    exit_code = 2

    def __init__(self, message: str = "", *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 仅影响展示
        if self.detail:
            return f"{self.message} | {self.detail}"
        return self.message


# --------------------------------------------------------------------------
# 框架 / 配置错误
# --------------------------------------------------------------------------
class ConfigError(AppAtError):
    """配置非法或缺失，例如未提供 ``--execute`` 且配置中 ``app.path`` 为空。"""


class FlowFormatError(AppAtError):
    """流程 JSON 文件解析或 schema 校验失败。

    ``detail`` 中带上文件路径与字段路径，便于直接定位到出错位置。
    """


class AppLaunchError(AppAtError):
    """被测应用启动失败，或启动后找不到主窗口。"""


class AppCrashedError(AppAtError):
    """被测应用在运行过程中退出或崩溃。"""


class ActionNotFoundError(AppAtError):
    """流程中引用了未注册的动作名。"""


class VisionNotImplementedError(AppAtError):
    """图像定位尚未实现。

    本期 ``vision.py`` 仅保留空入口：接口、路由、配置项均已就位，
    实际调用时抛出本异常，提示改用控件定位或等待图像能力落地。
    """


# --------------------------------------------------------------------------
# 用例执行失败
# --------------------------------------------------------------------------
class StepFailure(AppAtError):
    """用例步骤失败基类，计入报告失败数。"""

    exit_code = 1


class ElementNotFoundError(StepFailure):
    """在 ``timeout`` 内未能定位到目标控件。

    框架会自动落盘控件树快照，排查时对着快照修正 ``target`` 即可。
    """


class ElementNotReadyError(StepFailure):
    """控件已定位到，但未满足就绪条件（不可见 / 不可用 / 无坐标）。"""


class ActionError(StepFailure):
    """动作执行过程中出错，例如 UIA Pattern 不被 Qt 控件支持。"""


class AssertFailed(StepFailure):
    """断言不通过。"""

    def __init__(
        self,
        message: str = "",
        *,
        detail: str | None = None,
        expected: object = None,
        actual: object = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.expected = expected
        self.actual = actual


class VariableError(StepFailure):
    """流程中引用了未定义的变量。

    故意**不做**"保留字面量并告警"的宽容处理：``${password}`` 未被替换而原样
    输入登录框，会让一次配置疏漏伪装成产品缺陷，排查成本远高于立即失败。
    """

    def __init__(self, name: str, *, known: list[str] | None = None) -> None:
        detail = None
        if known:
            preview = ", ".join(sorted(known)[:40])
            detail = f"当前可用变量: {preview}"
        super().__init__(f"变量未定义: ${{{name}}}", detail=detail)
        self.name = name


class FocusLostError(StepFailure):
    """前台窗口不是被测应用，点击可能落到别处。

    报告中会标注 ``FOCUS_LOST``，以便与真实产品缺陷区分。
    """


# --------------------------------------------------------------------------
# 流程控制信号（非失败）
# --------------------------------------------------------------------------
class FlowAborted(Exception):
    """``on_fail: abort`` 触发，终止当前流程剩余步骤，直接进入 teardown。"""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class FlowSkipped(Exception):
    """``if`` 条件不成立，或 ``on_fail: skip_flow`` 触发，跳过剩余步骤。"""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
