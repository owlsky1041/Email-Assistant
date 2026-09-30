"""索引测试（§3.5）：切片入库、向量写入、增量复用、重建。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.context import AppContext
from src.models import AttachmentMeta, ParsedMessage
from src.vector_store import _dot, _pack_vector, _unpack_vector


def add_message(context: AppContext, uid: str, subject: str, body: str, **kw) -> int:
    # 注意：MailParser 会把 Message-ID 的尖括号剥掉，这里保持一致
    message = ParsedMessage(
        uid=uid,
        folder=kw.get("folder", "INBOX"),
        uidvalidity=1,
        message_id=f"{uid}@corp.com",
        subject=subject,
        sender="alice@corp.com",
        sender_name="爱丽丝",
        recipients="tester@corp.com",
        date=datetime(2024, 3, int(uid) if uid.isdigit() else 1, 10, 0, tzinfo=timezone.utc),
        body_text=body,
        body_markdown=body,
        attachments=kw.get("attachments", []),
        attachment_payloads=kw.get("attachment_payloads", {}),
    )
    archive = context.sync.exporter.export(message, account="tester@corp.com")
    record = context.sync._to_record(message, archive, duplicate_of=None)
    return context.db.insert_message(record, archive.attachments)


class TestVectorCodec:
    def test_roundtrip(self) -> None:
        vector = [1.0, -2.5, 3.25]
        assert list(_unpack_vector(_pack_vector(vector))) == pytest.approx(vector)

    def test_dot_product(self) -> None:
        a = _unpack_vector(_pack_vector([1.0, 0.0, 0.0]))
        b = _unpack_vector(_pack_vector([1.0, 0.0, 0.0]))
        assert _dot(a, b, 3) == pytest.approx(1.0)

    def test_orthogonal_is_zero(self) -> None:
        a = _unpack_vector(_pack_vector([1.0, 0.0]))
        b = _unpack_vector(_pack_vector([0.0, 1.0]))
        assert _dot(a, b, 2) == pytest.approx(0.0)


class TestIndexMessage:
    def test_creates_chunks_and_vectors(self, context: AppContext) -> None:
        pk = add_message(context, "1", "报销制度", "报销流程说明。" * 30)
        stats = context.indexer.index_pending()
        assert stats.messages == 1
        assert stats.chunks >= 1
        assert stats.embedded >= 1
        assert context.db.count_chunks() >= 1
        assert context.db.count_vectors() >= 1
        assert pk > 0

    def test_message_marked_indexed(self, context: AppContext) -> None:
        add_message(context, "1", "主题", "正文内容。" * 20)
        context.indexer.index_pending()
        assert context.indexer.count_pending() == 0

    def test_empty_body_produces_no_chunks(self, context: AppContext) -> None:
        pk = add_message(context, "1", "无正文", "")
        context.db.execute(
            "UPDATE messages SET body_text = '' WHERE id = ?", (pk,)
        )
        record = context.db.get_message(pk)
        count = context.indexer.index_message(record)
        assert count == 0
        assert context.indexer.count_pending() == 0

    def test_chunks_carry_metadata(self, context: AppContext) -> None:
        add_message(context, "1", "季度报告", "报告正文内容。" * 20)
        context.indexer.index_pending()
        chunks = context.db.get_chunks("1@corp.com")
        assert chunks
        # §3.4 每个切片保留的元数据
        for chunk in chunks:
            assert chunk["message_id"] == "1@corp.com"
            assert chunk["subject"] == "季度报告"
            assert chunk["folder"] == "INBOX"
            assert chunk["chunk_index"] >= 0
            assert chunk["local_markdown_path"].endswith(".md")
            assert chunk["token_count"] > 0


class TestIncrementalEmbedding:
    def test_unchanged_chunks_reuse_vectors(self, context: AppContext) -> None:
        """§3.5 增量索引：内容未变的切片不应重新调用模型。"""
        pk = add_message(context, "1", "主题", "固定正文内容。" * 30)
        first = context.indexer.index_pending()
        assert first.embedded >= 1 and first.reused == 0

        # 强制重新索引（不改内容）
        context.db.execute(
            "UPDATE messages SET indexed_at = NULL WHERE id = ?", (pk,)
        )
        second = context.indexer.index_pending()
        assert second.reused >= 1
        assert second.embedded == 0, "内容未变时不应产生新的嵌入调用"

    def test_changed_content_triggers_embedding(self, context: AppContext) -> None:
        pk = add_message(context, "1", "主题", "原始正文内容。" * 30)
        context.indexer.index_pending()
        context.db.execute(
            "UPDATE messages SET body_text = ?, indexed_at = NULL WHERE id = ?",
            ("全新的正文内容，与之前完全不同。" * 30, pk),
        )
        stats = context.indexer.index_pending()
        assert stats.embedded >= 1

    def test_partial_change_reuses_unchanged_chunks(self, context: AppContext) -> None:
        body = "\n\n".join(f"第 {i} 段固定内容，用于验证部分复用。" for i in range(30))
        pk = add_message(context, "1", "主题", body)
        context.indexer.index_pending()

        # 只在末尾追加一段
        context.db.execute(
            "UPDATE messages SET body_text = ?, indexed_at = NULL WHERE id = ?",
            (body + "\n\n新增的最后一段内容。", pk),
        )
        stats = context.indexer.index_pending()
        assert stats.reused >= 1, "未变化的切片应复用旧向量"


class TestVectorStoreSqlite:
    def test_query_returns_normalized_hits(self, context: AppContext) -> None:
        add_message(context, "1", "报销发票", "差旅报销发票内容。" * 20)
        add_message(context, "2", "服务器扩容", "扩容机器申请内容。" * 20)
        context.indexer.index_pending()

        vector = context.embedder.embed_query("报销发票")
        hits = context.vector_store.query(vector, top_k=5)
        assert hits
        assert all(-1.01 <= h.score <= 1.01 for h in hits)
        assert hits == sorted(hits, key=lambda h: h.score, reverse=True)

    def test_where_filter_by_message_id(self, context: AppContext) -> None:
        add_message(context, "1", "主题一", "内容一。" * 20)
        add_message(context, "2", "主题二", "内容二。" * 20)
        context.indexer.index_pending()
        vector = context.embedder.embed_query("内容")
        hits = context.vector_store.query(vector, top_k=10, where={"message_id": "1@corp.com"})
        assert hits
        for hit in hits:
            chunk = context.db.get_chunk(hit.chunk_id)
            assert chunk["message_id"] == "1@corp.com"

    def test_count_and_reset(self, context: AppContext) -> None:
        add_message(context, "1", "主题", "内容。" * 20)
        context.indexer.index_pending()
        assert context.vector_store.count() >= 1
        context.vector_store.reset()
        assert context.vector_store.count() == 0

    def test_delete_removes_from_index(self, context: AppContext) -> None:
        add_message(context, "1", "主题", "内容。" * 20)
        context.indexer.index_pending()
        chunk_ids = [c["id"] for c in context.db.get_chunks("1@corp.com")]
        context.vector_store.delete(chunk_ids)
        assert context.vector_store.count() == 0


class TestRemoveAndRebuild:
    def test_remove_message_clears_index(self, context: AppContext) -> None:
        pk = add_message(context, "1", "主题", "内容。" * 20)
        context.indexer.index_pending()
        assert context.db.count_chunks() > 0
        context.indexer.remove_message(pk)
        assert context.db.count_chunks() == 0
        assert context.vector_store.count() == 0

    def test_rebuild_regenerates_everything(self, context: AppContext) -> None:
        for i in range(1, 4):
            add_message(context, str(i), f"主题{i}", f"第{i}封邮件的内容。" * 20)
        context.indexer.index_pending()
        before = context.db.count_chunks()

        stats = context.indexer.rebuild()
        assert stats.messages == 3
        assert context.db.count_chunks() == before
        assert context.vector_store.count() == before

    def test_rebuild_after_vector_reset(self, context: AppContext) -> None:
        add_message(context, "1", "主题", "内容。" * 20)
        context.indexer.index_pending()
        context.vector_store.reset()
        assert context.vector_store.count() == 0
        context.indexer.rebuild()
        assert context.vector_store.count() >= 1


class TestHealth:
    def test_reports_backends(self, context: AppContext) -> None:
        health = context.indexer.health()
        assert health["embedder"]["backend"] == "hashing"
        assert health["vector_store"]["backend"] == "sqlite-bruteforce"
        assert "pending" in health

    def test_pending_count(self, context: AppContext) -> None:
        add_message(context, "1", "主题", "内容。" * 20)
        assert context.indexer.count_pending() == 1
        context.indexer.index_pending()
        assert context.indexer.count_pending() == 0


class TestHashingEmbedder:
    def test_deterministic(self, context: AppContext) -> None:
        a = context.embedder.embed(["同样的文本"])
        b = context.embedder.embed(["同样的文本"])
        assert a == b

    def test_different_text_different_vector(self, context: AppContext) -> None:
        a = context.embedder.embed(["发票报销"])[0]
        b = context.embedder.embed(["服务器扩容"])[0]
        assert _dot(
            _unpack_vector(_pack_vector(a)), _unpack_vector(_pack_vector(b)), len(a)
        ) < 0.95

    def test_normalized(self, context: AppContext) -> None:
        vector = context.embedder.embed(["测试文本"])[0]
        norm = sum(v * v for v in vector) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-6)

    def test_batch_matches_single(self, context: AppContext) -> None:
        texts = ["第一条", "第二条", "第三条"]
        batch = context.embedder.embed(texts)
        for index, text in enumerate(texts):
            assert batch[index] == context.embedder.embed([text])[0]

    def test_empty_string(self, context: AppContext) -> None:
        vector = context.embedder.embed([""])[0]
        assert len(vector) == context.embedder.dimension

    def test_embed_query(self, context: AppContext) -> None:
        assert len(context.embedder.embed_query("查询")) == context.embedder.dimension
