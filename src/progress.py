"""同步进度状态：供界面实时显示。

同步跑在后台线程，界面需要看到"正在做什么、做到哪了"。这里用一个
线程安全的共享状态对象在两个世界之间传递信息，避免界面直接侵入同步逻辑。

设计要点
--------
* 计数与事件都在锁内更新，多线程下载时也不会串数。
* 事件历史用有界 ``deque``，长时间运行不会吃内存。
* 只保留**摘要级**信息（文件夹名、主题截断、计数），不缓存邮件正文。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import utcnow

#: 最多保留多少条事件供界面回看
MAX_EVENTS = 200

#: 单个事件字段的长度上限（主题等），避免状态对象无限膨胀
_MAX_TEXT = 120


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(slots=True)
class SyncProgress:
    """线程安全的同步进度快照。"""

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    running: bool = False
    phase: str = "idle"          # idle | connecting | scanning | fetching | indexing | done | error
    started_at: datetime | None = None
    finished_at: datetime | None = None

    # 当前文件夹与总体进度
    current_folder: str = ""
    folders_total: int = 0
    folders_done: int = 0

    # 当前文件夹内的进度（界面进度条用这个）
    folder_messages_total: int = 0
    folder_messages_done: int = 0

    # 本次运行的累计计数
    fetched: int = 0
    archived: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    indexed: int = 0

    # 已完成的文件夹结果
    folder_results: list[dict[str, Any]] = field(default_factory=list)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))

    last_error: str = ""
    last_result: dict[str, Any] | None = None
    #: 来自配置的并发数。空闲时也要能显示，否则界面上永远是 1。
    workers: int = 1

    # ------------------------------------------------------------------
    # 事件入口（由 SyncService 的回调驱动）
    # ------------------------------------------------------------------

    def handle(self, event: str, payload: dict[str, Any]) -> None:
        """处理 SyncService 上报的进度事件。"""
        with self._lock:
            if event == "run_start":
                self._start(payload)
            elif event == "folder_start":
                self.current_folder = str(payload.get("folder") or "")
                self.folders_total = int(payload.get("total") or 0)
                self._log(f"开始同步文件夹 {self.current_folder}")
            elif event == "fetch_plan":
                self.folder_messages_total = int(payload.get("to_fetch") or 0)
                self.folder_messages_done = 0
                self.phase = "fetching"
                self._log(
                    f"{payload.get('folder')}：远端 {payload.get('remote')} 封，"
                    f"待下载 {payload.get('to_fetch')} 封"
                )
            elif event == "message_archived":
                self.folder_messages_done += 1
                self.archived += 1
                self._log(
                    f"已归档 [{self.folder_messages_done}/{self.folder_messages_total}] "
                    f"{payload.get('subject')}",
                    folder=payload.get("folder"),
                    level="item",
                )
            elif event == "message_failed":
                self.folder_messages_done += 1
                self.failed += 1
                self._log(f"失败：{payload.get('error')}", level="error")
            elif event == "folder_done":
                self.folders_done += 1
                self.folder_results.append(
                    {k: payload.get(k) for k in
                     ("folder", "status", "fetched", "archived", "skipped",
                      "failed", "deleted", "new_messages", "error_summary")}
                )
                self._log(
                    f"文件夹 {payload.get('folder')} 完成："
                    f"归档 {payload.get('archived')} 失败 {payload.get('failed')}",
                    level="ok" if not payload.get("failed") else "warn",
                )
            elif event == "index_start":
                self.phase = "indexing"
                self._log(f"开始建立索引，待处理 {payload.get('pending')} 封")
            elif event == "index_done":
                self.indexed = int(payload.get("chunks") or 0)
                self._log(
                    f"索引完成：{payload.get('chunks')} 切片"
                    f"（新嵌入 {payload.get('embedded')}，复用 {payload.get('reused')}）"
                )
            elif event == "error":
                self.last_error = _clip(payload.get("error"))
                self.phase = "error"
                self._log(f"错误：{self.last_error}", level="error")
            elif event == "run_done":
                self._finish(payload)

    def mark_running(self, *, workers: int = 1) -> None:
        with self._lock:
            self.running = True
            self.phase = "connecting"
            self.started_at = utcnow()
            self.finished_at = None
            self.current_folder = ""
            self.folders_total = self.folders_done = 0
            self.folder_messages_total = self.folder_messages_done = 0
            self.fetched = self.archived = self.skipped = 0
            self.failed = self.deleted = self.indexed = 0
            self.folder_results = []
            self.events.clear()
            self.last_error = ""
            self.workers = workers
            self._log("开始同步" + (f"（{workers} 个并发连接）" if workers > 1 else ""))

    def _start(self, payload: dict[str, Any]) -> None:
        self.mark_running(workers=int(payload.get("workers") or self.workers))

    def _finish(self, payload: dict[str, Any]) -> None:
        self.running = False
        # partial = 个别文件夹失败但其余正常，不该显示成刺眼的"出错"
        if payload.get("status") == "success":
            self.phase = "done"
        elif payload.get("status") == "partial":
            self.phase = "partial"
        else:
            self.phase = "error"
        self.finished_at = utcnow()
        self.current_folder = ""
        self.last_result = payload or None
        self.last_error = _clip(payload.get("error_summary"))
        self._log(
            "同步结束：状态 "
            f"{payload.get('status')}，归档 {payload.get('archived')} 封"
            + (f"，错误：{self.last_error}" if self.last_error else ""),
            level="ok" if payload.get("status") == "success" else "warn",
        )

    def _log(self, message: str, *, level: str = "info", **extra: Any) -> None:
        self.events.append(
            {
                "at": utcnow().isoformat(timespec="seconds"),
                "level": level,
                "message": _clip(message, 200),
                **extra,
            }
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def snapshot(self, *, event_limit: int = 60) -> dict[str, Any]:
        """给界面的只读快照。"""
        with self._lock:
            total = self.folder_messages_total
            done = self.folder_messages_done
            percent = round(done * 100 / total, 1) if total else (100.0 if self.folders_done else 0.0)
            elapsed = (
                (self.finished_at or utcnow()) - self.started_at
            ).total_seconds() if self.started_at else 0.0
            rate = round(done / elapsed, 2) if elapsed > 0.5 and done else 0.0
            eta = round((total - done) / rate, 1) if rate > 0 and total > done else None

            events = list(self.events)[-event_limit:]
            return {
                "running": self.running,
                "phase": self.phase,
                "workers": self.workers,
                "current_folder": self.current_folder,
                "folders_total": self.folders_total,
                "folders_done": self.folders_done,
                "folder_messages_total": total,
                "folder_messages_done": done,
                "percent": percent,
                "elapsed_seconds": round(elapsed, 1),
                "rate_per_second": rate,
                "eta_seconds": eta,
                "fetched": self.fetched,
                "archived": self.archived,
                "skipped": self.skipped,
                "failed": self.failed,
                "deleted": self.deleted,
                "indexed": self.indexed,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "last_error": self.last_error,
                "last_result": self.last_result,
                "folder_results": list(self.folder_results),
                "events": events,
            }
