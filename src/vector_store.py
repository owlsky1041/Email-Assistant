"""向量存储后端（§3.5）。

三个后端
--------
``chroma``
    计划书首选方案，持久化到 ``data/chromadb``。
``sqlite-vec``
    使用 SQLite 原生向量扩展，速度快、无额外服务。
``sqlite-bruteforce``
    零依赖兜底：向量以 float32 BLOB 存在 ``kb_vectors.vector_blob``，
    查询时全量点积。上万封邮件（约 5-15 万切片）在内存中完全可承受。
"""

from __future__ import annotations

import logging
import struct
import threading
from abc import ABC, abstractmethod
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .cancellation import CancellationToken, get_cancellation_token
from .config import AppConfig
from .database import Database

logger = logging.getLogger(__name__)


class VectorStoreError(RuntimeError):
    """向量库不可用。"""


@dataclass(slots=True)
class VectorHit:
    chunk_id: int
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

class VectorStore(ABC):
    """向量库统一接口。全部以 ``chunk_id``（整数）为主键。"""

    backend: str = "base"

    @abstractmethod
    def upsert(
        self,
        chunk_ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        """写入/更新向量，返回写入条数。"""

    @abstractmethod
    def query(
        self,
        vector: Sequence[float],
        top_k: int = 10,
        *,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """按余弦相似度检索。"""

    @abstractmethod
    def delete(self, chunk_ids: Sequence[int]) -> int:
        """删除指定切片向量。"""

    @abstractmethod
    def count(self) -> int:
        """向量总数。"""

    @abstractmethod
    def reset(self) -> None:
        """清空全部向量（重建索引时使用）。"""

    def delete_by_message(self, message_id: str) -> int:
        """按 message_id 删除。默认走子类提供的 chunk 列表。"""
        return 0

    def persist(self) -> None:
        """把内存中的改动落盘（Chroma 自动持久化，此处为空实现）。"""

    def health(self) -> dict[str, Any]:
        return {"backend": self.backend, "count": self.count()}


# ---------------------------------------------------------------------------
# ChromaDB
# ---------------------------------------------------------------------------

class ChromaVectorStore(VectorStore):
    """ChromaDB 持久化后端。"""

    backend = "chroma"

    def __init__(
        self,
        persist_dir: str,
        collection: str = "mail_chunks",
        *,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        try:
            import chromadb  # type: ignore
            from chromadb.config import Settings  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise VectorStoreError(
                "ChromaDB 后端未安装，请执行：pip install -r requirements-optional.txt"
            ) from exc

        self.cancel = cancel_token or get_cancellation_token()
        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
        )
        self.collection_name = collection

    def upsert(
        self,
        chunk_ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        if not chunk_ids:
            return 0
        ids = [str(c) for c in chunk_ids]
        metas = [
            _flatten_metadata(m if metadatas else {}) for m in (metadatas or [{}] * len(ids))
        ]
        # 分批写入，避免单次请求过大
        written = 0
        for start in range(0, len(ids), 256):
            self.cancel.raise_if_cancelled()
            stop = start + 256
            self._collection.upsert(
                ids=ids[start:stop],
                embeddings=[list(v) for v in vectors[start:stop]],
                metadatas=metas[start:stop],
            )
            written += len(ids[start:stop])
        return written

    def query(
        self,
        vector: Sequence[float],
        top_k: int = 10,
        *,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        if top_k <= 0 or self.count() == 0:
            return []
        result = self._collection.query(
            query_embeddings=[list(vector)],
            n_results=min(top_k, max(self.count(), 1)),
            where=_flatten_where(where) if where else None,
            include=["metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        hits: list[VectorHit] = []
        for i, raw_id in enumerate(ids):
            distance = float(distances[i]) if i < len(distances) else 1.0
            # cosine distance -> similarity
            score = 1.0 - distance
            meta = metadatas[i] if i < len(metadatas) and metadatas[i] else {}
            hits.append(VectorHit(chunk_id=int(raw_id), score=score, metadata=dict(meta)))
        return hits

    def delete(self, chunk_ids: Sequence[int]) -> int:
        ids = [str(c) for c in chunk_ids if c is not None]
        if not ids:
            return 0
        self._collection.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception:  # noqa: BLE001
            return 0

    def reset(self) -> None:
        try:
            self._client.delete_collection(self.collection_name)
        except Exception:  # noqa: BLE001
            logger.debug("删除 Chroma 集合失败（可能不存在）", exc_info=True)
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name, metadata={"hnsw:space": "cosine"}
        )

    def health(self) -> dict[str, Any]:
        return {"backend": self.backend, "collection": self.collection_name, "count": self.count()}


# ---------------------------------------------------------------------------
# SQLite 后端（sqlite-vec 或全量暴力检索）
# ---------------------------------------------------------------------------

class SqliteVectorStore(VectorStore):
    """把向量存进 SQLite 的 ``kb_vectors.vector_blob``。

    ``use_sqlite_vec=True`` 时若环境提供了 ``sqlite_vec`` 扩展则启用原生加速，
    否则自动退化为全量点积（带内存缓存）。
    """

    def __init__(
        self,
        database: Database,
        *,
        model: str = "",
        use_sqlite_vec: bool = False,
        cache_limit: int = 200_000,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self.db = database
        self.model = model
        self.backend = "sqlite-vec" if use_sqlite_vec else "sqlite-bruteforce"
        self.cache_limit = cache_limit
        self.cancel = cancel_token or get_cancellation_token()
        self._lock = threading.RLock()
        self._cache: dict[int, array] | None = None
        self._cache_dim = 0
        self._vec_available = False
        if use_sqlite_vec:
            self._vec_available = self._try_load_vec_extension()
            if not self._vec_available:
                logger.warning("sqlite-vec 扩展不可用，退化为全量向量检索")
                self.backend = "sqlite-bruteforce"

    def _try_load_vec_extension(self) -> bool:
        try:
            import sqlite_vec  # type: ignore

            conn = self.db.conn
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            logger.info("已启用 sqlite-vec 原生向量扩展")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info("sqlite-vec 不可用：%s", exc)
            return False

    # ---- 写入 ----

    def upsert(
        self,
        chunk_ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        if not chunk_ids:
            return 0
        with self.db.transaction() as conn:
            for chunk_id, vector in zip(chunk_ids, vectors):
                blob = _pack_vector(vector)
                conn.execute(
                    "UPDATE kb_vectors SET vector_blob = ?, dimension = ?, backend = ? "
                    "WHERE chunk_id = ?",
                    (blob, len(vector), self.backend, int(chunk_id)),
                )
        self._invalidate()
        return len(chunk_ids)

    def delete(self, chunk_ids: Sequence[int]) -> int:
        ids = [int(c) for c in chunk_ids if c is not None]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE kb_vectors SET vector_blob = NULL WHERE chunk_id IN ({placeholders})",
                ids,
            )
        self._invalidate()
        return len(ids)

    def delete_by_message(self, message_id: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE kb_vectors SET vector_blob = NULL WHERE message_id = ?", (message_id,)
            )
            affected = cur.rowcount or 0
        self._invalidate()
        return affected

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM kb_vectors WHERE vector_blob IS NOT NULL")
        return int(row["c"]) if row else 0

    def reset(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE kb_vectors SET vector_blob = NULL")
        self._invalidate()

    # ---- 查询 ----

    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _load_cache(self) -> tuple[dict[int, array], int]:
        with self._lock:
            if self._cache is not None:
                return self._cache, self._cache_dim

            rows = self.db.query(
                "SELECT chunk_id, dimension, vector_blob FROM kb_vectors "
                "WHERE vector_blob IS NOT NULL LIMIT ?",
                (self.cache_limit,),
            )
            cache: dict[int, array] = {}
            dim = 0
            for row in rows:
                blob = row["vector_blob"]
                if not blob:
                    continue
                vector = _unpack_vector(blob)
                cache[int(row["chunk_id"])] = vector
                dim = max(dim, len(vector))
            self._cache = cache
            self._cache_dim = dim
            logger.debug("向量缓存已加载：%d 条，%d 维", len(cache), dim)
            return cache, dim

    def query(
        self,
        vector: Sequence[float],
        top_k: int = 10,
        *,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        if top_k <= 0:
            return []
        cache, dim = self._load_cache()
        if not cache:
            return []

        query_vec = array("f", vector)
        allowed: set[int] | None = None
        if where:
            allowed = self._filter_ids(where)
            if not allowed:
                return []

        hits: list[VectorHit] = []
        for chunk_id, stored in cache.items():
            if allowed is not None and chunk_id not in allowed:
                continue
            score = _dot(query_vec, stored, dim)
            hits.append(VectorHit(chunk_id=chunk_id, score=score))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def _filter_ids(self, where: dict[str, Any]) -> set[int]:
        """把元数据过滤下推到 SQL，避免全量加载后过滤。"""
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in where.items():
            if key in ("message_id", "folder", "model", "content_hash", "backend"):
                clauses.append(f"{key} = ?")
                params.append(value)
            elif key == "message_ids":
                ids = list(value)
                if not ids:
                    return set()
                clauses.append(f"message_id IN ({','.join('?' for _ in ids)})")
                params.extend(ids)
        sql = "SELECT chunk_id FROM kb_vectors WHERE vector_blob IS NOT NULL"
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        return {int(r["chunk_id"]) for r in self.db.query(sql, params)}

    def health(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "count": self.count(),
            "sqlite_vec": self._vec_available,
        }


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def create_vector_store(
    config: AppConfig,
    database: Database,
    *,
    model_name: str = "",
    cancel_token: CancellationToken | None = None,
) -> VectorStore:
    """按配置创建向量库，失败时自动降级到 SQLite 后端。"""
    cfg = config.vector
    token = cancel_token or get_cancellation_token()
    errors: list[str] = []

    if cfg.backend in ("auto", "chroma"):
        try:
            store = ChromaVectorStore(
                str(config.chroma_path), cfg.collection, cancel_token=token
            )
            logger.info("向量库后端：ChromaDB（%s）", config.chroma_path)
            return store
        except Exception as exc:  # noqa: BLE001
            errors.append(f"chroma: {exc}")
            if cfg.backend == "chroma":
                logger.error("ChromaDB 初始化失败，降级为 SQLite 后端：%s", exc)

    try:
        store = SqliteVectorStore(
            database,
            model=model_name,
            use_sqlite_vec=cfg.backend in ("auto", "sqlite-vec"),
            cache_limit=cfg.bruteforce_cache_limit,
            cancel_token=token,
        )
        if cfg.backend == "auto" and errors:
            logger.warning(
                "向量库已降级为 %s。若需 ChromaDB，请执行："
                "pip install -r requirements-optional.txt",
                store.backend,
            )
        logger.info("向量库后端：%s", store.backend)
        return store
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"无法初始化向量库：{exc}; 先前错误：{errors}") from exc


# ---------------------------------------------------------------------------
# 向量编解码 / 相似度
# ---------------------------------------------------------------------------

def _pack_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> array:
    values = array("f")
    values.frombytes(blob)
    return values


# 公开别名（供 indexer 等模块使用）
pack_vector = _pack_vector
unpack_vector = _unpack_vector


def _dot(a: array, b: array, dim: int) -> float:
    """余弦相似度（向量已在写入前归一化）。"""
    length = min(len(a), len(b), dim) if dim else min(len(a), len(b))
    total = 0.0
    for i in range(length):
        total += a[i] * b[i]
    return total


def _flatten_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Chroma 只接受 str/int/float/bool 元数据。"""
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def _flatten_where(where: dict[str, Any]) -> dict[str, Any]:
    """把简单字典转成 Chroma 的 where 语法。"""
    if len(where) == 1:
        key, value = next(iter(where.items()))
        if isinstance(value, (list, tuple, set)):
            return {key: {"$in": list(value)}}
        return {key: value}
    return {"$and": [{k: v} for k, v in where.items()]}
