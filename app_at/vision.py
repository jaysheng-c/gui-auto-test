"""图像定位兜底 —— **本期仅保留空入口，未实现**。

设计目标是"接线完整、实现留白"：

* ``locator.py`` 已经会在 ``target.image`` 出现或 ``vision.enabled=true`` 时
  正常路由到本模块，因此后续填充实现**无需改动框架其他任何代码**；
* ``flow.schema.json`` 已保留 ``image`` / ``assert_image`` 字段定义；
* ``config/default.yaml`` 已保留 ``vision`` 配置段。

之所以现在不做：app 是 Qt 应用，UIA 控件树通常已足够定位；图像识别对
分辨率、DPI、主题、字体渲染高度敏感，维护成本远高于控件定位。只有当出现
自绘控件（图表、画布、视频区）在控件树中表现为单个黑盒 Pane 时，才值得引入。

启用步骤（届时）::

    1. pip install numpy opencv-python        # 需要 OCR 再加 rapidocr-onnxruntime
    2. 实现本模块的 find_by_image / wait_for_image / click_by_image
    3. config/default.yaml 里把 vision.enabled 置为 true
    4. 把控件截图模板放到 resources/images/，JSON 里写 "image": "btn_ok.png"
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Sequence

from .errors import VisionNotImplementedError

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings

__all__ = [
    "Region",
    "ImageMatch",
    "ImageLocator",
    "find_by_image",
    "find_all_by_image",
    "wait_for_image",
    "click_by_image",
    "ocr_text",
    "create_locator",
    "is_available",
]

_HINT = (
    "图像定位当前未实现（本期仅预留空入口）。"
    "请改用控件定位（auto_id / name / type / parents），"
    "或用 click_offset 按父控件矩形做相对坐标点击；"
    "启用图像能力的方法见本模块 docstring 与 docs/flow_authoring.md。"
)


def _not_implemented(what: str) -> VisionNotImplementedError:
    return VisionNotImplementedError(f"图像定位能力「{what}」尚未实现", detail=_HINT)


# ----------------------------------------------------------------------
# 数据结构（已定义，实现落地后可直接使用）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Region:
    """屏幕矩形区域，坐标为绝对像素。"""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        """转成 pywinauto / PIL 惯用的 (left, top, right, bottom)。"""
        return self.left, self.top, self.right, self.bottom

    @classmethod
    def from_rect(cls, rect: Any) -> Region:
        """从 pywinauto 的 RECT 对象构造。"""
        return cls(int(rect.left), int(rect.top), int(rect.width()), int(rect.height()))


@dataclass(frozen=True)
class ImageMatch:
    """一次模板匹配的结果。"""

    region: Region
    confidence: float
    template: str

    @property
    def center(self) -> tuple[int, int]:
        return self.region.center


class ImageLocator(Protocol):
    """图像定位器协议。实现类只需满足这些签名即可无缝接入 ``locator.py``。"""

    def find(
        self,
        template: str | Path,
        *,
        region: Region | None = None,
        threshold: float | None = None,
        grayscale: bool = True,
    ) -> ImageMatch | None: ...

    def find_all(
        self,
        template: str | Path,
        *,
        region: Region | None = None,
        threshold: float | None = None,
    ) -> Sequence[ImageMatch]: ...

    def wait(
        self,
        template: str | Path,
        *,
        timeout: float = 10.0,
        interval: float = 0.5,
        region: Region | None = None,
        threshold: float | None = None,
    ) -> ImageMatch: ...

    def click(
        self,
        template: str | Path,
        *,
        timeout: float = 10.0,
        region: Region | None = None,
        threshold: float | None = None,
        button: str = "left",
        offset: tuple[int, int] = (0, 0),
    ) -> ImageMatch: ...


# ----------------------------------------------------------------------
# 模块级函数入口（locator.py 调用的就是这些）
# ----------------------------------------------------------------------
def is_available() -> bool:
    """图像能力是否可用。本期恒为 False。

    ``locator.py`` 用它决定是否提前给出更明确的报错信息。
    """
    return False


def find_by_image(
    template: str | Path,
    *,
    region: Region | None = None,
    threshold: float = 0.85,
    settings: Settings | None = None,
) -> ImageMatch | None:
    """在屏幕（或指定区域）中查找模板图片，返回最佳匹配。"""
    raise _not_implemented("find_by_image")


def find_all_by_image(
    template: str | Path,
    *,
    region: Region | None = None,
    threshold: float = 0.85,
    settings: Settings | None = None,
) -> Sequence[ImageMatch]:
    """查找全部匹配项，用于 ``assert_count`` 之类的数量断言。"""
    raise _not_implemented("find_all_by_image")


def wait_for_image(
    template: str | Path,
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
    region: Region | None = None,
    threshold: float = 0.85,
    settings: Settings | None = None,
) -> ImageMatch:
    """轮询等待模板出现，超时抛 ``ElementNotFoundError``。"""
    raise _not_implemented("wait_for_image")


def click_by_image(
    template: str | Path,
    *,
    timeout: float = 10.0,
    region: Region | None = None,
    threshold: float = 0.85,
    button: str = "left",
    offset: tuple[int, int] = (0, 0),
    settings: Settings | None = None,
) -> ImageMatch:
    """等待模板出现并点击其中心（可加偏移）。"""
    raise _not_implemented("click_by_image")


def ocr_text(region: Region | None = None, *, lang: str = "ch") -> str:
    """OCR 识别指定区域文本。依赖 rapidocr-onnxruntime，本期未启用。"""
    raise _not_implemented("ocr_text")


def create_locator(settings: Settings | None = None) -> ImageLocator:
    """工厂函数：返回 ``ImageLocator`` 实现实例。"""
    raise _not_implemented("create_locator")
