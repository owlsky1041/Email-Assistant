"""知识库 API 测试（§3.8 / §5）：接口齐全、鉴权、不泄露敏感信息。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.context import AppContext
from src.kb_api import check_bind_address, create_app
from src.models import ParsedMessage


@pytest.fixture
def seeded(context: AppContext) -> AppContext:
    """写入两封邮件并建索引。"""
    from tests.conftest import build_eml, parse_eml

    corpus = [
        ("1", "季度报销发票汇总", "本季度差旅报销发票已整理完毕，请财务审核。"),
        ("2", "服务器扩容申请", "申请对订单服务进行扩容，需要新增三台机器。"),
    ]
    for uid, subject, body in corpus:
        raw = build_eml(
            subject=subject,
            text=body * 8,
            message_id=f"<api{uid}@corp.com>",
            attachments=[("附件.txt", b"att", "text/plain")] if uid == "2" else None,
        )
        parsed = context.sync.parser.parse(
            parse_eml(raw), folder="INBOX", uid=uid, uidvalidity=1
        )
        archive = context.sync.exporter.export(parsed, account="tester@corp.com")
        record = context.sync._to_record(parsed, archive, duplicate_of=None)
        context.db.insert_message(record, archive.attachments)
    context.indexer.index_pending()
    return context


@pytest.fixture
def client(seeded: AppContext) -> TestClient:
    return TestClient(create_app(seeded))


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token-abcdefghijklmnop"}


class TestHealth:
    def test_health_ok(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"]["fts_available"] is True
        assert body["counts"]["messages"] == 2

    def test_health_does_not_load_model(self, tmp_config) -> None:
        """健康检查不应触发模型加载（否则探针会拖慢启动）。"""
        fresh = AppContext(tmp_config, configure_logging=False)
        try:
            with TestClient(create_app(fresh)) as c:
                body = c.get("/api/health").json()
            assert "embedding" not in body
            assert fresh._embedder is None, "健康检查不应初始化嵌入模型"
        finally:
            fresh.close()

    def test_docs_available(self, client: TestClient) -> None:
        """验收标准：可通过 /docs 查看接口文档。"""
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"] == "邮件知识库 API"

    def test_openapi_declares_all_required_endpoints(self, client: TestClient) -> None:
        """§3.8 推荐接口必须全部存在。"""
        paths = client.get("/openapi.json").json()["paths"]
        required = [
            "/api/health",
            "/api/sync/status",
            "/api/sync/trigger",
            "/api/messages",
            "/api/messages/{message_ref}",
            "/api/messages/{message_ref}/content",
            "/api/attachments/{attachment_id}",
            "/api/search",
            "/api/embed",
            "/api/chunks/{message_id}",
        ]
        for path in required:
            assert path in paths, f"缺少接口：{path}"


class TestAuthentication:
    def test_rejects_missing_token(self, client: TestClient) -> None:
        assert client.get("/api/messages").status_code == 401

    def test_rejects_wrong_token(self, client: TestClient) -> None:
        response = client.get(
            "/api/messages", headers={"Authorization": "Bearer wrong-token"}
        )
        assert response.status_code == 401

    def test_rejects_malformed_header(self, client: TestClient) -> None:
        assert client.get(
            "/api/messages", headers={"Authorization": "test-token-abcdefghijklmnop"}
        ).status_code == 401

    def test_accepts_valid_token(self, client: TestClient, auth_headers) -> None:
        assert client.get("/api/messages", headers=auth_headers).status_code == 200

    def test_health_never_requires_auth(self, client: TestClient) -> None:
        """健康检查保持免鉴权，便于外部探针。"""
        assert client.get("/api/health").status_code == 200

    def test_no_auth_when_token_empty(self, context: AppContext) -> None:
        context.config.api.token = ""
        client = TestClient(create_app(context))
        assert client.get("/api/messages").status_code == 200


class TestSyncEndpoints:
    def test_status(self, client: TestClient, auth_headers) -> None:
        body = client.get("/api/sync/status", headers=auth_headers).json()
        assert "running" in body
        assert body["total_messages"] == 2
        assert "***" in body["account"], "账号必须脱敏"

    def test_trigger_accepted(self, client: TestClient, auth_headers, monkeypatch) -> None:
        # 避免真的去连 IMAP
        from src.sync_service import SyncService

        monkeypatch.setattr(
            SyncService, "sync_all", lambda self, **kw: __import__(
                "src.models", fromlist=["SyncResult"]
            ).SyncResult(folder="*")
        )
        body = client.post(
            "/api/sync/trigger", headers=auth_headers, json={"full": False}
        ).json()
        assert body["accepted"] is True

    def test_trigger_with_empty_body(self, client: TestClient, auth_headers, monkeypatch) -> None:
        from src.sync_service import SyncService

        monkeypatch.setattr(
            SyncService, "sync_all", lambda self, **kw: __import__(
                "src.models", fromlist=["SyncResult"]
            ).SyncResult(folder="*")
        )
        assert client.post("/api/sync/trigger", headers=auth_headers).status_code == 200


class TestMessageEndpoints:
    def test_list(self, client: TestClient, auth_headers) -> None:
        body = client.get("/api/messages", headers=auth_headers).json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        for item in body["items"]:
            assert "body_text" not in item

    def test_list_filter_by_subject(self, client: TestClient, auth_headers) -> None:
        body = client.get(
            "/api/messages", headers=auth_headers, params={"subject": "报销"}
        ).json()
        assert body["total"] == 1

    def test_list_filter_by_has_attachments(self, client: TestClient, auth_headers) -> None:
        body = client.get(
            "/api/messages", headers=auth_headers, params={"has_attachments": "true"}
        ).json()
        assert body["total"] == 1

    def test_list_pagination(self, client: TestClient, auth_headers) -> None:
        body = client.get(
            "/api/messages", headers=auth_headers, params={"limit": 1, "offset": 0}
        ).json()
        assert body["total"] == 2 and len(body["items"]) == 1

    def test_get_by_primary_key(self, client: TestClient, auth_headers) -> None:
        pk = client.get("/api/messages", headers=auth_headers).json()["items"][0]["id"]
        body = client.get(f"/api/messages/{pk}", headers=auth_headers).json()
        assert body["id"] == pk
        assert "attachments" in body

    def test_get_by_message_id(self, client: TestClient, auth_headers) -> None:
        body = client.get(
            "/api/messages/api1@corp.com", headers=auth_headers
        ).json()
        assert body["message_id"] == "api1@corp.com"
        assert body["subject"] == "季度报销发票汇总"

    def test_get_by_bracketed_message_id(self, client: TestClient, auth_headers) -> None:
        body = client.get(
            "/api/messages/<api1@corp.com>", headers=auth_headers
        ).json()
        assert body["message_id"] == "api1@corp.com"

    def test_get_unknown_returns_404(self, client: TestClient, auth_headers) -> None:
        assert client.get("/api/messages/9999", headers=auth_headers).status_code == 404

    def test_attachment_count_in_detail(self, client: TestClient, auth_headers) -> None:
        body = client.get("/api/messages/api2@corp.com", headers=auth_headers).json()
        assert body["has_attachments"] is True
        assert len(body["attachments"]) == 1
        assert body["attachments"][0]["exists"] is True

    def test_chunk_count_in_detail(self, client: TestClient, auth_headers) -> None:
        body = client.get("/api/messages/api1@corp.com", headers=auth_headers).json()
        assert body["chunk_count"] >= 1


class TestContentEndpoint:
    def test_returns_markdown(self, client: TestClient, auth_headers) -> None:
        response = client.get(
            "/api/messages/api1@corp.com/content", headers=auth_headers
        )
        assert response.status_code == 200
        assert "报销" in response.text

    def test_frontmatter_excluded_by_default(self, client: TestClient, auth_headers) -> None:
        response = client.get(
            "/api/messages/api1@corp.com/content", headers=auth_headers
        )
        assert not response.text.startswith("---")

    def test_frontmatter_included_on_request(self, client: TestClient, auth_headers) -> None:
        response = client.get(
            "/api/messages/api1@corp.com/content",
            headers=auth_headers,
            params={"include_frontmatter": "true"},
        )
        assert response.text.startswith("---")
        assert "message_id" in response.text

    def test_text_format(self, client: TestClient, auth_headers) -> None:
        response = client.get(
            "/api/messages/api1@corp.com/content",
            headers=auth_headers,
            params={"fmt": "text"},
        )
        assert "**" not in response.text

    def test_unknown_message_404(self, client: TestClient, auth_headers) -> None:
        assert client.get(
            "/api/messages/nope@x.com/content", headers=auth_headers
        ).status_code == 404


class TestAttachmentEndpoint:
    def test_returns_local_path(self, client: TestClient, auth_headers) -> None:
        detail = client.get("/api/messages/api2@corp.com", headers=auth_headers).json()
        attachment_id = detail["attachments"][0]["id"]
        body = client.get(f"/api/attachments/{attachment_id}", headers=auth_headers).json()
        assert body["filename"] == "附件.txt"
        assert body["exists"] is True
        assert body["size_on_disk"] == 3

    def test_unknown_404(self, client: TestClient, auth_headers) -> None:
        assert client.get("/api/attachments/99999", headers=auth_headers).status_code == 404


class TestSearchEndpoint:
    def test_hybrid(self, client: TestClient, auth_headers) -> None:
        response = client.post(
            "/api/search", headers=auth_headers, json={"query": "报销发票"}
        )
        body = response.json()
        assert response.status_code == 200
        assert body["count"] >= 1

    def test_result_shape(self, client: TestClient, auth_headers) -> None:
        """§3.7 搜索结果字段。"""
        body = client.post(
            "/api/search", headers=auth_headers, json={"query": "报销", "limit": 1}
        ).json()
        result = body["results"][0]
        for key in ("subject", "sender", "date", "folder", "snippet",
                    "local_markdown_path", "score"):
            assert key in result

    def test_keyword_mode(self, client: TestClient, auth_headers) -> None:
        body = client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "扩容", "mode": "keyword"},
        ).json()
        assert body["mode"] == "keyword"
        assert body["results"][0]["subject"] == "服务器扩容申请"

    def test_vector_mode(self, client: TestClient, auth_headers) -> None:
        body = client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "报销", "mode": "vector"},
        ).json()
        assert body["count"] >= 1

    def test_folder_filter(self, client: TestClient, auth_headers) -> None:
        body = client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "报销", "folder": ["Sent"]},
        ).json()
        assert body["count"] == 0

    def test_limit_validated(self, client: TestClient, auth_headers) -> None:
        response = client.post(
            "/api/search", headers=auth_headers, json={"query": "x", "limit": 9999}
        )
        assert response.status_code == 422

    def test_empty_query_rejected(self, client: TestClient, auth_headers) -> None:
        response = client.post(
            "/api/search", headers=auth_headers, json={"query": ""}
        )
        assert response.status_code == 422

    def test_no_match_returns_empty(self, client: TestClient, auth_headers) -> None:
        """完全无关的查询不应返回结果（最小相似度阈值 + 关键词精确匹配）。"""
        for mode in ("hybrid", "keyword", "vector"):
            body = client.post(
                "/api/search", headers=auth_headers, json={"query": "量子纠缠", "mode": mode}
            ).json()
            assert body["count"] == 0, f"{mode} 模式返回了无关结果：{body['results']}"
            assert body["results"] == []


class TestEmbedEndpoint:
    def test_returns_vectors(self, client: TestClient, auth_headers) -> None:
        body = client.post(
            "/api/embed", headers=auth_headers, json={"texts": ["文本一", "文本二"]}
        ).json()
        assert body["count"] == 2
        assert body["dimension"] == 128
        assert len(body["vectors"][0]) == 128

    def test_rejects_empty_list(self, client: TestClient, auth_headers) -> None:
        response = client.post("/api/embed", headers=auth_headers, json={"texts": []})
        assert response.status_code == 422


class TestChunksEndpoint:
    def test_returns_chunks_with_metadata(self, client: TestClient, auth_headers) -> None:
        body = client.get("/api/chunks/api1@corp.com", headers=auth_headers).json()
        assert body["total"] >= 1
        item = body["items"][0]
        for key in ("chunk_id", "message_id", "chunk_index", "text", "subject",
                    "sender", "date", "folder", "local_markdown_path"):
            assert key in item

    def test_unknown_message_empty(self, client: TestClient, auth_headers) -> None:
        body = client.get("/api/chunks/nope@x.com", headers=auth_headers).json()
        assert body["total"] == 0

    def test_pagination(self, client: TestClient, auth_headers) -> None:
        body = client.get(
            "/api/chunks/api1@corp.com", headers=auth_headers, params={"limit": 1}
        ).json()
        assert len(body["items"]) <= 1


class TestStatsEndpoint:
    def test_statistics(self, client: TestClient, auth_headers) -> None:
        body = client.get("/api/stats", headers=auth_headers).json()
        assert body["messages"] == 2
        assert "folders" in body


class TestNoSecretLeakage:
    """§8 安全要求：API 不得返回授权码、令牌等敏感配置。"""

    def test_no_secret_in_any_response(self, client: TestClient, auth_headers) -> None:
        secret_values = [
            "test-auth-code-0123456789",
            "test-token-abcdefghijklmnop",
        ]
        endpoints = [
            ("GET", "/api/health", None),
            ("GET", "/api/sync/status", None),
            ("GET", "/api/messages", None),
            ("GET", "/api/stats", None),
            ("GET", "/api/messages/api1@corp.com", None),
            ("GET", "/api/messages/api1@corp.com/content", None),
            ("GET", "/api/chunks/api1@corp.com", None),
        ]
        for method, url, payload in endpoints:
            response = client.request(method, url, headers=auth_headers, json=payload)
            text = response.text
            for secret in secret_values:
                assert secret not in text, f"{url} 泄露了敏感值"

    def test_no_config_dump_endpoint(self, client: TestClient) -> None:
        """不应存在暴露原始配置的接口。"""
        paths = client.get("/openapi.json").json()["paths"]
        assert not any("config" in path for path in paths)

    def test_sync_status_hides_auth_fields(self, client: TestClient, auth_headers) -> None:
        body = client.get("/api/sync/status", headers=auth_headers).json()
        text = json.dumps(body, ensure_ascii=False)
        assert "auth_code" not in text
        assert "imap_server" not in text

    def test_raw_html_not_exposed(self, client: TestClient, auth_headers) -> None:
        """§3.8 不暴露原始 HTML。"""
        body = client.get("/api/messages/api1@corp.com", headers=auth_headers).json()
        assert "text_html" not in body
        assert "raw_headers" not in body
        assert "text_plain" not in body


class TestBindAddressGuard:
    def test_loopback_allowed(self) -> None:
        assert check_bind_address("127.0.0.1") is None
        assert check_bind_address("::1") is None
        assert check_bind_address("localhost") is None

    def test_wildcard_flagged(self) -> None:
        """§8 API 默认不监听 0.0.0.0。"""
        message = check_bind_address("0.0.0.0")
        assert message is not None
        assert "127.0.0.1" in message

    def test_lan_address_flagged(self) -> None:
        assert check_bind_address("192.168.1.10") is not None


class TestErrorHandling:
    def test_validation_error_is_422(self, client: TestClient, auth_headers) -> None:
        response = client.post("/api/search", headers=auth_headers, json={})
        assert response.status_code == 422

    def test_unknown_path_404(self, client: TestClient, auth_headers) -> None:
        assert client.get("/api/nonexistent", headers=auth_headers).status_code == 404

    def test_error_response_hides_internals(self, client: TestClient, auth_headers) -> None:
        """错误响应不应包含堆栈或文件路径。"""
        response = client.get("/api/messages/9999", headers=auth_headers)
        text = response.text
        assert "Traceback" not in text
        assert "/root/" not in text
