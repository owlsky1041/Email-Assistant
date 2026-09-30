"""应用上下文：集中装配数据库、嵌入器、向量库、索引与检索服务。

所有组件**按需惰性创建**——因为加载本地嵌入模型需要数秒，
而 ``status`` / ``list`` 之类的命令并不需要它。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken, get_cancellation_token
from .config import AppConfig
from .database import Database
from .embedder import Embedder, create_embedder
from .indexer import IndexService
from .logging_setup import setup_logging
from .models import utcnow
from .search import SearchEngine
from .sync_service import SyncService
from .vector_store import VectorStore, create_vector_store

logger = logging.getLogger(__name__)


class AppContext:
    """应用级依赖容器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        cancel_token: CancellationToken | None = None,
        configure_logging: bool = True,
    ) -> None:
        self.config = config
        self.cancel = cancel_token or get_cancellation_token()
        if configure_logging:
            setup_logging(config)
        config.ensure_directories()

        self.db = Database(config.sqlite_file)
        self.db.initialize()

        self._embedder: Embedder | None = None
        self._vector_store: VectorStore | None = None
        self._indexer: IndexService | None = None
        self._search: SearchEngine | None = None
        self._sync: SyncService | None = None
        self._lock = threading.RLock()
        self._closed = False

    # ------------------------------------------------------------------
    # 惰性组件
    # ------------------------------------------------------------------

    @property
    def embedder(self) -> Embedder:
        with self._lock:
            if self._embedder is None:
                logger.info("正在初始化嵌入后端…")
                self._embedder = create_embedder(self.config, cancel_token=self.cancel)
            return self._embedder

    @property
    def vector_store(self) -> VectorStore:
        with self._lock:
            if self._vector_store is None:
                logger.info("正在初始化向量库…")
                self._vector_store = create_vector_store(
                    self.config,
                    self.db,
                    model_name=getattr(self.embedder, "name", ""),
                    cancel_token=self.cancel,
                )
            return self._vector_store

    @property
    def indexer(self) -> IndexService:
        with self._lock:
            if self._indexer is None:
                self._indexer = IndexService(
                    self.db, self.embedder, self.vector_store, self.config, cancel_token=self.cancel
                )
            return self._indexer

    @property
    def search(self) -> SearchEngine:
        with self._lock:
            if self._search is None:
                # 传工厂而非实例：纯关键词检索 / 邮件列表不会加载嵌入模型
                self._search = SearchEngine(
                    self.db,
                    config=self.config,
                    vector_store=lambda: self.vector_store,
                    embedder=lambda: self.embedder,
                )
            return self._search

    @property
    def sync(self) -> SyncService:
        with self._lock:
            if self._sync is None:
                self._sync = SyncService(
                    self.config,
                    self.db,
                    index_service_factory=lambda: self.indexer,
                    cancel_token=self.cancel,
                )
            return self._sync

    @property
    def sync_running(self) -> bool:
        """轻量查询同步状态。

        不能直接用 ``context.sync.is_running``——那会实例化 SyncService，
        进而连锁加载嵌入模型与向量库，使健康检查变得极其昂贵。
        """
        sync = self._sync
        return sync is not None and sync.is_running

    def warmup(self) -> dict[str, Any]:
        """预先加载模型与向量库（服务启动时调用，避免首个请求超时）。"""
        info: dict[str, Any] = {}
        try:
            info["embedder"] = self.embedder.health()
        except Exception as exc:  # noqa: BLE001
            logger.error("嵌入后端预热失败：%s", exc)
            info["embedder"] = {"error": str(exc)}
        try:
            info["vector_store"] = self.vector_store.health()
        except Exception as exc:  # noqa: BLE001
            logger.error("向量库预热失败：%s", exc)
            info["vector_store"] = {"error": str(exc)}
        return info

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def backup(self, *, include_files: bool = False, label: str = "") -> Path:
        """备份数据库（可选附带归档目录）。

        §7 数据库备份功能：使用 ``VACUUM INTO``，可在 WAL 模式下安全热备。
        """
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{label}" if label else ""
        target_dir = self.config.backup_path
        target_dir.mkdir(parents=True, exist_ok=True)
        db_copy = target_dir / f"mail_{stamp}{suffix}.db"

        self.db.execute("PRAGMA wal_checkpoint(FULL)")
        try:
            self.db.execute("VACUUM INTO ?", (str(db_copy),))
        except sqlite3.OperationalError:
            # 旧版 SQLite 不支持参数化 VACUUM INTO
            escaped = str(db_copy).replace("'", "''")
            self.db.execute(f"VACUUM INTO '{escaped}'")

        logger.info("数据库已备份至 %s", db_copy)

        if not include_files:
            return db_copy

        archive = target_dir / f"mail_archive_{stamp}{suffix}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(self.config.archive_path.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(self.config.archive_path.parent))
        logger.info("归档文件已打包至 %s", archive)
        return archive

    def restore(self, backup_file: str | Path) -> Path:
        """从备份恢复数据库（覆盖当前库，先另存旧库）。"""
        source = Path(backup_file)
        if not source.is_file():
            raise FileNotFoundError(f"备份文件不存在：{source}")

        # 校验备份可用
        probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            result = probe.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError(f"备份文件完整性校验失败：{result}")
        finally:
            probe.close()

        self.db.close()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if Path(self.db.path).exists():
            shutil.copy2(self.db.path, f"{self.db.path}.{stamp}.before-restore")
        shutil.copy2(source, self.db.path)

        self.db = Database(self.config.sqlite_file)
        self.db.initialize()
        # 依赖旧连接的派生服务必须一并重建，否则向量缓存会指向已被替换的数据
        self._reset_data_services()
        logger.info("已从 %s 恢复数据库", source)
        return Path(self.db.path)

    def _reset_data_services(self) -> None:
        """丢弃所有持有 Database 引用的派生服务，使其在下次访问时重建。"""
        with self._lock:
            self._vector_store = None
            self._indexer = None
            self._search = None
            self._sync = None

    def close(self) -> None:
        """关闭全部资源。**幂等**：重复调用不会报错，也不会重复记录日志。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._vector_store is not None:
                self._vector_store.persist()
        except Exception:  # noqa: BLE001
            logger.debug("向量库落盘失败", exc_info=True)
        try:
            self.db.optimize()
        except Exception:  # noqa: BLE001
            logger.debug("数据库优化失败", exc_info=True)
        self.db.close()
        logger.info("应用上下文已关闭")

    def __enter__(self) -> "AppContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        self.close()
        return False

    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "account": self.config.email.address,
            "database": self.db.schema_summary(),
            "storage": {
                "archive_dir": str(self.config.archive_path),
                "attachment_dir": str(self.config.attachment_path),
                "chroma_dir": str(self.config.chroma_path),
            },
            "counts": {
                "messages": self.db.count_messages(),
                "chunks": self.db.count_chunks(),
                "vectors": self.db.count_vectors(),
            },
            "started_at": utcnow().isoformat(),
        }


def create_context(config: AppConfig | None = None, **kwargs: Any) -> AppContext:
    if config is None:
        from .config import load_config

        config = load_config()
    return AppContext(config, **kwargs)
