"""同步服务测试（§3.1 / §11.2）。

用假 IMAP 客户端完整验证：增量下载、重复运行不重复下载、水位推进、
UIDVALIDITY 变化、超大附件保护、全量比对、单封失败隔离、同步日志。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.sync_service as sync_module
from src.context import AppContext
from src.imap_client import PartInfo
from src.models import utcnow
from tests.conftest import FakeImapClient, build_eml


@pytest.fixture
def patch_imap(monkeypatch: pytest.MonkeyPatch):
    """把 SyncService 使用的 ImapClient 替换为假实现。"""
    holder: dict[str, FakeImapClient] = {}

    def _install(client: FakeImapClient) -> FakeImapClient:
        holder["client"] = client
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: client)
        return client

    return _install


class TestInitialSync:
    def test_archives_every_message(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        result = context.sync.sync_all()

        assert result.status in ("success", "partial")
        assert result.archived == 4
        assert result.failed == 0
        assert context.db.count_messages() == 4

    def test_creates_markdown_files(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()

        files = list(context.config.archive_path.rglob("*.md"))
        assert len(files) == 4
        for path in files:
            text = path.read_text(encoding="utf-8")
            assert text.startswith("---\n")
            assert "message_id:" in text

    def test_folder_structure_preserved(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        """§3.2 保持邮箱原始文件夹结构。"""
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()

        dirs = {p.name for p in context.config.archive_path.rglob("*") if p.is_dir()}
        assert "INBOX" in dirs
        assert "Sent" in dirs

    def test_attachment_written_to_disk(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()

        attachments = [
            r for r in context.db.query("SELECT * FROM attachments WHERE downloaded = 1")
        ]
        assert len(attachments) == 1
        path = Path(attachments[0]["local_path"])
        assert path.is_file()
        assert path.parent.name == "attachments"
        assert path.stat().st_size == attachments[0]["size_bytes"]

    def test_sync_log_written(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()

        logs = context.db.recent_sync_logs()
        assert len(logs) == 2  # INBOX + Sent
        assert all(log["status"] in ("success", "partial") for log in logs)
        assert all(log["finished_at"] for log in logs)

    def test_folder_watermarks_set(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()

        inbox = context.db.get_folder("tester@corp.com", "INBOX")
        assert inbox is not None
        assert inbox.last_uid == 3
        assert inbox.uidvalidity == 1
        assert inbox.last_sync_at is not None

    def test_indexes_after_sync(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all(index=True)
        assert context.db.count_chunks() > 0
        assert context.indexer.count_pending() == 0

    def test_no_index_flag(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all(index=False)
        assert context.db.count_messages() == 4
        assert context.db.count_chunks() == 0


class TestIdempotency:
    def test_second_run_downloads_nothing(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        """验收标准：重复运行不会重复下载同一封邮件。"""
        client = patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        first_fetches = len([c for c in client.calls if c[0] == "fetch"])

        client.calls.clear()
        result = context.sync.sync_all()
        second_fetches = len([c for c in client.calls if c[0] == "fetch"])

        assert first_fetches == 4
        assert second_fetches == 0, "第二次同步不应重新下载任何邮件"
        assert result.archived == 0
        assert context.db.count_messages() == 4

    def test_no_duplicate_files(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        context.sync.sync_all()
        assert len(list(context.config.archive_path.rglob("*.md"))) == 4


class TestIncremental:
    def test_only_new_message_downloaded(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        client = patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()

        # 服务端新增一封
        fake_mailbox["INBOX"]["4"] = build_eml(
            subject="新增的邮件", text="这是后来才收到的新邮件。", message_id="<inbox-4@corp.com>"
        )
        client.calls.clear()
        result = context.sync.sync_all()

        fetched = [c for c in client.calls if c[0] == "fetch"]
        assert len(fetched) == 1
        assert result.archived == 1
        assert context.db.count_messages() == 5

    def test_watermark_advances(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        assert context.db.get_folder("tester@corp.com", "INBOX").last_uid == 3

        fake_mailbox["INBOX"]["7"] = build_eml(subject="跳号邮件", text="正文")
        context.sync.sync_all()
        assert context.db.get_folder("tester@corp.com", "INBOX").last_uid == 7

    def test_incremental_search_criteria_used(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        """§3.1 增量同步应带上 UID 水位条件，而不是每次 ALL。"""
        client = patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        client.calls.clear()
        context.sync.sync_all()
        searches = [c[1] for c in client.calls if c[0] == "search"]
        # 第二次同步不应再发 ALL
        assert "ALL" not in searches

    def test_full_rescan_flag(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all(full=True)
        assert context.db.count_messages() == 4


class TestUidValidityChange:
    def test_forces_full_resync(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        """§11.2 UIDVALIDITY 变化说明 UID 空间重建，必须丢弃旧水位。"""
        client = FakeImapClient(fake_mailbox, uidvalidity=1)
        patch_imap(client)
        context.sync.sync_all()
        assert context.db.get_folder("tester@corp.com", "INBOX").last_uid == 3

        # 服务端重建 UID 空间
        client.uidvalidity_by_folder = {"INBOX": 2, "Sent": 2}
        result = context.sync.sync_all()

        state = context.db.get_folder("tester@corp.com", "INBOX")
        assert state.uidvalidity == 2
        assert state.last_uid == 3
        # UIDVALIDITY 变了意味着 UID 可能指向不同邮件，必须重新归档
        assert result.archived == 4

    def test_reset_watermark_before_resync(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        client = FakeImapClient(fake_mailbox, uidvalidity=1)
        patch_imap(client)
        context.sync.sync_all()

        client.uidvalidity_by_folder = {"INBOX": 5, "Sent": 5}
        client.calls.clear()
        context.sync.sync_all()
        # UIDVALIDITY 变化后应重新发 ALL
        assert any(c == ("search", "ALL") for c in client.calls)


class TestOversizeAttachments:
    def test_oversize_attachment_not_downloaded(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        """§3.2 超大附件只记录元数据。"""
        big = build_eml(
            subject="超大附件邮件",
            text="正文仍然要保留。",
            message_id="<big@corp.com>",
            attachments=[("大文件.zip", b"x" * 200_000, "application/zip")],
        )
        mailbox = {"INBOX": {"1": big}}
        client = FakeImapClient(
            mailbox,
            part_sizes={
                "INBOX:1": [
                    PartInfo(section="1", content_type="text/plain", size=40),
                    PartInfo(section="2", content_type="application/zip", size=200_000,
                             filename="大文件.zip", disposition="attachment"),
                ]
            },
        )
        # 阈值设为 10KB，使附件超限
        context.config.sync.max_attachment_size_mb = 10 / 1024
        context.sync.parser.max_attachment_bytes = 10 * 1024
        client.max_attachment_bytes = 10 * 1024
        patch_imap(client)

        result = context.sync.sync_all()
        assert result.archived == 1

        rows = context.db.query("SELECT * FROM attachments")
        assert len(rows) == 1
        assert rows[0]["downloaded"] == 0
        assert rows[0]["size_bytes"] == 200_000
        assert "阈值" in (rows[0]["skip_reason"] or "")

    def test_body_still_archived_for_oversize(
        self, context: AppContext, patch_imap
    ) -> None:
        raw = build_eml(subject="大邮件", text="这段正文必须保留下来。",
                        message_id="<big2@corp.com>")
        client = FakeImapClient({"INBOX": {"1": raw}}, max_attachment_bytes=1)
        client.max_attachment_bytes = 1
        context.sync.parser.max_attachment_bytes = 1
        patch_imap(client)

        context.sync.sync_all()
        record = context.db.find_by_message_id("tester@corp.com", "big2@corp.com")
        assert record is not None
        assert record.local_markdown_path
        assert Path(record.local_markdown_path).is_file()


class TestReconciliation:
    def test_deleted_messages_marked(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        """§11.2 处理网页端删除的邮件。"""
        client = patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        assert context.db.count_messages() == 4

        del fake_mailbox["INBOX"]["2"]
        client.calls.clear()
        result = context.sync.sync_all(full=True)

        assert result.deleted == 1
        assert context.db.count_messages() == 3
        assert context.db.count_messages(include_deleted=True) == 4

    def test_deleted_removed_from_index(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        client = patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        before = context.db.count_chunks()

        target = context.db.find_by_message_id("tester@corp.com", "inbox-2@corp.com")
        del fake_mailbox["INBOX"]["2"]
        context.sync.sync_all(full=True)

        assert context.db.count_chunks() < before
        assert context.db.get_chunks_by_pk(target.pk) == []

    def test_reconcile_disabled(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        context.config.sync.reconcile_deletions = False
        client = patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        del fake_mailbox["INBOX"]["2"]
        result = context.sync.sync_all(full=True)
        assert result.deleted == 0
        assert context.db.count_messages() == 4

    def test_interval_gating(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        """刚比对过就不应再比对（避免每轮都全量扫描）。"""
        client = patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        context.db.update_folder_state(
            "tester@corp.com", "INBOX", last_full_scan_at=utcnow()
        )
        del fake_mailbox["INBOX"]["2"]
        client.calls.clear()
        result = context.sync.sync_all(full=False)
        assert result.deleted == 0
        assert not any(c[0] == "search" and c[1] == "ALL" for c in client.calls)

    def test_full_flag_bypasses_interval(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        """--full 应强制比对，不受比对周期限制。"""
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        context.db.update_folder_state(
            "tester@corp.com", "INBOX", last_full_scan_at=utcnow()
        )
        del fake_mailbox["INBOX"]["2"]
        result = context.sync.sync_all(full=True)
        assert result.deleted == 1

    def test_incremental_sync_reconciles_against_full_uid_list(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        """回归测试：增量同步触发比对时，必须拉取**全量** UID 列表。

        早期实现在增量同步时直接把「水位之后的新邮件」当作远端全集，
        导致窗口之外的所有邮件被误标为已删除。
        """
        client = patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()

        # 让比对到期，但保持增量同步（last_uid=3，无新邮件）
        context.db.update_folder_state(
            "tester@corp.com",
            "INBOX",
            last_full_scan_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        client.calls.clear()
        result = context.sync.sync_all(full=False)

        assert ("search", "ALL") in client.calls, "比对必须额外拉一次全量 UID"
        assert result.deleted == 0
        assert context.db.count_messages() == 4, "窗口外的邮件不能被误删"

    def test_no_reconcile_when_truncated(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        """达到单次上限时不做比对，避免基于不完整状态误标删除。"""
        context.config.sync.max_messages_per_run = 1
        context.config.sync.fetch_batch_size = 1
        patch_imap(FakeImapClient(fake_mailbox))
        result = context.sync.sync_all(full=True)
        assert result.deleted == 0
        assert result.archived == 1


class TestDeduplicationAcrossFolders:
    def test_same_message_id_in_two_folders_not_reindexed(
        self, context: AppContext, patch_imap
    ) -> None:
        """§3.2 优先用 Message-ID 去重。"""
        raw = build_eml(subject="跨文件夹重复", text="同一封邮件出现在两个文件夹。",
                        message_id="<dup@corp.com>")
        mailbox = {"INBOX": {"1": raw}, "Archive": {"1": raw}}
        patch_imap(FakeImapClient(mailbox))

        context.sync.sync_all()
        assert context.db.count_messages() == 2  # 两个文件夹各有归档

        rows = context.db.query("SELECT id, folder, duplicate_of, indexed_at FROM messages ORDER BY id")
        assert rows[1]["duplicate_of"] == rows[0]["id"]
        assert rows[1]["indexed_at"] is not None, "重复邮件应跳过索引"

    def test_duplicate_still_archived_locally(
        self, context: AppContext, patch_imap
    ) -> None:
        raw = build_eml(subject="跨文件夹重复", text="正文", message_id="<dup2@corp.com>")
        patch_imap(FakeImapClient({"INBOX": {"1": raw}, "Archive": {"1": raw}}))
        context.sync.sync_all()
        assert len(list(context.config.archive_path.rglob("*.md"))) == 2


class TestErrorIsolation:
    def test_single_failure_does_not_stop_folder(
        self, context: AppContext, fake_mailbox, patch_imap
    ) -> None:
        patch_imap(FakeImapClient(fake_mailbox, fail_uids={"2"}))
        result = context.sync.sync_all()
        assert result.archived == 3
        assert result.failed == 1
        assert result.error_summary is not None

    def test_partial_status_recorded(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox, fail_uids={"2"}))
        context.sync.sync_all()
        logs = context.db.recent_sync_logs()
        statuses = {log["folder"]: log["status"] for log in logs}
        assert statuses["INBOX"] == "partial"

    def test_whole_folder_failure_captured(self, context: AppContext, patch_imap) -> None:
        class Broken(FakeImapClient):
            def search_uids(self, criteria="ALL", *, folder=None):
                raise RuntimeError("模拟搜索失败")

        patch_imap(Broken({"INBOX": {}}))
        result = context.sync.sync_all()
        assert result.status == "failed"
        assert "模拟搜索失败" in (result.error_summary or "")


class TestFolderSelection:
    def test_exclude_folders(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        context.config.sync.exclude_folders = ["Sent"]
        patch_imap(FakeImapClient(fake_mailbox))
        result = context.sync.sync_all()
        assert result.archived == 3
        assert all(f.name != "Sent" for f in context.db.list_folders())

    def test_explicit_folders(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        result = context.sync.sync_all(folders=["Sent"])
        assert result.archived == 1

    def test_unknown_folder_skipped(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        result = context.sync.sync_all(folders=["不存在"])
        assert result.archived == 0

    def test_single_folder_sync(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        client = patch_imap(FakeImapClient(fake_mailbox))
        result = context.sync.sync_folder(client, "INBOX")
        assert result.archived == 3


class TestMaxMessagesPerRun:
    def test_limit_respected(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        context.config.sync.max_messages_per_run = 2
        context.config.sync.fetch_batch_size = 1
        patch_imap(FakeImapClient(fake_mailbox))
        result = context.sync.sync_all()
        assert result.archived == 2
        assert result.skipped > 0

    def test_resumes_next_run(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        """§7 断点续传：受限于上限后，下一轮应继续。"""
        context.config.sync.max_messages_per_run = 2
        context.config.sync.fetch_batch_size = 1
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        result = context.sync.sync_all()
        assert result.archived >= 1
        assert context.db.count_messages() == 4


class TestStatusReporting:
    def test_status_shape(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        status = context.sync.status()
        assert status["total_messages"] == 4
        assert status["running"] is False
        assert len(status["folders"]) == 2
        assert status["last_sync"] is not None

    def test_account_is_masked(self, context: AppContext) -> None:
        assert "***" in context.sync.status()["account"]

    def test_last_result_stored(self, context: AppContext, fake_mailbox, patch_imap) -> None:
        patch_imap(FakeImapClient(fake_mailbox))
        context.sync.sync_all()
        assert context.sync.last_result is not None


class TestBatching:
    def test_respects_batch_size(self, context: AppContext, patch_imap) -> None:
        mailbox = {
            "INBOX": {
                str(i): build_eml(subject=f"邮件{i}", text=f"第 {i} 封正文。",
                                  message_id=f"<batch{i}@corp.com>")
                for i in range(1, 26)
            }
        }
        client = patch_imap(FakeImapClient(mailbox))
        context.config.sync.fetch_batch_size = 5
        context.sync.sync_all()
        assert context.db.count_messages() == 25
        # 每批一次 size 预检 + 逐封下载
        assert len([c for c in client.calls if c[0] == "fetch"]) == 25
