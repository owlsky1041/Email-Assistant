"""设置界面 API 与服务测试。

重点覆盖**安全属性**：设置接口能改写配置并写入授权码，
比只读接口危险得多，防护必须可靠。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import AppConfig, load_config
from src.config_writer import write_default_config
from src.context import AppContext
from src.settings_api import (
    create_settings_router,
    is_loopback,
    load_setup_html,
    register_settings,
)
from src.settings_service import SettingsError, SettingsService

TOKEN = "setup-token-for-tests-0123456789"
HEADERS = {"X-Setup-Token": TOKEN}


@pytest.fixture
def service(context: AppContext) -> SettingsService:
    return SettingsService(context.config)


@pytest.fixture
def client(context: AppContext) -> TestClient:
    app = FastAPI()
    app.include_router(create_settings_router(context, token=TOKEN))
    return TestClient(app)


class TestLoopbackGuard:
    """最关键的一条：设置接口绝不能出现在非回环地址上。"""

    def test_loopback_detection(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost", "LOCALHOST"):
            assert is_loopback(host) is True
        for host in ("0.0.0.0", "192.168.1.5", "example.com", ""):
            assert is_loopback(host) is False

    def test_router_refuses_non_loopback(self, context: AppContext) -> None:
        context.config.api.host = "0.0.0.0"
        with pytest.raises(SettingsError, match="拒绝在非回环地址"):
            create_settings_router(context, token=TOKEN)

    def test_register_skips_non_loopback(self, context: AppContext) -> None:
        """挂载失败不应让整个服务起不来，只是没有设置能力。"""
        context.config.api.host = "0.0.0.0"
        app = FastAPI()
        assert register_settings(app, context) is None
        # 只应剩下 FastAPI 自带的文档路由，不应有任何设置接口
        assert not [p for p in client_paths(app) if p.startswith("/api/settings")
                    or p.startswith("/api/sync") or p == "/setup"]

    def test_register_returns_token_on_loopback(self, context: AppContext) -> None:
        app = FastAPI()
        token = register_settings(app, context)
        assert token and len(token) > 20

    def test_token_is_random_per_instance(self, context: AppContext) -> None:
        a = create_settings_router(context).setup_token  # type: ignore[attr-defined]
        b = create_settings_router(context).setup_token  # type: ignore[attr-defined]
        assert a != b


def client_paths(app: FastAPI) -> list[str]:
    return [r.path for r in app.routes if hasattr(r, "path")]


class TestTokenGuard:
    def test_missing_token_rejected(self, client: TestClient) -> None:
        assert client.get("/api/settings").status_code == 401

    def test_wrong_token_rejected(self, client: TestClient) -> None:
        res = client.get("/api/settings", headers={"X-Setup-Token": "wrong"})
        assert res.status_code == 401

    def test_valid_token_accepted(self, client: TestClient) -> None:
        assert client.get("/api/settings", headers=HEADERS).status_code == 200

    def test_all_write_endpoints_require_token(self, client: TestClient) -> None:
        for method, path, body in [
            ("PUT", "/api/settings", {"email": {"address": "x@y.com"}}),
            ("PUT", "/api/settings/auth-code", {"code": "abcdef123456"}),
            ("DELETE", "/api/settings/auth-code", None),
            ("POST", "/api/settings/test-connection", {}),
            ("POST", "/api/settings/pick-directory", {}),
            ("POST", "/api/settings/open-path", {}),
            ("POST", "/api/settings/migrate", {"new_root": "/tmp/x"}),
            ("POST", "/api/sync/start", {}),
            ("GET", "/api/sync/progress", None),
            ("GET", "/api/settings/folders", None),
        ]:
            res = client.request(method, path, json=body)
            assert res.status_code == 401, f"{method} {path} 未校验令牌"

    def test_setup_page_itself_needs_no_token_but_carries_it(self, client: TestClient) -> None:
        """页面本身开放（否则打不开），但令牌只对同源脚本可见。"""
        res = client.get("/setup")
        assert res.status_code == 200
        assert TOKEN in res.text


class TestCsrfProtection:
    def test_cross_origin_rejected(self, client: TestClient) -> None:
        res = client.get(
            "/api/settings",
            headers={**HEADERS, "Origin": "https://evil.example.com"},
        )
        assert res.status_code == 403

    def test_same_origin_allowed(self, client: TestClient) -> None:
        res = client.get(
            "/api/settings",
            headers={**HEADERS, "Origin": "http://testserver"},
        )
        assert res.status_code == 200

    def test_sec_fetch_cross_site_rejected(self, client: TestClient) -> None:
        res = client.get(
            "/api/settings", headers={**HEADERS, "Sec-Fetch-Site": "cross-site"}
        )
        assert res.status_code == 403

    def test_cross_origin_blocked_even_with_valid_token(self, client: TestClient) -> None:
        """双重防护：即使令牌泄露，跨站请求也进不来。"""
        res = client.put(
            "/api/settings",
            headers={**HEADERS, "Origin": "https://evil.example.com"},
            json={"email": {"address": "attacker@evil.com"}},
        )
        assert res.status_code == 403


class TestAuthCodeNeverLeaks:
    SECRET = "QQMailAuthCode1234567890"

    def test_read_omits_code(self, client: TestClient, context: AppContext,
                             monkeypatch: pytest.MonkeyPatch) -> None:
        # conftest 设了环境变量，而环境变量优先级高于密钥库；
        # 这里要测的是密钥库路径，因此先清掉
        monkeypatch.delenv(context.config.email.auth_code_env, raising=False)
        SettingsService(context.config).set_auth_code(self.SECRET)
        body = client.get("/api/settings", headers=HEADERS).json()
        assert self.SECRET not in str(body)
        assert body["auth"]["configured"] is True
        assert body["auth"]["length"] == len(self.SECRET)
        assert body["auth"]["backend"] in ("keyring", "encrypted-file")
        assert "code" not in body["auth"]

    def test_write_response_omits_code(self, client: TestClient) -> None:
        res = client.put(
            "/api/settings/auth-code", headers=HEADERS, json={"code": self.SECRET}
        )
        assert res.status_code == 200
        assert self.SECRET not in res.text
        assert res.json()["configured"] is True

    def test_code_not_written_to_config_file(self, client: TestClient, context: AppContext) -> None:
        client.put("/api/settings/auth-code", headers=HEADERS, json={"code": self.SECRET})
        config_file = context.config.source_path
        if config_file and Path(config_file).is_file():
            assert self.SECRET not in Path(config_file).read_text(encoding="utf-8")

    def test_code_too_short_rejected(self, client: TestClient) -> None:
        res = client.put("/api/settings/auth-code", headers=HEADERS, json={"code": "ab"})
        assert res.status_code == 422

    def test_empty_code_rejected(self, client: TestClient) -> None:
        res = client.put("/api/settings/auth-code", headers=HEADERS, json={"code": "  "})
        assert res.status_code == 422

    def test_delete_clears(self, client: TestClient, context: AppContext,
                           monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(context.config.email.auth_code_env, raising=False)
        SettingsService(context.config).set_auth_code(self.SECRET)
        assert client.delete("/api/settings/auth-code", headers=HEADERS).status_code == 200
        body = client.get("/api/settings", headers=HEADERS).json()
        assert body["auth"]["configured"] is False


class TestSettingsRead:
    def test_returns_all_sections(self, client: TestClient) -> None:
        body = client.get("/api/settings", headers=HEADERS).json()
        for section in ("email", "auth", "storage", "sync", "embedding",
                        "vector", "api", "log", "tray", "runtime"):
            assert section in body

    def test_storage_shows_resolved_paths(self, client: TestClient) -> None:
        body = client.get("/api/settings", headers=HEADERS).json()
        assert body["storage"]["resolved"]["sqlite"].endswith("mail.db")
        assert "archive_size_mb" in body["storage"]

    def test_runtime_reports_config_path(self, client: TestClient) -> None:
        body = client.get("/api/settings", headers=HEADERS).json()
        assert body["runtime"]["config_path"]


class TestSettingsWrite:
    def test_update_email(self, client: TestClient) -> None:
        res = client.put("/api/settings", headers=HEADERS, json={
            "email": {"address": "new@corp.com", "imap_port": 143, "use_ssl": False},
        })
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["settings"]["email"]["address"] == "new@corp.com"
        assert body["settings"]["email"]["imap_port"] == 143

    def test_persisted_to_file(self, client: TestClient, context: AppContext) -> None:
        client.put("/api/settings", headers=HEADERS, json={
            "email": {"address": "saved@corp.com"},
        })
        path = context.config.source_path
        assert path is not None
        parsed = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        assert parsed["email"]["address"] == "saved@corp.com"

    def test_invalid_value_rejected(self, client: TestClient, context: AppContext) -> None:
        res = client.put("/api/settings", headers=HEADERS, json={
            "email": {"imap_port": 99999},
        })
        assert res.status_code == 422
        # 拒绝后配置不能被改坏
        assert context.config.email.imap_port != 99999

    def test_unknown_section_rejected(self, client: TestClient) -> None:
        res = client.put("/api/settings", headers=HEADERS, json={"hacked": {"x": 1}})
        assert res.status_code == 422

    def test_empty_payload_rejected(self, client: TestClient) -> None:
        assert client.put("/api/settings", headers=HEADERS, json={}).status_code == 422

    def test_data_root_rewrites_all_storage_paths(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        target = tmp_path / "newroot"
        res = client.put("/api/settings", headers=HEADERS, json={"data_root": str(target)})
        assert res.status_code == 200
        storage = res.json()["settings"]["storage"]
        assert storage["sqlite_path"].startswith(str(target))
        assert storage["archive_dir"].startswith(str(target))
        assert storage["chroma_dir"].startswith(str(target))

    def test_read_exposes_fetch_workers(self, client: TestClient) -> None:
        """回归：read() 曾漏掉 fetch_workers，界面上的并发数会显示为空。"""
        body = client.get("/api/settings", headers=HEADERS).json()
        assert "fetch_workers" in body["sync"]
        assert isinstance(body["sync"]["fetch_workers"], int)

    def test_sync_settings_roundtrip(self, client: TestClient) -> None:
        res = client.put("/api/settings", headers=HEADERS, json={"sync": {
            "interval_minutes": 30,
            "fetch_workers": 5,
            "max_attachment_size_mb": 20.5,
            "exclude_folders": ["垃圾邮件", "Junk"],
        }})
        assert res.status_code == 200
        sync = res.json()["settings"]["sync"]
        assert sync["interval_minutes"] == 30
        assert sync["fetch_workers"] == 5
        assert sync["exclude_folders"] == ["垃圾邮件", "Junk"]

    def test_workers_out_of_range_rejected(self, client: TestClient) -> None:
        res = client.put("/api/settings", headers=HEADERS, json={"sync": {"fetch_workers": 99}})
        assert res.status_code == 422


class TestSettingsServiceUnit:
    def test_expand_data_root(self, tmp_path: Path) -> None:
        patch = SettingsService._expand_data_root(str(tmp_path))
        assert patch["storage"]["sqlite_path"] == str(tmp_path / "data/sqlite/mail.db")
        assert patch["log"]["dir"] == str(tmp_path / "logs")

    def test_write_rejects_non_dict_section(self, service: SettingsService) -> None:
        with pytest.raises(SettingsError, match="必须是对象"):
            service.write({"email": "not-a-dict"})

    def test_write_empty_rejected(self, service: SettingsService) -> None:
        with pytest.raises(SettingsError):
            service.write({})

    def test_migrate_same_dir_rejected(self, service: SettingsService, context: AppContext) -> None:
        from src.settings_service import _common_root

        result = service.migrate_data(str(_common_root(context.config)))
        assert result["ok"] is False
        assert "相同" in result["error"]

    def test_migrate_moves_data(self, service: SettingsService, context: AppContext,
                                tmp_path: Path) -> None:
        archive = context.config.archive_path
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "one.md").write_text("内容", encoding="utf-8")

        target = tmp_path / "moved"
        result = service.migrate_data(str(target))
        assert result["ok"] is True
        assert (target / "mail_archive" / "one.md").is_file()
        assert "mail_archive" in result["moved"]

    def test_test_connection_without_address(self, service: SettingsService,
                                             context: AppContext) -> None:
        context.config.email.address = ""
        result = service.test_connection()
        assert result["ok"] is False
        assert "邮箱地址" in result["error"]

    def test_test_connection_without_auth(self, service: SettingsService,
                                          context: AppContext, monkeypatch) -> None:
        monkeypatch.delenv(context.config.email.auth_code_env, raising=False)
        context.config.source_path = None  # 避免读到真实密钥库
        result = service.test_connection()
        assert result["ok"] is False

    def test_open_path_creates_missing_dir(self, service: SettingsService, tmp_path: Path) -> None:
        target = tmp_path / "made" / "up"
        result = service.open_path(str(target))
        assert target.is_dir()
        assert "path" in result


class TestSetupPage:
    def test_html_exists_and_has_token_placeholder(self, context: AppContext) -> None:
        html = load_setup_html("TOKEN123")
        assert "TOKEN123" in html
        assert "__SETUP_TOKEN__" not in html

    def test_page_has_no_external_resources(self) -> None:
        """设置页必须能离线打开：不能引用任何外部 CDN。"""
        html = load_setup_html("t")
        for bad in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr", "googleapis"):
            assert bad not in html

    def test_page_mentions_required_fields(self) -> None:
        html = load_setup_html("t")
        for label in ("IMAP 服务器", "授权码", "数据根目录", "并发下载连接数", "同步进度"):
            assert label in html

    def test_page_reads_token_from_injection(self) -> None:
        assert 'const TOKEN = "__SETUP_TOKEN__";' in load_setup_html("__SETUP_TOKEN__")


class TestSettingsAvailabilityIsVisible:
    """设置界面不可用时必须能一眼看出原因。

    回归背景：用户打开 /setup 只拿到 {"detail":"Not Found"}，
    而原因（页面没打进包 / 地址非回环）只留在日志里，排查成本极高。
    """

    def test_status_recorded_when_enabled(self, context: AppContext) -> None:
        app = FastAPI()
        token = register_settings(app, context)
        status = app.state.settings_status
        assert token
        assert status["enabled"] is True
        assert status["reason"] == "ok"
        assert status["url"].endswith("/setup")

    def test_status_recorded_when_non_loopback(self, context: AppContext) -> None:
        context.config.api.host = "0.0.0.0"
        app = FastAPI()
        assert register_settings(app, context) is None
        status = app.state.settings_status
        assert status["enabled"] is False
        assert "回环" in status["reason"]

    def test_status_recorded_when_page_missing(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """页面没打进包时也要报出明确原因，而不是等用户打开才 500。"""
        import src.settings_api as api_module

        monkeypatch.setattr(api_module, "WEBUI_DIR", tmp_path / "nowhere")
        app = FastAPI()
        assert register_settings(app, context) is None
        status = app.state.settings_status
        assert status["enabled"] is False
        assert "settings.html" in status["reason"]

    def test_health_endpoint_exposes_status(self, context: AppContext) -> None:
        from src.kb_api import create_app

        app = create_app(context)
        with TestClient(app) as c:
            body = c.get("/api/health").json()
        assert "settings_ui" in body
        assert body["settings_ui"]["enabled"] is True
        assert body["settings_ui"]["url"].endswith("/setup")

    def test_health_reports_disabled_reason(self, context: AppContext) -> None:
        from src.kb_api import create_app

        context.config.api.host = "0.0.0.0"
        app = create_app(context)
        with TestClient(app) as c:
            body = c.get("/api/health").json()
        assert body["settings_ui"]["enabled"] is False
        assert body["settings_ui"]["reason"]


class TestFolderPickerViaSubprocess:
    """回归：目录选择框必须跑在**独立进程**里。

    tkinter 的对话框要求在主线程运行。设置接口的调用发生在 FastAPI 的
    工作线程中，在那里创建 Tk 窗口**不会显示**（实测：进程一直卡住，
    窗口列表里什么都没有）。所以必须 spawn 子进程 ——
    这一点在所有平台上都成立，不只是 Linux。
    """

    def test_uses_subprocess_not_inprocess_tkinter(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess as sp

        from src import settings_service

        monkeypatch.setattr(settings_service, "has_display", lambda: True, raising=False)
        monkeypatch.setattr(
            "src.tray_app.has_display", lambda: True
        )

        calls: list[list[str]] = []

        class FakeCompleted:
            returncode = 0
            stderr = b""

        picked = str(Path(tempfile.gettempdir()) / "选中的目录")

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            # 模拟用户选中了目录：子进程把结果写进 --out 指定的文件
            out = Path(cmd[cmd.index("--out") + 1])
            out.write_text(picked, encoding="utf-8")
            return FakeCompleted()

        monkeypatch.setattr(sp, "run", fake_run)

        result = SettingsService(context.config).pick_directory(
            initial=tempfile.gettempdir()
        )
        assert result["ok"] is True
        # 返回值会经 Path() 规范化，各平台分隔符不同，比语义而不是比字面量
        assert Path(result["path"]) == Path(picked)
        assert calls, "应当通过子进程执行，而不是在当前进程里调用 tkinter"
        assert "_pick-directory" in calls[0]

    def test_reports_cancel(self, context: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        from src import settings_service

        monkeypatch.setattr("src.tray_app.has_display", lambda: True)

        class FakeCompleted:
            returncode = 0
            stderr = b""

        def fake_run(cmd, **kwargs):
            Path(cmd[cmd.index("--out") + 1]).write_text("", encoding="utf-8")
            return FakeCompleted()

        monkeypatch.setattr(sp, "run", fake_run)
        result = SettingsService(context.config).pick_directory()
        assert result["ok"] is False
        assert result["cancelled"] is True

    def test_reports_timeout(self, context: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        from src import settings_service

        monkeypatch.setattr("src.tray_app.has_display", lambda: True)

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(sp, "run", fake_run)
        result = SettingsService(context.config).pick_directory()
        assert result["ok"] is False
        assert "超时" in result["error"]

    def test_reports_subprocess_failure(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess as sp

        from src import settings_service

        monkeypatch.setattr("src.tray_app.has_display", lambda: True)

        class FakeCompleted:
            returncode = 3
            stderr = "缺少 tkinter".encode("utf-8")

        def fake_run(cmd, **kwargs):
            Path(cmd[cmd.index("--out") + 1]).write_text("", encoding="utf-8")
            return FakeCompleted()

        monkeypatch.setattr(sp, "run", fake_run)
        result = SettingsService(context.config).pick_directory()
        assert result["ok"] is False
        assert "tkinter" in result["error"]

    def test_temp_file_cleaned_up(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess as sp

        from src import settings_service

        monkeypatch.setattr("src.tray_app.has_display", lambda: True)
        seen: list[Path] = []

        class FakeCompleted:
            returncode = 0
            stderr = b""

        def fake_run(cmd, **kwargs):
            out = Path(cmd[cmd.index("--out") + 1])
            seen.append(out)
            out.write_text("/tmp/x", encoding="utf-8")
            return FakeCompleted()

        monkeypatch.setattr(sp, "run", fake_run)
        SettingsService(context.config).pick_directory()
        assert seen and not seen[0].exists(), "临时文件应当被清理"

    def test_no_display_returns_clear_error(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.tray_app.has_display", lambda: False)
        result = SettingsService(context.config).pick_directory()
        assert result["ok"] is False
        assert "图形界面" in result["error"]


class TestPickDirectoryCliCommand:
    """内部命令 `_pick-directory` 必须存在且把结果写进文件。"""

    def test_command_registered(self) -> None:
        from src.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["_pick-directory", "--out", "/tmp/x.txt"])
        assert args.out == "/tmp/x.txt"
        assert args.func.__name__ == "cmd_pick_directory"

    def test_requires_out_argument(self) -> None:
        from src.cli import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["_pick-directory"])

    def test_writes_empty_on_missing_tkinter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没有 tkinter 时必须写出空文件并给出可读错误，而不是让父进程挂住。"""
        import builtins

        from src import cli

        out = tmp_path / "result.txt"
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("tkinter"):
                raise ImportError("No module named 'tkinter'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        # 用 SimpleNamespace：类体里赋值会遮蔽外层同名变量
        import types

        args = types.SimpleNamespace(out=str(out), initial="")
        rc = cli.cmd_pick_directory(args)  # type: ignore[arg-type]
        assert rc == 3
        assert out.read_text(encoding="utf-8") == ""
