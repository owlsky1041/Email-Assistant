"""同步进度与并发下载测试。

并发下载的关键约束是 **IMAP 连接不能跨线程共享**，
因此除了功能正确性，还要验证"每线程独立连接"这一性质。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import src.sync_service as sync_module
from src.context import AppContext
from src.progress import SyncProgress
from src.sync_service import _ClientPool
from tests.conftest import FakeImapClient, build_eml


# ---------------------------------------------------------------------------
# 进度状态
# ---------------------------------------------------------------------------

class TestSyncProgress:
    def test_initial_snapshot(self) -> None:
        snap = SyncProgress().snapshot()
        assert snap["running"] is False
        assert snap["phase"] == "idle"
        assert snap["percent"] == 0.0
        assert snap["events"] == []

    def test_run_lifecycle(self) -> None:
        p = SyncProgress()
        p.handle("run_start", {"workers": 3})
        assert p.snapshot()["running"] is True
        assert p.snapshot()["workers"] == 3

        p.handle("run_done", {"status": "success", "archived": 5})
        snap = p.snapshot()
        assert snap["running"] is False
        assert snap["phase"] == "done"
        assert snap["finished_at"]

    def test_fetch_plan_sets_total(self) -> None:
        p = SyncProgress()
        p.handle("run_start", {})
        p.handle("fetch_plan", {"folder": "INBOX", "remote": 10, "to_fetch": 8})
        snap = p.snapshot()
        assert snap["folder_messages_total"] == 8
        assert snap["phase"] == "fetching"

    def test_message_archived_increments(self) -> None:
        p = SyncProgress()
        p.handle("run_start", {})
        p.handle("fetch_plan", {"to_fetch": 4})
        for i in range(3):
            p.handle("message_archived", {"subject": f"主题{i}", "folder": "INBOX"})
        snap = p.snapshot()
        assert snap["archived"] == 3
        assert snap["folder_messages_done"] == 3
        assert snap["percent"] == 75.0

    def test_failed_increments_and_logs_error(self) -> None:
        p = SyncProgress()
        p.handle("run_start", {})
        p.handle("fetch_plan", {"to_fetch": 2})
        p.handle("message_failed", {"error": "uid=1: timeout"})
        snap = p.snapshot()
        assert snap["failed"] == 1
        assert any(e["level"] == "error" for e in snap["events"])

    def test_percent_handles_zero_total(self) -> None:
        p = SyncProgress()
        p.handle("run_start", {})
        assert p.snapshot()["percent"] == 0.0  # 不能除零

    def test_folder_done_collected(self) -> None:
        p = SyncProgress()
        p.handle("run_start", {})
        p.handle("folder_done", {"folder": "INBOX", "status": "success", "archived": 3})
        snap = p.snapshot()
        assert snap["folders_done"] == 1
        assert snap["folder_results"][0]["folder"] == "INBOX"

    def test_error_summary_sets_error_phase(self) -> None:
        p = SyncProgress()
        p.handle("run_start", {})
        p.handle("run_done", {"status": "failed", "error_summary": "认证失败"})
        snap = p.snapshot()
        assert snap["phase"] == "error"
        assert "认证失败" in snap["last_error"]

    def test_events_are_bounded(self) -> None:
        """长时间运行不能无限堆积事件。"""
        p = SyncProgress()
        p.handle("run_start", {})
        for i in range(1000):
            p.handle("message_archived", {"subject": f"第{i}封"})
        assert len(p.snapshot(event_limit=10_000)["events"]) <= 200

    def test_event_text_is_truncated(self) -> None:
        p = SyncProgress()
        p.handle("run_start", {})
        p.handle("message_archived", {"subject": "超长主题" * 200})
        event = p.snapshot()["events"][-1]
        assert len(event["message"]) <= 220

    def test_thread_safety(self) -> None:
        """并发上报不应丢计数或抛异常。"""
        p = SyncProgress()
        p.handle("run_start", {})
        p.handle("fetch_plan", {"to_fetch": 800})
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for i in range(100):
                    p.handle("message_archived", {"subject": f"s{i}"})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert p.snapshot()["archived"] == 800

    def test_snapshot_isolated_from_state(self) -> None:
        """快照必须是副本，外部修改不应影响内部状态。"""
        p = SyncProgress()
        p.handle("run_start", {})
        snap = p.snapshot()
        snap["folder_results"].append({"folder": "伪造"})
        assert p.snapshot()["folder_results"] == []


# ---------------------------------------------------------------------------
# 并发下载
# ---------------------------------------------------------------------------

def make_mailbox(count: int, *, folder: str = "INBOX") -> dict[str, dict[str, bytes]]:
    return {
        folder: {
            str(i): build_eml(
                subject=f"邮件{i}",
                text=f"第 {i} 封正文内容。" * 5,
                message_id=f"<parallel-{i}@corp.com>",
            )
            for i in range(1, count + 1)
        }
    }


class TestParallelDownload:
    def test_all_messages_archived(self, context: AppContext, monkeypatch) -> None:
        mailbox = make_mailbox(30)
        client = FakeImapClient(mailbox)
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: client)

        context.config.sync.fetch_workers = 4
        context.config.sync.fetch_batch_size = 30
        result = context.sync.sync_all(index=False)

        assert result.archived == 30
        assert result.failed == 0
        assert context.db.count_messages() == 30

    def test_no_duplicates_or_loss(self, context: AppContext, monkeypatch) -> None:
        mailbox = make_mailbox(50)
        client = FakeImapClient(mailbox)
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: client)

        context.config.sync.fetch_workers = 5
        context.config.sync.fetch_batch_size = 25
        context.sync.sync_all(index=False)

        rows = context.db.query("SELECT uid FROM messages ORDER BY CAST(uid AS INTEGER)")
        uids = [r["uid"] for r in rows]
        assert len(uids) == 50
        assert len(set(uids)) == 50, "并发下载出现了重复归档"

    def test_each_thread_gets_its_own_connection(
        self, context: AppContext, monkeypatch
    ) -> None:
        """核心约束：imaplib 连接不能跨线程共享。"""
        created: list[tuple[int, FakeImapClient]] = []
        lock = threading.Lock()

        def factory(*args, **kwargs):
            c = FakeImapClient(make_mailbox(40))
            with lock:
                created.append((threading.get_ident(), c))
            return c

        monkeypatch.setattr(sync_module, "ImapClient", factory)
        context.config.sync.fetch_workers = 4
        context.config.sync.fetch_batch_size = 40
        context.sync.sync_all(index=False)

        # 每个线程最多创建一条连接，且不同线程的连接互不相同
        by_thread: dict[int, list[FakeImapClient]] = {}
        for tid, c in created:
            by_thread.setdefault(tid, []).append(c)
        assert len(created) >= 2, "并发模式下应创建多条连接"
        for tid, clients in by_thread.items():
            assert len(clients) == 1, f"线程 {tid} 创建了多条连接"

    def test_workers_one_uses_single_connection(
        self, context: AppContext, monkeypatch
    ) -> None:
        calls: list[int] = []

        def factory(*args, **kwargs):
            calls.append(1)
            return FakeImapClient(make_mailbox(10))

        monkeypatch.setattr(sync_module, "ImapClient", factory)
        context.config.sync.fetch_workers = 1
        result = context.sync.sync_all(index=False)
        assert result.archived == 10
        assert len(calls) == 1, "顺序模式下不应额外建连"

    def test_failure_isolated_per_message(self, context: AppContext, monkeypatch) -> None:
        mailbox = make_mailbox(20)
        client = FakeImapClient(mailbox, fail_uids={"7", "13"})
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: client)

        context.config.sync.fetch_workers = 4
        result = context.sync.sync_all(index=False)
        assert result.archived == 18
        assert result.failed == 2
        assert context.db.count_messages() == 18

    def test_connection_failure_does_not_kill_batch(
        self, context: AppContext, monkeypatch
    ) -> None:
        """某个线程建连失败时，其余邮件仍应正常归档。"""
        state = {"n": 0}

        def factory(*args, **kwargs):
            state["n"] += 1
            if state["n"] == 2:
                raise OSError("模拟连接被拒")
            return FakeImapClient(make_mailbox(12))

        monkeypatch.setattr(sync_module, "ImapClient", factory)
        context.config.sync.fetch_workers = 3
        result = context.sync.sync_all(index=False)
        assert result.archived >= 1
        assert result.failed >= 1

    def test_progress_reports_workers(self, context: AppContext, monkeypatch) -> None:
        client = FakeImapClient(make_mailbox(10))
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: client)
        context.config.sync.fetch_workers = 3
        context.sync.sync_all(index=False)

        snap = context.progress.snapshot()
        assert snap["workers"] == 3
        assert snap["archived"] == 10
        assert snap["running"] is False
        assert snap["phase"] == "done"

    def test_progress_events_recorded(self, context: AppContext, monkeypatch) -> None:
        client = FakeImapClient(make_mailbox(6))
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: client)
        context.config.sync.fetch_workers = 2
        context.sync.sync_all(index=False)

        events = context.progress.snapshot()["events"]
        assert any("开始同步" in e["message"] for e in events)
        assert any("同步结束" in e["message"] for e in events)
        assert any(e["level"] == "item" for e in events)

    def test_watermark_correct_under_parallelism(
        self, context: AppContext, monkeypatch
    ) -> None:
        """并发下水位仍须推进到最高已处理 UID，保证断点续传。"""
        mailbox = make_mailbox(24)
        client = FakeImapClient(mailbox)
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: client)

        context.config.sync.fetch_workers = 4
        context.config.sync.fetch_batch_size = 24
        context.sync.sync_all(index=False)

        state = context.db.get_folder("tester@corp.com", "INBOX")
        assert state is not None
        assert state.last_uid == 24

    def test_budget_truncation_with_workers(self, context: AppContext, monkeypatch) -> None:
        """并发 + 单次上限：水位只能停在实际处理过的 UID 上。"""
        mailbox = make_mailbox(20)
        client = FakeImapClient(mailbox)
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: client)

        context.config.sync.fetch_workers = 4
        context.config.sync.fetch_batch_size = 20
        context.config.sync.max_messages_per_run = 8
        result = context.sync.sync_all(index=False)

        assert result.archived == 8
        state = context.db.get_folder("tester@corp.com", "INBOX")
        assert state.last_uid == 8, "水位不能越过未处理的邮件"

        # 下一轮应继续，不丢邮件
        result2 = context.sync.sync_all(index=False)
        assert result2.archived == 8
        assert context.db.count_messages() == 16


class TestClientPool:
    def test_reuses_connection_within_thread(self, context: AppContext, monkeypatch) -> None:
        created: list[FakeImapClient] = []

        def factory(*args, **kwargs):
            c = FakeImapClient(make_mailbox(2))
            created.append(c)
            return c

        monkeypatch.setattr(sync_module, "ImapClient", factory)
        pool = _ClientPool(context.config, "code", context.cancel)
        try:
            first = pool.get()
            again = pool.get()
            assert first is again
            assert len(created) == 1
        finally:
            pool.close_all()

    def test_different_threads_get_different_connections(
        self, context: AppContext, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            sync_module, "ImapClient", lambda *a, **kw: FakeImapClient(make_mailbox(2))
        )
        pool = _ClientPool(context.config, "code", context.cancel)
        seen: dict[int, object] = {}
        try:
            def grab() -> None:
                seen[threading.get_ident()] = pool.get()

            threads = [threading.Thread(target=grab) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            assert len(seen) == 3
            assert len({id(v) for v in seen.values()}) == 3
        finally:
            pool.close_all()

    def test_close_all_is_idempotent(self, context: AppContext, monkeypatch) -> None:
        monkeypatch.setattr(
            sync_module, "ImapClient", lambda *a, **kw: FakeImapClient(make_mailbox(1))
        )
        pool = _ClientPool(context.config, "code", context.cancel)
        pool.get()
        pool.close_all()
        pool.close_all()  # 不应抛异常


class TestIdleSnapshot:
    def test_reports_configured_workers_when_idle(self, context: AppContext) -> None:
        """空闲时也应显示配置的并发数，而不是恒为 1。"""
        context.config.sync.fetch_workers = 5
        from src.progress import SyncProgress

        assert SyncProgress(workers=5).snapshot()["workers"] == 5

    def test_context_seeds_workers_from_config(self, tmp_config) -> None:
        from src.context import AppContext

        tmp_config.sync.fetch_workers = 4
        ctx = AppContext(tmp_config, configure_logging=False)
        try:
            assert ctx.progress.snapshot()["workers"] == 4
        finally:
            ctx.close()
