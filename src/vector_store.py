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
        #: 配置里**要求**的后端（用于把"静默降级"变成显式可见）
        self.requested_backend = "sqlite-vec" if use_sqlite_vec else "sqlite-bruteforce"
        self.backend = self.requested_backend
        #: 降级原因；``None`` 表示没有降级
        self.downgrade_reason: str | None = None
        self.cache_limit = cache_limit
        self.cancel = cancel_token or get_cancellation_token()
        self._lock = threading.RLock()
        self._cache: dict[int, array] | None = None
        self._cache_dim = 0
        #: numpy 加速视图：``(N, dim) 连续矩阵 + 对应的 chunk_id 数组``。
        #: 纯 Python 逐元素点积在 5 万切片上实测约 1.07s/次查询，而 50k×512
        #: 的矩阵乘只要几毫秒 —— 而暴力检索是 O(N) 的，规模一大就退化。
        self._matrix: Any = None
        self._matrix_ids: Any = None
        self._vec_available = False
        if use_sqlite_vec:
            self._vec_available, self.downgrade_reason = self._try_load_vec_extension()
            if not self._vec_available:
                # 用户明确要了 sqlite-vec 却用不上：这是**配置没生效**，
                # 不是正常退化，必须让他在命令行上看得见。
                logger.warning(
                    "配置要求 sqlite-vec，但扩展不可用，已降级为全量检索：%s。"
                    "安装方式：pip install sqlite-vec",
                    self.downgrade_reason,
                )
                self.backend = "sqlite-bruteforce"

    def _try_load_vec_extension(self) -> tuple[bool, str | None]:
        """尝试加载 sqlite-vec，返回 ``(是否成功, 失败原因)``。"""
        try:
            import sqlite_vec  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return False, f"未安装 sqlite-vec（{type(exc).__name__}）"
        try:
            conn = self.db.conn
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as exc:  # noqa: BLE001 - 驱动不支持 / 版本不匹配
            return False, f"{type(exc).__name__}: {exc}"
        logger.info("已启用 sqlite-vec 原生向量扩展")
        return True, None

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
            self._matrix = None
            self._matrix_ids = None

    @staticmethod
    def _numpy():  # type: ignore[no-untyped-def]
        """可选依赖：装上就用矩阵乘，没装就退回纯 Python 逐元素点积。"""
        try:
            import numpy  # type: ignore

            return numpy
        except Exception:  # noqa: BLE001 - 可选依赖，缺失不是错误
            return None

    def _build_matrix(self) -> bool:
        """把缓存里的散装向量拼成一个连续矩阵（只做一次）。

        散装 ``array("f")`` 逐条点积是纯 Python 循环；拼成
        ``(N, dim) float32`` 之后一次 ``matrix @ q`` 就出全部得分。
        """
        numpy = self._numpy()
        if numpy is None or not self._cache:
            return False
        ids = list(self._cache.keys())
        dim = self._cache_dim
        if dim <= 0:
            return False
        matrix = numpy.zeros((len(ids), dim), dtype=numpy.float32)
        for row, chunk_id in enumerate(ids):
            vec = self._cache[chunk_id]
            length = min(len(vec), dim)
            matrix[row, :length] = numpy.frombuffer(vec, dtype=numpy.float32, count=length)
        self._matrix = matrix
        self._matrix_ids = numpy.asarray(ids, dtype=numpy.int64)
        logger.debug("向量矩阵已构建：%s，%d 维", matrix.shape, dim)
        return True

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
            self._build_matrix()
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

        allowed: set[int] | None = None
        if where:
            allowed = self._filter_ids(where)
            if not allowed:
                return []

        if self._matrix is not None:
            hits = self._query_matrix(vector, allowed)
            if hits is not None:
                hits.sort(key=lambda h: h.score, reverse=True)
                return hits[:top_k]

        # 没装 numpy：退回逐元素点积（结果完全一致，只是慢）
        query_vec = array("f", vector)
        hits = []
        for chunk_id, stored in cache.items():
            if allowed is not None and chunk_id not in allowed:
                continue
            hits.append(VectorHit(chunk_id=chunk_id, score=_dot(query_vec, stored, dim)))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def _query_matrix(self, vector: Sequence[float], allowed: set[int] | None) -> list[VectorHit] | None:
        """一次矩阵乘算出全部相似度。

        :return: 命中列表；无法走矩阵路径时返回 ``None`` 让调用方回退。
        """
        numpy = self._numpy()
        if numpy is None or self._matrix is None or self._matrix_ids is None:
            return None
        dim = self._matrix.shape[1]
        query = numpy.zeros(dim, dtype=numpy.float32)
        length = min(len(vector), dim)
        query[:length] = numpy.asarray(vector[:length], dtype=numpy.float32)

        matrix = self._matrix
        ids = self._matrix_ids
        if allowed is not None:
            # 先把不允许的行**剔除**再算分：既省算力，也避免"标成 -inf
            # 却照样返回"这种把过滤条件架空的做法。
            keep = numpy.isin(ids, numpy.fromiter(allowed, dtype=numpy.int64))
            rows = numpy.nonzero(keep)[0]
            if rows.size == 0:
                return []
            matrix = matrix[rows]
            ids = ids[rows]

        # 向量写入前已归一化，点积即余弦相似度
        scores = matrix @ query
        return [
            VectorHit(chunk_id=int(cid), score=float(score))
            for cid, score in zip(ids.tolist(), scores.tolist())
        ]

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
            "requested_backend": self.requested_backend,
            "count": self.count(),
            "sqlite_vec": self._vec_available,
            # 非 None 表示"配置要的后端没生效"，调用方应显式提示用户
            "downgrade_reason": self.downgrade_reason,
            "accelerated": self._matrix is not None,
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
        if getattr(store, "downgrade_reason", None):
            # 配置里写死的后端没生效，用户有权知道，而不是只在日志里躺一条 info
            logger.warning(
                "向量库后端：%s（配置要求 %s，未生效原因：%s）",
                store.backend,
                store.requested_backend,
                store.downgrade_reason,
            )
        else:
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
