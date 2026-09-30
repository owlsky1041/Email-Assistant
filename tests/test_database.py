"""数据库测试：WAL、FTS5 中文检索、去重、软删除、切片与向量。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.database import Database, build_match_query, segment_cjk
from src.models import AttachmentMeta, Chunk, FolderState, MessageRecord, SyncResult


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    yield database
    database.close()


def make_record(**overrides) -> MessageRecord:
    base = dict(
        account="tester@corp.com",
        message_id="m1@corp.com",
        uid="1",
        uidvalidity=1,
        folder="INBOX",
        subject="季度报销发票",
        sender="alice@corp.com",
        sender_name="爱丽丝",
        recipients="tester@corp.com",
        cc="",
        date_utc=datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc),
        date_raw="Fri, 1 Mar 2024 10:00:00 +0000",
        local_markdown_path="/archive/INBOX/a.md",
        body_text="本季度差旅报销发票已整理完毕，请财务审核。",
        has_attachments=False,
        size_bytes=1024,
        content_hash="hash-1",
    )
    base.update(overrides)
    return MessageRecord(**base)


class TestSegmentation:
    def test_cjk_split_into_chars(self) -> None:
        assert segment_cjk("邮件") == "邮 件"
        assert segment_cjk("报告2024 Q1") == "报 告 2024 Q1"

    def test_latin_untouched(self) -> None:
        assert "hello" in segment_cjk("hello")

    def test_match_query_chinese_phrase(self) -> None:
        assert build_match_query("发票") == '"发 票"'

    def test_match_query_multi_term_and(self) -> None:
        assert build_match_query("发票 报销") == '"发 票" AND "报 销"'

    def test_match_query_english_prefix(self) -> None:
        assert build_match_query("invoi") == '"invoi"*'

    def test_match_query_empty(self) -> None:
        assert build_match_query("") == ""
        assert build_match_query("   ") == ""
        assert build_match_query("，，。") == ""

    def test_match_query_escapes_quotes(self) -> None:
        result = build_match_query('a"b')
        assert result.count('"') % 2 == 0  # 引号必须成对


class TestSchemaAndPragmas:
    def test_schema_version(self, db: Database) -> None:
        assert db.schema_summary()["schema_version"] == 1

    def test_required_tables_exist(self, db: Database) -> None:
        tables = set(db.schema_summary()["tables"])
        for required in ("messages", "attachments", "sync_log", "kb_chunks", "kb_vectors",
                         "folders"):
            assert required in tables

    def test_wal_mode_enabled(self, db: Database) -> None:
        """§11.1 必须开启 WAL 以支持多线程并发读写。"""
        assert db.schema_summary()["journal_mode"].lower() == "wal"

    def test_fts_available(self, db: Database) -> None:
        assert db.fts_available is True

    def test_integrity_ok(self, db: Database) -> None:
        assert db.integrity_check() == "ok"

    def test_migration_idempotent(self, db: Database) -> None:
        db.initialize()
        db.initialize()
        assert db.schema_summary()["schema_version"] == 1


class TestMessages:
    def test_insert_and_get(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        record = db.get_message(pk)
        assert record is not None
        assert record.subject == "季度报销发票"
        assert record.sender_name == "爱丽丝"
        assert record.date_utc.year == 2024

    def test_returns_attachment_metadata(self, db: Database) -> None:
        att = AttachmentMeta(filename="a.pdf", size_bytes=10, part_index=1, downloaded=True)
        pk = db.insert_message(make_record(), [att])
        rows = db.list_attachments(pk)
        assert len(rows) == 1
        assert rows[0]["filename"] == "a.pdf"

    def test_duplicate_uid_updates_not_duplicates(self, db: Database) -> None:
        pk1 = db.insert_message(make_record(subject="第一版"))
        pk2 = db.insert_message(make_record(subject="第二版"))
        assert pk1 == pk2
        assert db.count_messages() == 1
        assert db.get_message(pk1).subject == "第二版"

    def test_different_uid_same_message_id(self, db: Database) -> None:
        db.insert_message(make_record(uid="1"))
        db.insert_message(make_record(uid="2"))
        assert db.count_messages() == 2

    def test_message_exists(self, db: Database) -> None:
        assert not db.message_exists("tester@corp.com", "INBOX", 1, "1")
        db.insert_message(make_record())
        assert db.message_exists("tester@corp.com", "INBOX", 1, "1")
        assert not db.message_exists("tester@corp.com", "INBOX", 1, "99")

    def test_find_by_message_id(self, db: Database) -> None:
        db.insert_message(make_record())
        found = db.find_by_message_id("tester@corp.com", "m1@corp.com")
        assert found is not None and found.subject == "季度报销发票"
        assert db.find_by_message_id("tester@corp.com", "nope") is None

    def test_attachment_replaced_on_reinsert(self, db: Database) -> None:
        pk = db.insert_message(make_record(), [AttachmentMeta(filename="old", part_index=1)])
        db.insert_message(make_record(), [AttachmentMeta(filename="new", part_index=1)])
        rows = db.list_attachments(pk)
        assert len(rows) == 1 and rows[0]["filename"] == "new"

    def test_duplicate_of_recorded(self, db: Database) -> None:
        original = db.insert_message(make_record())
        copy = db.insert_message(make_record(uid="2", folder="All", duplicate_of=original))
        assert db.get_message(copy).duplicate_of == original

    def test_duplicate_is_marked_indexed(self, db: Database) -> None:
        original = db.insert_message(make_record())
        db.insert_message(make_record(uid="2", folder="All", duplicate_of=original))
        pending = db.messages_pending_index()
        assert all(r.uid != "2" for r in pending)


class TestFullTextSearch:
    def test_chinese_keyword_match(self, db: Database) -> None:
        db.insert_message(make_record())
        rows = db.query(
            "SELECT m.id FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH ?",
            (build_match_query("报销发票"),),
        )
        assert len(rows) == 1

    def test_chinese_partial_word_match(self, db: Database) -> None:
        """中文子串（2 字）也要能命中，这是 unicode61 分词做不到的。"""
        db.insert_message(make_record())
        rows = db.query(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (build_match_query("发票"),),
        )
        assert len(rows) == 1

    def test_non_matching_term(self, db: Database) -> None:
        db.insert_message(make_record())
        rows = db.query(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (build_match_query("飞机票"),),
        )
        assert rows == []

    def test_subject_searchable(self, db: Database) -> None:
        db.insert_message(make_record(body_text="无关内容"))
        rows = db.query(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (build_match_query("报销"),),
        )
        assert len(rows) == 1

    def test_sender_searchable(self, db: Database) -> None:
        db.insert_message(make_record(subject="x", body_text="y"))
        rows = db.query(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (build_match_query("爱丽丝"),),
        )
        assert len(rows) == 1

    def test_fts_updated_on_reinsert(self, db: Database) -> None:
        pk = db.insert_message(make_record(body_text="原始内容"))
        db.insert_message(make_record(body_text="更新后的内容"))
        rows = db.query(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (build_match_query("原始内容"),),
        )
        assert rows == []
        rows = db.query(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (build_match_query("更新后的内容"),),
        )
        assert len(rows) == 1

    def test_rebuild_fts(self, db: Database) -> None:
        db.insert_message(make_record())
        count = db.rebuild_fts()
        assert count == 1
        rows = db.query(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (build_match_query("发票"),),
        )
        assert len(rows) == 1


class TestFolders:
    def test_upsert_and_get(self, db: Database) -> None:
        db.upsert_folder(FolderState(account="a@x.com", name="INBOX", uidvalidity=5))
        state = db.get_folder("a@x.com", "INBOX")
        assert state is not None and state.uidvalidity == 5

    def test_update_watermark(self, db: Database) -> None:
        db.upsert_folder(FolderState(account="a@x.com", name="INBOX"))
        db.update_folder_state("a@x.com", "INBOX", last_uid=42)
        assert db.get_folder("a@x.com", "INBOX").last_uid == 42

    def test_reset_uidvalidity_clears_watermark(self, db: Database) -> None:
        db.upsert_folder(FolderState(account="a@x.com", name="INBOX", uidvalidity=1))
        db.update_folder_state("a@x.com", "INBOX", last_uid=100)
        db.reset_folder_uidvalidity("a@x.com", "INBOX", 2)
        state = db.get_folder("a@x.com", "INBOX")
        assert state.uidvalidity == 2 and state.last_uid == 0

    def test_upsert_preserves_watermark(self, db: Database) -> None:
        db.upsert_folder(FolderState(account="a@x.com", name="INBOX"))
        db.update_folder_state("a@x.com", "INBOX", last_uid=77)
        db.upsert_folder(FolderState(account="a@x.com", name="INBOX", uidvalidity=1))
        assert db.get_folder("a@x.com", "INBOX").last_uid == 77


class TestSoftDelete:
    def test_marks_missing_uids(self, db: Database) -> None:
        for uid in ("1", "2", "3"):
            db.insert_message(make_record(uid=uid))
        deleted = db.soft_delete_missing("tester@corp.com", "INBOX", {"1", "3"})
        assert deleted == 1
        assert db.count_messages() == 2
        assert db.count_messages(include_deleted=True) == 3

    def test_no_change_when_all_present(self, db: Database) -> None:
        db.insert_message(make_record(uid="1"))
        assert db.soft_delete_missing("tester@corp.com", "INBOX", {"1"}) == 0

    def test_deleted_excluded_from_default_count(self, db: Database) -> None:
        db.insert_message(make_record(uid="1"))
        db.soft_delete_by_uid("tester@corp.com", "INBOX", "1")
        assert db.count_messages() == 0

    def test_reinsert_revives_deleted(self, db: Database) -> None:
        db.insert_message(make_record(uid="1"))
        db.soft_delete_by_uid("tester@corp.com", "INBOX", "1")
        db.insert_message(make_record(uid="1"))
        assert db.count_messages() == 1


class TestChunksAndVectors:
    def test_replace_chunks(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        chunks = [
            Chunk(chunk_index=0, text="第一段", token_count=3, message_id="m1@corp.com"),
            Chunk(chunk_index=1, text="第二段", token_count=3, message_id="m1@corp.com"),
        ]
        ids = db.replace_chunks(pk, chunks)
        assert len(ids) == 2
        assert db.count_chunks() == 2

    def test_replace_removes_old_chunks(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        db.replace_chunks(pk, [Chunk(chunk_index=0, text="旧", token_count=1)])
        db.replace_chunks(pk, [Chunk(chunk_index=0, text="新", token_count=1)])
        rows = db.get_chunks_by_pk(pk)
        assert len(rows) == 1 and rows[0]["text"] == "新"

    def test_chunks_fts_updated(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        db.replace_chunks(pk, [Chunk(chunk_index=0, text="切片正文内容", token_count=5)])
        rows = db.query(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
            (build_match_query("切片正文"),),
        )
        assert len(rows) == 1
        db.replace_chunks(pk, [Chunk(chunk_index=0, text="完全不同的内容", token_count=5)])
        rows = db.query(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
            (build_match_query("切片正文"),),
        )
        assert rows == []

    def test_get_chunks_by_message_id(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        db.replace_chunks(pk, [Chunk(chunk_index=0, text="x", token_count=1, message_id="m1@corp.com")])
        assert len(db.get_chunks("m1@corp.com")) == 1

    def test_record_and_count_vectors(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        [cid] = db.replace_chunks(pk, [Chunk(chunk_index=0, text="x", token_count=1)])
        db.record_vector(cid, message_id="m1@corp.com", model="hashing", dimension=128,
                         backend="sqlite", vector_ref=str(cid), vector_blob=b"\x00" * 8)
        assert db.count_vectors() == 1
        assert len(db.vectors_for_message("m1@corp.com")) == 1

    def test_vector_upsert_not_duplicate(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        [cid] = db.replace_chunks(pk, [Chunk(chunk_index=0, text="x", token_count=1)])
        for _ in range(3):
            db.record_vector(cid, message_id="m", model="hashing", dimension=128,
                             backend="sqlite", vector_ref=str(cid))
        assert db.count_vectors() == 1

    def test_cascade_delete_chunks_with_message(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        [cid] = db.replace_chunks(pk, [Chunk(chunk_index=0, text="x", token_count=1)])
        db.record_vector(cid, message_id="m", model="hashing", dimension=1,
                         backend="sqlite", vector_ref=str(cid))
        with db.transaction() as conn:
            conn.execute("DELETE FROM messages WHERE id = ?", (pk,))
        assert db.count_chunks() == 0
        assert db.count_vectors() == 0

    def test_delete_chunks_for_message(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        db.replace_chunks(pk, [Chunk(chunk_index=0, text="x", token_count=1)])
        assert db.delete_chunks_for_message(pk) != []
        assert db.count_chunks() == 0


class TestSyncLog:
    def test_lifecycle(self, db: Database) -> None:
        log_id = db.start_sync_log("a@x.com", "INBOX")
        result = SyncResult(folder="INBOX", fetched=10, archived=8, failed=2)
        db.finish_sync_log(log_id, result)
        logs = db.recent_sync_logs()
        assert len(logs) == 1
        assert logs[0]["status"] == "partial"
        assert logs[0]["archived"] == 8

    def test_error_summary_recorded(self, db: Database) -> None:
        log_id = db.start_sync_log("a@x.com", "INBOX")
        db.finish_sync_log(log_id, SyncResult(error_summary="连接超时"))
        assert db.last_sync_summary()["error_summary"] == "连接超时"


class TestFolderStatistics:
    def test_groups_by_folder(self, db: Database) -> None:
        db.insert_message(make_record(uid="1", folder="INBOX"))
        db.insert_message(make_record(uid="2", folder="Sent"))
        stats = db.folder_statistics()
        assert {s["folder"] for s in stats} == {"INBOX", "Sent"}


class TestPendingIndex:
    def test_new_message_is_pending(self, db: Database) -> None:
        db.insert_message(make_record())
        assert len(db.messages_pending_index()) == 1

    def test_indexed_message_not_pending(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        db.mark_indexed(pk, "hash-1")
        assert db.messages_pending_index() == []

    def test_updated_message_pending_again(self, db: Database) -> None:
        pk = db.insert_message(make_record())
        db.mark_indexed(pk, "hash-1")
        db.insert_message(make_record(body_text="内容已变化"))
        assert len(db.messages_pending_index()) == 1


class TestMemoryDatabase:
    def test_in_memory_works(self) -> None:
        database = Database(":memory:")
        database.initialize()
        try:
            database.insert_message(make_record())
            assert database.count_messages() == 1
        finally:
            database.close()


class TestMatchQueryVariants:
    """中文自然语言问句的渐进放宽（回归测试）。"""

    def test_strict_variant_first(self) -> None:
        from src.database import build_match_query_variants

        variants = build_match_query_variants("报销发票")
        assert variants[0] == '"报 销 发 票"'

    def test_progressively_shorter(self) -> None:
        from src.database import build_match_query_variants

        variants = build_match_query_variants("报销发票怎么弄")
        assert variants[0] == '"报 销 发 票 怎 么 弄"'
        assert '"报 销 发 票"' in variants
        assert '"报 销"' in variants

    def test_variant_count_bounded(self) -> None:
        from src.database import build_match_query_variants

        variants = build_match_query_variants("一个非常非常长的自然语言问题描述", max_variants=8)
        assert len(variants) <= 8

    def test_short_query_no_variants(self) -> None:
        from src.database import build_match_query_variants

        assert build_match_query_variants("发票") == ['"发 票"']

    def test_empty_query(self) -> None:
        from src.database import build_match_query_variants

        assert build_match_query_variants("") == []
        assert build_match_query_variants("   ") == []

    def test_english_not_shortened(self) -> None:
        from src.database import build_match_query_variants

        variants = build_match_query_variants("invoice")
        assert variants == ['"invoice"*']


class TestNaturalLanguageKeywordSearch:
    def test_long_question_finds_document(self, db: Database) -> None:
        """自然语言问句必须能命中，而不是因为要求整句连续匹配而零结果。"""
        from src.database import build_match_query_variants

        db.insert_message(make_record(subject="季度报销发票汇总",
                                      body_text="本季度差旅报销发票已整理完毕。"))
        found = False
        for expr in build_match_query_variants("报销发票怎么弄"):
            rows = db.query(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (expr,)
            )
            if rows:
                found = True
                break
        assert found, "渐进放宽后应能命中"

    def test_unrelated_question_still_empty(self, db: Database) -> None:
        from src.database import build_match_query_variants

        db.insert_message(make_record())
        for expr in build_match_query_variants("量子纠缠实验"):
            rows = db.query(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (expr,)
            )
            assert rows == [], f"无关查询不应命中：{expr}"
