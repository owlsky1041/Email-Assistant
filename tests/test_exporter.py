"""Markdown 归档测试（§3.2）：frontmatter、附件原子写入、去重、内联图回填。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.config import AppConfig
from src.markdown_exporter import MarkdownExporter
from src.models import AttachmentMeta, ParsedMessage


def make_message(**overrides) -> ParsedMessage:
    base = dict(
        uid="42",
        folder="INBOX",
        uidvalidity=1,
        message_id="m42@corp.com",
        subject="季度报销汇总",
        sender="alice@corp.com",
        sender_name="爱丽丝",
        recipients="tester@corp.com",
        cc="bob@corp.com",
        date=datetime(2024, 3, 1, 10, 30, 45, tzinfo=timezone.utc),
        date_raw="Fri, 1 Mar 2024 10:30:45 +0000",
        text_plain="正文内容",
        body_text="正文内容",
        body_markdown="正文内容",
        size_bytes=2048,
    )
    base.update(overrides)
    return ParsedMessage(**base)


@pytest.fixture
def exporter(tmp_config: AppConfig) -> MarkdownExporter:
    return MarkdownExporter(tmp_config)


class TestPathBuilding:
    def test_folder_structure_preserved(self, exporter: MarkdownExporter) -> None:
        """§3.2 保持邮箱原始文件夹结构。"""
        path = exporter.folder_dir("客户/2024/发票", "me@corp.com")
        assert path.parts[-3:] == ("客户", "2024", "发票")

    def test_account_subdir(self, exporter: MarkdownExporter) -> None:
        path = exporter.folder_dir("INBOX", "me@corp.com")
        assert "me@corp.com" in path.parts

    def test_filename_format(self, exporter: MarkdownExporter) -> None:
        """§3.2 文件名格式 YYYYMMDD_HHmmss_主题_邮件ID.md"""
        name = exporter.build_filename(make_message())
        assert name == "20240301_103045_季度报销汇总_42.md"

    def test_illegal_subject_sanitized(self, exporter: MarkdownExporter) -> None:
        name = exporter.build_filename(make_message(subject='报告:2024/Q1<>?'))
        assert not any(ch in name for ch in ':*?<>/')

    def test_missing_date_falls_back(self, exporter: MarkdownExporter) -> None:
        name = exporter.build_filename(make_message(date=None))
        assert name.startswith("00000000_000000_")

    def test_traversal_folder_blocked(self, exporter: MarkdownExporter) -> None:
        path = exporter.folder_dir("../../etc", "me@corp.com")
        assert ".." not in path.parts


class TestFrontmatter:
    def test_required_fields_present(self, exporter: MarkdownExporter, tmp_config: AppConfig) -> None:
        """§3.2 规定的 frontmatter 字段必须齐全。"""
        result = exporter.export(make_message(), account="me@corp.com")
        meta, _ = MarkdownExporter.read_markdown(result.markdown_path)
        for field in ("message_id", "subject", "from", "to", "cc", "date", "folder",
                      "uid", "attachments", "local_path"):
            assert field in meta, f"缺少 frontmatter 字段：{field}"

        assert meta["message_id"] == "m42@corp.com"
        assert meta["subject"] == "季度报销汇总"
        assert meta["from"] == "爱丽丝 <alice@corp.com>"
        assert meta["to"] == "tester@corp.com"
        assert meta["cc"] == "bob@corp.com"
        assert meta["folder"] == "INBOX"
        assert meta["uid"] == "42"
        assert meta["attachments"] == []
        assert meta["local_path"].endswith(".md")

    def test_yaml_is_valid(self, exporter: MarkdownExporter) -> None:
        result = exporter.export(make_message(), account="me@corp.com")
        text = result.markdown_path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        parts = text.split("---", 2)
        parsed = yaml.safe_load(parts[1])
        assert isinstance(parsed, dict)

    def test_body_present_after_frontmatter(self, exporter: MarkdownExporter) -> None:
        result = exporter.export(
            make_message(body_markdown="这是**正文**内容。"), account="me@corp.com"
        )
        _, body = MarkdownExporter.read_markdown(result.markdown_path)
        assert "这是**正文**内容。" in body

    def test_unicode_not_escaped(self, exporter: MarkdownExporter) -> None:
        result = exporter.export(make_message(), account="me@corp.com")
        text = result.markdown_path.read_text(encoding="utf-8")
        assert "季度报销汇总" in text
        assert "\\u5b63" not in text


class TestAttachmentHandling:
    def test_attachment_saved_in_sibling_dir(self, exporter: MarkdownExporter) -> None:
        """§3.2 附件保存到同级 attachments/ 子目录。"""
        message = make_message(
            attachments=[AttachmentMeta(filename="报表.xlsx", size_bytes=8, part_index=1)],
            attachment_payloads={1: b"xlsxdata"},
        )
        result = exporter.export(message, account="me@corp.com")
        meta = result.attachments[0]
        assert meta.downloaded
        assert Path(meta.local_path).read_bytes() == b"xlsxdata"
        assert Path(meta.local_path).parent.name == "attachments"
        assert Path(meta.local_path).parent.parent == result.markdown_path.parent

    def test_frontmatter_lists_attachments(self, exporter: MarkdownExporter) -> None:
        message = make_message(
            attachments=[AttachmentMeta(filename="a.pdf", size_bytes=3, part_index=1)],
            attachment_payloads={1: b"pdf"},
        )
        result = exporter.export(message, account="me@corp.com")
        meta, _ = MarkdownExporter.read_markdown(result.markdown_path)
        assert meta["attachments"] == ["a.pdf"]

    def test_sha256_recorded(self, exporter: MarkdownExporter) -> None:
        message = make_message(
            attachments=[AttachmentMeta(filename="a.bin", size_bytes=5, part_index=1)],
            attachment_payloads={1: b"hello"},
        )
        result = exporter.export(message, account="me@corp.com")
        from src.utils import sha256_bytes

        assert result.attachments[0].sha256 == sha256_bytes(b"hello")

    def test_skipped_attachment_not_written(self, exporter: MarkdownExporter) -> None:
        message = make_message(
            attachments=[
                AttachmentMeta(filename="big.zip", size_bytes=999999, part_index=1,
                               downloaded=False, skip_reason="超过大小阈值")
            ]
        )
        result = exporter.export(message, account="me@corp.com")
        assert result.attachments[0].downloaded is False
        assert not (result.markdown_path.parent / "attachments").exists()

    def test_filename_collision_gets_suffix(self, exporter: MarkdownExporter) -> None:
        message = make_message(
            uid="1",
            attachments=[AttachmentMeta(filename="same.txt", size_bytes=1, part_index=1)],
            attachment_payloads={1: b"a"},
        )
        first = exporter.export(message, account="me@corp.com")
        second = exporter.export(make_message(uid="2"), account="me@corp.com")
        again = exporter.export(
            make_message(
                uid="3",
                attachments=[AttachmentMeta(filename="same.txt", size_bytes=1, part_index=1)],
                attachment_payloads={1: b"b"},
            ),
            account="me@corp.com",
        )
        assert first.attachments[0].local_path != again.attachments[0].local_path
        assert Path(again.attachments[0].local_path).name == "same_1.txt"
        assert second is not None

    def test_no_tmp_files_left(self, exporter: MarkdownExporter) -> None:
        message = make_message(
            attachments=[AttachmentMeta(filename="a.bin", size_bytes=4, part_index=1)],
            attachment_payloads={1: b"data"},
        )
        result = exporter.export(message, account="me@corp.com")
        assert list(result.markdown_path.parent.rglob("*.tmp")) == []

    def test_illegal_attachment_name_sanitized(self, exporter: MarkdownExporter) -> None:
        message = make_message(
            attachments=[AttachmentMeta(filename='bad:name?.txt', size_bytes=1, part_index=1)],
            attachment_payloads={1: b"x"},
        )
        result = exporter.export(message, account="me@corp.com")
        assert "?" not in Path(result.attachments[0].local_path).name


class TestInlineImageRewrite:
    def test_cid_replaced_with_real_relative_path(self, exporter: MarkdownExporter) -> None:
        """§11.3 内联图片落盘后，Markdown 必须引用真实相对路径。"""
        html = '<p>见图</p><img src="cid:logo@corp" width="200" height="60">'
        message = make_message(
            text_html=html,
            text_plain="",
            body_markdown="",
            body_text="",
            attachments=[
                AttachmentMeta(filename="logo.png", size_bytes=4, content_id="logo@corp",
                               is_inline=True, part_index=1)
            ],
            attachment_payloads={1: b"png!"},
            cid_map={"logo@corp": "attachments/logo.png"},
        )
        result = exporter.export(message, account="me@corp.com")
        _, body = MarkdownExporter.read_markdown(result.markdown_path)
        assert "attachments/logo.png" in body
        assert "cid:" not in body

    def test_cid_uses_final_name_after_collision(self, exporter: MarkdownExporter) -> None:
        """改名后 Markdown 里的引用也必须跟着改。"""
        html = '<img src="cid:x@y">'
        message = make_message(
            uid="1", text_html=html, text_plain="", body_markdown="", body_text="",
            attachments=[AttachmentMeta(filename="pic.png", size_bytes=1, content_id="x@y",
                                        is_inline=True, part_index=1)],
            attachment_payloads={1: b"1"},
            cid_map={"x@y": "attachments/pic.png"},
        )
        exporter.export(message, account="me@corp.com")
        # 第二封同名图片会被改名
        message2 = make_message(
            uid="2", text_html=html, text_plain="", body_markdown="", body_text="",
            attachments=[AttachmentMeta(filename="pic.png", size_bytes=1, content_id="x@y",
                                        is_inline=True, part_index=1)],
            attachment_payloads={1: b"2"},
            cid_map={"x@y": "attachments/pic.png"},
        )
        result2 = exporter.export(message2, account="me@corp.com")
        _, body2 = MarkdownExporter.read_markdown(result2.markdown_path)
        assert "attachments/pic_1.png" in body2


class TestDedupBehaviour:
    def test_second_export_gets_new_file(self, exporter: MarkdownExporter) -> None:
        """同一封邮件重复导出不会覆盖既有归档。"""
        first = exporter.export(make_message(), account="me@corp.com")
        second = exporter.export(make_message(), account="me@corp.com")
        assert first.markdown_path != second.markdown_path
        assert first.markdown_path.exists() and second.markdown_path.exists()

    def test_overwrite_flag(self, exporter: MarkdownExporter) -> None:
        first = exporter.export(make_message(), account="me@corp.com")
        second = exporter.export(make_message(), account="me@corp.com", overwrite=True)
        assert first.markdown_path == second.markdown_path


class TestReadMarkdown:
    def test_roundtrip(self, exporter: MarkdownExporter) -> None:
        result = exporter.export(
            make_message(body_markdown="内容在此。"), account="me@corp.com"
        )
        meta, body = MarkdownExporter.read_markdown(result.markdown_path)
        assert isinstance(meta, dict)
        assert "内容在此。" in body

    def test_file_without_frontmatter(self, tmp_path: Path) -> None:
        p = tmp_path / "plain.md"
        p.write_text("纯正文", encoding="utf-8")
        meta, body = MarkdownExporter.read_markdown(p)
        assert meta == {} and body == "纯正文"


class TestGlobalAttachmentLayout:
    def test_global_layout_uses_attachment_dir(self, tmp_config: AppConfig) -> None:
        tmp_config.storage.attachment_layout = "global"
        exporter = MarkdownExporter(tmp_config)
        message = make_message(
            attachments=[AttachmentMeta(filename="a.bin", size_bytes=2, part_index=1)],
            attachment_payloads={1: b"ab"},
        )
        result = exporter.export(message, account="me@corp.com")
        path = Path(result.attachments[0].local_path)
        assert tmp_config.attachment_path in path.parents
