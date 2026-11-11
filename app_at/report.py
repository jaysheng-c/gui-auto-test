"""测试结果模型与报告输出（JSON + 单文件 HTML）。

``RunResult`` 是唯一事实来源：JSON 直接由它序列化，HTML 也由它渲染，
两者不会出现口径不一致。
"""

from __future__ import annotations

import json
import platform
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifacts import Artifacts
from .log import get_logger

__all__ = [
    "Status",
    "StepResult",
    "CaseResult",
    "SuiteResult",
    "RunResult",
    "write_json_report",
    "render_html_report",
    "write_latest_pointer",
    "build_environment",
]

_LOG = get_logger("report")


class Status:
    """执行状态。用例级与步骤级共用同一套取值。"""

    PASS = "passed"
    FAIL = "failed"
    SKIP = "skipped"
    ERROR = "error"      # 框架/环境错误，区别于用例断言失败
    ALL = (PASS, FAIL, SKIP, ERROR)


@dataclass
class StepResult:
    """一个步骤的执行结果。"""

    index: int = 0
    id: str = ""
    action: str = ""
    desc: str = ""
    phase: str = "steps"          # setup / steps / teardown
    status: str = Status.PASS
    duration: float = 0.0
    message: str = ""
    error: str = ""
    error_type: str = ""
    expected: Any = None
    actual: Any = None
    target: str = ""
    optional: bool = False
    retries: int = 0
    screenshot: str | None = None      # 相对 report 根目录的路径
    screenshot_b64: str | None = None  # embed 模式下内联
    tree: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"[{self.id or self.index}] {self.action}"


@dataclass
class CaseResult:
    """一条用例（含 data_set 的某一轮迭代）的结果。"""

    case_id: str = ""
    name: str = ""
    tags: list[str] = field(default_factory=list)
    priority: str = ""
    source: str = ""
    iteration: int = 1
    status: str = Status.PASS
    duration: float = 0.0
    error: str = ""
    error_type: str = ""
    skip_reason: str = ""
    flags: list[str] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)

    @property
    def title(self) -> str:
        suffix = f" #{self.iteration}" if self.iteration > 1 else ""
        return f"{self.case_id}{suffix} {self.name}".strip()

    @property
    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in Status.ALL}
        for st in self.steps:
            out[st.status] = out.get(st.status, 0) + 1
        return out


@dataclass
class SuiteResult:
    """一个套件的结果。"""

    name: str = ""
    source: str = ""
    status: str = Status.PASS
    duration: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in Status.ALL}
        for c in self.cases:
            out[c.status] = out.get(c.status, 0) + 1
        return out


@dataclass
class RunResult:
    """一次运行的总结果。"""

    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration: float = 0.0
    exit_code: int = 0
    command_line: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    suites: list[SuiteResult] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def cases(self) -> list[CaseResult]:
        return [c for s in self.suites for c in s.cases]

    @property
    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in Status.ALL}
        for c in self.cases:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def pass_rate(self) -> float:
        """通过率。跳过的用例不计入分母 —— 否则一堆 SKIP 会虚高通过率。"""
        counted = self.counts[Status.PASS] + self.counts[Status.FAIL] + self.counts[Status.ERROR]
        return round(self.counts[Status.PASS] * 100.0 / counted, 2) if counted else 0.0

    @property
    def failed_cases(self) -> list[CaseResult]:
        return [c for c in self.cases if c.status in (Status.FAIL, Status.ERROR)]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["summary"] = {
            "total": self.total,
            **self.counts,
            "pass_rate": self.pass_rate,
        }
        return data


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------
def write_json_report(result: RunResult, path: Path | str) -> Path:
    """写出 ``result.json``。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2, default=str)
    _LOG.debug("已写出 JSON 报告 %s", p)
    return p


# ----------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------
def _template_env() -> Any:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    tpl_dir = Path(__file__).resolve().parent / "templates"
    return Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html_report(
    result: RunResult,
    path: Path | str,
    artifacts: Artifacts | None = None,
    *,
    embed: bool = True,
) -> Path:
    """渲染单文件 HTML 报告。

    ``embed=True`` 时把截图 base64 内联，产出的单个 HTML 可以直接发出去，
    不依赖同目录的 screenshots/ 文件夹。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if embed and artifacts is not None:
        _inline_screenshots(result, artifacts)

    env = _template_env()
    template = env.get_template("report.html.j2")
    html = template.render(
        result=result,
        Status=Status,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        embed=embed,
    )
    p.write_text(html, encoding="utf-8")
    _LOG.debug("已写出 HTML 报告 %s", p)
    return p


def _inline_screenshots(result: RunResult, artifacts: Artifacts) -> None:
    for suite in result.suites:
        for case in suite.cases:
            for step in case.steps:
                if step.screenshot and not step.screenshot_b64:
                    step.screenshot_b64 = artifacts.read_b64(artifacts.root / step.screenshot)


def write_latest_pointer(report_root: Path | str, result: RunResult, html_path: Path | None) -> None:
    """在 ``reports/`` 下留一个指向最近一次运行的入口。

    不用符号链接：Windows 上创建 symlink 需要管理员权限或开发者模式，
    直接复制单文件 HTML 更省事，也便于把最近结果单独发出去。
    """
    root = Path(report_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "latest.txt").write_text(
            f"{result.run_id}\n{html_path or ''}\n", encoding="utf-8"
        )
        if html_path and Path(html_path).is_file():
            shutil.copyfile(html_path, root / "latest.html")
    except OSError as exc:  # pragma: no cover
        _LOG.debug("写入 latest 指针失败: %s", exc)


def build_environment(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """采集运行环境信息，写进报告便于复现问题。"""
    import sys

    env: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.node(),
        "processor": platform.processor() or "",
    }
    try:
        import pywinauto

        env["pywinauto"] = pywinauto.__version__
    except Exception:  # noqa: BLE001
        env["pywinauto"] = "unknown"
    try:
        import ctypes

        env["screen"] = "x".join(
            str(v) for v in (ctypes.windll.user32.GetSystemMetrics(0),
                             ctypes.windll.user32.GetSystemMetrics(1))
        )
        # GetProcessDpiAwareness 需要通过指针回传，直传 None 会报错
        dpi_value = ctypes.c_int(0)
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(dpi_value))
        env["dpi_awareness"] = {0: "unaware", 1: "system", 2: "per-monitor"}.get(
            dpi_value.value, str(dpi_value.value)
        )
    except Exception:  # noqa: BLE001 - 环境信息采集失败不影响测试
        pass
    if extra:
        env.update(extra)
    return env
