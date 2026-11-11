"""命令行入口。

用法与 README 保持一致 —— argparse 对 ``--opt=value`` 与 ``--opt value`` 两种写法
都支持，因此文档里的示例可以原样复制::

    python auto_test.py --execute=E:/path/app.exe
    python auto_test.py --execute=... --suite=test_flow_file/suite_smoke.json
    python auto_test.py --execute=... --flow=tc001_launch
    python auto_test.py --execute=... --case=TC_LOGIN_001 --tags=smoke,login
    python auto_test.py --inspect --execute=... --wait=30
    python auto_test.py --validate
    python auto_test.py --list

**退出码**：``0`` 全部通过 / ``1`` 存在未通过用例 / ``2`` 框架或配置错误。
CI 直接依据退出码判定即可，不必解析报告。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Settings
from .errors import ConfigError, AppAtError
from .log import get_logger, setup_logging

__all__ = ["build_parser", "main"]

_LOG = get_logger("cli")

_EPILOG = """\
示例:
  python auto_test.py --execute=E:/app/app.exe
  python auto_test.py --execute=E:/app/app.exe --suite=suite_smoke --repeat=3
  python auto_test.py --execute=E:/app/app.exe --case=TC_MAIN_002 --tags=smoke
  python auto_test.py --inspect --execute=E:/app/app.exe --wait=30
  python auto_test.py --validate
  python auto_test.py --list --list-actions

退出码: 0 全部通过 / 1 存在未通过用例 / 2 框架或配置错误
详细文档: docs/flow_authoring.md
"""


# ----------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto_test.py",
        description="app GUI 自动化测试框架（pywinauto + UIA，用例为 test_flow_file/*.json）",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    g = parser.add_argument_group("运行目标")
    g.add_argument("--execute", metavar="PATH", help="被测应用可执行文件路径，如 E:/app/app.exe")
    g.add_argument("--suite", metavar="NAME", help="套件文件，缺省用配置里的 default_suite")
    g.add_argument("--flow", metavar="NAME", action="append", default=[],
                   help="直接运行指定流程（可重复），此时忽略 --suite")
    g.add_argument("--case", metavar="ID", action="append", default=[],
                   help="只运行指定用例 id（可重复，也可逗号分隔）")
    g.add_argument("--tags", metavar="LIST", action="append", default=[],
                   help="只运行带指定标签的用例，逗号分隔；多个标签之间是「或」关系")
    g.add_argument("paths", nargs="*", metavar="FILE",
                   help="流程文件（等价于 --flow）；配合 --validate / --list 时作为过滤范围")

    g = parser.add_argument_group("运行控制")
    g.add_argument("--repeat", type=int, metavar="N", help="整套流程重复执行的轮数")
    g.add_argument("--continue-on-error", dest="continue_on_error", action="store_true",
                   default=None, help="单条用例失败后继续跑后续用例")
    g.add_argument("--stop-on-error", dest="continue_on_error", action="store_false",
                   help="单条用例失败后立即停止（覆盖套件配置）")
    g.add_argument("--timeout-scale", type=float, metavar="X",
                   help="全局超时放大系数，慢机器上填 1.5 之类，不必逐个改 JSON")
    g.add_argument("--var", metavar="K=V", action="append", default=[],
                   help="注入全局变量（可重复），流程中用 ${K} 引用")
    g.add_argument("--attach", action="store_true",
                   help="连接已运行的实例，不启动也不关闭应用（调试用例时很有用）")
    g.add_argument("--pid", type=int, help="配合 --attach 指定进程号")
    g.add_argument("--allow-script", action="store_true",
                   help="允许 run_py 等逃生口动作（默认禁用）")

    g = parser.add_argument_group("辅助命令（不跑用例）")
    g.add_argument("--validate", action="store_true",
                   help="仅校验流程 JSON，错误精确到字段路径")
    g.add_argument("--list", action="store_true", help="列出套件 / 流程 / 用例 / 标签")
    g.add_argument("--list-actions", action="store_true", help="列出全部可用动作，按分类分组")
    g.add_argument("--inspect", action="store_true", help="导出控件树到 reports/inspect/")
    g.add_argument("--inspect-window", metavar="TITLE_RE", help="--inspect 时指定窗口标题正则")
    g.add_argument("--wait", type=float, metavar="SEC", default=0.0,
                   help="--inspect 导出前的等待秒数，留出时间手工登录并切到目标界面")
    g.add_argument("--keep-open", action="store_true", help="--inspect 结束后不关闭应用")
    g.add_argument("--depth", type=int, help="--inspect 控件树导出深度上限")

    g = parser.add_argument_group("输出")
    g.add_argument("--report-dir", metavar="DIR", help="报告输出根目录，缺省 reports")
    g.add_argument("--log-level", metavar="LEVEL", help="DEBUG / INFO / WARNING / ERROR")
    g.add_argument("--no-html", action="store_true", help="不生成 HTML 报告")
    g.add_argument("--no-json", action="store_true", help="不生成 result.json")
    g.add_argument("--no-embed", action="store_true", help="截图不内嵌，HTML 体积更小但依赖同目录文件")
    g.add_argument("--config", metavar="PATH", help=f"配置文件，缺省 {DEFAULT_CONFIG_PATH}")
    g.add_argument("--version", action="version", version=f"app_at {__version__}")
    return parser


def _split_csv(values: Sequence[str]) -> list[str]:
    """``--tags=a,b --tags=c`` 与 ``--tags=a --tags=b`` 都支持。"""
    out: list[str] = []
    for raw in values:
        out.extend(item.strip() for item in str(raw).split(",") if item.strip())
    return out


def _parse_vars(pairs: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"--var 需要 K=V 形式: {pair!r}")
        key, _, value = pair.partition("=")
        out[key.strip()] = value
    return out


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """把 CLI 参数翻译成 Settings 的点号覆盖项，None 值由 load 自动忽略。"""
    return {
        "app.path": args.execute,
        "run.repeat": args.repeat,
        "run.timeout_scale": args.timeout_scale,
        "run.allow_script": True if args.allow_script else None,
        "report.dir": args.report_dir,
        "log.level": args.log_level,
        "report.html": False if args.no_html else None,
        "report.json": False if args.no_json else None,
        "report.embed_screenshots": False if args.no_embed else None,
    }


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 先只开控制台日志：文件日志路径依赖 run_id，由 Runner 创建产物目录后再挂
    setup_logging(args.log_level or "INFO")

    # DPI 感知必须在任何 UIA 调用之前设置。auto_test.py 已经设过一次，
    # 这里再设是为了让 `python -m app_at.cli` 这种直接调用也正确 —— 重复设置无副作用。
    if args.execute or args.inspect or args.attach:
        from .app import set_dpi_awareness

        mode = set_dpi_awareness()
        _LOG.debug("进程 DPI 感知: %s", mode)

    try:
        settings = Settings.load(args.config, _build_overrides(args))
    except AppAtError as exc:
        _die(exc)
        return exc.exit_code
    # --var 是追加而不是替换：配置文件里的全局变量仍然有效
    settings.vars.update(_parse_vars(args.var))

    try:
        if args.validate:
            return cmd_validate(settings, args)
        if args.list or args.list_actions:
            return cmd_list(settings, args)
        if args.inspect:
            return cmd_inspect(settings, args)
        return cmd_run(settings, args)
    except KeyboardInterrupt:
        _LOG.warning("已被用户中断")
        return 2
    except AppAtError as exc:
        _die(exc)
        return exc.exit_code


def _die(exc: AppAtError) -> None:
    """把框架异常渲染成人能直接照做的提示。"""
    _LOG.error("%s", exc.message or exc)
    if exc.detail:
        for line in str(exc.detail).splitlines():
            _LOG.error("  %s", line)


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------
def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    from .runner import RunOptions, Runner

    flows = list(args.flow) + list(args.paths)
    if not flows and not args.suite and not args.execute:
        # 既没指定目标也没指定应用：这不是"跑默认套件"，而是用法错误
        raise ConfigError(
            "未指定被测应用",
            detail="请用 --execute=E:/path/app.exe；仅校验流程可用 --validate，查看清单可用 --list",
        )

    options = RunOptions(
        suite=args.suite,
        flows=flows,
        cases=_split_csv(args.case),
        tags=_split_csv(args.tags),
        repeat=args.repeat or 0,
        attach=args.attach,
        pid=args.pid,
        continue_on_error=args.continue_on_error,
        command_line=" ".join(sys.argv),
    )
    runner = Runner(settings, options)
    result = runner.run()
    paths = runner.write_reports()
    _print_summary(result, paths)
    return runner.exit_code()


def _print_summary(result: Any, paths: dict[str, Path]) -> None:
    counts = result.counts
    _LOG.info("")
    _LOG.info("=" * 72)
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="运行汇总", show_header=False, box=None, pad_edge=False)
        table.add_column(justify="right", style="bold")
        table.add_column()
        rows = [
            ("用例总数", str(result.total), ""),
            ("通过", str(counts["passed"]), "green" if counts["passed"] else "dim"),
            ("失败", str(counts["failed"]), "red" if counts["failed"] else "dim"),
            ("框架错误", str(counts["error"]), "magenta" if counts["error"] else "dim"),
            ("跳过", str(counts["skipped"]), "yellow" if counts["skipped"] else "dim"),
            ("通过率", f"{result.pass_rate}%", ""),
            ("耗时", f"{result.duration:.1f}s", ""),
        ]
        for label, value, style in rows:
            table.add_row(label, f"[{style or 'default'}]{value}[/{style or 'default'}]")
        Console().print(table)
    except Exception:  # noqa: BLE001 - rich 不可用时降级为纯文本
        _LOG.info(
            "总计 %d | 通过 %d | 失败 %d | 错误 %d | 跳过 %d | 通过率 %s%% | 耗时 %.1fs",
            result.total, counts["passed"], counts["failed"], counts["error"],
            counts["skipped"], result.pass_rate, result.duration,
        )

    if result.flags:
        _LOG.warning("运行标记: %s（这些失败需先排除环境因素再判定为产品缺陷）", ", ".join(result.flags))
    for case in result.failed_cases[:15]:
        first = next((s for s in case.steps if s.status in ("failed", "error")), None)
        where = f"{first.label}: {first.error}" if first else (case.error or "无错误信息")
        # 用 GBK 能编出的 × 而不是 ✗：中文 Windows 控制台默认 GBK，
        # 编不出的字符会让整条日志输出失败（见 log._harden_stdio）
        _LOG.error("× %s — %s", case.title, str(where).splitlines()[0][:200])
    if len(result.failed_cases) > 15:
        _LOG.error("…另有 %d 条未通过，详见报告", len(result.failed_cases) - 15)

    for kind, path in paths.items():
        _LOG.info("%s 报告: %s", "HTML" if kind == "html" else "JSON", path)
    _LOG.info("产物目录: %s", paths.get("html", paths.get("json", Path("."))).parent)
    _LOG.info("=" * 72)


# ----------------------------------------------------------------------
# validate
# ----------------------------------------------------------------------
def cmd_validate(settings: Settings, args: argparse.Namespace) -> int:
    from .flow_loader import validate_files

    targets = [settings.resolve_flow_file(p) for p in (args.paths or [])] or None
    issues, checked = validate_files(settings, targets)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity != "error"]

    for issue in errors:
        _LOG.error("%s", issue)
    for issue in warnings:
        _LOG.warning("%s", issue)

    _LOG.info("")
    if errors:
        _LOG.error("校验未通过：%d 个文件，%d 处错误，%d 处警告", len(checked), len(errors), len(warnings))
        return 2
    _LOG.info("校验通过：%d 个文件，%d 处警告", len(checked), len(warnings))
    return 0


# ----------------------------------------------------------------------
# list
# ----------------------------------------------------------------------
def cmd_list(settings: Settings, args: argparse.Namespace) -> int:
    from .runner import collect_inventory

    inv = collect_inventory(settings)
    _LOG.info("流程目录: %s", settings.flow_dir)

    _LOG.info("")
    _LOG.info("套件（%d）", len(inv["suites"]))
    for s in inv["suites"]:
        _LOG.info("  %-28s %s", s["file"], s["name"] or s["suite"])
        for f in s["flows"]:
            _LOG.info("      └─ %s", f)

    _LOG.info("")
    cases = [f for f in inv["flows"] if not f["sub"]]
    subs = [f for f in inv["flows"] if f["sub"]]

    _LOG.info("用例（%d）", len(cases))
    for f in cases:
        _log_flow_line(f)

    if subs:
        _LOG.info("")
        _LOG.info("可复用子流程（%d）—— 由 include 引入，不要列进套件的 flows", len(subs))
        for f in subs:
            _log_flow_line(f)

    _LOG.info("")
    _LOG.info("标签（%d）", len(inv["tags"]))
    _LOG.info("  %s", ", ".join(f"{t}({n})" for t, n in inv["tags"].items()) or "（无）")

    if args.list_actions:
        _print_actions(inv["actions"])

    if inv["errors"]:
        _LOG.error("")
        _LOG.error("以下文件加载失败：")
        for e in inv["errors"]:
            _LOG.error("  %s", e)
        return 2
    return 0


def _log_flow_line(f: dict[str, Any]) -> None:
    """输出一行用例摘要，并把值得注意的状态挂在末尾。"""
    marks = []
    if not f["enabled"]:
        marks.append("已禁用")
    if not f.get("in_suite"):
        marks.append("未入套件")
    suffix = f"  [{'/'.join(marks)}]" if marks else ""
    _LOG.info(
        "  %-18s %-4s %3d 步  tags=%-22s %s%s",
        f["id"], f["priority"], f["steps"], ",".join(f["tags"]) or "-", f["name"], suffix,
    )


def _print_actions(categories: dict[str, list[str]]) -> None:
    from .actions import ACTION_REGISTRY

    total = sum(len(v) for v in categories.values())
    _LOG.info("")
    _LOG.info("可用动作（%d）", total)
    order = ["window", "mouse", "keyboard", "control", "wait", "assert",
             "dialog", "flow", "other"]
    for category in order + [c for c in sorted(categories) if c not in order]:
        names = categories.get(category)
        if not names:
            continue
        _LOG.info("  [%s]", category)
        for name in names:
            desc = ACTION_REGISTRY[name].description or ""
            _LOG.info("    %-22s %s", name, desc)


# ----------------------------------------------------------------------
# inspect
# ----------------------------------------------------------------------
def cmd_inspect(settings: Settings, args: argparse.Namespace) -> int:
    from .inspect_tool import format_result, inspect_app

    if not args.attach and not args.execute and not settings.app.path:
        raise ConfigError(
            "--inspect 需要指定被测应用",
            detail="请加 --execute=E:/path/app.exe，或用 --attach 连接已运行的实例",
        )
    result = inspect_app(
        settings,
        attach=args.attach,
        pid=args.pid,
        wait=args.wait,
        title=args.inspect_window,
        depth=args.depth,
        keep_open=args.keep_open,
    )
    _LOG.info("")
    _LOG.info("=" * 72)
    for line in format_result(result).splitlines():
        _LOG.info("%s", line)
    _LOG.info("可交互控件 AutomationId 覆盖率 %.1f%%（门禁线 70%%，详见 docs/spike_report.md）",
              result.coverage)
    _LOG.info("=" * 72)
    return 0
