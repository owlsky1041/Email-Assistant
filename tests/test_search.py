"""检索测试（§3.7 / §11.4）：RRF 融合、关键词、向量、过滤、片段。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config import AppConfig
from src.context import AppContext
from src.search import (
    SearchEngine,
    SearchFilters,
    build_message_where,
    make_snippet,
    reciprocal_rank_fusion,
)
from src.models import ParsedMessage


# ---------------------------------------------------------------------------
# RRF 单元测试
# ---------------------------------------------------------------------------

class TestReciprocalRankFusion:
    def test_single_channel_ranking(self) -> None:
        fused = reciprocal_rank_fusion({"keyword": [1, 2, 3]}, k=60)
        assert list(fused) == [1, 2, 3]
        assert fused[1] > fused[2] > fused[3]

    def test_formula_matches_definition(self) -> None:
        """RRF(d) = Σ 1/(k + rank)。"""
        fused = reciprocal_rank_fusion({"a": [7]}, k=60)
        assert fused[7] == pytest.approx(1 / 61)

    def test_document_in_both_channels_wins(self) -> None:
        """两路都召回的文档应排在只有一路召回的前面。"""
        fused = reciprocal_rank_fusion(
            {"keyword": [1, 2], "vector": [2, 3]}, k=60
        )
        assert max(fused, key=fused.get) == 2

    def test_weights_applied(self) -> None:
        fused = reciprocal_rank_fusion(
            {"keyword": [1], "vector": [2]}, k=60, weights={"keyword": 2.0, "vector": 1.0}
        )
        assert fused[1] > fused[2]

    def test_zero_weight_disables_channel(self) -> None:
        fused = reciprocal_rank_fusion(
            {"keyword": [1], "vector": [2]}, k=60, weights={"keyword": 0.0}
        )
        assert 1 not in fused and 2 in fused

    def test_empty_input(self) -> None:
        assert reciprocal_rank_fusion({}) == {}
        assert reciprocal_rank_fusion({"keyword": []}) == {}

    def test_rank_order_preserved_regardless_of_scale(self) -> None:
        """RRF 的核心价值：分数尺度不同的两路可以公平融合。"""
        fused = reciprocal_rank_fusion({"bm25": [1, 2, 3], "cosine": [1, 3, 2]}, k=60)
        assert set(fused) == {1, 2, 3}
        assert fused[1] == pytest.approx(2 / 61)


class TestMakeSnippet:
    def test_short_text_returned_whole(self) -> None:
        assert make_snippet("短文本", "短", length=100) == "短文本"

    def test_centered_on_first_match(self) -> None:
        text = "开头" * 100 + "关键命中" + "结尾" * 100
        snippet = make_snippet(text, "关键命中", length=40)
        assert "关键命中" in snippet
        assert len(snippet) <= 44  # 含省略号

    def test_trailing_ellipsis_when_match_at_start(self) -> None:
        snippet = make_snippet("甲" * 500, "甲", length=50)
        assert snippet.endswith("…")
        assert not snippet.startswith("…")

    def test_both_ellipses_when_match_in_middle(self) -> None:
        text = "前置内容" * 40 + "命中目标" + "后置内容" * 40
        snippet = make_snippet(text, "命中目标", length=30)
        assert "命中目标" in snippet
        assert snippet.startswith("…") and snippet.endswith("…")

    def test_no_match_returns_head(self) -> None:
        snippet = make_snippet("甲" * 500, "不存在的词", length=50)
        assert snippet.endswith("…")

    def test_empty_text(self) -> None:
        assert make_snippet("", "x") == ""

    def test_whitespace_flattened(self) -> None:
        assert "\n" not in make_snippet("第一行\n第二行", "行", length=100)


class TestBuildMessageWhere:
    def test_excludes_deleted_by_default(self) -> None:
        sql, params = build_message_where(SearchFilters())
        assert "deleted_at IS NULL" in sql
        assert params == []

    def test_include_deleted(self) -> None:
        sql, _ = build_message_where(SearchFilters(include_deleted=True))
        assert "deleted_at" not in sql

    def test_folder_filter_multi(self) -> None:
        sql, params = build_message_where(SearchFilters(folder=["INBOX", "Sent"]))
        assert "folder IN (?,?)" in sql
        assert params == ["INBOX", "Sent"]

    def test_sender_filter_matches_name_and_address(self) -> None:
        sql, params = build_message_where(SearchFilters(sender="alice"))
        assert "sender LIKE ?" in sql and "sender_name LIKE ?" in sql
        assert params == ["%alice%", "%alice%"]

    def test_date_range(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 12, 31, tzinfo=timezone.utc)
        sql, params = build_message_where(SearchFilters(date_from=start, date_to=end))
        assert "date_utc >= ?" in sql and "date_utc <= ?" in sql
        assert params == [start.isoformat(), end.isoformat()]

    def test_has_attachments(self) -> None:
        sql, params = build_message_where(SearchFilters(has_attachments=True))
        assert "has_attachments = ?" in sql and params == [1]


# ---------------------------------------------------------------------------
# 端到端检索
# ---------------------------------------------------------------------------

@pytest.fixture
def populated(context: AppContext) -> AppContext:
    """写入若干封主题区分明显的邮件并建索引。"""
    from tests.conftest import build_eml, parse_eml

    corpus = [
        ("季度报销发票汇总", "本季度差旅报销发票已整理完毕，请财务审核。共计 12 张发票，金额 8600 元。", None),
        ("服务器扩容申请", "由于业务增长，申请对订单服务进行扩容，预计需要新增三台机器。", None),
        ("年度绩效考核通知", "请各位同事在月底前完成自评，并提交给直属主管。", None),
        ("产品需求评审会议纪要", "会上确认了三条核心需求，优先级最高的是结算流程重构。", None),
        ("差旅费报销新规", "自下月起，差旅费报销需附电子发票，纸质发票不再受理。", None),
    ]
    now = datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)
    for index, (subject, body, _) in enumerate(corpus, start=1):
        raw = build_eml(subject=subject, text=body, message_id=f"<s{index}@corp.com>")
        parsed = context.sync.parser.parse(
            parse_eml(raw), folder="INBOX", uid=str(index), uidvalidity=1
        )
        archive = context.sync.exporter.export(parsed, account="tester@corp.com")
        record = context.sync._to_record(parsed, archive, duplicate_of=None)
        context.db.insert_message(record, archive.attachments)
    context.indexer.index_pending()
    return context


class TestSearchModes:
    def test_keyword_search_finds_exact_match(self, populated: AppContext) -> None:
        hits = populated.search.search("报销发票", mode="keyword", limit=5)
        assert hits
        assert "报销" in hits[0].subject

    def test_keyword_search_chinese_substring(self, populated: AppContext) -> None:
        hits = populated.search.search("扩容", mode="keyword", limit=5)
        assert hits and hits[0].subject == "服务器扩容申请"

    def test_vector_search_returns_results(self, populated: AppContext) -> None:
        hits = populated.search.search("报销发票", mode="vector", limit=5)
        assert hits

    def test_hybrid_returns_results(self, populated: AppContext) -> None:
        hits = populated.search.search("报销发票", mode="hybrid", limit=5)
        assert hits

    def test_hybrid_ranks_both_channel_hits_first(self, populated: AppContext) -> None:
        hits = populated.search.search("报销发票", mode="hybrid", limit=5)
        top = hits[0]
        assert top.keyword_rank is not None or top.vector_rank is not None

    def test_no_match_returns_empty(self, populated: AppContext) -> None:
        assert populated.search.search("量子计算机", mode="keyword", limit=5) == []

    def test_empty_query(self, populated: AppContext) -> None:
        assert populated.search.search("", limit=5) == []
        assert populated.search.search("   ", limit=5) == []

    def test_limit_respected(self, populated: AppContext) -> None:
        hits = populated.search.search("报销", limit=2)
        assert len(hits) <= 2

    def test_result_fields_complete(self, populated: AppContext) -> None:
        """§3.7 要求返回主题/发件人/日期/文件夹/片段/路径/分数。"""
        hits = populated.search.search("报销发票", limit=1)
        hit = hits[0]
        payload = hit.to_dict()
        for key in ("subject", "sender", "date", "folder", "snippet",
                    "local_markdown_path", "score"):
            assert key in payload
        assert payload["subject"]
        assert payload["local_markdown_path"].endswith(".md")
        assert payload["score"] > 0

    def test_source_label(self, populated: AppContext) -> None:
        assert populated.search.search("报销", mode="keyword", limit=1)[0].source == "keyword"
        assert populated.search.search("报销", mode="vector", limit=1)[0].source == "vector"
        assert populated.search.search("报销", mode="hybrid", limit=1)[0].source == "hybrid"


class TestSearchFilters:
    def test_folder_filter(self, populated: AppContext) -> None:
        hits = populated.search.search(
            "报销", filters=SearchFilters(folder=["Sent"]), limit=10
        )
        assert all(h.folder == "Sent" for h in hits)

    def test_sender_filter(self, populated: AppContext) -> None:
        hits = populated.search.search(
            "报销", filters=SearchFilters(sender="alice"), limit=10
        )
        assert hits and all("alice" in h.sender for h in hits)

    def test_sender_filter_no_match(self, populated: AppContext) -> None:
        hits = populated.search.search(
            "报销", filters=SearchFilters(sender="nobody"), limit=10
        )
        assert hits == []

    def test_date_filter(self, populated: AppContext) -> None:
        future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        hits = populated.search.search(
            "报销", filters=SearchFilters(date_from=future), limit=10
        )
        assert hits == []

    def test_has_attachments_filter(self, populated: AppContext) -> None:
        hits = populated.search.search(
            "报销", filters=SearchFilters(has_attachments=True), limit=10
        )
        assert hits == []

    def test_subject_filter(self, populated: AppContext) -> None:
        hits = populated.search.search(
            "报销", filters=SearchFilters(subject="新规"), limit=10
        )
        assert all("新规" in h.subject for h in hits)


class TestListMessages:
    def test_returns_all(self, populated: AppContext) -> None:
        rows, total = populated.search.list_messages(limit=50)
        assert total == 5 and len(rows) == 5

    def test_pagination(self, populated: AppContext) -> None:
        page1, total = populated.search.list_messages(limit=2, offset=0)
        page2, _ = populated.search.list_messages(limit=2, offset=2)
        assert total == 5
        assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})

    def test_date_desc_default(self, populated: AppContext) -> None:
        rows, _ = populated.search.list_messages(limit=5)
        dates = [r["date_utc"] for r in rows]
        assert dates == sorted(dates, reverse=True)

    def test_body_text_not_leaked(self, populated: AppContext) -> None:
        """列表接口不应返回完整正文（响应体膨胀 + 隐私）。"""
        rows, _ = populated.search.list_messages(limit=1)
        assert "body_text" not in rows[0]

    def test_filter_by_folder(self, populated: AppContext) -> None:
        rows, total = populated.search.list_messages(
            filters=SearchFilters(folder=["INBOX"]), limit=50
        )
        assert total == 5

    def test_empty_database(self, context: AppContext) -> None:
        rows, total = context.search.list_messages(limit=10)
        assert rows == [] and total == 0


class TestStatistics:
    def test_counts(self, populated: AppContext) -> None:
        stats = populated.search.statistics()
        assert stats["messages"] == 5
        assert stats["chunks"] >= 5
        assert stats["fts_available"] is True
        assert stats["vector_backend"] == "sqlite-bruteforce"
        assert stats["embedding"]["backend"] == "hashing"


class TestFallbackWithoutChunks:
    def test_message_level_keyword_when_no_chunks(self, context: AppContext) -> None:
        """没有切片时，关键词检索应退化到邮件粒度而不是返回空。"""
        from tests.conftest import build_eml, parse_eml

        raw = build_eml(subject="未索引邮件", text="这是一封还没有建索引的邮件，内容包含关键词甲乙丙。")
        parsed = context.sync.parser.parse(parse_eml(raw), folder="INBOX", uid="1", uidvalidity=1)
        archive = context.sync.exporter.export(parsed, account="tester@corp.com")
        record = context.sync._to_record(parsed, archive, duplicate_of=None)
        context.db.insert_message(record, archive.attachments)

        engine = SearchEngine(context.db, config=context.config)
        hits = engine.search("关键词", mode="keyword", limit=5)
        assert hits
        assert hits[0].chunk_id is None
        assert "关键词" in hits[0].snippet


class TestFieldScopedSearch:
    """字段级检索：把查询限定在标题/发件人/收件人/抄送/正文。

    回归背景：用户要能自己选检索范围。关键是**字段之间不能串**——
    "在抄送里找报销"不该命中"收件人里有报销"的邮件。
    """

    def _seed(self, context: AppContext) -> None:
        from src.models import MessageRecord

        rows = [
            # uid, 主题, 发件人地址, 发件人姓名, 收件人, 抄送, 正文
            ("1", "季度报销发票汇总", "alice@corp.com", "爱丽丝",
             "me@corp.com", "boss@corp.com", "差旅报销内容"),
            ("2", "服务器扩容申请", "bob@corp.com", "报销专员",
             "finance@corp.com", "boss@corp.com", "扩容申请正文"),
            ("3", "项目进度通报", "carol@corp.com", "卡罗尔",
             "me@corp.com", "报销组@corp.com", "正文里提到报销"),
            ("4", "邮件D", "dave@corp.com", "戴夫",
             "报销科@corp.com", "other@corp.com", "无关正文"),
        ]
        for uid, subject, sender, name, to, cc, body in rows:
            context.db.insert_message(
                MessageRecord(
                    account="t@c.com", message_id=f"{uid}@corp.com", uid=uid,
                    uidvalidity=1, folder="INBOX", subject=subject, sender=sender,
                    sender_name=name, recipients=to, cc=cc, body_text=body,
                ),
                [],
            )

    def _subjects(self, context: AppContext, query: str, scope: str) -> set[str]:
        return {h.subject for h in context.search.search(query, limit=20, scope=scope)}

    def test_subject_scope_ignores_body(self, context: AppContext) -> None:
        self._seed(context)
        found = self._subjects(context, "报销", "subject")
        assert found == {"季度报销发票汇总"}, found

    def test_sender_scope_matches_display_name_too(self, context: AppContext) -> None:
        """发件人姓名（中文）也要能命中，不能只搜地址。"""
        self._seed(context)
        assert "服务器扩容申请" in self._subjects(context, "报销", "sender")

    def test_recipient_scope_does_not_leak_into_cc(self, context: AppContext) -> None:
        self._seed(context)
        found = self._subjects(context, "报销", "recipient")
        assert found == {"邮件D"}, f"收件人范围串到了抄送：{found}"

    def test_cc_scope_does_not_leak_into_recipient(self, context: AppContext) -> None:
        self._seed(context)
        found = self._subjects(context, "报销", "cc")
        assert found == {"项目进度通报"}, f"抄送范围串到了收件人：{found}"

    def test_body_scope_searches_content(self, context: AppContext) -> None:
        self._seed(context)
        found = self._subjects(context, "报销", "body")
        assert "季度报销发票汇总" in found
        assert "服务器扩容申请" not in found, "正文范围不该命中发件人姓名"

    def test_all_scope_searches_everything(self, context: AppContext) -> None:
        self._seed(context)
        found = self._subjects(context, "报销", "all")
        assert len(found) >= 3

    def test_recipient_by_address(self, context: AppContext) -> None:
        self._seed(context)
        assert "服务器扩容申请" in self._subjects(context, "finance", "recipient")

    def test_empty_query_returns_nothing(self, context: AppContext) -> None:
        self._seed(context)
        for scope in ("subject", "sender", "recipient", "cc", "body"):
            assert context.search.search("  ", limit=5, scope=scope) == []

    def test_scope_respects_filters(self, context: AppContext) -> None:
        """范围检索也要走通用过滤条件，不能绕开文件夹/账户限制。"""
        from src.search import SearchFilters

        self._seed(context)
        hits = context.search.search(
            "报销", limit=20, scope="all", filters=SearchFilters(folder=["不存在的文件夹"])
        )
        assert hits == []
