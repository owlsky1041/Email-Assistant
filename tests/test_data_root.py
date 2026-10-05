"""数据目录定位与切换的测试（升级不丢数据的关键）。

回归背景
--------
用户已经同步了上万封邮件。最危险的操作是"换了数据根却不知道" ——
程序会在新位置建一个空库，界面看起来正常，但邮件全"消失"了。
因此这里覆盖的是：能不能**看清楚**数据在哪，以及能不能**安全接上**已有数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import AppConfig, load_config
from src.context import AppContext
from src.data_root import (
    adopt_data_root,
    candidate_roots,
    describe_paths,
    looks_like_archive,
)


def _make_archive(root: Path, *, messages: int = 0) -> None:
    """在 root 下造一个"看起来同步过"的数据目录。"""
    (root / "sqlite").mkdir(parents=True, exist_ok=True)
    (root / "mail_archive").mkdir(parents=True, exist_ok=True)
    import sqlite3

    from src.database import Database

    from src.models import MessageRecord

    db = Database(root / "sqlite" / "mail.db")
    db.initialize()
    for i in range(1, messages + 1):
        db.insert_message(
            MessageRecord(
                account="t@c.com", message_id=f"{i}@corp.com", uid=str(i),
                uidvalidity=1, folder="INBOX", subject=f"第 {i} 封",
                body_text="正文",
            ),
            [],
        )


class TestLooksLikeArchive:
    def test_detects_database(self, tmp_path: Path) -> None:
        _make_archive(tmp_path)
        assert looks_like_archive(tmp_path)

    def test_detects_archive_dir_only(self, tmp_path: Path) -> None:
        (tmp_path / "mail_archive").mkdir()
        assert looks_like_archive(tmp_path)

    def test_rejects_empty_dir(self, tmp_path: Path) -> None:
        (tmp_path / "随手建的").mkdir()
        assert not looks_like_archive(tmp_path)

    def test_rejects_nonexistent(self, tmp_path: Path) -> None:
        assert not looks_like_archive(tmp_path / "没有这个目录")


class TestDescribePaths:
    def test_lists_every_destination(self, context: AppContext) -> None:
        report = describe_paths(context.config)
        labels = [label for label, _, _ in report.entries]
        assert "邮件归档" in labels
        assert "数据库" in labels
        assert "内容仓库" in labels

    def test_reports_message_count(self, context: AppContext) -> None:
        from tests.test_indexer import add_message

        add_message(context, "1", "主题", "正文")
        report = describe_paths(context.config)
        assert report.has_database
        assert report.message_count == 1

    def test_renders_human_readable(self, context: AppContext) -> None:
        text = describe_paths(context.config).render()
        assert "数据根目录" in text
        assert "数据库中的邮件" in text


class TestAdoptDataRoot:
    def test_adopts_existing_archive(self, context: AppContext, tmp_path: Path) -> None:
        """核心场景：新版本装好后数据在别处，一条命令接上。"""
        old_root = tmp_path / "老数据"
        _make_archive(old_root, messages=3)

        result = adopt_data_root(context.config, old_root)
        assert result.ok, result.error
        assert result.message_count == 3, "接上后应能看到原有邮件"

        reloaded = load_config(context.config.source_path)
        assert reloaded.sqlite_file == (old_root / "sqlite" / "mail.db").resolve()
        assert reloaded.archive_path == (old_root / "mail_archive").resolve()

    def test_does_not_move_or_delete_anything(
        self, context: AppContext, tmp_path: Path
    ) -> None:
        """只改配置，数据原地不动 —— 选错了改回来即可。"""
        old_root = tmp_path / "老数据"
        _make_archive(old_root, messages=2)
        marker = old_root / "mail_archive" / "留个记号.txt"
        marker.write_text("别动我", encoding="utf-8")

        adopt_data_root(context.config, old_root)

        assert marker.read_text(encoding="utf-8") == "别动我"
        assert (old_root / "sqlite" / "mail.db").is_file()

    def test_rejects_missing_directory(self, context: AppContext, tmp_path: Path) -> None:
        result = adopt_data_root(context.config, tmp_path / "不存在")
        assert not result.ok
        assert "目录不存在" in result.error

    def test_rejects_directory_that_is_not_archive(
        self, context: AppContext, tmp_path: Path
    ) -> None:
        """避免用户指到"文档"这种目录，然后以为数据丢了。"""
        junk = tmp_path / "我的文档"
        (junk / "随便").mkdir(parents=True)
        result = adopt_data_root(context.config, junk)
        assert not result.ok
        assert "看起来不是数据目录" in result.error

    def test_adopt_specific_archive_without_db(
        self, context: AppContext, tmp_path: Path
    ) -> None:
        """只有 mail_archive 没有库也算（归档是用户资产，库可以重建）。"""
        root = tmp_path / "只有归档"
        (root / "mail_archive").mkdir(parents=True)
        result = adopt_data_root(context.config, root)
        assert result.ok
        assert result.message_count == 0


class TestCandidateRoots:
    def test_does_not_return_active_root(self, context: AppContext) -> None:
        active = context.config.archive_path.parent.resolve()
        assert active not in [p.resolve() for p in candidate_roots(context.config)]

    def test_ignores_dirs_without_data(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """空目录不该被当成"候选数据目录"，否则会误导用户。"""
        import src.data_root as module

        monkeypatch.setattr(module, "is_frozen", lambda: False)
        monkeypatch.setattr(module.sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        (tmp_path / "EmailAssistant").mkdir(parents=True, exist_ok=True)
        assert candidate_roots(context.config) == []

    def test_finds_user_data_dir(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import src.data_root as module

        monkeypatch.setattr(module, "is_frozen", lambda: False)
        monkeypatch.setattr(module.sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        _make_archive(tmp_path / "EmailAssistant")
        found = candidate_roots(context.config)
        assert (tmp_path / "EmailAssistant").resolve() in [p.resolve() for p in found]
