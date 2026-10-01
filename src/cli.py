"""命令行接口。

::

    python main.py init                 # 交互式配置向导
    python main.py auth set             # 安全写入授权码
    python main.py doctor               # 环境自检
    python main.py sync                 # 手动同步
    python main.py search "报销发票"    # 混合检索
    python main.py serve                # 启动知识库 API
    python main.py tray                 # 托盘常驻
    python main.py demo                 # 生成演示数据（无需邮箱）
    python main.py model status         # 查看嵌入模型状态
    python main.py settings             # 可视化设置界面
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .context import AppContext
from .logging_setup import setup_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------

def _force_utf8_console() -> None:
    """让标准输出使用 UTF-8。

    Windows 控制台默认使用区域代码页（英文系统是 cp1252/cp437），
    此时 ``print("中文")`` 会直接抛 ``UnicodeEncodeError``，
    整个命令崩掉。Python 3.7+ 的 ``reconfigure`` 可避免要求用户
    先手动执行 ``chcp 65001``。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _supports_color() -> bool:
    return sys.stdout.isatty() and sys.platform != "win32"


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _supports_color() else text


def info(message: str) -> None:
    print(message, flush=True)


def ok(message: str) -> None:
    print(_c("✓ ", "32") + message, flush=True)


def warn(message: str) -> None:
    print(_c("! ", "33") + message, flush=True)


def fail(message: str) -> None:
    print(_c("✗ ", "31") + message, file=sys.stderr, flush=True)


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str), flush=True)


def _console_level(args: argparse.Namespace) -> str | None:
    if getattr(args, "verbose", False):
        return "DEBUG"
    return getattr(args, "log_level", None)


def _load_context(args: argparse.Namespace, *, quiet: bool = False) -> AppContext:
    from .config import load_config

    config = load_config(getattr(args, "config", None))
    level = _console_level(args)
    if level:
        config.log.level = level
    context = AppContext(config, configure_logging=not quiet)
    if quiet:
        setup_logging(config, force=True)
    return context


# ---------------------------------------------------------------------------
# init / auth
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    from .config_writer import config_file_path, write_default_config

    path = write_default_config(args.config, overwrite=args.force)
    ok(f"配置文件：{path}")

    if args.non_interactive or not sys.stdin.isatty():
        info("")
        info("下一步：")
        info("  1) 编辑上面的配置文件，填写 email.address")
        info("  2) 执行 `python main.py auth set` 写入授权码")
        info("  3) 执行 `python main.py doctor` 检查环境")
        info("  4) 执行 `python main.py sync` 开始首次同步")
        return 0

    info("")
    info(_c("— 配置向导 —", "36"))
    address = input("邮箱地址（留空跳过）：").strip()
    if address:
        from .config_writer import update_config

        update_config({"email": {"address": address}}, args.config)
        ok(f"已写入邮箱地址：{address}")

    answer = input("现在设置授权码吗？(y/N)：").strip().lower()
    if answer == "y":
        _set_auth_code(args)
    info("")
    info("完成。执行 `python main.py doctor` 做一次自检。")
    return 0


def _set_auth_code(args: argparse.Namespace) -> int:
    from .config import load_config
    from .secret_store import SecretStore, SecretStoreError

    config = load_config(getattr(args, "config", None))
    if not config.email.address:
        warn("尚未配置邮箱地址（email.address），授权码仍可保存")

    info("提示：授权码可在 腾讯企业邮箱 → 设置 → 客户端设置 中生成。")
    info("      输入过程不会回显。")
    code = getpass.getpass("授权码：").strip()
    if args.auth_code:
        code = args.auth_code.strip()
    if not code:
        fail("授权码为空，已取消")
        return 1

    try:
        backend = SecretStore(config).set(config.email.auth_code_ref, code)
    except SecretStoreError as exc:
        fail(str(exc))
        return 1

    ok(f"授权码已保存（存储后端：{backend}）")
    if backend == "encrypted-file":
        warn("本地加密文件的密钥与密文同机存放，仅防误读。建议改用环境变量：")
        warn(f"  export {config.email.auth_code_env}='<授权码>'")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    from .config import load_config, resolve_auth_code
    from .secret_store import SecretStore

    config = load_config(getattr(args, "config", None))
    action = args.auth_action

    if action == "set":
        return _set_auth_code(args)

    if action == "clear":
        removed = SecretStore(config).delete(config.email.auth_code_ref)
        ok("授权码已删除" if removed else "未找到已保存的授权码")
        return 0

    if action == "status":
        backend = SecretStore(config).backend_name(config.email.auth_code_ref)
        code = resolve_auth_code(config)
        if code:
            ok(f"授权码可用（来源：{backend}，长度 {len(code)}）")
            return 0
        warn("未配置授权码")
        info(f"  请执行 `python main.py auth set`，或设置环境变量 {config.email.auth_code_env}")
        return 1

    if action == "show":
        code = resolve_auth_code(config)
        if not code:
            fail("未配置授权码")
            return 1
        # 仅在显式要求时输出，且只显示前后各 2 位
        masked = f"{code[:2]}…{code[-2:]}" if len(code) > 6 else "***"
        info(f"授权码（脱敏）：{masked}")
        return 0

    fail(f"未知 auth 子命令：{action}")
    return 2


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    """环境与配置自检（§7）。"""
    import platform

    from .config import load_config, resolve_api_token, resolve_auth_code
    from .config_writer import config_file_path
    from .embedder import create_embedder
    from .secret_store import SecretStore

    results: dict[str, Any] = {"checks": [], "ok": True}
    # --json 时必须只输出 JSON，否则管道/脚本无法解析
    quiet = bool(getattr(args, "json", False))

    def record(
        name: str, passed: bool, detail: str = "", hint: str = "", *, warn_only: bool = False
    ) -> None:
        results["checks"].append(
            {
                "name": name,
                "ok": passed,
                "warn_only": warn_only,
                "detail": detail,
                "hint": hint,
            }
        )
        if not passed and not warn_only:
            results["ok"] = False
        if quiet:
            return
        if passed:
            ok(f"{name}" + (f" —— {detail}" if detail else ""))
        elif warn_only:
            warn(f"{name}" + (f" —— {detail}" if detail else ""))
        else:
            fail(f"{name}" + (f" —— {detail}" if detail else ""))
        if not passed and hint:
            info(f"    ↳ {hint}")

    if not quiet:
        info(_c("— 环境自检 —", "36"))
    record(
        "Python 版本",
        sys.version_info >= (3, 10),
        f"{platform.python_version()} on {platform.system()}",
        "需要 Python 3.10 或更高版本",
    )

    # 依赖
    for module, hint in (
        ("yaml", "pip install PyYAML"),
        ("pydantic", "pip install pydantic"),
        ("imap_tools", "pip install imap-tools"),
        ("bs4", "pip install beautifulsoup4"),
        ("markdownify", "pip install markdownify"),
        ("fastapi", "pip install fastapi uvicorn"),
        ("apscheduler", "pip install APScheduler"),
        ("cryptography", "pip install cryptography"),
    ):
        try:
            __import__(module)
            record(f"依赖 {module}", True)
        except ImportError:
            record(f"依赖 {module}", False, "未安装", hint)

    # 可选依赖（缺失只降级，不算失败）
    for module, purpose in (
        ("onnxruntime", "ONNX 嵌入后端（推荐）"),
        ("tokenizers", "ONNX 分词器"),
        ("chromadb", "ChromaDB 向量库"),
        ("pystray", "系统托盘"),
        ("PIL", "托盘图标绘制"),
    ):
        try:
            __import__(module)
            record(f"可选依赖 {module}", True, purpose)
        except Exception as exc:  # noqa: BLE001
            # 必须捕获所有异常而非只捕 ImportError：
            # 无显示环境下 `import pystray` 抛的是 Xlib.error.DisplayNameError，
            # 只捕 ImportError 会让 doctor 在无头服务器上直接崩溃。
            reason = str(exc) or type(exc).__name__
            record(
                f"可选依赖 {module}",
                False,
                f"不可用：{type(exc).__name__}: {reason}"[:160] + f"（{purpose}）",
                "pip install -r requirements-optional.txt",
                warn_only=True,
            )

    # 配置文件
    config_path = config_file_path(getattr(args, "config", None))
    record("配置文件", config_path.is_file(), str(config_path), "执行 `python main.py init`")
    if not config_path.is_file():
        _print_json(results)
        return 1

    config = load_config(config_path)
    record("邮箱地址已配置", bool(config.email.address),
           config.email.address or "未填写", "编辑配置文件的 email.address")

    backend = SecretStore(config).backend_name(config.email.auth_code_ref)
    auth_code = resolve_auth_code(config)
    record(
        "授权码已配置",
        bool(auth_code),
        f"来源：{backend}",
        "执行 `python main.py auth set`",
    )

    # 目录可写
    for label, path in (
        ("归档目录", config.archive_path),
        ("数据库目录", config.sqlite_file.parent),
        ("日志目录", config.log_path),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            record(f"{label}可写", True, str(path))
        except OSError as exc:
            record(f"{label}可写", False, str(exc), f"检查路径权限：{path}")

    # 数据库
    try:
        from .database import Database

        db = Database(config.sqlite_file)
        db.initialize()
        summary = db.schema_summary()
        record(
            "SQLite 可用",
            True,
            f"schema v{summary['schema_version']}，journal={summary['journal_mode']}",
        )
        record(
            "FTS5 全文检索",
            bool(summary["fts_available"]),
            "可用" if summary["fts_available"] else "不可用，关键词检索将退化为 LIKE",
        )
        record("数据库完整性", db.integrity_check() == "ok", db.integrity_check())
    except Exception as exc:  # noqa: BLE001
        record("数据库", False, str(exc))

    # 嵌入后端（缺失时仅告警：关键词检索仍可正常工作）
    try:
        embedder = create_embedder(config)
        record(
            "嵌入后端",
            embedder.name != "hashing",
            f"{embedder.name} / {embedder.dimension} 维",
            "安装 requirements-optional.txt 并导出 ONNX 模型以启用语义检索：\n"
            f"      optimum-cli export onnx --model {config.embedding.model} {config.model_path}",
            warn_only=True,
        )
        if args.check_model:
            vector = embedder.embed(["自检文本"])
            record("嵌入推理", bool(vector and vector[0]), f"输出 {len(vector[0])} 维")
    except Exception as exc:  # noqa: BLE001
        record("嵌入后端", False, str(exc), warn_only=True)

    # API 端口
    if resolve_api_token(config):
        record("API 鉴权", True, "已设置访问令牌")
    else:
        record("API 鉴权", True, "未设置（仅回环地址可达）")

    if config.api.host not in ("127.0.0.1", "::1", "localhost"):
        record(
            "API 监听地址",
            False,
            f"{config.api.host} 不是回环地址",
            "§8 安全要求：请改回 127.0.0.1",
        )
    else:
        record("API 监听地址", True, config.api.host)

    # IMAP 连通性
    if args.check_imap and config.email.address and auth_code:
        try:
            from .sync_service import SyncService
            from .database import Database

            db = Database(config.sqlite_file)
            db.initialize()
            service = SyncService(config, db)
            result = service.test_connection()
            record(
                "IMAP 连通性",
                True,
                f"{result['server']}，{result['folder_count']} 个文件夹",
            )
        except Exception as exc:  # noqa: BLE001
            record("IMAP 连通性", False, str(exc), "检查授权码与网络")
    else:
        record("IMAP 连通性", True, "已跳过（使用 --check-imap 启用）")

    if not quiet:
        info("")
        if results["ok"]:
            ok(_c("自检通过", "32"))
        else:
            warn("自检存在问题，见上方 ✗ 项")
    else:
        _print_json(results)
    return 0 if results["ok"] else 1


# ---------------------------------------------------------------------------
# 同步 / 状态
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> int:
    context = _load_context(args)
    try:
        if args.folders:
            result = context.sync.sync_all(
                folders=args.folders, full=args.full, index=not args.no_index
            )
        else:
            result = context.sync.sync_all(full=args.full, index=not args.no_index)

        payload = result.to_dict()
        if args.json:
            _print_json({"sync": payload, "index": context.indexer.health() if not args.no_index else None})
        else:
            status = payload["status"]
            (ok if status == "success" else warn)(f"同步完成：状态 {status}")
            info(f"  下载 {payload['fetched']} 封 / 归档 {payload['archived']} 封 / "
                 f"跳过 {payload['skipped']} / 失败 {payload['failed']} / 删除 {payload['deleted']}")
            reused = payload.get("attachments_reused") or 0
            if reused:
                info(f"  附件去重：{reused} 个命中已有内容，用硬链接复用（未重复占盘）")
            if payload["error_summary"]:
                warn(f"  错误摘要：{payload['error_summary']}")
            if not args.no_index:
                health = context.indexer.health()
                info(f"  知识库：{health['chunks']} 切片 / {health['vectors']} 向量")
        return 0 if payload["status"] != "failed" else 1
    finally:
        context.close()


def cmd_status(args: argparse.Namespace) -> int:
    context = _load_context(args)
    try:
        status = context.sync.status()
        if args.json:
            _print_json(status)
            return 0

        info(_c("— 同步状态 —", "36"))
        info(f"账号        : {status['account']}")
        info(f"运行中      : {'是' if status['running'] else '否'}")
        info(f"邮件总数    : {status['total_messages']}")
        info(f"切片 / 向量 : {status['chunks']} / {context.db.count_vectors()}")
        info(f"待索引      : {status['pending_index']}")
        info(f"数据库      : {context.db.path}")
        last = status.get("last_sync")
        if last:
            info(
                f"上次同步    : {last.get('finished_at') or last.get('started_at')} "
                f"[{last.get('status')}] 归档 {last.get('archived')} 封"
            )
        info("")
        info(_c("— 文件夹水位 —", "36"))
        for folder in status["folders"]:
            info(
                f"  {folder['name']:<28} uidvalidity={folder['uidvalidity']:<6} "
                f"last_uid={folder['last_uid']:<8} 上次={folder['last_sync_at'] or '从未'}"
            )
        return 0
    finally:
        context.close()


def cmd_folders(args: argparse.Namespace) -> int:
    context = _load_context(args)
    try:
        folders = context.sync.fetch_folders()
        if args.json:
            _print_json(folders)
            return 0
        info(_c(f"— 服务端文件夹（{len(folders)} 个）—", "36"))
        for folder in folders:
            flag = "" if folder["selectable"] else "  [不可选]"
            info(f"  {folder['name']}{flag}   flags={','.join(folder['flags']) or '-'}")
        return 0
    except Exception as exc:  # noqa: BLE001
        fail(str(exc))
        return 1
    finally:
        context.close()


def cmd_search(args: argparse.Namespace) -> int:
    from .search import SearchFilters

    context = _load_context(args)
    try:
        filters = SearchFilters(
            folder=args.folder or None,
            sender=args.sender,
            recipient=args.recipient,
            subject=args.subject,
            has_attachments=args.has_attachments,
        )
        hits = context.search.search(
            args.query, limit=args.limit, mode=args.mode, filters=filters
        )
        if args.json:
            _print_json([hit.to_dict() for hit in hits])
            return 0
        if not hits:
            warn("没有找到匹配的邮件")
            return 1
        info(_c(f"— 命中 {len(hits)} 条（模式：{args.mode}）—", "36"))
        for i, hit in enumerate(hits, start=1):
            info("")
            info(f"{_c(str(i), '36')}. {_c(hit.subject, '1')}")
            info(f"   发件人：{hit.sender}")
            info(f"   时间  ：{hit.date}    文件夹：{hit.folder}")
            info(f"   分数  ：{hit.score:.5f}  (kw_rank={hit.keyword_rank}, vec_rank={hit.vector_rank})")
            info(f"   片段  ：{hit.snippet}")
            info(f"   路径  ：{hit.local_markdown_path}")
        return 0
    finally:
        context.close()


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------

def cmd_index(args: argparse.Namespace) -> int:
    context = _load_context(args)
    try:
        if args.rebuild:
            warn("重建索引会重新生成全部切片与向量，可能需要较长时间…")
            if not args.yes:
                answer = input("确认继续？(y/N)：").strip().lower()
                if answer != "y":
                    info("已取消")
                    return 0
            stats = context.indexer.rebuild()
        else:
            pending = context.indexer.count_pending()
            if not pending:
                ok("没有待索引的邮件")
                return 0
            info(f"待索引 {pending} 封邮件…")
            stats = context.indexer.index_pending(limit=args.limit)

        if args.json:
            _print_json(stats.to_dict())
        else:
            ok(
                f"索引完成：{stats.messages} 封 / {stats.chunks} 切片 "
                f"（新嵌入 {stats.embedded}，复用 {stats.reused}，失败 {stats.failed}）"
            )
            for error in stats.errors[:5]:
                warn(f"  {error}")
        return 0 if stats.failed == 0 else 1
    finally:
        context.close()


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    from .cancellation import install_signal_handlers

    context = _load_context(args)
    token = install_signal_handlers(context.cancel)
    try:
        from .kb_api import serve

        serve(context, host=args.host, port=args.port)
        return 0
    except KeyboardInterrupt:
        info("")
        info("正在关闭…")
        return 0
    finally:
        token.cancel("API 服务退出")
        context.close()


def cmd_pick_directory(args: argparse.Namespace) -> int:
    """内部命令：在**独立进程**中弹出目录选择框。

    为什么必须是独立进程
    --------------------
    tkinter 的对话框要求跑在**主线程**。设置接口的调用发生在 FastAPI 的
    工作线程里，在那里创建 Tk 窗口根本不会显示（实测：进程一直卡住，
    窗口列表里什么都没有）。而独立进程天然拥有自己的主线程。

    结果通过**临时文件**回传，而不是 stdout：打包成窗口版 exe 时
    （``console=False``）``sys.stdout`` 可能是 None，管道不可靠。
    """
    out_file = args.out
    if not out_file:
        print("ERROR: 缺少 --out 参数", file=sys.stderr)
        return 2

    def _write(value: str) -> None:
        try:
            Path(out_file).write_text(value, encoding="utf-8")
        except OSError:
            pass

    try:
        import tkinter
        from tkinter import filedialog
    except Exception as exc:  # noqa: BLE001
        _write("")
        print(f"ERROR: 当前环境不支持目录选择框：{exc}", file=sys.stderr)
        return 3

    root = None
    try:
        root = tkinter.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001 - 部分平台不支持
            pass
        chosen = filedialog.askdirectory(
            title="选择数据存放目录",
            initialdir=args.initial or None,
            mustexist=False,
        )
    except Exception as exc:  # noqa: BLE001
        _write("")
        print(f"ERROR: 目录选择失败：{exc}", file=sys.stderr)
        return 4
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

    _write(str(chosen or ""))
    return 0


def cmd_settings_gui(args: argparse.Namespace) -> int:
    """内部命令：打开**原生**设置窗口。

    这是一条独立子进程入口：调用方（托盘菜单、设置 API）跑在后台线程里，
    而 tkinter 只能在主线程创建窗口。让子进程自己去开窗口最省事，
    也顺带避免了两套线程模型互相干扰。
    """
    from .gui.settings_window import run_settings_window, window_available

    if not window_available():
        print("ERROR: 当前环境没有图形界面，请改用 `--browser` 模式", file=sys.stderr)
        return 3

    context = _load_context(args, quiet=True)
    try:
        # 还没配完邮箱/授权码时，授权码是必填项
        from .config import resolve_auth_code

        require_auth = not resolve_auth_code(context.config)
        saved = run_settings_window(context.config, require_auth=require_auth)
        return 0 if saved else 1
    finally:
        context.close()


def cmd_main_gui(args: argparse.Namespace) -> int:
    """内部命令：打开**主窗口**（状态面板 + 检索）。

    与 ``_settings-gui`` 一样是独立子进程入口：托盘菜单跑在 pystray 线程里，
    而 tkinter 只能在主线程创建窗口。
    """
    from .gui import window_available
    from .gui.main_window import run_main_window

    if not window_available():
        print("ERROR: 当前环境没有图形界面", file=sys.stderr)
        return 3

    context = _load_context(args, quiet=True)
    try:
        return run_main_window(context, autosync=bool(getattr(args, "sync", False)))
    finally:
        context.close()


def cmd_app(args: argparse.Namespace) -> int:
    """打开程序主窗口；没有图形界面时给出明确指引而不是报错退出。"""
    from .gui import window_available
    from .gui.main_window import run_main_window

    if not window_available():
        warn("当前环境没有图形界面，主窗口不可用。")
        info("命令行等价操作：")
        info("  python main.py sync               # 同步")
        info("  python main.py search \"关键字\"    # 检索")
        info("  python main.py status             # 状态")
        return 3

    context = _load_context(args, quiet=True)
    try:
        return run_main_window(context, autosync=bool(getattr(args, "sync", False)))
    finally:
        context.close()


def cmd_settings(args: argparse.Namespace) -> int:
    """打开设置界面。

    默认走**原生窗口**（tkinter），不弹浏览器、不需要敲命令。
    ``--browser`` 可以退回旧的网页设置页。
    """
    if not args.browser:
        from .gui.settings_window import run_settings_window, window_available

        if window_available():
            context = _load_context(args, quiet=True)
            try:
                from .config import resolve_auth_code

                require_auth = not resolve_auth_code(context.config)
                saved = run_settings_window(context.config, require_auth=require_auth)
                return 0 if saved else 1
            finally:
                context.close()
        warn("当前环境没有图形界面，自动改用浏览器模式")

    import threading

    from .cancellation import install_signal_handlers
    from .kb_api import serve
    from .tray_app import open_url

    context = _load_context(args)
    token = install_signal_handlers(context.cancel)
    host = args.host or context.config.api.host
    port = args.port or context.config.api.port
    url = f"http://{host}:{port}/setup"

    if args.no_browser:
        info(f"设置界面：{url}")
    else:
        # 等 uvicorn 起来再打开，避免打开一个连接被拒的页面
        threading.Timer(1.8, lambda: open_url(url)).start()
        info(f"即将打开设置界面：{url}")

    try:
        serve(context, host=host, port=port)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        token.cancel("设置界面退出")
        context.close()


def cmd_tray(args: argparse.Namespace) -> int:
    from .cancellation import install_signal_handlers

    context = _load_context(args)
    token = install_signal_handlers(context.cancel)
    try:
        from .tray_app import run_tray

        return run_tray(context, with_api=not args.no_api)
    except KeyboardInterrupt:
        info("")
        info("正在关闭…")
        return 0
    finally:
        token.cancel("托盘退出")
        context.close()


# ---------------------------------------------------------------------------
# 维护
# ---------------------------------------------------------------------------

def cmd_backup(args: argparse.Namespace) -> int:
    """备份。

    **默认连归档文件与附件一起打包**：附件只存在于磁盘上，数据库里只有
    路径和 sha256。只备份 ``mail.db`` 会让用户以为已经备份好了，
    实际上附件永久丢失。需要廉价快照时才用 ``--db-only``。
    """
    context = _load_context(args)
    try:
        # 兼容旧写法：--with-files 已经是默认行为
        include_files = not args.db_only
        result = context.backup_full(
            include_files=include_files, label=args.label or ""
        )
        ok(f"备份完成：{result.db}")
        if not args.quiet:
            info(f"  数据库：{result.db.stat().st_size / 1024:.1f} KB  {result.db}")
        if result.files is not None:
            if not args.quiet:
                info(f"  归档与附件：{result.files.stat().st_size / 1024:.1f} KB  {result.files}")
        else:
            warn(
                "本次只备份了数据库，**不含附件**。"
                "附件无法仅凭数据库恢复，需要时请去掉 --db-only 重新备份。"
            )
        return 0
    finally:
        context.close()


def cmd_restore(args: argparse.Namespace) -> int:
    context = _load_context(args)
    try:
        if not args.yes:
            warn(f"将从 {args.backup_file} 恢复数据库，当前数据库会被覆盖（会先另存一份）")
            answer = input("确认继续？(y/N)：").strip().lower()
            if answer != "y":
                info("已取消")
                return 0
        path = context.restore(args.backup_file)
        ok(f"已恢复：{path}")
        return 0
    finally:
        context.close()


def cmd_migrate_blobs(args: argparse.Namespace) -> int:
    """给已有归档补建内容寻址仓库（只增不删，可重复执行）。"""
    from .blob_maintenance import migrate_blobs

    context = _load_context(args)
    try:
        info("正在把已有附件补建进 blob 仓库（不会移动或删除任何原文件）…")
        stats = migrate_blobs(
            context.config, context.db, limit=args.limit, relink=args.relink
        )
        ok(stats.describe())
        if stats.errors:
            warn("部分条目需要注意：")
            for line in stats.errors:
                warn(f"  · {line}")
        if stats.missing:
            warn(
                "缺失的文件无法从数据库恢复（库里只有路径与 sha256）。"
                "如果之前备份过归档，请先解压回原位置再重跑本命令。"
            )
        return 0 if not stats.failed else 1
    finally:
        context.close()


def cmd_verify_blobs(args: argparse.Namespace) -> int:
    """校验归档完整性，可选就地修复。"""
    from .blob_maintenance import verify_blobs

    context = _load_context(args)
    try:
        info("正在校验归档与 blob 仓库" + ("（逐字节重算哈希，可能较慢）" if not args.quick else "…"))
        stats = verify_blobs(
            context.config,
            context.db,
            deep=not args.quick,
            repair=args.repair,
            limit=args.limit,
        )
        (ok if stats.healthy else warn)(stats.describe())
        for line in stats.errors:
            warn(f"  · {line}")
        if not stats.healthy and not args.repair:
            info("提示：加 --repair 可从 blob 重建缺失的归档文件")
        return 0 if stats.healthy else 1
    finally:
        context.close()


def cmd_rebuild_fts(args: argparse.Namespace) -> int:
    context = _load_context(args)
    try:
        count = context.db.rebuild_fts()
        ok(f"FTS 索引已重建（{count} 封邮件）")
        return 0
    finally:
        context.close()


def cmd_config(args: argparse.Namespace) -> int:
    from .config_writer import config_file_path

    context = _load_context(args, quiet=True)
    try:
        if args.show:
            _print_json(context.config.model_dump())
            return 0
        info(str(config_file_path(getattr(args, "config", None))))
        return 0
    finally:
        context.close()


# ---------------------------------------------------------------------------
# 演示数据
# ---------------------------------------------------------------------------

def cmd_demo(args: argparse.Namespace) -> int:
    """生成演示数据，方便在没有邮箱时先体验完整功能。"""
    from .demo_data import generate_demo_data

    context = _load_context(args)
    try:
        if not args.yes:
            info("将向本地归档写入演示邮件（不会连接任何邮箱）。")
            if args.reset:
                warn("--reset 会清空现有数据库中的全部邮件记录与向量索引。")
            answer = input("继续？(y/N)：").strip().lower()
            if answer != "y":
                info("已取消")
                return 0

        result = generate_demo_data(context, count=args.count, reset=args.reset)
        if args.json:
            _print_json(result)
            return 0

        stats = result["index"]
        ok(f"已归档 {result['created']} 封演示邮件 → {result['archive']}")
        ok(
            f"索引完成：{stats['messages']} 封 / {stats['chunks']} 切片 "
            f"（新嵌入 {stats['embedded']}，复用 {stats['reused']}，失败 {stats['failed']}）"
        )
        info("")
        info("下一步：")
        info("  python main.py status")
        info('  python main.py search "报销发票怎么弄"')
        info("  python main.py serve     # 然后访问 http://127.0.0.1:8990/docs")
        info("  python main.py tray")
        return 0
    finally:
        context.close()


# ---------------------------------------------------------------------------
# 嵌入模型管理
# ---------------------------------------------------------------------------

def cmd_model(args: argparse.Namespace) -> int:
    """管理 ONNX 嵌入模型（import / download / status）。"""
    from .config import load_config
    from .model_manager import (
        DEFAULT_ENDPOINT,
        ModelError,
        check_model_dir,
        download_model,
        import_model,
        resolve_endpoint,
    )

    config = load_config(getattr(args, "config", None))
    target = config.model_path
    action = args.model_action

    if action == "status":
        status = check_model_dir(target)
        if args.json:
            _print_json(status.to_dict())
            return 0 if status.ready else 1
        info(f"模型目录 : {status.path}")
        info(f"占用空间 : {status.size_mb if hasattr(status, 'size_mb') else round(status.size_bytes/1024/1024, 1)} MB")
        info(f"文件     : {', '.join(status.files) or '(空)'}")
        if status.ready:
            ok(f"模型就绪 —— {status.note}")
            return 0
        warn(f"模型不可用 —— {status.note}")
        info("")
        info("获取模型的三条路径：")
        info("  1) 从已有目录导入（离线/内网）：")
        info("     python main.py model import /path/to/model-dir")
        info("  2) 从发行包附件或内部镜像下载：")
        info("     python main.py model download --url <URL>")
        info("  3) 从 HuggingFace 仓库下载（区分度可能不如自建导出，需自行验证）：")
        info("     python main.py model download --repo <owner/name> --endpoint https://hf-mirror.com")
        return 1

    if action == "import":
        source = args.source
        if not source:
            fail("请提供源目录或 .zip 文件路径")
            return 2
        try:
            status = import_model(source, target, overwrite=args.force)
        except ModelError as exc:
            fail(str(exc))
            return 1
        if status.ready:
            ok(f"模型已导入到 {status.path}（{status.size_bytes/1024/1024:.1f} MB）")
            return 0
        warn(f"导入后模型仍不可用：{status.note}")
        return 1

    if action == "download":
        endpoint = resolve_endpoint(args.endpoint)
        if args.repo and endpoint == DEFAULT_ENDPOINT:
            warn("未指定 --endpoint。中国大陆访问 huggingface.co 通常不可达，")
            warn("如遇失败请改用：--endpoint https://hf-mirror.com")
        info(f"目标目录：{target}")
        info(f"来源：{args.url or f'{endpoint}/{args.repo}'}")

        last_shown = [0]

        def progress(name: str, received: int, total: int) -> None:
            if total and received - last_shown[0] > (5 << 20):
                last_shown[0] = received
                pct = received * 100 // total
                print(f"  {name}: {pct}%", flush=True)

        try:
            status = download_model(
                target,
                url=args.url,
                repo=args.repo,
                endpoint=endpoint,
                variant=args.variant,
                overwrite=args.force,
                on_progress=progress,
            )
        except ModelError as exc:
            fail(str(exc))
            return 1
        if status.ready:
            ok(f"模型已就绪：{status.path}（{status.size_bytes/1024/1024:.1f} MB）")
            if status.note and "⚠️" in status.note:
                warn(status.note.strip())
            info("")
            info("下一步：python main.py index --rebuild")
            return 0
        warn(f"下载完成但模型不可用：{status.note}")
        return 1

    fail(f"未知 model 子命令：{action}")
    return 2


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="email-assistant",
        description="腾讯企业邮箱邮件管理助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="store_true", help="显示版本后退出")

    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="生成配置模板 / 配置向导")
    p_init.add_argument("--force", action="store_true", help="覆盖已有配置")
    p_init.add_argument("--non-interactive", action="store_true", help="只生成模板，不提问")
    p_init.set_defaults(func=cmd_init)

    p_auth = sub.add_parser("auth", help="管理授权码")
    p_auth.add_argument(
        "auth_action", choices=["set", "clear", "status", "show"], help="操作"
    )
    p_auth.add_argument("--auth-code", help="直接提供授权码（不安全，仅用于自动化）")
    p_auth.set_defaults(func=cmd_auth)

    p_doctor = sub.add_parser("doctor", help="环境与配置自检")
    p_doctor.add_argument("--check-imap", action="store_true", help="测试 IMAP 连通性")
    p_doctor.add_argument("--check-model", action="store_true", help="执行一次嵌入推理")
    p_doctor.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_doctor.set_defaults(func=cmd_doctor)

    p_sync = sub.add_parser("sync", help="手动同步邮件")
    p_sync.add_argument("--folder", dest="folders", action="append", help="只同步指定文件夹")
    p_sync.add_argument("--full", action="store_true", help="忽略水位，全量重新扫描")
    p_sync.add_argument("--no-index", action="store_true", help="跳过切片与向量索引")
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_status = sub.add_parser("status", help="查看同步状态")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_folders = sub.add_parser("folders", help="列出服务端文件夹")
    p_folders.add_argument("--json", action="store_true")
    p_folders.set_defaults(func=cmd_folders)

    p_search = sub.add_parser("search", help="检索邮件")
    p_search.add_argument("query", help="检索语句")
    p_search.add_argument("--mode", choices=["hybrid", "keyword", "vector"], default="hybrid")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--folder", action="append", help="限定文件夹，可重复")
    p_search.add_argument("--sender", help="发件人包含")
    p_search.add_argument("--recipient", help="收件人包含")
    p_search.add_argument("--subject", help="主题包含")
    p_search.add_argument("--has-attachments", dest="has_attachments", action="store_true",
                          default=None)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_index = sub.add_parser("index", help="生成切片与向量索引")
    p_index.add_argument("--rebuild", action="store_true", help="清空并全量重建")
    p_index.add_argument("--limit", type=int, default=0, help="最多处理多少封")
    p_index.add_argument("--yes", "-y", action="store_true", help="跳过二次确认")
    p_index.add_argument("--json", action="store_true")
    p_index.set_defaults(func=cmd_index)

    p_serve = sub.add_parser("serve", help="启动知识库 API")
    p_serve.add_argument("--host", help="监听地址（默认取配置）")
    p_serve.add_argument("--port", type=int, help="监听端口（默认 8990）")
    p_serve.set_defaults(func=cmd_serve)

    # 内部命令：供设置界面弹出目录选择框，不面向用户
    p_pick = sub.add_parser("_pick-directory", help=argparse.SUPPRESS)
    p_pick.add_argument("--out", required=True, help=argparse.SUPPRESS)
    p_pick.add_argument("--initial", default="", help=argparse.SUPPRESS)
    p_pick.set_defaults(func=cmd_pick_directory)

    # 内部命令：在独立子进程中打开原生设置窗口（供托盘等后台线程调用）
    p_gui = sub.add_parser("_settings-gui", help=argparse.SUPPRESS)
    p_gui.set_defaults(func=cmd_settings_gui)

    p_main = sub.add_parser("_main-gui", help=argparse.SUPPRESS)
    p_main.add_argument("--sync", action="store_true", help=argparse.SUPPRESS)
    p_main.set_defaults(func=cmd_main_gui)

    p_app = sub.add_parser("app", help="打开程序主窗口（状态面板 + 检索）")
    p_app.add_argument("--sync", action="store_true", help="打开后立刻同步一次")
    p_app.set_defaults(func=cmd_app)

    p_settings = sub.add_parser("settings", help="打开设置界面（默认原生窗口）")
    p_settings.add_argument(
        "--browser", action="store_true", help="改用浏览器里的设置页面（旧界面）"
    )
    p_settings.add_argument("--host", help="浏览器模式的监听地址（默认取配置）")
    p_settings.add_argument("--port", type=int, help="浏览器模式的监听端口（默认 8990）")
    p_settings.add_argument("--no-browser", action="store_true", help="只启动服务，不自动打开浏览器")
    p_settings.set_defaults(func=cmd_settings)

    p_tray = sub.add_parser("tray", help="托盘常驻 + 定时同步")
    p_tray.add_argument("--no-api", action="store_true", help="不启动 API 子进程")
    p_tray.set_defaults(func=cmd_tray)

    p_backup = sub.add_parser("backup", help="备份数据库 + 归档文件（默认）")
    p_backup.add_argument(
        "--db-only",
        action="store_true",
        help="只备份数据库快照（**不含附件**，附件只能从压缩包恢复）",
    )
    # 旧写法：文件现在是默认行为，保留这个开关只为不破坏已有脚本
    p_backup.add_argument("--with-files", action="store_true", help=argparse.SUPPRESS)
    p_backup.add_argument("--label", help="备份文件标签")
    p_backup.add_argument("--quiet", action="store_true")
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="从备份恢复")
    p_restore.add_argument("backup_file", help="备份文件路径")
    p_restore.add_argument("--yes", "-y", action="store_true")
    p_restore.set_defaults(func=cmd_restore)

    p_mig = sub.add_parser(
        "migrate-blobs", help="为已有归档补建内容寻址仓库（blobs）"
    )
    p_mig.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    p_mig.add_argument(
        "--relink",
        action="store_true",
        help="把内容相同但仍各占一份 inode 的附件合并为硬链接（原子替换，先校验内容）",
    )
    p_mig.set_defaults(func=cmd_migrate_blobs)

    p_ver = sub.add_parser("verify-blobs", help="校验归档完整性（可 --repair）")
    p_ver.add_argument("--quick", action="store_true", help="只查文件是否存在，不重算哈希")
    p_ver.add_argument("--repair", action="store_true", help="从 blob 重建缺失的归档文件")
    p_ver.add_argument("--limit", type=int, default=0, help="只校验前 N 条（0=全部）")
    p_ver.set_defaults(func=cmd_verify_blobs)

    p_fts = sub.add_parser("rebuild-fts", help="重建全文索引")
    p_fts.set_defaults(func=cmd_rebuild_fts)

    p_config = sub.add_parser("config", help="显示配置")
    p_config.add_argument("--show", action="store_true", help="输出完整配置（JSON）")
    p_config.set_defaults(func=cmd_config)

    p_demo = sub.add_parser("demo", help="生成演示数据（无需真实邮箱）")
    p_demo.add_argument("--count", type=int, default=12, help="生成数量")
    p_demo.add_argument("--reset", action="store_true", help="先清空现有数据")
    p_demo.add_argument("--yes", "-y", action="store_true", help="跳过二次确认")
    p_demo.add_argument("--json", action="store_true")
    p_demo.set_defaults(func=cmd_demo)

    p_model = sub.add_parser("model", help="管理 ONNX 嵌入模型")
    p_model.add_argument(
        "model_action", choices=["status", "import", "download"], help="操作"
    )
    p_model.add_argument("source", nargs="?", help="import 的源目录或 .zip")
    p_model.add_argument("--url", help="download 的直接下载地址（zip 或裸 model.onnx）")
    p_model.add_argument("--repo", help="download 的 HuggingFace 仓库，如 owner/name")
    p_model.add_argument("--endpoint", help="HF 端点（默认 https://huggingface.co）")
    p_model.add_argument("--variant", default="model.onnx", help="仓库内的 onnx 文件名")
    p_model.add_argument("--force", action="store_true", help="覆盖已有文件")
    p_model.add_argument("--json", action="store_true")
    p_model.set_defaults(func=cmd_model)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print(f"email-assistant {__version__}")
        return 0

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        info("")
        info("已中断")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，避免刷栈
        fail(f"{type(exc).__name__}: {exc}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1
