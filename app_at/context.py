"""运行上下文。

``RunContext`` 贯穿一次 ``auto_test.py`` 调用，持有配置、产物目录、变量解析器、
应用会话以及**窗口别名表**。它是所有动作函数的第一个入参，动作因此无需关心
全局状态，也便于后续把执行并发化（每个 worker 一个 context）。

窗口别名的意义：流程 JSON 里写 ``"window": "login_dialog"`` 比反复写一长串
定位条件可读得多，别名由 ``switch_window`` 动作在运行时注册。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .artifacts import Artifacts, safe_name
from .config import MAIN_WINDOW_ALIAS, Settings
from .log import get_logger
from .variables import VariableResolver, build_default_dynamic

if TYPE_CHECKING:  # 仅用于类型标注，避免与 app 模块循环导入
    from .app import AppSession

__all__ = ["WindowRef", "RunContext"]


@dataclass
class WindowRef:
    """一个已注册窗口的定位条件。

    只存**条件**而不存 pywinauto wrapper 实例：Qt 界面刷新后 COM 引用会失效，
    每次使用都应基于条件重新查找。
    """

    alias: str
    title: str | None = None
    title_re: str | None = None
    auto_id: str | None = None
    class_name: str | None = None
    handle: int | None = None
    process: int | None = None
    best_match: str | None = None
    depth: int | None = None
    # 记录来源，报告与日志里能看出这个窗口是谁注册的
    source: str = "config"

    def criteria(self) -> dict[str, Any]:
        """转成 ``app.window(**kwargs)`` 可用的关键字参数。"""
        crit: dict[str, Any] = {}
        if self.title is not None:
            crit["title"] = self.title
        if self.title_re is not None:
            crit["title_re"] = self.title_re
        if self.auto_id is not None:
            crit["auto_id"] = self.auto_id
        if self.class_name is not None:
            crit["class_name"] = self.class_name
        if self.handle is not None:
            crit["handle"] = self.handle
        if self.process is not None:
            crit["process"] = self.process
        if self.best_match is not None:
            crit["best_match"] = self.best_match
        if self.depth is not None:
            crit["depth"] = self.depth
        return crit

    def describe(self) -> str:
        parts = [f"{k}={v!r}" for k, v in self.criteria().items()]
        return f"<window {self.alias}: " + (" ".join(parts) or "无条件") + ">"


class RunContext:
    """一次运行的全局上下文。"""

    def __init__(
        self,
        settings: Settings,
        run_id: str | None = None,
        *,
        root_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.root_dir = Path(root_dir) if root_dir else settings.report_root / self.run_id
        self.root_dir.mkdir(parents=True, exist_ok=True)

        self.logger = get_logger("run")
        self.artifacts = Artifacts(self.root_dir)
        self.started_at = datetime.now()
        self._t0 = time.perf_counter()

        # 全局变量作用域：config.vars 为最底层，suite/flow 依次派生子解析器
        self.resolver = VariableResolver(
            [dict(settings.vars)],
            dynamic=build_default_dynamic(
                run_id=self.run_id,
                extra={
                    "__project_root__": str(settings.project_root),
                    "__report_dir__": str(self.root_dir),
                    "__app_path__": str(settings.app.path or ""),
                },
            ),
        )

        self.app: AppSession | None = None
        self.windows: dict[str, WindowRef] = {}
        # 当前用例的变量作用域，由 runner 在每条用例开始时设置。
        # set_var / if 等动作读写的是它，而不是全局 resolver，
        # 这样用例内写入的变量不会泄漏到下一条用例。
        self.case_resolver: VariableResolver = self.resolver
        # 子步骤/子流程执行器，由 runner 注入，供 include / repeat / if 使用。
        # 放在上下文而不放在参数里，是为了避开 actions <-> runner 的循环导入。
        self.executor: Any = None
        # 当前执行位置，用于日志前缀与产物命名
        self.current_case: str = ""
        self.current_step: str = ""
        self.current_iteration: int = 1
        # 运行期标记，报告里用于区分真实缺陷与环境问题
        self.flags: set[str] = set()

    # ------------------------------------------------------------------
    # 基本属性
    # ------------------------------------------------------------------
    @property
    def log_file(self) -> Path:
        return self.root_dir / "run.log"

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def scaled(self, timeout: float | None) -> float:
        """按全局 ``timeout_scale`` 放大超时。"""
        return self.settings.scaled(timeout)

    def add_flag(self, flag: str) -> None:
        self.flags.add(flag)

    # ------------------------------------------------------------------
    # 窗口别名
    # ------------------------------------------------------------------
    def register_window(self, ref: WindowRef) -> None:
        self.windows[ref.alias] = ref
        self.logger.debug("注册窗口别名 %s", ref.describe())

    def register_main_window(
        self,
        *,
        title: str | None = None,
        title_re: str | None = None,
        process: int | None = None,
        class_name: str | None = None,
    ) -> WindowRef:
        """注册主窗口别名 ``main``。"""
        ref = WindowRef(
            alias=MAIN_WINDOW_ALIAS,
            title=title,
            title_re=title_re,
            class_name=class_name,
            process=process,
            source="app",
        )
        self.register_window(ref)
        return ref

    def window_ref(self, alias: str | None = None) -> WindowRef:
        """取窗口定位条件；别名不存在时抛 ``KeyError`` 由调用方转成友好错误。"""
        key = alias or MAIN_WINDOW_ALIAS
        if key not in self.windows:
            raise KeyError(key)
        return self.windows[key]

    def has_window(self, alias: str) -> bool:
        return alias in self.windows

    def reset_case_windows(self) -> None:
        """用例开始时清掉非 main 的别名，避免上一条用例注册的对话框干扰下一条。"""
        for alias in [a for a in self.windows if a != MAIN_WINDOW_ALIAS]:
            del self.windows[alias]

    # ------------------------------------------------------------------
    # 变量
    # ------------------------------------------------------------------
    def child_resolver(self, extra: dict[str, Any] | None = None) -> VariableResolver:
        return self.resolver.child(extra)

    def set_step_context(self, case_id: str, step_id: str, iteration: int = 1) -> None:
        self.current_case = case_id
        self.current_step = step_id
        self.current_iteration = iteration

    # ------------------------------------------------------------------
    # 产物命名
    # ------------------------------------------------------------------
    def artifact_stem(self, suffix: str = "") -> str:
        """生成 ``<case>_<step>[_<suffix>]`` 形式的产物文件名主干。"""
        parts = [
            safe_name(self.current_case or "case"),
            safe_name(self.current_step or "step"),
        ]
        if self.current_iteration > 1:
            parts.append(f"iter{self.current_iteration}")
        if suffix:
            parts.append(safe_name(suffix))
        return "_".join(p for p in parts if p)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RunContext run_id={self.run_id} root={self.root_dir} windows={list(self.windows)}>"
