"""知识库索引：切片 -> 嵌入 -> 向量入库（§3.4 / §3.5）。

增量策略
--------
``kb_vectors.vector_blob`` 保存向量的权威副本（无论后端是 Chroma 还是 SQLite）。
重建切片时按**内容哈希**比对：哈希未变的切片直接复用旧向量，
既不重新调用模型、也不重新写库。

对「上万封邮件 + 本地 CPU 推理」的场景，这是把重建时间从小时级压到分钟级的关键。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from .cancellation import CancellationToken, get_cancellation_token
from .chunker import TextChunker
from .config import AppConfig
from .database import Database
from .embedder import Embedder
from .models import Chunk, MessageRecord
from .vector_store import VectorStore, _pack_vector, _unpack_vector

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IndexStats:
    messages: int = 0
    chunks: int = 0
    embedded: int = 0
    reused: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "IndexStats") -> None:
        self.messages += other.messages
        self.chunks += other.chunks
        self.embedded += other.embedded
        self.reused += other.reused
        self.failed += other.failed
        self.errors.extend(other.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "chunks": self.chunks,
            "embedded": self.embedded,
            "reused": self.reused,
            "failed": self.failed,
            "errors": self.errors[:10],
        }


class IndexService:
    """负责切片、嵌入与向量写入。"""

    def __init__(
        self,
        database: Database,
        embedder: Embedder,
        vector_store: VectorStore,
        config: AppConfig,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self.db = database
        self.embedder = embedder
        self.vector_store = vector_store
        self.config = config
        self.cancel = cancel_token or get_cancellation_token()
        self.chunker = TextChunker(
            chunk_size=config.embedding.chunk_size,
            chunk_overlap=config.embedding.chunk_overlap,
        )
        self.model_name = getattr(embedder, "name", "unknown")
        self.batch_size = max(1, config.embedding.batch_size)
        self.is_sqlite_backend = vector_store.backend.startswith("sqlite")

    # ------------------------------------------------------------------
    # 单封邮件
    # ------------------------------------------------------------------

    def index_message(self, record: MessageRecord, *, stats: IndexStats | None = None) -> int:
        """为单封邮件重建切片与向量，返回切片数。"""
        stats = stats or IndexStats()
        if record.pk is None:
            return 0

        # 必须在替换切片之前取回旧向量（ON DELETE CASCADE 会一起删掉）
        reusable = self._collect_reusable_vectors(record.pk)

        body = self._budgeted_body(record.body_text or "")
        chunks = self.chunker.split(
            body,
            message_pk=record.pk,
            message_id=record.message_id,
            subject=record.subject,
            sender=record.sender,
            date=record.date_utc,
            folder=record.folder,
            local_markdown_path=record.local_markdown_path,
        )
        for chunk in chunks:
            chunk.content_hash = chunk.content_hash or chunk.compute_hash()

        chunk_ids = self.db.replace_chunks(record.pk, chunks)
        stats.chunks += len(chunks)

        if not chunk_ids:
            self.db.mark_indexed(record.pk, record.content_hash)
            stats.messages += 1
            return 0

        pending: list[tuple[int, Chunk]] = []
        for chunk_id, chunk in zip(chunk_ids, chunks):
            blob = reusable.get(chunk.content_hash)
            if blob is None:
                pending.append((chunk_id, chunk))
                continue
            try:
                self._persist_vector(
                    chunk_id, chunk, record, _unpack_vector(blob), blob=blob
                )
                stats.reused += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("复用向量失败，改为重新嵌入：%s", exc)
                pending.append((chunk_id, chunk))

        for batch in _batched(pending, self.batch_size):
            self.cancel.raise_if_cancelled()
            texts = [self._embedding_text(chunk, record) for _, chunk in batch]
            try:
                vectors = self.embedder.embed(texts)
            except Exception as exc:  # noqa: BLE001 - 单批失败不应中断整轮
                stats.failed += len(batch)
                stats.errors.append(f"{record.message_id}: 嵌入失败 {exc}")
                logger.error("嵌入失败（%s）：%s", record.message_id, exc)
                continue

            for (chunk_id, chunk), vector in zip(batch, vectors):
                try:
                    self._persist_vector(chunk_id, chunk, record, vector)
                    stats.embedded += 1
                except Exception as exc:  # noqa: BLE001
                    stats.failed += 1
                    stats.errors.append(f"chunk {chunk_id}: 写入失败 {exc}")
                    logger.error("向量写入失败（chunk=%s）：%s", chunk_id, exc)

        self.db.mark_indexed(record.pk, record.content_hash)
        stats.messages += 1
        return len(chunk_ids)

    # ------------------------------------------------------------------

    def _persist_vector(
        self,
        chunk_id: int,
        chunk: Chunk,
        record: MessageRecord,
        vector: Sequence[float],
        *,
        blob: bytes | None = None,
    ) -> None:
        """写入向量库 + 记录元数据（BLOB 作为权威副本）。"""
        digest = chunk.content_hash
        metadata = {
            "message_pk": record.pk or 0,
            "message_id": record.message_id,
            "chunk_index": chunk.chunk_index,
            "subject": record.subject,
            "sender": record.sender,
            "folder": record.folder,
            "date_utc": record.date_utc.isoformat() if record.date_utc else "",
            "local_markdown_path": record.local_markdown_path,
        }
        self.vector_store.upsert([chunk_id], [list(vector)], [metadata])

        payload = blob
        if payload is None:
            payload = _pack_vector(vector) if self.is_sqlite_backend else None
            if not self.is_sqlite_backend and self.config.vector.cache_blobs:
                payload = _pack_vector(vector)

        self.db.record_vector(
            chunk_id,
            message_id=record.message_id,
            model=self.model_name,
            dimension=self.embedder.dimension,
            backend=self.vector_store.backend,
            vector_ref=str(chunk_id),
            content_hash=digest,
            vector_blob=payload,
        )

    @staticmethod
    def _embedding_text(chunk: Chunk, record: MessageRecord) -> str:
        """嵌入文本 = 主题 + 切片正文。

        邮件正文经常省略上下文，把主题拼进嵌入文本能明显提升召回质量。
        """
        subject = (record.subject or "").strip()
        if subject and subject != "(无主题)":
            return f"{subject}\n{chunk.text}"
        return chunk.text

    def _budgeted_body(self, body: str) -> str:
        budget = self.config.sync.max_body_index_size_kb * 1024
        encoded = body.encode("utf-8", "replace")
        if len(encoded) <= budget:
            return body
        logger.debug("正文超过索引预算（%d 字节），已截断", budget)
        return encoded[:budget].decode("utf-8", "ignore")

    def _collect_reusable_vectors(self, message_pk: int) -> dict[str, bytes]:
        """取回 ``{内容哈希: 向量BLOB}``。"""
        rows = self.db.query(
            """
            SELECT c.content_hash AS content_hash, v.vector_blob AS vector_blob
            FROM kb_chunks c
            JOIN kb_vectors v ON v.chunk_id = c.id
            WHERE c.message_pk = ? AND v.vector_blob IS NOT NULL
            """,
            (message_pk,),
        )
        return {
            str(row["content_hash"]): bytes(row["vector_blob"])
            for row in rows
            if row["content_hash"] and row["vector_blob"]
        }

    # ------------------------------------------------------------------
    # 批量 / 维护
    # ------------------------------------------------------------------

    def index_pending(self, *, limit: int = 0, stats: IndexStats | None = None) -> IndexStats:
        """索引所有待处理邮件（新增或正文已变化）。"""
        stats = stats or IndexStats()
        batch = 200
        processed = 0
        while True:
            self.cancel.raise_if_cancelled()
            remaining = (limit - processed) if limit else batch
            if limit and remaining <= 0:
                break
            records = self.db.messages_pending_index(limit=min(batch, remaining))
            if not records:
                break
            for record in records:
                self.cancel.raise_if_cancelled()
                try:
                    self.index_message(record, stats=stats)
                except Exception as exc:  # noqa: BLE001
                    stats.failed += 1
                    stats.errors.append(f"{record.message_id}: {exc}")
                    logger.exception("索引邮件失败：%s", record.message_id)
                processed += 1
        self.vector_store.persist()
        return stats

    def count_pending(self) -> int:
        row = self.db.query_one(
            """
            SELECT COUNT(*) AS c FROM messages
            WHERE deleted_at IS NULL AND (indexed_at IS NULL OR indexed_at < updated_at)
            """
        )
        return int(row["c"]) if row else 0

    def remove_message(self, message_pk: int) -> None:
        """删除邮件对应的切片与向量。"""
        chunk_ids = self.db.delete_chunks_for_message(message_pk)
        if chunk_ids:
            try:
                self.vector_store.delete(chunk_ids)
            except Exception:  # noqa: BLE001
                logger.warning("删除向量失败（chunk=%s）", chunk_ids, exc_info=True)

    def rebuild(self, *, batch_size: int = 200) -> IndexStats:
        """全量重建索引（清空切片与向量后重跑）。"""
        stats = IndexStats()
        logger.info("开始重建知识库索引…")
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM kb_chunks")
            if self.db.fts_available:
                conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM kb_vectors")
        try:
            self.vector_store.reset()
        except Exception:  # noqa: BLE001
            logger.warning("清空向量库失败", exc_info=True)

        offset = 0
        while True:
            self.cancel.raise_if_cancelled()
            rows = self.db.query(
                "SELECT id FROM messages WHERE deleted_at IS NULL ORDER BY id LIMIT ? OFFSET ?",
                (batch_size, offset),
            )
            if not rows:
                break
            for row in rows:
                self.cancel.raise_if_cancelled()
                record = self.db.get_message(int(row["id"]))
                if record is None:
                    continue
                try:
                    self.index_message(record, stats=stats)
                except Exception as exc:  # noqa: BLE001
                    stats.failed += 1
                    stats.errors.append(f"{record.message_id}: {exc}")
            offset += len(rows)
        self.vector_store.persist()
        logger.info("索引重建完成：%s", stats.to_dict())
        return stats

    def health(self) -> dict[str, Any]:
        return {
            "embedder": self.embedder.health(),
            "vector_store": self.vector_store.health(),
            "pending": self.count_pending(),
            "chunks": self.db.count_chunks(),
            "vectors": self.db.count_vectors(),
        }


def _batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
