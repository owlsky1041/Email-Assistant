"""取消令牌、优雅退出、BODYSTRUCTURE 解析与数据库备份测试。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.cancellation import (
    CancellationToken,
    CancelledError,
    get_cancellation_token,
    install_signal_handlers,
    reset_cancellation_token,
)
from src.context import AppContext
from src.imap_client import parse_bodystructure


class TestCancellationToken:
    def test_initially_not_cancelled(self) -> None:
        assert CancellationToken().cancelled is False

    def test_cancel_sets_flag(self) -> None:
        token = CancellationToken()
        token.cancel("测试")
        assert token.cancelled is True
        assert token.reason == "测试"

    def test_raise_if_cancelled(self) -> None:
        token = CancellationToken()
        token.raise_if_cancelled()  # 不应抛出
        token.cancel()
        with pytest.raises(CancelledError):
            token.raise_if_cancelled()

    def test_callbacks_invoked(self) -> None:
        token = CancellationToken()
        calls: list[str] = []
        token.on_cancel(lambda: calls.append("a"))
        token.on_cancel(lambda: calls.append("b"))
        token.cancel()
        assert calls == ["a", "b"]

    def test_callback_registered_after_cancel_runs_immediately(self) -> None:
        """§11.4 已取消后再注册的回调必须立刻执行，避免任务卡住。"""
        token = CancellationToken()
        token.cancel()
        calls: list[str] = []
        token.on_cancel(lambda: calls.append("late"))
        assert calls == ["late"]

    def test_unregister(self) -> None:
        token = CancellationToken()
        calls: list[str] = []
        unregister = token.on_cancel(lambda: calls.append("x"))
        unregister()
        token.cancel()
        assert calls == []

    def test_callback_exception_does_not_block_others(self) -> None:
        token = CancellationToken()
        calls: list[str] = []

        def boom() -> None:
            raise RuntimeError("回调炸了")

        token.on_cancel(boom)
        token.on_cancel(lambda: calls.append("ok"))
        token.cancel()  # 不应抛出
        assert calls == ["ok"]

    def test_cancel_is_idempotent(self) -> None:
        token = CancellationToken()
        calls: list[int] = []
        token.on_cancel(lambda: calls.append(1))
        token.cancel("第一次")
        token.cancel("第二次")
        assert calls == [1]
        assert token.reason == "第一次"

    def test_wait_returns_early_on_cancel(self) -> None:
        token = CancellationToken()

        def cancel_soon() -> None:
            time.sleep(0.05)
            token.cancel()

        threading.Thread(target=cancel_soon, daemon=True).start()
        start = time.monotonic()
        assert token.wait(5.0) is True
        assert time.monotonic() - start < 2.0, "取消应立即唤醒等待者"

    def test_wait_times_out_uncancelled(self) -> None:
        token = CancellationToken()
        start = time.monotonic()
        assert token.wait(0.1) is False
        assert time.monotonic() - start >= 0.09

    def test_thread_safety(self) -> None:
        """多线程同时注册与取消不应抛异常。"""
        token = CancellationToken()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(50):
                    token.on_cancel(lambda: None)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        token.cancel()
        for t in threads:
            t.join(timeout=5)
        assert errors == []

    def test_global_token_singleton(self) -> None:
        reset_cancellation_token()
        first = get_cancellation_token()
        second = get_cancellation_token()
        assert first is second

    def test_reset_gives_fresh_token(self) -> None:
        token = get_cancellation_token()
        token.cancel()
        fresh = reset_cancellation_token()
        assert fresh.cancelled is False
        assert fresh is not token


class TestSignalHandlers:
    def test_install_returns_token(self) -> None:
        reset_cancellation_token()
        token = CancellationToken()
        assert install_signal_handlers(token) is token


class TestParseBodystructure:
    def test_simple_singlepart(self) -> None:
        raw = b'("text" "plain" ("charset" "utf-8") NIL NIL "7BIT" 1234 56 NIL NIL NIL)'
        parts = parse_bodystructure(raw)
        assert len(parts) == 1
        assert parts[0].content_type == "text/plain"
        assert parts[0].size == 1234
        assert parts[0].lines == 56
        assert parts[0].section == "1"

    def test_multipart_with_attachment(self) -> None:
        raw = (
            b'(("text" "plain" ("charset" "utf-8") NIL NIL "7BIT" 100 5 NIL NIL NIL)'
            b'("application" "pdf" ("name" "report.pdf") NIL NIL "BASE64" 50000 NIL'
            b' ("attachment" ("filename" "report.pdf")) NIL) "mixed")'
        )
        parts = parse_bodystructure(raw)
        assert len(parts) == 2
        text, attachment = parts
        assert text.content_type == "text/plain"
        assert text.section == "1"
        assert attachment.content_type == "application/pdf"
        assert attachment.section == "2"
        assert attachment.size == 50000
        assert attachment.filename == "report.pdf"
        assert attachment.disposition == "attachment"
        assert not attachment.is_text

    def test_nested_multipart_sections(self) -> None:
        raw = (
            b'((("text" "plain" ("charset" "utf-8") NIL NIL "7BIT" 10 1 NIL NIL NIL)'
            b'("text" "html" ("charset" "utf-8") NIL NIL "7BIT" 20 2 NIL NIL NIL)'
            b'"alternative" NIL NIL NIL)'
            b'("image" "png" ("name" "logo.png") NIL NIL "BASE64" 300 NIL'
            b' ("inline" ("filename" "logo.png")) NIL) "mixed")'
        )
        parts = parse_bodystructure(raw)
        sections = {p.section: p.content_type for p in parts}
        assert sections["1.1"] == "text/plain"
        assert sections["1.2"] == "text/html"
        assert sections["2"] == "image/png"
        inline = [p for p in parts if p.disposition == "inline"]
        assert len(inline) == 1

    def test_encoded_filename_decoded(self) -> None:
        raw = (
            b'(("text" "plain" NIL NIL NIL "7BIT" 10 1 NIL NIL NIL)'
            b'("application" "octet-stream" ("name" "=?utf-8?B?5oql6KGoLnBkZg==?=") NIL NIL'
            b' "BASE64" 100 NIL NIL NIL) "mixed")'
        )
        parts = parse_bodystructure(raw)
        assert parts[1].filename == "报表.pdf"

    def test_malformed_returns_empty(self) -> None:
        assert parse_bodystructure(b"garbage") == []
        assert parse_bodystructure(b"") == []
        assert parse_bodystructure(b"()") == []

    def test_does_not_raise_on_truncated(self) -> None:
        assert isinstance(parse_bodystructure(b'(("text" "plain"'), list)

    def test_quoted_values_with_spaces(self) -> None:
        raw = b'("text" "plain" ("name" "my report final.pdf") NIL NIL "7BIT" 10 1 NIL NIL NIL)'
        parts = parse_bodystructure(raw)
        assert parts[0].filename == "my report final.pdf"


class TestBackupRestore:
    def test_backup_creates_file(self, context: AppContext) -> None:
        """§7 数据库备份功能。"""
        path = context.backup()
        assert path.is_file()
        assert path.suffix == ".db"
        assert path.stat().st_size > 0

    def test_backup_is_valid_sqlite(self, context: AppContext) -> None:
        import sqlite3

        path = context.backup()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "messages" in tables
        finally:
            conn.close()

    def test_backup_preserves_data(self, context: AppContext) -> None:
        import sqlite3

        from src.models import MessageRecord

        context.db.insert_message(
            MessageRecord(account="a@x.com", uid="1", message_id="m1", folder="INBOX",
                          subject="备份测试", body_text="内容")
        )
        path = context.backup()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_backup_with_files(self, context: AppContext) -> None:
        import zipfile

        from src.models import ParsedMessage

        message = ParsedMessage(uid="1", folder="INBOX", subject="归档测试",
                                body_markdown="正文", body_text="正文")
        context.sync.exporter.export(message, account="a@x.com")

        archive = context.backup(include_files=True)
        assert archive.suffix == ".zip"
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        assert any(name.endswith(".md") for name in names)

    def test_backup_label(self, context: AppContext) -> None:
        path = context.backup(label="manual")
        assert "manual" in path.name

    def test_restore_replaces_database(self, context: AppContext) -> None:
        from src.models import MessageRecord

        context.db.insert_message(
            MessageRecord(account="a@x.com", uid="1", message_id="m1", folder="INBOX",
                          subject="恢复前", body_text="x")
        )
        backup = context.backup()

        context.db.insert_message(
            MessageRecord(account="a@x.com", uid="2", message_id="m2", folder="INBOX",
                          subject="恢复后新增", body_text="y")
        )
        assert context.db.count_messages() == 2

        context.restore(backup)
        assert context.db.count_messages() == 1

    def test_restore_missing_file_raises(self, context: AppContext, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            context.restore(tmp_path / "absent.db")

    def test_restore_rejects_corrupt_file(self, context: AppContext, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus.db"
        bogus.write_bytes(b"this is not a database at all")
        with pytest.raises(Exception):
            context.restore(bogus)


class TestContextLifecycle:
    def test_summary(self, context: AppContext) -> None:
        summary = context.summary()
        assert summary["database"]["schema_version"] == 1
        assert "counts" in summary

    def test_context_manager(self, tmp_config: AppContext) -> None:
        with AppContext(tmp_config, configure_logging=False) as ctx:
            assert ctx.db.count_messages() == 0

    def test_lazy_components_not_loaded(self, tmp_config) -> None:
        """未使用的组件不应被提前初始化（避免启动就加载模型）。"""
        ctx = AppContext(tmp_config, configure_logging=False)
        try:
            assert ctx._embedder is None
            assert ctx._vector_store is None
            assert ctx._indexer is None
            assert ctx.sync_running is False
            assert ctx._sync is None
        finally:
            ctx.close()

    def test_warmup_loads_components(self, context: AppContext) -> None:
        info = context.warmup()
        assert "embedder" in info and "vector_store" in info
        assert context._embedder is not None


class TestContextServiceInvalidation:
    """回归测试：恢复数据库后，派生服务不能继续持有旧连接。"""

    def test_restore_rebuilds_data_services(self, context: AppContext) -> None:
        from src.models import MessageRecord

        context.db.insert_message(
            MessageRecord(account="a@x.com", uid="1", message_id="m1", folder="INBOX",
                          subject="恢复前", body_text="发票内容")
        )
        context.indexer.index_pending()
        assert context._vector_store is not None

        backup = context.backup()
        context.restore(backup)

        assert context._vector_store is None, "向量库必须被丢弃并在下次访问时重建"
        assert context._indexer is None
        assert context._search is None
        assert context._sync is None

        # 重建后的向量库必须绑定到新的 Database 实例
        assert context.vector_store.db is context.db
        assert context.search.search("发票", limit=3)

    def test_restore_then_search_consistent(self, context: AppContext) -> None:
        from src.models import MessageRecord

        context.db.insert_message(
            MessageRecord(account="a@x.com", uid="1", message_id="m1", folder="INBOX",
                          subject="保留的邮件", body_text="这条内容在备份里。")
        )
        context.indexer.index_pending()
        backup = context.backup()

        context.db.insert_message(
            MessageRecord(account="a@x.com", uid="2", message_id="m2", folder="INBOX",
                          subject="将被丢弃", body_text="这条内容不在备份里。")
        )
        context.indexer.index_pending()

        context.restore(backup)
        hits = context.search.search("内容", limit=10)
        subjects = {h.subject for h in hits}
        assert "保留的邮件" in subjects
        assert "将被丢弃" not in subjects


class TestCloseIdempotent:
    def test_double_close_is_safe(self, tmp_config) -> None:
        """回归测试：托盘与 CLI 都会关闭上下文，重复关闭不应报错或重复记日志。"""
        ctx = AppContext(tmp_config, configure_logging=False)
        ctx.close()
        ctx.close()  # 不应抛异常

    def test_close_after_restore(self, context: AppContext) -> None:
        context.restore(context.backup())
        context.close()
        context.close()


class TestOptionalImportProbe:
    """可选依赖探测必须容错。

    回归测试：无显示环境下 `import pystray` 抛的是
    `Xlib.error.DisplayNameError` 而不是 ImportError。
    早期 doctor 只捕获 ImportError，装过托盘依赖的无头服务器上会直接崩溃。
    """

    def test_returns_true_for_existing_module(self) -> None:
        from src.utils import optional_import_works

        ok, reason = optional_import_works("json")
        assert ok is True and reason == ""

    def test_returns_false_for_missing_module(self) -> None:
        from src.utils import optional_import_works

        ok, reason = optional_import_works("definitely_not_a_real_module_xyz")
        assert ok is False
        assert "ModuleNotFoundError" in reason

    def test_survives_non_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟像 pystray 那样抛非 ImportError 的模块。"""
        import builtins
        import sys
        import types

        from src.utils import optional_import_works

        fake = types.ModuleType("fake_exploding_module")

        def _boom(*args, **kwargs):
            raise RuntimeError("no display available")

        fake.__getattr__ = _boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fake_exploding_module", fake)
        ok, reason = optional_import_works("fake_exploding_module")
        assert ok is True  # 模块能拿到就不算失败

        # 真正模拟"导入即抛非 ImportError"
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "exploding_module":
                raise RuntimeError("no display available")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        ok, reason = optional_import_works("exploding_module")
        assert ok is False
        assert "RuntimeError" in reason


class TestWindowsConsoleEncoding:
    """回归测试：Windows 控制台默认代码页不是 UTF-8。

    `print("中文")` 在英文版 Windows（cp1252/cp437）上会抛
    UnicodeEncodeError 并让命令直接崩溃。CLI 启动时必须把
    stdout/stderr 切到 UTF-8。
    """

    def test_reconfigure_is_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src import cli as cli_module

        called: list[dict] = []

        class FakeStream:
            def reconfigure(self, **kwargs):
                called.append(kwargs)

        monkeypatch.setattr(cli_module.sys, "stdout", FakeStream())
        monkeypatch.setattr(cli_module.sys, "stderr", FakeStream())
        cli_module._force_utf8_console()

        assert len(called) == 2
        assert all(c["encoding"] == "utf-8" for c in called)
        assert all(c["errors"] == "replace" for c in called)

    def test_survives_stream_without_reconfigure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """被重定向到不支持 reconfigure 的对象时不能报错。"""
        from src import cli as cli_module

        class Bare:
            pass

        monkeypatch.setattr(cli_module.sys, "stdout", Bare())
        monkeypatch.setattr(cli_module.sys, "stderr", Bare())
        cli_module._force_utf8_console()  # 不应抛异常

    def test_survives_reconfigure_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src import cli as cli_module

        class Broken:
            def reconfigure(self, **kwargs):
                raise OSError("不支持的终端")

        monkeypatch.setattr(cli_module.sys, "stdout", Broken())
        monkeypatch.setattr(cli_module.sys, "stderr", Broken())
        cli_module._force_utf8_console()

    def test_main_calls_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src import cli as cli_module

        seen: list[bool] = []
        monkeypatch.setattr(cli_module, "_force_utf8_console", lambda: seen.append(True))
        cli_module.main(["--version"])
        assert seen == [True]
