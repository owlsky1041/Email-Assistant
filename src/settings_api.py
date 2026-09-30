"""设置界面 API（读写在本地回环上的配置管理接口）。

安全模型
--------
设置接口能**改写配置并写入授权码**，比只读的知识库接口危险得多。
一个恶意网页只要能让浏览器向 ``127.0.0.1:8990`` 发请求，就可能改配置、
甚至把数据目录指向别处。因此这里叠加三重防护：

1. **强制回环**：一旦 ``api.host`` 不是回环地址，直接拒绝注册设置接口
   （而不是仅告警）。用户在公网暴露知识库 API 时，设置能力必须一并消失。
2. **一次性令牌**：进程启动时随机生成，注入到 ``/setup`` 页面。
   跨站脚本读不到响应体（CORS），因此拿不到令牌；令牌还随进程退出而失效。
3. **同源校验**：拒绝带跨站 ``Origin`` 或 ``Sec-Fetch-Site: cross-site``
   的请求，阻断 CSRF（表单提交这类"简单请求"不会带自定义头，
   光靠令牌已能拦住，同源校验是第二道）。

授权码永远只写不读。
"""

from __future__ import annotations

import logging
import secrets
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from .config import AppConfig
from .context import AppContext
from .settings_service import SettingsError, SettingsService

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
SETUP_TOKEN_HEADER = "x-setup-token"

#: 设置页面的 HTML 模板（打包时随 src/webui 一起收集）
WEBUI_DIR = Path(__file__).resolve().parent / "webui"


def is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def load_setup_html(token: str) -> str:
    """读取设置页并注入令牌。"""
    page = WEBUI_DIR / "settings.html"
    if not page.is_file():
        raise FileNotFoundError(f"缺少设置页面文件：{page}")
    html = page.read_text(encoding="utf-8")
    return html.replace("__SETUP_TOKEN__", token)


def create_settings_router(  # noqa: C901 - 路由集中一处便于审阅
    context: AppContext,
    *,
    token: str | None = None,
    allow_non_loopback: bool = False,
) -> APIRouter:
    """构建设置路由。

    :param allow_non_loopback: 仅供测试使用，生产环境下非回环监听会直接抛错。
    """
    config: AppConfig = context.config
    if not allow_non_loopback and not is_loopback(config.api.host):
        raise SettingsError(
            f"拒绝在非回环地址 {config.api.host} 上提供设置接口："
            "该接口可改写配置与授权码，只能监听 127.0.0.1。"
        )

    setup_token = token or secrets.token_urlsafe(32)
    service = SettingsService(context.config)
    router = APIRouter(tags=["设置"])

    # ---- 防护 --------------------------------------------------------

    def _guard(request: Request) -> None:
        """同源校验 + 令牌校验。"""
        # 同源校验：阻断 CSRF
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            allowed = {f"http://{host}", f"https://{host}"}
            if origin not in allowed:
                logger.warning("拒绝跨站设置请求：Origin=%s", origin)
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="拒绝跨站请求")
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="拒绝跨站请求")

        # 令牌校验（用 compare_digest 防时序侧信道）
        supplied = request.headers.get(SETUP_TOKEN_HEADER, "")
        if not supplied or not secrets.compare_digest(supplied, setup_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="缺少或无效的设置令牌。请从托盘菜单或 `main.py settings` 重新打开设置界面。",
            )

    # ---- 页面 --------------------------------------------------------

    @router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_page() -> HTMLResponse:
        """设置页面本身不校验令牌。

        页面里会带上令牌，而跨站脚本因同源策略读不到响应体，
        所以拿不到它；任何**写操作**仍然必须携带令牌。
        """
        try:
            return HTMLResponse(load_setup_html(setup_token))
        except FileNotFoundError as exc:
            return HTMLResponse(
                f"<h1>设置页面缺失</h1><pre>{exc}</pre>", status_code=500
            )

    # ---- 读写 --------------------------------------------------------

    @router.get("/api/settings", summary="读取当前设置（不含任何密钥）")
    def get_settings(request: Request) -> dict[str, Any]:
        _guard(request)
        return service.read()

    @router.put("/api/settings", summary="保存设置")
    def put_settings(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _guard(request)
        try:
            changed = service.write(payload)
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "changed": changed, "settings": service.read()}

    @router.put("/api/settings/auth-code", summary="保存授权码（只写不读）")
    def put_auth_code(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _guard(request)
        code = str(payload.get("code") or "")
        try:
            result = service.set_auth_code(code)
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # 只回状态，绝不回显授权码
        return {"ok": True, "backend": result["backend"], "configured": True}

    @router.delete("/api/settings/auth-code", summary="清除授权码")
    def delete_auth_code(request: Request) -> dict[str, Any]:
        _guard(request)
        removed = service.clear_auth_code()
        return {"ok": True, "removed": removed}

    # ---- 同步进度与触发 ----------------------------------------------

    @router.get("/api/sync/progress", summary="同步进度（供界面轮询）")
    def sync_progress(request: Request, event_limit: int = 60) -> dict[str, Any]:
        _guard(request)
        snap = context.progress.snapshot(event_limit=max(0, min(event_limit, 200)))
        stats = context.db.count_messages()
        snap["total_messages"] = stats
        snap["pending_index"] = context.db.count_pending_index()
        snap["chunks"] = context.db.count_chunks()
        return snap

    @router.post("/api/sync/start", summary="开始同步")
    def sync_start(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        _guard(request)
        if context.sync_running:
            return {"accepted": False, "detail": "同步已在进行中"}
        folders = payload.get("folders") or None
        full = bool(payload.get("full"))
        # 同步跑在后台线程：设置界面立刻返回并转为轮询进度
        thread = threading.Thread(
            target=context.sync.sync_all,
            kwargs={"folders": folders, "full": full, "index": True},
            name="ui-sync",
            daemon=True,
        )
        thread.start()
        return {"accepted": True, "detail": "同步已开始"}

    # ---- 连接测试 ----------------------------------------------------

    @router.post("/api/settings/test-connection", summary="测试 IMAP 连接")
    def test_connection(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        _guard(request)
        # 允许用界面上刚输入、尚未保存的授权码做一次性测试
        auth_code = payload.get("auth_code")
        return service.test_connection(auth_code=str(auth_code) if auth_code else None)

    @router.get("/api/settings/folders", summary="列出邮箱文件夹")
    def list_folders(request: Request) -> dict[str, Any]:
        _guard(request)
        result = service.test_connection()
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "连接失败"))
        return {"folders": result.get("folders", [])}

    # ---- 目录 --------------------------------------------------------

    @router.post("/api/settings/pick-directory", summary="弹出目录选择框")
    def pick_directory(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        _guard(request)
        return service.pick_directory(initial=str(payload.get("initial") or ""))

    @router.post("/api/settings/open-path", summary="在文件管理器中打开目录")
    def open_path(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        _guard(request)
        return service.open_path(str(payload.get("path") or ""))

    @router.post("/api/settings/migrate", summary="迁移现有数据到新目录")
    def migrate(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _guard(request)
        new_root = str(payload.get("new_root") or "").strip()
        if not new_root:
            raise HTTPException(status_code=422, detail="请提供 new_root")
        try:
            result = service.migrate_data(new_root)
        except (SettingsError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not result.get("ok"):
            raise HTTPException(status_code=422, detail=result.get("error", "迁移失败"))
        return result

    # 让外部（托盘 / CLI）能拿到令牌
    router.setup_token = setup_token  # type: ignore[attr-defined]
    return router


def register_settings(app: Any, context: AppContext, *, token: str | None = None) -> str | None:
    """把设置路由挂到已有 FastAPI 应用上。

    非回环监听时**跳过注册**并返回 ``None``（而不是让整个服务启动失败）。
    """
    config: AppConfig = context.config
    if not is_loopback(config.api.host):
        logger.warning(
            "API 监听在非回环地址 %s，已停用设置界面（该接口可改写配置与授权码）",
            config.api.host,
        )
        return None
    try:
        router = create_settings_router(context, token=token)
    except SettingsError as exc:
        logger.warning("设置界面不可用：%s", exc)
        return None
    app.include_router(router)
    return getattr(router, "setup_token", None)


def unauthorized_response() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "缺少设置令牌"})
