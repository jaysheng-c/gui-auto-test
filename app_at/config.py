"""配置加载与合并。

三层配置优先级（高 → 低）：

1. CLI 参数（``--execute`` / ``--timeout-scale`` / ``--log-level`` ...）
2. ``--config`` 指定的 YAML，缺省为 ``config/default.yaml``
3. 本模块 dataclass 中写死的默认值

所有相对路径统一基于 ``project_root``（即 ``auto_test.py`` 所在目录）解析，
保证从任意工作目录调用都能得到一致结果。
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

__all__ = [
    "AppConfig",
    "RunConfig",
    "ReportConfig",
    "LogConfig",
    "VisionConfig",
    "Settings",
    "flatten_mapping",
    "PROJECT_ROOT",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_FLOW_DIR",
]

# 框架包所在目录的上一级 == 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
DEFAULT_FLOW_DIR = PROJECT_ROOT / "test_flow_file"

# 主窗口别名，流程 JSON 中 target.window 的默认值
MAIN_WINDOW_ALIAS = "main"

_VALID_SCREENSHOT_MODES = ("always", "on_fail", "never")
_VALID_CLICK_MODES = ("mouse", "invoke")


@dataclass
class MainWindowConfig:
    """主窗口识别条件。

    Qt 窗口的 ``class_name`` 形如 ``Qt643QWindowIcon``，随 Qt 小版本变化，
    **不可依赖**；标题也可能被同进程的弹层窗口撞名（app 的下拉弹层标题
    同样是 ``app``），只用标题会命中多个顶层窗口而报
    ``ElementAmbiguousError``。因此 ``auto_id``（Qt objectName →
    AutomationId）是最稳的主键，弹层没有 objectName，靠它即可唯一确定主窗口。
    """

    title: str | None = None
    title_re: str | None = None
    auto_id: str | None = None
    class_name: str | None = None
    timeout: float = 60.0


@dataclass
class AppConfig:
    """被测应用相关配置。"""

    path: str | None = None
    args: list[str] = field(default_factory=list)
    workdir: str | None = None
    backend: str = "uia"
    startup_timeout: float = 60.0
    # 启动前注入的环境变量。QT_ACCESSIBILITY=1 用于确保 Qt 的 UIA 桥被激活。
    env: dict[str, str] = field(default_factory=dict)
    # 进程名，用于 teardown 时清理残留进程；为空则由 path 推导
    process_name: str | None = None
    main_window: MainWindowConfig = field(default_factory=MainWindowConfig)


@dataclass
class RunConfig:
    """执行与容错策略。"""

    default_timeout: float = 15.0
    poll_interval: float = 0.5
    retries: int = 1
    # 全局超时放大系数，慢机器上无需逐个修改 JSON
    timeout_scale: float = 1.0
    continue_on_error: bool = False
    screenshot: str = "on_fail"
    dump_tree_on_fail: bool = True
    foreground_check: bool = True
    click_by: str = "mouse"
    repeat: int = 1
    # 控件树搜索深度上限，避免全树深搜导致超时
    search_depth: int | None = None
    allow_script: bool = False


@dataclass
class ReportConfig:
    """报告输出配置。"""

    dir: str = "reports"
    html: bool = True
    json: bool = True
    # 截图 base64 内嵌，产出单文件 HTML 便于直接分发
    embed_screenshots: bool = True


@dataclass
class LogConfig:
    """日志配置。"""

    level: str = "INFO"
    file: bool = True


@dataclass
class VisionConfig:
    """图像兜底配置。本期为空入口，仅占位。"""

    enabled: bool = False
    image_dir: str = "resources/images"
    threshold: float = 0.85


@dataclass
class Settings:
    """一次运行的完整配置。"""

    app: AppConfig = field(default_factory=AppConfig)
    run: RunConfig = field(default_factory=RunConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    log: LogConfig = field(default_factory=LogConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    # 全局变量，可被 suite.vars / flow.vars / data_set 逐层覆盖
    vars: dict[str, Any] = field(default_factory=dict)
    # 默认套件文件（相对 flow_dir 或绝对路径）
    default_suite: str = "suite_smoke.json"

    project_root: Path = field(default=PROJECT_ROOT, repr=False)
    flow_dir: Path = field(default=DEFAULT_FLOW_DIR, repr=False)

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    def scaled(self, seconds: float | None) -> float:
        """按 ``timeout_scale`` 放大超时值，None 时返回全局默认超时。"""
        base = self.run.default_timeout if seconds is None else float(seconds)
        return max(0.0, base * self.run.timeout_scale)

    @property
    def report_root(self) -> Path:
        return self.resolve(self.report.dir)

    def resolve(self, path: str | Path | None) -> Path:
        """把相对路径解析为基于 ``project_root`` 的绝对路径。"""
        if path is None:
            return self.project_root
        p = Path(str(path)).expanduser()
        return p if p.is_absolute() else (self.project_root / p)

    def resolve_flow_file(self, name: str | Path) -> Path:
        """把流程/套件名解析为实际文件路径。

        支持三种写法：绝对路径、相对项目根的路径、以及仅文件名
        （在 ``flow_dir`` 下查找，``.json`` 后缀可省略）。
        """
        raw = str(name).strip()
        if not raw:
            raise ConfigError("流程文件名为空")
        p = Path(raw).expanduser()
        candidates: list[Path] = []
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(self.project_root / p)
            candidates.append(self.flow_dir / p)
            if p.suffix.lower() != ".json":
                candidates.append(self.flow_dir / f"{p.name}.json")
                candidates.append(self.project_root / f"{p.name}.json")
        for c in candidates:
            if c.is_file():
                return c.resolve()
        raise ConfigError(
            f"找不到流程文件: {raw}",
            detail="; 已尝试: " + ", ".join(str(c) for c in candidates),
        )

    def validate(self) -> None:
        """校验配置自洽性，非法时抛 ``ConfigError``。"""
        if self.app.backend not in ("uia", "win32"):
            raise ConfigError(f"app.backend 非法: {self.app.backend}", detail="只支持 uia / win32")
        if self.run.screenshot not in _VALID_SCREENSHOT_MODES:
            raise ConfigError(
                f"run.screenshot 非法: {self.run.screenshot}",
                detail=f"可选值 {_VALID_SCREENSHOT_MODES}",
            )
        if self.run.click_by not in _VALID_CLICK_MODES:
            raise ConfigError(
                f"run.click_by 非法: {self.run.click_by}",
                detail=f"可选值 {_VALID_CLICK_MODES}；Qt 的 UIA Pattern 支持不完整，推荐 mouse",
            )
        if self.run.timeout_scale <= 0:
            raise ConfigError("run.timeout_scale 必须大于 0")
        if self.run.repeat < 1:
            raise ConfigError("run.repeat 必须 >= 1")
        mw = self.app.main_window
        if not mw.title and not mw.title_re and not mw.auto_id:
            raise ConfigError(
                "未配置主窗口识别条件",
                detail="请在 config/default.yaml 或套件的 app.main_window 中设置 title / title_re / auto_id",
            )

    def apply_overrides(self, data: dict[str, Any] | None, *, prefix: str = "") -> None:
        """把套件里的嵌套覆盖应用到自身（就地修改）。

        套件 JSON 写的是嵌套结构（``{"main_window": {"title_re": ...}}``），
        而 dataclass 字段不能直接塞 dict，因此先展平成点号路径再逐项赋值。
        字段名拼写错误会由 :func:`_set_dotted` 报 ``ConfigError``，而不是静默忽略 ——
        套件里写错的配置比没写更危险。
        """
        for dotted, value in flatten_mapping(data or {}, prefix).items():
            _set_dotted(self, dotted, value)

    def resolve_app_path(self, cli_execute: str | None) -> Path:
        """确定被测应用可执行文件路径。

        优先级：``--execute`` > ``config.app.path`` > 环境变量 ``app_EXE``。
        """
        raw = cli_execute or self.app.path or os.environ.get("app_EXE")
        if not raw:
            raise ConfigError(
                "未指定被测应用路径",
                detail="请使用 --execute=E:/path/to/app.exe，或在配置中设置 app.path",
            )
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if not p.exists():
            raise ConfigError(f"被测应用不存在: {p}")
        if not p.is_file():
            raise ConfigError(f"被测应用路径不是文件: {p}")
        return p

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Settings:
        """加载配置。

        :param config_path: YAML 路径，None 表示使用 ``config/default.yaml``；
                            文件不存在时仅用内置默认值（不报错，便于最小化运行）。
        :param overrides: CLI 覆盖项，形如 ``{"app.path": ..., "run.retries": 2}``，
                          None 值会被忽略。
        """
        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover
                raise ConfigError("缺少 PyYAML 依赖", detail="pip install -r requirements.txt") from exc
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = yaml.safe_load(fh) or {}
            except Exception as exc:
                raise ConfigError(f"配置文件解析失败: {path}", detail=str(exc)) from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"配置文件顶层必须是对象: {path}")
            data = loaded
        elif config_path:
            # 用户显式指定的配置文件不存在属于错误；默认配置缺失则静默降级
            raise ConfigError(f"配置文件不存在: {path}")

        settings = _from_dict(cls, data)
        settings.project_root = PROJECT_ROOT
        settings.flow_dir = settings.resolve("test_flow_file")

        if overrides:
            for dotted, value in overrides.items():
                if value is None:
                    continue
                _set_dotted(settings, dotted, value)

        settings.report.dir = str(settings.resolve(settings.report.dir))
        settings.vision.image_dir = str(settings.resolve(settings.vision.image_dir))
        settings.validate()
        return settings


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _from_dict(cls: type, data: Any) -> Any:
    """递归地把 dict 填充进 dataclass，忽略未知字段。

    忽略而非报错，是为了让旧配置文件在新增字段后仍可继续使用；
    字段名拼写错误则由 ``--validate-config`` 与 schema 校验兜住。
    """
    if not is_dataclass(cls):
        return data
    if not isinstance(data, dict):
        data = {}
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = f.type
        # 嵌套 dataclass：按类型名在全局命名空间里找
        nested = _nested_dataclass(ftype)
        if nested is not None and isinstance(value, dict):
            kwargs[f.name] = _from_dict(nested, value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def _nested_dataclass(type_hint: Any) -> type | None:
    """从字段类型注解中解析出嵌套 dataclass 类型（不含泛型容器）。"""
    if isinstance(type_hint, str):
        name = type_hint.split("[")[0].split(".")[-1].strip()
        candidate = globals().get(name)
        return candidate if is_dataclass(candidate) else None
    return type_hint if is_dataclass(type_hint) else None


def flatten_mapping(data: Any, prefix: str = "") -> dict[str, Any]:
    """把嵌套 dict 展平成 ``{"a.b.c": value}`` 形式。

    只递归 **dict**：list（如 ``app.args``）与标量一律当叶子值，
    否则 ``args: ["--x"]`` 会被拆成 ``args.0`` 这种无法赋值的伪路径。
    空 dict 不产出任何项（等价于“不覆盖”）。
    """
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        if prefix:
            out[prefix] = data
        return out
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            out.update(flatten_mapping(value, dotted))
        else:
            out[dotted] = value
    return out


def _set_dotted(obj: Any, dotted: str, value: Any) -> None:
    """按 ``a.b.c`` 形式设置嵌套字段。

    中间节点既可能是 dataclass（``app.main_window``），也可能是普通 dict
    （``app.env``）。后者必须按 key 走而不是按属性走 —— 用 ``hasattr`` 去判
    dict 的话，``app.env.QT_ACCESSIBILITY`` 会被当成「配置项不存在」直接报错，
    而它恰恰是套件 ``app`` 段最常用的覆盖项之一（也是激活 Qt UIA 桥的关键项）。

    dict 是**逐 key 合并**而不是整体替换：套件只声明 ``QT_ACCESSIBILITY`` 时，
    ``default.yaml`` 里的其余环境变量应当保留。
    """
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = _descend(cur, part, dotted)
    leaf = parts[-1]
    if isinstance(cur, MutableMapping):
        cur[leaf] = value
        return
    if not hasattr(cur, leaf):
        raise ConfigError(f"配置项不存在: {dotted}")
    setattr(cur, leaf, value)


def _descend(cur: Any, part: str, dotted: str) -> Any:
    """向下走一层，dataclass 按属性、dict 按 key。

    dict 里缺这一层时**自动补一个空 dict** 而不是报错：套件完全可以在
    ``app.env`` 里声明 ``default.yaml`` 没写过的变量，这是合法用法。
    dataclass 仍然严格报错，因为字段名写错必须被发现。
    """
    if isinstance(cur, MutableMapping):
        child = cur.get(part)
        if not isinstance(child, MutableMapping):
            child = {}
            cur[part] = child
        return child
    if not hasattr(cur, part):
        raise ConfigError(f"配置项不存在: {dotted}", detail=f"缺少 {part}")
    return getattr(cur, part)
