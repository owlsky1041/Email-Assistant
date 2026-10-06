"""单实例约束：同时只允许运行一个客户端。

为什么需要
----------
双击托盘图标 / 右键「打开主窗口」时，如果又拉起一个完整客户端，就会出现
两个进程各自持有数据库、向量库与 API 端口。ChromaDB 这类组件对同目录
多进程访问很敏感，Windows 上表现为直接崩溃。

做法
----
* 应用进程（tray / app / 双击启动）启动时抢一把**排他文件锁**
  （Windows ``msvcrt.locking`` / POSIX ``flock``）。抢不到说明已有实例在跑。
* 抢锁成功的一方在回环地址上开一个极小的**控制端口**，把端口与一次性
  令牌写进锁文件。第二次启动读出来、连上去，把想做的事情（打开主窗口）
  交给已在运行的实例，自己立刻退出 —— 用户看到的是"窗口被打开"，
  而不是"多了一个客户端"。
* 令牌只写在本用户可读的锁文件里，防止同机其它用户乱发指令。

主窗口另有一把 `.mainwindow.lock`：同时只允许一个主窗口。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

APP_LOCK_NAME = ".app.lock"
WINDOW_LOCK_NAME = ".mainwindow.lock"
CONTROL_TIMEOUT = 3.0


class _FileLock:
    """跨平台的排他文件锁。

    Windows 用 ``msvcrt.locking``，POSIX 用 ``fcntl.flock``。
    两者都是**按打开的文件描述符**加锁：同一进程里两次独立打开也会互相
    冲突，因此可以在同进程内测试。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        """尝试加锁；已被别人持有返回 ``False``。"""
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = open(self.path, "a+", encoding="utf-8")
        except OSError as exc:
            logger.warning("无法打开锁文件 %s：%s", self.path, exc)
            return False
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.debug("释放锁失败", exc_info=True)
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

def meta_path_for(lock_path: Path) -> Path:
    """锁文件对应的元数据文件。

    为什么**不写进锁文件本身**：Windows 的 ``msvcrt.locking`` 锁的是一个
    字节区间，对该区间做 ``truncate``/写入在部分环境会失败 —— 表现是
    "锁住了但端口写不进去"，于是第二次启动永远找不到正在运行的实例。
    锁与元数据分成两个文件，互不干扰。
    """
    return Path(str(lock_path) + ".json")


def write_meta(lock_path: Path, data: dict[str, Any]) -> None:
    """写入端口/令牌（原子替换，权限 0600）。"""
    target = meta_path_for(lock_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError:
        logger.debug("写入实例元数据失败", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_meta(lock_path: Path) -> dict[str, Any]:
    """读取端口/令牌；不存在或损坏时返回空字典。"""
    try:
        text = meta_path_for(lock_path).read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class ControlServer:
    """回环地址上的极简控制通道：一行 JSON 进，一行 JSON 出。"""

    def __init__(self, handler: Callable[[str], dict[str, Any]]) -> None:
        self._handler = handler
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.port = 0
        self.token = ""
        self._lock_path: Path | None = None

    def start(self, *, lock_path: Path | None = None) -> int:
        import secrets

        self.token = secrets.token_urlsafe(24)
        self._lock_path = Path(lock_path) if lock_path is not None else None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        sock.settimeout(0.5)
        self._sock = sock
        self.port = int(sock.getsockname()[1])

        self._thread = threading.Thread(target=self._serve, name="instance-control", daemon=True)
        self._thread.start()

        if self._lock_path is not None:
            write_meta(
                self._lock_path,
                {"port": self.port, "token": self.token, "pid": os.getpid()},
            )
        logger.debug("实例控制通道已监听 127.0.0.1:%s", self.port)
        return self.port

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(conn)
            except Exception:  # noqa: BLE001 - 单个请求出错不该拖垮监听
                logger.debug("处理控制请求失败", exc_info=True)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(CONTROL_TIMEOUT)
        chunks: list[bytes] = []
        while True:
            try:
                block = conn.recv(4096)
            except (socket.timeout, OSError):
                break
            if not block:
                break
            chunks.append(block)
            if b"\n" in block:
                break
        raw = b"".join(chunks).decode("utf-8", "replace").strip()
        if not raw:
            return
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            conn.sendall(b'{"ok":false,"error":"bad json"}\n')
            return
        if not isinstance(request, dict) or request.get("token") != self.token:
            conn.sendall(b'{"ok":false,"error":"unauthorized"}\n')
            return
        action = str(request.get("action") or "")
        try:
            reply = self._handler(action)
        except Exception as exc:  # noqa: BLE001
            logger.exception("控制动作执行失败：%s", action)
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def send_to_running(lock_path: Path, action: str, *, timeout: float = CONTROL_TIMEOUT) -> dict[str, Any]:
    """把动作交给已在运行的实例。

    :return: ``{"ok": bool, ...}``；连不上时 ``ok`` 为 ``False``。
    """
    meta = read_meta(lock_path)
    port = meta.get("port")
    token = meta.get("token")
    if not port or not token:
        return {"ok": False, "error": "没有可用的控制端口（锁文件缺少信息）"}
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as sock:
            payload = json.dumps({"action": action, "token": token})
            sock.sendall((payload + "\n").encode("utf-8"))
            sock.settimeout(timeout)
            data = sock.recv(4096)
    except OSError as exc:
        return {"ok": False, "error": f"无法连接正在运行的实例：{exc}"}
    try:
        return json.loads(data.decode("utf-8", "replace").strip() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "响应不是合法 JSON"}


class SingleInstance:
    """应用级单实例守卫。

    用法::

        guard = SingleInstance(lock_path)
        if not guard.acquire():
            guard.hand_off("open-main")   # 交给已在运行的实例
            return 0
        guard.serve(handler)              # 本进程是主实例
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self._lock = _FileLock(self.lock_path)
        self._server: ControlServer | None = None
        self.is_primary = False

    def acquire(self) -> bool:
        self.is_primary = self._lock.acquire()
        if not self.is_primary:
            logger.info("已有实例在运行（锁：%s）", self.lock_path)
        return self.is_primary

    def hand_off(self, action: str) -> dict[str, Any]:
        """把动作交给主实例；顺手清理陈旧锁文件。"""
        result = send_to_running(self.lock_path, action)
        if not result.get("ok"):
            logger.warning("无法把「%s」交给已运行的实例：%s", action, result.get("error"))
        return result

    def serve(self, handler: Callable[[str], dict[str, Any]]) -> int:
        """作为主实例开始接受控制指令，返回监听端口。"""
        self._server = ControlServer(handler)
        return self._server.start(lock_path=self.lock_path)

    def close(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
        # 元数据一并清掉：留着过期的端口只会让下一次启动白等一次连接超时
        try:
            meta_path_for(self.lock_path).unlink(missing_ok=True)
        except OSError:
            pass
        self._lock.release()
        self.is_primary = False
