"""被测应用会话：启动、连接、健康检查、焦点保护、残留清理。

pywinauto 一律**惰性导入**：这样 ``--validate`` / ``--list`` 等不需要驱动 GUI
的命令，在没有 pywinauto 的环境（或非 Windows）下依然可用。

Qt 相关注意点（都已在代码里落实）：

* 启动前注入 ``QT_ACCESSIBILITY=1``，确保 Qt 的 UIA 桥被激活；
* 窗口 ``class_name`` 形如 ``Qt643QWindowIcon``，随 Qt 小版本变化，只按标题识别；
* Qt 的 UIA Pattern 支持不完整，点击默认走 ``click_input()``（真实鼠标）；
* 界面刷新后 COM 引用会失效，因此本类只保存**定位条件**，不缓存 wrapper。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from .config import Settings
from .errors import AppCrashedError, AppLaunchError, FocusLostError
from .log import get_logger

__all__ = ["set_dpi_awareness", "AppSession"]

_LOG = get_logger("app")

# pywinauto 相关对象缓存，首次使用时导入
_PWA: dict[str, Any] = {}


def _pwa() -> dict[str, Any]:
    """惰性导入 pywinauto，返回缓存的模块对象表。"""
    if not _PWA:
        try:
            from pywinauto import Application, Desktop
            from pywinauto.findwindows import ElementNotFoundError
            from pywinauto.timings import TimeoutError as PwaTimeoutError
        except ImportError as exc:  # pragma: no cover
            raise AppLaunchError(
                "缺少 pywinauto 依赖，无法驱动 GUI",
                detail="请执行 .venv\\Scripts\\python.exe -m pip install -r requirements.txt",
            ) from exc
        _PWA.update(
            Application=Application,
            Desktop=Desktop,
            ElementNotFoundError=ElementNotFoundError,
            TimeoutError=PwaTimeoutError,
        )
    return _PWA


def _pwa_errors() -> tuple[type[BaseException], ...]:
    p = _pwa()
    return (p["ElementNotFoundError"], p["TimeoutError"])


# ----------------------------------------------------------------------
# DPI
# ----------------------------------------------------------------------
def set_dpi_awareness() -> str:
    """把当前进程设为 Per-Monitor V2 DPI 感知，逐级降级。

    **必须在 import pywinauto / 任何 UIA 调用之前执行**，否则高分屏下
    拿到的控件坐标与真实屏幕坐标存在缩放偏差，点击会整体错位。

    进程的 DPI 感知只能设置一次，重复设置会返回失败，这里静默忽略。

    :return: 实际生效的模式名，用于报告环境信息
    """
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4，Win10 1703+，返回 BOOL
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    # PROCESS_PER_MONITOR_DPI_AWARE == 2，Win8.1+，返回 HRESULT（0 为成功）
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    # 走到这里说明本次设置未生效 —— 极可能是进程早已被设过（只能设一次），
    # 因此不报错，由 build_environment() 回报实际生效的模式
    return "unchanged"


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------
class AppSession:
    """一个 app 进程实例及其 UIA 连接。"""

    def __init__(self, settings: Settings, *, log_dir: Path | None = None) -> None:
        self.settings = settings
        self.log_dir = Path(log_dir) if log_dir else None
        self.exe_path: Path | None = None
        self.pid: int | None = None
        self.proc: psutil.Process | None = None
        self.app: Any = None
        self.dpi_mode: str = "unknown"
        self._stdout_fh: Any = None
        self._stderr_fh: Any = None
        self._main_wrapper: Any = None

    # ------------------------------------------------------------------
    # 启动 / 连接
    # ------------------------------------------------------------------
    def launch(self, exe: str | Path | None = None, args: list[str] | None = None) -> int:
        """启动被测应用并建立 UIA 连接。

        不直接用 ``pywinauto.Application.start()``，而是自己 ``Popen`` 后再
        ``connect(process=pid)``，原因是需要精确控制：

        * **环境变量注入**（``QT_ACCESSIBILITY=1``）；
        * 把应用 stdout/stderr 重定向到报告目录 —— 应用崩溃时这是唯一线索，
          且写文件而非管道可避免缓冲区写满导致应用阻塞。
        """
        cfg = self.settings.app
        self.exe_path = Path(exe) if exe else self.settings.resolve_app_path(None)
        cmdline = [str(self.exe_path), *(args if args is not None else list(cfg.args))]
        workdir = str(self.settings.resolve(cfg.workdir)) if cfg.workdir else str(self.exe_path.parent)

        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (cfg.env or {}).items()})

        self._open_app_log_files()
        _LOG.info("启动应用: %s", " ".join(cmdline))
        _LOG.debug("工作目录=%s 注入环境变量=%s", workdir, cfg.env or {})

        try:
            popen = subprocess.Popen(  # noqa: S603 - 路径由使用者通过 --execute 显式指定
                cmdline,
                cwd=workdir,
                env=env,
                stdout=self._stdout_fh or subprocess.DEVNULL,
                stderr=self._stderr_fh or subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            self._close_app_log_files()
            raise AppLaunchError(f"无法启动应用: {self.exe_path}", detail=str(exc)) from exc

        self.pid = popen.pid
        try:
            self.proc = psutil.Process(self.pid)
        except psutil.Error:  # pragma: no cover - 进程刚起就消失
            self.proc = None

        # 极早期的崩溃（缺 DLL、许可校验失败）会在毫秒级退出，先给一点观察窗口
        time.sleep(0.5)
        if popen.poll() is not None:
            self._close_app_log_files()
            raise AppLaunchError(
                f"应用启动后立即退出，返回码 {popen.returncode}",
                detail=self._tail_app_logs(),
            )

        self._connect(self.pid, cfg.startup_timeout)
        return self.pid

    def connect_existing(self, *, pid: int | None = None, title: str | None = None) -> None:
        """连接到已在运行的实例，供调试流程时使用（不重启应用）。"""
        cfg = self.settings.app
        if pid:
            self.pid = pid
            try:
                self.proc = psutil.Process(pid)
                self.exe_path = Path(self.proc.exe())
            except psutil.Error as exc:
                raise AppLaunchError(f"找不到进程 pid={pid}", detail=str(exc)) from exc
            self._connect(pid, cfg.startup_timeout)
        else:
            p = _pwa()
            try:
                self.app = p["Application"](backend=cfg.backend).connect(
                    title=title or cfg.main_window.title,
                    timeout=cfg.startup_timeout,
                )
            except Exception as exc:
                raise AppLaunchError("连接已运行实例失败", detail=str(exc)) from exc
            self.pid = self.app.process

    def _connect(self, pid: int, timeout: float) -> None:
        p = _pwa()
        try:
            self.app = p["Application"](backend=self.settings.app.backend).connect(
                process=pid, timeout=self.settings.scaled(timeout)
            )
        except Exception as exc:
            raise AppLaunchError(
                f"已启动进程 pid={pid}，但 UIA 无法连接其窗口",
                detail=(
                    f"{exc}；常见原因：应用无可见窗口、Qt UIA 桥未激活"
                    "（检查 QT_ACCESSIBILITY）、或以管理员权限运行导致会话隔离"
                ),
            ) from exc
        _LOG.debug("UIA 已连接 pid=%s backend=%s", pid, self.settings.app.backend)

    def wait_main_window(self, timeout: float | None = None) -> Any:
        """等待并返回主窗口 wrapper，同时校验其可访问性。"""
        if self.app is None:
            raise AppLaunchError("应用尚未启动", detail="请先调用 launch 或 connect_existing")
        mw = self.settings.app.main_window
        crit: dict[str, Any] = {}
        if mw.title:
            crit["title"] = mw.title
        if mw.title_re:
            crit["title_re"] = mw.title_re
        if mw.class_name:
            crit["class_name"] = mw.class_name
        # 不能传 process：``Application.window()`` 会自己注入 ``app=self``，
        # 两者共存时 pywinauto 报 "process and app cannot be combined"。
        # ``app=self`` 本身已经限定在本进程（connect/start 时已绑定 pid）。
        if not any(k in crit for k in ("title", "title_re", "best_match")):
            raise AppLaunchError("未配置主窗口识别条件", detail="请设置 app.main_window.title 或 title_re")

        limit = self.settings.scaled(timeout if timeout is not None else mw.timeout)
        spec = self.app.window(**crit)
        try:
            spec.wait("exists", timeout=limit, retry_interval=self.settings.run.poll_interval)
        except _pwa_errors() as exc:
            titles = self._visible_top_titles()
            raise AppLaunchError(
                f"在 {limit:.0f}s 内未找到主窗口 {crit}",
                detail=(
                    f"当前可见顶层窗口: {titles or '（无）'}。"
                    "请核对 config/default.yaml 的 app.main_window.title_re"
                ),
            ) from exc

        wrapper = spec.wrapper_object()
        self._main_wrapper = wrapper
        _LOG.info("已找到主窗口: %r", wrapper.window_text())
        return wrapper

    # ------------------------------------------------------------------
    # 窗口访问
    # ------------------------------------------------------------------
    def window(self, **criteria: Any) -> Any:
        """按条件返回 ``WindowSpecification``（惰性对象，尚未真正查找）。

        ``process`` / ``app`` / ``backend`` 在这里统一剔掉：pywinauto 的
        ``Application.window()`` 会自己注入 ``app=self`` 与 ``backend``，而
        ``find_elements`` 禁止 ``app`` 与 ``process`` 共存（报
        ``Keywords "process" and "app" cannot be combined``）。
        ``WindowRef`` 里保留 ``process`` 是为了日志与报告可读，不应该因此
        让每一次窗口解析都失败 —— 所以在这个唯一入口做容错，而不是要求
        每个调用方自己记得不能传。
        """
        if self.app is None:
            raise AppLaunchError("应用尚未启动")
        for key in ("process", "app", "backend"):
            criteria.pop(key, None)
        return self.app.window(**criteria)

    def top_windows(self) -> list[Any]:
        """当前进程的所有顶层窗口 wrapper 列表。"""
        if self.app is None:
            return []
        try:
            return list(self.app.windows())
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("枚举顶层窗口失败: %s", exc)
            return []

    def _visible_top_titles(self) -> list[str]:
        """枚举桌面上可见顶层窗口标题，用于启动失败时的诊断信息。"""
        try:
            desktop = _pwa()["Desktop"](backend=self.settings.app.backend)
            return [
                w.window_text()
                for w in desktop.windows()
                if w.is_visible() and w.window_text()
            ][:30]
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("枚举桌面窗口失败: %s", exc)
            return []

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def is_alive(self) -> bool:
        """被测进程是否仍在运行。"""
        if self.proc is None:
            return False
        try:
            return self.proc.is_running() and self.proc.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def assert_alive(self) -> None:
        """进程已退出则抛 ``AppCrashedError``，附带应用自身日志尾部。"""
        if not self.is_alive():
            code = self.exit_code()
            raise AppCrashedError(
                f"被测应用已退出（pid={self.pid}, 返回码={code}）",
                detail=self._tail_app_logs(),
            )

    def exit_code(self) -> int | None:
        if self.proc is None:
            return None
        try:
            with self.proc.oneshot():
                return self.proc.exitcode if self.proc.is_running() is False else None
        except psutil.Error:
            return None

    def wait_exit(self, timeout: float = 10.0) -> bool:
        """等待进程退出，超时返回 False。"""
        if self.proc is None:
            return True
        try:
            self.proc.wait(timeout=timeout)
            return True
        except psutil.TimeoutExpired:
            return False

    # ------------------------------------------------------------------
    # 焦点
    # ------------------------------------------------------------------
    @staticmethod
    def foreground_pid() -> int | None:
        """当前前台窗口所属进程 pid，取不到返回 None。"""
        try:
            import win32gui
            import win32process
        except ImportError:  # pragma: no cover
            return None
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            return int(win32process.GetWindowThreadProcessId(hwnd)[1])
        except Exception:  # noqa: BLE001
            return None

    def ensure_foreground(self, wrapper: Any = None, *, timeout: float = 5.0) -> bool:
        """确保被测窗口位于前台。

        GUI 自动化独占前台：运行期间人为切窗口会让点击落到别处，且表现为
        **随机失败**，极难排查。这里主动检测并把窗口拉回前台，仍失败则由
        调用方抛 ``FocusLostError``，在报告中标注 ``FOCUS_LOST`` 与真实缺陷区分。
        """
        if not self.settings.run.foreground_check:
            return True
        target = wrapper or self._main_wrapper
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            fg = self.foreground_pid()
            if fg is not None and self.pid is not None and fg == self.pid:
                return True
            if target is not None:
                try:
                    if target.is_minimized():
                        target.restore()
                    target.set_focus()
                except Exception as exc:  # noqa: BLE001
                    _LOG.debug("set_focus 失败: %s", exc)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def require_foreground(self, wrapper: Any = None, *, timeout: float = 5.0) -> None:
        if not self.ensure_foreground(wrapper, timeout=timeout):
            fg = self.foreground_pid()
            raise FocusLostError(
                "被测应用未能获得前台焦点",
                detail=(
                    f"前台进程 pid={fg}，被测 pid={self.pid}。"
                    "运行期间请勿操作鼠标键盘或切换窗口；"
                    "建议在专用虚拟机或独立用户会话中执行自动化"
                ),
            )

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def close(self, *, force: bool = False, timeout: float = 10.0) -> None:
        """关闭应用。

        优先走正常关闭（``WM_CLOSE``），让应用有机会保存状态、释放资源；
        ``force`` 或正常关闭超时时直接 kill。
        """
        if not self.is_alive():
            self._close_app_log_files()
            return
        if not force:
            try:
                if self._main_wrapper is not None:
                    self._main_wrapper.close()
                elif self.proc is not None:
                    self.proc.terminate()
                if self.wait_exit(timeout):
                    _LOG.info("应用已正常退出")
                    self._close_app_log_files()
                    return
                _LOG.warning("应用在 %.0fs 内未退出，转为强制结束", timeout)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("正常关闭失败（%s），转为强制结束", exc)
        self.kill()

    def kill(self) -> None:
        """强制结束进程及其子进程。"""
        if self.proc is None:
            return
        try:
            for child in self.proc.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            self.proc.kill()
            self.proc.wait(timeout=5)
        except psutil.Error as exc:
            _LOG.debug("强制结束进程失败: %s", exc)
        finally:
            self._close_app_log_files()

    def kill_leftovers(self) -> int:
        """清理同名残留进程，避免污染下一条用例。

        只清理**不是本次启动**的同名进程；本次进程由 ``close()`` 负责。
        """
        name = self.settings.app.process_name
        if not name and self.exe_path:
            name = self.exe_path.name
        if not name:
            return 0
        killed = 0
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info["name"] or "").lower() != name.lower():
                    continue
                if self.pid is not None and p.info["pid"] == self.pid:
                    continue
                p.kill()
                killed += 1
                _LOG.warning("清理残留进程 %s (pid=%s)", name, p.info["pid"])
            except (psutil.Error, KeyError):
                continue
        return killed

    # ------------------------------------------------------------------
    # 应用日志
    # ------------------------------------------------------------------
    def _open_app_log_files(self) -> None:
        if self.log_dir is None:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._stdout_fh = open(self.log_dir / "app_stdout.log", "w", encoding="utf-8", errors="replace")
            self._stderr_fh = open(self.log_dir / "app_stderr.log", "w", encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover
            _LOG.debug("创建应用日志文件失败: %s", exc)
            self._stdout_fh = self._stderr_fh = None

    def _close_app_log_files(self) -> None:
        for fh in (self._stdout_fh, self._stderr_fh):
            try:
                if fh:
                    fh.flush()
                    fh.close()
            except OSError:  # pragma: no cover
                pass
        self._stdout_fh = self._stderr_fh = None

    def _tail_app_logs(self, lines: int = 20) -> str:
        """取应用自身日志尾部，用于崩溃诊断。"""
        if self.log_dir is None:
            return ""
        out: list[str] = []
        for fname in ("app_stderr.log", "app_stdout.log"):
            path = self.log_dir / fname
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                tail = "\n".join(text.splitlines()[-lines:])
                out.append(f"--- {fname} ---\n{tail}")
        return "\n".join(out)

    def env_snapshot(self) -> dict[str, Any]:
        """报告里展示的环境信息。"""
        return {
            "exe": str(self.exe_path) if self.exe_path else None,
            "pid": self.pid,
            "backend": self.settings.app.backend,
            "dpi_awareness": self.dpi_mode,
            "app_version": self._file_version(),
        }

    def _file_version(self) -> str | None:
        """读取 exe 的文件版本号，便于报告区分被测版本。"""
        if not self.exe_path or not self.exe_path.is_file():
            return None
        try:
            import win32api

            info = win32api.GetFileVersionInfo(str(self.exe_path), "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            return ".".join(str(x) for x in (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF))
        except Exception:  # noqa: BLE001 - 版本号取不到不影响测试
            return None
