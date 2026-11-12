"""流程编排与执行。

三层结构，职责自上而下收窄：

* :class:`Runner` —— 一次 ``auto_test.py`` 调用的总编排：解析目标套件/流程、
  应用套件级配置覆盖、按 ``--repeat`` 轮次执行、汇总 :class:`RunResult`。
* :class:`FlowRunner` —— 执行一条用例：应用生命周期、变量作用域、
  ``setup`` / ``steps`` / ``teardown`` 三段、``data_set`` 数据驱动。
* :class:`StepExecutor` —— 执行一段步骤：变量绑定、动作派发、失败留证、
  ``on_fail`` 策略；同时充当 ``ctx.executor``，为 ``include`` / ``repeat`` / ``if``
  提供子步骤执行能力。

**应用生命周期是「每条用例一次」**：用例开始前清残留并启动，结束后关闭并再清残留。
这样用例之间互不污染（上一条没关掉的对话框不会让下一条莫名失败），代价是每条用例
多一次启动耗时 —— 对冒烟套件而言，独立性比速度更重要。需要复用已运行实例时用
``--attach``，此时框架不启动也不关闭应用。
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import asserts  # noqa: F401 - 导入即触发断言动作注册
from .actions import ActionResult, get_action, make_element
from .app import AppSession
from .artifacts import safe_name
from .config import Settings
from .context import RunContext
from .element import Element
from .errors import (
    ActionError,
    AppCrashedError,
    AssertFailed,
    ConfigError,
    AppAtError,
    FlowAborted,
    FlowSkipped,
    FocusLostError,
    StepFailure,
)
from .flow import Flow, Step, Suite
from .flow_loader import load_flow, load_schema, load_suite, load_suite_flows
from .log import get_logger, setup_logging
from .report import (
    CaseResult,
    RunResult,
    Status,
    StepResult,
    SuiteResult,
    build_environment,
    render_html_report,
    write_json_report,
    write_latest_pointer,
)
from .variables import VariableResolver

__all__ = [
    "RunOptions",
    "Runner",
    "FlowRunner",
    "StepExecutor",
    "MAX_INCLUDE_DEPTH",
    "FALLBACK_CASE_TIMEOUT",
    "TEARDOWN_WARN",
]

_LOG = get_logger("runner")

# include 嵌套上限：子流程互相 include 会无限递归，给一个明确上限而不是等栈溢出
MAX_INCLUDE_DEPTH = 8
# 用例未声明 timeout 时的兜底看门狗（秒），避免单条用例把整个套件挂死
FALLBACK_CASE_TIMEOUT = 600.0
# 收尾阶段有步骤未成功时打在用例与本次运行上的标记
TEARDOWN_WARN = "TEARDOWN_WARN"


class CaseTimeoutError(ActionError):
    """用例级看门狗触发。

    继承 ``ActionError`` 是为了复用现有的失败处理路径，但状态归为 ``ERROR``
    而非 ``FAIL``：超时通常意味着应用卡死或环境异常，不是断言结论。
    """


# ----------------------------------------------------------------------
# 运行选项
# ----------------------------------------------------------------------
@dataclass
class RunOptions:
    """CLI 传入的运行选项。"""

    suite: str | None = None
    flows: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    repeat: int = 0                       # 0 表示沿用 settings.run.repeat
    attach: bool = False                  # 连接已运行实例，不启动/不关闭
    pid: int | None = None
    continue_on_error: bool | None = None  # None 表示沿用配置
    command_line: str = ""


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _format_error(exc: BaseException) -> str:
    """把异常渲染成报告里可读的文本。

    完整栈写进日志（``run.log``），报告只留结论 —— 报告是给人快速定位用的，
    几十行栈会把真正的原因挤出视野。非框架异常附带少量栈尾，避免信息不足。
    """
    if isinstance(exc, AppAtError):
        return str(exc)
    text = f"{type(exc).__name__}: {exc}".strip()
    frames = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tail = [ln for ln in "".join(frames).splitlines() if ln.strip().startswith('File')]
    if tail:
        text += "\n" + "\n".join(tail[-3:])
    return text


def _infra_step(
    ctx: RunContext,
    case: CaseResult,
    phase: str,
    action: str,
    desc: str,
    fn: Callable[[], Any],
) -> Any:
    """执行一个框架级动作（启动应用、关窗等）并记录为步骤。

    这些动作不是流程 JSON 写的，但把它们记进报告很有价值：启动耗时异常、
    主窗口迟迟不出现，都能直接在步骤表里看到，而不用去翻日志。
    """
    result = StepResult(
        index=len(case.steps) + 1,
        id=action,
        action=action,
        desc=desc,
        phase=phase,
    )
    case.steps.append(result)
    t0 = time.perf_counter()
    try:
        out = fn()
        result.status = Status.PASS
        result.message = str(out) if out else ""
        return out
    except Exception as exc:  # noqa: BLE001 - 统一记录后原样抛出
        result.status = Status.FAIL if isinstance(exc, StepFailure) else Status.ERROR
        result.error = _format_error(exc)
        result.error_type = type(exc).__name__
        _mark_flags_from(ctx, exc, case)
        _LOG.error("%s 失败：%s", desc, exc, exc_info=True)
        raise
    finally:
        result.duration = time.perf_counter() - t0


def _mark_flags_from(
    ctx: RunContext, exc: BaseException, case: CaseResult | None = None
) -> None:
    """把环境类异常转成运行标记，报告里据此区分「产品缺陷」与「环境问题」。"""
    flag: str | None = None
    if isinstance(exc, FocusLostError):
        flag = "FOCUS_LOST"
    elif isinstance(exc, AppCrashedError):
        flag = "APP_CRASHED"
    elif isinstance(exc, CaseTimeoutError):
        flag = "CASE_TIMEOUT"
    if not flag:
        return
    ctx.add_flag(flag)
    if case is not None and flag not in case.flags:
        case.flags.append(flag)


# ======================================================================
# 步骤执行器
# ======================================================================
class StepExecutor:
    """执行一段步骤，并作为 ``ctx.executor`` 支持 include / repeat / if。

    ``run_steps`` 返回执行步数；``include`` 返回子流程执行步数。
    """

    def __init__(
        self,
        ctx: RunContext,
        case: CaseResult,
        resolver: VariableResolver,
        *,
        schema: dict[str, Any] | None = None,
        phase: str = "steps",
        deadline: float | None = None,
        limit: float | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.case = case
        self.resolver = resolver
        self.schema = schema
        self.phase = phase
        self.deadline = deadline
        self.limit = limit
        # include 的相对路径基准：当前正在执行的流程所在目录。
        # 只按全局 flow_dir 解析的话，把流程分子目录存放就会全部找不到子流程
        self.base_dir = base_dir
        # teardown 阶段置 True：收尾步骤的失败只记录，不再触发 abort，
        # 否则一个清理动作失败会让后面的清理全部跳过，残留更严重
        self.force_continue = False
        self._include_depth = 0
        self._include_chain: list[str] = []
        # include 进来的子流程若声明了 teardown，推迟到用例结束时执行 ——
        # 子流程是被 setup 引入的片段，立刻收尾会把它建立的状态拆掉
        self.deferred_teardown: list[Step] = []

    # ------------------------------------------------------------------
    # ctx.executor 契约
    # ------------------------------------------------------------------
    def include(self, name: str, vars: dict[str, Any] | None = None) -> int:
        """执行子流程，返回执行步数。"""
        if self._include_depth >= MAX_INCLUDE_DEPTH:
            raise ActionError(
                f"子流程嵌套超过 {MAX_INCLUDE_DEPTH} 层",
                detail=f"调用链 {self._include_chain}；请检查是否存在互相 include",
            )
        path = self._resolve_include(name)
        sub = load_flow(path, self.ctx.settings, schema=self.schema)

        if not sub.enabled:
            _LOG.info("子流程 %s 已禁用（%s），跳过", sub.id, sub.skip_reason or "enabled=false")
            return 0

        # 调用点传入的 vars 优先于子流程自身的默认值
        merged = dict(sub.vars or {})
        merged.update(vars or {})
        saved_resolver = self.resolver
        saved_base = self.base_dir
        self.resolver = saved_resolver.child(merged)
        # 子流程内的 include 继续以子流程自身目录为基准
        self.base_dir = path.parent
        self._include_depth += 1
        self._include_chain.append(sub.id)
        _LOG.info("→ include %s（%s）", name, sub.id)
        try:
            count = self.run_steps(sub.setup, label=f"{sub.id}.setup")
            count += self.run_steps(sub.steps, label=f"{sub.id}.steps")
            self.deferred_teardown.extend(sub.teardown)
            return count
        finally:
            self._include_depth -= 1
            self._include_chain.pop()
            self.resolver = saved_resolver
            self.base_dir = saved_base

    def _resolve_include(self, name: str) -> Path:
        """解析 include 引用的子流程，**调用方目录优先**，再回退到全局 ``flow_dir``。

        与 :func:`_resolve_suite_flow` 保持同一口径：相对引用先按“和当前文件
        放一起”理解，这样流程可以按模块分子目录存放；找不到才交给
        ``resolve_flow_file`` 去项目根与 ``flow_dir`` 里搜，兼容只写文件名的旧流程。
        """
        raw = str(name).strip()
        if raw and self.base_dir is not None:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                for cand in (self.base_dir / p, self.base_dir / f"{p.name}.json"):
                    if cand.is_file():
                        return cand.resolve()
        return self.ctx.settings.resolve_flow_file(raw)

    def run_steps(
        self,
        steps: Sequence[Step] | None,
        extra_vars: dict[str, Any] | None = None,
        label: str = "",
        phase: str | None = None,
    ) -> int:
        """顺序执行一段步骤，返回执行步数。

        ``FlowAborted`` / ``FlowSkipped`` 会向上穿透 —— 它们表达的是「终止整条
        流程」，被 repeat / if 吞掉就等于 ``on_fail: abort`` 失效了。
        """
        if not steps:
            return 0
        saved_phase = self.phase
        if phase:
            self.phase = phase
        pushed = bool(extra_vars)
        if pushed:
            # 块级作用域只影响读取；set_var 的写入目标是 pin 住的用例作用域
            self.resolver.push(extra_vars)
        count = 0
        try:
            for step in steps:
                self.run_step(step, label=label)
                count += 1
        finally:
            if pushed:
                self.resolver.pop()
            self.phase = saved_phase
        return count

    # ------------------------------------------------------------------
    # 单个步骤
    # ------------------------------------------------------------------
    def run_step(self, step: Step, *, label: str = "") -> StepResult:
        """执行单个步骤，结果追加到 ``case.steps``。"""
        self._check_deadline()
        index = len(self.case.steps) + 1
        result = StepResult(
            index=index,
            id=step.id,
            action=step.action,
            desc=step.desc,
            phase=self.phase,
            optional=bool(step.optional),
            target=step.target.describe() if step.target else (step.window or ""),
        )
        self.case.steps.append(result)

        ctx = self.ctx
        ctx.set_step_context(self.case.case_id, step.id or str(index), self.case.iteration)
        prefix = f"{label}/" if label else ""
        _LOG.info("%s%s", prefix, step.label())

        bound = step
        element: Element | None = None
        t0 = time.perf_counter()
        try:
            # 变量在执行前才绑定，才能读到上一步 set_var 写入的值
            bound = step.bind(self.resolver)
            result.desc = bound.desc or result.desc
            result.target = bound.target.describe() if bound.target else (bound.window or "")
            result.optional = bool(bound.optional)

            action_def = get_action(bound.action)
            _check_required_params(action_def, bound)
            # Element 构造是惰性的（不触发 UIA 调用），因此无条件创建，
            # set_var 这类动作也能靠 target 从控件取文本
            element = make_element(ctx, bound)
            out = action_def.fn(ctx, bound, element) or ActionResult()

            result.status = Status.SKIP if out.skipped else Status.PASS
            result.message = out.message or ""
            result.warnings.extend(out.warnings)
            if out.message:
                _LOG.debug("%s→ %s", prefix, out.message)
        except (FlowAborted, FlowSkipped):
            # 流程控制信号，不是失败：先摘掉刚追加的空结果再上抛
            if result.status == Status.PASS and not result.message:
                self.case.steps.remove(result)
            raise
        except Exception as exc:  # noqa: BLE001 - 步骤失败在此统一收敛
            self._handle_failure(bound, result, element, exc)
        finally:
            result.duration = time.perf_counter() - t0

        # 只有走到这里才是成功路径（失败分支的留证已在 _handle_failure 内完成，
        # 必须放在那里：_apply_on_fail 会抛 FlowAborted，之后再留证就来不及了）
        self._capture(bound, result, element, failed=False)
        return result

    # ------------------------------------------------------------------
    def _handle_failure(
        self, bound: Step, result: StepResult, element: Element | None, exc: Exception
    ) -> None:
        result.error = _format_error(exc)
        result.error_type = type(exc).__name__
        if isinstance(exc, AssertFailed):
            result.expected = exc.expected
            result.actual = exc.actual
        _mark_flags_from(self.ctx, exc, self.case)
        _LOG.error("%s 失败：%s", result.label, exc, exc_info=True)

        # 框架/配置错误（未注册动作、schema 非法、应用崩溃）不是测试结论，
        # 也不能被 optional 掩盖 —— 那属于用例写错了
        is_framework = not isinstance(exc, StepFailure)
        result.status = Status.ERROR if is_framework else Status.FAIL

        # 留证先于一切分支：optional 与 on_fail 都可能让流程就此结束
        self._capture(bound, result, element, failed=True)

        if bound.optional and not is_framework:
            result.status = Status.SKIP
            result.warnings.append(f"可选步骤失败，已忽略：{exc}")
            _LOG.warning("可选步骤 %s 失败，按 optional 忽略", result.label)
            return

        self._apply_on_fail(bound, result)

    def _apply_on_fail(self, step: Step, result: StepResult) -> None:
        if self.force_continue or step.on_fail == "continue":
            return
        reason = f"{result.label} → {result.status}"
        if step.on_fail == "skip_flow":
            _LOG.warning("on_fail=skip_flow，跳过用例剩余步骤（%s）", reason)
            raise FlowSkipped(reason)
        raise FlowAborted(reason)

    def _capture(
        self, bound: Step, result: StepResult, element: Element | None, *, failed: bool
    ) -> None:
        """按策略落盘截图与控件树快照。"""
        run_cfg = self.ctx.settings.run
        mode = str(bound.screenshot or run_cfg.screenshot or "on_fail").lower()
        stem = self.ctx.artifact_stem(bound.action)

        if mode == "always" or (mode == "on_fail" and failed):
            path = self.ctx.artifacts.capture_screen(stem)
            if path:
                result.screenshot = self.ctx.artifacts.rel(path)

        want_tree = failed and run_cfg.dump_tree_on_fail and bound.dump_tree is not False
        if not want_tree:
            return
        root = self._tree_root(element)
        if root is None:
            _LOG.debug("无法取得控件树根对象，跳过快照")
            return
        header = (
            f"用例 {self.case.case_id} 步骤 {result.label}\n"
            f"失败原因: {result.error}\n"
            f"定位条件: {result.target}"
        )
        path = self.ctx.artifacts.dump_tree(root, stem, header=header)
        if path:
            result.tree = self.ctx.artifacts.rel(path)

    def _tree_root(self, element: Element | None) -> Any:
        """控件树快照的根：优先目标所在容器，退化到主窗口，再退化到任一顶层窗口。"""
        if element is not None:
            root = element.dump_root()
            if root is not None:
                return root
        session = self.ctx.app
        if session is None:
            return None
        try:
            ref = self.ctx.window_ref()
            return session.window(**ref.criteria()).wrapper_object()
        except Exception:  # noqa: BLE001
            windows = session.top_windows()
            return windows[0] if windows else None

    def _check_deadline(self) -> None:
        if self.deadline is not None and time.perf_counter() > self.deadline:
            limit = self.limit or 0
            raise CaseTimeoutError(
                f"用例执行超过 {limit:.0f}s 上限，已中止剩余步骤",
                detail="可能是应用卡死、某步骤等待过长，或 timeout 设置偏小",
            )


def _check_required_params(action_def: Any, step: Step) -> None:
    """校验动作声明的必填参数。

    参数可能在 ``step.params``（动作专属键），也可能是 Step 的具名字段
    （``include`` 的 ``flow``、``if`` 的 ``cond``），两处都要看。
    """
    missing = [
        name
        for name in action_def.required_params
        if step.params.get(name) is None and getattr(step, name, None) is None
    ]
    if missing:
        raise ActionError(
            f"动作 {step.action!r} 缺少必填参数 {missing}",
            detail=f"已提供: {sorted(step.params)}；参数说明见 docs/flow_authoring.md",
        )


# ======================================================================
# 用例执行器
# ======================================================================
class FlowRunner:
    """执行一条 :class:`Flow`，产出 1..n 个 :class:`CaseResult`。"""

    def __init__(
        self,
        ctx: RunContext,
        *,
        base_resolver: VariableResolver,
        schema: dict[str, Any] | None = None,
        attach: bool = False,
        pid: int | None = None,
    ) -> None:
        self.ctx = ctx
        self.base_resolver = base_resolver
        self.schema = schema
        self.attach = attach
        self.pid = pid
        self.app_env: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    def run(self, flow: Flow, *, repeat_round: int = 1) -> list[CaseResult]:
        if not flow.enabled:
            reason = flow.skip_reason or "enabled=false"
            _LOG.info("用例 %s 已禁用，跳过（%s）", flow.id, reason)
            return [
                CaseResult(
                    case_id=flow.id,
                    name=flow.name,
                    tags=list(flow.tags),
                    priority=flow.priority,
                    source=str(flow.source or ""),
                    iteration=repeat_round,
                    status=Status.SKIP,
                    skip_reason=reason,
                )
            ]

        rows: list[dict[str, Any]] = list(flow.data_set) or [{}]
        total = len(rows)
        return [
            self._run_once(flow, row, i, total, repeat_round)
            for i, row in enumerate(rows, start=1)
        ]

    # ------------------------------------------------------------------
    def _run_once(
        self,
        flow: Flow,
        row: dict[str, Any],
        row_index: int,
        rows_total: int,
        repeat_round: int,
    ) -> CaseResult:
        ctx = self.ctx
        # 数据驱动时把行号编进 case_id：产物文件名因此不会互相覆盖，
        # 报告里也能一眼看出是哪一行数据失败
        case_id = flow.id if rows_total <= 1 else f"{flow.id}[{row_index}]"
        name = flow.name if rows_total <= 1 else f"{flow.name}（数据行 {row_index}/{rows_total}）"
        case = CaseResult(
            case_id=case_id,
            name=name,
            tags=list(flow.tags),
            priority=flow.priority,
            source=str(flow.source or ""),
            iteration=repeat_round,
        )
        t0 = time.perf_counter()
        ctx.reset_case_windows()

        # 变量优先级：data_set 行 > flow.vars > suite.vars > config.vars
        scope = dict(flow.vars or {})
        scope.update(row or {})
        resolver = self.base_resolver.child(scope).pin()
        resolver.register_dynamic("__case_id__", lambda: case_id)
        resolver.register_dynamic("__iteration__", lambda: repeat_round)
        resolver.register_dynamic("__data_row__", lambda: dict(row or {}))
        ctx.case_resolver = resolver

        limit = ctx.scaled(flow.timeout if flow.timeout else FALLBACK_CASE_TIMEOUT)
        executor = StepExecutor(
            ctx, case, resolver, schema=self.schema,
            deadline=time.perf_counter() + limit, limit=limit,
            base_dir=flow.source.parent if flow.source else None,
        )
        ctx.executor = executor

        _LOG.info("")
        _LOG.info("─" * 72)
        _LOG.info("用例 %s%s", case_id, f" — {flow.name}" if flow.name else "")

        try:
            self._prepare_app(case)
            executor.run_steps(flow.setup, phase="setup")
            executor.run_steps(flow.steps, phase="steps")
        except FlowSkipped as sig:
            case.skip_reason = sig.reason or "流程被跳过"
            _LOG.info("用例 %s 跳过剩余步骤：%s", case_id, sig.reason)
        except FlowAborted as sig:
            _LOG.warning("用例 %s 中止剩余步骤：%s", case_id, sig.reason)
        except Exception as exc:  # noqa: BLE001 - 用例级异常（多为启动失败）
            case.error = _format_error(exc)
            case.error_type = type(exc).__name__
            _mark_flags_from(ctx, exc, case)
            _LOG.error("用例 %s 异常终止：%s", case_id, exc, exc_info=True)
        finally:
            try:
                self._teardown(flow, case, executor)
            finally:
                executor.deadline = None
                ctx.executor = None
                ctx.case_resolver = ctx.resolver
                ctx.set_step_context("", "", 1)

        case.status = _case_status(case)
        case.duration = time.perf_counter() - t0
        _LOG.info("用例 %s 结果：%s（%.2fs）", case_id, case.status.upper(), case.duration)
        return case

    # ------------------------------------------------------------------
    def _prepare_app(self, case: CaseResult) -> None:
        """启动（或连接）被测应用并注册主窗口别名。"""
        ctx = self.ctx
        settings = ctx.settings
        log_dir = ctx.root_dir / "app_logs" / safe_name(case.case_id)

        def _start() -> str:
            session = AppSession(settings, log_dir=log_dir)
            ctx.app = session
            if self.attach:
                session.connect_existing(pid=self.pid, title=settings.app.main_window.title)
                return f"已连接运行中的实例 pid={session.pid}"
            exe = settings.resolve_app_path(None)
            session.exe_path = exe
            # 上一条用例若异常退出，残留进程会与新实例抢同名窗口，先清干净
            killed = session.kill_leftovers()
            pid = session.launch(exe)
            return f"已启动 pid={pid}" + (f"（清理残留进程 {killed} 个）" if killed else "")

        _infra_step(ctx, case, "setup", "launch_app", "启动被测应用", _start)

        session = ctx.app
        assert session is not None  # _infra_step 成功即已赋值
        wrapper = _infra_step(
            ctx, case, "setup", "wait_main_window", "等待主窗口出现", session.wait_main_window
        )
        # 用**配置里的**识别条件注册别名，而不是刚取到的窗口标题：
        # app 登录后标题可能变化，写死精确标题会让后续步骤全部定位失败
        mw = settings.app.main_window
        ctx.register_main_window(
            title=mw.title,
            title_re=mw.title_re,
            auto_id=mw.auto_id,
            class_name=mw.class_name,
            process=session.pid,
        )
        if self.app_env is None:
            self.app_env = session.env_snapshot()
        try:
            _LOG.info("主窗口标题：%r", wrapper.window_text())
        except Exception:  # noqa: BLE001
            pass

    def _teardown(self, flow: Flow, case: CaseResult, executor: StepExecutor) -> None:
        """收尾：先跑清理步骤，再关应用。无论前面发生什么都必然执行。"""
        executor.phase = "teardown"
        executor.deadline = None
        executor.force_continue = True
        first = len(case.steps)
        for step in [*executor.deferred_teardown, *flow.teardown]:
            try:
                executor.run_step(step)
            except (FlowAborted, FlowSkipped) as sig:
                _LOG.info("teardown 提前结束：%s", sig)
                break
            except Exception as exc:  # noqa: BLE001 - 收尾异常只记录
                _LOG.error("teardown 步骤异常：%s", exc, exc_info=True)
        executor.deferred_teardown.clear()
        self._shutdown_app(case)
        self._flag_teardown_problems(case, first)

    def _flag_teardown_problems(self, case: CaseResult, first: int) -> None:
        """把 teardown 阶段的失败汇总成一个运行标记。

        收尾失败不改变用例结论（见 :func:`_case_status`），但必须显眼：
        “弹窗没点掉导致下一条用例起步就不干净”这类问题靠它暴露。
        """
        bad = [st for st in case.steps[first:] if st.status in (Status.FAIL, Status.ERROR)]
        if not bad:
            return
        if TEARDOWN_WARN not in case.flags:
            case.flags.append(TEARDOWN_WARN)
        self.ctx.add_flag(TEARDOWN_WARN)
        names = ", ".join(st.label for st in bad)
        _LOG.warning("teardown 中有 %d 个步骤未成功：%s", len(bad), names)

    def _shutdown_app(self, case: CaseResult) -> None:
        ctx = self.ctx
        session = ctx.app
        if session is None:
            return
        try:
            if self.attach:
                _LOG.info("--attach 模式：保留应用进程 pid=%s", session.pid)
                return

            def _close() -> str:
                session.close(force=False, timeout=10.0)
                left = session.kill_leftovers()
                return "已正常退出" + (f"，清理残留进程 {left} 个" if left else "")

            _infra_step(
                ctx, case, "teardown", "close_app", "关闭被测应用并清理残留进程", _close
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.error("关闭应用失败，强制结束：%s", exc)
            session.kill()
        finally:
            ctx.app = None


def _case_status(case: CaseResult) -> str:
    """汇总步骤状态得到用例状态。

    **teardown 阶段的步骤不参与判定**：测试结论在主体步骤跑完时就已经成立，
    一个清理动作失败（弹窗没点掉、应用不响应 WM_CLOSE）不应该把通过的用例
    翻成失败 —— 那会很快摧毁团队对套件的信任。失败仍然完整记录在步骤表里，
    并通过 ``TEARDOWN_WARN`` 标记提醒（残留进程由 ``kill_leftovers`` 兜底）。
    """
    statuses = [st.status for st in case.steps if st.phase != "teardown"]
    if case.error or Status.ERROR in statuses:
        return Status.ERROR
    if Status.FAIL in statuses:
        return Status.FAIL
    if not statuses:
        return Status.SKIP if case.skip_reason else Status.PASS
    if Status.PASS not in statuses:
        # 全是 SKIP：用例被禁用，或所有步骤都是失败但被 optional 忽略
        return Status.SKIP
    return Status.PASS


def _suite_status(suite: SuiteResult) -> str:
    statuses = [c.status for c in suite.cases]
    if Status.ERROR in statuses:
        return Status.ERROR
    if Status.FAIL in statuses:
        return Status.FAIL
    if not statuses or Status.PASS not in statuses:
        return Status.SKIP
    return Status.PASS


# ======================================================================
# 总编排
# ======================================================================
class Runner:
    """一次 ``auto_test.py`` 调用的总编排。"""

    def __init__(
        self,
        settings: Settings,
        options: RunOptions | None = None,
        *,
        ctx: RunContext | None = None,
    ) -> None:
        self.settings = settings
        self.options = options or RunOptions()
        self.ctx = ctx or RunContext(settings)
        # 文件日志此刻才能挂上：路径依赖 run_id，而 run_id 由 RunContext 生成
        setup_logging(
            settings.log.level, self.ctx.log_file if settings.log.file else None
        )
        self.schema: dict[str, Any] | None = None
        self.result: RunResult | None = None
        self._load_error: AppAtError | None = None
        self._app_env: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    def run(self) -> RunResult:
        ctx = self.ctx
        result = RunResult(
            run_id=ctx.run_id,
            started_at=ctx.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            command_line=self.options.command_line,
        )
        self.result = result
        _LOG.info("运行编号 %s", ctx.run_id)
        _LOG.info("产物目录 %s", ctx.root_dir)

        try:
            suite, flows = self._resolve_targets()
        except AppAtError as exc:
            self._load_error = exc
            _LOG.error("加载测试目标失败：%s", exc)
            result.suites.append(
                SuiteResult(
                    name="加载失败",
                    status=Status.ERROR,
                    cases=[
                        CaseResult(
                            case_id="<load>",
                            name="加载套件 / 流程文件",
                            status=Status.ERROR,
                            error=_format_error(exc),
                            error_type=type(exc).__name__,
                        )
                    ],
                )
            )
            return self._finish(result)

        flow_runner = FlowRunner(
            ctx,
            base_resolver=ctx.resolver.child(dict(suite.vars or {})),
            schema=self.schema,
            attach=self.options.attach,
            pid=self.options.pid,
        )
        try:
            self._execute(suite, flows, flow_runner, result)
        finally:
            self._app_env = flow_runner.app_env
            self._cleanup_leftovers()
        return self._finish(result)

    # ------------------------------------------------------------------
    def _resolve_targets(self) -> tuple[Suite, list[Flow]]:
        """确定要跑哪些流程，并应用套件级配置覆盖。"""
        opt = self.options
        self.schema = load_schema(self.settings)

        if opt.flows:
            suite = Suite(suite="adhoc", name="命令行指定的流程")
            flows = [
                load_flow(self.settings.resolve_flow_file(n), self.settings, schema=self.schema)
                for n in opt.flows
            ]
        else:
            path = self.settings.resolve_flow_file(opt.suite or self.settings.default_suite)
            suite = load_suite(path, self.settings, schema=self.schema)
            # 覆盖必须在加载流程之前生效：步骤默认超时、重试次数都来自最终配置
            self.settings.apply_overrides(suite.app_overrides, prefix="app")
            self.settings.apply_overrides(suite.settings_overrides, prefix="run")
            self.settings.validate()
            flows = load_suite_flows(suite, self.settings, schema=self.schema)

        ids = {c.strip() for c in opt.cases if c.strip()} or None
        tags = {t.strip() for t in opt.tags if t.strip()} or None
        selected = [f for f in flows if f.matches(ids=ids, tags=tags)]
        if not selected:
            raise ConfigError(
                "没有匹配的用例",
                detail=(
                    f"共加载 {len(flows)} 条流程，过滤条件 "
                    f"case={sorted(ids) if ids else '无'} tags={sorted(tags) if tags else '无'}；"
                    f"可用用例: {', '.join(f.id for f in flows) or '（空）'}"
                ),
            )
        if len(selected) != len(flows):
            _LOG.info("过滤后保留 %d/%d 条用例", len(selected), len(flows))
        return suite, selected

    # ------------------------------------------------------------------
    def _execute(
        self,
        suite: Suite,
        flows: list[Flow],
        flow_runner: FlowRunner,
        result: RunResult,
    ) -> None:
        ctx = self.ctx
        opt = self.options
        repeat = max(1, int(opt.repeat or self.settings.run.repeat or 1))
        continue_on_error = (
            self.settings.run.continue_on_error
            if opt.continue_on_error is None
            else bool(opt.continue_on_error)
        )

        suite_result = SuiteResult(name=suite.display_name, source=str(suite.source or ""))
        result.suites.append(suite_result)
        _LOG.info(
            "套件「%s」：%d 条用例，重复 %d 轮，continue_on_error=%s",
            suite_result.name, len(flows), repeat, continue_on_error,
        )

        t0 = time.perf_counter()
        stop = False
        try:
            for round_index in range(1, repeat + 1):
                if repeat > 1:
                    _LOG.info("=== 第 %d/%d 轮 ===", round_index, repeat)
                for flow in flows:
                    if stop:
                        break
                    cases = self._run_flow_safely(flow_runner, flow, round_index)
                    suite_result.cases.extend(cases)
                    if not continue_on_error and any(
                        c.status in (Status.FAIL, Status.ERROR) for c in cases
                    ):
                        _LOG.error(
                            "用例 %s 未通过且未开启 continue_on_error，终止后续用例",
                            cases[0].case_id,
                        )
                        stop = True
        except KeyboardInterrupt:
            ctx.add_flag("INTERRUPTED")
            _LOG.warning("收到中断信号，提前结束（已完成 %d 条用例）", len(suite_result.cases))

        suite_result.duration = time.perf_counter() - t0
        suite_result.status = _suite_status(suite_result)

    def _run_flow_safely(
        self, flow_runner: FlowRunner, flow: Flow, round_index: int
    ) -> list[CaseResult]:
        """单条用例的意外异常不能拖垮整个套件。"""
        try:
            return flow_runner.run(flow, repeat_round=round_index)
        except Exception as exc:  # noqa: BLE001
            _LOG.error("用例 %s 发生未预期异常：%s", flow.id, exc, exc_info=True)
            return [
                CaseResult(
                    case_id=flow.id,
                    name=flow.name,
                    tags=list(flow.tags),
                    priority=flow.priority,
                    source=str(flow.source or ""),
                    iteration=round_index,
                    status=Status.ERROR,
                    error=_format_error(exc),
                    error_type=type(exc).__name__,
                )
            ]

    # ------------------------------------------------------------------
    def _cleanup_leftovers(self) -> None:
        """整个运行结束时再扫一遍，确保不留 app 进程。"""
        if self.options.attach:
            self.ctx.app = None
            return
        probe = AppSession(self.settings)
        try:
            session = self.ctx.app
            if session is not None and session.exe_path:
                probe.exe_path = session.exe_path
            else:
                probe.exe_path = self.settings.resolve_app_path(None)
            killed = probe.kill_leftovers()
            if killed:
                _LOG.warning("收尾清理残留进程 %d 个", killed)
        except AppAtError as exc:
            _LOG.debug("跳过残留进程清理：%s", exc)
        finally:
            self.ctx.app = None

    def _finish(self, result: RunResult) -> RunResult:
        result.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result.duration = self.ctx.elapsed
        result.flags = sorted(self.ctx.flags)
        result.environment = build_environment(self._app_env)
        result.exit_code = self.exit_code()
        return result

    # ------------------------------------------------------------------
    def exit_code(self) -> int:
        """0 全通过 / 1 有用例未通过 / 2 框架或配置错误。"""
        if self._load_error is not None:
            return self._load_error.exit_code
        if self.result is None:
            return 2
        counts = self.result.counts
        return 1 if (counts[Status.FAIL] + counts[Status.ERROR]) else 0

    def write_reports(self) -> dict[str, Path]:
        """落盘 JSON 与 HTML 报告，返回实际写出的路径。"""
        cfg = self.settings.report
        root = self.ctx.root_dir
        out: dict[str, Path] = {}
        if self.result is None:
            return out
        # JSON 必须先于 HTML：render_html_report 会把截图 base64 回填进结果对象，
        # 顺序颠倒会让 result.json 体积膨胀几十倍
        if cfg.json:
            out["json"] = write_json_report(self.result, root / "result.json")
        html: Path | None = None
        if cfg.html:
            html = render_html_report(
                self.result, root / "report.html", self.ctx.artifacts,
                embed=cfg.embed_screenshots,
            )
            out["html"] = html
        write_latest_pointer(self.settings.report_root, self.result, html)
        return out


# ----------------------------------------------------------------------
# 只读的清单能力（--list 用）
# ----------------------------------------------------------------------
def _iter_steps(steps: Sequence[Step] | None) -> Iterator[Step]:
    """深度遍历步骤，含 repeat 的 ``steps`` 与 if 的 ``then`` / ``else``。"""
    for st in steps or []:
        yield st
        yield from _iter_steps(st.steps)
        yield from _iter_steps(st.then_steps)
        yield from _iter_steps(st.else_steps)


def _all_steps(flow: Flow) -> list[Step]:
    return list(_iter_steps([*flow.setup, *flow.steps, *flow.teardown]))


def _resolve_file_name(settings: Settings, raw: str, base_dir: Path | None = None) -> str:
    """把流程名解析成文件名；解析不了就退而取字面量的 basename。

    ``--list`` 是只读命令，引用的文件缺失应该体现在清单里而不是抛异常。
    ``base_dir`` 用于相对引用：include 以调用方目录为基准、套件的 flows 以
    套件目录为基准，与运行时的解析口径保持一致，否则子目录里的流程会被误判为
    “未入套件”而重复出现在用例清单里。
    """
    if base_dir is not None and "${" not in raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            for cand in (base_dir / p, base_dir / f"{p.name}.json"):
                if cand.is_file():
                    return cand.name
    try:
        return settings.resolve_flow_file(raw).name
    except AppAtError:
        return Path(raw).name


def collect_inventory(settings: Settings) -> dict[str, Any]:
    """扫描流程目录，汇总套件 / 用例 / 标签 / 动作清单。

    单独成函数而不放在 Runner 里：``--list`` 不启动应用、不产生报告目录，
    走 Runner 会白建一个 ``reports/<run_id>/`` 空壳。

    返回的每个用例条目带两个标志：``sub``（被其他流程 include，属于可复用
    片段而不是独立用例）与 ``in_suite``（被某个套件引用）。``--list`` 据此分区
    展示，否则子流程会混在用例里让人误以为能单独跑。
    """
    from .actions import ACTION_REGISTRY
    from .flow_loader import discover_flow_files

    schema = load_schema(settings)
    suites: list[dict[str, Any]] = []
    flows: list[dict[str, Any]] = []
    tags: dict[str, int] = {}
    errors: list[str] = []
    included: set[str] = set()

    for path in discover_flow_files(settings):
        try:
            from .flow_loader import _is_suite_data, load_json

            data = load_json(path)
            if isinstance(data, dict) and _is_suite_data(data, path):
                suite = load_suite(path, settings, schema=schema)
                suites.append(
                    {"file": path.name, "path": path, "suite": suite.suite, "name": suite.name,
                     "flows": suite.flows}
                )
                continue
            flow = load_flow(path, settings, schema=schema)
            all_steps = _all_steps(flow)
            refs = {st.flow for st in all_steps if st.action == "include" and st.flow}
            included.update(_resolve_file_name(settings, r, path.parent) for r in refs)
            flows.append(
                {"file": path.name, "id": flow.id, "name": flow.name,
                 "tags": flow.tags, "priority": flow.priority,
                 "steps": len(all_steps), "enabled": flow.enabled,
                 "includes": sorted(refs)}
            )
            for t in flow.tags:
                tags[t] = tags.get(t, 0) + 1
        except AppAtError as exc:
            errors.append(f"{path.name}: {exc}")

    in_suite = {
        _resolve_file_name(settings, raw, s.get("path").parent if s.get("path") else None)
        for s in suites
        for raw in s["flows"]
    }
    for entry in flows:
        entry["sub"] = entry["file"] in included
        entry["in_suite"] = entry["file"] in in_suite

    categories: dict[str, list[str]] = {}
    for name, action_def in sorted(ACTION_REGISTRY.items()):
        categories.setdefault(action_def.category or "other", []).append(name)

    return {
        "suites": suites,
        "flows": flows,
        "tags": dict(sorted(tags.items())),
        "actions": categories,
        "errors": errors,
    }
