"""定时同步调度（§3.6）。

使用 APScheduler ``BackgroundScheduler``：

* ``sync`` 任务：每 ``sync.interval_minutes`` 分钟增量同步一次；
* ``reconcile`` 任务：每天全量比对一次 UID，处理网页端删除/移动；
* ``index`` 任务：把待索引邮件补齐（同步被中断时的补偿）；
* ``maintenance`` 任务：每天备份 + 数据库整理。

调度线程只负责触发，真正的耗时工作仍在工作线程内执行并检查取消令牌。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .cancellation import CancellationToken, get_cancellation_token
from .config import AppConfig
from .context import AppContext
from .models import utcnow

logger = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    APSCHEDULER_AVAILABLE = True
except Exception:  # noqa: BLE001 pragma: no cover
    BackgroundScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    IntervalTrigger = None  # type: ignore
    APSCHEDULER_AVAILABLE = False


class SyncScheduler:
    """同步任务调度器。"""

    def __init__(
        self,
        context: AppContext,
        *,
        cancel_token: CancellationToken | None = None,
        on_new_mail=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.context = context
        self.config: AppConfig = context.config
        self.cancel = cancel_token or get_cancellation_token()
        self.on_new_mail = on_new_mail
        self._scheduler = None
        self._last_backup: datetime | None = None

    # ------------------------------------------------------------------

    def start(self) -> None:
        if not APSCHEDULER_AVAILABLE:
            logger.error(
                "APScheduler 未安装，定时同步不可用。请执行：pip install APScheduler"
            )
            return
        if self._scheduler is not None:
            return

        self._scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={
                "coalesce": True,       # 错过的任务合并为一次
                "max_instances": 1,     # 同一任务不并发
                "misfire_grace_time": 300,
            },
        )

        interval = self.config.sync.interval_minutes
        self._scheduler.add_job(
            self._run_sync,
            IntervalTrigger(minutes=interval),
            id="sync",
            name=f"每 {interval} 分钟增量同步",
            next_run_time=utcnow() + timedelta(seconds=20),  # 启动后稍等再跑
        )

        self._scheduler.add_job(
            self._run_reconcile,
            CronTrigger(hour=3, minute=30),
            id="reconcile",
            name="每日全量 UID 比对",
        )

        self._scheduler.add_job(
            self._run_index,
            IntervalTrigger(minutes=max(interval, 15)),
            id="index",
            name="补齐待索引邮件",
        )

        self._scheduler.add_job(
            self._run_maintenance,
            CronTrigger(hour=4, minute=0),
            id="maintenance",
            name="每日备份与数据库整理",
        )

        self._scheduler.start()
        logger.info(
            "调度器已启动：同步间隔 %d 分钟，每日 03:30 全量比对，04:00 备份", interval
        )

    def shutdown(self, *, wait: bool = False) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=wait)
            logger.info("调度器已停止")
        except Exception:  # noqa: BLE001
            logger.debug("调度器停止异常", exc_info=True)
        finally:
            self._scheduler = None

    def jobs(self) -> list[dict[str, Any]]:
        if self._scheduler is None:
            return []
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in self._scheduler.get_jobs()
        ]

    def trigger_now(self, *, index: bool = True) -> None:
        """手动触发一次同步（不等待）。"""
        import threading

        threading.Thread(
            target=self._run_sync, kwargs={"index": index}, name="manual-sync", daemon=True
        ).start()

    # ------------------------------------------------------------------
    # 任务体
    # ------------------------------------------------------------------

    def _run_sync(self, index: bool = True) -> None:
        if self.cancel.cancelled:
            return
        if self.context.sync.is_running:
            logger.info("上一次同步尚未结束，跳过本轮")
            return
        logger.info("定时同步开始")
        try:
            result = self.context.sync.sync_all(index=index)
            payload = result.to_dict()
            logger.info(
                "定时同步结束：状态=%s 下载=%d 归档=%d 失败=%d 删除=%d",
                payload["status"],
                payload["fetched"],
                payload["archived"],
                payload["failed"],
                payload["deleted"],
            )
            if result.new_message_ids and self.on_new_mail is not None:
                try:
                    self.on_new_mail(result)
                except Exception:  # noqa: BLE001 - 通知失败不影响同步
                    logger.debug("新邮件回调失败", exc_info=True)
        except Exception:  # noqa: BLE001
            logger.exception("定时同步异常")

    def _run_reconcile(self) -> None:
        if self.cancel.cancelled:
            return
        logger.info("每日全量比对开始")
        try:
            self.context.sync.sync_all(full=True, index=False)
        except Exception:  # noqa: BLE001
            logger.exception("全量比对异常")

    def _run_index(self) -> None:
        if self.cancel.cancelled:
            return
        pending = self.context.indexer.count_pending()
        if not pending:
            return
        logger.info("补齐索引：%d 封待处理", pending)
        try:
            stats = self.context.indexer.index_pending()
            logger.info("补齐索引完成：%s", stats.to_dict())
        except Exception:  # noqa: BLE001
            logger.exception("补齐索引异常")

    def _run_maintenance(self) -> None:
        if self.cancel.cancelled:
            return
        try:
            path = self.context.backup()
            logger.info("每日备份完成：%s", path)
            self.context.db.optimize()
            self._last_backup = utcnow()
        except Exception:  # noqa: BLE001
            logger.exception("每日维护异常")
