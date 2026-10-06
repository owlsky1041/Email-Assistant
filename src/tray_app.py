"""系统托盘常驻与通知（§3.6 / §11.1）。

并发模型
--------
托盘/UI 主线程**只负责显示**：
* 同步、索引、嵌入全部由 ``SyncScheduler`` 的工作线程执行；
* 知识库 API 运行在**独立子进程**，彻底隔离 uvicorn 与 GUI 事件循环；
* 二者通过 SQLite（WAL）与 HTTP 通信，不共享内存。

无 GUI 环境（服务器 / CI / 无 ``DISPLAY``）会自动降级为纯守护模式，
只跑调度器，不报错。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken, get_cancellation_token
from .config import AppConfig
from .context import AppContext
from .models import SyncResult, utcnow
from .scheduler import SyncScheduler

logger = logging.getLogger(__name__)

try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore

    TRAY_AVAILABLE = True
except Exception:  # noqa: BLE001 pragma: no cover
    pystray = None  # type: ignore
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    TRAY_AVAILABLE = False

try:
    from plyer import notification as plyer_notification  # type: ignore

    PLYER_AVAILABLE = True
except Exception:  # noqa: BLE001 pragma: no cover
    plyer_notification = None  # type: ignore
    PLYER_AVAILABLE = False

INTERVAL_CHOICES = (5, 10, 15, 30, 60)

#: 托盘标题。Windows / macOS 的后端按 Unicode 处理，可以用中文；
#: 而 pystray 的 **X11 后端用 latin-1 编码 WM_NAME**，中文会在**构造 Icon 时**
#: 直接抛 UnicodeEncodeError，导致整个托盘起不来（实测确认）。
TRAY_TITLE_ZH = "邮件管理助手"
TRAY_TITLE_ASCII = "Email Assistant"


def tray_title() -> str:
    """按平台选择托盘标题。

    Linux/X11 只能安全使用 ASCII；这不是偏好问题，是避免托盘直接崩溃。
    """
    if sys.platform.startswith("linux"):
        return TRAY_TITLE_ASCII
    return TRAY_TITLE_ZH


def has_display() -> bool:
    """判断当前环境是否具备图形界面。"""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def x11_tray_host_available() -> bool | None:
    """X11 下是否真的有系统托盘宿主。

    ``None`` 表示"判断不了"（非 Linux/X11，或没有 python-xlib）。

    为什么需要它：pystray 的 X11 后端走的是老的 XEmbed 协议，需要
    ``_NET_SYSTEM_TRAY_S<screen>`` 这个 selection 有人持有。而 Plasma 6、
    GNOME 3.26+ 等现代桌面**只提供 StatusNotifierItem**，根本不建这个
    selection —— 此时 pystray 会在自己的后台线程里抛 AssertionError，
    异常传不回 ``icon.run()``，于是进程活着、图标永远不出现，用户看到的
    是一个"什么都没发生"的命令。
    """
    if not sys.platform.startswith("linux"):
        return None
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return None  # 纯 Wayland 走 appindicator 后端，判断不了
    if not os.environ.get("DISPLAY"):
        return False
    try:
        from Xlib import display as xdisplay  # type: ignore
    except Exception:  # noqa: BLE001 - 没有 python-xlib 就交给 pystray 自己试
        return None
    try:
        conn = xdisplay.Display()
        try:
            screen = conn.get_default_screen()
            atom = conn.intern_atom(f"_NET_SYSTEM_TRAY_S{screen}")
            owner = conn.get_selection_owner(atom)
            return bool(owner)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def open_local_path(path: str | Path) -> bool:
    """用系统默认程序打开本地文件/目录。"""
    target = Path(path)
    if not target.exists():
        logger.warning("路径不存在：%s", target)
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except Exception:  # noqa: BLE001
        logger.exception("打开路径失败：%s", target)
        return False


def open_url(url: str) -> bool:
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001
        logger.exception("打开链接失败：%s", url)
        return False


def build_icon_image(size: int = 64):  # type: ignore[no-untyped-def]
    """用 Pillow 画一个简单的信封图标，免去外部资源依赖。"""
    if not TRAY_AVAILABLE:
        return None
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = size // 8
    body = (margin, margin + size // 8, size - margin, size - margin - size // 8)
    draw.rounded_rectangle(body, radius=size // 12, fill=(38, 103, 201, 255))
    # 信封的折角
    draw.line(
        [
            (body[0], body[1]),
            (size // 2, body[1] + (body[3] - body[1]) // 2),
            (body[2], body[1]),
        ],
        fill=(255, 255, 255, 255),
        width=max(2, size // 16),
        joint="curve",
    )
    return image


class TrayApplication:
    """托盘应用主控。"""

    def __init__(
        self,
        context: AppContext,
        *,
        cancel_token: CancellationToken | None = None,
        with_api: bool = True,
        instance_guard: Any = None,
    ) -> None:
        self.context = context
        #: 单实例守卫；作为主实例时用它开控制通道，接住第二次启动的请求
        self.instance_guard = instance_guard
        self.config: AppConfig = context.config
        self.cancel = cancel_token or get_cancellation_token()
        self.with_api = with_api
        self.scheduler = SyncScheduler(context, cancel_token=self.cancel, on_new_mail=self._on_new_mail)
        self._icon = None
        self._api_process: subprocess.Popen[bytes] | None = None
        self._latest_path: str = ""
        self._status_text = "就绪"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def run(self) -> int:
        self._start_control_channel()
        self.scheduler.start()
        if self.with_api:
            self.start_api_process()

        usable = self.config.tray.enabled and TRAY_AVAILABLE and has_display()
        if usable and not self._tray_host_ready():
            reason = (
                "当前桌面没有 XEmbed 系统托盘"
                "（Plasma 6 / GNOME 3.26+ 只提供 StatusNotifierItem）"
            )
            logger.warning("托盘不可用：%s，改为守护模式", reason)
            logger.warning(
                "设置界面仍可用：执行 `main.py settings`，"
                "或直接编辑配置文件；同步照常在后台运行。"
            )
            self._notify(f"托盘不可用（{reason}），已在后台运行")
            return self._run_headless()

        if not usable:
            reason = (
                "配置已禁用托盘"
                if not self.config.tray.enabled
                else "未安装 pystray/Pillow"
                if not TRAY_AVAILABLE
                else "当前环境没有图形界面"
            )
            logger.info("以守护模式运行（%s），按 Ctrl+C 退出", reason)
            return self._run_headless()

        logger.info("托盘已启动，右键图标查看菜单")

        # 首次运行引导：还没配邮箱或授权码时，仍然先弹**设置窗口**
        # （主窗口没有账号信息就只是个空壳，引导顺序反了）。
        # 配好之后再打开主窗口就是自然的下一步。
        if self.needs_setup():
            logger.info("检测到尚未完成配置，自动打开设置界面")
            self._notify("请先完成邮箱配置")
            threading.Timer(3.0, self._action_open_settings).start()
        else:
            logger.info("配置就绪，打开主窗口")
            threading.Timer(1.5, self._action_open_main).start()

        return self._run_tray()

    def _tray_host_ready(self) -> bool:
        """托盘宿主预检，避免"进程活着但图标永远不出现"。"""
        available = x11_tray_host_available()
        if available is None:
            return True  # 判断不了就交给 pystray 自己试
        return available

    def _start_control_channel(self) -> None:
        """作为主实例接收第二次启动的请求。

        用户双击托盘图标 / 再次运行程序时，我们不去起第二个客户端
        （两个进程抢数据库与向量库在 Windows 上会直接崩），而是让对方
        把"打开主窗口"这个意图发过来，由本进程打开。
        """
        if self.instance_guard is None:
            return
        handlers = {
            "open-main": self._action_open_main,
            "open-settings": self._action_open_settings,
            "sync": self._action_sync,
        }

        def handle(action: str) -> dict[str, Any]:
            if action == "ping":
                return {"ok": True, "pid": os.getpid()}
            func = handlers.get(action)
            if func is None:
                return {"ok": False, "error": f"未知动作：{action}"}
            # 控制通道跑在独立线程，而这些动作本来就在别的线程里被调用过
            # （pystray 线程），因此可以安全地在这里调用。
            func()
            return {"ok": True, "action": action}

        try:
            port = self.instance_guard.serve(handle)
            logger.info("已启用单实例控制通道（127.0.0.1:%s）", port)
        except OSError as exc:
            logger.warning("无法启用单实例控制通道：%s", exc)

    def _run_headless(self) -> int:
        try:
            while not self.cancel.wait(1.0):
                pass
        except KeyboardInterrupt:
            logger.info("收到中断信号")
        finally:
            self.shutdown()
        return 0

    def _run_tray(self) -> int:
        assert pystray is not None
        menu = pystray.Menu(
            pystray.MenuItem("打开主窗口", self._action_open_main, default=True),
            pystray.MenuItem("立即同步", self._action_sync),
            pystray.MenuItem("设置…", self._action_open_settings),
            pystray.MenuItem("打开最新邮件", self._action_open_latest),
            pystray.MenuItem("打开归档目录", self._action_open_archive),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "同步间隔",
                pystray.Menu(
                    *[
                        pystray.MenuItem(
                            f"{minutes} 分钟",
                            self._make_interval_action(minutes),
                            checked=self._make_interval_check(minutes),
                            radio=True,
                        )
                        for minutes in INTERVAL_CHOICES
                    ]
                ),
            ),
            pystray.MenuItem("打开知识库 API 文档", self._action_open_docs),
            pystray.MenuItem("查看状态", self._action_show_status),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._action_quit),
        )

        self._icon = self._build_icon(menu)
        try:
            self._icon.run()
        except Exception:  # noqa: BLE001
            logger.exception("托盘运行失败，降级为守护模式")
            return self._run_headless()
        finally:
            self.shutdown()
        return 0

    def _build_icon(self, menu):  # type: ignore[no-untyped-def]
        """构造托盘图标，并在后端不支持非 ASCII 标题时自动降级。

        某些后端（如 pystray 的 X11 实现）用 latin-1 编码窗口标题，
        中文会直接抛 UnicodeEncodeError。这里兜住，保证托盘仍能起来，
        而不是让用户看到一个什么都点不到的图标。
        """
        assert pystray is not None
        title = tray_title()
        try:
            return pystray.Icon(
                "email-assistant", icon=build_icon_image(), title=title, menu=menu
            )
        except UnicodeEncodeError:
            logger.warning(
                "当前托盘后端不支持非 ASCII 标题（%s），回退为英文标题", title
            )
            return pystray.Icon(
                "email-assistant",
                icon=build_icon_image(),
                title=TRAY_TITLE_ASCII,
                menu=menu,
            )

    def shutdown(self) -> None:
        logger.info("正在退出…")
        if self.instance_guard is not None:
            try:
                self.instance_guard.close()
            except Exception:  # noqa: BLE001
                logger.debug("释放单实例锁失败", exc_info=True)
        self.cancel.cancel("托盘退出")
        self.scheduler.shutdown(wait=False)
        self.stop_api_process()
        try:
            self.context.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭上下文时出错", exc_info=True)

    # ------------------------------------------------------------------
    # API 子进程
    # ------------------------------------------------------------------

    def start_api_process(self) -> None:
        """把 uvicorn 放进独立子进程（§11.1 API 服务隔离）。"""
        if self._api_process is not None:
            return
        host = self.config.api.host
        port = self.config.api.port

        from .config import is_frozen

        if is_frozen():
            # 打包产物没有 main.py 可执行，直接重新运行自身
            cmd = [sys.executable, "serve", "--host", host, "--port", str(port)]
        else:
            cmd = [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "main.py"),
                "serve",
                "--host",
                host,
                "--port",
                str(port),
            ]
        try:
            kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._api_process = subprocess.Popen(cmd, **kwargs)
            logger.info("知识库 API 子进程已启动（pid=%s）", self._api_process.pid)
        except Exception:  # noqa: BLE001
            logger.exception("启动 API 子进程失败，托盘将继续运行")
            self._api_process = None

    def stop_api_process(self) -> None:
        if self._api_process is None:
            return
        try:
            self._api_process.terminate()
            self._api_process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                self._api_process.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._api_process = None
            logger.info("知识库 API 子进程已停止")

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    def _on_new_mail(self, result: SyncResult) -> None:
        """§3.6：新邮件气泡通知。"""
        if not self.config.tray.notify_on_new_mail:
            return
        count = len(result.new_message_ids)
        if count <= 0:
            return

        latest = self._latest_message_path()
        self._latest_path = latest
        title = f"收到 {count} 封新邮件"
        message = self._latest_subject() or "点击托盘图标查看详情"

        sent = False
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
                sent = True
            except Exception:  # noqa: BLE001
                logger.debug("托盘通知失败", exc_info=True)
        if not sent and PLYER_AVAILABLE:
            try:
                plyer_notification.notify(
                    title=title, message=message, app_name="邮件管理助手", timeout=10
                )
                sent = True
            except Exception:  # noqa: BLE001
                logger.debug("plyer 通知失败", exc_info=True)
        if not sent:
            logger.info("【新邮件】%s —— %s", title, message)

        # 按配置自动打开
        action = self.config.tray.click_action
        if action in ("markdown", "both") and latest:
            open_local_path(latest)
        if action in ("webmail", "both"):
            open_url(self.config.tray.webmail_url)

    def _latest_message_path(self) -> str:
        row = self.context.db.query_one(
            "SELECT local_markdown_path FROM messages WHERE deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        return str(row["local_markdown_path"]) if row else ""

    def _latest_subject(self) -> str:
        row = self.context.db.query_one(
            "SELECT subject FROM messages WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        return str(row["subject"]) if row else ""

    # ------------------------------------------------------------------
    # 菜单动作
    # ------------------------------------------------------------------

    def _action_sync(self, *_args: Any) -> None:
        if self.context.sync.is_running:
            self._notify("同步正在进行中")
            return
        self._notify("已开始同步…")
        threading.Thread(
            target=self.scheduler._run_sync, name="tray-sync", daemon=True
        ).start()

    def settings_url(self) -> str:
        return f"http://{self.config.api.host}:{self.config.api.port}/setup"

    def _action_open_main(self, *_args: Any) -> None:
        """打开程序主窗口（状态面板 + 检索）。

        托盘回调跑在 pystray 线程上，而 tkinter 只能在主线程创建窗口，
        因此派生一个独立子进程。
        """
        import subprocess

        from .gui import window_available
        from .gui.main_window import main_window_command

        if not window_available():
            logger.warning("当前环境没有图形界面，无法打开主窗口")
            self._notify("当前环境没有图形界面")
            return
        try:
            subprocess.Popen(main_window_command(self._config_path()))
        except OSError as exc:
            logger.error("打开主窗口失败：%s", exc)
            self._notify(f"打开主窗口失败：{exc}")

    def _action_open_settings(self, *_args: Any) -> None:
        """打开设置界面。

        默认弹**原生窗口**，不再把用户丢到浏览器里填表。托盘回调跑在
        pystray 的线程上，而 tkinter 只能在主线程创建窗口，因此这里
        fork 一个独立子进程去开窗口。子进程退出后重新加载配置，
        这样刚改的设置立刻生效，不用重启托盘。
        """
        from .gui.settings_window import open_window_process, window_available

        if window_available():
            self._notify("正在打开设置…")

            def _open() -> None:
                open_window_process(config_path=self._config_path(), wait=True)
                self._reload_config()

            threading.Thread(target=_open, name="settings-window", daemon=True).start()
            return

        # 没有图形界面：退回浏览器模式（服务器 / 纯命令行场景）
        logger.info("当前环境没有图形界面，改用浏览器设置页")
        if self._api_process is None:
            self.start_api_process()
            # 给 uvicorn 一点启动时间，否则浏览器会看到"拒绝连接"
            threading.Timer(2.0, lambda: open_url(self.settings_url())).start()
            self._notify("正在启动设置界面…")
            return
        open_url(self.settings_url())

    def _config_path(self) -> str | None:
        """当前实际加载的配置文件路径（设置窗口必须写回同一个文件）。"""
        source = getattr(self.config, "source_path", None)
        return str(source) if source else None

    def _reload_config(self) -> None:
        """设置窗口关闭后重新读配置。

        只重载"安全"的部分：账号、授权码、同步参数、清洗策略。
        **数据目录/数据库路径不热切换** —— 正在跑的 DB 连接和向量库句柄
        都指向老路径，中途换掉会写坏数据。这种情况下如实告诉用户要重启，
        而不是假装已经生效。
        """
        from .config import load_config

        old = self.config
        try:
            new = load_config(old.source_path)
        except Exception as exc:  # noqa: BLE001 - 配置读坏了不该把托盘弄挂
            logger.exception("重新加载配置失败")
            self._notify(f"配置读取失败：{exc}")
            return

        moved = [
            label
            for label, before, after in (
                ("数据目录", old.archive_path, new.archive_path),
                ("数据库", old.sqlite_file, new.sqlite_file),
            )
            if before != after
        ]

        self.config = new
        self.context.config = new
        self.scheduler.config = new

        if moved:
            logger.warning("检测到 %s 变化，需要重启才能生效", "、".join(moved))
            self._notify(f"{'、'.join(moved)}已变更，请重启程序后生效")
        else:
            logger.info("配置已重新加载")
            self._notify("配置已更新")

    def needs_setup(self) -> bool:
        """是否需要引导用户完成首次配置。"""
        from .config import resolve_auth_code

        if not self.config.email.address:
            return True
        try:
            return not resolve_auth_code(self.config)
        except Exception:  # noqa: BLE001
            return True

    def _action_open_latest(self, *_args: Any) -> None:
        path = self._latest_path or self._latest_message_path()
        if path:
            open_local_path(path)
        else:
            self._notify("还没有归档任何邮件")

    def _action_open_archive(self, *_args: Any) -> None:
        open_local_path(self.config.archive_path)

    def _action_open_docs(self, *_args: Any) -> None:
        open_url(f"http://{self.config.api.host}:{self.config.api.port}/docs")

    def _action_show_status(self, *_args: Any) -> None:
        status = self.context.sync.status()
        lines = [
            f"账号：{status['account']}",
            f"邮件总数：{status['total_messages']}",
            f"切片总数：{status['chunks']}",
            f"待索引：{status['pending_index']}",
            f"文件夹数：{len(status['folders'])}",
        ]
        last = status.get("last_sync")
        if last:
            lines.append(
                f"上次同步：{last.get('finished_at') or last.get('started_at')} "
                f"（{last.get('status')}，归档 {last.get('archived')} 封）"
            )
        text = "\n".join(lines)
        self._notify(text)
        logger.info("状态：\n%s", text)

    def _action_quit(self, *_args: Any) -> None:
        self.cancel.cancel("用户从托盘退出")
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass

    def _make_interval_action(self, minutes: int):  # type: ignore[no-untyped-def]
        def _action(*_args: Any) -> None:
            self.set_interval(minutes)

        return _action

    def _make_interval_check(self, minutes: int):  # type: ignore[no-untyped-def]
        def _checked(_item: Any) -> bool:
            return self.config.sync.interval_minutes == minutes

        return _checked

    def set_interval(self, minutes: int) -> None:
        """运行时修改同步间隔并持久化。"""
        from .config_writer import update_config

        self.config.sync.interval_minutes = minutes
        try:
            # 必须写回实际加载的那个配置文件，否则重启后设置丢失
            update_config({"sync": {"interval_minutes": minutes}}, self.config.source_path)
            logger.info("同步间隔已设为 %d 分钟", minutes)
        except Exception:  # noqa: BLE001
            logger.warning("同步间隔已生效，但写入配置文件失败", exc_info=True)

        # 重建 sync 任务
        self.scheduler.shutdown(wait=False)
        self.scheduler = SyncScheduler(
            self.context, cancel_token=self.cancel, on_new_mail=self._on_new_mail
        )
        self.scheduler.start()
        self._notify(f"同步间隔已改为 {minutes} 分钟")

    def _notify(self, message: str) -> None:
        self._status_text = message
        if self._icon is not None:
            try:
                # 标题必须是后端可编码的（X11 为 latin-1），不能直接拼中文
                self._icon.title = f"{tray_title()} - {message[:60]}"
                self._icon.notify(message, tray_title())
                return
            except Exception:  # noqa: BLE001
                logger.debug("托盘通知失败", exc_info=True)
        if PLYER_AVAILABLE:
            try:
                plyer_notification.notify(
                    title="邮件管理助手", message=message, app_name="邮件管理助手", timeout=5
                )
                return
            except Exception:  # noqa: BLE001
                logger.debug("plyer 通知失败", exc_info=True)
        logger.info("%s", message)


def run_tray(
    context: AppContext, *, with_api: bool = True, instance_guard: Any = None
) -> int:
    app = TrayApplication(context, with_api=with_api, instance_guard=instance_guard)
    return app.run()
