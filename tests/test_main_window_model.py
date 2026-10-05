"""主窗口逻辑层测试。

窗口本身需要显示器，但状态面板格式化、检索、邮件详情组装全在
``main_model`` 里，可以在无头环境完整覆盖 —— 这里测的就是这些。

回归背景
--------
主窗口是新加的入口：状态面板要能显示"总任务数 / 当前进度 / 正在同步的邮件"，
检索区要能输入关键字、出结果、点开看正文和附件。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.context import AppContext
from src.gui.main_model import (
    MAX_BODY_CHARS,
    SORT_KEYS,
    next_sort_state,
    sort_rows,
    AttachmentRow,
    DashboardView,
    SearchRow,
    corpus_stats,
    format_duration,
    format_size,
    load_detail,
    run_search,
)
from src.models import ParsedMessage
from tests.test_indexer import add_message


class TestFormatting:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (None, "—"),
            (-1, "—"),
            (0, "00:00"),
            (42, "00:42"),
            (60, "01:00"),
            (3725, "1:02:05"),
        ],
    )
    def test_duration(self, seconds: float | None, expected: str) -> None:
        assert format_duration(seconds) == expected

    @pytest.mark.parametrize(
        "size,expected",
        [(0, "0 B"), (None, "0 B"), (512, "512 B"), (1536, "1.5 KB"), (21249362, "20.3 MB")],
    )
    def test_size(self, size: int | None, expected: str) -> None:
        assert format_size(size) == expected


class TestDashboard:
    def _snapshot(self, **over) -> dict:
        base = {
            "running": True, "phase": "fetching", "workers": 4,
            "current_folder": "INBOX", "folders_total": 5, "folders_done": 1,
            "folder_messages_total": 50, "folder_messages_done": 31,
            "percent": 62.0, "rate_per_second": 12.3, "eta_seconds": 42,
            "elapsed_seconds": 125, "archived": 30, "skipped": 1, "failed": 0,
            "deleted": 0, "events": [],
        }
        base.update(over)
        return base

    def test_shows_corpus_totals(self) -> None:
        view = DashboardView.from_snapshot(
            self._snapshot(), messages=4807, chunks=12000, vectors=12000, pending_index=7
        )
        assert view.messages == 4807
        assert view.chunks == 12000
        assert view.pending_index == 7

    def test_shows_progress_and_folder(self) -> None:
        view = DashboardView.from_snapshot(self._snapshot())
        assert view.phase_label == "正在同步邮件"
        assert view.processed == 31 and view.total == 50
        assert view.percent == 62.0
        assert view.folders_progress == "INBOX（2/5）"

    def test_current_message_comes_from_latest_item_event(self) -> None:
        view = DashboardView.from_snapshot(
            self._snapshot(events=[
                {"level": "info", "message": "开始同步文件夹 INBOX"},
                {"level": "item", "message": "已归档 [30/50] 旧邮件"},
                {"level": "item", "message": "已归档 [31/50] 关于空分装置技术附件"},
            ])
        )
        assert view.current_message == "已归档 [31/50] 关于空分装置技术附件"

    def test_current_message_prefers_error(self) -> None:
        view = DashboardView.from_snapshot(
            self._snapshot(events=[
                {"level": "item", "message": "已归档 [30/50] 正常"},
                {"level": "error", "message": "失败：连接中断"},
            ])
        )
        assert view.current_message == "失败：连接中断"

    def test_empty_events_does_not_crash(self) -> None:
        view = DashboardView.from_snapshot(self._snapshot(events=[]))
        assert view.current_message == ""

    def test_idle_phase_label(self) -> None:
        view = DashboardView.from_snapshot(self._snapshot(running=False, phase="idle"))
        assert view.phase_label == "空闲"

    def test_rate_and_eta_placeholder_when_zero(self) -> None:
        view = DashboardView.from_snapshot(self._snapshot(rate_per_second=0, eta_seconds=None))
        assert view.rate_text == "—"
        assert view.eta_text == "—"

    def test_counters_line(self) -> None:
        view = DashboardView.from_snapshot(self._snapshot())
        assert "归档 30" in view.counters_text and "失败 0" in view.counters_text

    def test_folder_without_total_still_shows_name(self) -> None:
        view = DashboardView.from_snapshot(self._snapshot(folders_total=0))
        assert view.folders_progress == "INBOX"


class TestSearchRows:
    def test_row_tree_values(self) -> None:
        row = SearchRow(
            message_id="m1", subject="季度报销", sender="爱丽丝 <a@b.com>",
            date="2024-03-01T09:23:00+00:00", folder="INBOX", score=0.0328,
            snippet="...", source="hybrid",
        )
        values = row.to_tree_values()
        assert values[0] == "季度报销"
        assert values[2] == "2024-03-01 09:23:00"
        assert values[3].startswith("0.0328")

    def test_empty_subject_falls_back(self) -> None:
        row = SearchRow(message_id="m", subject="", sender="", date="", folder="",
                 score=0.0, snippet="", source="hybrid")
        assert row.to_tree_values()[0] == "(无主题)"


class TestRunSearch:
    def test_finds_indexed_mail(self, context: AppContext) -> None:
        add_message(context, "1", "季度报销发票汇总", "差旅报销发票已整理完毕。" * 10)
        add_message(context, "2", "服务器扩容申请", "扩容机器申请内容。" * 10)
        context.indexer.index_pending()

        rows = run_search(context, "报销发票", limit=10)
        assert rows, "应当命中"
        assert any("报销" in r.subject for r in rows)
        assert all(r.message_id for r in rows)

    def test_blank_query_returns_nothing(self, context: AppContext) -> None:
        assert run_search(context, "") == []
        assert run_search(context, "   ") == []

    def test_limit_is_respected(self, context: AppContext) -> None:
        for i in range(1, 8):
            add_message(context, str(i), f"报销单据 {i}", "报销内容。" * 20)
        context.indexer.index_pending()
        assert len(run_search(context, "报销", limit=3)) <= 3


class TestCorpusStats:
    def test_reports_counts(self, context: AppContext) -> None:
        add_message(context, "1", "主题", "正文。" * 30)
        context.indexer.index_pending()
        stats = corpus_stats(context)
        assert stats["messages"] == 1
        assert stats["chunks"] >= 1
        assert stats["vectors"] >= 1
        assert stats["pending_index"] == 0

    def test_pending_count_after_new_mail(self, context: AppContext) -> None:
        add_message(context, "1", "主题", "正文。" * 30)
        assert corpus_stats(context)["pending_index"] == 1


class TestLoadDetail:
    def _message(self, uid: str, *, attachments: bool = False) -> ParsedMessage:
        message = ParsedMessage(
            uid=uid,
            folder="INBOX",
            uidvalidity=1,
            message_id=f"{uid}@corp.com",
            subject=f"主题 {uid}",
            sender="alice@corp.com",
            sender_name="爱丽丝",
            recipients="tester@corp.com",
            cc="boss@corp.com",
            date=datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc),
            body_markdown="正文内容",
            body_text="正文内容\n第二段",
        )
        if attachments:
            from src.models import AttachmentMeta

            payload = b"hello attachment"
            message.attachments = [
                AttachmentMeta(filename="说明.txt", part_index=0, size_bytes=len(payload))
            ]
            message.attachment_payloads = {0: payload}
        return message

    def _insert(self, context: AppContext, uid: str, **kw) -> None:
        message = self._message(uid, **kw)
        archive = context.sync.exporter.export(message, account="tester@corp.com")
        record = context.sync._to_record(message, archive, duplicate_of=None)
        context.db.insert_message(record, archive.attachments)

    def test_loads_body_and_headers(self, context: AppContext) -> None:
        self._insert(context, "1")
        detail = load_detail(context, "1@corp.com")
        assert detail.found
        assert detail.subject == "主题 1"
        assert "爱丽丝" in detail.sender and "alice@corp.com" in detail.sender
        assert detail.recipients == "tester@corp.com"
        assert detail.cc == "boss@corp.com"
        assert "正文内容" in detail.body
        assert detail.folder == "INBOX"

    def test_accepts_angle_bracketed_id(self, context: AppContext) -> None:
        self._insert(context, "2")
        assert load_detail(context, "<2@corp.com>").found

    def test_lists_attachments_with_existence(self, context: AppContext) -> None:
        self._insert(context, "3", attachments=True)
        detail = load_detail(context, "3@corp.com")
        assert len(detail.attachments) == 1
        att = detail.attachments[0]
        assert att.filename == "说明.txt"
        assert att.exists is True
        assert Path(att.local_path).read_bytes() == b"hello attachment"
        assert att.to_tree_values()[2] == "✓"

    def test_missing_attachment_is_marked(self, context: AppContext) -> None:
        self._insert(context, "4", attachments=True)
        detail = load_detail(context, "4@corp.com")
        Path(detail.attachments[0].local_path).unlink()
        again = load_detail(context, "4@corp.com")
        assert again.attachments[0].exists is False
        assert again.attachments[0].to_tree_values()[2] == "缺失"

    def test_unknown_message_reports_error(self, context: AppContext) -> None:
        detail = load_detail(context, "nope@corp.com")
        assert detail.found is False
        assert "没有这封邮件" in detail.error

    def test_empty_id_reports_error(self, context: AppContext) -> None:
        assert load_detail(context, "").found is False

    def test_body_is_capped(self, context: AppContext) -> None:
        """正文过长必须截断，否则 Text 控件会被拖死。"""
        message = self._message("9")
        message.body_text = "字" * (MAX_BODY_CHARS + 5000)
        archive = context.sync.exporter.export(message, account="tester@corp.com")
        record = context.sync._to_record(message, archive, duplicate_of=None)
        context.db.insert_message(record, archive.attachments)

        detail = load_detail(context, "9@corp.com")
        assert len(detail.body) == MAX_BODY_CHARS
        assert detail.body_truncated is True

    def test_falls_back_to_markdown_when_db_body_empty(self, context: AppContext) -> None:
        """数据库正文为空时回落到磁盘上的 .md。"""
        self._insert(context, "5")
        context.db.execute(
            "UPDATE messages SET body_text = '' WHERE message_id = ?", ("5@corp.com",)
        )
        detail = load_detail(context, "5@corp.com")
        assert "正文内容" in detail.body

    def test_header_lines_skip_empty_values(self, context: AppContext) -> None:
        self._insert(context, "6")
        detail = load_detail(context, "6@corp.com")
        keys = [k for k, _ in detail.header_lines()]
        assert "主题" in keys
        assert all(v for _, v in detail.header_lines())


class TestAttachmentRow:
    def test_tree_values(self) -> None:
        row = AttachmentRow(
            attachment_id=1, filename="a.pdf", size_bytes=2048,
            content_type="application/pdf", local_path="/x/a.pdf", exists=True,
        )
        assert row.to_tree_values() == ("a.pdf", "2.0 KB", "✓")


class TestSorting:
    """结果表格点表头排序。

    回归背景：用户要求标题等列可以升序/降序。日期与分数必须按**真实值**
    排 —— 按字符串排会把 2024-09 排到 2024-10 之后。
    """

    def _rows(self) -> list[SearchRow]:
        def make(mid: str, subject: str, sender: str, date: str, score: float) -> SearchRow:
            return SearchRow(
                message_id=mid, subject=subject, sender=sender, date=date,
                folder="INBOX", score=score, snippet="", source="hybrid",
            )

        return [
            make("a", "乙项目", "zoe@x.com", "2024-10-01T09:00:00+00:00", 0.01),
            make("b", "甲项目", "alice@x.com", "2024-09-30T23:00:00+00:00", 0.03),
            make("c", "丙项目", "bob@x.com", "2024-11-15T12:00:00+00:00", 0.02),
        ]

    # 三条数据的对应关系（别凭直觉记）：
    #   a = "乙项目"(乙 U+4E59)   b = "甲项目"(甲 U+7532)   c = "丙项目"(丙 U+4E19)
    # Python 的字符串比较是**码点序**，不是拼音序：
    #   丙(4E19) < 乙(4E59) < 甲(7532)  →  c, a, b

    def test_sort_by_subject_ascending(self) -> None:
        rows = sort_rows(self._rows(), "subject")
        assert [r.message_id for r in rows] == ["c", "a", "b"]

    def test_sort_by_subject_descending(self) -> None:
        rows = sort_rows(self._rows(), "subject", descending=True)
        assert [r.message_id for r in rows] == ["b", "a", "c"]

    def test_sort_by_date_uses_real_value(self) -> None:
        """字符串排序会把 2024-09 排到 2024-10 之后，这里必须是时间序。"""
        asc = [r.message_id for r in sort_rows(self._rows(), "date")]
        assert asc == ["b", "a", "c"], asc

    def test_sort_by_score(self) -> None:
        asc = [r.message_id for r in sort_rows(self._rows(), "score")]
        assert asc == ["a", "c", "b"]
        desc = [r.message_id for r in sort_rows(self._rows(), "score", descending=True)]
        assert desc == ["b", "c", "a"]

    def test_sort_by_sender_is_case_insensitive(self) -> None:
        rows = self._rows() + [
            SearchRow(message_id="d", subject="丁", sender="Zed@x.com", date="2024-01-01",
                      folder="INBOX", score=0.0, snippet="", source="hybrid")
        ]
        order = [r.message_id for r in sort_rows(rows, "sender")]
        # 忽略大小写后：alice(b) < bob(c) < Zed(d) < zoe(a)
        # 不忽略大小写的话大写 Z(0x5A) 会排到小写字母前面，顺序就乱了。
        assert order == ["b", "c", "d", "a"], order

    def test_unknown_column_is_noop(self) -> None:
        rows = self._rows()
        assert [r.message_id for r in sort_rows(rows, "不存在")] == [r.message_id for r in rows]

    def test_sort_does_not_mutate_input(self) -> None:
        rows = self._rows()
        before = [r.message_id for r in rows]
        sort_rows(rows, "subject")
        assert [r.message_id for r in rows] == before

    def test_every_tree_column_has_a_sort_key(self) -> None:
        """表格里显示的列必须都能排序，否则点了没反应。"""
        for column in ("subject", "sender", "date", "score"):
            assert column in SORT_KEYS


class TestSortToggle:
    """点击表头时的升降序切换。

    回归背景：这段判断原本写在窗口方法里，一个笔误（引用了未定义的变量）
    直到真正点到表头才炸出来。抽成纯函数后可以无头覆盖。
    """

    def test_first_click_on_new_column_ascends(self) -> None:
        assert next_sort_state("score", True, "subject") == ("subject", False)

    def test_score_column_defaults_to_descending(self) -> None:
        """分数是"越相关越靠前"，默认就该从高到低。"""
        assert next_sort_state("subject", False, "score") == ("score", True)

    def test_clicking_same_column_toggles(self) -> None:
        assert next_sort_state("subject", False, "subject") == ("subject", True)
        assert next_sort_state("subject", True, "subject") == ("subject", False)

    def test_toggle_back_and_forth_is_stable(self) -> None:
        state = ("date", False)
        for _ in range(3):
            state = next_sort_state(*state, "date")
        assert state == ("date", True)

    def test_switching_columns_resets_direction(self) -> None:
        assert next_sort_state("subject", True, "date") == ("date", False)
