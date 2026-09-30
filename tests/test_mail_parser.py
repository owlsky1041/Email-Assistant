"""邮件 MIME 解析测试（§11.2）：中文头部、RFC2231 附件名、内联图、超大附件。"""

from __future__ import annotations

import email
import base64
from datetime import timezone
from email.mime.text import MIMEText

import pytest

from src.mail_parser import (
    MailParser,
    decode_filename,
    decode_mime_words,
    decode_payload_text,
    extract_addresses,
)
from tests.conftest import build_eml, parse_eml


class TestDecodeMimeWords:
    def test_plain_ascii(self) -> None:
        assert decode_mime_words("Hello") == "Hello"

    def test_none_and_empty(self) -> None:
        assert decode_mime_words(None) == ""
        assert decode_mime_words("") == ""

    def test_base64_utf8_chinese(self) -> None:
        # "测试主题" 的 RFC2047 编码
        assert decode_mime_words("=?utf-8?B?5rWL6K+V5Li76aKY?=") == "测试主题"

    def test_quoted_printable_chinese(self) -> None:
        assert "测试" in decode_mime_words("=?utf-8?Q?=E6=B5=8B=E8=AF=95?=")

    def test_gbk_encoded(self) -> None:
        raw = "测试".encode("gbk")
        encoded = f"=?gbk?B?{__import__('base64').b64encode(raw).decode()}?="
        assert decode_mime_words(encoded) == "测试"

    def test_mixed_encoded_and_plain(self) -> None:
        result = decode_mime_words("Re: =?utf-8?B?5rWL6K+V?= 补充")
        assert "测试" in result and "Re:" in result

    def test_malformed_header_does_not_raise(self) -> None:
        assert isinstance(decode_mime_words("=?utf-8?B?不完整"), str)


class TestDecodeFilename:
    def test_rfc2231_filename(self) -> None:
        msg = email.message.Message()
        msg.add_header(
            "Content-Disposition",
            "attachment",
            filename=("utf-8", "", "季度财报.xlsx"),
        )
        assert decode_filename(msg) == "季度财报.xlsx"

    def test_rfc2047_filename(self) -> None:
        raw = "=?utf-8?B?5oql6KGoLnBkZg==?="  # 报表.pdf
        msg = email.message.Message()
        msg["Content-Disposition"] = f"attachment; filename=\"{raw}\""
        assert decode_filename(msg) == "报表.pdf"

    def test_name_parameter_fallback(self) -> None:
        msg = email.message.Message()
        msg["Content-Type"] = 'application/pdf; name="doc.pdf"'
        assert decode_filename(msg) == "doc.pdf"

    def test_missing_filename(self) -> None:
        msg = email.message.Message()
        assert decode_filename(msg) == ""

    def test_strips_newlines(self) -> None:
        msg = email.message.Message()
        msg["Content-Disposition"] = 'attachment; filename="a\r\nb.txt"'
        assert "\n" not in decode_filename(msg)


class TestDecodePayloadText:
    def test_declared_utf8(self) -> None:
        part = MIMEText("中文内容", "plain", "utf-8")
        assert decode_payload_text(part) == "中文内容"

    def test_wrong_charset_declaration_falls_back(self) -> None:
        """中文邮件常见的 charset 声明错误必须能自愈。"""
        payload = "中文内容测试".encode("gb18030")
        part = email.message.Message()
        part["Content-Type"] = "text/plain; charset=utf-8"  # 谎报
        part["Content-Transfer-Encoding"] = "base64"
        part.set_payload(base64.b64encode(payload).decode())
        assert "中文内容测试" in decode_payload_text(part)

    def test_unknown_charset(self) -> None:
        payload = "内容".encode("utf-8")
        part = email.message.Message()
        part["Content-Type"] = "text/plain; charset=x-nonexistent"
        part["Content-Transfer-Encoding"] = "base64"
        part.set_payload(base64.b64encode(payload).decode())
        assert "内容" in decode_payload_text(part)


class TestExtractAddresses:
    def test_single(self) -> None:
        assert extract_addresses("Alice <a@x.com>") == ["Alice <a@x.com>"]

    def test_multiple(self) -> None:
        result = extract_addresses("a@x.com, Bob <b@x.com>")
        assert result == ["a@x.com", "Bob <b@x.com>"]

    def test_bare_address(self) -> None:
        assert extract_addresses("a@x.com") == ["a@x.com"]

    def test_empty(self) -> None:
        assert extract_addresses("") == []
        assert extract_addresses(None) == []

    def test_encoded_display_name(self) -> None:
        result = extract_addresses("=?utf-8?B?5byg5LiJ?= <z@x.com>")
        assert result == ["张三 <z@x.com>"]


class TestParseBasic:
    @pytest.fixture
    def parser(self) -> MailParser:
        return MailParser(max_attachment_bytes=1024 * 1024)

    def test_headers(self, parser: MailParser) -> None:
        raw = build_eml(
            subject="季度总结",
            sender="爱丽丝 <alice@corp.com>",
            to="bob@corp.com",
            message_id="<m1@corp.com>",
        )
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1", uidvalidity=1)
        assert parsed.subject == "季度总结"
        assert parsed.sender == "alice@corp.com"
        assert parsed.sender_name == "爱丽丝"
        assert parsed.message_id == "m1@corp.com"
        assert parsed.recipients == "bob@corp.com"
        assert parsed.uid == "1"
        assert parsed.folder == "INBOX"
        assert parsed.date is not None
        assert parsed.date.tzinfo is not None

    def test_missing_subject_gets_placeholder(self, parser: MailParser) -> None:
        msg = email.message.Message()
        msg["From"] = "a@x.com"
        parsed = parser.parse(msg, folder="INBOX", uid="1")
        assert parsed.subject == "(无主题)"

    def test_message_id_strips_angle_brackets(self, parser: MailParser) -> None:
        raw = build_eml(message_id="<abc@x.com>")
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert parsed.message_id == "abc@x.com"

    def test_dedup_key_prefers_message_id(self, parser: MailParser) -> None:
        raw = build_eml(message_id="<abc@x.com>")
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1", uidvalidity=7)
        assert parsed.dedup_key() == "mid:abc@x.com"

    def test_dedup_key_falls_back_to_uid(self, parser: MailParser) -> None:
        msg = email.message.Message()
        msg["Subject"] = "无 ID"
        parsed = parser.parse(msg, folder="INBOX", uid="9", uidvalidity=3)
        assert parsed.dedup_key() == "uid:INBOX:3:9"

    def test_no_body_gets_placeholder(self, parser: MailParser) -> None:
        msg = email.message.Message()
        msg["Subject"] = "空邮件"
        parsed = parser.parse(msg, folder="INBOX", uid="1")
        assert parsed.body_text


class TestParseAttachments:
    @pytest.fixture
    def parser(self) -> MailParser:
        return MailParser(max_attachment_bytes=1024 * 1024)

    def test_attachment_extracted(self, parser: MailParser) -> None:
        payload = b"file-content-bytes"
        raw = build_eml(
            subject="带附件",
            attachments=[("报表.xlsx", payload, "application/vnd.ms-excel")],
        )
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert len(parsed.attachments) == 1
        meta = parsed.attachments[0]
        assert meta.filename == "报表.xlsx"
        assert meta.size_bytes == len(payload)
        assert not meta.is_inline
        assert parsed.attachment_payloads[meta.part_index] == payload
        assert parsed.has_attachments

    def test_oversize_attachment_metadata_only(self) -> None:
        """§3.2 超大附件保护：只记录元数据，不下载。"""
        parser = MailParser(max_attachment_bytes=10)
        payload = b"x" * 5000
        raw = build_eml(attachments=[("大文件.zip", payload, "application/zip")])
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        meta = parsed.attachments[0]
        assert meta.downloaded is False
        assert "超过大小阈值" in (meta.skip_reason or "")
        assert meta.size_bytes == len(payload)
        assert meta.part_index not in parsed.attachment_payloads

    def test_download_disabled(self) -> None:
        parser = MailParser(max_attachment_bytes=10**9, download_attachments=False)
        raw = build_eml(attachments=[("a.txt", b"data", "text/plain")])
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert parsed.attachments[0].downloaded is False
        assert "配置为不下载" in (parsed.attachments[0].skip_reason or "")

    def test_multiple_attachments_have_unique_part_index(self, parser: MailParser) -> None:
        raw = build_eml(
            attachments=[
                ("a.txt", b"aaa", "text/plain"),
                ("b.txt", b"bbb", "text/plain"),
                ("c.txt", b"ccc", "text/plain"),
            ]
        )
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert len(parsed.attachments) == 3
        indexes = [a.part_index for a in parsed.attachments]
        assert len(set(indexes)) == 3

    def test_illegal_filename_sanitized(self, parser: MailParser) -> None:
        raw = build_eml(attachments=[('bad:name?.txt', b"x", "text/plain")])
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert not any(ch in parsed.attachments[0].filename for ch in ':*?')

    def test_empty_attachment_marked_skipped(self, parser: MailParser) -> None:
        raw = build_eml(attachments=[("empty.txt", b"", "text/plain")])
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert parsed.attachments[0].downloaded is False


class TestParseInlineImages:
    @pytest.fixture
    def parser(self) -> MailParser:
        return MailParser(max_attachment_bytes=1024 * 1024)

    def test_inline_image_flagged_and_cid_mapped(self, parser: MailParser) -> None:
        """§11.3：必须识别 Content-Disposition: inline 的图片并映射 cid。"""
        html = '<p>见图</p><img src="cid:logo@corp" width="200" height="60">'
        raw = build_eml(
            html=html,
            inline_images=[("logo@corp", b"\x89PNG-fake-bytes", "image/png")],
        )
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        inline = [a for a in parsed.attachments if a.is_inline]
        assert len(inline) == 1
        assert inline[0].content_id == "logo@corp"
        assert "logo@corp" in parsed.cid_map
        assert "attachments/" in parsed.cid_map["logo@corp"]

    def test_inline_image_not_counted_as_attachment(self, parser: MailParser) -> None:
        html = '<img src="cid:x@y">'
        raw = build_eml(html=html, inline_images=[("x@y", b"img", "image/png")])
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert parsed.has_attachments is False


class TestParseHtmlBody:
    @pytest.fixture
    def parser(self) -> MailParser:
        return MailParser(max_attachment_bytes=1024 * 1024)

    def test_html_preferred_over_plain(self, parser: MailParser) -> None:
        raw = build_eml(text="纯文本版", html="<p>HTML 版正文</p>")
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert "HTML 版正文" in parsed.body_markdown

    def test_plain_only(self, parser: MailParser) -> None:
        raw = build_eml(text="只有纯文本")
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert "只有纯文本" in parsed.body_text

    def test_body_truncated_at_limit(self) -> None:
        parser = MailParser(max_attachment_bytes=10**9, max_body_bytes=50)
        raw = build_eml(text="长正文" * 500)
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert len(parsed.text_plain) <= 50

    def test_gbk_email(self, parser: MailParser) -> None:
        """GBK 编码的中文邮件必须正确解码。"""
        msg = MIMEText("这是国标编码的正文内容。", "plain", "gb18030")
        msg["Subject"] = "GBK 邮件"
        msg["From"] = "a@x.com"
        parsed = parser.parse(msg, folder="INBOX", uid="1")
        assert "国标编码" in parsed.body_text

    def test_nested_multipart(self, parser: MailParser) -> None:
        """multipart/mixed 套 multipart/alternative 的常见结构。"""
        raw = build_eml(
            text="纯文本部分",
            html="<p>HTML 部分</p>",
            attachments=[("f.txt", b"file", "text/plain")],
        )
        parsed = parser.parse(parse_eml(raw), folder="INBOX", uid="1")
        assert "HTML 部分" in parsed.body_markdown
        assert len(parsed.attachments) == 1
