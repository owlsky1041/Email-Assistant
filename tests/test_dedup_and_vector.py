"""附件去重与向量检索加速的回归。

两个问题都属于"不报错、只是悄悄变差"的类型：
* 附件重复内容被反复写盘 —— 数据库里明明有 ``sha256``，去重函数也写好了，
  但从来没人调用，``attachments_reused`` 永远是 0；
* 暴力向量检索是纯 Python 逐元素点积 —— 5 万切片约 1.07s/次查询，
  而 numpy 已在依赖里，一次矩阵乘只要几毫秒。
"""

from __future__ import annotations

import os
from array import array
from pathlib import Path

import pytest

import src.sync_service as sync_module
import src.vector_store as vector_module
from src.context import AppContext
from src.models import ParsedMessage
from src.vector_store import SqliteVectorStore, VectorHit
from tests.conftest import FakeImapClient, build_eml


# ---------------------------------------------------------------------------
# 附件去重
# ---------------------------------------------------------------------------

PAYLOAD = b"%PDF-1.4 " + b"same-bytes-everywhere" * 200


def _message(uid: str, filename: str = "技术附件.pdf") -> ParsedMessage:
    return ParsedMessage(
        uid=uid,
        folder="INBOX",
        subject=f"第 {uid} 封",
        body_markdown="正文",
        body_text="正文",
        attachments=[],
    )


class TestAttachmentDedup:
    def _mailbox(self) -> dict[str, dict[str, bytes]]:
        """两封邮件带**完全相同**的附件，外加一封不同的。"""
        return {
            "INBOX": {
                "1": build_eml(
                    subject="第一封",
                    text="附件请查收",
                    message_id="<d1@corp.com>",
                    attachments=[("技术附件.pdf", PAYLOAD, "application/pdf")],
                ),
                "2": build_eml(
                    subject="第二封",
                    text="同一份附件再发一次",
                    message_id="<d2@corp.com>",
                    attachments=[("技术附件.pdf", PAYLOAD, "application/pdf")],
                ),
                "3": build_eml(
                    subject="第三封",
                    text="另一份附件",
                    message_id="<d3@corp.com>",
                    attachments=[("别的.pdf", PAYLOAD + b"different", "application/pdf")],
                ),
            }
        }

    def test_second_copy_is_reused_not_rewritten(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一份内容第二次出现时必须复用，而不是又写一遍。"""
        mailbox = self._mailbox()
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: FakeImapClient(mailbox))

        context.sync.sync_all(index=False)

        rows = list(
            context.db.query(
                "SELECT filename, sha256, local_path, downloaded, skip_reason "
                "FROM attachments ORDER BY id"
            )
        )
        same = [r for r in rows if r["sha256"] == rows[0]["sha256"]]
        assert len(same) == 2, f"应该有两份相同内容的记录，实际 {len(same)}"

        reused = [r for r in same if r["skip_reason"] == "reused"]
        assert reused, "第二份相同内容没有被标记为 reused"
        # 两份都要可用（reused 不等于跳过）
        assert all(r["downloaded"] == 1 for r in same)
        assert all(Path(r["local_path"]).is_file() for r in same)

    def test_reused_file_shares_content(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """复用出来的文件内容必须与原文件逐字节相同。"""
        mailbox = self._mailbox()
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: FakeImapClient(mailbox))
        context.sync.sync_all(index=False)

        rows = list(
            context.db.query(
                "SELECT sha256, local_path FROM attachments ORDER BY id"
            )
        )
        by_hash: dict[str, list[Path]] = {}
        for row in rows:
            by_hash.setdefault(row["sha256"], []).append(Path(row["local_path"]))

        for digest, paths in by_hash.items():
            if len(paths) < 2:
                continue
            contents = {p.read_bytes() for p in paths}
            assert len(contents) == 1, f"{digest[:8]} 的多个副本内容不一致"

    def test_hardlink_when_supported(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """同一文件系统上应走硬链接（同 inode），这样才真的省磁盘。"""
        probe_a = tmp_path / "probe-a"
        probe_a.write_bytes(b"x")
        try:
            os.link(probe_a, tmp_path / "probe-b")
        except OSError:
            pytest.skip("当前文件系统不支持硬链接")

        mailbox = self._mailbox()
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: FakeImapClient(mailbox))
        context.sync.sync_all(index=False)

        rows = list(context.db.query("SELECT sha256, local_path FROM attachments ORDER BY id"))
        by_hash: dict[str, list[Path]] = {}
        for row in rows:
            by_hash.setdefault(row["sha256"], []).append(Path(row["local_path"]))
        linked = [
            paths for paths in by_hash.values() if len(paths) > 1
        ]
        assert linked, "没有出现重复内容，测试前提不成立"
        for paths in linked:
            inodes = {p.stat().st_ino for p in paths}
            assert len(inodes) == 1, f"同一内容占了 {len(inodes)} 个 inode，没有走硬链接"

    def test_different_content_is_written_separately(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不同内容不能被误判为重复。"""
        mailbox = self._mailbox()
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: FakeImapClient(mailbox))
        context.sync.sync_all(index=False)

        distinct = context.db.query_one("SELECT COUNT(DISTINCT sha256) AS n FROM attachments")["n"]
        assert distinct == 2, f"应有 2 种不同内容，实际 {distinct}"

    def test_lookup_ignores_missing_file(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """数据库里的路径失效时不能复用，必须老老实实重新写。"""
        mailbox = self._mailbox()
        monkeypatch.setattr(sync_module, "ImapClient", lambda *a, **kw: FakeImapClient(mailbox))
        context.sync.sync_all(index=False)

        # 把已记录的文件删掉，模拟用户手工清理
        row = context.db.query_one("SELECT local_path FROM attachments LIMIT 1")
        Path(row["local_path"]).unlink()

        assert context.sync._lookup_attachment_blob(
            context.db.query_one("SELECT sha256 FROM attachments LIMIT 1")["sha256"]
        ) is None

    def test_unknown_hash_returns_none(self, context: AppContext) -> None:
        assert context.sync._lookup_attachment_blob("0" * 64) is None

    def test_exporter_without_lookup_still_writes(
        self, context: AppContext, tmp_path: Path
    ) -> None:
        """没有接入去重时（独立使用 exporter）也必须正常落盘。"""
        from src.markdown_exporter import MarkdownExporter

        exporter = MarkdownExporter(context.config)
        message = _message("9")
        from src.models import AttachmentMeta

        message.attachments = [
            AttachmentMeta(filename="a.bin", part_index=0, size_bytes=len(PAYLOAD))
        ]
        message.attachment_payloads = {0: PAYLOAD}
        result = exporter.export(message, account="a@x.com")
        saved = [a for a in result.attachments if a.downloaded]
        assert saved and Path(saved[0].local_path).read_bytes() == PAYLOAD


# ---------------------------------------------------------------------------
# 向量检索加速
# ---------------------------------------------------------------------------


class TestVectorSearchAcceleration:
    @pytest.fixture
    def seeded(self, context: AppContext) -> AppContext:
        from datetime import datetime, timezone

        for i in range(1, 6):
            message = ParsedMessage(
                uid=str(i),
                folder="INBOX",
                uidvalidity=1,
                message_id=f"{i}@corp.com",
                subject=f"主题 {i}",
                sender="alice@corp.com",
                recipients="tester@corp.com",
                date=datetime(2024, 3, i, 10, 0, tzinfo=timezone.utc),
                body_markdown=f"正文内容 {i}。" * 30,
                body_text=f"正文内容 {i}。" * 30,
            )
            # 走真实路径：导出 → 转记录 → 入库，避免手工构造 MessageRecord
            archive = context.sync.exporter.export(message, account="tester@corp.com")
            record = context.sync._to_record(message, archive, duplicate_of=None)
            context.db.insert_message(record, archive.attachments)
        context.indexer.index_pending()
        return context

    def test_matrix_path_matches_python_fallback(self, seeded: AppContext) -> None:
        """两条路径必须给出**完全一致**的排序结果。"""
        store = seeded.vector_store
        vector = seeded.embedder.embed_query("正文内容")

        store._invalidate()
        store._load_cache()
        fast = store.query(vector, top_k=5)
        assert fast

        # 关掉矩阵路径，强制走纯 Python
        with_matrix = store._matrix
        store._matrix = None
        slow = store.query(vector, top_k=5)
        store._matrix = with_matrix

        assert [h.chunk_id for h in fast] == [h.chunk_id for h in slow]
        for a, b in zip(fast, slow):
            assert a.score == pytest.approx(b.score, abs=1e-6)

    def test_fallback_when_numpy_missing(
        self, seeded: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """numpy 是可选依赖，缺了必须还能检索。"""
        store = seeded.vector_store
        vector = seeded.embedder.embed_query("正文内容")
        expected = store.query(vector, top_k=3)

        monkeypatch.setattr(SqliteVectorStore, "_numpy", staticmethod(lambda: None))
        store._invalidate()
        store._load_cache()
        hits = store.query(vector, top_k=3)

        assert hits, "没有 numpy 时检索不应返回空"
        assert [h.chunk_id for h in hits] == [h.chunk_id for h in expected]

    def test_where_filter_excludes_rows(self, seeded: AppContext) -> None:
        """回归：矩阵路径曾把被过滤的行标成 -inf 后**照样返回**。"""
        store = seeded.vector_store
        vector = seeded.embedder.embed_query("正文内容")
        hits = store.query(vector, top_k=10, where={"message_id": "1@corp.com"})

        assert hits
        for hit in hits:
            assert store.db.get_chunk(hit.chunk_id)["message_id"] == "1@corp.com"

    def test_where_filter_matches_fallback(self, seeded: AppContext) -> None:
        store = seeded.vector_store
        vector = seeded.embedder.embed_query("正文内容")
        where = {"message_id": "2@corp.com"}

        fast = store.query(vector, top_k=10, where=where)
        with_matrix = store._matrix
        store._matrix = None
        slow = store.query(vector, top_k=10, where=where)
        store._matrix = with_matrix

        assert [h.chunk_id for h in fast] == [h.chunk_id for h in slow]

    def test_matrix_invalidated_on_reset(self, seeded: AppContext) -> None:
        """写入后必须重建矩阵，否则会查到过期向量。"""
        store = seeded.vector_store
        store.query(seeded.embedder.embed_query("正文"), top_k=3)
        assert store._matrix is not None
        store.reset()
        assert store._matrix is None

    def test_hits_are_sorted_desc(self, seeded: AppContext) -> None:
        hits = seeded.vector_store.query(seeded.embedder.embed_query("正文内容"), top_k=5)
        assert hits == sorted(hits, key=lambda h: h.score, reverse=True)
