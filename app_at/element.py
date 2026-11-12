"""控件封装：统一等待、重试、操作与取值。

三条贯穿全模块的设计约束：

1. **绝不缓存 wrapper**。Qt 界面刷新后旧的 COM 引用会失效，表现为随机报错。
   因此每次操作都重新走一遍「构建 WindowSpecification → wait → wrapper_object」，
   重试时同样重新定位。
2. **断言失败不重试**。重试只针对环境类抖动（控件未就绪、焦点被抢、Pattern 不支持），
   ``AssertFailed`` 是测试结论，重试没有意义且会掩盖问题。
3. **文本输入优先走 ValuePattern，而非键盘模拟**。开发机常开着中文输入法，
   ``type_keys`` 会经过 IME 产生乱码；ValuePattern 直接赋值，不受输入法影响。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from . import locator
from .config import Settings
from .context import RunContext
from .errors import (
    ActionError,
    AssertFailed,
    ConfigError,
    ElementNotFoundError,
    ElementNotReadyError,
    FlowAborted,
    FlowSkipped,
)
from .log import get_logger

__all__ = ["Element", "escape_keys", "find_descendant"]

_LOG = get_logger("element")

# 这些异常直接上抛，不参与重试
_NON_RETRYABLE = (
    AssertFailed,
    FlowAborted,
    FlowSkipped,
    ConfigError,
    ValueError,
    TypeError,
    KeyError,
    KeyboardInterrupt,
    SystemExit,
)

# pywinauto type_keys 的特殊字符，输入字面文本时必须转义
_KEY_SPECIAL = set("^%~+(){}[]")

# ToggleState: 0=Off 1=On 2=Indeterminate
_TOGGLE_OFF, _TOGGLE_ON = 0, 1

# select_item 内部对 wrapper.select() 的短重试次数。
# Qt 的下拉项在展开后才异步填进 UIA 树，首次 select 常常扑空；
# 不能依赖外层 run.retries（它可能被配成 0）来兜这件事。
_SELECT_ATTEMPTS = 3


def escape_keys(text: str) -> str:
    """把 ``type_keys`` 的特殊字符转义为字面量。

    ``^ % ~ + ( ) { } [ ]`` 在 pywinauto 按键语法里有含义，密码、文件名中
    出现这些字符时若不转义会被当成组合键，导致输入内容错乱。
    """
    return "".join(f"{{{c}}}" if c in _KEY_SPECIAL else c for c in str(text))


def _normalize_text(text: Any) -> str:
    """统一行尾并去掉首尾空白，用于控件文本的比较。

    控件读回的文本常带行尾符（实测 Windows 字符映射表的 Edit 写入 ``A``
    读回 ``A\\r``），多行控件更是普遍带 ``\\r\\n``。不处理的话，
    ``assert_text`` 的 eq 比较与 ``set_var`` 取到的变量会平白失败 ——
    而假失败比漏测更快地摧毁团队对套件的信任。因此只统一行尾并去掉
    首尾空白，**内部空白仍然严格比较**（否则 "A B" 能匹上 "AB"，就真成漏测了）。

    确实需要原始值（包括行尾符）时，用 ``assert_property`` 直接读 UIA 属性；
    需要宽松比较用 ``assert_text_contains`` 或 ``assert_text_matches``。
    """
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


class Element:
    """一个待操作/待断言的控件。"""

    def __init__(
        self,
        ctx: RunContext,
        spec: locator.LocatorSpec,
        *,
        timeout: float | None = None,
        interval: float | None = None,
        retries: int | None = None,
    ) -> None:
        self.ctx = ctx
        self.spec = spec
        self.settings: Settings = ctx.settings
        self.timeout = ctx.scaled(timeout)
        self.interval = float(interval) if interval is not None else self.settings.run.poll_interval
        self.retries = self.settings.run.retries if retries is None else max(0, int(retries))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Element {self.spec.describe()}>"

    # ==================================================================
    # 定位
    # ==================================================================
    def _spec_obj(self) -> Any:
        """重新构建 pywinauto ``WindowSpecification``（惰性，不触发查找）。"""
        return locator.build_window_spec(self.ctx, self.spec)

    def wait(self, states: str = "exists", timeout: float | None = None) -> Any:
        """等待控件达到指定状态并返回 wrapper。

        :param states: pywinauto 状态串，可组合：``exists`` ``visible``
                       ``enabled`` ``ready``(=visible+enabled) ``active``
        :raises ElementNotFoundError: 超时仍未满足
        """
        limit = self.timeout if timeout is None else self.ctx.scaled(timeout)
        spec_obj = self._spec_obj()
        try:
            spec_obj.wait(states, timeout=limit, retry_interval=self.interval)
            return spec_obj.wrapper_object()
        except _pwa_errors() as exc:
            raise ElementNotFoundError(
                f"在 {limit:.1f}s 内未找到满足 [{states}] 的控件",
                detail=f"{self.spec.describe()}；pywinauto: {exc}",
            ) from exc

    def find(self, timeout: float | None = None) -> Any:
        """只要求控件存在，不校验可见/可用。"""
        return self.wait("exists", timeout)

    def exists(self, timeout: float = 0.0) -> bool:
        """控件是否存在，不抛异常。用于 ``assert_not_exists`` 与 ``optional`` 步骤。"""
        try:
            self.wait("exists", timeout)
            return True
        except ElementNotFoundError:
            return False

    def wrapper(self) -> Any:
        """立即取 wrapper，取不到直接抛错（不做额外等待）。"""
        return self.wait("exists", 0.0)

    def all_wrappers(self) -> list[Any]:
        """返回全部匹配控件，用于数量断言（``assert_count``）。

        UIA 的 ``descendants()`` 底层走 ``uia_defines.build_condition``，**只支持**
        ``process`` / ``class_name`` / ``title`` / ``control_type`` / ``content_only``
        五个过滤条件，``auto_id``、``title_re`` 传进去会直接抛 TypeError。
        因此这些条件必须在 Python 侧二次过滤。
        """
        try:
            parent = self._parent_spec_obj()
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("获取父容器失败: %s", exc)
            return []

        spec = self.spec
        kwargs: dict[str, Any] = {}
        if spec.control_type:
            kwargs["control_type"] = spec.control_type
        if spec.class_name:
            kwargs["class_name"] = spec.class_name
        if spec.name:
            kwargs["title"] = spec.name
        if spec.depth is not None:
            kwargs["depth"] = spec.depth

        try:
            found = list(parent.descendants(**kwargs))
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("枚举后代控件失败 %s: %s", kwargs, exc)
            return []
        return [w for w in found if self._match_post_filter(w)]

    def _match_post_filter(self, w: Any) -> bool:
        """对 UIA 原生条件不支持的字段做 Python 侧过滤。"""
        spec = self.spec
        if not (spec.auto_id or spec.name_re or spec.class_name_re):
            return True
        info = getattr(w, "element_info", None)
        if spec.auto_id:
            actual = str(getattr(info, "automation_id", "") or "")
            if actual != spec.auto_id:
                return False
        if spec.name_re:
            name = str(getattr(info, "name", "") or w.window_text() or "")
            if not re.search(spec.name_re, name):
                return False
        if spec.class_name_re:
            if not re.search(spec.class_name_re, str(getattr(info, "class_name", "") or "")):
                return False
        return True

    def count(self) -> int:
        return len(self.all_wrappers())

    def _parent_spec_obj(self) -> Any:
        """返回 target 的父容器 spec（窗口 + parents 链），用于枚举同级控件。"""
        session = self.ctx.app
        if session is None or session.app is None:
            raise ElementNotFoundError("应用尚未启动")
        ref = self.ctx.window_ref(self.spec.window)
        node = session.window(**ref.criteria())
        for parent in self.spec.parents:
            node = node.child_window(**parent.criteria())
        return node.wrapper_object()

    # ==================================================================
    # 重试骨架
    # ==================================================================
    def _with_retry(self, what: str, fn: Callable[[Any], Any], states: str = "ready") -> Any:
        """执行 ``fn(wrapper)``，失败时按 ``retries`` 重试。

        每轮都重新定位 + 重新校验前台焦点。``AssertFailed`` 等结论性异常不重试。
        """
        attempts = self.retries + 1
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                wrapper = self.wait(states)
                self._guard_foreground(wrapper)
                return fn(wrapper)
            except _NON_RETRYABLE:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一收敛为 ActionError
                last = exc
                if attempt < attempts:
                    _LOG.warning(
                        "%s 第 %d/%d 次尝试失败（%s: %s），%.1fs 后重试",
                        what, attempt, attempts, type(exc).__name__, exc, self.interval,
                    )
                    time.sleep(self.interval)
        detail = f"{type(last).__name__}: {last}" if last else "未知原因"
        if isinstance(last, ElementNotFoundError):
            # 定位失败保留原类型，便于 runner 触发控件树留证
            raise last
        raise ActionError(f"{what} 重试 {attempts} 次后仍失败", detail=f"{detail}；{self.spec.describe()}")

    def _guard_foreground(self, wrapper: Any) -> None:
        """操作前确保被测应用在前台。

        对子控件应把**顶层窗口**拉回前台，直接对子控件 ``set_focus`` 在 Qt 下
        可能因为父窗口最小化而无效。
        """
        session = self.ctx.app
        if session is None:
            return
        top = None
        try:
            top = wrapper.top_level_parent()
        except Exception:  # noqa: BLE001
            top = None
        session.require_foreground(top or wrapper, timeout=3.0)

    # ==================================================================
    # 属性读取（断言用）
    # ==================================================================
    def text(self) -> str:
        """控件文本（已规范化：统一行尾、去掉首尾空白）。

        依次尝试：编辑框取值 → ``window_text()`` → ``texts()`` 首个非空项。
        Qt 的 QLabel/QLineEdit 在 UIA 下分别暴露为 Name 与 Value，必须都覆盖。

        规范化放在这个唯一出口（而不是每个断言里各做一遍），因为它同时影响
        ``assert_text*`` 与 ``set_var`` 从控件取值两条路径 —— 后者拿到带 ``\\r``
        的变量，会让后续所有引用它的比较都莫名失败。
        """

        def _read(w: Any) -> str:
            for getter in ("get_value", "selected_text", "window_text"):
                fn = getattr(w, getter, None)
                if callable(fn):
                    try:
                        val = fn()
                        if val is not None and str(val) != "":
                            return str(val)
                    except Exception:  # noqa: BLE001
                        continue
            try:
                for t in w.texts():
                    if t:
                        return str(t)
            except Exception:  # noqa: BLE001
                pass
            return ""

        return _normalize_text(self._with_retry("读取控件文本", _read, states="exists"))

    def texts(self) -> list[str]:
        return self._with_retry("读取控件文本列表", lambda w: [str(t) for t in w.texts()], "exists")

    def children_texts(self) -> list[str]:
        """子项文本列表，用于下拉框/列表内容断言。"""
        return self._with_retry(
            "读取子项文本",
            lambda w: [str(t) for t in (w.children_texts() if hasattr(w, "children_texts") else w.texts())],
            "exists",
        )

    def control_type(self) -> str:
        return self._with_retry(
            "读取控件类型",
            lambda w: str(getattr(w.element_info, "control_type", "") or w.friendly_class_name()),
            "exists",
        )

    def automation_id(self) -> str:
        return self._with_retry(
            "读取 AutomationId",
            lambda w: str(getattr(w, "automation_id", lambda: "")() or ""),
            "exists",
        )

    def class_name(self) -> str:
        return self._with_retry("读取 class_name", lambda w: str(w.class_name()), "exists")

    def rect(self) -> Any:
        return self._with_retry("读取控件矩形", lambda w: w.rectangle(), "exists")

    def is_enabled(self) -> bool:
        return bool(self._with_retry("检查是否可用", lambda w: w.is_enabled(), "exists"))

    def is_visible(self) -> bool:
        return bool(self._with_retry("检查是否可见", lambda w: w.is_visible(), "exists"))

    def is_expanded(self) -> bool | None:
        def _f(w: Any) -> bool | None:
            if hasattr(w, "is_expanded"):
                try:
                    return bool(w.is_expanded())
                except Exception:  # noqa: BLE001
                    return None
            return None

        return self._with_retry("检查展开状态", _f, "exists")

    def toggle_state(self) -> int | None:
        """复选/单选/开关状态：0=未选 1=已选 2=不确定；控件不支持时返回 None。"""

        def _f(w: Any) -> int | None:
            if hasattr(w, "get_toggle_state"):
                try:
                    return int(w.get_toggle_state())
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(w, "is_checked"):
                try:
                    return _TOGGLE_ON if w.is_checked() else _TOGGLE_OFF
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(w, "is_selected"):
                try:
                    return _TOGGLE_ON if w.is_selected() else _TOGGLE_OFF
                except Exception:  # noqa: BLE001
                    pass
            return None

        return self._with_retry("读取勾选状态", _f, "exists")

    def is_checked(self) -> bool:
        state = self.toggle_state()
        if state is None:
            raise ActionError(
                "控件不支持勾选状态读取",
                detail=f"{self.spec.describe()}；请确认控件是 CheckBox/RadioButton/ListItem",
            )
        return state == _TOGGLE_ON

    def item_count(self) -> int:
        """列表/下拉/表格的条目数。"""

        def _f(w: Any) -> int:
            if hasattr(w, "item_count"):
                try:
                    return int(w.item_count())
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(w, "tab_count"):
                try:
                    return int(w.tab_count())
                except Exception:  # noqa: BLE001
                    pass
            # 兜底：数直接子项里的列表类控件
            try:
                items = w.descendants(control_type="ListItem") or []
                items += w.descendants(control_type="DataItem") or []
                items += w.descendants(control_type="TreeItem") or []
                return len(items)
            except Exception:  # noqa: BLE001
                return len(w.children())

        return self._with_retry("读取条目数", _f, "exists")

    def get_property(self, name: str) -> Any:
        """读取任意 UIA / pywinauto 属性，供 ``assert_property`` 使用。"""

        def _f(w: Any) -> Any:
            props = {}
            for getter in ("get_properties", "legacy_properties", "writable_props"):
                fn = getattr(w, getter, None)
                if callable(fn):
                    try:
                        val = fn()
                        if isinstance(val, dict):
                            props.update(val)
                    except Exception:  # noqa: BLE001
                        continue
            if name in props:
                return props[name]
            info = getattr(w, "element_info", None)
            if info is not None and hasattr(info, name):
                return getattr(info, name)
            if hasattr(w, name):
                attr = getattr(w, name)
                return attr() if callable(attr) else attr
            available = ", ".join(sorted(props)[:40])
            raise ActionError(f"控件无属性 {name!r}", detail=f"可用属性(前40): {available}")

        return self._with_retry(f"读取属性 {name}", _f, "exists")

    # ==================================================================
    # 鼠标操作
    # ==================================================================
    def click(self, *, by: str | None = None, button: str = "left", double: bool = False) -> None:
        """点击控件中心。

        ``by='mouse'``（默认）走 ``click_input()`` 发送真实鼠标事件；
        ``by='invoke'`` 走 UIA Invoke Pattern。**Qt 的 Invoke Pattern 支持不完整**，
        很多自定义控件调用后无任何反应，因此默认必须是 mouse。
        """
        mode = (by or self.settings.run.click_by or "mouse").lower()

        def _f(w: Any) -> None:
            if mode == "invoke":
                if not hasattr(w, "invoke"):
                    raise ActionError(
                        "控件不支持 UIA Invoke Pattern",
                        detail=f"{self.spec.describe()}；请把 by 改为 'mouse'",
                    )
                w.invoke()
                return
            if double:
                w.double_click_input(button=button)
            elif button == "right":
                w.right_click_input()
            else:
                w.click_input(button=button)

        self._with_retry(f"点击({mode}{', 双击' if double else ''})", _f)

    def double_click(self, *, by: str | None = None) -> None:
        self.click(by=by, double=True)

    def right_click(self) -> None:
        self.click(button="right")

    def click_offset(self, dx: float, dy: float, *, button: str = "left", double: bool = False) -> None:
        """点击控件内相对坐标 (dx, dy)，单位像素，原点为控件左上角。

        自绘控件（图表、画布、自定义表格）在 UIA 树里往往只是一个黑盒 Pane，
        内部元素无法定位，此时只能按相对坐标点击。

        ``click_input(absolute=False)`` 的 coords 正是**相对控件矩形**的偏移，
        因此无需自己换算屏幕坐标，天然不受窗口移动影响。
        """

        def _f(w: Any) -> None:
            rect = w.rectangle()
            if rect.width() <= 0 or rect.height() <= 0:
                raise ElementNotReadyError(
                    "控件矩形为空，无法计算相对坐标", detail=f"{self.spec.describe()}"
                )
            if not (0 <= dx <= rect.width() and 0 <= dy <= rect.height()):
                _LOG.warning(
                    "click_offset(%s, %s) 超出控件范围 %sx%s，点击可能落到控件外",
                    dx, dy, rect.width(), rect.height(),
                )
            w.click_input(button=button, coords=(int(dx), int(dy)), double=double, absolute=False)

        self._with_retry(f"相对坐标点击({dx},{dy})", _f)

    def drag_to(self, target: Element | tuple[int, int], *, button: str = "left") -> None:
        """拖拽到另一个控件或绝对屏幕坐标。"""

        def _f(w: Any) -> None:
            if isinstance(target, Element):
                dst: Any = target.find()
            else:
                dst = tuple(target)
            w.drag_mouse_input(dst=dst, button=button, absolute=not isinstance(dst, tuple))

        self._with_retry("拖拽", _f)

    def scroll(self, direction: str = "down", amount: str = "line", count: int = 3) -> None:
        """滚动。``direction``: up/down/left/right；``amount``: line/page/document。"""

        def _f(w: Any) -> None:
            if hasattr(w, "scroll"):
                try:
                    w.scroll(direction, amount, count)
                    return
                except Exception as exc:  # noqa: BLE001
                    _LOG.debug("ScrollPattern 失败(%s)，回退到鼠标滚轮", exc)
            dist = count if direction == "up" else -count
            w.wheel_mouse_input(wheel_dist=dist)

        self._with_retry(f"滚动({direction}/{amount}x{count})", _f)

    # ==================================================================
    # 键盘 / 文本
    # ==================================================================
    def set_text(self, value: str, *, by: str | None = None, verify: bool = False) -> None:
        """写入文本，**先清空原内容**。

        默认优先 ValuePattern：不受输入法影响、不依赖窗口焦点、速度快。
        失败或校验不一致时回退到「焦点 + 全选 + 键盘输入」。
        """
        text = "" if value is None else str(value)
        mode = (by or "auto").lower()

        def _f(w: Any) -> None:
            if mode in ("auto", "value") and self._try_set_by_value(w, text):
                if not verify or self._readback_matches(w, text):
                    return
                _LOG.debug("ValuePattern 写入后读回不一致，回退到键盘输入")
            if mode == "value":
                raise ActionError(
                    "ValuePattern 写入失败", detail=f"{self.spec.describe()}；可改用 by='keyboard'"
                )
            self._set_by_keyboard(w, text)
            if verify and not self._readback_matches(w, text):
                actual = self._safe_readback(w)
                raise AssertFailed(
                    "文本写入后校验不一致", expected=text, actual=actual,
                    detail=self.spec.describe(),
                )

        self._with_retry(f"写入文本({text!r})", _f)

    @staticmethod
    def _try_set_by_value(w: Any, text: str) -> bool:
        for name in ("set_edit_text", "set_window_text"):
            fn = getattr(w, name, None)
            if callable(fn):
                try:
                    fn(text)
                    return True
                except Exception as exc:  # noqa: BLE001
                    _LOG.debug("%s 失败: %s", name, exc)
        # 直接操作 UIA ValuePattern
        try:
            iface = w.iface_value
            if iface is not None:
                iface.SetValue(text)
                return True
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("ValuePattern.SetValue 失败: %s", exc)
        return False

    @staticmethod
    def _safe_readback(w: Any) -> str:
        for getter in ("get_value", "window_text"):
            fn = getattr(w, getter, None)
            if callable(fn):
                try:
                    val = fn()
                    if val is not None:
                        return str(val)
                except Exception:  # noqa: BLE001
                    continue
        return ""

    def _readback_matches(self, w: Any, text: str) -> bool:
        actual = _normalize_text(self._safe_readback(w))
        # 密码框读回为空或掩码，此时无法校验，视为通过（由后续业务断言兜住）
        if actual == "":
            return True
        return actual == _normalize_text(text)

    @staticmethod
    def _set_by_keyboard(w: Any, text: str) -> None:
        w.set_focus()
        w.type_keys("^a{DELETE}", pause=0.02, set_foreground=True)
        if text:
            # escape_keys: 密码/路径里的 ^%~+(){}[] 不转义会被当成组合键
            w.type_keys(
                escape_keys(text),
                pause=0.02,
                with_spaces=True,
                with_tabs=True,
                with_newlines=True,
                set_foreground=True,
            )

    def type_keys(self, keys: str, *, literal: bool = False, pause: float = 0.03) -> None:
        """发送按键序列。

        ``literal=True`` 时把内容当纯文本转义后输入；否则 ``keys`` 按 pywinauto
        按键语法解释（``^{a}`` 全选、``{ENTER}`` 回车等）。
        """
        payload = escape_keys(keys) if literal else keys

        def _f(w: Any) -> None:
            w.type_keys(payload, pause=pause, with_spaces=True, with_tabs=True, with_newlines=True)

        self._with_retry(f"发送按键({payload!r})", _f)

    def clear_text(self) -> None:
        """清空文本框。"""
        self.set_text("")

    def hotkey(self, keys: str) -> None:
        """在当前焦点控件上发送组合键，如 ``^s`` / ``%{F4}``。"""
        self.type_keys(keys, literal=False)

    def press_key(self, key: str) -> None:
        """发送单个按键，如 ``ENTER`` / ``TAB`` / ``ESC`` / ``F5``。"""
        self.type_keys(f"{{{key.upper()}}}", literal=False)

    # ==================================================================
    # 控件状态操作
    # ==================================================================
    def select_item(self, item: str | int) -> None:
        """选择下拉项/列表项/标签页，``item`` 可为文本或 0 基索引。"""

        def _f(w: Any) -> None:
            if hasattr(w, "expand") and hasattr(w, "select"):
                # Qt 的下拉项是展开后才**异步**填进 UIA 树的：第一次 select
                # 往往报“找不到目标项”，而 expand/collapse 走过一轮之后就正常了。
                # 把这件事推给外层 retries 兜底很脆（retries=0 就直接失败），
                # 因此在这里就地短重试几次。
                last_exc: Exception | None = None
                for _ in range(_SELECT_ATTEMPTS):
                    try:
                        w.select(item)
                        return
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        time.sleep(max(0.2, self.interval))
                _LOG.debug("select(%r) 失败: %s，回退到点击子项", item, last_exc)
            # 回退：在子项里按文本找并点击。
            # 只能用 children()/descendants() —— ``child_window`` 是
            # WindowSpecification 的方法，而 _with_retry 传进来的一律是 wrapper，
            # 写 child_window 必然 AttributeError，把真正的失败原因（目标项找
            # 不到）掩盖成一句莫名其妙的类型错误。
            if isinstance(item, str):
                for finder in ("children", "descendants"):
                    fn = getattr(w, finder, None)
                    if not callable(fn):
                        continue
                    try:
                        matched = list(fn(title=item) or [])
                    except Exception as exc:  # noqa: BLE001
                        _LOG.debug("%s(title=%r) 失败: %s", finder, item, exc)
                        continue
                    if matched:
                        matched[0].click_input()
                        return
                raise ActionError(
                    f"未找到可选择的子项 {item!r}",
                    detail=f"{self.spec.describe()}；请核对选项文本是否与界面完全一致",
                )
            raise ActionError(
                "控件不支持选择操作", detail=f"{self.spec.describe()}；item={item!r}"
            )

        self._with_retry(f"选择({item!r})", _f)

    def set_toggle(self, want_on: bool) -> None:
        """把复选/开关设为目标状态；已符合则不做任何操作（幂等）。"""

        def _f(w: Any) -> None:
            target = _TOGGLE_ON if want_on else _TOGGLE_OFF
            for _ in range(2):
                state: int | None = None
                for getter in ("get_toggle_state",):
                    if hasattr(w, getter):
                        try:
                            state = int(getattr(w, getter)())
                        except Exception:  # noqa: BLE001
                            state = None
                if state is None and hasattr(w, "is_checked"):
                    try:
                        state = _TOGGLE_ON if w.is_checked() else _TOGGLE_OFF
                    except Exception:  # noqa: BLE001
                        state = None
                if state is None:
                    raise ActionError(
                        "控件不支持勾选状态", detail=f"{self.spec.describe()}"
                    )
                if state == target:
                    return
                if hasattr(w, "toggle"):
                    try:
                        w.toggle()
                    except Exception as exc:  # noqa: BLE001
                        _LOG.debug("TogglePattern 失败(%s)，回退到点击", exc)
                        w.click_input()
                else:
                    w.click_input()
                time.sleep(max(0.05, self.interval))
            raise ActionError(f"无法把控件设置为目标状态 {target}", detail=self.spec.describe())

        self._with_retry(f"设置勾选({want_on})", _f)

    def check(self) -> None:
        self.set_toggle(True)

    def uncheck(self) -> None:
        self.set_toggle(False)

    def toggle(self) -> None:
        """切换一次状态。"""

        def _f(w: Any) -> None:
            if hasattr(w, "toggle"):
                try:
                    w.toggle()
                    return
                except Exception as exc:  # noqa: BLE001
                    _LOG.debug("TogglePattern 失败(%s)，回退到点击", exc)
            w.click_input()

        self._with_retry("切换状态", _f)

    def set_expanded(self, want_expanded: bool) -> None:
        def _f(w: Any) -> None:
            if not hasattr(w, "expand") or not hasattr(w, "collapse"):
                raise ActionError("控件不支持展开/折叠", detail=self.spec.describe())
            try:
                current = bool(w.is_expanded())
            except Exception:  # noqa: BLE001
                current = not want_expanded
            if current == want_expanded:
                return
            w.expand() if want_expanded else w.collapse()

        self._with_retry(f"{'展开' if want_expanded else '折叠'}", _f)

    def set_value(self, value: float | str) -> None:
        """设置数值（滑块、进度输入等），走 RangeValue/Value Pattern。"""

        def _f(w: Any) -> None:
            if hasattr(w, "set_value"):
                w.set_value(value)
                return
            if self._try_set_by_value(w, str(value)):
                return
            raise ActionError("控件不支持设置数值", detail=self.spec.describe())

        self._with_retry(f"设置数值({value!r})", _f)

    def focus(self) -> None:
        self._with_retry("设置焦点", lambda w: w.set_focus())

    def menu_select(self, path: str, exact: bool = False) -> None:
        """按路径选择菜单项，如 ``文件->保存``。

        注意：Qt 自绘菜单常不暴露标准 Win32 菜单结构，此路径可能失效；
        失效时改用逐级 ``click`` 菜单项，这也是更贴近真实用户的做法。
        """

        def _f(w: Any) -> None:
            if not hasattr(w, "menu_select"):
                raise ActionError(
                    "控件不支持 menu_select",
                    detail=f"{self.spec.describe()}；Qt 自绘菜单请改用逐级 click",
                )
            w.menu_select(path, exact=exact)

        self._with_retry(f"菜单选择({path})", _f)

    def wait_idle(self) -> None:
        """等待 UIA 事件队列空闲，用于 Qt 异步刷新后的稳定点。"""

        def _f(w: Any) -> None:
            if hasattr(w, "wait_for_idle"):
                w.wait_for_idle()

        self._with_retry("等待界面空闲", _f, states="exists")

    # ------------------------------------------------------------------
    # 窗口级操作（与控件操作共用同一套重试与焦点保护）
    # ------------------------------------------------------------------
    def maximize(self) -> None:
        self._with_retry("最大化窗口", lambda w: w.maximize(), states="exists")

    def restore(self) -> None:
        self._with_retry("还原窗口", lambda w: w.restore(), states="exists")

    def minimize(self) -> None:
        self._with_retry("最小化窗口", lambda w: w.minimize(), states="exists")

    def close(self) -> None:
        self._with_retry("关闭窗口", lambda w: w.close(), states="exists")

    def activate(self) -> None:
        self._with_retry("激活窗口", lambda w: w.set_focus(), states="exists")

    def title(self) -> str:
        return self._with_retry("读取窗口标题", lambda w: str(w.window_text() or ""), "exists")

    # ==================================================================
    # 诊断
    # ==================================================================
    def describe_runtime(self) -> str:
        """尽力采集控件的运行时信息，用于失败报告（采集失败不影响主流程）。"""
        try:
            w = self.wait("exists", 0.0)
        except ElementNotFoundError:
            return f"未找到: {self.spec.describe()}"
        bits = [self.spec.describe()]
        for label, fn in (
            ("text", lambda: w.window_text()),
            ("type", lambda: getattr(w.element_info, "control_type", "")),
            ("auto_id", lambda: getattr(w.element_info, "automation_id", "")),
            ("class", lambda: w.class_name()),
            ("rect", lambda: str(w.rectangle())),
            ("enabled", lambda: w.is_enabled()),
            ("visible", lambda: w.is_visible()),
        ):
            try:
                bits.append(f"{label}={fn()!r}")
            except Exception:  # noqa: BLE001
                bits.append(f"{label}=<err>")
        return " ".join(bits)

    def dump_root(self) -> Any:
        """返回用于导出控件树的根对象（窗口优先，找不到则退回顶层窗口）。"""
        session = self.ctx.app
        if session is None:
            return None
        try:
            return self._parent_spec_obj()
        except Exception:  # noqa: BLE001
            try:
                ref = self.ctx.window_ref(self.spec.window)
                return session.window(**ref.criteria()).wrapper_object()
            except Exception:  # noqa: BLE001
                return None


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _pwa_errors() -> tuple[type[BaseException], ...]:
    """pywinauto 的"没找到/超时"异常类型，惰性导入。"""
    try:
        from pywinauto.findwindows import ElementAmbiguousError, ElementNotFoundError
        from pywinauto.timings import TimeoutError as PwaTimeoutError

        return (ElementNotFoundError, ElementAmbiguousError, PwaTimeoutError, TimeoutError)
    except ImportError:  # pragma: no cover
        return (Exception,)


def find_descendant(
    root: Any,
    *,
    control_type: str | None = None,
    title: str | None = None,
    title_re: str | None = None,
    class_name: str | None = None,
    timeout: float = 3.0,
    interval: float = 0.2,
) -> Any | None:
    """在已取到的 wrapper 下按条件查找后代，轮询直到超时，找不到返回 None。

    为何不用 ``root.child_window(...)``：**``child_window`` 只存在于
    ``WindowSpecification`` 上，``BaseWrapper`` 没有这个方法**。拿到 wrapper 之后
    只能用 ``descendants()``，而它底层走 ``uia_defines.build_condition``，
    仅支持 process / class_name / title / control_type / content_only，
    因此 ``title_re`` 必须在 Python 侧二次过滤。
    """
    kwargs: dict[str, Any] = {}
    if control_type:
        kwargs["control_type"] = control_type
    if title:
        kwargs["title"] = title
    if class_name:
        kwargs["class_name"] = class_name

    deadline = time.monotonic() + max(0.0, timeout)
    pattern = re.compile(title_re) if title_re else None
    while True:
        try:
            found = list(root.descendants(**kwargs))
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("descendants(%s) 失败: %s", kwargs, exc)
            found = []
        for w in found:
            if pattern is None:
                return w
            info = getattr(w, "element_info", None)
            name = str(getattr(info, "name", "") or "")
            if not name:
                try:
                    name = str(w.window_text() or "")
                except Exception:  # noqa: BLE001
                    name = ""
            if pattern.search(name):
                return w
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)
