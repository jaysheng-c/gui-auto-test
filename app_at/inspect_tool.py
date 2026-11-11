"""控件树探测工具（``--inspect``）。

写用例之前必须先看清 app 到底暴露了哪些控件、``AutomationId`` 是什么，
否则 ``target`` 只能靠猜。本工具做三件事：

1. ``control_tree.txt`` —— 完整的 ``print_control_identifiers()`` 输出，
   写 ``target`` 时直接对着它抄；
2. ``automation_ids.txt`` —— 按控件类型汇总所有**已有** AutomationId 的控件，
   这是判断 objectName 覆盖率、以及给开发提补充清单的直接依据；
3. ``summary.txt`` —— 数量统计与覆盖率，一眼看出 UIA 桥到底通不通。

覆盖率极低（例如整棵树只有个位数 AutomationId）通常意味着 Qt 的 UIA 桥没激活，
先检查 ``QT_ACCESSIBILITY`` 再怀疑用例写法。
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifacts import control_tree_text
from .config import Settings
from .errors import AppLaunchError
from .log import get_logger

__all__ = ["InspectResult", "inspect_app", "format_result"]

_LOG = get_logger("inspect")

# 内容型控件才值得统计 AutomationId；这些容器类型占了树的大半却没有交互价值
_NON_INTERACTIVE_TYPES = frozenset(
    {"Custom", "Group", "Pane", "Window", "ToolBar", "StatusBar", "TitleBar"}
)


@dataclass
class InspectResult:
    """一次探测的结果概要。"""

    out_dir: Path
    window_title: str = ""
    pid: int | None = None
    total: int = 0
    with_auto_id: int = 0
    interactive_total: int = 0
    interactive_with_auto_id: int = 0
    by_type: Counter = field(default_factory=Counter)
    files: list[Path] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """**可交互控件**的 AutomationId 覆盖率。

        只统计可交互控件：整树覆盖率会被大量 Pane/Group 稀释，
        而那些容器有没有 objectName 对写用例毫无影响。
        """
        if not self.interactive_total:
            return 0.0
        return round(self.interactive_with_auto_id * 100.0 / self.interactive_total, 1)


def inspect_app(
    settings: Settings,
    *,
    attach: bool = False,
    pid: int | None = None,
    wait: float = 0.0,
    title: str | None = None,
    depth: int | None = None,
    out_dir: Path | str | None = None,
    keep_open: bool = False,
) -> InspectResult:
    """启动（或连接）应用并导出控件树。

    :param wait: 导出前等待的秒数。留出这段时间手工登录、切到目标界面，
                 才能探测到登录后才存在的控件 —— 这是本工具最常用的姿势。
    :param title: 指定要探测的窗口标题正则，缺省用配置的主窗口。
    :param keep_open: 探测完不关闭应用，便于继续手工查看。
    """
    from .app import AppSession

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(out_dir) if out_dir else settings.report_root / "inspect" / stamp
    out.mkdir(parents=True, exist_ok=True)

    session = AppSession(settings, log_dir=out / "app_logs")
    try:
        if attach:
            session.connect_existing(pid=pid, title=title or settings.app.main_window.title)
        else:
            exe = settings.resolve_app_path(None)
            session.exe_path = exe
            session.kill_leftovers()
            session.launch(exe)

        if wait > 0:
            _LOG.info("等待 %.0f 秒，请在此期间把应用操作到要探测的界面…", wait)
            for remaining in range(int(wait), 0, -5):
                _LOG.info("  还剩 %ds", remaining)
                time.sleep(min(5, remaining))

        root = _resolve_root(session, settings, title)
        result = InspectResult(
            out_dir=out,
            window_title=_safe(root.window_text),
            pid=session.pid,
        )
        _LOG.info("探测窗口：%r", result.window_title)

        result.files.append(_dump_tree(root, out / "control_tree.txt", result.window_title))
        elements = _collect(root, depth)
        result.total = len(elements)
        _classify(result, elements)
        result.files.append(_write_auto_ids(elements, out / "automation_ids.txt"))
        result.files.append(_write_windows(session, out / "windows.txt"))
        result.files.append(_write_summary(result, out / "summary.txt"))
        return result
    finally:
        if not attach and not keep_open:
            try:
                session.close(force=False, timeout=5.0)
                session.kill_leftovers()
            except Exception as exc:  # noqa: BLE001 - 收尾失败不影响探测结果
                _LOG.debug("关闭应用失败：%s", exc)


# ----------------------------------------------------------------------
# 采集
# ----------------------------------------------------------------------
def _resolve_root(session: Any, settings: Settings, title: str | None) -> Any:
    """确定探测的根窗口。"""
    if title:
        spec = session.window(title_re=title)
        try:
            return spec.wait("exists", timeout=settings.scaled(30), retry_interval=0.5)
        except Exception as exc:  # noqa: BLE001
            raise AppLaunchError(
                f"未找到标题匹配 {title!r} 的窗口", detail=str(exc)
            ) from exc
    return session.wait_main_window()


def _collect(root: Any, depth: int | None) -> list[dict[str, Any]]:
    """把整棵树拍平成字典列表。单个控件属性读取失败不影响其余控件。"""
    try:
        wrappers = list(root.descendants(depth=depth))
    except TypeError:
        # depth=None 时部分 pywinauto 版本不接受该关键字
        wrappers = list(root.descendants())
    except Exception as exc:  # noqa: BLE001
        _LOG.error("枚举子控件失败：%s", exc)
        return []

    out: list[dict[str, Any]] = []
    for w in wrappers:
        info = getattr(w, "element_info", None)
        out.append(
            {
                "type": _safe(lambda: getattr(info, "control_type", "") or ""),
                "auto_id": _safe(lambda: getattr(info, "automation_id", "") or ""),
                "name": _safe(lambda: getattr(info, "name", "") or w.window_text() or ""),
                "class_name": _safe(lambda: getattr(info, "class_name", "") or ""),
                "rect": _safe(lambda: str(w.rectangle())),
                "enabled": _safe(lambda: w.is_enabled(), ""),
                "visible": _safe(lambda: w.is_visible(), ""),
            }
        )
    return out


def _classify(result: InspectResult, elements: list[dict[str, Any]]) -> None:
    for el in elements:
        ctype = el["type"] or "<unknown>"
        result.by_type[ctype] += 1
        if el["auto_id"]:
            result.with_auto_id += 1
        if ctype not in _NON_INTERACTIVE_TYPES:
            result.interactive_total += 1
            if el["auto_id"]:
                result.interactive_with_auto_id += 1


def _safe(fn: Any, default: Any = "") -> Any:
    """UIA 属性读取可能因控件已销毁而抛 COM 异常，一律降级为默认值。"""
    try:
        value = fn()
        return value if value is not None else default
    except Exception:  # noqa: BLE001
        return default


# ----------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------
def _dump_tree(root: Any, path: Path, title: str) -> Path:
    """导出控件树快照。

    导出逻辑与失败留证共用 :func:`control_tree_text`（wrapper 上没有
    ``print_control_identifiers``，需要从句柄临时造 spec，细节在那边）。
    """
    header = (
        f"# app 控件树快照\n# 窗口: {title!r}\n"
        f"# 时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        "# 写 target 时直接对着下面的 auto_id / title / control_type 抄\n\n"
    )
    body = control_tree_text(root, max_width=160)
    path.write_text(header + body, encoding="utf-8")
    _LOG.info("控件树已导出：%s", path)
    return path


def _write_auto_ids(elements: list[dict[str, Any]], path: Path) -> Path:
    """导出 AutomationId 清单，按控件类型分组。"""
    lines = [
        "# 已设置 AutomationId（Qt objectName）的控件清单",
        "# 格式: <control_type>  <automation_id>  |  name  |  rect",
        "# 本清单可直接作为 docs/objectname_requirements.md 的「已覆盖」部分",
        "",
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for el in elements:
        if el["auto_id"]:
            grouped.setdefault(el["type"] or "<unknown>", []).append(el)

    if not grouped:
        lines.append("（没有任何控件带 AutomationId —— Qt 的 UIA 桥可能未激活，")
        lines.append("  检查启动环境变量 QT_ACCESSIBILITY=1，详见 docs/spike_report.md）")
    for ctype in sorted(grouped):
        lines.append(f"## {ctype}（{len(grouped[ctype])} 个）")
        for el in sorted(grouped[ctype], key=lambda e: e["auto_id"]):
            lines.append(
                f"  {el['auto_id']:<40} | {el['name'][:40]:<40} | {el['rect']}"
            )
        lines.append("")

    lines.append("")
    lines.append("# 缺少 AutomationId 的可交互控件（建议推动开发补充 objectName）")
    missing = [
        el
        for el in elements
        if not el["auto_id"] and el["type"] not in _NON_INTERACTIVE_TYPES
    ]
    counter: Counter = Counter(el["type"] or "<unknown>" for el in missing)
    for ctype, n in counter.most_common():
        lines.append(f"## {ctype}（{n} 个缺 auto_id）")
        shown = 0
        for el in missing:
            if el["type"] != ctype or not el["name"]:
                continue
            lines.append(f"  name={el['name'][:60]!r}  class={el['class_name']}  {el['rect']}")
            shown += 1
            if shown >= 20:
                lines.append(f"  …（同类型其余 {n - shown} 个省略）")
                break
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    _LOG.info("AutomationId 清单已导出：%s", path)
    return path


def _write_windows(session: Any, path: Path) -> Path:
    """导出当前进程的所有顶层窗口，用于确认窗口别名与标题正则。"""
    lines = ["# 被测进程的顶层窗口", "# 格式: handle  title  |  class_name  |  visible"]
    for w in session.top_windows():
        lines.append(
            f"  {_safe(lambda: w.handle):<10} {_safe(lambda: w.window_text())!r:<50} | "
            f"{_safe(lambda: w.class_name())} | visible={_safe(lambda: w.is_visible())}"
        )
    if len(lines) == 2:
        lines.append("  （无顶层窗口）")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_summary(result: InspectResult, path: Path) -> Path:
    lines = [
        "# app 控件可识别性探测摘要",
        f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"窗口: {result.window_title!r}",
        f"进程: pid={result.pid}",
        "",
        f"控件总数:               {result.total}",
        f"带 AutomationId:        {result.with_auto_id}",
        f"可交互控件数:           {result.interactive_total}",
        f"可交互且带 AutomationId: {result.interactive_with_auto_id}",
        f"可交互控件覆盖率:       {result.coverage}%",
        "",
        "# 按控件类型统计",
    ]
    for ctype, n in result.by_type.most_common():
        lines.append(f"  {ctype:<20} {n}")
    lines += [
        "",
        "# 门禁判定（详见 docs/spike_report.md）",
        "  >= 70%  通过：可按 auto_id 为主键编写用例",
        "  30~70%  部分通过：核心控件可用，自绘区域需 click_offset 兜底",
        "  <  30%  不通过：先排查 QT_ACCESSIBILITY，仍无改善则需评估图像方案",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def format_result(result: InspectResult) -> str:
    """把探测结果渲染成控制台摘要。"""
    lines = [
        f"窗口        : {result.window_title!r} (pid={result.pid})",
        f"控件总数    : {result.total}",
        f"带 AutoId   : {result.with_auto_id}",
        f"可交互控件  : {result.interactive_total}（其中 {result.interactive_with_auto_id} 个有 AutoId）",
        f"覆盖率      : {result.coverage}%",
        f"产物目录    : {result.out_dir}",
    ]
    for f in result.files:
        lines.append(f"  - {f}")
    return "\n".join(lines)
