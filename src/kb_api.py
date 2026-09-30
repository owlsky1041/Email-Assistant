"""知识库本地 API（§3.8 / §5）。

安全约束
--------
* 默认只监听 ``127.0.0.1``（§8）；
* 支持 Bearer Token 鉴权；
* **不返回**授权码、密码、原始 HTML 及任何凭据；
* 触发同步在独立工作线程执行，不阻塞 API 事件循环（§11.1）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import AppConfig, resolve_api_token
from .context import AppContext
from .markdown_exporter import MarkdownExporter
from .search import SearchFilters

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="检索语句")
    mode: Literal["hybrid", "keyword", "vector"] = "hybrid"
    limit: int = Field(20, ge=1, le=100)
    folder: list[str] | None = None
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    has_attachments: bool | None = None
    snippet_length: int = Field(200, ge=40, le=2000)


class EmbedRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=128)
    normalize: bool = True


class SyncTriggerRequest(BaseModel):
    folders: list[str] | None = None
    full: bool = False
    index: bool = True


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(context: AppContext) -> FastAPI:
    config = context.config
    token = resolve_api_token(config)

    app = FastAPI(
        title="邮件知识库 API",
        description=(
            "腾讯企业邮箱邮件管理助手的本地只读检索接口。\n\n"
            "仅监听回环地址，供本机智能体 / RAG 系统调用。"
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.context = context
    app.state.sync_lock = threading.Lock()
    app.state.sync_thread = None

    # 设置界面（可改写配置与授权码）。仅在回环监听时注册，
    # 且所有写操作都要求一次性令牌 —— 详见 settings_api 的模块说明。
    from .settings_api import register_settings

    app.state.setup_token = register_settings(app, context)

    if config.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    # ---- 鉴权 --------------------------------------------------------

    async def require_token(request: Request) -> None:
        if not token:
            return
        header = request.headers.get("authorization", "")
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "bearer" or credentials.strip() != token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="缺少或无效的 Authorization: Bearer <token>",
                headers={"WWW-Authenticate": "Bearer"},
            )

    guarded = [Depends(require_token)]

    # ---- 异常处理 ----------------------------------------------------

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("API 处理 %s %s 时出错", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"内部错误：{type(exc).__name__}"},
        )

    # ---- 健康检查 ----------------------------------------------------

    @app.get("/api/health", tags=["系统"], summary="健康检查")
    def health() -> dict[str, Any]:
        ctx: AppContext = app.state.context
        db_state = ctx.db.schema_summary()
        payload: dict[str, Any] = {
            "status": "ok",
            "version": app.version,
            "database": {
                "schema_version": db_state["schema_version"],
                "journal_mode": db_state["journal_mode"],
                "fts_available": db_state["fts_available"],
            },
            "counts": {
                "messages": ctx.db.count_messages(),
                "chunks": ctx.db.count_chunks(),
                "vectors": ctx.db.count_vectors(),
            },
            "sync_running": ctx.sync_running,
        }
        # 只有真正初始化过才报告嵌入/向量后端，避免健康检查触发模型加载
        if ctx._embedder is not None:
            payload["embedding"] = ctx._embedder.health()
        if ctx._vector_store is not None:
            payload["vector_store"] = ctx._vector_store.health()
        return payload

    # ---- 同步 --------------------------------------------------------

    @app.get("/api/sync/status", tags=["同步"], summary="查询同步状态",
             dependencies=guarded)
    def sync_status() -> dict[str, Any]:
        ctx: AppContext = app.state.context
        return ctx.sync.status()

    @app.post("/api/sync/trigger", tags=["同步"], summary="手动触发同步",
              dependencies=guarded)
    def sync_trigger(payload: SyncTriggerRequest | None = None) -> dict[str, Any]:
        ctx: AppContext = app.state.context

        # §11.1：同步在后台线程执行，API 立即返回
        with app.state.sync_lock:
            thread = app.state.sync_thread
            if thread is not None and thread.is_alive():
                return {"accepted": False, "detail": "同步已在进行中"}
            options = payload or SyncTriggerRequest()

            def _run() -> None:
                ctx.sync.sync_all(
                    folders=options.folders, full=options.full, index=options.index
                )

            thread = threading.Thread(target=_run, name="api-sync", daemon=True)
            app.state.sync_thread = thread
            thread.start()

        return {
            "accepted": True,
            "detail": "同步任务已启动，可通过 GET /api/sync/status 查询进度",
        }

    # ---- 邮件 --------------------------------------------------------

    @app.get("/api/messages", tags=["邮件"], summary="检索邮件列表",
             dependencies=guarded)
    def list_messages(
        folder: list[str] | None = Query(None, description="文件夹，可重复"),
        sender: str | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        has_attachments: bool | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        order: Literal["date_desc", "date_asc", "subject", "sender"] = "date_desc",
    ) -> dict[str, Any]:
        ctx: AppContext = app.state.context
        filters = SearchFilters(
            folder=folder,
            sender=sender,
            recipient=recipient,
            subject=subject,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
        )
        rows, total = ctx.search.list_messages(
            filters=filters, limit=limit, offset=offset, order=order
        )
        return {"total": total, "limit": limit, "offset": offset, "items": rows}

    @app.get("/api/messages/{message_ref}", tags=["邮件"], summary="获取单封邮件元数据",
             dependencies=guarded)
    def get_message(message_ref: str) -> dict[str, Any]:
        ctx: AppContext = app.state.context
        record = _resolve_message(ctx, message_ref)
        attachments = [
            _attachment_payload(dict(row)) for row in ctx.db.list_attachments(record.pk or 0)
        ]
        return {
            "id": record.pk,
            "message_id": record.message_id,
            "uid": record.uid,
            "folder": record.folder,
            "subject": record.subject,
            "from": record.sender,
            "from_name": record.sender_name,
            "to": record.recipients,
            "cc": record.cc,
            "date": record.date_utc.isoformat() if record.date_utc else None,
            "local_markdown_path": record.local_markdown_path,
            "has_attachments": record.has_attachments,
            "size_bytes": record.size_bytes,
            "synced_at": record.synced_at.isoformat() if record.synced_at else None,
            "chunk_count": len(ctx.db.get_chunks_by_pk(record.pk or 0)),
            "attachments": attachments,
        }

    @app.get("/api/messages/{message_ref}/content", tags=["邮件"],
             summary="获取邮件 Markdown 正文", dependencies=guarded)
    def get_message_content(
        message_ref: str,
        include_frontmatter: bool = Query(False, description="是否包含 YAML frontmatter"),
        fmt: Literal["markdown", "text"] = Query("markdown"),
    ) -> Any:
        ctx: AppContext = app.state.context
        record = _resolve_message(ctx, message_ref)
        path = Path(record.local_markdown_path) if record.local_markdown_path else None

        if path and path.is_file():
            meta, body = MarkdownExporter.read_markdown(path)
            if include_frontmatter:
                import yaml

                body = f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()}\n---\n\n{body}"
        elif fmt == "text":
            return PlainTextResponse(record.body_text or "")
        else:
            return PlainTextResponse(
                f"# {record.subject}\n\n{record.body_text or '(无正文)'}",
                media_type="text/markdown",
            )

        if fmt == "text":
            return PlainTextResponse(body, media_type="text/plain; charset=utf-8")
        return PlainTextResponse(body, media_type="text/markdown; charset=utf-8")

    # ---- 附件 --------------------------------------------------------

    @app.get("/api/attachments/{attachment_id}", tags=["附件"], summary="获取附件本地路径",
             dependencies=guarded)
    def get_attachment(attachment_id: int) -> dict[str, Any]:
        ctx: AppContext = app.state.context
        row = ctx.db.get_attachment(attachment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="附件不存在")
        return _attachment_payload(dict(row))

    # ---- 检索 --------------------------------------------------------

    @app.post("/api/search", tags=["检索"], summary="关键词 + 向量混合检索",
              dependencies=guarded)
    def search(payload: SearchRequest) -> dict[str, Any]:
        ctx: AppContext = app.state.context
        filters = SearchFilters(
            folder=payload.folder,
            sender=payload.sender,
            recipient=payload.recipient,
            subject=payload.subject,
            date_from=payload.date_from,
            date_to=payload.date_to,
            has_attachments=payload.has_attachments,
        )
        hits = ctx.search.search(
            payload.query,
            limit=payload.limit,
            mode=payload.mode,
            filters=filters,
            snippet_length=payload.snippet_length,
        )
        return {
            "query": payload.query,
            "mode": payload.mode,
            "count": len(hits),
            "results": [hit.to_dict() for hit in hits],
        }

    # ---- 向量化 ------------------------------------------------------

    @app.post("/api/embed", tags=["向量"], summary="文本向量化", dependencies=guarded)
    def embed(payload: EmbedRequest) -> dict[str, Any]:
        ctx: AppContext = app.state.context
        try:
            vectors = ctx.embedder.embed(payload.texts)
        except Exception as exc:  # noqa: BLE001
            logger.error("向量化失败：%s", exc)
            raise HTTPException(status_code=503, detail=f"嵌入后端不可用：{exc}") from exc

        if not payload.normalize:
            import math

            vectors = [
                [v / (math.sqrt(sum(x * x for x in vec)) or 1.0) for v in vec]
                for vec in vectors
            ]
        return {
            "model": getattr(ctx.embedder, "name", "unknown"),
            "dimension": ctx.embedder.dimension,
            "count": len(vectors),
            "vectors": vectors,
        }

    # ---- 切片 --------------------------------------------------------

    @app.get("/api/chunks/{message_id:path}", tags=["切片"], summary="获取邮件切片",
             dependencies=guarded)
    def get_chunks(
        message_id: str,
        limit: int = Query(200, ge=1, le=2000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        ctx: AppContext = app.state.context
        rows = ctx.db.get_chunks(message_id)
        window = rows[offset : offset + limit]
        items = [
            {
                "chunk_id": row["id"],
                "message_id": row["message_id"],
                "chunk_index": row["chunk_index"],
                "text": row["text"],
                "token_count": row["token_count"],
                "subject": row["subject"],
                "sender": row["sender"],
                "date": row["date_utc"],
                "folder": row["folder"],
                "local_markdown_path": row["local_markdown_path"],
            }
            for row in window
        ]
        return {"message_id": message_id, "total": len(rows), "items": items}

    @app.get("/api/stats", tags=["系统"], summary="知识库统计", dependencies=guarded)
    def stats() -> dict[str, Any]:
        ctx: AppContext = app.state.context
        data = ctx.search.statistics()
        data["folders"] = ctx.db.folder_statistics()
        return data

    return app


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _resolve_message(context: AppContext, message_ref: str):
    """接受 ``messages.id`` 或 ``Message-ID`` 两种引用方式。"""
    reference = message_ref.strip()
    if reference.isdigit():
        record = context.db.get_message(int(reference))
        if record is not None:
            return record
    record = context.db.find_message_by_message_id(reference.strip("<>"))
    if record is None:
        record = context.db.find_message_by_message_id(reference)
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到邮件：{message_ref}")
    return record


def _attachment_payload(row: dict[str, Any]) -> dict[str, Any]:
    path = row.get("local_path")
    exists = bool(path) and Path(path).is_file()
    size_on_disk = Path(path).stat().st_size if exists else None
    return {
        "id": row.get("id"),
        "message_pk": row.get("message_pk"),
        "filename": row.get("filename"),
        "content_type": row.get("content_type"),
        "size_bytes": row.get("size_bytes"),
        "local_path": path,
        "exists": exists,
        "size_on_disk": size_on_disk,
        "sha256": row.get("sha256"),
        "is_inline": bool(row.get("is_inline")),
        "downloaded": bool(row.get("downloaded")),
        "skip_reason": row.get("skip_reason"),
    }


def check_bind_address(host: str) -> str | None:
    """返回非 ``None`` 表示这是一个需要告警的绑定地址（§8）。"""
    if host in LOOPBACK_HOSTS:
        return None
    return (
        f"⚠️  API 正在监听 {host}，非回环地址意味着同一网络内的其他主机可以访问你的邮件索引。"
        "生产环境请改回 127.0.0.1（配置项 api.host）。"
    )


def serve(context: AppContext, *, host: str | None = None, port: int | None = None) -> None:
    """启动 API 服务（阻塞）。"""
    import uvicorn

    config: AppConfig = context.config
    bind_host = host or config.api.host
    bind_port = port or config.api.port

    warning = check_bind_address(bind_host)
    if warning:
        logger.warning(warning)
        print(warning)

    if not resolve_api_token(config):
        logger.info("API 未设置访问令牌（仅回环地址可访问）")

    context.warmup()

    app = create_app(context)
    uvicorn.run(
        app,
        host=bind_host,
        port=bind_port,
        log_level=str(config.log.level).lower(),
        access_log=False,
        timeout_keep_alive=config.api.request_timeout,
    )
