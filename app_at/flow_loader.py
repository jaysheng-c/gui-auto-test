"""流程文件加载与校验。

职责：读 JSON → schema 校验（错误精确到字段路径）→ 构造 :class:`Flow` /
:class:`Suite` 模型 → 展开套件引用的流程文件。

``include`` 子流程**不在这里展开**，而是在运行时由动作执行：这样才能拿到调用方
的变量作用域，也便于对递归深度做保护。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .errors import FlowFormatError
from .flow import Flow, Step, Suite
from .log import get_logger

__all__ = [
    "ValidationIssue",
    "load_json",
    "load_schema",
    "validate_data",
    "load_flow",
    "load_suite",
    "load_suite_flows",
    "discover_flow_files",
    "validate_files",
    "SCHEMA_FILENAME",
]

_LOG = get_logger("flow_loader")

SCHEMA_FILENAME = "flow.schema.json"

# 套件文件名约定：以 suite 开头，或 JSON 内含 "flows" 字段
_SUITE_NAME_PREFIXES = ("suite",)


@dataclass
class ValidationIssue:
    """一条校验问题，供 ``--validate`` 汇总输出。"""

    file: Path
    path: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        loc = f" -> {self.path}" if self.path and self.path != "$" else ""
        return f"[{self.severity.upper()}] {self.file}{loc}: {self.message}"


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------
def load_json(path: Path | str) -> Any:
    """读 JSON 文件，失败时抛带路径的 ``FlowFormatError``。"""
    p = Path(path)
    try:
        # encoding 必须显式指定：中文 Windows 默认 GBK，含中文 desc 的流程文件会解码失败
        with open(p, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise FlowFormatError(f"流程文件不存在: {p}") from exc
    except json.JSONDecodeError as exc:
        raise FlowFormatError(
            f"JSON 语法错误: {p}", detail=f"第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise FlowFormatError(
            f"文件不是合法 UTF-8: {p}",
            detail=f"{exc}；请另存为 UTF-8（含中文 desc 时尤其注意）",
        ) from exc


def load_schema(settings: Settings) -> dict[str, Any] | None:
    """加载 JSON Schema；文件缺失时返回 None（跳过 schema 校验，不阻断运行）。"""
    path = settings.flow_dir / SCHEMA_FILENAME
    if not path.is_file():
        _LOG.debug("未找到 schema 文件 %s，跳过 schema 校验", path)
        return None
    data = load_json(path)
    if not isinstance(data, dict):
        raise FlowFormatError(f"schema 文件顶层必须是对象: {path}")
    return data


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def validate_data(data: Any, schema: dict[str, Any] | None) -> list[ValidationIssue]:
    """按 schema 校验数据，返回问题列表（不做异常抛出，便于批量汇总）。"""
    if schema is None:
        return []
    try:
        import jsonschema
    except ImportError:  # pragma: no cover
        _LOG.warning("未安装 jsonschema，跳过 schema 校验")
        return []

    validator_cls = jsonschema.validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except jsonschema.SchemaError as exc:
        return [ValidationIssue(Path("<schema>"), "", f"schema 自身非法: {exc.message}")]

    validator = validator_cls(schema)
    # 顶层是 oneOf(套件|流程)，直接校验的话所有子错误会被压成一条
    # 「不是恰好一个 oneOf 分支成立」，字段路径也丢在根上 —— 用户只能改一条跑一次。
    # 因此先判定这份数据到底是套件还是流程，再对着那一个分支校验，
    # 一次就能报出全部字段级错误。$ref 仍按根 schema 解析，
    # 所以分支里的 ``#/$defs/step`` 之类引用不受影响。
    branch = _select_branch(data, schema)
    raw_errors = (
        validator.iter_errors(data, _schema=branch)
        if branch is not None
        else validator.iter_errors(data)
    )

    issues: list[ValidationIssue] = []
    for err in sorted(raw_errors, key=_error_sort_key):
        leaf = _deepest_error(err)
        issues.append(
            ValidationIssue(
                file=Path("<memory>"),
                path=_format_path(leaf.absolute_path),
                message=leaf.message,
            )
        )
    return issues


def _select_branch(data: Any, schema: dict[str, Any]) -> dict[str, Any] | None:
    """在顶层 ``oneOf`` 的分支里挑出这份数据实际对应的那一个。

    判定规则与 :func:`_is_suite_data` 的前两条一致（那里还有按文件名兜底的
    第三条，但校验阶段面对的常常只是内存数据，不依赖文件名更稳）。
    认不出来、或 schema 不是预期的 ``oneOf($defs/suite, $defs/flow)`` 形状时
    返回 ``None``，退回原始的 oneOf 校验 —— 宁可得一条笼统的报错，
    也不能拿错的分支去校，那会报出一堆根本不存在的问题。
    """
    if not isinstance(data, dict) or not isinstance(schema.get("oneOf"), list):
        return None
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return None
    names = [
        name
        for branch in schema["oneOf"]
        if isinstance((name := _ref_def_name(branch, defs)), str)
    ]
    suite_name = next((n for n in names if "suite" in n), None)
    flow_name = next((n for n in names if "flow" in n), None)
    if suite_name is None or flow_name is None:
        return None

    if "flows" in data or "suite" in data:
        return defs[suite_name]
    if any(key in data for key in ("steps", "id", "setup", "tags")):
        return defs[flow_name]
    return None


def _ref_def_name(branch: Any, defs: dict[str, Any]) -> str | None:
    """把 ``{"$ref": "#/$defs/flow"}`` 解析成 ``"flow"``（仅当它确实存在时）。"""
    ref = branch.get("$ref") if isinstance(branch, dict) else None
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return None
    name = ref.rsplit("/", 1)[-1]
    return name if name in defs else None


def _error_sort_key(err: Any) -> tuple[str, ...]:
    """按字段路径排序，让同一区域的错误相邻。

    路径里混着 str（字段名）与 int（数组下标），直接拿 deque 当排序键会抛
    ``TypeError``；下标补零后转字符串，既能比较又能让 ``steps[2]`` 排在
    ``steps[10]`` 前面。
    """
    return tuple(
        f"{part:08d}" if isinstance(part, int) else str(part)
        for part in _deepest_error(err).absolute_path
    )


def _format_path(path: Iterable[Any]) -> str:
    """把 jsonschema 的路径 deque 渲染成 ``steps[1].target.auto_id`` 形式。"""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "$"


def _deepest_error(err: Any) -> Any:
    """顺着 ``context`` 走到最接近叶子的那条子错误。

    ``oneOf`` / ``anyOf`` 失败时原始 message 会罗列所有分支的子错误，非常冗长，
    而且外层错误的 ``absolute_path`` 停在根上 —— 只取 message 不取路径，
    报出来就是「有个字段错了但不告诉你是哪个」。因此 message 与路径
    必须成对地从同一个叶子错误上取。
    """
    best = err
    while best.context:
        best = max(best.context, key=lambda e: len(list(e.absolute_path)))
    return best


# ----------------------------------------------------------------------
# 模型构造
# ----------------------------------------------------------------------
def _is_suite_data(data: dict[str, Any], path: Path) -> bool:
    if "flows" in data or "suite" in data:
        return True
    if "steps" in data or "id" in data:
        return False
    return path.name.lower().startswith(_SUITE_NAME_PREFIXES)


def load_flow(path: Path | str, settings: Settings, *, schema: dict | None = None) -> Flow:
    """加载单个流程文件。"""
    p = Path(path)
    data = load_json(p)
    if not isinstance(data, dict):
        raise FlowFormatError(f"流程文件顶层必须是对象: {p}")

    issues = [i for i in validate_data(data, schema) if i.severity == "error"]
    if issues:
        raise FlowFormatError(
            f"流程文件 schema 校验失败: {p.name}",
            detail="; ".join(f"{i.path}: {i.message}" for i in issues[:10]),
        )

    flow_id = str(data.get("id") or p.stem)
    steps_where = f"{p.name}"
    flow = Flow(
        id=flow_id,
        name=str(data.get("name") or ""),
        tags=[str(t) for t in (data.get("tags") or [])],
        priority=str(data.get("priority") or "P2"),
        timeout=data.get("timeout"),
        vars=dict(data.get("vars") or {}),
        data_set=_normalize_data_set(data.get("data_set"), p),
        setup=_parse_step_list(data.get("setup"), f"{steps_where}.setup", p.name),
        steps=_parse_step_list(data.get("steps"), f"{steps_where}.steps", p.name),
        teardown=_parse_step_list(data.get("teardown"), f"{steps_where}.teardown", p.name),
        source=p.resolve(),
        enabled=bool(data.get("enabled", True)),
        skip_reason=str(data.get("skip_reason") or ""),
    )
    if not flow.steps and not flow.setup:
        _LOG.warning("流程 %s 没有任何 steps，执行时将直接判定为通过", flow.id)
    return flow


def _normalize_data_set(raw: Any, path: Path) -> list[dict[str, Any]]:
    """``data_set`` 必须是对象数组，每个对象是一轮迭代的变量。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FlowFormatError(f"{path.name}.data_set 必须是数组")
    out: list[dict[str, Any]] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise FlowFormatError(
                f"{path.name}.data_set[{i}] 必须是对象", detail=f"实际类型 {type(row).__name__}"
            )
        out.append(row)
    return out


def _parse_step_list(raw: Any, where: str, source: str) -> list[Step]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FlowFormatError(f"{where} 必须是数组")
    return [Step.parse(s, where=f"{where}[{i}]", source=source) for i, s in enumerate(raw)]


def load_suite(path: Path | str, settings: Settings, *, schema: dict | None = None) -> Suite:
    """加载套件文件。"""
    p = Path(path)
    data = load_json(p)
    if not isinstance(data, dict):
        raise FlowFormatError(f"套件文件顶层必须是对象: {p}")

    issues = [i for i in validate_data(data, schema) if i.severity == "error"]
    if issues:
        raise FlowFormatError(
            f"套件文件 schema 校验失败: {p.name}",
            detail="; ".join(f"{i.path}: {i.message}" for i in issues[:10]),
        )

    flows_raw = data.get("flows") or []
    if not isinstance(flows_raw, list):
        raise FlowFormatError(f"{p.name}.flows 必须是数组")

    return Suite(
        suite=str(data.get("suite") or p.stem),
        name=str(data.get("name") or ""),
        vars=dict(data.get("vars") or {}),
        flows=[str(f) for f in flows_raw],
        app_overrides=dict(data.get("app") or {}),
        settings_overrides=dict(data.get("settings") or {}),
        source=p.resolve(),
    )


def load_suite_flows(suite: Suite, settings: Settings, *, schema: dict | None = None) -> list[Flow]:
    """按套件声明顺序加载其引用的全部流程。"""
    if not suite.flows:
        raise FlowFormatError(
            f"套件 {suite.suite} 未引用任何流程",
            detail=f"请在 {suite.source} 的 flows 数组中列出流程文件",
        )
    flows: list[Flow] = []
    seen: set[Path] = set()
    for name in suite.flows:
        path = _resolve_suite_flow(suite, name, settings)
        if path in seen:
            _LOG.warning("套件 %s 重复引用了 %s，已跳过第二次", suite.suite, path.name)
            continue
        seen.add(path)
        flows.append(load_flow(path, settings, schema=schema))
    return flows


def _resolve_suite_flow(suite: Suite, name: str, settings: Settings) -> Path:
    """解析套件里的流程引用，**套件自身目录优先**，再回退到全局 ``flow_dir``。

    必须与 :func:`_semantic_checks` 的解析口径一致（那里用的是
    ``_Settings_for_probe``，同样以套件目录为基准），否则会出现
    「``--validate`` 通过但运行时找不到文件」这种最难查的不一致。
    """
    raw = str(name).strip()
    if raw and suite.source:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            cand = suite.source.parent / p
            if cand.is_file():
                return cand.resolve()
            if cand.suffix.lower() != ".json" and cand.with_suffix(".json").is_file():
                return cand.with_suffix(".json").resolve()
    return settings.resolve_flow_file(raw)


def discover_flow_files(settings: Settings) -> list[Path]:
    """列出 ``test_flow_file`` 下所有流程/套件 JSON（不含 schema）。

    ``_`` 开头的文件名被跳过，给「写到一半的草稿」留个位置：临时文件不会被
    ``--validate`` / ``--list`` 扰进来，也不会被当成未入套件的用例报出来。
    反过来：**正式用例不要以 ``_`` 开头**，否则会被静默忽略。
    子目录会递归扫描（``rglob``），流程可以按模块分目录存放。
    """
    if not settings.flow_dir.is_dir():
        return []
    return sorted(
        p
        for p in settings.flow_dir.rglob("*.json")
        if p.name != SCHEMA_FILENAME and not p.name.startswith("_")
    )


def validate_files(
    settings: Settings, files: Iterable[Path] | None = None
) -> tuple[list[ValidationIssue], list[Path]]:
    """批量校验流程文件。

    :return: ``(问题列表, 已校验文件列表)``
    """
    schema = load_schema(settings)
    targets = list(files) if files else discover_flow_files(settings)
    issues: list[ValidationIssue] = []
    checked: list[Path] = []
    for path in targets:
        checked.append(path)
        try:
            data = load_json(path)
        except FlowFormatError as exc:
            issues.append(ValidationIssue(path, "", str(exc)))
            continue
        if not isinstance(data, dict):
            issues.append(ValidationIssue(path, "", "顶层必须是 JSON 对象"))
            continue
        for issue in validate_data(data, schema):
            issue.file = path
            issues.append(issue)
        # schema 之外的结构性检查：字段合法但语义不通的情况
        issues.extend(_semantic_checks(path, data))
    return issues, checked


def _semantic_checks(path: Path, data: dict[str, Any]) -> list[ValidationIssue]:
    """schema 表达不了的语义约束。

    动作名**故意不写进 schema 的 enum**：否则每新增一个动作都要同步改 schema，
    违背「新增动作只改 actions.py」的设计目标。改为在这里对照动作注册表校验，
    既能拦住拼写错误，又不产生耦合。
    """
    out: list[ValidationIssue] = []

    if _is_suite_data(data, path):
        if not data.get("flows"):
            out.append(ValidationIssue(path, "flows", "套件未引用任何流程文件", "warning"))
        for i, name in enumerate(data.get("flows") or []):
            try:
                settings_probe = _Settings_for_probe(path)
                settings_probe.resolve_flow_file(name)
            except Exception as exc:  # noqa: BLE001
                out.append(
                    ValidationIssue(path, f"flows[{i}]", f"引用的流程文件无法解析: {exc}", "warning")
                )
        return out

    if not data.get("id"):
        out.append(
            ValidationIssue(path, "id", f"未显式声明 id，将使用文件名 {path.stem}", "warning")
        )

    known_actions = _known_action_names()
    steps = list(_iter_step_dicts(data))
    if not steps:
        out.append(ValidationIssue(path, "steps", "流程没有任何步骤", "warning"))

    ids: set[str] = set()
    for where, step in steps:
        action = step.get("action")
        if not action:
            out.append(ValidationIssue(path, f"{where}.action", "缺少必填字段 action"))
        elif known_actions and action not in known_actions:
            suggestion = _closest(action, known_actions)
            out.append(
                ValidationIssue(
                    path,
                    f"{where}.action",
                    f"未注册的动作 {action!r}"
                    + (f"，是否想写 {suggestion!r}？" if suggestion else "？"),
                )
            )

        sid = step.get("id")
        if sid:
            if sid in ids:
                out.append(
                    ValidationIssue(path, f"{where}.id", f"步骤 id 重复: {sid!r}", "warning")
                )
            ids.add(sid)

        target = step.get("target")
        if isinstance(target, dict) and target.get("image"):
            out.append(
                ValidationIssue(
                    path,
                    f"{where}.target.image",
                    "图像定位当前未实现（本期仅预留空入口），执行时会报 VisionNotImplementedError",
                    "warning",
                )
            )
        if action == "include":
            ref = step.get("flow")
            if not ref:
                out.append(ValidationIssue(path, f"{where}.flow", "include 动作必须指定 flow"))
            elif not _include_resolvable(path, str(ref)):
                # 子流程名拼错在运行到那一步才暴露，代价远高于校验期报一条警告
                out.append(
                    ValidationIssue(
                        path, f"{where}.flow",
                        f"include 引用的子流程找不到: {ref}", "warning",
                    )
                )
        if action in ("repeat",) and not step.get("steps"):
            out.append(ValidationIssue(path, f"{where}.steps", "repeat 动作必须提供 steps"))
        if action == "if" and not step.get("cond"):
            out.append(ValidationIssue(path, f"{where}.cond", "if 动作必须提供 cond"))
    return out


def _iter_step_dicts(data: dict[str, Any]):
    """递归遍历流程中所有步骤（含 setup/teardown 与 then/else/steps 嵌套）。

    :yield: ``(字段路径, 步骤dict)``
    """

    def walk(steps: Any, where: str):
        if not isinstance(steps, list):
            return
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                yield f"{where}[{i}]", {}
                continue
            sub = f"{where}[{i}]"
            yield sub, step
            for key in ("steps", "then", "else"):
                if key in step:
                    yield from walk(step[key], f"{sub}.{key}")

    for section in ("setup", "steps", "teardown"):
        yield from walk(data.get(section), section)


def _known_action_names() -> set[str]:
    """取动作注册表里的全部动作名；注册表不可用时返回空集（跳过校验）。"""
    try:
        from . import asserts  # noqa: F401 - 断言动作定义在另一个模块，不导入就注册不上
        from .actions import ACTION_REGISTRY

        return set(ACTION_REGISTRY)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("动作注册表不可用，跳过动作名校验: %s", exc)
        return set()


def _Settings_for_probe(suite_path: Path) -> Settings:
    """构造一个仅用于解析套件内流程引用的轻量 Settings。"""
    settings = Settings.load(None)
    settings.flow_dir = suite_path.parent
    return settings


def _include_resolvable(flow_path: Path, ref: str) -> bool:
    """判断 include 引用的子流程是否存在。

    解析顺序必须与运行时的 ``StepExecutor._resolve_include`` 一致：调用方
    所在目录优先，再回退到 ``flow_dir`` 与项目根。两边口径不同就会出现
    「``--validate`` 报了警告但实际能跑」或反之，两者都比没有校验更糟。
    """
    if "${" in ref:
        # 带变量的引用要运行时才能确定，静态检查一律放过
        return True
    p = Path(ref).expanduser()
    if p.is_absolute():
        return p.is_file()
    for cand in (flow_path.parent / p, flow_path.parent / f"{p.name}.json"):
        if cand.is_file():
            return True
    try:
        Settings.load(None).resolve_flow_file(ref)
    except Exception:  # noqa: BLE001 - 解析失败即视为找不到
        return False
    return True


def _closest(name: str, candidates: Iterable[str]) -> str | None:
    """给出最接近的候选动作名，拼错时直接提示正确答案。"""
    import difflib

    matches = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None
