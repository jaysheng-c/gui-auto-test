"""流程数据模型：Suite / Flow / Step。

模型分两个阶段：

* **解析态**（:func:`Step.parse`）：JSON 原样映射，值里可能还带 ``${var}``；
* **绑定态**（:meth:`Step.bind`）：执行前用 :class:`VariableResolver` 把所有字段
  递归解析成真实值，动作函数因此只需读普通属性，不必关心变量语法。

之所以在**执行前**才绑定，是为了支持 ``set_var`` 在运行中写入、``data_set``
逐行注入、以及 ``__now__`` 这类动态内置变量。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .config import MAIN_WINDOW_ALIAS
from .errors import FlowFormatError
from .locator import LocatorSpec
from .variables import VariableResolver

__all__ = [
    "Step",
    "Flow",
    "Suite",
    "STEP_KNOWN_KEYS",
    "ON_FAIL_ABORT",
    "ON_FAIL_CONTINUE",
    "ON_FAIL_SKIP_FLOW",
]

ON_FAIL_ABORT = "abort"
ON_FAIL_CONTINUE = "continue"
ON_FAIL_SKIP_FLOW = "skip_flow"
_VALID_ON_FAIL = (ON_FAIL_ABORT, ON_FAIL_CONTINUE, ON_FAIL_SKIP_FLOW)
_VALID_SCREENSHOT = ("always", "on_fail", "never")

# 这些 key 映射到 Step 的具名字段，其余一律进 params（动作专属参数）
STEP_KNOWN_KEYS = frozenset(
    {
        "action",
        "id",
        "desc",
        "target",
        "window",
        "value",
        "expect",
        "timeout",
        "interval",
        "retries",
        "optional",
        "on_fail",
        "screenshot",
        "dump_tree",
        "steps",
        "then",
        "else",
        "flow",
        "vars",
        "count",
        "over",
        "item_var",
        "index_var",
        "cond",
    }
)


@dataclass
class Step:
    """一个测试步骤。"""

    action: str
    id: str = ""
    desc: str = ""
    target: LocatorSpec | None = None
    window: str | None = None
    value: Any = None
    expect: Any = None
    timeout: float | None = None
    interval: float | None = None
    retries: int | None = None
    optional: bool = False
    on_fail: str = ON_FAIL_ABORT
    screenshot: str | None = None
    dump_tree: bool | None = None
    # 动作专属参数（button / double / dx / dy / keys / path / direction ...）
    params: dict[str, Any] = field(default_factory=dict)
    # 复合动作的子步骤
    steps: list[Step] = field(default_factory=list)
    then_steps: list[Step] = field(default_factory=list)
    else_steps: list[Step] = field(default_factory=list)
    # include 用
    flow: str | None = None
    vars: dict[str, Any] = field(default_factory=dict)
    # repeat 用
    count: int | None = None
    over: Any = None
    item_var: str = "item"
    index_var: str = "index"
    # if 用
    cond: Any = None
    # 溯源
    source: str = ""

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @classmethod
    def parse(cls, data: Any, *, where: str = "step", source: str = "") -> Step:
        if not isinstance(data, dict):
            raise FlowFormatError(f"{where} 必须是对象", detail=f"实际类型 {type(data).__name__}")

        action = data.get("action")
        if not action or not isinstance(action, str):
            raise FlowFormatError(f"{where} 缺少必填字段 action", detail=f"步骤内容: {data}")

        on_fail = data.get("on_fail", ON_FAIL_ABORT)
        if on_fail not in _VALID_ON_FAIL:
            raise FlowFormatError(
                f"{where}.on_fail 非法: {on_fail!r}", detail=f"可选值 {list(_VALID_ON_FAIL)}"
            )
        screenshot = data.get("screenshot")
        if screenshot is not None and screenshot not in _VALID_SCREENSHOT:
            raise FlowFormatError(
                f"{where}.screenshot 非法: {screenshot!r}", detail=f"可选值 {list(_VALID_SCREENSHOT)}"
            )

        target_raw = data.get("target")
        target = (
            LocatorSpec.from_dict(target_raw, where=f"{where}.target")
            if target_raw is not None
            else None
        )
        # window 可以写在 step 顶层，作为 target.window 的简写
        window = data.get("window")
        if window and target is not None and target.window == MAIN_WINDOW_ALIAS:
            target.window = window

        steps = _parse_steps(data.get("steps"), where=f"{where}.steps", source=source)
        then_steps = _parse_steps(data.get("then"), where=f"{where}.then", source=source)
        else_steps = _parse_steps(data.get("else"), where=f"{where}.else", source=source)

        count = data.get("count")
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 1):
            raise FlowFormatError(f"{where}.count 必须是正整数", detail=f"实际值 {count!r}")

        params = {k: v for k, v in data.items() if k not in STEP_KNOWN_KEYS}

        step_id = str(data.get("id") or "")
        return cls(
            action=action,
            id=step_id,
            desc=str(data.get("desc") or ""),
            target=target,
            window=window or (target.window if target else None),
            value=data.get("value"),
            expect=data.get("expect"),
            timeout=data.get("timeout"),
            interval=data.get("interval"),
            retries=data.get("retries"),
            optional=bool(data.get("optional", False)),
            on_fail=on_fail,
            screenshot=screenshot,
            dump_tree=data.get("dump_tree"),
            params=params,
            steps=steps,
            then_steps=then_steps,
            else_steps=else_steps,
            flow=data.get("flow"),
            vars=dict(data.get("vars") or {}),
            count=count,
            over=data.get("over"),
            item_var=str(data.get("item_var") or "item"),
            index_var=str(data.get("index_var") or "index"),
            cond=data.get("cond"),
            source=source or Path(where).name,
        )

    # ------------------------------------------------------------------
    # 绑定
    # ------------------------------------------------------------------
    def bind(self, resolver: VariableResolver) -> Step:
        """返回所有占位符已解析的副本，不修改原对象。

        **子步骤（``steps`` / ``then_steps`` / ``else_steps``）故意不在这里绑定**：
        它们由 ``StepExecutor.run_steps`` 在推入块级作用域（repeat 的
        ``item`` / ``index``、include 的子流程 vars）之后逐个绑定。在这里提前
        用外层 resolver 递归解析，会让 ``${index}`` 这类变量当场报
        “变量未定义”，即使不报错也只能拿到错的外层值。
        """
        bound_target = None
        if self.target is not None:
            raw = _target_to_dict(self.target)
            resolved = resolver.resolve(raw)
            bound_target = LocatorSpec.from_dict(resolved, where="target")

        return replace(
            self,
            desc=resolver.resolve(self.desc) if self.desc else self.desc,
            target=bound_target,
            window=resolver.resolve(self.window) if self.window else self.window,
            value=resolver.resolve(self.value),
            expect=resolver.resolve(self.expect),
            params=resolver.resolve(self.params),
            vars=resolver.resolve(self.vars),
            over=resolver.resolve(self.over),
            count=resolver.resolve(self.count),
            # cond 里同样允许写 ${var}（如 {"var": "mode", "equals": "${expect_mode}"}）
            cond=resolver.resolve(self.cond),
            flow=resolver.resolve(self.flow) if self.flow else self.flow,
        )

    # ------------------------------------------------------------------
    def param(self, name: str, default: Any = None) -> Any:
        """读取动作专属参数。"""
        return self.params.get(name, default)

    def label(self) -> str:
        """日志与报告里展示的步骤标识。"""
        sid = self.id or "?"
        desc = self.desc or self.action
        return f"[{sid}] {self.action} - {desc}"

    def __str__(self) -> str:  # pragma: no cover
        return self.label()


def _target_to_dict(spec: LocatorSpec) -> dict[str, Any]:
    """LocatorSpec → 可被 resolver 递归处理的 dict（bind 用）。"""
    out: dict[str, Any] = {"window": spec.window}
    for src, dst in (
        ("auto_id", "auto_id"),
        ("name", "name"),
        ("name_re", "name_re"),
        ("type", "control_type"),
        ("class", "class_name"),
        ("class_re", "class_name_re"),
        ("index", "index"),
        ("best_match", "best_match"),
        ("control_id", "control_id"),
        ("depth", "depth"),
        ("image", "image"),
        ("desc", "desc"),
    ):
        val = getattr(spec, dst)
        if val is not None:
            out[src] = val
    if spec.parents:
        out["parents"] = [_target_to_dict(p) for p in spec.parents]
    return out


def _parse_steps(data: Any, *, where: str, source: str = "") -> list[Step]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise FlowFormatError(f"{where} 必须是数组")
    return [Step.parse(s, where=f"{where}[{i}]", source=source) for i, s in enumerate(data)]


@dataclass
class Flow:
    """一个测试用例（对应一个 JSON 文件）。"""

    id: str
    name: str = ""
    tags: list[str] = field(default_factory=list)
    priority: str = "P2"
    timeout: float | None = None
    vars: dict[str, Any] = field(default_factory=dict)
    data_set: list[dict[str, Any]] = field(default_factory=list)
    setup: list[Step] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    teardown: list[Step] = field(default_factory=list)
    source: Path | None = None
    enabled: bool = True
    skip_reason: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.id} {self.name}".strip()

    @property
    def file_name(self) -> str:
        return self.source.name if self.source else f"{self.id}.json"

    def matches(self, *, ids: set[str] | None = None, tags: set[str] | None = None) -> bool:
        """按 ``--case`` / ``--tags`` 过滤。两个条件都给了则需同时满足。"""
        if ids and self.id not in ids:
            return False
        if tags and not (tags & set(self.tags)):
            return False
        return True


@dataclass
class Suite:
    """一个测试套件（对应一个 suite JSON 文件，聚合多个 Flow）。"""

    suite: str = "default"
    name: str = ""
    vars: dict[str, Any] = field(default_factory=dict)
    flows: list[str] = field(default_factory=list)
    # 对全局配置的局部覆盖，键路径与 Settings 一致，如 "app.main_window.title_re"
    app_overrides: dict[str, Any] = field(default_factory=dict)
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.suite
