"""定位器：把流程 JSON 里的 ``target`` 解析成 pywinauto 的 ``WindowSpecification``。

JSON 字段 → pywinauto 条件映射：

=========== ==================== =========================================
JSON        pywinauto            说明
=========== ==================== =========================================
``auto_id`` ``auto_id``          **首选**，对应 Qt ``objectName``
``name``    ``title``            可见文本，精确匹配
``name_re`` ``title_re``         可见文本，正则匹配
``type``    ``control_type``     UIA 控件类型，如 ``Button`` / ``Edit``
``class``   ``class_name``       Qt 下区分度低，仅作辅助
``index``   ``found_index``      **JSON 为 1 基，pywinauto 为 0 基，此处转换**
``parents`` 逐层 ``child_window`` 祖先链，自上而下，用于收窄搜索范围
``depth``   ``depth``            搜索深度上限
``image``   ——                   路由到 :mod:`vision`（本期空入口）
=========== ==================== =========================================

多个条件同时给出时为 AND 组合。``parents`` 的价值不只是消歧：Qt 主窗口控件树
往往有十几层，不加祖先链会触发全树深搜，单步耗时可达数秒甚至超时。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .config import MAIN_WINDOW_ALIAS
from .errors import ElementNotFoundError, FlowFormatError
from .log import get_logger
from . import vision

if TYPE_CHECKING:  # pragma: no cover
    from .context import RunContext

__all__ = ["LocatorSpec", "build_window_spec", "spec_from_dict"]

_LOG = get_logger("locator")

# JSON 中允许的 target 字段；未知字段直接报错，避免拼错后静默失效。
# 注意必须与 LocatorSpec.from_dict 实际映射的字段、以及 flow.schema.json 的
# target 定义保持一致：只允许不映射等于静默丢弃条件，比报错难查得多。
_ALLOWED_KEYS = frozenset(
    {
        "window",
        "auto_id",
        "name",
        "name_re",
        "type",
        "class",
        "class_re",
        "index",
        "parents",
        "depth",
        "best_match",
        "image",
        "control_id",
        "desc",
    }
)


@dataclass
class LocatorSpec:
    """一个控件定位条件。"""

    window: str = MAIN_WINDOW_ALIAS
    auto_id: str | None = None
    name: str | None = None
    name_re: str | None = None
    control_type: str | None = None
    class_name: str | None = None
    class_name_re: str | None = None
    index: int | None = None
    best_match: str | None = None
    control_id: int | None = None
    depth: int | None = None
    image: str | None = None
    desc: str | None = None
    parents: list[LocatorSpec] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: Any, *, where: str = "target") -> LocatorSpec:
        """从 JSON dict 构造，字段名拼写错误立即报错。"""
        if data is None:
            return cls()
        if isinstance(data, str):
            # 简写：直接给字符串时视为 auto_id，让简单用例更紧凑
            return cls(auto_id=data)
        if not isinstance(data, dict):
            raise FlowFormatError(f"{where} 必须是对象或字符串", detail=f"实际类型 {type(data).__name__}")

        unknown = set(data) - _ALLOWED_KEYS
        if unknown:
            raise FlowFormatError(
                f"{where} 含未知字段: {', '.join(sorted(unknown))}",
                detail=f"允许的字段: {', '.join(sorted(_ALLOWED_KEYS))}",
            )

        parents_raw = data.get("parents") or []
        if not isinstance(parents_raw, list):
            raise FlowFormatError(f"{where}.parents 必须是数组")
        parents = [
            cls.from_dict(p, where=f"{where}.parents[{i}]") for i, p in enumerate(parents_raw)
        ]

        index = data.get("index")
        if index is not None:
            if not isinstance(index, int) or isinstance(index, bool) or index < 1:
                raise FlowFormatError(
                    f"{where}.index 必须是从 1 开始的整数", detail=f"实际值 {index!r}"
                )

        return cls(
            window=data.get("window") or MAIN_WINDOW_ALIAS,
            auto_id=data.get("auto_id"),
            name=data.get("name"),
            name_re=data.get("name_re"),
            control_type=data.get("type"),
            class_name=data.get("class"),
            class_name_re=data.get("class_re"),
            index=index,
            best_match=data.get("best_match"),
            control_id=data.get("control_id"),
            depth=data.get("depth"),
            image=data.get("image"),
            desc=data.get("desc"),
            parents=parents,
        )

    # ------------------------------------------------------------------
    def criteria(self, *, default_depth: int | None = None) -> dict[str, Any]:
        """转成 ``child_window(**kwargs)`` 的关键字参数。

        ``found_index`` 做 1 基 → 0 基转换；JSON 作者按人类习惯从 1 数，
        pywinauto 内部按列表下标从 0 数。
        """
        crit: dict[str, Any] = {}
        if self.auto_id is not None:
            crit["auto_id"] = self.auto_id
        if self.name is not None:
            crit["title"] = self.name
        if self.name_re is not None:
            crit["title_re"] = self.name_re
        if self.control_type is not None:
            crit["control_type"] = self.control_type
        if self.class_name is not None:
            crit["class_name"] = self.class_name
        if self.class_name_re is not None:
            crit["class_name_re"] = self.class_name_re
        if self.best_match is not None:
            crit["best_match"] = self.best_match
        if self.control_id is not None:
            crit["control_id"] = self.control_id
        if self.index is not None:
            crit["found_index"] = self.index - 1
        depth = self.depth if self.depth is not None else default_depth
        if depth is not None:
            crit["depth"] = depth
        return crit

    def is_empty(self) -> bool:
        """除 ``window`` / ``desc`` 外没有给出任何定位条件。"""
        return not self.criteria() and not self.parents and not self.image

    def describe(self) -> str:
        parts = [f"{k}={v!r}" for k, v in self.criteria().items()]
        if self.parents:
            chain = " > ".join(p.describe() for p in self.parents)
            parts.insert(0, f"parents=({chain})")
        if self.image:
            parts.append(f"image={self.image!r}")
        head = f"window={self.window!r}"
        return head + (" " + " ".join(parts) if parts else "")


# 兼容别名
spec_from_dict = LocatorSpec.from_dict


def build_window_spec(ctx: RunContext, spec: LocatorSpec) -> Any:
    """把 :class:`LocatorSpec` 解析为 pywinauto ``WindowSpecification``。

    返回的是**惰性对象**，真正的查找发生在 ``wait()`` / ``wrapper_object()`` 时。

    :raises ElementNotFoundError: 窗口别名未注册
    :raises VisionNotImplementedError: ``target.image`` 被使用（本期未实现）
    """
    if spec.image:
        # 路由到 vision 模块；本期会抛 VisionNotImplementedError。
        # 保留这条分支是为了将来填充实现时不必改动 locator / actions / runner。
        _LOG.debug("target 使用图像定位，路由到 vision 模块: %s", spec.image)
        vision.find_by_image(
            ctx.settings.resolve(ctx.settings.vision.image_dir) / spec.image,
            threshold=ctx.settings.vision.threshold,
            settings=ctx.settings,
        )

    session = ctx.app
    if session is None or session.app is None:
        raise ElementNotFoundError(
            "应用尚未启动，无法定位控件", detail=f"target: {spec.describe()}"
        )

    try:
        ref = ctx.window_ref(spec.window)
    except KeyError as exc:
        known = ", ".join(sorted(ctx.windows)) or "（无）"
        raise ElementNotFoundError(
            f"窗口别名未注册: {spec.window!r}",
            detail=f"已注册别名: {known}；请先用 switch_window 动作注册，或检查 target.window 拼写",
        ) from exc

    default_depth = ctx.settings.run.search_depth
    # 1) 窗口层：top_level_only=True（Application.window 的默认行为）
    node = session.window(**ref.criteria())

    # 2) 祖先链：自上而下逐层收窄
    for parent in spec.parents:
        node = node.child_window(**parent.criteria(default_depth=default_depth))

    # 3) 目标控件本身；空条件时直接返回容器（窗口级动作用）
    target_crit = spec.criteria(default_depth=default_depth)
    if target_crit:
        node = node.child_window(**target_crit)

    _LOG.debug("定位链: %s -> %s", ref.describe(), spec.describe())
    return node
