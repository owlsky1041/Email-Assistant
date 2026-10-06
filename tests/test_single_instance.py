"""单实例约束的测试。

回归背景
--------
Windows 上双击托盘图标 / 右键「打开主窗口」时又拉起一个完整客户端，
两个进程各自持有数据库、向量库与 API 端口，结果是程序崩溃。
需求是**同时只能运行一个客户端**：第二次启动应把意图交给已在运行的实例。

这里覆盖两件容易写错的事：
1. 排他锁真的排他（同进程内两次独立打开也要冲突）；
2. 控制通道真的能把动作送到主实例，且**缺令牌会被拒**。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

from src.single_instance import (
    APP_LOCK_NAME,
    WINDOW_LOCK_NAME,
    ControlServer,
    SingleInstance,
    send_to_running,
)


class TestFileLock:
    def test_second_acquire_fails(self, tmp_path: Path) -> None:
        first = SingleInstance(tmp_path / APP_LOCK_NAME)
        second = SingleInstance(tmp_path / APP_LOCK_NAME)
        try:
            assert first.acquire() is True
            assert second.acquire() is False, "第二个实例不该拿到锁"
            assert second.is_primary is False
        finally:
            second.close()
            first.close()

    def test_lock_released_allows_next_instance(self, tmp_path: Path) -> None:
        first = SingleInstance(tmp_path / APP_LOCK_NAME)
        assert first.acquire()
        first.close()

        second = SingleInstance(tmp_path / APP_LOCK_NAME)
        try:
            assert second.acquire() is True, "上一个实例退出后应能拿到锁"
        finally:
            second.close()

    def test_window_lock_is_separate_from_app_lock(self, tmp_path: Path) -> None:
        """窗口锁与应用锁互不影响：托盘持有应用锁时，窗口助手仍要能起来。"""
        app_lock = SingleInstance(tmp_path / APP_LOCK_NAME)
        window_lock = SingleInstance(tmp_path / WINDOW_LOCK_NAME)
        try:
            assert app_lock.acquire()
            assert window_lock.acquire() is True, (
                "窗口锁必须是独立的一把 —— 否则托盘派生的主窗口会被自己挡住"
            )
        finally:
            window_lock.close()
            app_lock.close()

    def test_second_window_is_refused(self, tmp_path: Path) -> None:
        first = SingleInstance(tmp_path / WINDOW_LOCK_NAME)
        second = SingleInstance(tmp_path / WINDOW_LOCK_NAME)
        try:
            assert first.acquire()
            assert second.acquire() is False, "同时只允许一个主窗口"
        finally:
            second.close()
            first.close()

    def test_acquire_is_idempotent(self, tmp_path: Path) -> None:
        guard = SingleInstance(tmp_path / APP_LOCK_NAME)
        try:
            assert guard.acquire()
            assert guard.acquire() is True
        finally:
            guard.close()


class TestControlChannel:
    def test_dispatch_reaches_handler(self, tmp_path: Path) -> None:
        seen: list[str] = []
        event = threading.Event()

        def handler(action: str) -> dict:
            seen.append(action)
            event.set()
            return {"ok": True, "action": action}

        guard = SingleInstance(tmp_path / APP_LOCK_NAME)
        assert guard.acquire()
        try:
            guard.serve(handler)
            result = send_to_running(tmp_path / APP_LOCK_NAME, "open-main")
            assert result.get("ok") is True, result
            assert event.wait(3.0), "主实例没有收到动作"
            assert seen == ["open-main"]
        finally:
            guard.close()

    def test_ping_and_unknown_action(self, tmp_path: Path) -> None:
        guard = SingleInstance(tmp_path / APP_LOCK_NAME)
        assert guard.acquire()
        try:
            guard.serve(lambda a: {"ok": a == "ping"})
            assert send_to_running(tmp_path / APP_LOCK_NAME, "ping")["ok"] is True
            assert send_to_running(tmp_path / APP_LOCK_NAME, "nonsense")["ok"] is False
        finally:
            guard.close()

    def test_handler_exception_becomes_error_reply(self, tmp_path: Path) -> None:
        """动作抛异常不该把监听线程弄挂，而要回一个错误。"""
        def boom(_action: str) -> dict:
            raise RuntimeError("模拟失败")

        guard = SingleInstance(tmp_path / APP_LOCK_NAME)
        assert guard.acquire()
        try:
            guard.serve(boom)
            reply = send_to_running(tmp_path / APP_LOCK_NAME, "open-main")
            assert reply["ok"] is False
            assert "模拟失败" in reply["error"]
        finally:
            guard.close()

    def test_wrong_token_is_rejected(self, tmp_path: Path) -> None:
        """锁文件里的令牌是防同机其它用户乱发指令的，必须校验。"""
        called: list[str] = []
        guard = SingleInstance(tmp_path / APP_LOCK_NAME)
        assert guard.acquire()
        try:
            guard.serve(lambda a: called.append(a) or {"ok": True})
            meta = json.loads((tmp_path / APP_LOCK_NAME).read_text(encoding="utf-8"))
            port = int(meta["port"])

            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall((json.dumps({"action": "open-main", "token": "错误令牌"}) + "\n").encode())
                reply = json.loads(sock.recv(4096).decode("utf-8").strip())
            assert reply["ok"] is False
            assert "unauthorized" in reply["error"]
            assert called == [], "令牌不对时不该执行动作"
        finally:
            guard.close()

    def test_missing_port_file_reports_cleanly(self, tmp_path: Path) -> None:
        result = send_to_running(tmp_path / "根本没这个锁", "open-main")
        assert result["ok"] is False
        assert "控制端口" in result["error"]

    def test_stale_port_does_not_hang(self, tmp_path: Path) -> None:
        """锁文件里写着一个没人监听的端口时，要快速失败而不是卡住。"""
        lock = tmp_path / APP_LOCK_NAME
        # 拿一个必然空闲的端口
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        lock.write_text(json.dumps({"port": dead_port, "token": "x"}), encoding="utf-8")

        start = time.time()
        result = send_to_running(lock, "open-main", timeout=1.0)
        elapsed = time.time() - start
        assert result["ok"] is False
        assert elapsed < 5.0, f"失败得太慢：{elapsed:.1f}s"


class TestHandOff:
    def test_hand_off_when_nothing_running(self, tmp_path: Path) -> None:
        guard = SingleInstance(tmp_path / APP_LOCK_NAME)
        result = guard.hand_off("open-main")
        assert result["ok"] is False
