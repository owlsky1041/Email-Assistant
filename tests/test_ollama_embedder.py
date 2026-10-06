"""Ollama 嵌入后端的测试。

用**进程内的假 HTTP 服务**覆盖新旧两套接口，不依赖本机真的装了 Ollama。

回归背景：用户希望除了 ONNX / SentenceTransformer 之外，还能直接用
本地已跑着的 Ollama 模型做嵌入。它必须是**纯 HTTP**（不引入新依赖），
并且对 Ollama 各版本的接口差异要能自己适配。
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.embedder import EmbedderError, OllamaEmbedder

DIM = 8


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    raw = [(digest[i % len(digest)] / 255.0) - 0.5 for i in range(DIM)]
    norm = sum(x * x for x in raw) ** 0.5 or 1.0
    return [x / norm for x in raw]


class _Recorder:
    """记录假服务收到的请求，供断言使用。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    support_new = True
    recorder: _Recorder = _Recorder()

    def log_message(self, *args):  # noqa: D102 - 静音
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).recorder.requests.append({"path": self.path, **body})

        if self.path == "/api/embed":
            if not self.support_new:
                self._send(404, {"error": "not found"})
                return
            inputs = body.get("input") or []
            if isinstance(inputs, str):
                inputs = [inputs]
            self._send(200, {"embeddings": [_vector(t) for t in inputs]})
            return

        if self.path == "/api/embeddings":
            self._send(200, {"embedding": _vector(body.get("prompt", ""))})
            return

        self._send(404, {"error": "unknown"})

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def ollama_server():
    """起一个假 Ollama。

    每个服务带一个**独立的记录器**（挂在它自己的子类上），
    不然多个测试会互相看到对方的请求。
    """
    servers = []

    def start(*, support_new: bool = True):
        recorder = _Recorder()
        handler = type(
            "H", (_Handler,), {"support_new": support_new, "recorder": recorder}
        )
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append((httpd, recorder))
        return f"http://127.0.0.1:{httpd.server_address[1]}", recorder

    yield start
    for httpd, _ in servers:
        httpd.shutdown()
        httpd.server_close()


class TestOllamaEmbedder:
    def test_probes_dimension_from_server(self, ollama_server) -> None:
        """维度必须问服务端 —— 不同模型差别很大，猜不得。"""
        embedder = OllamaEmbedder("nomic-embed-text", base_url=ollama_server()[0])
        assert embedder.dimension == DIM
        assert embedder.name == "ollama"
        assert "nomic-embed-text" in embedder.backend_id

    def test_embed_returns_one_vector_per_text(self, ollama_server) -> None:
        embedder = OllamaEmbedder("m", base_url=ollama_server()[0], batch_size=2)
        vectors = embedder.embed(["甲", "乙", "丙"])
        assert len(vectors) == 3
        assert all(len(v) == DIM for v in vectors)

    def test_same_text_same_vector(self, ollama_server) -> None:
        embedder = OllamaEmbedder("m", base_url=ollama_server()[0])
        a, b = embedder.embed(["报销发票", "报销发票"])
        assert a == b

    def test_different_text_different_vector(self, ollama_server) -> None:
        embedder = OllamaEmbedder("m", base_url=ollama_server()[0])
        a, b = embedder.embed(["报销发票", "服务器扩容"])
        assert a != b

    def test_falls_back_to_legacy_endpoint(self, ollama_server) -> None:
        """老版本 Ollama 没有 /api/embed，必须自己退回 /api/embeddings。"""
        embedder = OllamaEmbedder("bge-m3", base_url=ollama_server(support_new=False)[0])
        assert embedder.dimension == DIM
        vectors = embedder.embed(["甲", "乙"])
        assert len(vectors) == 2
        assert all(len(v) == DIM for v in vectors)

    def test_batching_splits_requests(self, ollama_server) -> None:
        url, recorder = ollama_server()
        embedder = OllamaEmbedder("m", base_url=url, batch_size=2)
        embedder.embed(["a", "b", "c", "d", "e"])
        # 维度探测 1 次 + 实际 3 批（2/2/1）
        embed_calls = [c for c in recorder.requests if c["path"] == "/api/embed"]
        assert len(embed_calls) >= 3, embed_calls

    def test_query_prefix_is_applied(self, ollama_server) -> None:
        url, recorder = ollama_server()
        embedder = OllamaEmbedder("m", base_url=url, query_prefix="查询：")
        embedder.embed_query("报销")
        prompts = [c.get("input") for c in recorder.requests if c["path"] == "/api/embed"]
        flat = [t for batch in prompts for t in (batch or [])]
        assert any(t.startswith("查询：") for t in flat), flat

    def test_empty_input_makes_no_request(self, ollama_server) -> None:
        url, recorder = ollama_server()
        embedder = OllamaEmbedder("m", base_url=url)
        before = len(recorder.requests)
        assert embedder.embed([]) == []
        assert len(recorder.requests) == before

    def test_connection_error_is_actionable(self) -> None:
        """服务没起来时，报错要直接告诉用户去跑 ollama serve。"""
        with pytest.raises(EmbedderError) as info:
            OllamaEmbedder("m", base_url="http://127.0.0.1:1", timeout=2)
        assert "ollama serve" in str(info.value)

    def test_missing_embeddings_field_is_reported(self, ollama_server) -> None:
        class Bad(type("H", (_Handler,), {})):
            def do_POST(self):  # noqa: N802
                self._send(200, {"model": "m"})

        httpd = HTTPServer(("127.0.0.1", 0), Bad)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with pytest.raises(EmbedderError) as info:
                OllamaEmbedder("m", base_url=f"http://127.0.0.1:{httpd.server_address[1]}")
            assert "embedding" in str(info.value)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_url_trailing_slash_is_normalised(self, ollama_server) -> None:
        embedder = OllamaEmbedder("m", base_url=ollama_server()[0] + "/")
        assert not embedder.base_url.endswith("/")
        assert len(embedder.embed(["x"])) == 1

    def test_health_reports_model_and_url(self, ollama_server) -> None:
        url, _ = ollama_server()
        info = OllamaEmbedder("m", base_url=url).health()
        assert info["backend"] == "ollama"
        assert info["model"] == "m"
        assert info["base_url"] == url


class TestFactoryRouting:
    def test_ollama_not_in_auto_chain(self) -> None:
        """auto 不该去试 ollama —— 没装的人会被白等一次超时。"""
        import inspect

        from src import embedder as module

        source = inspect.getsource(module.create_embedder)
        assert 'if preferred == "ollama"' in source, (
            "ollama 必须只在显式选择时才用，不能进 auto 降级链"
        )

    def test_config_defaults(self) -> None:
        from src.config import EmbeddingConfig

        cfg = EmbeddingConfig()
        assert cfg.ollama_url.startswith("http://")
        assert cfg.ollama_model
        assert cfg.ollama_timeout > 0
