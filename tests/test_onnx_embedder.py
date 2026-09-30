"""ONNXRuntime 嵌入后端的端到端测试。

用**合成的小型 ONNX 模型**（等价于一次 Embedding 查表）验证整条链路：
会话创建 → 分词 → 前向 → attention-mask 平均池化 → L2 归一化。

这样无需下载真实模型（也不需要 PyTorch），就能覆盖 OnnxEmbedder 的全部代码路径。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

onnx = pytest.importorskip("onnx", reason="需要 onnx 构造测试模型")
pytest.importorskip("onnxruntime", reason="需要 onnxruntime")
pytest.importorskip("tokenizers", reason="需要 tokenizers")

import numpy as np  # noqa: E402
from onnx import TensorProto, helper  # noqa: E402

from src.embedder import EmbedderError, OnnxEmbedder  # noqa: E402

VOCAB = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "发票", "报销", "服务器", "扩容", "测试", "文本"]
HIDDEN = 8


def build_tiny_model(path: Path, *, seed: int = 42) -> None:
    """构造 Gather(embedding_matrix, input_ids) 模型，等价于 Embedding 层。"""
    rng = random.Random(seed)
    weights = np.array(
        [[rng.uniform(-1, 1) for _ in range(HIDDEN)] for _ in VOCAB], dtype=np.float32
    )

    graph = helper.make_graph(
        nodes=[
            helper.make_node(
                "Gather", ["embedding", "input_ids"], ["output"], axis=0
            )
        ],
        name="tiny-embedder",
        inputs=[
            helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"]),
        ],
        outputs=[
            helper.make_tensor_value_info(
                "output", TensorProto.FLOAT, ["batch", "seq", HIDDEN]
            )
        ],
        initializer=[
            helper.make_tensor(
                "embedding", TensorProto.FLOAT, weights.shape, weights.flatten().tolist()
            )
        ],
    )
    # 只暴露 input_ids：用于验证 OnnxEmbedder 能容忍缺失的 attention_mask
    # （部分导出模型确实只接受 input_ids）
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 17)], producer_name="test"
    )
    model.ir_version = 9
    onnx.save(model, str(path))


def build_tokenizer(path: Path) -> None:
    """最小可用的 WordLevel 分词器。"""
    vocab = {token: index for index, token in enumerate(VOCAB)}
    tokenizer = {
        "version": "1.0",
        "truncation": None,
        "padding": None,
        "added_tokens": [],
        "normalizer": None,
        "pre_tokenizer": {"type": "Whitespace"},
        "post_processor": None,
        "decoder": None,
        "model": {
            "type": "WordLevel",
            "vocab": vocab,
            "unk_token": "[UNK]",
        },
    }
    path.write_text(json.dumps(tokenizer, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    build_tiny_model(tmp_path / "model.onnx")
    build_tokenizer(tmp_path / "tokenizer.json")
    return tmp_path


@pytest.fixture
def embedder(model_dir: Path) -> OnnxEmbedder:
    return OnnxEmbedder(model_dir, max_length=16, batch_size=4)


class TestOnnxEmbedder:
    def test_loads_and_reports_dimension(self, embedder: OnnxEmbedder) -> None:
        assert embedder.name == "onnx"
        assert embedder.dimension == HIDDEN

    def test_embed_returns_correct_shape(self, embedder: OnnxEmbedder) -> None:
        vectors = embedder.embed(["发票 报销", "服务器 扩容"])
        assert len(vectors) == 2
        assert all(len(v) == HIDDEN for v in vectors)

    def test_vectors_are_l2_normalized(self, embedder: OnnxEmbedder) -> None:
        for vector in embedder.embed(["发票 报销", "测试 文本"]):
            norm = sum(v * v for v in vector) ** 0.5
            assert norm == pytest.approx(1.0, abs=1e-5)

    def test_deterministic(self, embedder: OnnxEmbedder) -> None:
        assert embedder.embed(["发票 报销"]) == embedder.embed(["发票 报销"])

    def test_different_tokens_differ(self, embedder: OnnxEmbedder) -> None:
        a = embedder.embed(["发票"])[0]
        b = embedder.embed(["扩容"])[0]
        assert a != b

    def test_padding_does_not_change_result(self, embedder: OnnxEmbedder) -> None:
        """padding 位置必须被 attention mask 屏蔽掉（平均池化的关键）。"""
        short = embedder.embed(["发票"])[0]
        padded = embedder.embed(["发票", "发票 报销 服务器 扩容 测试 文本"])[1]
        assert len(short) == len(padded)

    def test_batch_matches_single(self, embedder: OnnxEmbedder) -> None:
        texts = ["发票", "报销", "服务器"]
        batch = embedder.embed(texts)
        for index, text in enumerate(texts):
            assert batch[index] == pytest.approx(embedder.embed([text])[0], abs=1e-6)

    def test_batch_size_respected(self, model_dir: Path) -> None:
        small = OnnxEmbedder(model_dir, batch_size=1, max_length=16)
        vectors = small.embed(["发票", "报销", "服务器"])
        assert len(vectors) == 3

    def test_unknown_token_handled(self, embedder: OnnxEmbedder) -> None:
        vectors = embedder.embed(["完全不存在的词汇"])
        assert len(vectors) == 1
        assert all(v == v for v in vectors[0])  # 不是 NaN

    def test_query_prefix_applied(self, model_dir: Path) -> None:
        with_prefix = OnnxEmbedder(model_dir, query_prefix="发票 ", max_length=16)
        assert with_prefix.embed_query("报销") == with_prefix.embed(["发票 报销"])[0]

    def test_truncation_for_long_input(self, embedder: OnnxEmbedder) -> None:
        long_text = " ".join(["发票"] * 500)
        vectors = embedder.embed([long_text])
        assert len(vectors[0]) == HIDDEN

    def test_health_reports_model_dir(self, embedder: OnnxEmbedder, model_dir: Path) -> None:
        health = embedder.health()
        assert health["backend"] == "onnx"
        assert health["dimension"] == HIDDEN
        assert str(model_dir) in str(health["model_dir"])

    def test_empty_batch(self, embedder: OnnxEmbedder) -> None:
        assert embedder.embed([]) == []


class TestOnnxErrorHandling:
    def test_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(EmbedderError, match="model.onnx"):
            OnnxEmbedder(tmp_path / "nope")

    def test_missing_tokenizer(self, tmp_path: Path) -> None:
        build_tiny_model(tmp_path / "model.onnx")
        with pytest.raises(EmbedderError, match="分词器"):
            OnnxEmbedder(tmp_path)

    def test_alternate_onnx_filename_found(self, tmp_path: Path) -> None:
        """应该能在目录里找到任意 .onnx 文件。"""
        build_tiny_model(tmp_path / "custom_name.onnx")
        build_tokenizer(tmp_path / "tokenizer.json")
        assert OnnxEmbedder(tmp_path).dimension == HIDDEN


class TestOnnxIntegration:
    def test_full_pipeline_with_onnx(self, tmp_path: Path, model_dir: Path) -> None:
        """ONNX 嵌入 + SQLite 向量库 + RRF 混合检索 全链路。"""
        from datetime import datetime, timezone

        from src.config import load_config_from_mapping
        from src.context import AppContext
        from src.models import ParsedMessage

        config = load_config_from_mapping(
            {
                "storage": {
                    "archive_dir": str(tmp_path / "a"),
                    "sqlite_path": str(tmp_path / "m.db"),
                    "chroma_dir": str(tmp_path / "c"),
                    "backup_dir": str(tmp_path / "b"),
                    "attachment_dir": str(tmp_path / "at"),
                },
                "log": {"dir": str(tmp_path / "l"), "console": False, "level": "ERROR"},
                "embedding": {
                    "backend": "onnx",
                    "model_dir": str(model_dir),
                    "chunk_size": 60,
                    "chunk_overlap": 10,
                },
                "vector": {"backend": "sqlite-bruteforce"},
            },
            create_dirs=True,
        )
        context = AppContext(config, configure_logging=False)
        try:
            assert context.embedder.name == "onnx", "应使用 ONNX 后端而不是兜底后端"

            for index, (subject, body) in enumerate(
                [
                    ("发票汇总", "发票 报销 报销 发票"),
                    ("扩容申请", "服务器 扩容 服务器 扩容"),
                ],
                start=1,
            ):
                message = ParsedMessage(
                    uid=str(index), folder="INBOX", uidvalidity=1,
                    message_id=f"m{index}@x.com", subject=subject,
                    sender="a@corp.com",
                    date=datetime(2024, 3, 1, tzinfo=timezone.utc),
                    body_text=body * 5, body_markdown=body * 5,
                )
                archive = context.sync.exporter.export(message, account="t@x.com")
                record = context.sync._to_record(message, archive, duplicate_of=None)
                context.db.insert_message(record, archive.attachments)
            context.indexer.index_pending()

            assert context.db.count_vectors() >= 2
            hits = context.search.search("发票 报销", limit=2)
            assert hits
        finally:
            context.close()
