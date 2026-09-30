"""优雅退出支持（§11.4）。

所有耗时任务（IMAP 拉取、嵌入推理、批量入库）都必须周期性检查取消令牌，
在收到退出信号时尽快中断，确保 SQLite 事务提交、``.tmp`` 文件被清理。
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)


class CancelledError(Exception):
    """任务被请求取消。"""


class CancellationToken:
    """线程安全的一次性取消令牌。"""

    __slots__ = ("_event", "_callbacks", "_lock", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._reason: str | None = None

    # ---- 状态 ----
    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    # ---- 触发 ----
    def cancel(self, reason: str = "用户请求退出") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()
            callbacks = list(self._callbacks)
        logger.info("收到取消信号：%s", reason)
        for cb in callbacks:
            try:
                cb()
            except Exception:  # noqa: BLE001 - 回调失败不得阻断退出流程
                logger.exception("取消回调执行失败")

    # ---- 注册 ----
    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """注册取消回调；若已取消则立即执行。返回反注册函数。"""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)

                def _unregister() -> None:
                    with self._lock:
                        if callback in self._callbacks:
                            self._callbacks.remove(callback)

                return _unregister
        try:
            callback()
        except Exception:  # noqa: BLE001
            logger.exception("取消回调执行失败")

        def _noop() -> None:
            return None

        return _noop

    # ---- 检查 ----
    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError(self._reason or "任务已取消")

    def wait(self, timeout: float) -> bool:
        """可中断的 sleep：返回 True 表示被取消。"""
        return self._event.wait(timeout)


# ---------------------------------------------------------------------------
# 全局令牌
# ---------------------------------------------------------------------------

_global_token: CancellationToken | None = None
_global_lock = threading.Lock()


def get_cancellation_token() -> CancellationToken:
    global _global_token
    with _global_lock:
        if _global_token is None:
            _global_token = CancellationToken()
        return _global_token


def reset_cancellation_token() -> CancellationToken:
    """测试用：重置全局令牌。"""
    global _global_token
    with _global_lock:
        _global_token = CancellationToken()
        return _global_token


_installed = False


def install_signal_handlers(token: CancellationToken | None = None) -> CancellationToken:
    """注册 SIGINT / SIGTERM 处理器，触发全局取消令牌。

    Windows 下 ``SIGTERM`` 支持有限，额外依赖 ``KeyboardInterrupt`` 兜底。
    """
    global _installed
    token = token or get_cancellation_token()
    if _installed:
        return token
    _installed = True

    def _handler(signum, _frame):  # type: ignore[no-untyped-def]
        token.cancel(f"收到系统信号 {signum}")

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            # 非主线程或平台不支持时静默跳过
            logger.debug("无法注册信号处理器 %s", sig_name)
    return token
