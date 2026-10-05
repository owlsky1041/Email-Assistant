"""检索：关键词（FTS5/BM25）+ 向量（余弦）+ RRF 融合（§3.7 / §11.4）。

为什么必须用 RRF
----------------
BM25 分数是无界负数（越大越好但范围随语料变化），余弦相似度在 ``[-1, 1]``
且分布完全不同。直接加权相加需要人工调参且不稳定。
**Reciprocal Rank Fusion** 只依赖排名：

.. math::

    \\text{RRF}(d) = \\sum_{r \\in R} \\frac{w_r}{k + \\text{rank}_r(d)}

无需归一化，鲁棒性显著更好（``k`` 默认 60）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Literal

from .config import AppConfig
from .database import Database, build_match_query, build_match_query_variants
from .embedder import Embedder
from .models import SearchHit
from .vector_store import VectorStore

logger = logging.getLogger(__name__)

SearchMode = Literal["hybrid", "keyword", "vector"]


# ---------------------------------------------------------------------------
# 过滤条件
# ---------------------------------------------------------------------------

#: 字段级检索范围。``all`` 走原来的「关键词 + 向量」混合检索；
#: 其余值把查询**限定在某个字段**内匹配。
SearchScope = Literal["all", "subject", "sender", "recipient", "cc", "body"]

SCOPE_LABELS: dict[str, str] = {
    "all": "全部字段（混合检索）",
    "subject": "标题",
    "sender": "发件人",
    "recipient": "收件人",
    "cc": "抄送",
    "body": "邮件内容",
}

#: 能直接用 ``messages_fts`` 列过滤的范围
_FTS_COLUMN_SCOPE: dict[str, str] = {
    "subject": "subject",
    "sender": "sender",
    "recipient": "recipients",
}


@dataclass
@dataclass(slots=True)
class SearchFilters:
    """检索过滤条件（§3.7）。"""

    folder: list[str] | None = None
    account: str | None = None
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    has_attachments: bool | None = None
    include_deleted: bool = False

    def has_any(self) -> bool:
        return any(
            (
                self.folder,
                self.account,
                self.sender,
                self.recipient,
                self.subject,
                self.date_from,
                self.date_to,
                self.has_attachments is not None,
            )
        )


def build_message_where(filters: SearchFilters, *, alias: str = "m") -> tuple[str, list[Any]]:
    """构造 ``messages`` 表的 WHERE 片段（不含前导 AND）。"""
    clauses: list[str] = []
    params: list[Any] = []

    if not filters.include_deleted:
        clauses.append(f"{alias}.deleted_at IS NULL")
    if filters.account:
        clauses.append(f"{alias}.account = ?")
        params.append(filters.account)
    if filters.folder:
        placeholders = ",".join("?" for _ in filters.folder)
        clauses.append(f"{alias}.folder IN ({placeholders})")
        params.extend(filters.folder)
    if filters.sender:
        clauses.append(f"({alias}.sender LIKE ? OR {alias}.sender_name LIKE ?)")
        like = f"%{filters.sender}%"
        params.extend([like, like])
    if filters.recipient:
        clauses.append(f"({alias}.recipients LIKE ? OR {alias}.cc LIKE ?)")
        like = f"%{filters.recipient}%"
        params.extend([like, like])
    if filters.subject:
        clauses.append(f"{alias}.subject LIKE ?")
        params.append(f"%{filters.subject}%")
    if filters.date_from:
        clauses.append(f"{alias}.date_utc >= ?")
        params.append(filters.date_from.isoformat())
    if filters.date_to:
        clauses.append(f"{alias}.date_utc <= ?")
        params.append(filters.date_to.isoformat())
    if filters.has_attachments is not None:
        clauses.append(f"{alias}.has_attachments = ?")
        params.append(int(filters.has_attachments))

    return " AND ".join(clauses), params


# ---------------------------------------------------------------------------
# 片段生成
# ---------------------------------------------------------------------------

_TERM_SPLIT_RE = re.compile(r"[\s,，。；;、！!？?：:（）()\[\]{}<>\"'“”‘’/\\|@#$%^&*+=~`\-_]+")


def make_snippet(text: str, query: str, length: int = 200) -> str:
    """围绕首个命中词截取片段，避免把整封邮件塞进响应。"""
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= length:
        return flat

    terms = [t for t in _TERM_SPLIT_RE.split(query or "") if len(t) >= 1]
    position = -1
    for term in terms:
        found = flat.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return flat[:length].rstrip() + "…"

    half = length // 2
    start = max(0, position - half)
    end = min(len(flat), start + length)
    start = max(0, end - length)
    snippet = flat[start:end].strip()
    return f"{'…' if start > 0 else ''}{snippet}{'…' if end < len(flat) else ''}"


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(
    rankings: dict[str, list[int]],
    *,
    k: int = 60,
    weights: dict[str, float] | None = None,
) -> dict[int, float]:
    """多路召回结果的 RRF 融合。

    :param rankings: ``{通道名: [doc_id 按相关性降序]}``
    :return: ``{doc_id: 融合分数}``，分数越高越相关
    """
    weights = weights or {}
    scores: dict[int, float] = {}
    for channel, doc_ids in rankings.items():
        weight = weights.get(channel, 1.0)
        if weight <= 0:
            continue
        for rank, doc_id in enumerate(doc_ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
    return scores


# ---------------------------------------------------------------------------
# 检索引擎
# ---------------------------------------------------------------------------

def _as_factory(value: Any) -> Callable[[], Any] | None:
    """把实例或工厂统一成工厂函数。"""
    if value is None:
        return None
    if callable(value) and not isinstance(value, (VectorStore, Embedder)):
        return value
    return lambda: value


@dataclass(slots=True)
class _Candidate:
    chunk_id: int | None
    message_pk: int
    text: str = ""
    chunk_index: int | None = None
    keyword_rank: int | None = None
    vector_rank: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class SearchEngine:
    """统一检索入口。

    ``vector_store`` / ``embedder`` 既可以传实例，也可以传**工厂函数**。
    传工厂时，只有真正走到向量通道才会创建对象——这样纯关键词检索、
    ``/api/messages`` 列表接口都不会触发嵌入模型加载。
    """

    def __init__(
        self,
        database: Database,
        *,
        config: AppConfig,
        vector_store: VectorStore | Callable[[], VectorStore] | None = None,
        embedder: Embedder | Callable[[], Embedder] | None = None,
    ) -> None:
        self.db = database
        self.config = config
        self._vector_store_factory = _as_factory(vector_store)
        self._embedder_factory = _as_factory(embedder)
        self._vector_store_cache: VectorStore | None = None
        self._embedder_cache: Embedder | None = None

    # -- 惰性解析 ------------------------------------------------------

    def _get_vector_store(self) -> VectorStore | None:
        if self._vector_store_cache is None and self._vector_store_factory is not None:
            self._vector_store_cache = self._vector_store_factory()
        return self._vector_store_cache

    def _get_embedder(self) -> Embedder | None:
        if self._embedder_cache is None and self._embedder_factory is not None:
            self._embedder_cache = self._embedder_factory()
        return self._embedder_cache

    @property
    def vector_store(self) -> VectorStore | None:
        """仅在显式访问时才实例化（统计接口等）。"""
        return self._get_vector_store()

    @property
    def embedder(self) -> Embedder | None:
        return self._get_embedder()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        mode: SearchMode = "hybrid",
        filters: SearchFilters | None = None,
        snippet_length: int | None = None,
        scope: SearchScope = "all",
    ) -> list[SearchHit]:
        cfg = self.config.search
        limit = limit or cfg.default_limit
        filters = filters or SearchFilters()
        snippet_length = snippet_length or cfg.snippet_length
        candidate_pool = max(limit * cfg.candidate_multiplier, limit)

        if scope != "all":
            # 字段级检索只做**关键词匹配**：向量索引建在切片正文上，
            # 拿它去匹配"发件人"这类元数据字段没有意义，硬做只会给出
            # 看似相关实则无关的结果。
            return self._field_scoped_search(
                query, scope, filters, limit, snippet_length
            )

        candidates: dict[int, _Candidate] = {}
        rankings: dict[str, list[int]] = {}

        if mode in ("hybrid", "keyword"):
            keyword_ranking = self._keyword_channel(
                query, candidate_pool, filters, candidates
            )
            if keyword_ranking:
                rankings["keyword"] = keyword_ranking

        if mode in ("hybrid", "vector"):
            vector_ranking = self._vector_channel(query, candidate_pool, filters, candidates)
            if vector_ranking:
                rankings["vector"] = vector_ranking

        if not candidates:
            return []

        if mode == "keyword":
            fused = {cid: 1.0 / (cfg.rrf_k + i) for i, cid in enumerate(rankings.get("keyword", []), 1)}
        elif mode == "vector":
            fused = {cid: 1.0 / (cfg.rrf_k + i) for i, cid in enumerate(rankings.get("vector", []), 1)}
        else:
            fused = reciprocal_rank_fusion(
                rankings,
                k=cfg.rrf_k,
                weights={
                    "keyword": cfg.keyword_weight,
                    "vector": cfg.vector_weight,
                },
            )

        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        hits: list[SearchHit] = []
        for chunk_id, score in ordered:
            candidate = candidates.get(chunk_id)
            if candidate is None:
                continue
            hit = self._hydrate(candidate, score, query, snippet_length, mode)
            if hit is not None:
                hits.append(hit)
        return hits

    # ------------------------------------------------------------------
    # 字段级检索
    # ------------------------------------------------------------------

    def _field_scoped_search(
        self,
        query: str,
        scope: SearchScope,
        filters: SearchFilters,
        limit: int,
        snippet_length: int,
    ) -> list[SearchHit]:
        """把查询限定在某个字段内匹配。

        * 标题 / 发件人 / 收件人 → ``messages_fts`` 的**列过滤**
          （``subject : ("发 票" AND "报 销")``），保留 FTS 的排序能力；
        * 邮件内容 → ``chunks_fts``（正文切片就是它的匹配目标）；
        * **抄送没有单独建索引**，走 ``messages.cc`` 的 LIKE。
          为一个字段重建全部 FTS 需要迁移用户已有的上万封库，
          对一个"偶尔用用"的筛选条件不值得冒这个险。
        """
        text = (query or "").strip()
        if not text:
            return []
        if not self.db.fts_available and scope != "cc":
            return self._field_like_fallback(text, scope, filters, limit, snippet_length)

        variants = build_match_query_variants(text) or [build_match_query(text)]
        variants = [v for v in variants if v]
        if not variants:
            return self._field_like_fallback(text, scope, filters, limit, snippet_length)

        candidates: dict[int, _Candidate] = {}
        for match_expr in variants:
            keyword_ranking = self._field_channel(
                match_expr, scope, limit, filters, candidates
            )
            if keyword_ranking:
                if match_expr != variants[0]:
                    logger.debug("字段检索使用放宽表达式：%s", match_expr)
                break

        if not candidates:
            return self._field_like_fallback(text, scope, filters, limit, snippet_length)

        ordered = sorted(
            candidates.values(),
            key=lambda c: (c.keyword_rank if c.keyword_rank is not None else 10**9),
        )[:limit]
        hits: list[SearchHit] = []
        for candidate in ordered:
            hit = self._hydrate(candidate, 1.0, text, snippet_length, "keyword")
            if hit is not None:
                hits.append(hit)
        return hits

    def _field_channel(
        self,
        match_expr: str,
        scope: SearchScope,
        pool: int,
        filters: SearchFilters,
        candidates: dict[int, _Candidate],
    ) -> list[int]:
        if scope == "cc":
            rows = self._field_cc_rows(match_expr_source=match_expr, pool=pool, filters=filters)
            return self._collect_message_rows(rows, candidates, channel="keyword")

        if scope == "body":
            rows = self._keyword_chunks(match_expr, pool, filters)
            return self._collect_chunk_rows(rows, candidates, channel="keyword")

        column = _FTS_COLUMN_SCOPE.get(scope)
        if column is None:
            return []
        # FTS5 列过滤：`col : (expr)`。整段 expr 必须放在括号里，
        # 否则 `subject : "a" AND "b"` 会被解析成 `(subject:a) AND (b)` ——
        # "b" 就跑到别的字段去了。
        scoped = f"{column} : ({match_expr})"
        rows = self._keyword_messages(scoped, pool, filters)
        if rows:
            return self._collect_message_rows(rows, candidates, channel="keyword")

        # messages_fts 只覆盖"发件人地址 + 收件人"，发件人姓名在 m.sender_name，
        # 收件人里也常只有地址；补一次 LIKE 兜住中文姓名。
        like_column = {"sender": ("m.sender_name", "m.sender"),
                       "recipient": ("m.recipients",),
                       "subject": ("m.subject",)}[scope]
        return self._collect_message_rows(
            self._field_like_rows(match_expr, like_column, pool, filters, scope),
            candidates,
            channel="keyword",
        )

    def _field_like_fallback(
        self,
        query: str,
        scope: SearchScope,
        filters: SearchFilters,
        limit: int,
        snippet_length: int,
    ) -> list[SearchHit]:
        """FTS 不可用（或抄送字段）时的 LIKE 路径。"""
        columns = {
            "subject": ("m.subject",),
            "sender": ("m.sender", "m.sender_name"),
            "recipient": ("m.recipients",),
            "cc": ("m.cc",),
            "body": ("m.body_text",),
        }.get(scope)
        if not columns:
            return []

        rows = self._field_like_rows_query(query, columns, limit, filters)
        candidates: dict[int, _Candidate] = {}
        self._collect_message_rows(rows, candidates, channel="keyword")
        hits: list[SearchHit] = []
        for candidate in candidates.values():
            hit = self._hydrate(candidate, 1.0, query, snippet_length, "keyword")
            if hit is not None:
                hits.append(hit)
        return hits

    def _field_like_rows_query(
        self, query: str, columns: Sequence[str], pool: int, filters: SearchFilters
    ) -> list[Any]:
        terms = [t for t in _TERM_SPLIT_RE.split(query or "") if t][:5]
        if not terms:
            return []
        where_sql, params = build_message_where(filters)
        clauses = []
        like_params: list[Any] = []
        for term in terms:
            ors = " OR ".join(f"{col} LIKE ?" for col in columns)
            clauses.append(f"({ors})")
            like_params.extend([f"%{term}%"] * len(columns))
        sql = f"""
            SELECT m.id AS message_pk, 0.0 AS score
            FROM messages m
            WHERE {where_sql} AND {' AND '.join(clauses)}
            ORDER BY m.date_utc DESC
            LIMIT ?
        """
        return self.db.query(sql, [*params, *like_params, pool])

    def _field_cc_rows(
        self, *, match_expr_source: str, pool: int, filters: SearchFilters
    ) -> list[Any]:
        terms = [t for t in _TERM_SPLIT_RE.split(match_expr_source or "") if t][:5]
        return self._field_like_rows_query(
            " ".join(terms), ("m.cc",), pool, filters
        ) if terms else []

    def _field_like_rows(
        self,
        match_expr: str,
        columns: Sequence[str],
        pool: int,
        filters: SearchFilters,
        scope: SearchScope,
    ) -> list[Any]:
        terms = [t for t in _TERM_SPLIT_RE.split(match_expr or "") if t][:5]
        # match_expr 里带引号与 AND，先剥成裸词再 LIKE
        terms = [t.strip('"*() ') for t in terms]
        terms = [t for t in terms if t and t.upper() != "AND"]
        return self._field_like_rows_query(" ".join(terms), columns, pool, filters) if terms else []

    # ------------------------------------------------------------------
    # 关键词通道
    # ------------------------------------------------------------------

    def _keyword_channel(
        self,
        query: str,
        pool: int,
        filters: SearchFilters,
        candidates: dict[int, _Candidate],
    ) -> list[int]:
        """优先在切片粒度检索；没有切片时退化到邮件粒度。

        中文查询会依次尝试「严格短语 -> 逐步截短尾部」的变体，
        避免自然语言问句因为要求整句连续匹配而零命中。
        """
        if not query or not query.strip():
            return []
        if not self.db.fts_available:
            return self._keyword_like_fallback(query, pool, filters, candidates)

        variants = build_match_query_variants(query)
        if not variants:
            return self._keyword_like_fallback(query, pool, filters, candidates)

        for match_expr in variants:
            chunk_rows = self._keyword_chunks(match_expr, pool, filters)
            if chunk_rows:
                if match_expr != variants[0]:
                    logger.debug("关键词检索使用放宽表达式：%s", match_expr)
                return self._collect_chunk_rows(chunk_rows, candidates, channel="keyword")

            message_rows = self._keyword_messages(match_expr, pool, filters)
            if message_rows:
                if match_expr != variants[0]:
                    logger.debug("关键词检索使用放宽表达式：%s", match_expr)
                return self._collect_message_rows(message_rows, candidates, channel="keyword")

        return self._keyword_like_fallback(query, pool, filters, candidates)

    def _keyword_chunks(
        self, match_expr: str, pool: int, filters: SearchFilters
    ) -> list[Any]:
        where_sql, params = build_message_where(filters)
        sql = f"""
            SELECT c.id            AS chunk_id,
                   c.message_pk    AS message_pk,
                   c.chunk_index   AS chunk_index,
                   c.text          AS text,
                   bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN kb_chunks c ON c.id = chunks_fts.rowid
            JOIN messages  m ON m.id = c.message_pk
            WHERE chunks_fts MATCH ?
              AND {where_sql}
            ORDER BY score
            LIMIT ?
        """
        try:
            return self.db.query(sql, [match_expr, *params, pool])
        except Exception as exc:  # noqa: BLE001 - 畸形 MATCH 表达式会抛错
            logger.debug("切片 FTS 查询失败：%s", exc)
            return []

    def _keyword_messages(
        self, match_expr: str, pool: int, filters: SearchFilters
    ) -> list[Any]:
        where_sql, params = build_message_where(filters)
        sql = f"""
            SELECT m.id AS message_pk,
                   bm25(messages_fts) AS score
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            WHERE messages_fts MATCH ?
              AND {where_sql}
            ORDER BY score
            LIMIT ?
        """
        try:
            return self.db.query(sql, [match_expr, *params, pool])
        except Exception as exc:  # noqa: BLE001
            logger.debug("邮件 FTS 查询失败：%s", exc)
            return []

    def _keyword_like_fallback(
        self, query: str, pool: int, filters: SearchFilters, candidates: dict[int, _Candidate]
    ) -> list[int]:
        """FTS 不可用时的 LIKE 兜底（诚实告知性能代价）。"""
        terms = [t for t in _TERM_SPLIT_RE.split(query or "") if len(t) >= 2][:5]
        if not terms:
            return []
        where_sql, params = build_message_where(filters)
        like_clauses = " AND ".join(
            "(m.subject LIKE ? OR m.body_text LIKE ?)" for _ in terms
        )
        sql = f"""
            SELECT m.id AS message_pk FROM messages m
            WHERE {where_sql} AND {like_clauses}
            ORDER BY m.date_utc DESC LIMIT ?
        """
        like_params: list[Any] = []
        for term in terms:
            like_params.extend([f"%{term}%", f"%{term}%"])
        rows = self.db.query(sql, [*params, *like_params, pool])
        return self._collect_message_rows(rows, candidates, channel="keyword")

    def _collect_chunk_rows(
        self, rows: list[Any], candidates: dict[int, _Candidate], *, channel: str
    ) -> list[int]:
        ranking: list[int] = []
        for rank, row in enumerate(rows, start=1):
            chunk_id = int(row["chunk_id"])
            existing = candidates.get(chunk_id)
            if existing is None:
                candidates[chunk_id] = _Candidate(
                    chunk_id=chunk_id,
                    message_pk=int(row["message_pk"]),
                    text=row["text"] or "",
                    chunk_index=int(row["chunk_index"] or 0),
                )
            if channel == "keyword":
                candidates[chunk_id].keyword_rank = rank
                candidates[chunk_id].keyword_score = float(row["score"])
            else:
                candidates[chunk_id].vector_rank = rank
                candidates[chunk_id].vector_score = float(row["score"])
            ranking.append(chunk_id)
        return ranking

    def _collect_message_rows(
        self, rows: list[Any], candidates: dict[int, _Candidate], *, channel: str
    ) -> list[int]:
        """邮件粒度结果：用 ``-message_pk`` 作为候选键，避免与 chunk_id 冲突。"""
        ranking: list[int] = []
        for rank, row in enumerate(rows, start=1):
            message_pk = int(row["message_pk"])
            key = -message_pk
            if key not in candidates:
                candidates[key] = _Candidate(
                    chunk_id=None, message_pk=message_pk, text="", chunk_index=None
                )
            if channel == "keyword":
                candidates[key].keyword_rank = rank
                candidates[key].keyword_score = float(row["score"])
            else:
                candidates[key].vector_rank = rank
                candidates[key].vector_score = float(row["score"])
            ranking.append(key)
        return ranking

    # ------------------------------------------------------------------
    # 向量通道
    # ------------------------------------------------------------------

    def _vector_channel(
        self,
        query: str,
        pool: int,
        filters: SearchFilters,
        candidates: dict[int, _Candidate],
    ) -> list[int]:
        store = self._get_vector_store()
        embedder = self._get_embedder()
        if store is None or embedder is None:
            return []
        if not query or not query.strip() or store.count() == 0:
            return []

        try:
            vector = embedder.embed_query(query)
        except Exception as exc:  # noqa: BLE001
            logger.error("查询向量化失败：%s", exc)
            return []

        # 向量库里只有少量元数据字段，其余过滤条件必须先解析成允许的 message_pk 集合，
        # 否则 sender / 日期 / 主题等筛选会被静默忽略。
        allowed: set[int] | None = None
        if filters.has_any():
            allowed = self._allowed_message_pks(filters)
            if not allowed:
                return []

        # 过滤会淘汰部分结果，因此允许多取一些候选再截断
        fetch_k = pool if allowed is None else min(pool * 5, max(pool, 500))
        where = self._vector_where(filters)
        try:
            hits = store.query(vector, top_k=fetch_k, where=where)
        except Exception as exc:  # noqa: BLE001
            logger.error("向量检索失败：%s", exc)
            return []

        ranking: list[int] = []
        threshold = self._effective_min_score(embedder)
        margin = self.config.search.vector_score_margin
        relative_cut = (hits[0].score - margin) if (margin and hits) else None
        if relative_cut is not None:
            threshold = max(threshold, relative_cut)

        dropped = 0
        for hit in hits:
            if len(ranking) >= pool:
                break
            if threshold and hit.score < threshold:
                # 相似度不足：宁可少返回，也不要污染上下文
                dropped += 1
                continue
            chunk_id = hit.chunk_id
            message_pk = self._message_pk_for_hit(hit, candidates)
            if allowed is not None and message_pk not in allowed:
                continue
            if chunk_id not in candidates:
                candidates[chunk_id] = _Candidate(
                    chunk_id=chunk_id, message_pk=message_pk, text=hit.metadata.get("text", "")
                )
            elif message_pk:
                candidates[chunk_id].message_pk = message_pk
            candidates[chunk_id].vector_rank = len(ranking) + 1
            candidates[chunk_id].vector_score = hit.score
            ranking.append(chunk_id)
        if dropped:
            logger.debug(
                "向量召回过滤掉 %d 条低相似度结果（阈值 %.3f）", dropped, threshold
            )
        return ranking

    def _effective_min_score(self, embedder: Embedder) -> float:
        """确定实际生效的相似度下限。

        显式配置优先；未配置（0）时采用嵌入后端的推荐值，
        避免切换后端时召回行为被一个全局魔数悄悄改变。
        """
        configured = self.config.search.min_vector_score
        if configured and configured > 0:
            return float(configured)
        return float(getattr(embedder, "recommended_min_score", 0.0) or 0.0)

    def _message_pk_for_hit(
        self, hit: Any, candidates: dict[int, _Candidate]
    ) -> int:
        """确定向量命中对应的 ``messages.id``。"""
        existing = candidates.get(hit.chunk_id)
        if existing is not None and existing.message_pk:
            return existing.message_pk
        meta_pk = (hit.metadata or {}).get("message_pk")
        if meta_pk:
            try:
                return int(meta_pk)
            except (TypeError, ValueError):
                pass
        chunk = self.db.get_chunk(hit.chunk_id)
        return int(chunk["message_pk"]) if chunk else 0

    def _allowed_message_pks(self, filters: SearchFilters) -> set[int]:
        """把过滤条件解析成允许的 ``messages.id`` 集合。"""
        where_sql, params = build_message_where(filters)
        if not where_sql:
            return set()
        rows = self.db.query(f"SELECT m.id FROM messages m WHERE {where_sql}", params)
        return {int(row["id"]) for row in rows}

    def _vector_where(self, filters: SearchFilters) -> dict[str, Any] | None:
        """向量库支持的服务端过滤（能下推就下推，减少候选量）。"""
        where: dict[str, Any] = {}
        if filters.folder and len(filters.folder) == 1:
            where["folder"] = filters.folder[0]
        if filters.account:
            where["account"] = filters.account
        return where or None

    # ------------------------------------------------------------------
    # 结果组装
    # ------------------------------------------------------------------

    def _hydrate(
        self,
        candidate: _Candidate,
        score: float,
        query: str,
        snippet_length: int,
        mode: SearchMode,
    ) -> SearchHit | None:
        message_pk = candidate.message_pk
        text = candidate.text
        chunk_index = candidate.chunk_index

        # 邮件粒度候选（或向量库只返回了 chunk_id）需要补查数据库
        if not message_pk or (candidate.chunk_id is not None and not text):
            chunk = (
                self.db.get_chunk(candidate.chunk_id)
                if candidate.chunk_id is not None
                else None
            )
            if chunk is None:
                return None
            message_pk = int(chunk["message_pk"])
            text = chunk["text"] or text
            chunk_index = int(chunk["chunk_index"])

        row = self.db.query_one(
            """SELECT id, message_id, subject, sender, sender_name, date_utc, folder,
                      local_markdown_path, body_text
               FROM messages WHERE id = ?""",
            (message_pk,),
        )
        if row is None:
            return None

        body = text or row["body_text"] or ""
        sender = row["sender"] or ""
        if row["sender_name"]:
            sender = f"{row['sender_name']} <{sender}>"

        if mode == "keyword":
            source: Literal["keyword", "vector", "hybrid"] = "keyword"
        elif mode == "vector":
            source = "vector"
        else:
            source = "hybrid"

        return SearchHit(
            message_pk=message_pk,
            message_id=row["message_id"] or "",
            subject=row["subject"] or "",
            sender=sender,
            date=row["date_utc"] or "",
            folder=row["folder"] or "",
            snippet=make_snippet(body, query, snippet_length),
            local_markdown_path=row["local_markdown_path"] or "",
            score=score,
            chunk_id=candidate.chunk_id,
            chunk_index=chunk_index,
            source=source,
            keyword_rank=candidate.keyword_rank,
            vector_rank=candidate.vector_rank,
            keyword_score=candidate.keyword_score,
            vector_score=candidate.vector_score,
        )

    # ------------------------------------------------------------------
    # 邮件列表（非检索）
    # ------------------------------------------------------------------

    def list_messages(
        self,
        *,
        filters: SearchFilters | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "date_desc",
    ) -> tuple[list[dict[str, Any]], int]:
        """分页列出邮件，返回 ``(rows, total)``。"""
        filters = filters or SearchFilters()
        where_sql, params = build_message_where(filters)
        where_clause = f"WHERE {where_sql}" if where_sql else ""

        order_sql = {
            "date_desc": "m.date_utc DESC, m.id DESC",
            "date_asc": "m.date_utc ASC, m.id ASC",
            "subject": "m.subject ASC",
            "sender": "m.sender ASC",
        }.get(order, "m.date_utc DESC, m.id DESC")

        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS c FROM messages m {where_clause}", params
        )
        total = int(total_row["c"]) if total_row else 0

        rows = self.db.query(
            f"""
            SELECT m.id, m.account, m.message_id, m.uid, m.folder, m.subject, m.sender,
                   m.sender_name, m.recipients, m.cc, m.date_utc, m.date_raw,
                   m.local_markdown_path, m.has_attachments, m.size_bytes, m.synced_at
            FROM messages m
            {where_clause}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["has_attachments"] = bool(item["has_attachments"])
            item.pop("body_text", None)
            out.append(item)
        return out, total

    # ------------------------------------------------------------------

    def statistics(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "messages": self.db.count_messages(),
            "chunks": self.db.count_chunks(),
            "fts_available": self.db.fts_available,
        }
        store = self._get_vector_store()
        if store is not None:
            stats["vectors"] = store.count()
            stats["vector_backend"] = store.backend
        embedder = self._get_embedder()
        if embedder is not None:
            stats["embedding"] = embedder.health()
        return stats
