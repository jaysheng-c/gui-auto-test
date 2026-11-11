"""断言类动作。

所有断言失败统一抛 :class:`AssertFailed`，并带上 ``expected`` / ``actual``，
报告里因此能直接看出"期望什么、实际什么"，而不只是一句"失败"。

断言**不参与重试**（见 :meth:`Element._with_retry` 的 ``_NON_RETRYABLE``）：
重试只用于消化环境抖动，而断言结果就是测试结论，重试会掩盖真实问题。
"""

from __future__ import annotations

import re
from typing import Any

from .actions import ACTION_REGISTRY, ActionResult, action
from .actions import _opt as opt_param
from .actions import _param as req_param
from .actions import _require as require_element
from .context import RunContext
from .element import Element
from .errors import AssertFailed
from .flow import Step
from .log import get_logger

__all__ = ["ASSERT_ACTIONS"]

_LOG = get_logger("asserts")

_VALID_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "contains", "not_contains", "matches")


def _fail(message: str, *, expected: Any, actual: Any, step: Step, element: Element | None) -> AssertFailed:
    detail = element.spec.describe() if element is not None else ""
    if step.desc:
        detail = f"{step.desc} | {detail}" if detail else step.desc
    return AssertFailed(message, expected=expected, actual=actual, detail=detail or None)


def _compare(actual: Any, expected: Any, op: str) -> bool:
    """按操作符比较，数字场景自动做数值转换。"""
    if op == "eq":
        return _loose_eq(actual, expected)
    if op == "ne":
        return not _loose_eq(actual, expected)
    if op in ("gt", "ge", "lt", "le"):
        try:
            a, b = float(actual), float(expected)
        except (TypeError, ValueError) as exc:
            raise AssertFailed(
                f"数值比较失败（{op}）", expected=expected, actual=actual,
                detail=f"无法转成数字: {exc}",
            ) from exc
        return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
    if op == "contains":
        return str(expected) in str(actual if actual is not None else "")
    if op == "not_contains":
        return str(expected) not in str(actual if actual is not None else "")
    if op == "matches":
        return re.search(str(expected), str(actual if actual is not None else "")) is not None
    raise AssertFailed(f"不支持的比较操作符 {op!r}", expected=expected, actual=actual,
                       detail=f"可选 {list(_VALID_OPS)}")


def _loose_eq(actual: Any, expected: Any) -> bool:
    """宽松相等。

    控件文本一律是 str，而 JSON 里写的期望值可能是数字或布尔；
    严格要求类型一致会让 ``"expect": 3`` 永远匹配不上控件的 ``"3"``。
    """
    if actual == expected:
        return True
    if actual is None or expected is None:
        return False
    if isinstance(actual, bool) != isinstance(expected, bool):
        # True 与 "true" 视为相等，但 True 与 1 不等价，避免语义混淆
        return str(actual).strip().lower() == str(expected).strip().lower()
    return str(actual) == str(expected)


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "none", "null")


# ======================================================================
# 存在性
# ======================================================================
@action("assert_exists", needs_target=True, category="assert",
        description="断言控件存在；expect=false 时反向断言")
def _assert_exists(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    want = _bool(step.expect, True)
    found = el.exists(el.timeout)
    if found != want:
        raise _fail(
            f"控件{'应存在但没找到' if want else '不应存在却找到了'}",
            expected=f"exists={want}", actual=f"exists={found}", step=step, element=el,
        )
    return ActionResult(message=f"exists={found}")


@action("assert_not_exists", needs_target=True, category="assert",
        description="断言控件不存在（消失）")
def _assert_not_exists(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    # 反向断言用极短探测：等满整个 timeout 只会白白拖慢用例
    found = el.exists(0.0)
    if found:
        raise _fail(
            "控件不应存在却找到了", expected="exists=False", actual="exists=True",
            step=step, element=el,
        )
    return ActionResult(message="控件不存在，符合预期")


# ======================================================================
# 文本
# ======================================================================
@action("assert_text", needs_target=True, category="assert",
        description="断言控件文本与期望相等（可用 op 切换比较方式）")
def _assert_text(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    expected = step.expect
    if expected is None:
        expected = req_param(step, "expect", "text")
    op = str(opt_param(step, "op", "eq"))
    if op not in _VALID_OPS:
        raise AssertFailed(f"不支持的 op {op!r}", detail=f"可选 {list(_VALID_OPS)}")
    actual = el.text()
    if not _compare(actual, expected, op):
        raise _fail(f"文本不匹配（{op}）", expected=expected, actual=actual, step=step, element=el)
    return ActionResult(message=f"text={actual!r}")


@action("assert_text_contains", needs_target=True, category="assert",
        description="断言控件文本包含期望子串")
def _assert_text_contains(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    expected = step.expect if step.expect is not None else req_param(step, "expect", "text")
    actual = el.text()
    if str(expected) not in actual:
        raise _fail("文本不包含期望子串", expected=expected, actual=actual, step=step, element=el)
    return ActionResult(message=f"text={actual!r}")


@action("assert_text_matches", needs_target=True, category="assert",
        description="断言控件文本匹配正则")
def _assert_text_matches(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    expected = str(step.expect if step.expect is not None else req_param(step, "expect", "pattern"))
    actual = el.text()
    if re.search(expected, actual) is None:
        raise _fail("文本不匹配正则", expected=expected, actual=actual, step=step, element=el)
    return ActionResult(message=f"text={actual!r}")


# ======================================================================
# 状态
# ======================================================================
@action("assert_enabled", needs_target=True, category="assert",
        description="断言控件可用；expect=false 时断言不可用")
def _assert_enabled(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    want = _bool(step.expect, True)
    actual = el.is_enabled()
    if actual != want:
        raise _fail("控件可用状态不符", expected=want, actual=actual, step=step, element=el)
    return ActionResult(message=f"enabled={actual}")


@action("assert_visible", needs_target=True, category="assert",
        description="断言控件可见；expect=false 时断言不可见")
def _assert_visible(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    want = _bool(step.expect, True)
    actual = el.is_visible()
    if actual != want:
        raise _fail("控件可见状态不符", expected=want, actual=actual, step=step, element=el)
    return ActionResult(message=f"visible={actual}")


@action("assert_checked", needs_target=True, category="assert",
        description="断言勾选状态；expect=false 时断言未勾选")
def _assert_checked(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    want = _bool(step.expect, True)
    actual = el.is_checked()
    if actual != want:
        raise _fail("勾选状态不符", expected=want, actual=actual, step=step, element=el)
    return ActionResult(message=f"checked={actual}")


@action("assert_selected", needs_target=True, category="assert",
        description="断言列表项/标签页处于选中状态")
def _assert_selected(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    want = _bool(step.expect, True)
    actual = el.get_property("is_selected")
    actual_b = _bool(actual, False)
    if actual_b != want:
        raise _fail("选中状态不符", expected=want, actual=actual_b, step=step, element=el)
    return ActionResult(message=f"selected={actual_b}")


@action("assert_expanded", needs_target=True, category="assert",
        description="断言树节点/下拉框展开状态")
def _assert_expanded(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    want = _bool(step.expect, True)
    actual = el.is_expanded()
    if actual is None:
        raise AssertFailed(
            "控件不支持展开状态读取", detail=f"{el.spec.describe()}；请确认是 TreeItem/ComboBox"
        )
    if actual != want:
        raise _fail("展开状态不符", expected=want, actual=actual, step=step, element=el)
    return ActionResult(message=f"expanded={actual}")


# ======================================================================
# 数量 / 窗口 / 属性 / 进程
# ======================================================================
@action("assert_count", needs_target=True, category="assert",
        description="断言匹配到的控件数量；op 支持 eq/ne/gt/ge/lt/le")
def _assert_count(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    expected = step.expect if step.expect is not None else req_param(step, "expect", "count")
    op = str(opt_param(step, "op", "eq"))
    actual = el.count()
    if not _compare(actual, expected, op):
        raise _fail(f"控件数量不符（{op}）", expected=expected, actual=actual, step=step, element=el)
    return ActionResult(message=f"count={actual}")


@action("assert_window_title", needs_target=True, category="assert",
        description="断言窗口标题；op 支持 eq/contains/matches")
def _assert_window_title(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "窗口定位条件")
    expected = step.expect if step.expect is not None else req_param(step, "expect", "title")
    op = str(opt_param(step, "op", "contains"))
    actual = el.title()
    if not _compare(actual, expected, op):
        raise _fail(f"窗口标题不符（{op}）", expected=expected, actual=actual, step=step, element=el)
    return ActionResult(message=f"title={actual!r}")


@action("assert_property", needs_target=True, category="assert", required_params=("property",),
        description="断言任意 UIA 属性值，用于框架未内置的校验点")
def _assert_property(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    name = str(req_param(step, "property"))
    expected = step.expect if step.expect is not None else opt_param(step, "value")
    op = str(opt_param(step, "op", "eq"))
    actual = el.get_property(name)
    if expected is not None and not _compare(actual, expected, op):
        raise _fail(
            f"属性 {name} 不符（{op}）", expected=expected, actual=actual, step=step, element=el
        )
    return ActionResult(message=f"{name}={actual!r}")


@action("assert_process_alive", category="assert",
        description="断言被测进程仍在运行，用于捕捉崩溃")
def _assert_process_alive(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    """看门狗断言。应用崩溃时 UIA 往往抛出一堆费解的 COM 错误，
    在关键节点插一条本断言能让报告直接指出"进程已退出"。
    """
    ctx.app.assert_alive()
    return ActionResult(message=f"进程存活 pid={ctx.app.pid}")


@action("assert_item_count", needs_target=True, category="assert",
        description="断言列表/下拉/表格的条目数")
def _assert_item_count(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = require_element(step, element, "target")
    expected = step.expect if step.expect is not None else req_param(step, "expect", "count")
    op = str(opt_param(step, "op", "eq"))
    actual = el.item_count()
    if not _compare(actual, expected, op):
        raise _fail(f"条目数不符（{op}）", expected=expected, actual=actual, step=step, element=el)
    return ActionResult(message=f"item_count={actual}")


# 便于 CLI / 文档列出全部断言动作（定义在本行以上的 @action 都已注册完毕）
ASSERT_ACTIONS = tuple(
    name for name, d in sorted(ACTION_REGISTRY.items()) if d.category == "assert"
)
