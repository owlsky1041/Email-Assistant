"""可选后端测试（未安装依赖时自动跳过）。

* ChromaDB 向量库
* ONNXRuntime 嵌入后端
* 托盘（pystray / Pillow）
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.config import AppConfig, load_config_from_mapping
from src.context import AppContext
from src.models import ParsedMessage



def _has(module: str) -> bool:
    """模块是否可用。

    注意必须捕获所有异常：无显示环境下 ``import pystray`` 抛的是
    ``Xlib.error.DisplayNameError``，只捕 ImportError 会让整个测试收集失败。
    """
    try:
        __import__(module)
        return True
    except Exception:  # noqa: BLE001
        return False


requires_chroma = pytest.mark.skipif(not _has("chromadb"), reason="未安装 chromadb")
requires_onnx = pytest.mark.skipif(
    not (_has("onnxruntime") and _has("tokenizers") and _has("numpy")),
    reason="未安装 onnxruntime / tokenizers / numpy",
)
requires_tray = pytest.mark.skipif(
    not (_has("pystray") and _has("PIL")), reason="未安装 pystray / Pillow"
)


def make_config(tmp_path: Path, *, vector: str, embedder: str = "hashing") -> AppConfig:
    return load_config_from_mapping(
        {
            "storage": {
                "archive_dir": str(tmp_path / "a"),
                "sqlite_path": str(tmp_path / "m.db"),
                "chroma_dir": str(tmp_path / "c"),
                "backup_dir": str(tmp_path / "b"),
                    "blob_dir": str(tmp_path / "blobs"),
                "attachment_dir": str(tmp_path / "at"),
            },
            "log": {"dir": str(tmp_path / "l"), "console": False, "level": "ERROR"},
            "embedding": {
                "backend": embedder,
                "dimension": 256,
                "chunk_size": 120,
                "chunk_overlap": 30,
                # 必须指向临时目录：否则会命中项目里真实的 data/models 模型，
                # 让「模型缺失时降级」这类测试失去意义
                "model_dir": str(tmp_path / "models"),
            },
            "vector": {"backend": vector},
        },
        create_dirs=True,
    )


def seed(context: AppContext) -> None:
    corpus = [
        ("m1@x.com", "季度报销发票汇总", "本季度差旅报销发票已整理完毕，请财务审核。"),
        ("m2@x.com", "服务器扩容申请", "申请对订单服务进行扩容，需要新增三台机器。"),
        ("m3@x.com", "年度绩效考核通知", "请各位在月底前完成自评并提交主管。"),
        ("m4@x.com", "产品需求评审纪要", "会上确认三条核心需求，优先结算流程重构。"),
    ]
    for index, (mid, subject, body) in enumerate(corpus, start=1):
        message = ParsedMessage(
            uid=str(index), folder="INBOX", uidvalidity=1, message_id=mid,
            subject=subject, sender="alice@corp.com",
            date=datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc),
            body_text=body * 6, body_markdown=body * 6,
        )
        archive = context.sync.exporter.export(message, account="t@x.com")
        record = context.sync._to_record(message, archive, duplicate_of=None)
        context.db.insert_message(record, archive.attachments)
    context.indexer.index_pending()


@requires_chroma
class TestChromaBackend:
    def test_backend_selected(self, tmp_path: Path) -> None:
        ctx = AppContext(make_config(tmp_path, vector="chroma"), configure_logging=False)
        try:
            assert ctx.vector_store.backend == "chroma"
            assert ctx.vector_store.health()["backend"] == "chroma"
        finally:
            ctx.close()

    def test_index_and_search(self, tmp_path: Path) -> None:
        ctx = AppContext(make_config(tmp_path, vector="chroma"), configure_logging=False)
        try:
            seed(ctx)
            assert ctx.vector_store.count() >= 4
            hits = ctx.search.search("报销发票", limit=3)
            assert hits
            assert "报销" in hits[0].subject
        finally:
            ctx.close()

    def test_persistence_across_restart(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, vector="chroma")
        ctx = AppContext(config, configure_logging=False)
        seed(ctx)
        count = ctx.vector_store.count()
        ctx.close()

        ctx2 = AppContext(config, configure_logging=False)
        try:
            assert ctx2.vector_store.count() == count, "Chroma 应持久化到磁盘"
            assert ctx2.search.search("扩容", limit=2)
        finally:
            ctx2.close()

    def test_incremental_reuse_with_chroma(self, tmp_path: Path) -> None:
        """Chroma 后端同样要能复用未变化切片的向量。"""
        ctx = AppContext(make_config(tmp_path, vector="chroma"), configure_logging=False)
        try:
            seed(ctx)
            pk = ctx.db.query_one("SELECT id FROM messages LIMIT 1")["id"]
            ctx.db.execute("UPDATE messages SET indexed_at = NULL WHERE id = ?", (pk,))
            stats = ctx.indexer.index_pending()
            assert stats.reused >= 1
            assert stats.embedded == 0
        finally:
            ctx.close()

    def test_filters_applied_with_chroma(self, tmp_path: Path) -> None:
        """回归：Chroma 后端也必须尊重 sender / folder 等过滤条件。"""
        from src.search import SearchFilters

        ctx = AppContext(make_config(tmp_path, vector="chroma"), configure_logging=False)
        try:
            seed(ctx)
            hits = ctx.search.search(
                "报销", limit=5, filters=SearchFilters(folder=["Sent"])
            )
            assert hits == []
        finally:
            ctx.close()

    def test_reset_clears_collection(self, tmp_path: Path) -> None:
        ctx = AppContext(make_config(tmp_path, vector="chroma"), configure_logging=False)
        try:
            seed(ctx)
            assert ctx.vector_store.count() > 0
            ctx.vector_store.reset()
            assert ctx.vector_store.count() == 0
        finally:
            ctx.close()

    def test_delete_by_chunk_id(self, tmp_path: Path) -> None:
        ctx = AppContext(make_config(tmp_path, vector="chroma"), configure_logging=False)
        try:
            seed(ctx)
            chunks = [c["id"] for c in ctx.db.get_chunks("m1@x.com")]
            assert chunks
            ctx.vector_store.delete(chunks)
            assert ctx.vector_store.count() < 4 + 1
        finally:
            ctx.close()


@requires_onnx
class TestOnnxBackend:
    def test_missing_model_dir_raises_clear_error(self, tmp_path: Path) -> None:
        from src.embedder import EmbedderError, OnnxEmbedder

        with pytest.raises(EmbedderError, match="model.onnx"):
            OnnxEmbedder(tmp_path / "nonexistent")

    def test_auto_falls_back_when_model_absent(self, tmp_path: Path) -> None:
        """模型缺失时应降级到 hashing，而不是崩溃。"""
        ctx = AppContext(
            make_config(tmp_path, vector="sqlite-bruteforce", embedder="auto"),
            configure_logging=False,
        )
        try:
            assert ctx.embedder.name == "hashing"
        finally:
            ctx.close()


@requires_tray
class TestTrayAssets:
    def test_icon_image_built(self) -> None:
        from src.tray_app import build_icon_image

        image = build_icon_image(64)
        assert image is not None
        assert image.size == (64, 64)

    def test_tray_available_flag(self) -> None:
        from src.tray_app import TRAY_AVAILABLE

        assert TRAY_AVAILABLE is True


class TestOpenLocalPath:
    def test_missing_path_returns_false(self, tmp_path: Path) -> None:
        from src.tray_app import open_local_path

        assert open_local_path(tmp_path / "absent") is False


class TestAdaptiveScoreThreshold:
    """相似度阈值的自适应行为。

    不同嵌入后端的余弦分布差异极大，用一个全局魔数会在切换后端时
    悄悄改变召回行为，因此未显式配置时应采用后端推荐值。
    """

    def test_embedders_expose_recommended_score(self) -> None:
        from src.embedder import HashingEmbedder

        assert HashingEmbedder().recommended_min_score == 0.25

    @requires_onnx
    def test_onnx_recommends_higher_threshold(self) -> None:
        from src.embedder import OnnxEmbedder

        assert OnnxEmbedder.recommended_min_score == 0.35

    def test_config_zero_means_adaptive(self) -> None:
        from src.config import AppConfig

        assert AppConfig().search.min_vector_score == 0.0, "0 表示沿用后端推荐值"

    def test_search_engine_uses_backend_recommendation(self, tmp_path: Path) -> None:
        from src.database import Database
        from src.search import SearchEngine

        config = make_config(tmp_path, vector="sqlite-bruteforce")
        db = Database(tmp_path / "m.db")
        db.initialize()
        try:
            engine = SearchEngine(db, config=config)
            from src.embedder import HashingEmbedder

            assert engine._effective_min_score(HashingEmbedder()) == 0.25
        finally:
            db.close()

    def test_explicit_config_overrides_recommendation(self, tmp_path: Path) -> None:
        from src.database import Database
        from src.embedder import HashingEmbedder
        from src.search import SearchEngine

        config = make_config(tmp_path, vector="sqlite-bruteforce")
        config.search.min_vector_score = 0.9
        db = Database(tmp_path / "m.db")
        db.initialize()
        try:
            engine = SearchEngine(db, config=config)
            assert engine._effective_min_score(HashingEmbedder()) == 0.9
        finally:
            db.close()

    def test_health_reports_threshold(self, tmp_path: Path) -> None:
        ctx = AppContext(make_config(tmp_path, vector="sqlite-bruteforce"),
                         configure_logging=False)
        try:
            assert ctx.embedder.health()["recommended_min_score"] == 0.25
        finally:
            ctx.close()
