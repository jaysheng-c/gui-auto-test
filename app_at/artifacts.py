"""失败留证：截图与控件树快照落盘。

这是框架降低维护成本的关键模块 —— 定位失败时，排查者不需要重新复现，
直接打开 ``trees/*.txt`` 对着真实控件树修改 JSON 里的 ``target`` 即可。

所有方法都**吞掉自身异常**：留证失败绝不能掩盖真正的测试失败原因。
"""

from __future__ import annotations

import base64
import contextlib
import io
import re
from pathlib import Path
from typing import Any

from .compat import supported_kwargs
from .log import get_logger

__all__ = ["Artifacts", "safe_name", "control_tree_text"]

_LOG = get_logger("artifacts")

# Windows 文件名非法字符 + 空白
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_NAME_LEN = 120


def safe_name(name: str, fallback: str = "unnamed") -> str:
    """把任意字符串规范化为安全的文件名片段。"""
    cleaned = _INVALID_CHARS.sub("_", str(name or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", "_", cleaned)
    if not cleaned:
        return fallback
    return cleaned[:_MAX_NAME_LEN]


# ----------------------------------------------------------------------
# 控件树导出
# ----------------------------------------------------------------------
_WALK_DEPTH_LIMIT = 12


def control_tree_text(root: Any, *, depth: int | None = None, max_width: int = 160) -> str:
    """把控件树导出成文本，供失败留证与 ``--inspect`` 共用。

    两个坑在这里一并处理：

    1. ``print_control_identifiers`` **只存在于 ``WindowSpecification``**，wrapper
       上没有（直接调会抛 ``AttributeError``），因此需要用句柄临时造一个 spec；
       且各版本参数不同（``max_width`` 在 0.6.9 已被去掉），靠
       :func:`supported_kwargs` 过滤。
    2. 控件树可能很大且读取过程中 COM 引用会失效，pywinauto 自己就可能抛异常。
       因此它失败时**退回到自己遍历 ``children()``** 生成的简化树：留证宁可
       信息少一点，也不能整份为空 —— 空的控件树快照比没有更误导人。
    """
    printed, ok = _print_control_identifiers(root, depth=depth)
    if ok:
        return _clip(printed, max_width)
    walked = _walk_tree(root, depth=depth)
    marker = "" if not printed.strip() else "\n[print_control_identifiers 中断，以下为自行遍历的简化树]\n"
    return _clip(printed + marker + walked, max_width)


def _print_control_identifiers(root: Any, *, depth: int | None) -> tuple[str, bool]:
    """尝试用 pywinauto 原生导出；返回（已捕获文本, 是否完整成功）。"""
    printer = getattr(root, "print_control_identifiers", None)
    if printer is None:
        printer = _spec_printer(root)
    if printer is None:
        return "", False
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            printer(**supported_kwargs(printer, {"depth": depth, "max_width": 160}))
        return buf.getvalue(), True
    except Exception as exc:  # noqa: BLE001 - 导出失败不能让留证整体失败
        _LOG.debug("print_control_identifiers 失败，退回自行遍历: %s", exc)
        return buf.getvalue(), False


def _spec_printer(root: Any) -> Any:
    """从 wrapper 的句柄临时构造一个 ``WindowSpecification`` 以拿到导出方法。"""
    handle = getattr(root, "handle", None)
    if not handle:
        return None
    try:
        from pywinauto.application import WindowSpecification

        criteria: dict[str, Any] = {"handle": handle}
        backend = _backend_of(root)
        if backend:
            # 不写 backend 会按默认（win32）去解，对 UIA 控件拿到的是错的元素
            criteria["backend"] = backend
        return WindowSpecification(criteria, allow_magic_lookup=False).print_control_identifiers
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("无法从句柄构造 WindowSpecification: %s", exc)
        return None


def _backend_of(wrapper: Any) -> str:
    """判定 wrapper 所属后端，无法判定时返回空串（不写 backend 条件）。"""
    try:
        from pywinauto.uia_element_info import UIAElementInfo

        if isinstance(getattr(wrapper, "element_info", None), UIAElementInfo):
            return "uia"
    except Exception:  # noqa: BLE001
        return ""
    return "win32"


def _walk_tree(root: Any, *, depth: int | None = None) -> str:
    """自己递归 ``children()`` 生成简化控件树，作为原生导出的兜底。"""
    limit = int(depth) if depth else _WALK_DEPTH_LIMIT
    lines: list[str] = []

    def walk(node: Any, level: int) -> None:
        lines.append("    " * level + _describe(node))
        if level >= limit:
            return
        try:
            children = list(node.children())
        except Exception as exc:  # noqa: BLE001 - COM 引用失效就到此为止
            lines.append("    " * (level + 1) + f"<子控件读取失败: {type(exc).__name__}: {exc}>")
            return
        for child in children:
            walk(child, level + 1)

    try:
        walk(root, 0)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"<控件树遍历失败: {type(exc).__name__}: {exc}>")
    return "\n".join(lines) + "\n"


def _describe(node: Any) -> str:
    """一行控件描述，字段逐个容错：任一属性读不到不影响其余。"""
    def _get(attr: str, default: str = "") -> str:
        try:
            value = getattr(node, attr, None)
            value = value() if callable(value) else value
            return "" if value is None else str(value)
        except Exception:  # noqa: BLE001
            return default

    info = getattr(node, "element_info", None)
    ctrl = _get("control_type") or (getattr(info, "control_type", "") or "")
    auto_id = getattr(info, "automation_id", "") or ""
    klass = _get("class_name") or (getattr(info, "class_name", "") or "")
    title = _get("window_text") or (getattr(info, "name", "") or "")
    rect = _get("rectangle")
    bits = [f"[{ctrl or '?'}]", repr(title)]
    if auto_id:
        bits.append(f"auto_id={auto_id!r}")
    if klass:
        bits.append(f"class={klass!r}")
    if rect:
        bits.append(f"rect=({rect})")
    return "  ".join(bits)


def _clip(text: str, max_width: int) -> str:
    """逐行限宽，避免单个超长控件名把快照撑成无法阅读的大文件。"""
    if not text or max_width <= 0:
        return text
    return "".join(
        line if len(line) <= max_width else line[: max_width - 3] + "..."
        for line in text.splitlines(keepends=True)
    )


class Artifacts:
    """一次运行（``reports/<run_id>/``）的产物管理器。"""

    def __init__(self, root_dir: Path | str) -> None:
        self.root = Path(root_dir)
        self.screenshot_dir = self.root / "screenshots"
        self.tree_dir = self.root / "trees"
        for d in (self.root, self.screenshot_dir, self.tree_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def rel(self, path: Path | str | None) -> str | None:
        """返回相对 ``root`` 的 POSIX 风格路径，供 HTML 报告引用。"""
        if path is None:
            return None
        try:
            return Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except (ValueError, OSError):
            return str(path)

    def read_b64(self, path: Path | str | None) -> str | None:
        """把图片读成 base64，用于内嵌到单文件 HTML 报告。"""
        if path is None:
            return None
        try:
            p = Path(path)
            if not p.is_file():
                return None
            return base64.b64encode(p.read_bytes()).decode("ascii")
        except OSError as exc:  # pragma: no cover
            _LOG.debug("读取截图失败 %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------
    def capture_screen(self, name: str) -> Path | None:
        """全屏截图。"""
        target = self.screenshot_dir / f"{safe_name(name)}.png"
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab(all_screens=True)
            img.save(target, format="PNG")
            _LOG.debug("已保存全屏截图 %s", target)
            return target
        except Exception as exc:  # noqa: BLE001 - 留证失败不能影响主流程
            _LOG.debug("全屏截图失败 %s: %s", name, exc)
            return None

    def capture_element(self, wrapper: Any, name: str) -> Path | None:
        """控件区域截图。

        控件级截图能过滤掉无关桌面内容，排查更聚焦；失败时回退到全屏截图。
        """
        target = self.screenshot_dir / f"{safe_name(name)}.png"
        try:
            img = wrapper.capture_as_image()
            if img is not None:
                img.save(target, format="PNG")
                _LOG.debug("已保存控件截图 %s", target)
                return target
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("控件截图失败 %s: %s", name, exc)
        return self.capture_screen(name)

    # ------------------------------------------------------------------
    # 控件树
    # ------------------------------------------------------------------
    def dump_tree(
        self,
        wrapper: Any,
        name: str,
        *,
        depth: int | None = None,
        max_width: int = 160,
        header: str | None = None,
    ) -> Path | None:
        """导出控件树快照到 ``trees/<name>.txt``。
    
        具体导出逻辑在 :func:`control_tree_text`（``--inspect`` 共用同一套）；
        这里只负责拼表头与落盘，并保证异常不会向上冒泡 —— 留证失败绝不能
        掩盖真正的测试失败原因。
        """
        target = self.tree_dir / f"{safe_name(name)}.txt"
        try:
            body = control_tree_text(wrapper, depth=depth, max_width=max_width)
            parts = []
            if header:
                parts.append(header + "\n" + "=" * max_width + "\n")
            parts.append(body)
            target.write_text("".join(parts), encoding="utf-8")
            _LOG.debug("已保存控件树 %s", target)
            return target
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("控件树导出失败 %s: %s", name, exc)
            return None

    def write_text(self, subdir: str, name: str, content: str) -> Path | None:
        """通用文本产物落盘，用于 --inspect 等场景。"""
        try:
            d = self.root / subdir
            d.mkdir(parents=True, exist_ok=True)
            target = d / f"{safe_name(name)}.txt"
            target.write_text(content, encoding="utf-8")
            return target
        except OSError as exc:  # pragma: no cover
            _LOG.debug("写入产物失败 %s/%s: %s", subdir, name, exc)
            return None
