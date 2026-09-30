"""真实 ImapClient 的响应解析测试。

`fetch_sizes` / `fetch_text_parts` 需要解析 imaplib 返回的
「元组 + 字节串」混合结构，这是最容易出错的地方。
这里通过注入假的底层 mailbox 对象来精确验证解析逻辑，无需真实服务器。
"""

from __future__ import annotations

import email
from typing import Any

import pytest

from src.config import AppConfig
from src.imap_client import (
    ImapClient,
    _section_from_head,
    _tokenize_bodystructure,
)


class FakeUnderlyingMailbox:
    """模拟 imaplib.IMAP4_SSL，只实现被 ImapClient 用到的部分。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[Any, ...]] = []

    def uid(self, command: str, *args: Any):  # type: ignore[no-untyped-def]
        self.calls.append((command, *args))
        # 优先精确匹配 "FETCH:1,2"，其次匹配 "FETCH:..."，最后退到裸 "FETCH"
        exact = f"{command}:{args[1] if len(args) > 1 else ''}"
        if exact in self.responses:
            return self.responses[exact]
        for key, value in self.responses.items():
            if key.startswith(f"{command}:"):
                return value
        return self.responses.get(command, ("OK", []))

    def folder(self):  # type: ignore[no-untyped-def]
        return self

    def set(self, *args: Any, **kwargs: Any) -> None:
        return None

    def noop(self):  # type: ignore[no-untyped-def]
        return ("OK", [b""])


@pytest.fixture
def client(tmp_config: AppConfig) -> ImapClient:
    imap = ImapClient(tmp_config, "dummy-auth-code")
    imap._current_folder = "INBOX"
    return imap


class TestSectionFromHead:
    def test_simple_section(self) -> None:
        assert _section_from_head(b'1 (BODY[1] {123}') == "1"

    def test_nested_section(self) -> None:
        assert _section_from_head(b'1 (BODY[1.2] {50}') == "1.2"

    def test_mime_suffix(self) -> None:
        assert _section_from_head(b'1 (BODY[2.MIME] {80}') == "2.MIME"

    def test_no_section(self) -> None:
        assert _section_from_head(b"1 (RFC822.SIZE 100)") is None
        assert _section_from_head(b"") is None


class TestFetchSizes:
    def test_parses_uid_and_size(self, client: ImapClient) -> None:
        client._mailbox = FakeUnderlyingMailbox(
            {
                "FETCH:1,2": (
                    "OK",
                    [
                        (b"1 (UID 101 RFC822.SIZE 4096)", b""),
                        (b"2 (UID 102 RFC822.SIZE 8192)", b""),
                        b")",
                    ],
                )
            }
        )
        sizes = client.fetch_sizes(["1", "2"], folder="INBOX")
        # UID 才是键，FETCH 的序号不是
        assert sizes == {"101": 4096, "102": 8192}

    def test_chunks_large_requests(self, client: ImapClient) -> None:
        """§11.2 分页：超过 200 个 UID 要拆成多次 FETCH。"""
        uids = [str(i) for i in range(1, 451)]
        client._mailbox = FakeUnderlyingMailbox({"FETCH": ("OK", [])})
        client.fetch_sizes(uids, folder="INBOX")
        assert len(client._mailbox.calls) == 3  # 200 + 200 + 50

    def test_empty_input(self, client: ImapClient) -> None:
        client._mailbox = FakeUnderlyingMailbox({})
        assert client.fetch_sizes([], folder="INBOX") == {}

    def test_error_response_ignored(self, client: ImapClient) -> None:
        client._mailbox = FakeUnderlyingMailbox({"FETCH": ("NO", [b"error"])})
        assert client.fetch_sizes(["1"], folder="INBOX") == {}

    def test_malformed_entries_skipped(self, client: ImapClient) -> None:
        client._mailbox = FakeUnderlyingMailbox(
            {
                "FETCH": (
                    "OK",
                    [
                        (b"1 (UID 101 RFC822.SIZE 100)", b""),
                        b"garbage",
                        (b"2 (NO UID HERE)", b""),
                        (b"3 (UID 103 RFC822.SIZE 300)", b""),
                    ],
                )
            }
        )
        assert client.fetch_sizes(["1", "2", "3"], folder="INBOX") == {
            "101": 100,
            "103": 300,
        }

    def test_exception_falls_back_gracefully(self, client: ImapClient) -> None:
        class Boom(FakeUnderlyingMailbox):
            def uid(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                raise OSError("连接断开")

        client._mailbox = Boom({})
        # 拿不到大小不应让整次同步失败
        assert client.fetch_sizes(["1"], folder="INBOX") == {}


class TestFetchTextParts:
    def test_parses_mime_and_body(self, client: ImapClient) -> None:
        mime = b'Content-Type: text/plain; charset="utf-8"\r\nContent-Transfer-Encoding: 7bit\r\n'
        body = b"\r\nHello \xe4\xb8\xad\xe6\x96\x87"
        client._mailbox = FakeUnderlyingMailbox(
            {
                "FETCH:5": (
                    "OK",
                    [
                        (b"5 (BODY[1.MIME] {80}", mime),
                        (b" BODY[1] {20}", body),
                        b")",
                    ],
                )
            }
        )
        parts = client.fetch_text_parts("5", ["1"], folder="INBOX")
        assert len(parts) == 1
        parsed = email.message_from_bytes(parts[0])
        assert parsed.get_content_type() == "text/plain"
        text = parsed.get_payload(decode=True).decode("utf-8")
        assert "中文" in text

    def test_missing_section_skipped(self, client: ImapClient) -> None:
        client._mailbox = FakeUnderlyingMailbox({"FETCH:5": ("OK", [])})
        assert client.fetch_text_parts("5", ["1", "2"], folder="INBOX") == []

    def test_empty_sections(self, client: ImapClient) -> None:
        client._mailbox = FakeUnderlyingMailbox({})
        assert client.fetch_text_parts("5", [], folder="INBOX") == []

    def test_exception_returns_empty(self, client: ImapClient) -> None:
        class Boom(FakeUnderlyingMailbox):
            def uid(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                raise OSError("断开")

        client._mailbox = Boom({})
        assert client.fetch_text_parts("5", ["1"], folder="INBOX") == []


class TestTextSections:
    def test_picks_text_parts_only(self, client: ImapClient) -> None:
        from src.imap_client import PartInfo

        parts = [
            PartInfo(section="1", content_type="text/plain", size=100),
            PartInfo(section="2", content_type="application/pdf", size=200),
            PartInfo(section="3", content_type="text/html", size=300),
        ]
        assert client.text_sections(parts) == ["1", "3"]

    def test_excludes_oversize_text_parts(self, client: ImapClient) -> None:
        from src.imap_client import PartInfo

        client.max_attachment_bytes = 100
        parts = [
            PartInfo(section="1", content_type="text/plain", size=50),
            PartInfo(section="2", content_type="text/plain", size=999999),
        ]
        assert client.text_sections(parts) == ["1"]

    def test_caps_section_count(self, client: ImapClient) -> None:
        from src.imap_client import PartInfo

        parts = [
            PartInfo(section=str(i), content_type="text/plain", size=10)
            for i in range(1, 20)
        ]
        assert len(client.text_sections(parts)) <= 4


class TestTokenizer:
    def test_nested_lists(self) -> None:
        tree = _tokenize_bodystructure(b'(("a" "b") "c")')
        assert tree == [[[b"a", b"b"], b"c"]]

    def test_quoted_string_with_spaces(self) -> None:
        tree = _tokenize_bodystructure(b'("hello world" NIL)')
        assert tree == [[b"hello world", b"NIL"]]

    def test_escaped_quote(self) -> None:
        tree = _tokenize_bodystructure(b'("a\\"b")')
        assert tree == [[b'a"b']]

    def test_unbalanced_parens_do_not_crash(self) -> None:
        assert _tokenize_bodystructure(b'((("a"') is not None
        assert _tokenize_bodystructure(b')))') is not None


class TestAuthCodeHandling:
    def test_missing_auth_code_raises_clear_error(self, tmp_config: AppConfig) -> None:
        from src.imap_client import ImapAuthError, ImapClient

        tmp_config.email.address = ""
        client = ImapClient(tmp_config, "")
        with pytest.raises(ImapAuthError, match="未配置邮箱地址"):
            client.connect()

    def test_missing_auth_code_message_mentions_setup(
        self, tmp_config: AppConfig
    ) -> None:
        from src.imap_client import ImapAuthError, ImapClient

        client = ImapClient(tmp_config, "")
        with pytest.raises(ImapAuthError, match="授权码"):
            client.connect()

    def test_auth_code_registered_for_redaction(self, tmp_config: AppConfig) -> None:
        """§8：拿到授权码后必须立刻注册到日志脱敏列表。"""
        import logging

        from src.logging_setup import RedactingFilter, setup_logging

        setup_logging(tmp_config, force=True)
        secret = "AuthCodeShouldNeverAppear123"
        client = ImapClient(tmp_config, secret)
        # connect 会失败，但 register_runtime_secret 在此之前已执行
        with pytest.raises(Exception):
            client.connect()

        filters = [
            f
            for h in logging.getLogger().handlers
            for f in h.filters
            if isinstance(f, RedactingFilter)
        ]
        assert filters, "应存在脱敏过滤器"
        assert any(secret in f._secrets for f in filters), "授权码应已加入脱敏列表"


class TestDisconnect:
    def test_disconnect_is_safe_when_never_connected(self, client: ImapClient) -> None:
        client.disconnect()  # 不应抛异常

    def test_double_disconnect(self, client: ImapClient) -> None:
        client.disconnect()
        client.disconnect()

    def test_mailbox_access_before_connect_raises(self, client: ImapClient) -> None:
        from src.imap_client import ImapConnectionError

        with pytest.raises(ImapConnectionError):
            _ = client.mailbox


class TestFolderFiltering:
    def test_drafts_and_junk_flags(self) -> None:
        from src.imap_client import FolderInfoLite

        assert FolderInfoLite("Drafts", flags=("\\Drafts",)).is_drafts
        assert FolderInfoLite("Junk", flags=("\\Junk",)).is_junk
        assert not FolderInfoLite("INBOX").is_drafts
