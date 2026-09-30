"""SQLite 数据访问层。

设计要点
--------
* **WAL 模式**（§11.1）：支持多线程并发读写，避免 ``database is locked``。
* **线程局部连接**：每个线程持有独立连接，避免跨线程共享游标。
* **版本化迁移**：``schema_version`` 表驱动，可平滑升级到 PostgreSQL（预留）。
* **中文全文检索**：FTS5 + CJK 单字切分。SQLite 内置分词器不切中文，
  因此入库前把 CJK 字符拆成单字（"邮件助手" -> "邮 件 助 手"），
  查询时同样切分，即可获得准确的短语匹配能力。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    AttachmentMeta,
    Chunk,
    FolderState,
    MessageRecord,
    SyncResult,
    utcnow,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# CJK 统一表意文字 + 扩展 A + 兼容区 + 假名 + 谚文
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_TOKEN_SPLIT_RE = re.compile(r"[\s,，。；;、！!？?：:（）()\[\]{}<>\"'“”‘’/\\|@#$%^&*+=~`\-_]+")


def segment_cjk(text: str) -> str:
    """把 CJK 字符逐字用空格分隔，供 FTS5 unicode61 分词器索引。

    ``"邮件abc"`` -> ``"邮 件 abc"``。单空格分隔可将中文 FTS 索引体积
    相比「两侧补空格」的写法减半，检索效果完全相同。
    """
    if not text:
        return ""
    tokens: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            tokens.append("".join(buffer))
            buffer.clear()

    for ch in text:
        if _CJK_RE.match(ch):
            flush()
            tokens.append(ch)
        else:
            buffer.append(ch)
    flush()
    return " ".join(t for t in tokens if t)


def build_match_query(query: str, *, prefix: bool = True) -> str:
    """把用户查询转成 FTS5 MATCH 表达式。

    * 中文词 -> 单字短语，例如 ``发票报销`` -> ``"发 票 报 销"``
    * 英文/数字 -> 带前缀通配，例如 ``invoi`` -> ``"invoi"*``
    * 多个词之间是 AND 关系

    返回空串表示查询无效（调用方应回退到 LIKE）。
    """
    return _build_match_query(query, prefix=prefix, shorten=0)


def _build_match_query(query: str, *, prefix: bool, shorten: int) -> str:
    if not query or not query.strip():
        return ""
    terms = [t for t in _TOKEN_SPLIT_RE.split(query) if t]
    if not terms:
        return ""
    parts: list[str] = []
    for term in terms:
        if _CJK_RE.search(term):
            effective = term[: len(term) - shorten] if shorten else term
            if len(effective) < 2:
                effective = term
            chars = " ".join(effective)
            parts.append(f'"{chars}"')
        else:
            if shorten:
                continue  # 只对中文做放宽，英文保持精确
            escaped = term.replace('"', '""')
            parts.append(f'"{escaped}"*' if prefix else f'"{escaped}"')
    return " AND ".join(parts)


def build_match_query_variants(query: str, *, max_variants: int = 8) -> list[str]:
    """从严格到宽松生成多个 MATCH 表达式。

    SQLite FTS5 的中文检索是把 CJK 字符逐字索引、查询时拼成短语的。
    用户输入的自然语言问句（"报销发票怎么弄"）作为整体短语几乎不可能
    连续出现在正文里，于是严格匹配零命中。

    这里生成一系列「保留词序、逐步截短尾部」的前缀变体，按从长到短排列，
    调用方取第一个有命中的即可——既优先保证精度，又能兜住召回。

    对长问句采用**均匀采样**而非逐字递减，否则变体数很快用完，
    永远试不到真正能命中的短前缀。
    """
    strict = build_match_query(query)
    if not strict:
        return []

    terms = [t for t in _TOKEN_SPLIT_RE.split(query) if _CJK_RE.search(t)]
    longest = max((len(t) for t in terms), default=0)
    if longest < 3:
        return [strict]

    # 候选前缀长度：从 longest-1 递减到 2
    lengths = list(range(longest - 1, 1, -1))
    if len(lengths) > max_variants - 1:
        span = len(lengths)
        picks = max_variants - 1
        lengths = [lengths[int(i * span / picks)] for i in range(picks)]

    variants = [strict]
    for target in lengths:
        shorten = longest - target
        candidate = _build_match_query(query, prefix=True, shorten=shorten)
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


# ---------------------------------------------------------------------------
# 迁移脚本
# ---------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "初始表结构：messages / attachments / sync_log / kb_chunks / kb_vectors / folders",
        """
        CREATE TABLE IF NOT EXISTS folders (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            account            TEXT    NOT NULL,
            name               TEXT    NOT NULL,
            delimiter          TEXT    NOT NULL DEFAULT '/',
            uidvalidity        INTEGER NOT NULL DEFAULT 0,
            last_uid           INTEGER NOT NULL DEFAULT 0,
            last_sync_at       TEXT,
            last_full_scan_at  TEXT,
            message_count      INTEGER NOT NULL DEFAULT 0,
            selectable         INTEGER NOT NULL DEFAULT 1,
            UNIQUE (account, name)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            account              TEXT    NOT NULL DEFAULT '',
            message_id           TEXT    NOT NULL DEFAULT '',
            uid                  TEXT    NOT NULL DEFAULT '',
            uidvalidity          INTEGER NOT NULL DEFAULT 0,
            folder               TEXT    NOT NULL DEFAULT '',
            subject              TEXT    NOT NULL DEFAULT '',
            sender               TEXT    NOT NULL DEFAULT '',
            sender_name          TEXT    NOT NULL DEFAULT '',
            recipients           TEXT    NOT NULL DEFAULT '',
            cc                   TEXT    NOT NULL DEFAULT '',
            date_utc             TEXT,
            date_raw             TEXT    NOT NULL DEFAULT '',
            local_markdown_path  TEXT    NOT NULL DEFAULT '',
            body_text            TEXT    NOT NULL DEFAULT '',
            has_attachments      INTEGER NOT NULL DEFAULT 0,
            size_bytes           INTEGER NOT NULL DEFAULT 0,
            content_hash         TEXT    NOT NULL DEFAULT '',
            duplicate_of         INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            indexed_at           TEXT,
            synced_at            TEXT,
            updated_at           TEXT,
            deleted_at           TEXT,
            UNIQUE (account, folder, uidvalidity, uid)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages (account, message_id);
        CREATE INDEX IF NOT EXISTS idx_messages_folder     ON messages (account, folder, date_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_date       ON messages (date_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_sender     ON messages (sender);
        CREATE INDEX IF NOT EXISTS idx_messages_live       ON messages (deleted_at);

        CREATE TABLE IF NOT EXISTS attachments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            message_pk    INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            filename      TEXT    NOT NULL DEFAULT '',
            content_type  TEXT    NOT NULL DEFAULT 'application/octet-stream',
            size_bytes    INTEGER NOT NULL DEFAULT 0,
            content_id    TEXT,
            is_inline     INTEGER NOT NULL DEFAULT 0,
            sha256        TEXT,
            local_path    TEXT,
            downloaded    INTEGER NOT NULL DEFAULT 0,
            skip_reason   TEXT,
            part_index    INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments (message_pk);

        CREATE TABLE IF NOT EXISTS sync_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account       TEXT    NOT NULL DEFAULT '',
            folder        TEXT    NOT NULL DEFAULT '',
            started_at    TEXT    NOT NULL,
            finished_at   TEXT,
            status        TEXT    NOT NULL DEFAULT 'running',
            fetched       INTEGER NOT NULL DEFAULT 0,
            archived      INTEGER NOT NULL DEFAULT 0,
            skipped       INTEGER NOT NULL DEFAULT 0,
            failed        INTEGER NOT NULL DEFAULT 0,
            deleted       INTEGER NOT NULL DEFAULT 0,
            error_summary TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sync_log_started ON sync_log (started_at DESC);

        CREATE TABLE IF NOT EXISTS kb_chunks (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            message_pk           INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            message_id           TEXT    NOT NULL DEFAULT '',
            subject              TEXT    NOT NULL DEFAULT '',
            sender               TEXT    NOT NULL DEFAULT '',
            date_utc             TEXT,
            folder               TEXT    NOT NULL DEFAULT '',
            chunk_index          INTEGER NOT NULL DEFAULT 0,
            text                 TEXT    NOT NULL DEFAULT '',
            token_count          INTEGER NOT NULL DEFAULT 0,
            content_hash         TEXT    NOT NULL DEFAULT '',
            local_markdown_path  TEXT    NOT NULL DEFAULT '',
            created_at           TEXT    NOT NULL,
            UNIQUE (message_pk, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_message   ON kb_chunks (message_pk);
        CREATE INDEX IF NOT EXISTS idx_chunks_messageid ON kb_chunks (message_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_hash      ON kb_chunks (content_hash);

        CREATE TABLE IF NOT EXISTS kb_vectors (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id      INTEGER NOT NULL REFERENCES kb_chunks(id) ON DELETE CASCADE,
            message_id    TEXT    NOT NULL DEFAULT '',
            model         TEXT    NOT NULL DEFAULT '',
            dimension     INTEGER NOT NULL DEFAULT 0,
            backend       TEXT    NOT NULL DEFAULT '',
            vector_ref    TEXT    NOT NULL DEFAULT '',
            content_hash  TEXT    NOT NULL DEFAULT '',
            vector_blob   BLOB,
            created_at    TEXT    NOT NULL,
            UNIQUE (chunk_id, model)
        );
        CREATE INDEX IF NOT EXISTS idx_vectors_message ON kb_vectors (message_id);
        CREATE INDEX IF NOT EXISTS idx_vectors_chunk   ON kb_vectors (chunk_id);
        """,
    ),
]

FTS_TABLES = ("messages_fts", "chunks_fts")


def _now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    """SQLite 封装。线程安全（每线程独立连接）。"""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = str(path)
        self.read_only = read_only
        self._is_memory = self.path in (":memory:", "")
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        self._write_lock = threading.RLock()
        self._fts_available = True
        self._ensure_parent()
        with self._connect_ctx() as conn:
            self._configure(conn)

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _ensure_parent(self) -> None:
        if self._is_memory:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _new_connection(self) -> sqlite3.Connection:
        if self._is_memory:
            if self._shared is None:
                self._shared = sqlite3.connect(":memory:", check_same_thread=False)
            return self._shared
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,  # 自动提交，事务由我们自己管理
            check_same_thread=True,
        )
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._configure(conn)
            self._local.conn = conn
        return conn

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # §11.1 WAL
        if not self._is_memory:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-16000")  # ~16MB
        cur.close()

    @contextmanager
    def _connect_ctx(self) -> Iterator[sqlite3.Connection]:
        """临时连接（用于初始化和备份），不进入线程局部缓存。"""
        conn = sqlite3.connect(self.path if not self._is_memory else ":memory:")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None
        if self._shared is not None:
            try:
                self._shared.close()
            except sqlite3.Error:
                pass
            self._shared = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式写事务；异常时回滚（§11.4 保证事务完整性）。"""
        conn = self.conn
        with self._write_lock:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
            finally:
                cur.close()

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    # ------------------------------------------------------------------
    # 初始化 / 迁移
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """建表 + 迁移 + 建 FTS。幂等，可重复调用。"""
        with self._write_lock:
            conn = self.conn
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version     INTEGER PRIMARY KEY,
                    applied_at  TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT ''
                )
                """
            )
            applied = {
                int(r["version"])
                for r in conn.execute("SELECT version FROM schema_version").fetchall()
            }
            for version, description, script in MIGRATIONS:
                if version in applied:
                    continue
                logger.info("应用数据库迁移 v%s：%s", version, description)
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                try:
                    cur.executescript(script)
                    cur.execute(
                        "INSERT INTO schema_version (version, applied_at, description) "
                        "VALUES (?, ?, ?)",
                        (version, _now_iso(), description),
                    )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    cur.close()
            self._init_fts()

    def _init_fts(self) -> None:
        conn = self.conn
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    subject, sender, recipients, body_text,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, subject, sender,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )
            self._fts_available = True
        except sqlite3.OperationalError as exc:
            logger.error(
                "FTS5 不可用（%s）。关键词检索将退化为 LIKE 扫描，"
                "建议使用自带 FTS5 的 Python 发行版。",
                exc,
            )
            self._fts_available = False

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    def schema_summary(self) -> dict[str, Any]:
        tables = [
            r["name"]
            for r in self.query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        version = self.query_one("SELECT MAX(version) AS v FROM schema_version")
        return {
            "path": self.path,
            "schema_version": int(version["v"]) if version and version["v"] else 0,
            "tables": tables,
            "fts_available": self._fts_available,
            "journal_mode": (
                self.query_one("PRAGMA journal_mode")[0] if not self._is_memory else "memory"
            ),
        }

    # ------------------------------------------------------------------
    # folders
    # ------------------------------------------------------------------

    def upsert_folder(self, state: FolderState) -> int:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO folders (account, name, delimiter, uidvalidity, last_uid,
                                     last_sync_at, last_full_scan_at, message_count, selectable)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (account, name) DO UPDATE SET
                    delimiter          = excluded.delimiter,
                    uidvalidity        = excluded.uidvalidity,
                    selectable         = excluded.selectable
                """,
                (
                    state.account,
                    state.name,
                    state.delimiter,
                    state.uidvalidity or 0,
                    state.last_uid,
                    state.last_sync_at.isoformat() if state.last_sync_at else None,
                    state.last_full_scan_at.isoformat() if state.last_full_scan_at else None,
                    state.message_count,
                    int(state.selectable),
                ),
            )
            row = conn.execute(
                "SELECT id FROM folders WHERE account = ? AND name = ?",
                (state.account, state.name),
            ).fetchone()
        return int(row["id"])

    def get_folder(self, account: str, name: str) -> FolderState | None:
        row = self.query_one(
            "SELECT * FROM folders WHERE account = ? AND name = ?", (account, name)
        )
        if row is None:
            return None
        return FolderState(
            id=int(row["id"]),
            account=row["account"],
            name=row["name"],
            delimiter=row["delimiter"],
            uidvalidity=int(row["uidvalidity"] or 0),
            last_uid=int(row["last_uid"] or 0),
            last_sync_at=_parse_dt(row["last_sync_at"]),
            last_full_scan_at=_parse_dt(row["last_full_scan_at"]),
            message_count=int(row["message_count"] or 0),
            selectable=bool(row["selectable"]),
        )

    def list_folders(self, account: str | None = None) -> list[FolderState]:
        if account:
            rows = self.query(
                "SELECT * FROM folders WHERE account = ? ORDER BY name", (account,)
            )
        else:
            rows = self.query("SELECT * FROM folders ORDER BY account, name")
        return [
            FolderState(
                id=int(r["id"]),
                account=r["account"],
                name=r["name"],
                delimiter=r["delimiter"],
                uidvalidity=int(r["uidvalidity"] or 0),
                last_uid=int(r["last_uid"] or 0),
                last_sync_at=_parse_dt(r["last_sync_at"]),
                last_full_scan_at=_parse_dt(r["last_full_scan_at"]),
                message_count=int(r["message_count"] or 0),
                selectable=bool(r["selectable"]),
            )
            for r in rows
        ]

    def update_folder_state(
        self,
        account: str,
        name: str,
        *,
        uidvalidity: int | None = None,
        last_uid: int | None = None,
        last_sync_at: datetime | None = None,
        last_full_scan_at: datetime | None = None,
        message_count: int | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if uidvalidity is not None:
            sets.append("uidvalidity = ?")
            params.append(uidvalidity)
        if last_uid is not None:
            sets.append("last_uid = ?")
            params.append(last_uid)
        if last_sync_at is not None:
            sets.append("last_sync_at = ?")
            params.append(last_sync_at.isoformat())
        if last_full_scan_at is not None:
            sets.append("last_full_scan_at = ?")
            params.append(last_full_scan_at.isoformat())
        if message_count is not None:
            sets.append("message_count = ?")
            params.append(message_count)
        if not sets:
            return
        params.extend([account, name])
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE folders SET {', '.join(sets)} WHERE account = ? AND name = ?", params
            )

    def reset_folder_uidvalidity(self, account: str, name: str, new_uidvalidity: int) -> None:
        """UIDVALIDITY 变化说明服务端重建了 UID 空间，必须重新全量同步。"""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE folders SET uidvalidity = ?, last_uid = 0 WHERE account = ? AND name = ?",
                (new_uidvalidity, account, name),
            )

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------

    def message_exists(self, account: str, folder: str, uidvalidity: int, uid: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM messages WHERE account = ? AND folder = ? "
            "AND uidvalidity = ? AND uid = ? LIMIT 1",
            (account, folder, uidvalidity, uid),
        )
        return row is not None

    def find_by_message_id(self, account: str, message_id: str) -> MessageRecord | None:
        if not message_id:
            return None
        row = self.query_one(
            "SELECT * FROM messages WHERE account = ? AND message_id = ? "
            "ORDER BY id LIMIT 1",
            (account, message_id),
        )
        return self._row_to_message(row) if row else None

    def get_message(self, pk: int) -> MessageRecord | None:
        row = self.query_one("SELECT * FROM messages WHERE id = ?", (pk,))
        return self._row_to_message(row) if row else None

    def find_message_by_message_id(self, message_id: str) -> MessageRecord | None:
        row = self.query_one(
            "SELECT * FROM messages WHERE message_id = ? ORDER BY id LIMIT 1", (message_id,)
        )
        return self._row_to_message(row) if row else None

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            pk=int(row["id"]),
            account=row["account"],
            message_id=row["message_id"],
            uid=row["uid"],
            uidvalidity=int(row["uidvalidity"] or 0),
            folder=row["folder"],
            subject=row["subject"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            recipients=row["recipients"],
            cc=row["cc"],
            date_utc=_parse_dt(row["date_utc"]),
            date_raw=row["date_raw"],
            local_markdown_path=row["local_markdown_path"],
            body_text=row["body_text"],
            has_attachments=bool(row["has_attachments"]),
            size_bytes=int(row["size_bytes"] or 0),
            content_hash=row["content_hash"],
            duplicate_of=int(row["duplicate_of"]) if row["duplicate_of"] else None,
            synced_at=_parse_dt(row["synced_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            deleted_at=_parse_dt(row["deleted_at"]),
        )

    def insert_message(
        self,
        record: MessageRecord,
        attachments: Iterable[AttachmentMeta] = (),
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """插入邮件及其附件。返回 ``messages.id``。

        传入 ``conn`` 可复用外层事务（批量入库时显著更快）。
        """
        now = _now_iso()
        params = (
            record.account,
            record.message_id,
            record.uid,
            record.uidvalidity or 0,
            record.folder,
            record.subject,
            record.sender,
            record.sender_name,
            record.recipients,
            record.cc,
            record.date_utc.isoformat() if record.date_utc else None,
            record.date_raw,
            record.local_markdown_path,
            record.body_text,
            int(record.has_attachments),
            record.size_bytes,
            record.content_hash,
            record.duplicate_of,
            # 重复邮件的内容与原件一致，无需再走切片/嵌入
            now if record.duplicate_of else None,
            now,
            now,
        )
        sql = """
            INSERT INTO messages (
                account, message_id, uid, uidvalidity, folder, subject, sender, sender_name,
                recipients, cc, date_utc, date_raw, local_markdown_path, body_text,
                has_attachments, size_bytes, content_hash, duplicate_of, indexed_at,
                synced_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (account, folder, uidvalidity, uid) DO UPDATE SET
                subject             = excluded.subject,
                sender              = excluded.sender,
                recipients          = excluded.recipients,
                cc                  = excluded.cc,
                date_utc            = excluded.date_utc,
                local_markdown_path = excluded.local_markdown_path,
                body_text           = excluded.body_text,
                has_attachments     = excluded.has_attachments,
                size_bytes          = excluded.size_bytes,
                content_hash        = excluded.content_hash,
                duplicate_of        = excluded.duplicate_of,
                indexed_at          = excluded.indexed_at,
                updated_at          = excluded.updated_at,
                deleted_at          = NULL
            RETURNING id
        """
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
            pk = int(row["id"])
            self._replace_attachments(conn, pk, attachments, now)
            self._index_message(conn, pk, record)
            return pk

        with self.transaction() as own:
            row = own.execute(sql, params).fetchone()
            pk = int(row["id"])
            self._replace_attachments(own, pk, attachments, now)
            self._index_message(own, pk, record)
            return pk

    def _replace_attachments(
        self,
        conn: sqlite3.Connection,
        message_pk: int,
        attachments: Iterable[AttachmentMeta],
        now: str,
    ) -> None:
        conn.execute("DELETE FROM attachments WHERE message_pk = ?", (message_pk,))
        rows = [
            (
                message_pk,
                a.filename,
                a.content_type,
                a.size_bytes,
                a.content_id,
                int(a.is_inline),
                a.sha256,
                a.local_path,
                int(a.downloaded),
                a.skip_reason,
                a.part_index,
                now,
            )
            for a in attachments
        ]
        if rows:
            conn.executemany(
                """
                INSERT INTO attachments (
                    message_pk, filename, content_type, size_bytes, content_id, is_inline,
                    sha256, local_path, downloaded, skip_reason, part_index, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )

    def _index_message(
        self, conn: sqlite3.Connection, pk: int, record: MessageRecord
    ) -> None:
        """同步 FTS 索引（rowid 与 messages.id 对齐）。"""
        if not self._fts_available:
            return
        conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (pk,))
        conn.execute(
            "INSERT INTO messages_fts (rowid, subject, sender, recipients, body_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                pk,
                segment_cjk(record.subject or ""),
                segment_cjk(f"{record.sender_name} {record.sender}".strip()),
                segment_cjk(record.recipients or ""),
                segment_cjk(record.body_text or ""),
            ),
        )

    def mark_indexed(self, message_pk: int, content_hash: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE messages SET indexed_at = ?, content_hash = ? WHERE id = ?",
                (_now_iso(), content_hash, message_pk),
            )

    def count_pending_index(self) -> int:
        """待索引邮件数。不实例化 IndexService，供状态查询使用。"""
        row = self.query_one(
            "SELECT COUNT(*) AS c FROM messages "
            "WHERE deleted_at IS NULL AND (indexed_at IS NULL OR indexed_at < updated_at)"
        )
        return int(row["c"]) if row else 0

    def messages_pending_index(self, limit: int = 100) -> list[MessageRecord]:
        """需要生成切片/向量的邮件：从未索引，或正文哈希已变化。"""
        rows = self.query(
            """
            SELECT * FROM messages
            WHERE deleted_at IS NULL
              AND (indexed_at IS NULL OR indexed_at < updated_at)
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        )
        return [self._row_to_message(r) for r in rows]

    def soft_delete_missing(
        self, account: str, folder: str, keep_uids: set[str]
    ) -> int:
        """§11.2 全量比对：服务端已删除的邮件在本地标记为 deleted。"""
        rows = self.query(
            "SELECT id, uid FROM messages WHERE account = ? AND folder = ? "
            "AND deleted_at IS NULL",
            (account, folder),
        )
        missing = [int(r["id"]) for r in rows if str(r["uid"]) not in keep_uids]
        if not missing:
            return 0
        now = _now_iso()
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE messages SET deleted_at = ?, updated_at = ? WHERE id = ?",
                [(now, now, pk) for pk in missing],
            )
        logger.info("文件夹 %s 检测到 %d 封邮件已在服务端移除", folder, len(missing))
        return len(missing)

    def soft_delete_by_uid(self, account: str, folder: str, uid: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE messages SET deleted_at = ?, updated_at = ? "
                "WHERE account = ? AND folder = ? AND uid = ?",
                (_now_iso(), _now_iso(), account, folder, uid),
            )

    def update_message_path(self, pk: int, path: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE messages SET local_markdown_path = ?, updated_at = ? WHERE id = ?",
                (path, _now_iso(), pk),
            )

    def count_messages(self, *, include_deleted: bool = False) -> int:
        sql = "SELECT COUNT(*) AS c FROM messages"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        row = self.query_one(sql)
        return int(row["c"]) if row else 0

    def folder_statistics(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT account, folder,
                   COUNT(*) AS total,
                   SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) AS active,
                   MAX(date_utc) AS latest
            FROM messages GROUP BY account, folder ORDER BY account, folder
            """
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # attachments
    # ------------------------------------------------------------------

    def list_attachments(self, message_pk: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM attachments WHERE message_pk = ? ORDER BY part_index, id",
            (message_pk,),
        )

    def get_attachment(self, attachment_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))

    def attachment_by_hash(self, sha256: str) -> sqlite3.Row | None:
        """按内容哈希查找已下载文件，实现跨邮件附件去重。"""
        return self.query_one(
            "SELECT * FROM attachments WHERE sha256 = ? AND downloaded = 1 LIMIT 1",
            (sha256,),
        )

    # ------------------------------------------------------------------
    # sync_log
    # ------------------------------------------------------------------

    def start_sync_log(self, account: str, folder: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO sync_log (account, folder, started_at, status) "
                "VALUES (?, ?, ?, 'running')",
                (account, folder, _now_iso()),
            )
            return int(cur.lastrowid)

    def finish_sync_log(
        self, log_id: int, result: SyncResult, status: str | None = None
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE sync_log SET finished_at = ?, status = ?, fetched = ?, archived = ?,
                       skipped = ?, failed = ?, deleted = ?, error_summary = ?
                WHERE id = ?
                """,
                (
                    _now_iso(),
                    status or result.status,
                    result.fetched,
                    result.archived,
                    result.skipped,
                    result.failed,
                    result.deleted,
                    result.error_summary,
                    log_id,
                ),
            )

    def recent_sync_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def last_sync_summary(self) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # kb_chunks / kb_vectors
    # ------------------------------------------------------------------

    def replace_chunks(self, message_pk: int, chunks: Sequence[Chunk]) -> list[int]:
        """替换某封邮件的全部切片，返回新的 chunk id 列表。"""
        now = _now_iso()
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT id FROM kb_chunks WHERE message_pk = ?", (message_pk,)
            ).fetchall()
            old_ids = [int(r["id"]) for r in old]
            if old_ids:
                conn.execute("DELETE FROM kb_chunks WHERE message_pk = ?", (message_pk,))
                if self._fts_available:
                    conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?",
                                     [(i,) for i in old_ids])
            new_ids: list[int] = []
            for chunk in chunks:
                cur = conn.execute(
                    """
                    INSERT INTO kb_chunks (
                        message_pk, message_id, subject, sender, date_utc, folder, chunk_index,
                        text, token_count, content_hash, local_markdown_path, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        message_pk,
                        chunk.message_id,
                        chunk.subject,
                        chunk.sender,
                        chunk.date_utc.isoformat() if chunk.date_utc else None,
                        chunk.folder,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.token_count,
                        chunk.content_hash or chunk.compute_hash(),
                        chunk.local_markdown_path,
                        now,
                    ),
                )
                cid = int(cur.lastrowid)
                new_ids.append(cid)
                if self._fts_available:
                    conn.execute(
                        "INSERT INTO chunks_fts (rowid, text, subject, sender) VALUES (?,?,?,?)",
                        (
                            cid,
                            segment_cjk(chunk.text),
                            segment_cjk(chunk.subject or ""),
                            segment_cjk(chunk.sender or ""),
                        ),
                    )
            return new_ids

    def get_chunks(self, message_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM kb_chunks WHERE message_id = ? ORDER BY message_pk, chunk_index",
            (message_id,),
        )
        return [dict(r) for r in rows]

    def get_chunks_by_pk(self, message_pk: int) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM kb_chunks WHERE message_pk = ? ORDER BY chunk_index", (message_pk,)
        )
        return [dict(r) for r in rows]

    def get_chunk(self, chunk_id: int) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM kb_chunks WHERE id = ?", (chunk_id,))
        return dict(row) if row else None

    def count_chunks(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS c FROM kb_chunks")
        return int(row["c"]) if row else 0

    def delete_chunks_for_message(self, message_pk: int) -> list[int]:
        chunk_ids = [
            int(r["id"])
            for r in self.query("SELECT id FROM kb_chunks WHERE message_pk = ?", (message_pk,))
        ]
        with self.transaction() as conn:
            if chunk_ids:
                conn.execute("DELETE FROM kb_chunks WHERE message_pk = ?", (message_pk,))
                if self._fts_available:
                    conn.executemany(
                        "DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in chunk_ids]
                    )
        return chunk_ids

    def record_vector(
        self,
        chunk_id: int,
        *,
        message_id: str,
        model: str,
        dimension: int,
        backend: str,
        vector_ref: str,
        content_hash: str = "",
        vector_blob: bytes | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO kb_vectors (chunk_id, message_id, model, dimension, backend,
                                        vector_ref, content_hash, vector_blob, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT (chunk_id, model) DO UPDATE SET
                    dimension = excluded.dimension,
                    backend = excluded.backend,
                    vector_ref = excluded.vector_ref,
                    content_hash = excluded.content_hash,
                    vector_blob = excluded.vector_blob,
                    created_at = excluded.created_at
                """,
                (chunk_id, message_id, model, dimension, backend, vector_ref,
                 content_hash, vector_blob, _now_iso()),
            )

    def delete_vectors_for_chunks(self, chunk_ids: Sequence[int]) -> None:
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        with self.transaction() as conn:
            conn.execute(
                f"DELETE FROM kb_vectors WHERE chunk_id IN ({placeholders})", list(chunk_ids)
            )

    def vectors_for_message(self, message_id: str) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM kb_vectors WHERE message_id = ?", (message_id,))
        return [dict(r) for r in rows]

    def count_vectors(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS c FROM kb_vectors")
        return int(row["c"]) if row else 0

    def clear_vector_records(self) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM kb_vectors")

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def rebuild_fts(self) -> int:
        """重建全部 FTS 索引（数据修复 / 迁移后使用）。"""
        if not self._fts_available:
            return 0
        count = 0
        with self.transaction() as conn:
            conn.execute("DELETE FROM messages_fts")
            conn.execute("DELETE FROM chunks_fts")
            for row in conn.execute(
                "SELECT id, subject, sender, sender_name, recipients, body_text FROM messages"
            ).fetchall():
                conn.execute(
                    "INSERT INTO messages_fts (rowid, subject, sender, recipients, body_text) "
                    "VALUES (?,?,?,?,?)",
                    (
                        int(row["id"]),
                        segment_cjk(row["subject"] or ""),
                        segment_cjk(f"{row['sender_name']} {row['sender']}".strip()),
                        segment_cjk(row["recipients"] or ""),
                        segment_cjk(row["body_text"] or ""),
                    ),
                )
                count += 1
            for row in conn.execute(
                "SELECT id, text, subject, sender FROM kb_chunks"
            ).fetchall():
                conn.execute(
                    "INSERT INTO chunks_fts (rowid, text, subject, sender) VALUES (?,?,?,?)",
                    (
                        int(row["id"]),
                        segment_cjk(row["text"] or ""),
                        segment_cjk(row["subject"] or ""),
                        segment_cjk(row["sender"] or ""),
                    ),
                )
        logger.info("FTS 索引已重建，共 %d 封邮件", count)
        return count

    def optimize(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            logger.debug("optimize 失败", exc_info=True)

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def integrity_check(self) -> str:
        row = self.query_one("PRAGMA integrity_check")
        return str(row[0]) if row else "unknown"
