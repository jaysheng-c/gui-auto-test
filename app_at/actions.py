"""动作注册表与实现。

新增一个动作 = 在本文件加一个 ``@action(...)`` 装饰的函数，**无需改动框架其他代码**。

统一签名::

    def fn(ctx: RunContext, step: Step, element: Element | None) -> ActionResult | None

* ``step`` 已完成变量绑定，直接读属性即可；
* ``element`` 由 dispatcher 按 ``needs_target`` 构造，窗口级动作拿到的是窗口本身；
* 失败请抛 :mod:`errors` 中的异常，不要返回 ``ok=False``，这样 runner 才能
  统一触发重试、留证与 ``on_fail`` 策略。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import RunContext, WindowRef
from .element import Element, escape_keys, find_descendant
from .errors import (
    ActionError,
    ActionNotFoundError,
    AssertFailed,
    ConfigError,
    ElementNotFoundError,
)
from .flow import Step
from .locator import LocatorSpec
from .log import get_logger

__all__ = [
    "ActionResult",
    "ActionDef",
    "ACTION_REGISTRY",
    "action",
    "get_action",
    "make_element",
    "eval_condition",
    "action_names",
]

_LOG = get_logger("actions")

ActionFn = Callable[[RunContext, Step, "Element | None"], "ActionResult | None"]


@dataclass
class ActionResult:
    """动作执行结果。断言失败通过抛异常表达，不通过这里返回。"""

    message: str = ""
    data: Any = None
    skipped: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ActionDef:
    """一个动作的元信息。"""

    name: str
    fn: ActionFn
    needs_target: bool = False
    category: str = ""
    required_params: tuple[str, ...] = ()
    description: str = ""


ACTION_REGISTRY: dict[str, ActionDef] = {}


def action(
    name: str,
    *,
    needs_target: bool = False,
    category: str = "",
    required_params: tuple[str, ...] | list[str] = (),
    description: str = "",
) -> Callable[[ActionFn], ActionFn]:
    """注册动作。重名视为编码错误，直接抛异常而不是静默覆盖。"""

    def decorator(fn: ActionFn) -> ActionFn:
        if name in ACTION_REGISTRY:
            raise ConfigError(f"动作重复注册: {name}")
        ACTION_REGISTRY[name] = ActionDef(
            name=name,
            fn=fn,
            needs_target=needs_target,
            category=category,
            required_params=tuple(required_params),
            description=description or _first_doc_line(fn),
        )
        return fn

    return decorator


def _first_doc_line(fn: Callable[..., Any]) -> str:
    """取函数 docstring 首行作为动作描述，避免显式声明与注释不同步。"""
    doc = (fn.__doc__ or "").strip()
    return doc.splitlines()[0].strip() if doc else ""


def get_action(name: str) -> ActionDef:
    try:
        return ACTION_REGISTRY[name]
    except KeyError:
        import difflib

        hint = difflib.get_close_matches(name, list(ACTION_REGISTRY), n=3, cutoff=0.6)
        raise ActionNotFoundError(
            f"未注册的动作: {name!r}",
            detail=(f"是否想写 {hint}？" if hint else "") + f" 可用动作共 {len(ACTION_REGISTRY)} 个，"
            "清单见 docs/flow_authoring.md",
        ) from None


def action_names(category: str | None = None) -> list[str]:
    names = [
        n for n, d in sorted(ACTION_REGISTRY.items()) if category is None or d.category == category
    ]
    return names


# ----------------------------------------------------------------------
# Element 构造
# ----------------------------------------------------------------------
def make_element(ctx: RunContext, step: Step) -> Element | None:
    """按步骤的 ``target`` / ``window`` 构造 Element。

    没有 ``target`` 但有 ``window`` 时，构造一个空定位条件的 Element ——
    它解析出来就是窗口本身，于是窗口级动作（maximize/close_window 等）
    与控件级动作共用同一套等待、重试、留证逻辑，不需要另写一条分支。
    """
    if step.target is not None:
        spec = step.target
    elif step.window:
        spec = LocatorSpec(window=step.window)
    else:
        return None
    return Element(
        ctx,
        spec,
        timeout=step.timeout,
        interval=step.interval,
        retries=step.retries,
    )


def _require(step: Step, element: Element | None, what: str) -> Element:
    if element is None:
        raise ActionError(
            f"动作 {step.action!r} 需要 target 或 window", detail=f"缺少{what}"
        )
    return element


def _param(step: Step, name: str, *alt: str) -> Any:
    """读取动作参数，支持别名（如 dx/x）。取不到抛 ActionError。"""
    for key in (name, *alt):
        if key in step.params and step.params[key] is not None:
            return step.params[key]
    if step.value is not None and name in ("value", "text", "keys"):
        return step.value
    raise ActionError(
        f"动作 {step.action!r} 缺少必填参数 {name!r}",
        detail=f"已提供参数: {sorted(step.params)}" + (f"；备选名 {list(alt)}" if alt else ""),
    )


def _opt(step: Step, name: str, default: Any = None, *alt: str) -> Any:
    for key in (name, *alt):
        if key in step.params and step.params[key] is not None:
            return step.params[key]
    return default


# ======================================================================
# 窗口
# ======================================================================
@action("activate_window", needs_target=True, category="window", description="把窗口激活并置于前台")
def _activate_window(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "窗口定位条件")
    el.activate()
    ctx.app.require_foreground(el.wait("exists"), timeout=5.0)
    return ActionResult(message="已激活窗口")


@action("maximize", needs_target=True, category="window", description="最大化窗口")
def _maximize(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "窗口定位条件").maximize()
    return ActionResult()


@action("restore", needs_target=True, category="window", description="还原窗口")
def _restore(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "窗口定位条件").restore()
    return ActionResult()


@action("minimize", needs_target=True, category="window", description="最小化窗口")
def _minimize(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "窗口定位条件").minimize()
    return ActionResult()


@action("close_window", needs_target=True, category="window", description="关闭窗口；force=true 时直接结束进程")
def _close_window(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    force = bool(_opt(step, "force", False))
    timeout = float(_opt(step, "wait", 10.0))
    if force:
        ctx.app.close(force=True)
        return ActionResult(message="已强制结束被测进程")
    el = _require(step, element, "窗口定位条件")
    try:
        el.close()
    except Exception as exc:  # noqa: BLE001
        # teardown 里很常见：前一步 Alt+F4 已经把窗口关掉了，此时 WM_CLOSE 无处可发。
        # 绝不能就此抛出 —— 一旦抛出，下面的“等退出 + 强制结束”兜底就都不会执行，
        # 结果反而是留下一具僵尸进程污染下一条用例。记 debug 后继续等进程退出即可。
        _LOG.debug("发送 WM_CLOSE 失败（%s），继续等待进程退出", exc)
    if not ctx.app.wait_exit(timeout):
        _LOG.warning("窗口关闭后进程仍在运行，%.0fs 后将强制结束", timeout)
        ctx.app.close(force=True)
    return ActionResult(message="窗口已关闭")


@action("switch_window", category="window", required_params=("alias",),
        description="注册窗口别名，后续步骤可用 target.window 引用")
def _switch_window(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    """把 ``target`` 描述的条件登记为窗口别名。

    典型用途：登录后弹出的对话框、二级窗口。注册后其余步骤写
    ``"window": "export_dialog"`` 即可，比每步重复一长串条件可读得多。
    """
    alias = str(_param(step, "alias"))
    spec = step.target
    ref = WindowRef(alias=alias, source="switch_window")
    if spec is not None:
        crit = spec.criteria()
        ref.title = crit.get("title")
        ref.title_re = crit.get("title_re")
        ref.auto_id = crit.get("auto_id")
        ref.class_name = crit.get("class_name")
        ref.best_match = crit.get("best_match")
        ref.depth = crit.get("depth")
    if ref.process is None and ctx.app and ctx.app.pid:
        # 限定进程，避免匹配到同名的其他应用窗口
        ref.process = ctx.app.pid
    if not ref.criteria():
        raise ActionError(f"switch_window 需要至少一个窗口识别条件", detail=f"alias={alias}")

    ctx.register_window(ref)
    states = str(_opt(step, "states", "exists"))
    if element is not None:
        element.wait(states, step.timeout)
    return ActionResult(message=f"{ref.describe()}")


@action("wait_window", needs_target=True, category="window", description="等待窗口达到指定状态")
def _wait_window(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "窗口定位条件")
    states = str(_opt(step, "states", "exists"))
    wrapper = el.wait(states, step.timeout)
    return ActionResult(message=f"窗口已就绪 [{states}]: {wrapper.window_text()!r}")


# ======================================================================
# 鼠标
# ======================================================================
@action("click", needs_target=True, category="mouse", description="点击控件；by=mouse 走真实鼠标（Qt 推荐）")
def _click(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    el.click(
        by=_opt(step, "by"),
        button=str(_opt(step, "button", "left")),
        double=bool(_opt(step, "double", False)),
    )
    return ActionResult()


@action("double_click", needs_target=True, category="mouse", description="双击控件")
def _double_click(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    el.double_click(by=_opt(step, "by"), button=str(_opt(step, "button", "left")))
    return ActionResult()


@action("right_click", needs_target=True, category="mouse", description="右键点击控件")
def _right_click(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    el.right_click()
    return ActionResult()


@action("click_offset", needs_target=True, category="mouse", required_params=("dx", "dy"),
        description="按控件内相对坐标点击，用于 UIA 看不到的自绘控件")
def _click_offset(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    """自绘控件（图表、画布、自定义表格）在 UIA 树里常是一个黑盒 Pane，
    内部元素无法定位，只能按相对坐标点击。偏移以控件左上角为原点，
    因此窗口移动不影响结果。
    """
    el = _require(step, element, "target")
    dx = float(_param(step, "dx", "x"))
    dy = float(_param(step, "dy", "y"))
    el.click_offset(dx, dy, button=str(_opt(step, "button", "left")),
                    double=bool(_opt(step, "double", False)))
    return ActionResult(message=f"相对坐标点击 ({dx}, {dy})")


@action("drag_drop", needs_target=True, category="mouse", required_params=("to",),
        description="拖拽到另一个控件或绝对坐标")
def _drag_drop(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    to = _param(step, "to")
    if isinstance(to, dict):
        dst: Any = Element(ctx, LocatorSpec.from_dict(to, where="to"), timeout=step.timeout)
    elif isinstance(to, (list, tuple)) and len(to) == 2:
        dst = (int(to[0]), int(to[1]))
    else:
        raise ActionError("drag_drop 的 to 必须是 target 对象或 [x, y] 坐标", detail=f"实际 {to!r}")
    el.drag_to(dst, button=str(_opt(step, "button", "left")))
    return ActionResult()


@action("scroll", needs_target=True, category="mouse", description="滚动控件")
def _scroll(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    direction = str(_opt(step, "direction", "down"))
    amount = str(_opt(step, "amount", "line"))
    # count 被 repeat 动作占为 Step 的具名字段（见 flow.STEP_KNOWN_KEYS），
    # 因此 JSON 里写的 "count" 落在 step.count 而不是 params，两处都要看
    raw_count = _opt(step, "count", None)
    if raw_count is None:
        raw_count = 3 if step.count is None else step.count
    count = int(raw_count)
    el.scroll(direction, amount, count)
    return ActionResult(message=f"滚动 {direction}/{amount} x{count}")


# ======================================================================
# 键盘 / 文本
# ======================================================================
@action("set_text", needs_target=True, category="keyboard",
        description="写入文本（先清空）；默认走 ValuePattern，不受输入法影响")
def _set_text(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    value = step.value
    if value is None:
        value = _opt(step, "text", "")
    el.set_text("" if value is None else str(value), by=_opt(step, "by"),
                verify=bool(_opt(step, "verify", False)))
    return ActionResult()


@action("type_keys", needs_target=True, category="keyboard",
        description="发送按键序列；literal=true 时按纯文本转义输入")
def _type_keys(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    keys = step.value if step.value is not None else _param(step, "keys")
    el.type_keys(str(keys), literal=bool(_opt(step, "literal", False)),
                 pause=float(_opt(step, "pause", 0.03)))
    return ActionResult()


@action("clear_text", needs_target=True, category="keyboard", description="清空文本框")
def _clear_text(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    el.clear_text()
    return ActionResult()


@action("hotkey", needs_target=True, category="keyboard",
        description="发送组合键，如 ^s 保存、%{F4} 关闭")
def _hotkey(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    keys = str(step.value if step.value is not None else _param(step, "keys"))
    el.hotkey(keys)
    return ActionResult(message=f"组合键 {keys}")


@action("press_key", needs_target=True, category="keyboard", description="发送单个按键，如 ENTER / TAB / F5")
def _press_key(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    key = str(step.value if step.value is not None else _param(step, "key"))
    el.press_key(key)
    return ActionResult(message=f"按键 {key}")


# ======================================================================
# 控件状态
# ======================================================================
@action("select_item", needs_target=True, category="control", description="选择下拉项/列表项/标签页")
def _select_item(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    item = step.value if step.value is not None else _param(step, "item")
    el.select_item(item)
    return ActionResult(message=f"已选择 {item!r}")


@action("check", needs_target=True, category="control", description="勾选（已勾选则不动，幂等）")
def _check(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "target").check()
    return ActionResult()


@action("uncheck", needs_target=True, category="control", description="取消勾选（幂等）")
def _uncheck(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "target").uncheck()
    return ActionResult()


@action("toggle", needs_target=True, category="control", description="切换一次勾选状态")
def _toggle(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "target").toggle()
    return ActionResult()


@action("expand", needs_target=True, category="control", description="展开树节点/下拉框")
def _expand(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "target").set_expanded(True)
    return ActionResult()


@action("collapse", needs_target=True, category="control", description="折叠树节点/下拉框")
def _collapse(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "target").set_expanded(False)
    return ActionResult()


@action("set_value", needs_target=True, category="control", description="设置数值（滑块等）")
def _set_value(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    value = step.value if step.value is not None else _param(step, "value")
    el.set_value(value)
    return ActionResult(message=f"已设置数值 {value!r}")


@action("focus", needs_target=True, category="control", description="把焦点设到控件上")
def _focus(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    _require(step, element, "target").focus()
    return ActionResult()


@action("menu_select", needs_target=True, category="control", required_params=("path",),
        description="按路径选菜单；Qt 自绘菜单可能不支持，此时改用逐级 click")
def _menu_select(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    path = str(_param(step, "path"))
    el.menu_select(path, exact=bool(_opt(step, "exact", False)))
    return ActionResult(message=f"菜单 {path}")


# ======================================================================
# 等待
# ======================================================================
@action("sleep", category="wait", description="固定等待。能不用就不用，优先 wait_exists")
def _sleep(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    raw = step.value if step.value is not None else _opt(step, "seconds", 1.0)
    seconds = ctx.scaled(float(raw))
    _LOG.debug("固定等待 %.2fs", seconds)
    time.sleep(seconds)
    return ActionResult(message=f"等待 {seconds:.2f}s")


@action("wait_exists", needs_target=True, category="wait", description="等待控件出现")
def _wait_exists(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    states = str(_opt(step, "states", "exists"))
    el.wait(states, step.timeout)
    return ActionResult(message=f"控件已出现 [{states}]")


@action("wait_gone", needs_target=True, category="wait",
        description="等待控件消失，用于等待进度条/遮罩结束")
def _wait_gone(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    limit = el.timeout
    deadline = time.monotonic() + limit
    while True:
        if not el.exists(0.0):
            return ActionResult(message="控件已消失")
        if time.monotonic() >= deadline:
            raise ElementNotFoundError(
                f"控件在 {limit:.1f}s 内未消失",
                detail=f"{el.spec.describe()}；常见于加载遮罩未关闭或弹窗未处理",
            )
        time.sleep(el.interval)


@action("wait_enabled", needs_target=True, category="wait", description="等待控件变为可用")
def _wait_enabled(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    el.wait("exists enabled", step.timeout)
    return ActionResult(message="控件已可用")


@action("wait_text", needs_target=True, category="wait",
        description="等待控件文本达到期望值，用于等待异步加载完成")
def _wait_text(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    """比 ``sleep`` 可靠得多的同步手段：轮询文本直到匹配，匹配即刻返回。"""
    el = _require(step, element, "target")
    expected = step.expect if step.expect is not None else step.value
    if expected is None:
        raise ActionError("wait_text 需要 expect 或 value")
    mode = str(_opt(step, "match", "contains"))  # contains | equals | regex
    expected_s = str(expected)
    limit = el.timeout
    deadline = time.monotonic() + limit
    last = ""
    while True:
        try:
            last = el.text()
        except Exception:  # noqa: BLE001 - 控件可能还没出现，继续轮询
            last = ""
        if _text_matches(last, expected_s, mode):
            return ActionResult(message=f"文本已匹配: {last!r}")
        if time.monotonic() >= deadline:
            raise AssertFailed(
                f"等待文本超时（{limit:.1f}s，匹配方式 {mode}）",
                expected=expected_s, actual=last, detail=el.spec.describe(),
            )
        time.sleep(el.interval)


def _text_matches(actual: str, expected: str, mode: str) -> bool:
    if mode == "equals":
        return actual == expected
    if mode == "regex":
        return re.search(expected, actual) is not None
    return expected in actual


# ======================================================================
# 系统对话框
# ======================================================================
@action("handle_dialog", needs_target=True, category="dialog",
        description="处理弹窗：定位对话框并点击指定按钮")
def _handle_dialog(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    """按 ``confirm`` 指定的按钮文本点击，用于确认框/警告框。

    按钮名默认覆盖中英文系统：``确定|确认|是|OK|Yes|Open|Save``。
    注意拿到的是 wrapper，只能用 ``descendants`` 系查找（参见 ``find_descendant``）。
    """
    el = _require(step, element, "target")
    dlg = el.wait("exists", step.timeout)
    pattern = str(_opt(step, "confirm", r"确定|确认|是|OK|Ok|Yes|Open|Save|打开|保存"))
    button_name = _opt(step, "button")
    btn = find_descendant(
        dlg,
        control_type="Button",
        title=str(button_name) if button_name else None,
        title_re=None if button_name else pattern,
        timeout=el.timeout,
        interval=el.interval,
    )
    if btn is None:
        raise ActionError(
            f"未能在弹窗中找到按钮 {button_name or pattern!r}",
            detail=(
                f"弹窗标题={dlg.window_text()!r}；"
                "请先用 --inspect 查看弹窗控件树，或用 target 直接定位到按钮"
            ),
        )
    btn.click_input()
    return ActionResult(message=f"已点击弹窗按钮 {button_name or pattern!r}")


@action("set_file_path", needs_target=True, category="dialog",
        description="在打开/保存对话框中填入路径并确认")
def _set_file_path(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    """处理 Windows 原生文件对话框（class_name 为 ``#32770``）。

    Qt6 在 Windows 上默认使用原生文件对话框，其结构随系统语言变化，
    因此文件名输入框按「对话框内第一个 Edit」定位，确认按钮按文本正则匹配。
    """
    el = _require(step, element, "target")
    path = str(step.value if step.value is not None else _param(step, "path"))
    dlg = el.wait("exists", step.timeout)

    edit = find_descendant(dlg, control_type="Edit", timeout=3.0, interval=0.2)
    if edit is None:
        edit = find_descendant(dlg, class_name="Edit", timeout=2.0, interval=0.2)
    if edit is None:
        raise ActionError(
            "文件对话框中找不到文件名输入框",
            detail="请用 --inspect 导出对话框控件树，并用 target 精确指定 Edit 控件",
        )
    edit.set_focus()
    edit.type_keys("^a{DELETE}", pause=0.02)
    # escape_keys：路径里的括号、加号等会被 pywinauto 当成组合键语法
    edit.type_keys(escape_keys(path), pause=0.02, with_spaces=True, set_foreground=True)

    confirm = str(_opt(step, "confirm", r"打开|保存|确定|Open|Save|OK"))
    btn = find_descendant(dlg, control_type="Button", title_re=confirm, timeout=3.0, interval=0.2)
    if btn is not None:
        btn.click_input()
    else:
        # 找不到按钮就直接回车，原生对话框的默认按钮即为确认
        _LOG.debug("未匹配到确认按钮 %r，改用回车提交", confirm)
        edit.type_keys("{ENTER}", pause=0.02)
    return ActionResult(message=f"已填入路径 {path}")


# ======================================================================
# 流程控制
# ======================================================================
@action("include", category="flow", required_params=("flow",), description="执行子流程，共享当前变量作用域")
def _include(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    # flow 同样是 Step 的具名字段，不在 params 里，不能用 _param 取
    name = str(step.flow or _opt(step, "flow", ""))
    if not name:
        raise ActionError(
            "include 动作缺少必填参数 flow",
            detail='写法：{"action": "include", "flow": "common_login.json"}',
        )
    executor = getattr(ctx, "executor", None)
    if executor is None:
        raise ActionError("include 动作不可用", detail="运行器未注入子流程执行器")
    # 递归深度由 executor 内部累加与拦截，不依赖步骤对象携带状态
    count = executor.include(name, step.vars)
    return ActionResult(message=f"已执行子流程 {name}（{count} 步）")


@action("repeat", category="flow", description="重复执行子步骤；count 定次数，over 遍历数组")
def _repeat(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    if not step.steps:
        raise ActionError("repeat 动作必须提供 steps")
    executor = getattr(ctx, "executor", None)
    if executor is None:
        raise ActionError("repeat 动作不可用", detail="运行器未注入子步骤执行器")

    if step.over is not None:
        items = step.over
        if isinstance(items, str):
            # 允许 "a,b,c" 这种紧凑写法
            items = [s.strip() for s in items.split(",") if s.strip()]
        if not isinstance(items, list):
            raise ActionError("repeat.over 必须是数组或逗号分隔字符串", detail=f"实际 {type(items).__name__}")
        total = 0
        for i, item in enumerate(items):
            total += executor.run_steps(
                step.steps,
                extra_vars={step.item_var: item, step.index_var: i},
                label=f"repeat[{i}]",
            )
        return ActionResult(message=f"遍历 {len(items)} 项，共执行 {total} 步")

    count = step.count
    if count is None:
        raise ActionError("repeat 需要 count 或 over 之一")
    total = 0
    for i in range(int(count)):
        total += executor.run_steps(step.steps, extra_vars={step.index_var: i}, label=f"repeat[{i}]")
    return ActionResult(message=f"重复 {count} 次，共执行 {total} 步")


@action("if", category="flow", required_params=("cond",), description="条件分支：cond 成立走 then，否则走 else")
def _if(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    executor = getattr(ctx, "executor", None)
    if executor is None:
        raise ActionError("if 动作不可用", detail="运行器未注入子步骤执行器")
    ok = eval_condition(ctx, step.cond, step)
    branch = step.then_steps if ok else step.else_steps
    if not branch:
        return ActionResult(message=f"条件{'成立' if ok else '不成立'}，无对应分支", skipped=True)
    n = executor.run_steps(branch, label=f"if.{'then' if ok else 'else'}")
    return ActionResult(message=f"条件{'成立' if ok else '不成立'}，执行 {n} 步")


@action("set_var", category="flow", required_params=("name",), description="写入当前用例作用域的变量")
def _set_var(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    name = str(_param(step, "name"))
    value = step.value if step.value is not None else step.params.get("value")
    # 控件取值：给了 target 但没写 value，就把控件文本写进变量，
    # 便于把界面上的动态数据（版本号、设备名）传给后续断言。
    # 注意判定顺序 —— 先取控件值再报错，否则这条路径永远走不到
    if value is None and element is not None:
        value = element.text()
    if value is None:
        raise ActionError(
            "set_var 需要 value",
            detail='写法：{"action": "set_var", "name": "x", "value": "1"}；'
                   '或提供 target 从控件取文本',
        )
    ctx.case_resolver.set(name, value)
    return ActionResult(message=f"{name} = {value!r}")


@action("log", category="flow", description="向日志输出一条信息，用于流程内标注")
def _log_action(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    message = str(step.value if step.value is not None else _opt(step, "message", ""))
    level = str(_opt(step, "level", "info")).lower()
    getattr(_LOG, level, _LOG.info)(message)
    return ActionResult(message=message)


# ----------------------------------------------------------------------
# 条件求值
# ----------------------------------------------------------------------
def eval_condition(ctx: RunContext, cond: Any, step: Step | None = None) -> bool:
    """求值 ``if`` 动作的条件。

    支持::

        {"var": "mode", "equals": "admin"}     # 另有 not_equals/contains/gt/lt/is_true/is_false
        {"exists": {<target>}}                  # 控件存在
        {"not_exists": {<target>}}
        {"all": [c1, c2]} / {"any": [...]} / {"not": c}
    """
    if cond is None:
        raise ActionError("条件为空")
    if isinstance(cond, bool):
        return cond
    if not isinstance(cond, dict):
        raise ActionError("条件必须是对象或布尔值", detail=f"实际 {cond!r}")

    if "all" in cond:
        return all(eval_condition(ctx, c, step) for c in _as_list(cond["all"]))
    if "any" in cond:
        return any(eval_condition(ctx, c, step) for c in _as_list(cond["any"]))
    if "not" in cond:
        return not eval_condition(ctx, cond["not"], step)

    if "exists" in cond or "not_exists" in cond:
        want_exists = "exists" in cond
        raw = cond.get("exists") if want_exists else cond.get("not_exists")
        spec = LocatorSpec.from_dict(raw, where="cond.exists")
        # 判定"存在"给一个短等待（界面可能正在弹出）；判定"不存在"则立即采样，
        # 否则每个 not_exists 分支都要白等一个完整超时。
        # timeout 是 Step 的具名字段而不在 params 里，两处都要看
        probe = 3.0
        if step is not None and step.timeout is not None:
            probe = float(step.timeout)
        probe = float(_opt(step, "timeout", probe)) if want_exists else 0.0
        found = Element(ctx, spec, timeout=probe, retries=0).exists(probe)
        return found if want_exists else not found

    if "var" in cond:
        resolver = getattr(ctx, "case_resolver", ctx.resolver)
        actual = resolver.get(str(cond["var"]))
        if "equals" in cond:
            return _loose_eq(actual, cond["equals"])
        if "not_equals" in cond:
            return not _loose_eq(actual, cond["not_equals"])
        if "contains" in cond:
            return str(cond["contains"]) in str(actual or "")
        if "gt" in cond:
            return float(actual) > float(cond["gt"])
        if "lt" in cond:
            return float(actual) < float(cond["lt"])
        if cond.get("is_true"):
            return _truthy(actual)
        if cond.get("is_false"):
            return not _truthy(actual)
        return _truthy(actual)

    raise ActionError(
        "无法识别的条件写法",
        detail=f"{cond}；支持 var/equals/not_equals/contains/gt/lt/is_true/is_false/exists/not_exists/all/any/not",
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _loose_eq(actual: Any, expected: Any) -> bool:
    """宽松相等：JSON 里写 ``"3"`` 也应能匹配控件取回的 ``3``。"""
    if actual == expected:
        return True
    if actual is None or expected is None:
        return False
    return str(actual) == str(expected)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none", "null")
    return bool(value)
