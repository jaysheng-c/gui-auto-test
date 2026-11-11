"""变量解析。

流程 JSON 中的 ``${name}`` 占位符在**步骤执行前**才被解析，因此可以引用
``set_var`` 动作在运行中写入的值。

解析优先级（高 → 低）::

    内置动态变量(__now__ 等) > 当前作用域(set_var / data_set 行)
      > flow.vars > suite.vars > config.vars

环境变量不参与隐式查找，必须显式写 ``${env:NAME}``，避免机器环境差异
导致用例行为不可预期。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Callable

from .errors import ConfigError, VariableError
from .log import get_logger

__all__ = ["VariableResolver", "build_default_dynamic", "VAR_PATTERN"]

_LOG = get_logger("variables")

# 查找不到且调用方未给默认值时的哨兵，用于区分"未定义"与"定义为 None"
_MISSING = object()

# 变量的值本身还可以是占位符（套件里写 "password": "${env:app_PWD}" 很常见），
# 因此解析要能顺着引用链往下走；上限用于在写错成 a->b->a 时立即报错而不是死循环
_MAX_CHAIN_DEPTH = 8

# ${name} / ${env:NAME} / ${a.b}
VAR_PATTERN = re.compile(r"\$\{\s*([^{}]+?)\s*\}")
# 整个字符串就是一个占位符 —— 此时保留原始类型（int/bool/dict），不做字符串化
_FULL_MATCH = re.compile(r"^\s*\$\{\s*([^{}]+?)\s*\}$")
# 允许的名字：字母数字下划线，支持点号取嵌套
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class VariableResolver:
    """带作用域栈的变量解析器。

    ``scopes`` 从低到高排列，查找时逆序遍历。``child()`` 派生出的解析器
    按引用共享父级作用域，并额外拥有一层自己的可写作用域，这样用例内
    ``set_var`` 不会污染套件级变量。

    **读取**遵循作用域栈，**写入**则始终落在 :meth:`pin` 固定的那一层
    （通常是用例作用域）。两者分开是必要的：``repeat`` / ``if`` 会为子步骤
    压入临时块作用域，若 ``set_var`` 写进块作用域，循环一结束值就消失了，
    而流程作者的意图显然是"把这个值留给后续步骤"。
    """

    def __init__(
        self,
        scopes: list[dict[str, Any]] | None = None,
        *,
        dynamic: dict[str, Callable[[], Any]] | None = None,
        write_scope: dict[str, Any] | None = None,
        _copy: bool = True,
    ) -> None:
        if _copy:
            self.scopes: list[dict[str, Any]] = [dict(s) for s in (scopes or []) if s]
        else:
            # child() 走这条路径：直接引用父级 dict，不复制
            self.scopes = list(scopes or [])
        self._dynamic: dict[str, Callable[[], Any]] = dict(dynamic or {})
        self._write_scope: dict[str, Any] | None = write_scope

    # ------------------------------------------------------------------
    # 作用域管理
    # ------------------------------------------------------------------
    def child(self, extra: dict[str, Any] | None = None) -> VariableResolver:
        """派生子解析器：父作用域按引用共享，并追加一层可写作用域。"""
        return VariableResolver(
            [*self.scopes, dict(extra or {})],
            dynamic=self._dynamic,
            write_scope=self._write_scope,
            _copy=False,
        )

    def push(self, mapping: dict[str, Any] | None = None) -> None:
        self.scopes.append(dict(mapping or {}))

    def pop(self) -> dict[str, Any] | None:
        """弹出最上层作用域；仅剩一层时不动，保证解析器始终可用。"""
        return self.scopes.pop() if len(self.scopes) > 1 else None

    def pin(self) -> VariableResolver:
        """把当前最上层作用域固定为 ``set()`` 的写入目标，返回自身便于链式调用。

        由 runner 在每条用例开始时调用一次；此后派生的所有子解析器都继承这个
        写入目标，因此 ``include`` 的子流程、``repeat`` 的循环体里 ``set_var``
        写入的值，在用例结束前一直可见。
        """
        self._write_scope = self._writable()
        return self

    def set(self, key: str, value: Any) -> None:
        """写入固定作用域（``set_var`` 动作使用）。"""
        target = self._write_scope if self._write_scope is not None else self._writable()
        target[key] = value

    def update(self, mapping: dict[str, Any] | None) -> None:
        if mapping:
            target = self._write_scope if self._write_scope is not None else self._writable()
            target.update(mapping)

    def _writable(self) -> dict[str, Any]:
        if not self.scopes:
            self.scopes.append({})
        return self.scopes[-1]

    def register_dynamic(self, name: str, producer: Callable[[], Any]) -> None:
        """注册动态内置变量，每次引用都重新求值（如 ``__now__``）。"""
        self._dynamic[name] = producer

    def known_names(self) -> list[str]:
        names: set[str] = set()
        for scope in self.scopes:
            names.update(scope.keys())
        names.update(self._dynamic.keys())
        return sorted(names)

    # ------------------------------------------------------------------
    # 查找
    # ------------------------------------------------------------------
    def get(self, name: str, default: Any = _MISSING) -> Any:
        """按优先级查找变量，支持 ``a.b`` 形式的嵌套取值。

        未找到且未提供 ``default`` 时抛 ``VariableError``。
        """
        if name in self._dynamic:
            return self._dynamic[name]()

        if name.startswith("env:"):
            value = os.environ.get(name[4:])
            if value is not None:
                return value
            if default is not _MISSING:
                return default
            raise VariableError(name, known=self.known_names())

        if not _NAME_RE.match(name):
            if default is not _MISSING:
                return default
            raise VariableError(name, known=self.known_names())

        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]

        # 嵌套取值：a.b -> 先在各作用域找 a，再逐层下钻
        if "." in name:
            head, *rest = name.split(".")
            for scope in reversed(self.scopes):
                if head in scope:
                    cur: Any = scope[head]
                    try:
                        for part in rest:
                            cur = cur[part]
                        return cur
                    except (KeyError, IndexError, TypeError):
                        break

        if default is not _MISSING:
            return default
        raise VariableError(name, known=self.known_names())

    def has(self, name: str) -> bool:
        try:
            self.get(name)
            return True
        except VariableError:
            return False

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def resolve(self, value: Any) -> Any:
        """递归解析任意结构中的占位符。

        dict 的 key 同样会被解析（少数动作允许用变量做键名）。
        """
        if isinstance(value, str):
            return self._resolve_str(value)
        if isinstance(value, dict):
            return {self.resolve(k): self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.resolve(v) for v in value)
        return value

    def _resolve_str(self, text: str, depth: int = 0) -> Any:
        if "${" not in text:
            return text
        if depth >= _MAX_CHAIN_DEPTH:
            raise ConfigError(
                f"变量引用链过深或存在循环: {text}",
                detail=f"已展开 {depth} 层仍未得到最终值；请检查 vars 中是否互相引用",
            )

        full = _FULL_MATCH.match(text)
        if full:
            # 整串就是单个占位符：保留原始类型，避免 5 变成 "5" 导致断言误判
            value = self.get(full.group(1))
            if isinstance(value, str) and "${" in value:
                return self._resolve_str(value, depth + 1)
            return value

        def _sub(match: re.Match[str]) -> str:
            resolved = self.get(match.group(1))
            if isinstance(resolved, str) and "${" in resolved:
                resolved = self._resolve_str(resolved, depth + 1)
            if resolved is None:
                return ""
            if isinstance(resolved, bool):
                # JSON 语义下 bool 转字符串用小写，与流程文件书写习惯一致
                return "true" if resolved else "false"
            return str(resolved)

        return VAR_PATTERN.sub(_sub, text)


# ----------------------------------------------------------------------
# 内置动态变量
# ----------------------------------------------------------------------
def build_default_dynamic(
    *,
    run_id: str = "",
    case_id: str = "",
    step_id: str = "",
    iteration: int = 1,
    extra: dict[str, Any] | None = None,
) -> dict[str, Callable[[], Any]]:
    """构造内置变量表。

    时间类变量做成惰性求值，保证每次引用都拿到当下时刻；
    ``extra`` 中的静态值用默认参数绑定，避免闭包捕获循环变量。
    """
    table: dict[str, Callable[[], Any]] = {
        "__now__": lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "__date__": lambda: datetime.now().strftime("%Y-%m-%d"),
        "__time__": lambda: datetime.now().strftime("%H%M%S"),
        "__timestamp__": lambda: int(datetime.now().timestamp()),
        "__run_id__": lambda: run_id,
        "__case_id__": lambda: case_id,
        "__step_id__": lambda: step_id,
        "__iteration__": lambda: iteration,
    }
    for key, val in (extra or {}).items():
        table[key] = lambda _v=val: _v
    return table
