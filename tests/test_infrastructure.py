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


class TestVersionSingleSource:
    """版本号必须只有一个来源（src/__init__.py）。

    回归：曾经在 kb_api、spec、installer.iss、pyproject 各写一份 0.1.0，
    发布时极易出现"程序报 0.1.0 而安装包写 0.1.1"的不一致。
    """

    def test_package_exposes_version(self) -> None:
        from src import __version__

        assert __version__
        assert __version__.count(".") >= 1

    def test_kb_api_uses_package_version(self) -> None:
        import inspect

        from src import __version__, kb_api

        src_text = inspect.getsource(kb_api.create_app)
        assert "version=__version__" in src_text
        assert "0.1." not in src_text, "kb_api 里不应再硬编码版本号"
        assert __version__

    #: 仓库根：必须用 __file__ 推导。
    #: 不能用 src.config.PROJECT_ROOT —— 它随 EMAIL_ASSISTANT_HOME 变化
    #: （CI 的测试任务恰好会设这个变量），届时这些文件根本找不到。
    REPO_ROOT = Path(__file__).resolve().parent.parent

    def test_spec_reads_version_dynamically(self) -> None:
        spec = (self.REPO_ROOT / "packaging" / "email-assistant.spec").read_text(encoding="utf-8")
        assert "_read_version()" in spec
        assert 'CFBundleShortVersionString": APP_VERSION' in spec

    def test_pyproject_uses_dynamic_version(self) -> None:
        text = (self.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'dynamic = ["version"]' in text
        assert 'attr = "src.__version__"' in text

    def test_installer_has_no_hardcoded_release_version(self) -> None:
        text = (self.REPO_ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8-sig")
        assert '#define MyAppVersion "0.0.0"' in text, "安装脚本应由构建脚本传入版本"

    def test_spec_version_parser_matches_package(self, tmp_path: Path) -> None:
        """spec 里的正则必须能解析出与包一致的版本。"""
        import re

        from src import __version__

        text = (self.REPO_ROOT / "src" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        assert match and match.group(1) == __version__


class TestRepoRootUsageConvention:
    """静态约定检查：测试不得用 src.config.PROJECT_ROOT 定位仓库文件。

    该项目根会随 EMAIL_ASSISTANT_HOME 变化（这是**受支持的用户配置**），
    而 CI 的测试任务恰好会设这个变量。我已经因此踩了两次坑
    （test_config 的 doctor 测试、test_infrastructure 的版本测试），
    两次都是本地全绿、CI 全红。这类"本地过、CI 挂"最难排查，
    所以用一条静态检查把约定固化下来。
    """

    REPO_ROOT = Path(__file__).resolve().parent.parent

    #: 合理的例外：这里两边都用同一个模块常量推导，
    #: 断言的是"回退行为"而非某个绝对路径，因此不受 PROJECT_ROOT 变化影响。
    ALLOWLIST = {
        "test_security.py": "断言 SecretStore 在无 source_path 时回退到模块的 config 目录，"
                            "左右两侧同源推导，自洽",
    }

    def test_no_test_uses_config_project_root_for_repo_files(self) -> None:
        offenders: list[str] = []
        self_name = Path(__file__).name
        for path in sorted((self.REPO_ROOT / "tests").glob("test_*.py")):
            if path.name in (self_name, *self.ALLOWLIST):
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                if "config.PROJECT_ROOT" in stripped or (
                    "import PROJECT_ROOT" in stripped
                ):
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
        assert not offenders, (
            "以下位置用可被 EMAIL_ASSISTANT_HOME 覆盖的 PROJECT_ROOT 定位仓库文件，"
            "在 CI 上必然失败；请改用 Path(__file__).resolve().parent.parent：\n  "
            + "\n  ".join(offenders)
        )

    #: 允许用 config.PROJECT_ROOT 的场景：它表示"用户的数据根"，
    #: 用来放配置文件、数据目录是**正确**的；只有用来定位**源码文件**才是错的。
    SRC_ALLOWED_PATTERNS = (
        "DEFAULT_CONFIG_PATH",   # 默认配置文件路径：本就该跟随用户目录
        "config_file_path",
    )

    def test_src_does_not_use_config_project_root_for_source_files(self) -> None:
        """src/ 里不得用 config.PROJECT_ROOT 定位源码文件。

        回归：settings_service 曾用 PROJECT_ROOT / "main.py" 拼子进程命令，
        而该值随 EMAIL_ASSISTANT_HOME 变化，导致设置了该变量的用户
        "浏览…"目录选择框直接报 can't open file '<home>/main.py'。
        """
        offenders: list[str] = []
        src_dir = self.REPO_ROOT / "src"
        for path in sorted(src_dir.rglob("*.py")):
            if path.name == "config.py":       # PROJECT_ROOT 的定义处
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#") or "import" in stripped:
                    continue
                if "PROJECT_ROOT" not in stripped:
                    continue
                if any(ok in stripped for ok in self.SRC_ALLOWED_PATTERNS):
                    continue
                offenders.append(f"{path.relative_to(self.REPO_ROOT)}:{lineno}: {stripped}")
        assert not offenders, (
            "以下位置用可被 EMAIL_ASSISTANT_HOME 覆盖的 PROJECT_ROOT 定位源码文件；"
            "请改用 Path(__file__).resolve().parent.parent（SOURCE_ROOT）：\n  "
            + "\n  ".join(offenders)
        )

    def test_allowlist_entries_still_exist(self) -> None:
        """例外清单里的文件必须真实存在，避免清单腐烂。"""
        tests_dir = self.REPO_ROOT / "tests"
        for name in self.ALLOWLIST:
            assert (tests_dir / name).is_file(), f"例外清单指向了不存在的文件：{name}"

    def test_suite_passes_with_home_override(self) -> None:
        """确保没有其它地方隐含依赖"PROJECT_ROOT 就是仓库根"。"""
        import os

        from src.config import runtime_root

        original = os.environ.get("EMAIL_ASSISTANT_HOME")
        try:
            os.environ["EMAIL_ASSISTANT_HOME"] = "/tmp/ea-convention-probe"
            assert runtime_root() != self.REPO_ROOT
        finally:
            if original is None:
                os.environ.pop("EMAIL_ASSISTANT_HOME", None)
            else:
                os.environ["EMAIL_ASSISTANT_HOME"] = original


class TestTrayTitleEncoding:
    """回归：pystray 的 X11 后端用 latin-1 编码窗口标题，中文会直接崩。

    实测：`pystray.Icon(title="邮件管理助手")` 在 X11 上抛
    UnicodeEncodeError，整个托盘起不来。而这条路径在无头环境下
    （pystray 在 import 阶段就失败、直接降级守护模式）从未被执行过。
    """

    def test_linux_uses_ascii_title(self) -> None:
        import sys

        from src.tray_app import TRAY_TITLE_ASCII, tray_title

        if sys.platform.startswith("linux"):
            assert tray_title() == TRAY_TITLE_ASCII
            tray_title().encode("latin-1")  # 必须能被 latin-1 编码
        else:
            assert tray_title()  # 其它平台用中文

    def test_title_is_always_latin1_encodable_on_linux(self) -> None:
        from src import tray_app

        original = tray_app.sys.platform
        try:
            tray_app.sys.platform = "linux"
            tray_app.tray_title().encode("latin-1")
        finally:
            tray_app.sys.platform = original

    def test_build_icon_falls_back_on_unicode_error(self, tmp_config) -> None:
        """后端不支持非 ASCII 标题时必须降级，而不是让托盘起不来。"""
        from src import tray_app

        if not tray_app.TRAY_AVAILABLE:
            pytest.skip("未安装 pystray/Pillow")

        attempts: list[str] = []

        class FakeIcon:
            def __init__(self, name, icon=None, title=None, menu=None):
                attempts.append(title)
                if title and any(ord(c) > 255 for c in title):
                    raise UnicodeEncodeError("latin-1", title, 0, 1, "不支持中文")
                self.title = title

        class FakePystray:
            Icon = FakeIcon

        original = tray_app.pystray
        try:
            tray_app.pystray = FakePystray
            app = tray_app.TrayApplication.__new__(tray_app.TrayApplication)
            icon = app._build_icon(menu=None)
            assert len(attempts) == 2, "应先尝试原标题，再回退"
            assert icon.title.encode("latin-1")
        finally:
            tray_app.pystray = original

    def test_build_icon_no_retry_when_title_ok(self, tmp_config) -> None:
        from src import tray_app

        if not tray_app.TRAY_AVAILABLE:
            pytest.skip("未安装 pystray/Pillow")

        attempts: list[str] = []

        class FakeIcon:
            def __init__(self, name, icon=None, title=None, menu=None):
                attempts.append(title)
                self.title = title

        class FakePystray:
            Icon = FakeIcon

        original = tray_app.pystray
        try:
            tray_app.pystray = FakePystray
            app = tray_app.TrayApplication.__new__(tray_app.TrayApplication)
            app._build_icon(menu=None)
            assert len(attempts) == 1
        finally:
            tray_app.pystray = original


class TestFolderPickerAvailability:
    def test_pick_directory_checks_display_before_spawning(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无图形界面时直接返回，不去 spawn 子进程。

        tkinter 缺失的报错改由子进程（`_pick-directory`）上报 ——
        见 test_settings_api.TestPickDirectoryCliCommand。
        这里只锁住"显示检查在前"这一顺序，避免无谓地拉起进程。
        """
        import subprocess as sp

        from src.settings_service import SettingsService

        monkeypatch.setattr("src.tray_app.has_display", lambda: False)
        spawned: list[object] = []
        monkeypatch.setattr(sp, "run", lambda *a, **kw: spawned.append(a))

        result = SettingsService(context.config).pick_directory()
        assert result["ok"] is False
        assert "图形界面" in result["error"]
        assert spawned == [], "无显示时不应启动子进程"

    def test_pick_directory_reports_no_display(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.settings_service import SettingsService
        from src.tray_app import has_display

        if not has_display():
            result = SettingsService(context.config).pick_directory()
            assert result["ok"] is False
            assert "图形界面" in result["error"] or "不支持" in result["error"]
