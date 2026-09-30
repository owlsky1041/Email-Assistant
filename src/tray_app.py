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


def has_display() -> bool:
    """判断当前环境是否具备图形界面。"""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


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
    ) -> None:
        self.context = context
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
        self.scheduler.start()
        if self.with_api:
            self.start_api_process()

        usable = self.config.tray.enabled and TRAY_AVAILABLE and has_display()
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
        return self._run_tray()

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
            pystray.MenuItem("立即同步", self._action_sync, default=True),
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

        self._icon = pystray.Icon(
            "email-assistant",
            icon=build_icon_image(),
            title="邮件管理助手",
            menu=menu,
        )
        try:
            self._icon.run()
        except Exception:  # noqa: BLE001
            logger.exception("托盘运行失败，降级为守护模式")
            return self._run_headless()
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        logger.info("正在退出…")
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
                self._icon.title = f"邮件管理助手 —— {message[:60]}"
                self._icon.notify(message, "邮件管理助手")
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


def run_tray(context: AppContext, *, with_api: bool = True) -> int:
    app = TrayApplication(context, with_api=with_api)
    return app.run()
